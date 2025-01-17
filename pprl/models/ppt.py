from __future__ import annotations

from typing import Callable

import gymnasium.spaces as spaces
import torch
import torch.nn as nn
from parllel import ArrayTree
from torch import Tensor

from pprl.envs import PointCloudSpace
from pprl.models.modules.tokenizer import Tokenizer
from pprl.models.modules.transformer import SequencePooling, TransformerEncoder
from pprl.utils.array_dict import dict_to_batched_data


class PointPatchTransformer(nn.Module):
    def __init__(
        self,
        obs_space: spaces.Space,
        tokenizer: Callable,
        pos_embedder: Callable,
        transformer_encoder: Callable,
        embed_dim: int,
        state_embed_dim: int | None = None,
    ) -> None:
        super().__init__()

        if obs_is_dict := isinstance(obs_space, spaces.Dict):
            point_space = obs_space["points"]
        else:
            point_space = obs_space
        assert isinstance(point_space, PointCloudSpace)
        self.obs_is_dict = obs_is_dict

        point_dim = point_space.shape[0]
        self.tokenizer: Tokenizer = tokenizer(point_dim=point_dim, embed_dim=embed_dim)
        self.pos_embedder: nn.Module = pos_embedder(token_dim=embed_dim)
        self.transformer_encoder: TransformerEncoder = transformer_encoder(
            embed_dim=embed_dim
        )
        self.pooling = SequencePooling(embed_dim=embed_dim)

        # initialize weights of attention layers before creating further modules
        self.apply(self._init_weights)

        self.state_encoder = None
        if self.obs_is_dict:
            state_dim = sum(
                space.shape[0] for name, space in obs_space.items() if name != "points"
            )
            # maybe create linear projection layer for state vector
            if state_embed_dim is not None:
                self.state_encoder = nn.Linear(state_dim, state_embed_dim)
                self.state_dim = state_embed_dim
            else:
                self.state_dim = state_dim
        else:
            self.state_dim = 0

    def forward(self, observation: ArrayTree[Tensor]) -> Tensor:
        point_cloud: ArrayTree[Tensor] = (
            observation["points"] if self.obs_is_dict else observation
        )
        pos, batch, color = dict_to_batched_data(point_cloud)  # type: ignore

        x, _, center_points = self.tokenizer(pos, batch, color)
        pos = self.pos_embedder(center_points)
        x = self.transformer_encoder(x, pos)
        encoder_out = self.pooling(x)

        if self.obs_is_dict:
            state = [elem for name, elem in observation.items() if name != "points"]
            if self.state_encoder is not None:
                state = torch.concatenate(state, dim=-1)
                state = self.state_encoder(state)
                state = [state]

            encoder_out = torch.concatenate([encoder_out] + state, dim=-1)

        return encoder_out

    @property
    def embed_dim(self) -> int:
        return self.transformer_encoder.embed_dim + self.state_dim

    def _init_weights(self, m):
        # TODO: verify weight init for various versions of point MAE/GPT
        if isinstance(m, nn.Linear):
            nn.init.trunc_normal_(m.weight, std=0.02)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
