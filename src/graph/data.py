"""Torch side of the graph: HeteroData and the temporal neighbor loader.

Temporal rule (tests/test_graph.py): every transaction in a seed's sampled subgraph, other than
the seed itself, has a strictly earlier ``TransactionDT`` -- also through entity nodes
(tx -> device/email/card/addr -> tx). PyG's temporal sampler keeps neighbors with
``time <= seed time``; seeds are therefore given ``input_time = TransactionDT - 1`` (times are
whole seconds), which is exactly "strictly earlier". Entity nodes get time 0 (always
reachable; they carry no information of their own). ``disjoint=True`` gives every seed its own
subgraph, so the rule holds per seed, not per batch.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import torch
from torch_geometric.data import HeteroData
from torch_geometric.loader import NeighborLoader

from src.array_types import IntArray
from src.graph.build import ENTITY_COLUMNS, TxGraph

TX = "tx"
DEFAULT_FANOUT = (15, 10)  # per edge type: hop 1 (tx -> entity), hop 2 (entity -> tx)


def to_hetero(graph: TxGraph) -> HeteroData:
    data = HeteroData()
    data[TX].x_num = torch.from_numpy(graph.x_num)
    data[TX].x_cat = torch.from_numpy(graph.x_cat)
    data[TX].y = torch.from_numpy(graph.labels)
    data[TX].time = torch.from_numpy(graph.tx_time)
    data[TX].num_nodes = graph.num_tx
    for node_type in ENTITY_COLUMNS:
        n = len(graph.entity_names[node_type])
        data[node_type].num_nodes = n
        data[node_type].time = torch.zeros(n, dtype=torch.long)
        e = torch.from_numpy(graph.edges[node_type])
        data[TX, f"has_{node_type}", node_type].edge_index = e
        data[node_type, f"rev_{node_type}", TX].edge_index = e.flip(0)
    return data


def temporal_loader(
    data: HeteroData,
    seeds: IntArray,
    *,
    batch_size: int = 1024,
    fanout: Sequence[int] = DEFAULT_FANOUT,
    shuffle: bool = False,
    seed: int = 0,
) -> NeighborLoader:
    """Disjoint temporal neighbor sampling around ``seeds`` (tx node indices)."""
    index = torch.from_numpy(np.asarray(seeds, dtype=np.int64))
    generator = torch.Generator().manual_seed(seed)
    return NeighborLoader(
        data,
        num_neighbors={et: list(fanout) for et in data.edge_types},
        input_nodes=(TX, index),
        input_time=data[TX].time[index] - 1,  # strictly earlier than the seed
        time_attr="time",
        temporal_strategy="uniform",
        disjoint=True,
        batch_size=batch_size,
        shuffle=shuffle,
        generator=generator,
    )
