"""Runtime settings from environment variables (no secrets have defaults).

Feature store (docs/feature_store.md): ``FEAST_REDIS`` (``host:port``) enables Feast; every
saved event then updates the entity state in Redis. ``FEATURE_SOURCE=feast`` additionally reads
the history features from Feast instead of the event history in the store (default ``store``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from src.models.train import ARTIFACTS_DIR
from src.serving.model import ModelBundle
from src.serving.store import InMemoryStore, PostgresStore, TransactionStore

if TYPE_CHECKING:
    from src.serving.scorer import Scorer

FEATURE_SOURCES = ("store", "feast")


@dataclass(frozen=True)
class Settings:
    model_dir: Path
    database_url: str | None
    kafka_bootstrap_servers: str
    kafka_topic: str
    kafka_group_id: str
    kafka_dlq_topic: str = "transactions.dlq"
    feature_source: str = "store"
    feast_redis: str | None = None
    feast_repo_dir: Path = ARTIFACTS_DIR.parent / "feast"

    def __post_init__(self) -> None:
        if self.feature_source not in FEATURE_SOURCES:
            raise ValueError(f"FEATURE_SOURCE must be one of {FEATURE_SOURCES}")
        if self.feature_source == "feast" and not self.feast_redis:
            raise ValueError("FEATURE_SOURCE=feast needs FEAST_REDIS (online store host:port)")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            model_dir=Path(os.environ.get("MODEL_DIR", ARTIFACTS_DIR)),
            database_url=os.environ.get("DATABASE_URL") or None,
            kafka_bootstrap_servers=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            kafka_topic=os.environ.get("KAFKA_TOPIC", "transactions"),
            kafka_group_id=os.environ.get("KAFKA_GROUP_ID", "fraud-scorer"),
            kafka_dlq_topic=os.environ.get("KAFKA_DLQ_TOPIC", "transactions.dlq"),
            feature_source=os.environ.get("FEATURE_SOURCE", "store"),
            feast_redis=os.environ.get("FEAST_REDIS") or None,
            feast_repo_dir=Path(os.environ.get("FEAST_REPO_DIR", ARTIFACTS_DIR.parent / "feast")),
        )

    def build_store(self) -> TransactionStore:
        return PostgresStore(self.database_url) if self.database_url else InMemoryStore()

    def load_bundle(self) -> ModelBundle:
        return ModelBundle.load(self.model_dir)

    def build_scorer(self) -> Scorer:
        from src.serving.scorer import Scorer

        bundle = self.load_bundle()
        feast = None
        if self.feast_redis:  # imported only when enabled
            from src.features.feast_store import FeastFeatures, open_store

            store = open_store(
                self.feast_repo_dir, redis=self.feast_redis, config=bundle.history_config
            )
            feast = FeastFeatures(store, bundle.history_config)
        return Scorer(
            bundle, self.build_store(), feast=feast, read_from_feast=self.feature_source == "feast"
        )
