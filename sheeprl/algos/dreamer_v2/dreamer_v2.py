"""Dreamer-V2 implementation from [https://arxiv.org/abs/2010.02193](https://arxiv.org/abs/2010.02193).
Adapted from the original implementation from https://github.com/danijar/dreamerv2
"""

import copy
import os
import pathlib
import time
from dataclasses import asdict
from typing import Dict, Sequence

import gymnasium as gym
import numpy as np
import torch
import torch.nn.functional as F
from lightning.fabric import Fabric
from lightning.fabric.fabric import _is_using_cli
from lightning.fabric.wrappers import _FabricModule
from tensordict import TensorDict
from tensordict.tensordict import TensorDictBase
from torch import Tensor
from torch.distributions import Bernoulli, Distribution, Independent, Normal, OneHotCategorical
from torch.optim import Adam, Optimizer
from torch.utils.data import BatchSampler
from torchmetrics import MeanMetric

from sheeprl.algos.dreamer_v2.agent import PlayerDV2, WorldModel, build_models
from sheeprl.algos.dreamer_v2.args import DreamerV2Args
from sheeprl.algos.dreamer_v2.loss import reconstruction_loss
from sheeprl.algos.dreamer_v2.utils import compute_lambda_values, test
from sheeprl.data.buffers import AsyncReplayBuffer, EpisodeBuffer
from sheeprl.utils.callback import CheckpointCallback
from sheeprl.utils.env import make_dict_env
from sheeprl.utils.logger import create_tensorboard_logger
from sheeprl.utils.metric import MetricAggregator
from sheeprl.utils.parser import HfArgumentParser
from sheeprl.utils.registry import register_algorithm
from sheeprl.utils.utils import polynomial_decay

# Decomment the following two lines if you cannot start an experiment with DMC environments
# os.environ["PYOPENGL_PLATFORM"] = ""
# os.environ["MUJOCO_GL"] = "osmesa"


def train(
    fabric: Fabric,
    world_model: WorldModel,
    actor: _FabricModule,
    critic: _FabricModule,
    target_critic: torch.nn.Module,
    world_optimizer: Optimizer,
    actor_optimizer: Optimizer,
    critic_optimizer: Optimizer,
    data: TensorDictBase,
    aggregator: MetricAggregator,
    args: DreamerV2Args,
    cnn_keys: Sequence[str],
    mlp_keys: Sequence[str],
    actions_dim: Sequence[int],
) -> None:
    """Runs one-step update of the agent.

    The follwing designations are used:
        - recurrent_state: is what is called ht or deterministic state from Figure 2 in .
        - prior: the stochastic state coming out from the transition model, depicted as z-hat_t in Figure 2.
        - posterior: the stochastic state coming out from the representation model, depicted as z_t in Figure 2.
        - latent state: the concatenation of the stochastic (can be both the prior or the posterior one)
        and recurrent states on the last dimension.
        - p: the output of the transition model, from Eq. 1.
        - q: the output of the representation model, from Eq. 1.
        - po: the output of the observation model (decoder), from Eq. 1.
        - pr: the output of the reward model, from Eq. 1.
        - pc: the output of the continue model (discout predictor), from Eq. 1.
        - pv: the output of the value model (critic), from Eq. 3.

    In particular, the agent is updated as following:

    1. Dynamic Learning:
        - Encoder: encode the observations.
        - Recurrent Model: compute the recurrent state from the previous recurrent state,
            the previous posterior state, and from the previous actions.
        - Transition Model: predict the stochastic state from the recurrent state, i.e., the deterministic state or ht.
        - Representation Model: compute the actual stochastic state from the recurrent state and
            from the embedded observations provided by the environment.
        - Observation Model: reconstructs observations from latent states.
        - Reward Model: estimate rewards from the latent states.
        - Update the models
    2. Behaviour Learning:
        - Imagine trajectories in the latent space from each latent state
        s_t up to the horizon H: s'_(t+1), ..., s'_(t+H).
        - Predict rewards and values in the imagined trajectories.
        - Compute lambda targets (Eq. 4 in [https://arxiv.org/abs/2010.02193](https://arxiv.org/abs/2010.02193))
        - Update the actor and the critic

    Args:
        fabric (Fabric): the fabric instance.
        world_model (_FabricModule): the world model wrapped with Fabric.
        actor (_FabricModule): the actor model wrapped with Fabric.
        critic (_FabricModule): the critic model wrapped with Fabric.
        target_critic (nn.Module): the target critic model.
        world_optimizer (Optimizer): the world optimizer.
        actor_optimizer (Optimizer): the actor optimizer.
        critic_optimizer (Optimizer): the critic optimizer.
        data (TensorDictBase): the batch of data to use for training.
        aggregator (MetricAggregator): the aggregator to print the metrics.
        args (DreamerV2Args): the configs.
        cnn_keys (Sequence[str]): the cnn keys to encode/decode.
        mlp_keys (Sequence[str]): the mlp keys to encode/decode.
        actions_dim (Sequence[int]): the actions dimension.
    """

    # The environment interaction goes like this:
    # Actions:       0   a1       a2       a3
    #                    ^ \      ^ \      ^ \
    #                   /   \    /   \    /   \
    #                  /     v  /     v  /     v
    # Observations:  o0       o1       o2      o3
    # Rewards:       0        r1       r2      r3
    # Dones:         0        d1       d2      d3
    # Is-first       1        i1       i2      i3

    batch_size = args.per_rank_batch_size
    sequence_length = args.per_rank_sequence_length
    device = fabric.device
    batch_obs = {k: data[k] / 255 - 0.5 for k in cnn_keys}
    batch_obs.update({k: data[k] for k in mlp_keys})

    # Given how the environment interaction works, we assume that the first element in a sequence
    # is the first one, as if the environment has been reset
    data["is_first"][0, :] = torch.tensor([1.0], device=fabric.device).expand_as(data["is_first"][0, :])

    # Dynamic Learning
    stoch_state_size = args.stochastic_size * args.discrete_size
    recurrent_state = torch.zeros(1, batch_size, args.recurrent_state_size, device=device)
    posterior = torch.zeros(1, batch_size, args.stochastic_size, args.discrete_size, device=device)

    # Initialize the recurrent_states, which will contain all the recurrent states
    # computed during the dynamic learning phase
    recurrent_states = torch.zeros(sequence_length, batch_size, args.recurrent_state_size, device=device)

    # Initialize all the tensor to collect priors and posteriors states with their associated logits
    priors_logits = torch.empty(sequence_length, batch_size, stoch_state_size, device=device)
    posteriors = torch.empty(sequence_length, batch_size, args.stochastic_size, args.discrete_size, device=device)
    posteriors_logits = torch.empty(sequence_length, batch_size, stoch_state_size, device=device)

    # Embed observations from the environment
    embedded_obs = world_model.encoder(batch_obs)

    for i in range(0, sequence_length):
        # One step of dynamic learning, which take the posterior state, the recurrent state, the action
        # and the observation and compute the next recurrent, prior and posterior states
        recurrent_state, posterior, _, posterior_logits, prior_logits = world_model.rssm.dynamic(
            posterior, recurrent_state, data["actions"][i : i + 1], embedded_obs[i : i + 1], data["is_first"][i : i + 1]
        )
        recurrent_states[i] = recurrent_state
        priors_logits[i] = prior_logits
        posteriors[i] = posterior
        posteriors_logits[i] = posterior_logits

    # Concatenate the posteriors with the recurrent states on the last dimension.
    # Latent_states has dimension (sequence_length, batch_size, recurrent_state_size + stochastic_size * discrete_size)
    latent_states = torch.cat((posteriors.view(*posteriors.shape[:-2], -1), recurrent_states), -1)

    # Compute predictions for the observations
    decoded_information: Dict[str, torch.Tensor] = world_model.observation_model(latent_states)

    # Compute the distribution over the reconstructed observations
    po = {k: Independent(Normal(rec_obs, 1), len(rec_obs.shape[2:])) for k, rec_obs in decoded_information.items()}

    # Compute the distribution over the rewards
    pr = Independent(Normal(world_model.reward_model(latent_states), 1), 1)

    # Compute the distribution over the terminal steps, if required
    if args.use_continues and world_model.continue_model:
        pc = Independent(Bernoulli(logits=world_model.continue_model(latent_states), validate_args=False), 1)
        continue_targets = (1 - data["dones"]) * args.gamma
    else:
        pc = continue_targets = None

    # Reshape posterior and prior logits to shape [T, B, 32, 32]
    priors_logits = priors_logits.view(*priors_logits.shape[:-1], args.stochastic_size, args.discrete_size)
    posteriors_logits = posteriors_logits.view(*posteriors_logits.shape[:-1], args.stochastic_size, args.discrete_size)

    # World model optimization step
    world_optimizer.zero_grad(set_to_none=True)
    rec_loss, kl, state_loss, reward_loss, observation_loss, continue_loss = reconstruction_loss(
        po,
        batch_obs,
        pr,
        data["rewards"],
        priors_logits,
        posteriors_logits,
        args.kl_balancing_alpha,
        args.kl_free_nats,
        args.kl_free_avg,
        args.kl_regularizer,
        pc,
        continue_targets,
        args.continue_scale_factor,
    )
    fabric.backward(rec_loss)
    if args.clip_gradients is not None and args.clip_gradients > 0:
        world_model_grads = fabric.clip_gradients(
            module=world_model, optimizer=world_optimizer, max_norm=args.clip_gradients, error_if_nonfinite=False
        )
    world_optimizer.step()
    aggregator.update("Grads/world_model", world_model_grads.mean().detach())
    aggregator.update("Loss/reconstruction_loss", rec_loss.detach())
    aggregator.update("Loss/observation_loss", observation_loss.detach())
    aggregator.update("Loss/reward_loss", reward_loss.detach())
    aggregator.update("Loss/state_loss", state_loss.detach())
    aggregator.update("Loss/continue_loss", continue_loss.detach())
    aggregator.update("State/kl", kl.mean().detach())
    aggregator.update(
        "State/post_entropy",
        Independent(OneHotCategorical(logits=posteriors_logits.detach()), 1).entropy().mean().detach(),
    )
    aggregator.update(
        "State/prior_entropy",
        Independent(OneHotCategorical(logits=priors_logits.detach()), 1).entropy().mean().detach(),
    )

    # Behaviour Learning
    # (1, batch_size * sequence_length, stochastic_size * discrete_size)
    imagined_prior = posteriors.detach().reshape(1, -1, stoch_state_size)

    # (1, batch_size * sequence_length, recurrent_state_size).
    recurrent_state = recurrent_states.detach().reshape(1, -1, args.recurrent_state_size)

    # (1, batch_size * sequence_length, recurrent_state_size + stochastic_size * discrete_size)
    imagined_latent_state = torch.cat((imagined_prior, recurrent_state), -1)

    # Initialize the tensor of the imagined trajectories
    imagined_trajectories = torch.empty(
        args.horizon + 1,
        batch_size * sequence_length,
        stoch_state_size + args.recurrent_state_size,
        device=device,
    )
    imagined_trajectories[0] = imagined_latent_state

    # Initialize the tensor of the imagined actions
    imagined_actions = torch.empty(
        args.horizon + 1,
        batch_size * sequence_length,
        data["actions"].shape[-1],
        device=device,
    )
    imagined_actions[0] = torch.zeros(1, batch_size * sequence_length, data["actions"].shape[-1])

    # The imagination goes like this, with H=3:
    # Actions:       0   a'1      a'2     a'3
    #                    ^ \      ^ \      ^ \
    #                   /   \    /   \    /   \
    #                  /     v  /     v  /     v
    # States:        z0 ---> z'1 ---> z'2 ---> z'3
    # Rewards:       r'0     r'1      r'2      r'3
    # Values:        v'0     v'1      v'2      v'3
    # Lambda-values: l'0     l'1      l'2
    # Continues:     c0      c'1      c'2      c'3
    # where z0 comes from the posterior (is initialized as the concatenation of the posteriors and the recurrent states)
    # while z'i is the imagined states (prior)

    # Imagine trajectories in the latent space
    for i in range(1, args.horizon + 1):
        # (1, batch_size * sequence_length, sum(actions_dim))
        actions = torch.cat(actor(imagined_latent_state.detach())[0], dim=-1)
        imagined_actions[i] = actions

        # Imagination step
        imagined_prior, recurrent_state = world_model.rssm.imagination(imagined_prior, recurrent_state, actions)

        # Update current state
        imagined_prior = imagined_prior.view(1, -1, stoch_state_size)
        imagined_latent_state = torch.cat((imagined_prior, recurrent_state), -1)
        imagined_trajectories[i] = imagined_latent_state

    # Predict values and rewards
    predicted_target_values = Independent(Normal(target_critic(imagined_trajectories), 1), 1).mode
    predicted_rewards = Independent(Normal(world_model.reward_model(imagined_trajectories), 1), 1).mode
    if args.use_continues and world_model.continue_model:
        continues = Independent(
            Bernoulli(logits=world_model.continue_model(imagined_trajectories), validate_args=False), 1
        ).mean
        true_done = (1 - data["dones"]).reshape(1, -1, 1) * args.gamma
        continues = torch.cat((true_done, continues[1:]))
    else:
        continues = torch.ones_like(predicted_rewards.detach()) * args.gamma

    # Compute the lambda_values, by passing as last value the value of the last imagined state
    # (horizon, batch_size * sequence_length, 1)
    lambda_values = compute_lambda_values(
        predicted_rewards[:-1],
        predicted_target_values[:-1],
        continues[:-1],
        bootstrap=predicted_target_values[-1:],
        horizon=args.horizon,
        lmbda=args.lmbda,
    )

    # Compute the discounts to multiply the lambda values
    with torch.no_grad():
        discount = torch.cumprod(torch.cat((torch.ones_like(continues[:1]), continues[:-1]), 0), 0)

    # Actor optimization step. Eq. 6 from the paper
    # Given the following diagram, with H=3:
    # Actions:       0  [a'1]    [a'2]     a'3
    #                    ^ \      ^ \      ^ \
    #                   /   \    /   \    /   \
    #                  /     v  /     v  /     v
    # States:       [z0] -> [z'1] ->  z'2 ->   z'3
    # Values:       [v'0]   [v'1]     v'2      v'3
    # Lambda-values: l'0    [l'1]    [l'2]
    # Entropies:            [e'1]    [e'2]
    # The quantities wrapped into `[]` are the ones used for the actor optimization.
    # From Hafner (https://github.com/danijar/dreamerv2/blob/main/dreamerv2/agent.py#L253):
    # `Two states are lost at the end of the trajectory, one for the boostrap
    #  value prediction and one because the corresponding action does not lead
    #  anywhere anymore. One target is lost at the start of the trajectory
    #  because the initial state comes from the replay buffer.`
    actor_optimizer.zero_grad(set_to_none=True)
    policies: Sequence[Distribution] = actor(imagined_trajectories[:-2].detach())[1]

    # Dynamics backpropagation
    dynamics = lambda_values[1:]

    # Reinforce
    advantage = (lambda_values[1:] - predicted_target_values[:-2]).detach()
    reinforce = (
        torch.stack(
            [
                p.log_prob(imgnd_act[1:-1].detach()).unsqueeze(-1)
                for p, imgnd_act in zip(policies, torch.split(imagined_actions, actions_dim, -1))
            ],
            -1,
        ).sum(-1)
        * advantage
    )
    objective = args.objective_mix * reinforce + (1 - args.objective_mix) * dynamics
    try:
        entropy = args.actor_ent_coef * torch.stack([p.entropy() for p in policies], -1).sum(dim=-1)
    except NotImplementedError:
        entropy = torch.zeros_like(objective)
    policy_loss = -torch.mean(discount[:-2].detach() * (objective + entropy.unsqueeze(-1)))
    fabric.backward(policy_loss)
    if args.clip_gradients is not None and args.clip_gradients > 0:
        actor_grads = fabric.clip_gradients(
            module=actor, optimizer=actor_optimizer, max_norm=args.clip_gradients, error_if_nonfinite=False
        )
    actor_optimizer.step()
    aggregator.update("Grads/actor", actor_grads.mean().detach())
    aggregator.update("Loss/policy_loss", policy_loss.detach())

    # Predict the values distribution only for the first H (horizon)
    # imagined states (to match the dimension with the lambda values),
    # It removes the last imagined state in the trajectory because it is used for bootstrapping
    qv = Independent(Normal(critic(imagined_trajectories.detach()[:-1]), 1), 1)

    # Critic optimization step. Eq. 5 from the paper.
    critic_optimizer.zero_grad(set_to_none=True)
    value_loss = -torch.mean(discount[:-1, ..., 0] * qv.log_prob(lambda_values.detach()))
    fabric.backward(value_loss)
    if args.clip_gradients is not None and args.clip_gradients > 0:
        critic_grads = fabric.clip_gradients(
            module=critic, optimizer=critic_optimizer, max_norm=args.clip_gradients, error_if_nonfinite=False
        )
    critic_optimizer.step()
    aggregator.update("Grads/critic", critic_grads.mean().detach())
    aggregator.update("Loss/value_loss", value_loss.detach())

    # Reset everything
    actor_optimizer.zero_grad(set_to_none=True)
    critic_optimizer.zero_grad(set_to_none=True)
    world_optimizer.zero_grad(set_to_none=True)


@register_algorithm()
def main():
    parser = HfArgumentParser(DreamerV2Args)
    args: DreamerV2Args = parser.parse_args_into_dataclasses()[0]

    # These arguments cannot be changed
    args.screen_size = 64
    args.frame_stack = -1

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
        args = DreamerV2Args(**state["args"])
        args.per_rank_batch_size = state["batch_size"] // fabric.world_size
        ckpt_path = pathlib.Path(args.checkpoint_path)

    # Create TensorBoardLogger. This will create the logger only on the
    # rank-0 process
    logger, log_dir = create_tensorboard_logger(fabric, args, "dreamer_v2")
    if fabric.is_global_zero:
        fabric._loggers = [logger]
        fabric.logger.log_hyperparams(asdict(args))

    # Environment setup
    vectorized_env = gym.vector.SyncVectorEnv if args.sync_env else gym.vector.AsyncVectorEnv
    envs = vectorized_env(
        [
            make_dict_env(
                args.env_id,
                args.seed + rank * args.num_envs + i,
                rank,
                args,
                logger.log_dir if rank == 0 else None,
                "train",
                vector_env_idx=i,
            )
            for i in range(args.num_envs)
        ]
    )

    action_space = envs.single_action_space
    observation_space = envs.single_observation_space

    is_continuous = isinstance(action_space, gym.spaces.Box)
    is_multidiscrete = isinstance(action_space, gym.spaces.MultiDiscrete)
    actions_dim = (
        action_space.shape if is_continuous else (action_space.nvec.tolist() if is_multidiscrete else [action_space.n])
    )
    clip_rewards_fn = lambda r: torch.tanh(r) if args.clip_rewards else r
    cnn_keys = []
    mlp_keys = []
    if isinstance(observation_space, gym.spaces.Dict):
        cnn_keys = []
        for k, v in observation_space.spaces.items():
            if args.cnn_keys and k in args.cnn_keys:
                if len(v.shape) == 3:
                    cnn_keys.append(k)
                else:
                    fabric.print(
                        f"Found a CNN key which is not an image: `{k}` of shape {v.shape}. "
                        "Try to transform the observation from the environment into a 3D image"
                    )
        mlp_keys = []
        for k, v in observation_space.spaces.items():
            if args.mlp_keys and k in args.mlp_keys:
                if len(v.shape) == 1:
                    mlp_keys.append(k)
                else:
                    fabric.print(
                        f"Found an MLP key which is not a vector: `{k}` of shape {v.shape}. "
                        "Try to flatten the observation from the environment"
                    )
    else:
        raise RuntimeError(f"Unexpected observation type, should be of type Dict, got: {observation_space}")
    if cnn_keys == [] and mlp_keys == []:
        raise RuntimeError(
            "You should specify at least one CNN keys or MLP keys from the cli: `--cnn_keys rgb` or `--mlp_keys state` "
        )
    fabric.print("CNN keys:", cnn_keys)
    fabric.print("MLP keys:", mlp_keys)
    obs_keys = cnn_keys + mlp_keys

    world_model, actor, critic, target_critic = build_models(
        fabric,
        actions_dim,
        is_continuous,
        args,
        observation_space,
        cnn_keys,
        mlp_keys,
        state["world_model"] if args.checkpoint_path else None,
        state["actor"] if args.checkpoint_path else None,
        state["critic"] if args.checkpoint_path else None,
        state["target_critic"] if args.checkpoint_path else None,
    )
    player = PlayerDV2(
        world_model.encoder.module,
        world_model.rssm.recurrent_model.module,
        world_model.rssm.representation_model.module,
        actor.module,
        actions_dim,
        args.expl_amount,
        args.num_envs,
        args.stochastic_size,
        args.recurrent_state_size,
        fabric.device,
        discrete_size=args.discrete_size,
    )

    # Optimizers
    world_optimizer = Adam(world_model.parameters(), lr=args.world_lr, weight_decay=1e-6, eps=1e-5)
    actor_optimizer = Adam(actor.parameters(), lr=args.actor_lr, weight_decay=1e-6, eps=1e-5)
    critic_optimizer = Adam(critic.parameters(), lr=args.critic_lr, weight_decay=1e-6, eps=1e-5)
    if args.checkpoint_path:
        world_optimizer.load_state_dict(state["world_optimizer"])
        actor_optimizer.load_state_dict(state["actor_optimizer"])
        critic_optimizer.load_state_dict(state["critic_optimizer"])
    world_optimizer, actor_optimizer, critic_optimizer = fabric.setup_optimizers(
        world_optimizer, actor_optimizer, critic_optimizer
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
                "State/post_entropy": MeanMetric(sync_on_compute=False),
                "State/prior_entropy": MeanMetric(sync_on_compute=False),
                "State/kl": MeanMetric(sync_on_compute=False),
                "Params/exploration_amout": MeanMetric(sync_on_compute=False),
                "Grads/world_model": MeanMetric(sync_on_compute=False),
                "Grads/actor": MeanMetric(sync_on_compute=False),
                "Grads/critic": MeanMetric(sync_on_compute=False),
            }
        )
    aggregator.to(fabric.device)

    # Local data
    buffer_size = args.buffer_size // int(args.num_envs * fabric.world_size) if not args.dry_run else 2
    buffer_type = args.buffer_type.lower()
    if buffer_type == "sequential":
        rb = AsyncReplayBuffer(
            buffer_size,
            args.num_envs,
            device="cpu",
            memmap=args.memmap_buffer,
            memmap_dir=os.path.join(log_dir, "memmap_buffer", f"rank_{fabric.global_rank}"),
            sequential=True,
        )
    elif buffer_type == "episode":
        rb = EpisodeBuffer(
            buffer_size,
            sequence_length=args.per_rank_sequence_length,
            device="cpu",
            memmap=args.memmap_buffer,
            memmap_dir=os.path.join(log_dir, "memmap_buffer", f"rank_{fabric.global_rank}"),
        )
    else:
        raise ValueError(f"Unrecognized buffer type: must be one of `sequential` or `episode`, received: {buffer_type}")
    if args.checkpoint_path and args.checkpoint_buffer:
        if isinstance(state["rb"], list) and fabric.world_size == len(state["rb"]):
            rb = state["rb"][fabric.global_rank]
        elif isinstance(state["rb"], (AsyncReplayBuffer, EpisodeBuffer)):
            rb = state["rb"]
        else:
            raise RuntimeError(f"Given {len(state['rb'])}, but {fabric.world_size} processes are instantiated")
    step_data = TensorDict({}, batch_size=[args.num_envs], device="cpu")
    expl_decay_steps = state["expl_decay_steps"] if args.checkpoint_path else 0

    # Global variables
    start_time = time.perf_counter()
    start_step = state["global_step"] // fabric.world_size if args.checkpoint_path else 1
    single_global_step = int(args.num_envs * fabric.world_size * args.action_repeat)
    step_before_training = args.train_every // single_global_step if not args.dry_run else 0
    num_updates = args.total_steps // single_global_step if not args.dry_run else 1
    learning_starts = args.learning_starts // single_global_step if not args.dry_run else 0
    if args.checkpoint_path and not args.checkpoint_buffer:
        learning_starts += start_step
    max_step_expl_decay = args.max_step_expl_decay // (args.gradient_steps * fabric.world_size)
    if args.checkpoint_path:
        player.expl_amount = polynomial_decay(
            expl_decay_steps,
            initial=args.expl_amount,
            final=args.expl_min,
            max_decay_steps=max_step_expl_decay,
        )

    # Get the first environment observation and start the optimization
    episode_steps = [[] for _ in range(args.num_envs)]
    o = envs.reset(seed=args.seed)[0]
    obs = {}
    for k in o.keys():
        if k in obs_keys:
            torch_obs = torch.from_numpy(o[k]).view(args.num_envs, *o[k].shape[1:])
            if k in mlp_keys:
                # Images stay uint8 to save space
                torch_obs = torch_obs.float()
            step_data[k] = torch_obs
            obs[k] = torch_obs
    step_data["dones"] = torch.zeros(args.num_envs, 1)
    step_data["actions"] = torch.zeros(args.num_envs, sum(actions_dim))
    step_data["rewards"] = torch.zeros(args.num_envs, 1)
    step_data["is_first"] = torch.ones_like(step_data["dones"])
    if buffer_type == "sequential":
        rb.add(step_data[None, ...])
    else:
        for i, env_ep in enumerate(episode_steps):
            env_ep.append(step_data[i : i + 1][None, ...])
    player.init_states()

    gradient_steps = 0
    for global_step in range(start_step, num_updates + 1):
        # Sample an action given the observation received by the environment
        if global_step <= learning_starts and args.checkpoint_path is None and "minedojo" not in args.env_id:
            real_actions = actions = np.array(envs.action_space.sample())
            if not is_continuous:
                actions = np.concatenate(
                    [
                        F.one_hot(torch.tensor(act), act_dim).numpy()
                        for act, act_dim in zip(actions.reshape(len(actions_dim), -1), actions_dim)
                    ],
                    axis=-1,
                )
        else:
            with torch.no_grad():
                preprocessed_obs = {}
                for k, v in obs.items():
                    if k in cnn_keys:
                        preprocessed_obs[k] = v[None, ...].to(device) / 255 - 0.5
                    else:
                        preprocessed_obs[k] = v[None, ...].to(device)
                mask = {k: v for k, v in preprocessed_obs.items() if k.startswith("mask")}
                if len(mask) == 0:
                    mask = None
                real_actions = actions = player.get_exploration_action(preprocessed_obs, is_continuous, mask)
                actions = torch.cat(actions, -1).cpu().numpy()
                if is_continuous:
                    real_actions = torch.cat(real_actions, -1).cpu().numpy()
                else:
                    real_actions = np.array([real_act.cpu().argmax(dim=-1).numpy() for real_act in real_actions])

        step_data["is_first"] = copy.deepcopy(step_data["dones"])
        o, rewards, dones, truncated, infos = envs.step(real_actions.reshape(envs.action_space.shape))
        dones = np.logical_or(dones, truncated)
        if args.dry_run and buffer_type == "episode":
            dones = np.ones_like(dones)

        if "final_info" in infos:
            for i, agent_final_info in enumerate(infos["final_info"]):
                if agent_final_info is not None and "episode" in agent_final_info:
                    fabric.print(
                        f"Rank-0: global_step={global_step}, reward_env_{i}={agent_final_info['episode']['r'][0]}"
                    )
                    aggregator.update("Rewards/rew_avg", agent_final_info["episode"]["r"][0])
                    aggregator.update("Game/ep_len_avg", agent_final_info["episode"]["l"][0])

        # Save the real next observation
        real_next_obs = copy.deepcopy(o)
        if "final_observation" in infos:
            for idx, final_obs in enumerate(infos["final_observation"]):
                if final_obs is not None:
                    for k, v in final_obs.items():
                        real_next_obs[k][idx] = v

        next_obs: Dict[str, Tensor] = {}
        for k in real_next_obs.keys():  # [N_envs, N_obs]
            if k in obs_keys:
                next_obs[k] = torch.from_numpy(o[k]).view(args.num_envs, *o[k].shape[1:])
                step_data[k] = torch.from_numpy(real_next_obs[k]).view(args.num_envs, *real_next_obs[k].shape[1:])
                if k in mlp_keys:
                    next_obs[k] = next_obs[k].float()
                    step_data[k] = step_data[k].float()
        actions = torch.from_numpy(actions).view(args.num_envs, -1).float()
        rewards = torch.from_numpy(rewards).view(args.num_envs, -1).float()
        dones = torch.from_numpy(dones).view(args.num_envs, -1).float()

        # Next_obs becomes the new obs
        obs = next_obs

        step_data["dones"] = dones
        step_data["actions"] = actions
        step_data["rewards"] = clip_rewards_fn(rewards)
        if buffer_type == "sequential":
            rb.add(step_data[None, ...])
        else:
            for i, env_ep in enumerate(episode_steps):
                env_ep.append(step_data[i : i + 1][None, ...])

        # Reset and save the observation coming from the automatic reset
        dones_idxes = dones.nonzero(as_tuple=True)[0].tolist()
        reset_envs = len(dones_idxes)
        if reset_envs > 0:
            reset_data = TensorDict({}, batch_size=[reset_envs], device="cpu")
            for k in next_obs.keys():
                reset_data[k] = next_obs[k][dones_idxes]
            reset_data["dones"] = torch.zeros(reset_envs, 1)
            reset_data["actions"] = torch.zeros(reset_envs, np.sum(actions_dim))
            reset_data["rewards"] = torch.zeros(reset_envs, 1)
            reset_data["is_first"] = torch.ones_like(reset_data["dones"])
            if buffer_type == "episode":
                for i, d in enumerate(dones_idxes):
                    if len(episode_steps[d]) >= args.per_rank_sequence_length:
                        rb.add(torch.cat(episode_steps[d], dim=0))
                        episode_steps[d] = [reset_data[i : i + 1][None, ...]]
            else:
                rb.add(reset_data[None, ...], dones_idxes)
            # Reset dones so that `is_first` is updated
            for d in dones_idxes:
                step_data["dones"][d] = torch.zeros_like(step_data["dones"][d])
            # Reset internal agent states
            player.init_states(dones_idxes)

        step_before_training -= 1

        # Train the agent
        if global_step >= learning_starts and step_before_training <= 0:
            fabric.barrier()
            if buffer_type == "sequential":
                local_data = rb.sample(
                    args.per_rank_batch_size,
                    sequence_length=args.per_rank_sequence_length,
                    n_samples=args.pretrain_steps if global_step == learning_starts else args.gradient_steps,
                ).to(device)
            else:
                local_data = rb.sample(
                    args.per_rank_batch_size,
                    n_samples=args.pretrain_steps if global_step == learning_starts else args.gradient_steps,
                    prioritize_ends=args.prioritize_ends,
                ).to(device)
            distributed_sampler = BatchSampler(range(local_data.shape[0]), batch_size=1, drop_last=False)
            for i in distributed_sampler:
                if gradient_steps % args.critic_target_network_update_freq == 0:
                    for cp, tcp in zip(critic.module.parameters(), target_critic.parameters()):
                        tcp.data.copy_(cp.data)
                train(
                    fabric,
                    world_model,
                    actor,
                    critic,
                    target_critic,
                    world_optimizer,
                    actor_optimizer,
                    critic_optimizer,
                    local_data[i].view(args.per_rank_sequence_length, args.per_rank_batch_size),
                    aggregator,
                    args,
                    cnn_keys,
                    mlp_keys,
                    actions_dim,
                )
                gradient_steps += 1
            step_before_training = args.train_every // single_global_step
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
                "target_critic": target_critic.state_dict(),
                "world_optimizer": world_optimizer.state_dict(),
                "actor_optimizer": actor_optimizer.state_dict(),
                "critic_optimizer": critic_optimizer.state_dict(),
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

    envs.close()
    if fabric.is_global_zero:
        test(player, fabric, args, cnn_keys, mlp_keys)


if __name__ == "__main__":
    main()
