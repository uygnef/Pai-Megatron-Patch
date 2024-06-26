# Copyright (c) 2023 Alibaba PAI and Nvidia Megatron-LM Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from abc import ABC, abstractmethod
import torch
from torch.nn.modules.module import Module
from typing import Any, Optional, Tuple
import torch.nn as nn

from megatron import get_args
from megatron.core import parallel_state
from megatron.core.transformer.module import MegatronModule

from .experts import GroupedMLP, SequentialMLP
from .router import TopKRouter
from .token_dispatcher import MoEDroplessTokenDispatcher
from ..transformer_config import TransformerConfig
from ..load_balance import LoadBalancer
from ..transformer.mlp import MLPSubmodules

class BaseMoELayer(MegatronModule, ABC):
    """Base class for a mixture of experts layer.

    Args:
        config (TransformerConfig): Configuration object for the transformer model.
    """

    def __init__(self, config: TransformerConfig):
        super(BaseMoELayer, self).__init__(config)
        self.config = config
        self.expert_parallel_size = parallel_state.get_expert_model_parallel_world_size()
        assert self.config.num_moe_experts % self.expert_parallel_size == 0
        self.num_local_experts = self.config.num_moe_experts // self.expert_parallel_size
        local_expert_indices_offset = (
            parallel_state.get_expert_model_parallel_rank() * self.num_local_experts
        )
        self.local_expert_indices = [
            local_expert_indices_offset + i for i in range(self.num_local_experts)
        ]
        self.router = None
        self.experts = None
        self.token_dispatcher = None

    @abstractmethod
    def forward(self, hidden_states):
        pass


class MoELayer(BaseMoELayer):
    """Mixture of experts Layer **currently only supports no token dropping**.

    Args:
        BaseMoELayer (MegatronModule): Base class for MoE layers
    """

    def __init__(self, config: TransformerConfig, submodules: MLPSubmodules = None, layer_number=None):
        self.submodules = submodules
        super(MoELayer, self).__init__(config=config)
        self.router = TopKRouter(
            self.num_local_experts, self.local_expert_indices, config=self.config, layer_num=layer_number,
        )
        if self.config.moe_grouped_gemm:
            self.experts = GroupedMLP(self.num_local_experts, self.config)
        else:
            assert isinstance(self.submodules, MLPSubmodules)
            self.experts = SequentialMLP(self.num_local_experts, self.config, self.submodules)
        self.token_dispatcher = MoEDroplessTokenDispatcher(
            self.num_local_experts, self.local_expert_indices, config=self.config
        )
        args = get_args()
        self.enable_moe_load_balance = args.load_balance_interval is not None
        self.load_balancer = LoadBalancer(self.experts, self.router)


    def forward(self, hidden_states: torch.Tensor):
        """
        Forward pass for the MoE layer.

        The method routes input tokens to the appropriate expert networks,
        processes the tokens with the experts, and then combines the outputs.

        Args:
            hidden_states (torch.Tensor): The input tensor containing the hidden states
            from the previous layer of the transformer model.This tensor is expected to 
            have a shape compatible with the expectations of the MoE layer, typically
            [batch_size, sequence_length, hidden_size].

        Returns:
            Tupletorch.Tensor, torch.Tensor: A tuple containing two elements:
                - The first element is the output tensor after processing by the MoE layer.
                  It has the same shape as the input hidden_states.
                - The second element is the bias introduced by the MLP experts, which may
                need to be accounted for in subsequent layers or loss calculations.
        """
        # process MoE
        scores, indices = self.router(hidden_states)
        (
            dispatched_input,
            tokens_per_expert,
            scores,
            indices,
            global_local_map,
        ) = self.token_dispatcher.token_permutation(hidden_states, scores, indices)

        # MoE expert load balance
        if self.enable_moe_load_balance:
            with torch.no_grad():
                self.load_balancer.update_load(tokens_per_expert)
        expert_output, mlp_bias = self.experts(dispatched_input, tokens_per_expert)
        output, mlp_bias = self.token_dispatcher.token_unpermutation(
            expert_output, scores, indices, global_local_map, mlp_bias
        )
        return output, mlp_bias

def apply_load_balance(model: nn.Module, optim: Any) -> None:
    """
    apply load balance to every experts in the model
    """

    def _apply_recursive(module: nn.Module):
        for _, sub_module in module.named_children():
            if isinstance(sub_module, MoELayer):
                # if sub_module.enable_load_balance == True:
                sub_module.load_balancer.balance_load(optim)
            _apply_recursive(sub_module)

    torch.cuda.empty_cache()
    _apply_recursive(model[0])
    torch.cuda.empty_cache()


def print_token_dist(model: nn.Module, step) -> None:
    """
    apply load balance to every experts in the model
    """

    def _apply_recursive(module: nn.Module):
        for _, sub_module in module.named_children():
            if isinstance(sub_module, MoELayer):
                sub_module.load_balancer.print_token_dist(step)
            _apply_recursive(sub_module)
    _apply_recursive(model[0])
