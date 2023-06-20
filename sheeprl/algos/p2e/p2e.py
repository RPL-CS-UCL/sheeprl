import os
import pathlib
import time
from dataclasses import asdict
from datetime import datetime
from typing import List, Optional

import gymnasium as gym
import numpy as np
import torch
import torch.nn.functional as F
from lightning.fabric import Fabric
from lightning.fabric.fabric import _is_using_cli
from lightning.fabric.loggers import TensorBoardLogger
from lightning.fabric.plugins.collectives import TorchCollective
from lightning.fabric.wrappers import _FabricModule, _FabricOptimizer
from lightning.pytorch.utilities.seed import isolate_rng
from tensordict import TensorDict
from tensordict.tensordict import TensorDictBase
from torch import nn
from torch.distributions import Bernoulli, Independent, Normal
from torch.optim import Adam, Optimizer
from torch.utils.data import BatchSampler
from torchmetrics import MeanMetric

from sheeprl.algos.dreamer_v1.agent import Player, WorldModel, build_models
from sheeprl.algos.dreamer_v1.dreamer_v1 import train
from sheeprl.algos.dreamer_v1.loss import actor_loss, critic_loss, reconstruction_loss
from sheeprl.algos.dreamer_v1.utils import cnn_forward, compute_lambda_values, init_weights, make_env, test
from sheeprl.algos.p2e.args import P2EArgs
from sheeprl.data.buffers import SequentialReplayBuffer
from sheeprl.models.models import MLP
from sheeprl.utils.callback import CheckpointCallback
from sheeprl.utils.metric import MetricAggregator
from sheeprl.utils.parser import HfArgumentParser
from sheeprl.utils.registry import register_algorithm
from sheeprl.utils.utils import polynomial_decay

os.environ["MUJOCO_GL"] = "osmesa"
os.environ["MINEDOJO_HEADLESS"] = "1"


def train(
    fabric: Fabric,
    world_model: WorldModel,
    actor: _FabricModule,
    critic: _FabricModule,
    world_optimizer: _FabricOptimizer,
    actor_optimizer: _FabricOptimizer,
    critic_optimizer: _FabricOptimizer,
    data: TensorDictBase,
    aggregator: MetricAggregator,
    args: P2EArgs,
    encoder_output_size: int,
    ensembles: _FabricModule,
    ensemble_optimizer: _FabricOptimizer,
) -> None:
    batch_size = args.per_rank_batch_size
    sequence_length = args.per_rank_sequence_length
    observation_shape = data["observations"].shape[-3:]
    device = fabric.device
    batch_obs = data["observations"] / 255 - 0.5

    # Dynamic Learning
    # initialize the recurrent_state that must be a tuple of tensors (one for GRU or RNN).
    # the dimension of each vector must be (1, batch_size, recurrent_state_size)
    # the recurrent state is the deterministic state (or ht) from the Figure 2c in [https://arxiv.org/abs/1811.04551](https://arxiv.org/abs/1811.04551)
    recurrent_state = torch.zeros(1, batch_size, args.recurrent_state_size, device=device)

    # initialize the stochastic_state that must be of dimension (batch_size, 1, stochastic_size)
    # the stochastic state is the stochastic state (or st) from the Figure 2c in [https://arxiv.org/abs/1811.04551](https://arxiv.org/abs/1811.04551)
    stochastic_state = torch.zeros(1, batch_size, args.stochastic_size, device=device)

    # initialize the tensors for dynamic learning
    # recurrent_states will contain all the recurrent states computed during the dynamic learning phase,
    # and its dimension is (sequence_length, batch_size, recurrent_state_size)
    recurrent_states = torch.empty(sequence_length, batch_size, args.recurrent_state_size, device=device)
    # stochastic_states will contain all the stochastic states computed during the dynamic learning phase,
    # and its dimension is (sequence_length, batch_size, stochastic_size)
    stochastic_states = torch.empty(sequence_length, batch_size, args.stochastic_size, device=device)

    # states_mean and states_std will contain all the actual means and stds of the sthocastic states respectively,
    # their dimension is (sequence_length, batch_size, stochastic_size)
    states_mean = torch.empty(sequence_length, batch_size, args.stochastic_size, device=device)
    states_std = torch.empty(sequence_length, batch_size, args.stochastic_size, device=device)

    # pred_states_mean and pred_states_std will contain all the predicted means and stds of the sthocastic states respectively,
    # their dimension is (sequence_length, batch_size, stochastic_size)
    pred_states_mean = torch.empty(sequence_length, batch_size, args.stochastic_size, device=device)
    pred_states_std = torch.empty(sequence_length, batch_size, args.stochastic_size, device=device)

    embedded_obs = cnn_forward(world_model.encoder, batch_obs, observation_shape, (-1,))

    for i in range(0, sequence_length):
        # one step of dynamic learning, take the stochastic state, the recurrent state, the action, and the observation
        # compute the actual mean and std of the stochastic state, the new recurrent state, the new stochastic state,
        # and the predicted mean and std of the stochastic state
        state_mean_std, recurrent_state, stochastic_state, pred_state_mean_std = world_model.rssm.dynamic(
            stochastic_state, recurrent_state, data["actions"][i : i + 1], embedded_obs[i : i + 1]
        )
        recurrent_states[i] = recurrent_state
        stochastic_states[i] = stochastic_state
        states_mean[i] = state_mean_std[0]
        states_std[i] = state_mean_std[1]
        pred_states_mean[i] = pred_state_mean_std[0]
        pred_states_std[i] = pred_state_mean_std[1]

    # concatenate the stochastic states with the recurrent states on the last dimension
    # latent_states tensor has dimension (sequence_length, batch_size, recurrent_state_size + stochastic_size)
    latent_states = torch.cat((stochastic_states, recurrent_states), -1)

    # compute predictions for the observations
    decoded_information = cnn_forward(
        world_model.observation_model, latent_states, (latent_states.shape[-1],), observation_shape
    )
    # compute the distribution of the reconstructed observations
    # it is necessary an Independent distribution because
    # it is necessary to create (batch_size * sequence_length) independent distributions,
    # each producing a sample of size observations.shape
    qo = Independent(Normal(decoded_information, 1), len(observation_shape))

    # compute predictions for the rewards
    # it is necessary an Independent distribution because
    # it is necessary to create (batch_size * sequence_length) independent distributions,
    # each producing a sample of size equal to the number of rewards
    qr = Independent(Normal(world_model.reward_model(latent_states), 1), 1)

    # compute predictions for terminal steps, if required
    if args.use_continues and world_model.continue_model:
        qc = Bernoulli(logits=world_model.continue_model(latent_states))
        continue_targets = (1 - data["dones"]) * args.gamma
    else:
        qc = continue_targets = None

    # compute the distributions of the states (actual and predicted)
    # it is necessary an Independent distribution because
    # it is necessary to create (batch_size * sequence_length) independent distributions,
    # each producing a sample of size equal to the stochastic size
    p = Independent(Normal(states_mean, states_std), 1)
    q = Independent(Normal(pred_states_mean, pred_states_std), 1)

    # world model optimization step
    world_optimizer.zero_grad(set_to_none=True)
    # compute the overall loss of the world model
    rec_loss, state_loss, reward_loss, observation_loss, continue_loss = reconstruction_loss(
        qo,
        batch_obs,
        qr,
        data["rewards"],
        p,
        q,
        args.kl_free_nats,
        args.kl_regularizer,
        qc,
        continue_targets,
        args.continue_scale_factor,
    )
    fabric.backward(rec_loss)
    if args.clip_gradients is not None and args.clip_gradients > 0:
        fabric.clip_gradients(module=world_model, optimizer=world_optimizer, max_norm=args.clip_gradients)
    world_optimizer.step()
    aggregator.update("Loss/reconstruction_loss", rec_loss.detach())
    aggregator.update("Loss/observation_loss", observation_loss.detach())
    aggregator.update("Loss/reward_loss", reward_loss.detach())
    aggregator.update("Loss/state_loss", state_loss.detach())
    aggregator.update("Loss/continue_loss", continue_loss.detach())
    aggregator.update("State/p_entropy", p.entropy().mean().detach())
    aggregator.update("State/q_entropy", q.entropy().mean().detach())

    # Ensemble
    loss = 0
    ensemble_optimizer.zero_grad(set_to_none=True)
    for i, ens in enumerate(ensembles):
        out = ens(latent_states.detach(), data["actions"].detach())[:-1]
        next_obs_embedding_dist = Independent(Normal(out, 1), 1)
        loss -= next_obs_embedding_dist.log_prob(stochastic_states.detach()[1:]).mean()
    loss.backward()
    fabric.clip_gradients(module=ens, optimizer=ensemble_optimizer, max_norm=args.ensemble_clip_gradients)
    ensemble_optimizer.step()
    aggregator.update(f"Ensemble/loss_{i}", loss.detach().cpu())

    # Behaviour Learning
    # unflatten first 2 dimensions of recurernt and stochastic states in order to have all the states on the first dimension.
    # The 1 in the second dimension is needed for the recurrent model in the imagination step,
    # 1 because the agent imagines one state at a time.
    # (1, batch_size * sequence_length, stochastic_size)
    imagined_stochastic_state = stochastic_states.detach().reshape(1, -1, args.stochastic_size)

    # initialize the recurrent state of the recurrent model with the recurrent states computed
    # during the dynamic learning phase, its shape is (1, batch_size * sequence_length, recurrent_state_size).
    recurrent_state = recurrent_states.detach().reshape(1, -1, args.recurrent_state_size)

    # (1, batch_size * sequence_length, determinisitic_size + stochastic_size)
    imagined_latent_states = torch.cat((imagined_stochastic_state, recurrent_state), -1)

    # initialize the tensor of the imagined states
    imagined_trajectories = torch.empty(
        args.horizon, batch_size * sequence_length, args.stochastic_size + args.recurrent_state_size, device=device
    )

    all_actions = torch.zeros(args.horizon, batch_size * sequence_length, data["actions"].shape[-1], device=device)

    # imagine trajectories in the latent space
    for i in range(args.horizon):
        # actions tensor has dimension (1, batch_size * sequence_length, num_actions)
        actions = actor(imagined_latent_states.detach())
        all_actions[0] = actions

        # imagination step
        imagined_stochastic_state, recurrent_state = world_model.rssm.imagination(
            imagined_stochastic_state, recurrent_state, actions
        )

        # update current state
        imagined_latent_states = torch.cat((imagined_stochastic_state, recurrent_state), -1)
        imagined_trajectories[i] = imagined_latent_states

    # predict values and rewards
    # it is necessary an Independent distribution because
    # it is necessary to create (batch_size * sequence_length) independent distributions,
    # each producing a sample of size equal to the number of values/rewards
    predicted_values = Independent(Normal(critic(imagined_trajectories[1:]), 1), 1).mean

    # Predict intrinsic reward
    next_obs_embedding = torch.zeros(
        len(ensembles),
        args.horizon - 1,
        batch_size * sequence_length,
        encoder_output_size,
        device=device,
    )
    for i, ens in enumerate(ensembles):
        next_obs_embedding[i] = ens(imagined_trajectories.detach(), all_actions.detach())[:-1]

    # next_obs_embedding -> N_ensemble x Horizon x Batch_size*Seq_len x Obs_embedding_size
    intrinsic_reward = next_obs_embedding.var(0).mean(-1, keepdim=True) * args.intrinsic_reward_multiplier
    aggregator.update("Rewards/intrinsic", intrinsic_reward.detach().cpu().mean())

    if args.use_continues and world_model.continue_model:
        done_mask = Independent(Bernoulli(logits=world_model.continue_model(imagined_trajectories)), 1).mean
    else:
        done_mask = torch.ones_like(intrinsic_reward.detach()) * args.gamma

    # compute the lambda_values, by passing as last values the values of the last imagined state
    # the dimensions of the lambda_values tensor are
    # (horizon, batch_size * sequence_length, recurrent_state_size + stochastic_size)
    lambda_values = compute_lambda_values(
        intrinsic_reward,
        predicted_values,
        done_mask,
        last_values=predicted_values[-1],
        horizon=args.horizon - 1,
        lmbda=args.lmbda,
    )

    # compute the discounts to multiply to the lambda values
    with torch.no_grad():
        # the time steps in Eq. 7 and Eq. 8 of the paper are weighted by the cumulative product of the predicted
        # discount factors, estimated by the continue model, so terms are wighted down based on how likely
        # the imagined trajectory would have ended.
        # Ref. subsection "Learning objectives" of paragraph 3 (Learning Behaviors by Latent Imagination)
        # in [https://doi.org/10.48550/arXiv.1912.01603](https://doi.org/10.48550/arXiv.1912.01603)
        #
        # Suppose the case in which the continue model is not used and gamma = .99
        # done_mask.shape = (15, 2500, 1)
        # done_mask = [
        #   [ [.99], ..., [.99] ], (2500 columns)
        #   ...
        # ] (15 rows)
        # torch.ones_like(done_mask[:1]) = [
        #   [ [1.], ..., [1.] ]
        # ] (1 row and 2500 columns), the discount of the time step 0 is 1.
        # done_mask[:-2] = [
        #   [ [.99], ..., [.99] ], (2500 columns)
        #   ...
        # ] (13 rows)
        # torch.cat((torch.ones_like(done_mask[:1]), done_mask[:-2]), 0) = [
        #   [ [1.], ..., [1.] ], (2500 columns)
        #   [ [.99], ..., [.99] ],
        #   ...,
        #   [ [.99], ..., [.99] ],
        # ] (14 rows), the total number of imagined steps is 15, but one is lost because of the values computation
        # torch.cumprod(torch.cat((torch.ones_like(done_mask[:1]), done_mask[:-2]), 0), 0) = [
        #   [ [1.], ..., [1.] ], (2500 columns)
        #   [ [.99], ..., [.99] ],
        #   [ [.9801], ..., [.9801] ],
        #   ...,
        #   [ [.8775], ..., [.8775] ],
        # ] (14 rows)
        discount = torch.cumprod(torch.cat((torch.ones_like(done_mask[:1]), done_mask[:-2]), 0), 0)

    # actor optimization step
    actor_optimizer.zero_grad(set_to_none=True)
    # compute the policy loss
    policy_loss = actor_loss(discount * lambda_values)
    fabric.backward(policy_loss)
    if args.clip_gradients is not None and args.clip_gradients > 0:
        fabric.clip_gradients(module=actor, optimizer=actor_optimizer, max_norm=args.clip_gradients)
    actor_optimizer.step()
    aggregator.update("Loss/policy_loss", policy_loss.detach())

    # predict the values distribution only for the first H (horizon) imagined states (to match the dimension with the lambda values),
    # it removes the last imagined state in the trajectory because it is used only for compuing correclty the lambda values
    qv = Independent(Normal(critic(imagined_trajectories.detach())[1:-1], 1), 1)

    # critic optimization step
    critic_optimizer.zero_grad(set_to_none=True)
    # compute the value loss
    # the discount has shape (horizon, seuqence_length * batch_size, 1), so,
    # it is necessary to remove the last dimension properly match the shapes
    # for the log prob
    value_loss = critic_loss(qv, lambda_values.detach(), discount[..., 0])
    fabric.backward(value_loss)
    if args.clip_gradients is not None and args.clip_gradients > 0:
        fabric.clip_gradients(module=critic, optimizer=critic_optimizer, max_norm=args.clip_gradients)
    critic_optimizer.step()
    aggregator.update("Loss/value_loss", value_loss.detach())

    # Reset everything
    actor_optimizer.zero_grad(set_to_none=True)
    critic_optimizer.zero_grad(set_to_none=True)
    world_optimizer.zero_grad(set_to_none=True)
    ensemble_optimizer.zero_grad(set_to_none=True)


@register_algorithm()
def main():
    parser = HfArgumentParser(P2EArgs)
    args: P2EArgs = parser.parse_args_into_dataclasses()[0]
    args.num_envs = 1
    torch.set_num_threads(1)

    # Initialize Fabric
    fabric = Fabric(callbacks=[CheckpointCallback()])
    if not _is_using_cli():
        fabric.launch()
    rank = fabric.global_rank
    device = fabric.device
    fabric.seed_everything(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic

    if args.checkpoint_path:
        state = fabric.load(args.checkpoint_path)
        state["args"]["checkpoint_path"] = args.checkpoint_path
        args = P2EArgs(**state["args"])
        args.per_rank_batch_size = state["batch_size"] // fabric.world_size
        ckpt_path = pathlib.Path(args.checkpoint_path)

    # Set logger only on rank-0 but share the logger directory: since we don't know
    # what is happening during the `fabric.save()` method, at least we assure that all
    # ranks save under the same named folder.
    # As a plus, rank-0 sets the time uniquely for everyone
    world_collective = TorchCollective()
    if fabric.world_size > 1:
        world_collective.setup()
        world_collective.create_group()
    if rank == 0:
        root_dir = (
            args.root_dir
            if args.root_dir is not None
            else os.path.join("logs", "dreamer_v1", datetime.today().strftime("%Y-%m-%d_%H-%M-%S"))
        )
        run_name = (
            args.run_name
            if args.run_name is not None
            else f"{args.env_id}_{args.exp_name}_{args.seed}_{int(time.time())}"
        )
        if args.checkpoint_path:
            root_dir = ckpt_path.parent.parent
            run_name = "resume_from_checkpoint"
        logger = TensorBoardLogger(root_dir=root_dir, name=run_name)
        fabric._loggers = [logger]
        log_dir = logger.log_dir
        fabric.logger.log_hyperparams(asdict(args))
        if fabric.world_size > 1:
            world_collective.broadcast_object_list([log_dir], src=0)
    else:
        data = [None]
        world_collective.broadcast_object_list(data, src=0)
        log_dir = data[0]
        os.makedirs(log_dir, exist_ok=True)

    env: gym.Env = make_env(
        args.env_id,
        args.seed + rank * args.num_envs,
        rank,
        args,
        logger.log_dir if rank == 0 else None,
        "train",
    )

    is_continuous = isinstance(env.action_space, gym.spaces.Box)
    action_dim = env.action_space.shape[0] if is_continuous else env.action_space.n
    observation_shape = env.observation_space.shape
    clip_rewards_fn = lambda r: torch.tanh(r) if args.clip_rewards else r

    world_model, actor, critic, info = build_models(
        fabric,
        action_dim,
        observation_shape,
        is_continuous,
        args,
        state["world_model"] if args.checkpoint_path else None,
        state["actor"] if args.checkpoint_path else None,
        state["critic"] if args.checkpoint_path else None,
    )
    ens_list = []
    with isolate_rng():
        for i in range(5):
            fabric.seed_everything(args.seed + i)
            ens_list.append(
                MLP(
                    input_dims=action_dim + args.recurrent_state_size + args.stochastic_size,
                    output_dim=args.stochastic_size,
                    hidden_sizes=[args.dense_units] * args.num_layers,
                ).apply(init_weights)
            )
    ensembles = nn.ModuleList(ens_list)
    if args.checkpoint_path:
        ensembles.load_state_dict(state["ensembles"])
    fabric.setup_module(ensembles)
    player = Player(
        world_model.encoder.module,
        world_model.rssm.recurrent_model.module,
        world_model.rssm.representation_model.module,
        actor.module,
        action_dim,
        args.expl_amount,
        args.num_envs,
        args.stochastic_size,
        args.recurrent_state_size,
        fabric.device,
    )

    # Optimizers
    world_optimizer = Adam(world_model.parameters(), eps=1e-5, lr=args.world_lr, weight_decay=1e-6)
    actor_optimizer = Adam(actor.parameters(), eps=1e-5, lr=args.actor_lr, weight_decay=1e-6)
    critic_optimizer = Adam(critic.parameters(), eps=1e-5, lr=args.critic_lr, weight_decay=1e-6)
    ensemble_optimizer = Adam(ensembles.parameters(), eps=args.ensemble_eps, lr=args.ensemble_lr, weight_decay=1e-6)
    if args.checkpoint_path:
        world_optimizer.load_state_dict(state["world_optimizer"])
        actor_optimizer.load_state_dict(state["actor_optimizer"])
        critic_optimizer.load_state_dict(state["critic_optimizer"])
        ensemble_optimizer.load_state_dict(state["ensemble_optimizer"])
    world_optimizer, actor_optimizer, critic_optimizer, ensemble_optimizer = fabric.setup_optimizers(
        world_optimizer, actor_optimizer, critic_optimizer, ensemble_optimizer
    )

    # Metrics
    with device:
        aggregator = MetricAggregator(
            {
                "Rewards/rew_avg": MeanMetric(sync_on_compute=False),
                "Game/ep_len_avg": MeanMetric(sync_on_compute=False),
                "Time/step_per_second": MeanMetric(sync_on_compute=False),
                "Loss/reconstruction_loss": MeanMetric(sync_on_compute=False),
                "Loss/value_loss": MeanMetric(sync_on_compute=False),
                "Loss/policy_loss": MeanMetric(sync_on_compute=False),
                "Loss/observation_loss": MeanMetric(sync_on_compute=False),
                "Loss/reward_loss": MeanMetric(sync_on_compute=False),
                "Loss/state_loss": MeanMetric(sync_on_compute=False),
                "Loss/continue_loss": MeanMetric(sync_on_compute=False),
                "State/p_entropy": MeanMetric(sync_on_compute=False),
                "State/q_entropy": MeanMetric(sync_on_compute=False),
                "Params/exploration_amout": MeanMetric(sync_on_compute=False),
                "Ensemble/loss_0": MeanMetric(sync_on_compute=False),
                "Ensemble/loss_1": MeanMetric(sync_on_compute=False),
                "Ensemble/loss_2": MeanMetric(sync_on_compute=False),
                "Ensemble/loss_3": MeanMetric(sync_on_compute=False),
                "Ensemble/loss_4": MeanMetric(sync_on_compute=False),
                "Rewards/intrinsic": MeanMetric(sync_on_compute=False),
            }
        )

    # Local data
    buffer_size = (
        args.buffer_size // int(args.num_envs * fabric.world_size * args.action_repeat) if not args.dry_run else 2
    )
    rb = SequentialReplayBuffer(buffer_size, args.num_envs, device="cpu", memmap=args.memmap_buffer)
    if args.checkpoint_path and args.checkpoint_buffer:
        if isinstance(state["rb"], list) and fabric.world_size == len(state["rb"]):
            rb = state["rb"][fabric.global_rank]
        elif isinstance(state["rb"], SequentialReplayBuffer):
            rb = state["rb"]
        else:
            raise RuntimeError(f"Given {len(state['rb'])}, but {fabric.world_size} processes are instantiated")
    step_data = TensorDict({}, batch_size=[args.num_envs], device="cpu")
    expl_decay_steps = state["expl_decay_steps"] if args.checkpoint_path else 0

    # Global variables
    start_time = time.perf_counter()
    start_step = state["global_step"] // fabric.world_size if args.checkpoint_path else 1
    step_before_training = args.train_every // (fabric.world_size * args.action_repeat) if not args.dry_run else 0
    num_updates = int(args.total_steps // (fabric.world_size * args.action_repeat)) if not args.dry_run else 1
    learning_starts = (args.learning_starts // (fabric.world_size * args.action_repeat)) if not args.dry_run else 0
    if args.checkpoint_path and not args.checkpoint_buffer:
        learning_starts = start_step + args.learning_starts // int(fabric.world_size * args.action_repeat)
    max_step_expl_decay = args.max_step_expl_decay // (args.gradient_steps * fabric.world_size)
    if args.checkpoint_path:
        player.expl_amount = polynomial_decay(
            expl_decay_steps,
            initial=args.expl_amount,
            final=args.expl_min,
            max_decay_steps=max_step_expl_decay,
        )

    # Get the first environment observation and start the optimization
    obs = torch.from_numpy(env.reset(seed=args.seed)[0]).view(args.num_envs, *observation_shape)  # [N_envs, N_obs]
    step_data["dones"] = torch.zeros(args.num_envs, 1)
    step_data["actions"] = torch.zeros(args.num_envs, action_dim)
    step_data["rewards"] = torch.zeros(args.num_envs, 1)
    step_data["observations"] = obs
    rb.add(step_data[None, ...])
    player.init_states()

    for global_step in range(start_step, num_updates + 1):
        # Sample an action given the observation received by the environment
        if global_step < learning_starts and args.checkpoint_path is None:
            real_actions = actions = np.array(env.action_space.sample())
            if not is_continuous:
                actions = F.one_hot(torch.tensor(actions), action_dim).numpy()
        else:
            with torch.no_grad():
                real_actions = actions = player.get_exploration_action(
                    obs[None, ...].to(device) / 255 - 0.5, is_continuous
                )
                actions = actions.cpu().numpy()
                real_actions = real_actions.cpu().numpy()
                if is_continuous:
                    real_actions = real_actions.reshape(action_dim)
                else:
                    real_actions = real_actions.argmax()
        next_obs, rewards, dones, truncated, infos = env.step(real_actions)
        dones = np.logical_or(dones, truncated)

        if (dones or truncated) and "episode" in infos:
            fabric.print(f"Rank-0: global_step={global_step}, reward_env_{0}={infos['episode']['r'][0]}")
            aggregator.update("Rewards/rew_avg", infos["episode"]["r"][0])
            aggregator.update("Game/ep_len_avg", infos["episode"]["l"][0])

        next_obs = torch.from_numpy(next_obs).view(args.num_envs, *observation_shape)
        actions = torch.from_numpy(actions).view(args.num_envs, -1).float()
        rewards = torch.tensor([rewards]).view(args.num_envs, -1).float()
        dones = torch.tensor([bool(dones)]).view(args.num_envs, -1).float()

        # next_obs becomes the new obs
        obs = next_obs

        step_data["dones"] = dones
        step_data["actions"] = actions
        step_data["observations"] = obs
        step_data["rewards"] = clip_rewards_fn(rewards)
        rb.add(step_data[None, ...])

        if dones or truncated:
            obs = torch.from_numpy(env.reset(seed=args.seed)[0]).view(
                args.num_envs, *observation_shape
            )  # [N_envs, N_obs]
            step_data["dones"] = torch.zeros(args.num_envs, 1)
            step_data["actions"] = torch.zeros(args.num_envs, action_dim)
            step_data["rewards"] = torch.zeros(args.num_envs, 1)
            step_data["observations"] = obs
            rb.add(step_data[None, ...])
            player.init_states()

        step_before_training -= 1

        # Train the agent
        if global_step > learning_starts and step_before_training <= 0:
            fabric.barrier()
            local_data = rb.sample(
                args.per_rank_batch_size,
                sequence_length=args.per_rank_sequence_length,
                n_samples=args.gradient_steps,
            ).to(device)
            distributed_sampler = BatchSampler(range(local_data.shape[0]), batch_size=1, drop_last=False)
            for i in distributed_sampler:
                train(
                    fabric,
                    world_model,
                    actor,
                    critic,
                    world_optimizer,
                    actor_optimizer,
                    critic_optimizer,
                    local_data[i].view(args.per_rank_sequence_length, args.per_rank_batch_size),
                    aggregator,
                    args,
                    encoder_output_size=info["encoder_output_size"],
                    ensembles=ensembles,
                    ensemble_optimizer=ensemble_optimizer,
                )
            step_before_training = args.train_every // (args.num_envs * fabric.world_size * args.action_repeat)
            if args.expl_decay:
                expl_decay_steps += 1
                player.expl_amount = polynomial_decay(
                    expl_decay_steps,
                    initial=args.expl_amount,
                    final=args.expl_min,
                    max_decay_steps=max_step_expl_decay,
                )
            aggregator.update("Params/exploration_amout", player.expl_amount)
        aggregator.update("Time/step_per_second", int(global_step / (time.perf_counter() - start_time)))
        fabric.log_dict(aggregator.compute(), global_step)
        aggregator.reset()

        # Checkpoint Model
        if (
            (args.checkpoint_every > 0 and global_step % args.checkpoint_every == 0)
            or args.dry_run
            or global_step == num_updates
        ):
            state = {
                "world_model": world_model.state_dict(),
                "actor": actor.state_dict(),
                "critic": critic.state_dict(),
                "ensembles": ensembles.state_dict(),
                "world_optimizer": world_optimizer.state_dict(),
                "actor_optimizer": actor_optimizer.state_dict(),
                "critic_optimizer": critic_optimizer.state_dict(),
                "ensemble_optimizer": ensemble_optimizer.state_dict(),
                "expl_decay_steps": expl_decay_steps,
                "args": asdict(args),
                "global_step": global_step * fabric.world_size,
                "batch_size": args.per_rank_batch_size * fabric.world_size,
            }
            ckpt_path = log_dir + f"/checkpoint/ckpt_{global_step}_{fabric.global_rank}.ckpt"
            fabric.call(
                "on_checkpoint_coupled",
                fabric=fabric,
                ckpt_path=ckpt_path,
                state=state,
                replay_buffer=rb if args.checkpoint_buffer else None,
            )

    env.close()
    if fabric.is_global_zero:
        test(player, fabric, args)


if __name__ == "__main__":
    main()
