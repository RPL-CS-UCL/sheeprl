from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np
import torch
from lightning.fabric import Fabric
from lightning.fabric.wrappers import _FabricModule
from torch import Tensor, nn
from torch.distributions import Normal, OneHotCategorical

from sheeprl.algos.dreamer_v1.args import DreamerV1Args
from sheeprl.algos.dreamer_v1.utils import compute_stochastic_state
from sheeprl.algos.dreamer_v2.agent import Actor, CNNDecoder, CNNEncoder, MinedojoActor, MLPDecoder, MLPEncoder
from sheeprl.models.models import MLP, MultiDecoder, MultiEncoder
from sheeprl.utils.utils import init_weights


class RecurrentModel(nn.Module):
    """
    Recurrent model for the model-base Dreamer agent.

    Args:
        input_size (int): the input size of the model.
        recurrent_state_size (int): the size of the recurrent state.
        activation (nn.Module): the activation function.
            Default to ELU.
    """

    def __init__(self, input_size: int, recurrent_state_size: int, activation: nn.Module = nn.ELU) -> None:
        super().__init__()
        self.mlp = nn.Sequential(nn.Linear(input_size, recurrent_state_size), activation())
        self.rnn = nn.GRU(recurrent_state_size, recurrent_state_size)

    def forward(self, input: Tensor, recurrent_state: Tensor) -> Tuple[Tensor, Tensor]:
        """
        Compute the next recurrent state from the latent state (stochastic and recurrent states) and the actions.

        Args:
            input (Tensor): the input tensor composed by the stochastic state and the actions concatenated together.
            recurrent_state (Tensor): the previous recurrent state.

        Returns:
            the computed recurrent output and recurrent state.
        """
        feat = self.mlp(input)
        self.rnn.flatten_parameters()
        out, recurrent_state = self.rnn(feat, recurrent_state)
        return out, recurrent_state


class RSSM(nn.Module):
    """RSSM model for the model-base DreamerV1 agent.

    Args:
        recurrent_model (_FabricModule): the recurrent model of the RSSM model
            described in [https://arxiv.org/abs/1811.04551](https://arxiv.org/abs/1811.04551).
        representation_model (_FabricModule): the representation model composed
            by a multi-layer perceptron to compute the posterior state.
            For more information see [https://arxiv.org/abs/1912.01603](https://arxiv.org/abs/1912.01603).
        transition_model (_FabricModule): the transition model described
            in [https://arxiv.org/abs/1912.01603](https://arxiv.org/abs/1912.01603).
            The model is composed by a multi-layer perceptron to predict the prior state.
        min_std (float): the minimum value of the standard deviation computed
            by the representation and transition models.
            Default to 0.1.
    """

    def __init__(
        self,
        recurrent_model: _FabricModule,
        representation_model: _FabricModule,
        transition_model: _FabricModule,
        min_std: float = 0.1,
    ) -> None:
        super().__init__()
        self.recurrent_model = recurrent_model
        self.representation_model = representation_model
        self.transition_model = transition_model
        self.min_std = min_std

    def dynamic(
        self,
        posterior: Tensor,
        recurrent_state: Tensor,
        action: Tensor,
        embedded_obs: Tensor,
    ) -> Tuple[Tensor, Tensor, Tensor, Tuple[Tensor, Tensor], Tuple[Tensor, Tensor]]:
        """
        Perform one step of the dynamic learning:
            Recurrent model: compute the recurrent state from the previous latent space, the action taken by the agent,
                i.e., it computes the deterministic state (or ht).
            Transition model: predict the prior state from the recurrent output.
            Representation model: compute the posterior state from the recurrent state and from
                the embedded observations provided by the environment.
        For more information see [https://arxiv.org/abs/1811.04551](https://arxiv.org/abs/1811.04551)
        and [https://arxiv.org/abs/1912.01603](https://arxiv.org/abs/1912.01603).

        Args:
            posterior (Tensor): the posterior state.
            recurrent_state (Tensor): a tuple representing the recurrent state of the recurrent model.
            action (Tensor): the action taken by the agent.
            embedded_obs (Tensor): the embedded observations provided by the environment.

        Returns:
            The recurrent state (Tensor): the recurrent state of the recurrent model.
            The posterior state (Tensor): computed by the representation model
            from the recurrent state and the embedded observation.
            The prior state (Tensor): computed by the transition model from the recurrent state
            and the embedded observation.
            The posterior mean and std (Tuple[Tensor, Tensor]): the posterior mean and std of
            the distribution of the posterior state.
            The prior mean and std (Tuple[Tensor, Tensor]): the predicted mean and std of
            the distribution of the prior state.
        """
        recurrent_out, recurrent_state = self.recurrent_model(torch.cat((posterior, action), -1), recurrent_state)
        prior_state_mean_std, prior = self._transition(recurrent_out)
        posterior_mean_std, posterior = self._representation(recurrent_state, embedded_obs)
        return recurrent_state, posterior, prior, posterior_mean_std, prior_state_mean_std

    def _representation(self, recurrent_state: Tensor, embedded_obs: Tensor) -> Tuple[Tuple[Tensor, Tensor], Tensor]:
        """Compute the distribution of the posterior state.

        Args:
            recurrent_state (Tensor): the recurrent state of the recurrent model, i.e.,
                what is called h or deterministic state in
                [https://arxiv.org/abs/1811.04551](https://arxiv.org/abs/1811.04551).
            embedded_obs (Tensor): the embedded real observations provided by the environment.

        Returns:
            posterior_mean_std (Tensor, Tensor): the mean and the standard deviation
            of the distribution of the posterior state.
            posterior (Tensor): the sampled posterior.
        """
        posterior_mean_std, posterior = compute_stochastic_state(
            self.representation_model(torch.cat((recurrent_state, embedded_obs), -1)),
            event_shape=1,
            min_std=self.min_std,
        )
        return posterior_mean_std, posterior

    def _transition(self, recurrent_out: Tensor) -> Tuple[Tuple[Tensor, Tensor], Tensor]:
        """Predict the prior state (Transition Model).

        Args:
            recurrent_out (Tensor): the output of the recurrent model, i.e., the deterministic part of the latent space.

        Returns:
            The predicted mean and the standard deviation of the distribution of the prior state (Tensor, Tensor).
            The prior state (Tensor): the sampled prior state predicted by the transition model.
        """
        prior_mean_std = self.transition_model(recurrent_out)
        return compute_stochastic_state(prior_mean_std, event_shape=1, min_std=self.min_std)

    def imagination(self, stochastic_state: Tensor, recurrent_state: Tensor, actions: Tensor) -> Tuple[Tensor, Tensor]:
        """One-step imagination of the next latent state.
        It can be used several times to imagine trajectories in the latent space (Transition Model).

        Args:
            stochastic_state (Tensor): the stochastic space (can be either the posterior or the prior).
                Shape (1, batch_size, stochastic_size).
            recurrent_state (Tensor): a tuple representing the recurrent state of the recurrent model.
            actions (Tensor): the actions taken by the agent.
                Shape (1, batch_size, stochastic_size).

        Returns:
            The imagined prior state (Tuple[Tensor, Tensor]): the imagined prior state.
            The recurrent state (Tensor).
        """
        recurrent_output, recurrent_state = self.recurrent_model(
            torch.cat((stochastic_state, actions), -1), recurrent_state
        )
        _, imagined_prior = self._transition(recurrent_output)
        return imagined_prior, recurrent_state


class WorldModel(nn.Module):
    """Wrapper class for the World model.

    Args:
        encoder (_FabricModule): the encoder.
        rssm (RSSM): the rssm.
        observation_model (_FabricModule): the observation model.
        reward_model (_FabricModule): the reward model.
        continue_model (_FabricModule, optional): the continue model.
    """

    def __init__(
        self,
        encoder: _FabricModule,
        rssm: RSSM,
        observation_model: _FabricModule,
        reward_model: _FabricModule,
        continue_model: Optional[_FabricModule],
    ) -> None:
        super().__init__()
        self.encoder = encoder
        self.rssm = rssm
        self.observation_model = observation_model
        self.reward_model = reward_model
        self.continue_model = continue_model


class PlayerDV1(nn.Module):
    """The model of the DreamerV1 player.

    Args:
        encoder (nn.Module): the encoder.
        recurrent_model (nn.Module): the recurrent model.
        representation_model (nn.Module): the representation model.
        actor (nn.Module): the actor.
        actions_dim (Sequence[int]): the dimension of each action.
        expl_amout (float): the exploration amout to use during training.
        num_envs (int): the number of environments.
        stochastic_size (int): the size of the stochastic state.
        recurrent_state_size (int): the size of the recurrent state.
        device (torch.device): the device to work on.
    """

    def __init__(
        self,
        encoder: nn.Module,
        recurrent_model: nn.Module,
        representation_model: nn.Module,
        actor: nn.Module,
        actions_dim: Sequence[int],
        expl_amount: float,
        num_envs: int,
        stochastic_size: int,
        recurrent_state_size: int,
        device: torch.device,
    ) -> None:
        super().__init__()
        self.encoder = encoder
        self.recurrent_model = recurrent_model
        self.representation_model = representation_model
        self.actor = actor
        self.device = device
        self.expl_amount = expl_amount
        self.actions_dim = actions_dim
        self.stochastic_size = stochastic_size
        self.recurrent_state_size = recurrent_state_size
        self.num_envs = num_envs
        self.init_states()

    def init_states(self, reset_envs: Optional[Sequence[int]] = None) -> None:
        """Initialize the states and the actions for the ended environments.

        Args:
            reset_envs (Optional[Sequence[int]], optional): which environments' states to reset.
                If None, then all environments' states are reset.
                Defaults to None.
        """
        if reset_envs is None or len(reset_envs) == 0:
            self.actions = torch.zeros(1, self.num_envs, np.sum(self.actions_dim), device=self.device)
            self.recurrent_state = torch.zeros(1, self.num_envs, self.recurrent_state_size, device=self.device)
            self.stochastic_state = torch.zeros(1, self.num_envs, self.stochastic_size, device=self.device)
        else:
            self.actions[:, reset_envs] = torch.zeros_like(self.actions[:, reset_envs])
            self.recurrent_state[:, reset_envs] = torch.zeros_like(self.recurrent_state[:, reset_envs])
            self.stochastic_state[:, reset_envs] = torch.zeros_like(self.stochastic_state[:, reset_envs])

    def get_exploration_action(
        self, obs: Tensor, is_continuous: bool, mask: Optional[Dict[str, Tensor]] = None
    ) -> Sequence[Tensor]:
        """Return the actions with a certain amount of noise for exploration.

        Args:
            obs (Tensor): the current observations.
            is_continuous (bool): whether or not the actions are continuous.
            mask (Dict[str, Tensor], optional): the action mask (whether or not each action can be executed).
                Defaults to None.

        Returns:
            The actions the agent has to perform (Sequence[Tensor]).
        """
        actions = self.get_greedy_action(obs, mask=mask)
        if is_continuous:
            self.actions = torch.cat(actions, -1)
            if self.expl_amount > 0.0:
                self.actions = torch.clip(Normal(self.actions, self.expl_amount).sample(), -1, 1)
            expl_actions = [self.actions]
        else:
            expl_actions = []
            for act in actions:
                sample = OneHotCategorical(logits=torch.zeros_like(act)).sample().to(self.device)
                expl_actions.append(
                    torch.where(torch.rand(act.shape[:1], device=self.device) < self.expl_amount, sample, act)
                )
            self.actions = torch.cat(expl_actions, -1)
        return tuple(expl_actions)

    def get_greedy_action(
        self, obs: Tensor, is_training: bool = True, mask: Optional[Dict[str, Tensor]] = None
    ) -> Sequence[Tensor]:
        """Return the greedy actions.

        Args:
            obs (Tensor): the current observations.
            is_training (bool): whether it is training.
                Default to True.
            mask (Dict[str, Tensor], optional): the action mask (whether or not each action can be executed).
                Defaults to None.

        Returns:
            The actions the agent has to perform (Sequence[Tensor]).
        """
        embedded_obs = self.encoder(obs)
        _, self.recurrent_state = self.recurrent_model(
            torch.cat((self.stochastic_state, self.actions), -1), self.recurrent_state
        )
        _, self.stochastic_state = compute_stochastic_state(
            self.representation_model(torch.cat((self.recurrent_state, embedded_obs), -1))
        )
        actions, _ = self.actor(torch.cat((self.stochastic_state, self.recurrent_state), -1), is_training, mask)
        self.actions = torch.cat(actions, -1)
        return actions


def build_models(
    fabric: Fabric,
    actions_dim: Sequence[int],
    is_continuous: bool,
    args: DreamerV1Args,
    obs_space: Dict[str, Any],
    cnn_keys: Sequence[str],
    mlp_keys: Sequence[str],
    world_model_state: Optional[Dict[str, Tensor]] = None,
    actor_state: Optional[Dict[str, Tensor]] = None,
    critic_state: Optional[Dict[str, Tensor]] = None,
) -> Tuple[WorldModel, _FabricModule, _FabricModule]:
    """Build the models and wrap them with Fabric.

    Args:
        fabric (Fabric): the fabric object.
        actions_dim (Sequence[int]): the dimension of the actions.
        is_continuous (bool): whether or not the actions are continuous.
        args (DreamerV1Args): the hyper-parameters of DreamerV1.
        obs_space (Dict[str, Any]): the observation space.
        cnn_keys (Sequence[str]): the keys of the observation space to encode through the cnn encoder.
        mlp_keys (Sequence[str]): the keys of the observation space to encode through the mlp encoder.
        world_model_state (Dict[str, Tensor], optional): the state loaded from a previous checkpoint of the world model.
            Default to None.
        actor_state: (Dict[str, Tensor], optional): the state loaded from a previous checkpoint of the actor.
            Default to None.
        critic_state: (Dict[str, Tensor], optional): the state loaded from a previous checkpoint of the critic.
            Default to None.

    Returns:
        The world model (WorldModel): composed by the encoder, rssm, observation and
        reward models and the continue model.
        The actor (_FabricModule).
        The critic (_FabricModule).
    """
    if args.cnn_channels_multiplier <= 0:
        raise ValueError(f"cnn_channels_multiplier must be greater than zero, given {args.cnn_channels_multiplier}")
    if args.dense_units <= 0:
        raise ValueError(f"dense_units must be greater than zero, given {args.dense_units}")
    try:
        cnn_act = getattr(nn, args.cnn_act)
    except AttributeError:
        raise ValueError(
            f"Invalid value for cnn_act, given {args.cnn_act}, "
            "must be one of https://pytorch.org/docs/stable/nn.html#non-linear-activations-weighted-sum-nonlinearity"
        )
    try:
        dense_act = getattr(nn, args.dense_act)
    except AttributeError:
        raise ValueError(
            f"Invalid value for dense_act, given {args.dense_act}, "
            "must be one of https://pytorch.org/docs/stable/nn.html#non-linear-activations-weighted-sum-nonlinearity"
        )

    # Sizes
    latent_state_size = args.stochastic_size + args.recurrent_state_size
    mlp_dims = [obs_space[k].shape[0] for k in mlp_keys]

    # Define models
    cnn_encoder = (
        CNNEncoder(
            keys=cnn_keys,
            input_channels=[int(np.prod(obs_space[k].shape[:-2])) for k in cnn_keys],
            image_size=obs_space[cnn_keys[0]].shape[-2:],
            channels_multiplier=args.cnn_channels_multiplier,
            layer_norm=False,
            activation=cnn_act,
        )
        if cnn_keys is not None and len(cnn_keys) > 0
        else None
    )
    mlp_encoder = (
        MLPEncoder(
            keys=mlp_keys,
            input_dims=mlp_dims,
            mlp_layers=args.mlp_layers,
            dense_units=args.dense_units,
            activation=dense_act,
            layer_norm=False,
        )
        if mlp_keys is not None and len(mlp_keys) > 0
        else None
    )
    encoder = MultiEncoder(cnn_encoder, mlp_encoder)
    recurrent_model = RecurrentModel(
        input_size=sum(actions_dim) + args.stochastic_size, recurrent_state_size=args.recurrent_state_size
    )
    representation_model = MLP(
        input_dims=args.recurrent_state_size + encoder.cnn_output_dim + encoder.mlp_output_dim,
        output_dim=args.stochastic_size * 2,
        hidden_sizes=[args.hidden_size],
        activation=dense_act,
        flatten_dim=None,
    )
    transition_model = MLP(
        input_dims=args.recurrent_state_size,
        output_dim=args.stochastic_size * 2,
        hidden_sizes=[args.hidden_size],
        activation=dense_act,
        flatten_dim=None,
    )
    rssm = RSSM(
        recurrent_model.apply(init_weights),
        representation_model.apply(init_weights),
        transition_model.apply(init_weights),
        args.min_std,
    )
    cnn_decoder = (
        CNNDecoder(
            keys=cnn_keys,
            output_channels=[int(np.prod(obs_space[k].shape[:-2])) for k in cnn_keys],
            channels_multiplier=args.cnn_channels_multiplier,
            latent_state_size=latent_state_size,
            cnn_encoder_output_dim=cnn_encoder.output_dim,
            image_size=obs_space[cnn_keys[0]].shape[-2:],
            activation=cnn_act,
            layer_norm=False,
        )
        if cnn_keys is not None and len(cnn_keys) > 0
        else None
    )
    mlp_decoder = (
        MLPDecoder(
            keys=mlp_keys,
            output_dims=mlp_dims,
            latent_state_size=latent_state_size,
            mlp_layers=args.mlp_layers,
            dense_units=args.dense_units,
            activation=dense_act,
            layer_norm=False,
        )
        if mlp_keys is not None and len(mlp_keys) > 0
        else None
    )
    observation_model = MultiDecoder(cnn_decoder, mlp_decoder)
    reward_model = MLP(
        input_dims=latent_state_size,
        output_dim=1,
        hidden_sizes=[args.dense_units] * args.mlp_layers,
        activation=dense_act,
        flatten_dim=None,
    )
    if args.use_continues:
        continue_model = MLP(
            input_dims=latent_state_size,
            output_dim=1,
            hidden_sizes=[args.dense_units] * args.mlp_layers,
            activation=dense_act,
            flatten_dim=None,
        )
    world_model = WorldModel(
        encoder.apply(init_weights),
        rssm,
        observation_model.apply(init_weights),
        reward_model.apply(init_weights),
        continue_model.apply(init_weights) if args.use_continues else None,
    )
    if "minedojo" in args.env_id:
        actor = MinedojoActor(
            latent_state_size,
            actions_dim,
            is_continuous,
            args.actor_init_std,
            args.actor_min_std,
            args.dense_units,
            dense_act,
            args.mlp_layers,
            distribution="tanh_normal",
            layer_norm=False,
        )
    else:
        actor = Actor(
            latent_state_size,
            actions_dim,
            is_continuous,
            args.actor_init_std,
            args.actor_min_std,
            args.dense_units,
            dense_act,
            args.mlp_layers,
            distribution="tanh_normal",
            layer_norm=False,
        )
    critic = MLP(
        input_dims=latent_state_size,
        output_dim=1,
        hidden_sizes=[args.dense_units] * args.mlp_layers,
        activation=dense_act,
        flatten_dim=None,
    )
    actor.apply(init_weights)
    critic.apply(init_weights)

    # Load models from checkpoint
    if world_model_state:
        world_model.load_state_dict(world_model_state)
    if actor_state:
        actor.load_state_dict(actor_state)
    if critic_state:
        critic.load_state_dict(critic_state)

    # Setup models with Fabric
    world_model.encoder = fabric.setup_module(world_model.encoder)
    world_model.observation_model = fabric.setup_module(world_model.observation_model)
    world_model.reward_model = fabric.setup_module(world_model.reward_model)
    world_model.rssm.recurrent_model = fabric.setup_module(world_model.rssm.recurrent_model)
    world_model.rssm.representation_model = fabric.setup_module(world_model.rssm.representation_model)
    world_model.rssm.transition_model = fabric.setup_module(world_model.rssm.transition_model)
    if world_model.continue_model:
        world_model.continue_model = fabric.setup_module(world_model.continue_model)
    actor = fabric.setup_module(actor)
    critic = fabric.setup_module(critic)

    return world_model, actor, critic
