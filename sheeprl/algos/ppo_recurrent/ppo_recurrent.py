import copy
import itertools
import os
import time
import warnings
from contextlib import nullcontext
from dataclasses import asdict
from math import prod
from typing import List

import gymnasium as gym
import numpy as np
import torch
from lightning.fabric import Fabric
from lightning.fabric.fabric import _is_using_cli
from tensordict import TensorDict
from tensordict.tensordict import TensorDictBase, pad_sequence
from torch.distributed.algorithms.join import Join
from torch.distributions import Categorical
from torch.optim import Adam
from torch.utils.data.sampler import BatchSampler, RandomSampler
from torchmetrics import MeanMetric

from sheeprl.algos.ppo.loss import entropy_loss, policy_loss, value_loss
from sheeprl.algos.ppo_recurrent.agent import RecurrentPPOAgent
from sheeprl.algos.ppo_recurrent.args import RecurrentPPOArgs
from sheeprl.algos.ppo_recurrent.utils import test
from sheeprl.data import ReplayBuffer
from sheeprl.utils.callback import CheckpointCallback
from sheeprl.utils.env import make_env
from sheeprl.utils.logger import create_tensorboard_logger
from sheeprl.utils.metric import MetricAggregator
from sheeprl.utils.parser import HfArgumentParser
from sheeprl.utils.registry import register_algorithm
from sheeprl.utils.utils import gae, normalize_tensor, polynomial_decay


def train(
    fabric: Fabric,
    agent: RecurrentPPOAgent,
    optimizer: torch.optim.Optimizer,
    data: TensorDictBase,
    aggregator: MetricAggregator,
    args: RecurrentPPOArgs,
):
    num_sequences = data.shape[1]
    if args.per_rank_num_batches > 0:
        batch_size = num_sequences // args.per_rank_num_batches
        batch_size = batch_size if batch_size > 0 else num_sequences
    else:
        batch_size = 1
    with Join([agent._forward_module]) if fabric.world_size > 1 else nullcontext():
        for _ in range(args.update_epochs):
            states = ((data["actor_hxs"], data["actor_cxs"]), (data["critic_hxs"], data["critic_cxs"]))
            sampler = BatchSampler(
                RandomSampler(range(num_sequences)),
                batch_size=batch_size,
                drop_last=False,
            )  # Random sampling sequences
            for idxes in sampler:
                batch = data[:, idxes]
                mask = batch["mask"].unsqueeze(-1)
                action_logits, new_values, _ = agent(
                    batch["observations"],
                    state=tuple([tuple([s[:1, idxes] for s in state]) for state in states]),
                    mask=mask,
                )
                dist = Categorical(logits=action_logits.unsqueeze(-2))

                normalized_advantages = batch["advantages"][mask]
                if args.normalize_advantages and len(normalized_advantages) > 1:
                    normalized_advantages = normalize_tensor(normalized_advantages)

                # Policy loss
                pg_loss = policy_loss(
                    dist.log_prob(batch["actions"])[mask],
                    batch["logprobs"][mask],
                    normalized_advantages,
                    args.clip_coef,
                    "mean",
                )

                # Value loss
                v_loss = value_loss(
                    new_values[mask],
                    batch["values"][mask],
                    batch["returns"][mask],
                    args.clip_coef,
                    args.clip_vloss,
                    "mean",
                )

                # Entropy loss
                ent_loss = entropy_loss(dist.entropy()[mask], "mean")

                # Equation (9) in the paper
                loss = pg_loss + args.vf_coef * v_loss + args.ent_coef * ent_loss

                optimizer.zero_grad(set_to_none=True)
                fabric.backward(loss)
                if args.max_grad_norm > 0.0:
                    fabric.clip_gradients(agent, optimizer, max_norm=args.max_grad_norm)
                optimizer.step()

                # Update metrics
                aggregator.update("Loss/policy_loss", pg_loss.detach())
                aggregator.update("Loss/value_loss", v_loss.detach())
                aggregator.update("Loss/entropy_loss", ent_loss.detach())


@register_algorithm(decoupled=True)
def main():
    parser = HfArgumentParser(RecurrentPPOArgs)
    args: RecurrentPPOArgs = parser.parse_args_into_dataclasses()[0]
    initial_ent_coef = copy.deepcopy(args.ent_coef)
    initial_clip_coef = copy.deepcopy(args.clip_coef)

    if args.share_data:
        warnings.warn("The script has been called with --share-data: with recurrent PPO only gradients are shared")

    # Initialize Fabric
    fabric = Fabric(callbacks=[CheckpointCallback()])
    if not _is_using_cli():
        fabric.launch()
    rank = fabric.global_rank
    world_size = fabric.world_size
    device = fabric.device
    fabric.seed_everything(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic

    # Create TensorBoardLogger. This will create the logger only on the
    # rank-0 process
    logger, log_dir = create_tensorboard_logger(fabric, args, "ppo_recurrent")
    if fabric.is_global_zero:
        fabric._loggers = [logger]
        fabric.logger.log_hyperparams(asdict(args))

    # Environment setup
    vectorized_env = gym.vector.SyncVectorEnv if args.sync_env else gym.vector.AsyncVectorEnv
    envs = vectorized_env(
        [
            make_env(
                args.env_id,
                args.seed + rank * args.num_envs + i,
                rank,
                args.capture_video,
                logger.log_dir if rank == 0 else None,
                "train",
                mask_velocities=args.mask_vel,
                vector_env_idx=i,
            )
            for i in range(args.num_envs)
        ]
    )
    if not isinstance(envs.single_action_space, gym.spaces.Discrete):
        raise ValueError("Only discrete action space is supported by the PPO recurrent agent")
    if len(envs.single_observation_space.shape) > 1:
        raise ValueError(
            "Only environments with vector-only observations are supported by the PPO recurrent agent. "
            f"Provided environment: {args.env_id}"
        )

    # Define the agent and the optimizer and setup them with Fabric
    obs_dim = prod(envs.single_observation_space.shape)
    agent = fabric.setup_module(
        RecurrentPPOAgent(
            observation_dim=obs_dim,
            action_dim=envs.single_action_space.n,
            lstm_hidden_size=args.lstm_hidden_size,
            actor_hidden_size=args.actor_hidden_size,
            actor_pre_lstm_hidden_size=args.actor_pre_lstm_hidden_size,
            critic_hidden_size=args.critic_hidden_size,
            critic_pre_lstm_hidden_size=args.critic_pre_lstm_hidden_size,
            num_envs=args.num_envs,
        )
    )
    optimizer = fabric.setup_optimizers(Adam(params=agent.parameters(), lr=args.lr, eps=1e-4))

    # Metrics
    with device:
        aggregator = MetricAggregator(
            {
                "Rewards/rew_avg": MeanMetric(),
                "Game/ep_len_avg": MeanMetric(),
                "Time/step_per_second": MeanMetric(),
                "Loss/value_loss": MeanMetric(),
                "Loss/policy_loss": MeanMetric(),
                "Loss/entropy_loss": MeanMetric(),
            }
        )

    # Local data
    rb = ReplayBuffer(
        args.rollout_steps,
        args.num_envs,
        device=device,
        memmap=args.memmap_buffer,
        memmap_dir=os.path.join(log_dir, "memmap_buffer", f"rank_{fabric.global_rank}"),
    )
    step_data = TensorDict({}, batch_size=[1, args.num_envs], device=device)

    # Global variables
    global_step = 0
    start_time = time.perf_counter()
    single_global_rollout = int(args.num_envs * args.rollout_steps * world_size)
    num_updates = args.total_steps // single_global_rollout if not args.dry_run else 1

    # Linear learning rate scheduler
    if args.anneal_lr:
        from torch.optim.lr_scheduler import PolynomialLR

        scheduler = PolynomialLR(optimizer=optimizer, total_iters=num_updates, power=1.0)

    with device:
        # Get the first environment observation and start the optimization
        next_obs = torch.tensor(envs.reset(seed=args.seed)[0], dtype=torch.float32).unsqueeze(0)  # [1, N_envs, N_obs]
        next_done = torch.zeros(1, args.num_envs, 1, dtype=torch.float32)  # [1, N_envs, 1]
        next_state = agent.initial_states

    for update in range(1, num_updates + 1):
        for _ in range(0, args.rollout_steps):
            global_step += args.num_envs * world_size

            with torch.no_grad():
                # Sample an action given the observation received by the environment
                action_logits, values, state = agent.module(next_obs, state=next_state)
                dist = Categorical(logits=action_logits.unsqueeze(-2))
                action = dist.sample()
                logprob = dist.log_prob(action)

            # Single environment step
            obs, reward, done, truncated, info = envs.step(action.cpu().numpy().reshape(envs.action_space.shape))
            done = np.logical_or(done, truncated)

            with device:
                obs = torch.tensor(obs, dtype=torch.float32).unsqueeze(0)  # [1, N_envs, N_obs]
                done = torch.tensor(done, dtype=torch.float32).view(1, args.num_envs, -1)  # [1, N_envs, 1]
                reward = torch.tensor(reward, dtype=torch.float32).view(1, args.num_envs, -1)  # [1, N_envs, 1]

            step_data["dones"] = next_done
            step_data["values"] = values
            step_data["actions"] = action
            step_data["rewards"] = reward
            step_data["logprobs"] = logprob
            step_data["observations"] = next_obs
            step_data["actor_hxs"] = next_state[0][0]
            step_data["actor_cxs"] = next_state[0][1]
            step_data["critic_hxs"] = next_state[1][0]
            step_data["critic_cxs"] = next_state[1][1]

            # Append data to buffer
            rb.add(step_data)

            # Update observation, done and recurrent state
            next_obs = obs
            next_done = done
            if args.reset_recurrent_state_on_done:
                next_state = tuple([tuple([(1 - done) * e for e in s]) for s in state])
            else:
                next_state = state

            if "final_info" in info:
                for i, agent_final_info in enumerate(info["final_info"]):
                    if agent_final_info is not None and "episode" in agent_final_info:
                        fabric.print(
                            f"Rank-0: global_step={global_step}, reward_env_{i}={agent_final_info['episode']['r'][0]}"
                        )
                        aggregator.update("Rewards/rew_avg", agent_final_info["episode"]["r"][0])
                        aggregator.update("Game/ep_len_avg", agent_final_info["episode"]["l"][0])

        # Estimate returns with GAE (https://arxiv.org/abs/1506.02438)
        with torch.no_grad():
            next_value, _ = agent.module.get_values(next_obs, critic_state=next_state[1])
            returns, advantages = gae(
                rb["rewards"],
                rb["values"],
                rb["dones"],
                next_value,
                next_done,
                args.rollout_steps,
                args.gamma,
                args.gae_lambda,
            )

            # Add returns and advantages to the buffer
            rb["returns"] = returns.float()
            rb["advantages"] = advantages.float()

        # Get the training data as a TensorDict
        local_data = rb.buffer

        # Train the agent

        # Prepare data
        # 1. Split data into episodes (for every environment)
        episodes: List[TensorDictBase] = []
        for env_id in range(args.num_envs):
            env_data = local_data[:, env_id]  # [N_steps, *]
            episode_ends = env_data["dones"].nonzero(as_tuple=True)[0]
            episode_ends = episode_ends.tolist()
            episode_ends.append(args.rollout_steps)
            start = 0
            for ep_end_idx in episode_ends:
                stop = ep_end_idx
                # Do not include the done, since when we encounter a done it means that
                # the episode has started
                episode = env_data[start:stop]
                if len(episode) > 0:
                    episodes.append(episode)
                start = stop
        # 2. Split every episode into sequences of length `per_rank_batch_size`
        if args.per_rank_batch_size is not None and args.per_rank_batch_size > 0:
            sequences = list(itertools.chain.from_iterable([ep.split(args.per_rank_batch_size) for ep in episodes]))
        else:
            sequences = episodes
        padded_sequences = pad_sequence(sequences, batch_first=False, return_mask=True)  # [Seq_len, Num_seq, *]
        train(fabric, agent, optimizer, padded_sequences, aggregator, args)

        if args.anneal_lr:
            fabric.log("Info/learning_rate", scheduler.get_last_lr()[0], global_step)
            scheduler.step()
        else:
            fabric.log("Info/learning_rate", args.lr, global_step)

        fabric.log("Info/clip_coef", args.clip_coef, global_step)
        if args.anneal_clip_coef:
            args.clip_coef = polynomial_decay(
                update, initial=initial_clip_coef, final=0.0, max_decay_steps=num_updates, power=1.0
            )

        fabric.log("Info/ent_coef", args.ent_coef, global_step)
        if args.anneal_ent_coef:
            args.ent_coef = polynomial_decay(
                update, initial=initial_ent_coef, final=0.0, max_decay_steps=num_updates, power=1.0
            )

        # Log metrics
        metrics_dict = aggregator.compute()
        fabric.log("Time/step_per_second", int(global_step / (time.perf_counter() - start_time)), global_step)
        fabric.log_dict(metrics_dict, global_step)
        aggregator.reset()

        # Checkpoint model
        if (args.checkpoint_every > 0 and update % args.checkpoint_every == 0) or args.dry_run or update == num_updates:
            state = {
                "agent": agent.state_dict(),
                "optimizer": optimizer.state_dict(),
                "args": asdict(args),
                "update_step": update,
                "scheduler": scheduler.state_dict() if args.anneal_lr else None,
            }
            ckpt_path = os.path.join(log_dir, f"checkpoint/ckpt_{update}_{fabric.global_rank}.ckpt")
            fabric.call("on_checkpoint_coupled", fabric=fabric, ckpt_path=ckpt_path, state=state)

    envs.close()
    if fabric.is_global_zero:
        test_env = make_env(
            args.env_id,
            None,
            0,
            args.capture_video,
            fabric.logger.log_dir,
            "test",
            mask_velocities=args.mask_vel,
            vector_env_idx=0,
        )()
        test(agent.module, test_env, fabric, args)


if __name__ == "__main__":
    main()
