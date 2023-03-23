from typing import List

import torch
import torch.nn as nn
from e3nn.o3 import Irreps
from e3nn.o3 import TensorProduct
from e3nn.nn import FullyConnectedNet
from e3nn.util.jit import compile_mode
from torch_scatter import scatter

import sevenn._keys as KEY
from sevenn._const import AtomGraphDataType
from sevenn.nn.activation import ShiftedSoftPlus


@compile_mode('script')
class IrrepsConvolution(nn.Module):
    """
    same as nequips convolution part (fig 1.d)
    """
    def __init__(
        self,
        irreps_x: Irreps,
        irreps_filter: Irreps,
        irreps_out: Irreps,
        weight_layer_input_to_hidden: List[int],
        weight_layer_act=ShiftedSoftPlus,
        denumerator: float = 1.0,
        data_key_x: str = KEY.NODE_FEATURE,
        data_key_filter: str = KEY.EDGE_ATTR,
        data_key_weight_input: str = KEY.EDGE_EMBEDDING,
        data_key_edge_idx: str = KEY.EDGE_IDX,
        is_parallel: bool = False,
    ):
        super().__init__()
        self.denumerator = denumerator
        self.KEY_X = data_key_x
        self.KEY_FILTER = data_key_filter
        self.KEY_WEIGHT_INPUT = data_key_weight_input
        self.KEY_EDGE_IDX = data_key_edge_idx
        self.is_parallel = is_parallel

        instructions = []
        irreps_mid = []
        for i, (mul_x, ir_x) in enumerate(irreps_x):
            for j, (mul_filter, ir_filter) in enumerate(irreps_filter):
                for ir_out in ir_x * ir_filter:
                    if ir_out in irreps_out:  # here we drop l > lmax
                        k = len(irreps_mid)
                        irreps_mid.append((mul_x, ir_out))
                        instructions.append((i, j, k, "uvu", True))

        irreps_mid = Irreps(irreps_mid)
        irreps_mid, p, _ = irreps_mid.sort()
        instructions = [
            (i_in1, i_in2, p[i_out], mode, train)
            for i_in1, i_in2, i_out, mode, train in instructions
        ]
        self.convolution = TensorProduct(
            irreps_x,
            irreps_filter,
            irreps_mid,
            instructions,
            shared_weights=False,
            internal_weights=False,
        )

        self.weight_nn = FullyConnectedNet(
            weight_layer_input_to_hidden + [self.convolution.weight_numel],
            weight_layer_act
        )

    def forward(self, data: AtomGraphDataType) -> AtomGraphDataType:
        weight = self.weight_nn(data[self.KEY_WEIGHT_INPUT])
        x = data[self.KEY_X]
        i2f self.is_parallel:
            x = torch.cat([x, data[KEY.NODE_FEATURE_GHOST]])

        # note that 1 -> src 0 -> dst
        edge_src = data[self.KEY_EDGE_IDX][1]
        edge_dst = data[self.KEY_EDGE_IDX][0]

        message = self.convolution(
            x[edge_src], data[self.KEY_FILTER], weight
        )

        x = scatter(message, edge_dst, dim=0, dim_size=len(x))
        x.div(self.denumerator)
        if self.is_parallel:
            # NLOCAL is # of atoms in system at 'CPU'
            x = torch.tensor_split(x, data[KEY.NLOCAL])[0]
        data[self.KEY_X] = x
        return data
