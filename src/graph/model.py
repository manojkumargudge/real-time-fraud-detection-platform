"""Heterogeneous GraphSAGE classifier on transaction nodes."""

from __future__ import annotations

from typing import Any

import torch
from torch import Tensor, nn
from torch_geometric.nn import HeteroConv, SAGEConv

from src.graph.data import TX

EMBED_DIM = 4


class HeteroSAGE(nn.Module):
    """``tx`` inputs: scaled numeric features + categorical embeddings. Entity nodes have no
    features of their own -- a learned vector per node *type*, so values seen only in validation
    need no learned parameters. Two SAGE layers cover tx -> entity -> tx."""

    def __init__(
        self,
        metadata: tuple[list[str], list[tuple[str, str, str]]],
        num_numeric: int,
        category_sizes: list[int],
        hidden: int = 64,
        layers: int = 2,
        dropout: float = 0.1,
    ) -> None:
        super().__init__()
        node_types, edge_types = metadata
        self.embeds = nn.ModuleList(nn.Embedding(n, EMBED_DIM) for n in category_sizes)
        self.tx_in = nn.Linear(num_numeric + EMBED_DIM * len(category_sizes), hidden)
        self.entity_vec = nn.ParameterDict(
            {t: nn.Parameter(torch.zeros(hidden)) for t in node_types if t != TX}
        )
        self.convs = nn.ModuleList(
            HeteroConv({et: SAGEConv((hidden, hidden), hidden) for et in edge_types}, aggr="sum")
            for _ in range(layers)
        )
        self.dropout = nn.Dropout(dropout)
        self.head = nn.Linear(hidden, 1)

    def forward(self, batch: Any) -> Tensor:
        cat = [emb(batch[TX].x_cat[:, i]) for i, emb in enumerate(self.embeds)]
        h: dict[str, Tensor] = {
            TX: torch.relu(self.tx_in(torch.cat([batch[TX].x_num, *cat], dim=-1)))
        }
        for t, vec in self.entity_vec.items():
            h[t] = vec.expand(batch[t].num_nodes, -1)
        for conv in self.convs:
            out = conv(h, batch.edge_index_dict)
            h = {t: self.dropout(torch.relu(out.get(t, h[t]))) for t in h}
        logits: Tensor = self.head(h[TX]).squeeze(-1)
        return logits
