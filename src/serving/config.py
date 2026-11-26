"""Runtime settings from environment variables (no secrets have defaults).

Feature store (docs/feature_store.md): ``FEATURE_SOURCE`` selects where the history features
come from -- ``feast`` (default: entity state in Redis, started from the training history, so
features match training) or ``store`` (the card's event history in PostgreSQL; skewed for
entities with history before the stream). ``FEAST_REDIS`` (``host:port``) is required for
``feast``; when set, every saved event also updates the Feast state.
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
    feature_source: str = "feast"
    feast_redis: str | None = None
    feast_repo_dir: Path = ARTIFACTS_DIR.parent / "feast"

    def __post_init__(self) -> None:
        if self.feature_source not in FEATURE_SOURCES:
            raise ValueError(f"FEATURE_SOURCE must be one of {FEATURE_SOURCES}")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            model_dir=Path(os.environ.get("MODEL_DIR", ARTIFACTS_DIR)),
            database_url=os.environ.get("DATABASE_URL") or None,
            kafka_bootstrap_servers=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            kafka_topic=os.environ.get("KAFKA_TOPIC", "transactions"),
            kafka_group_id=os.environ.get("KAFKA_GROUP_ID", "fraud-scorer"),
            kafka_dlq_topic=os.environ.get("KAFKA_DLQ_TOPIC", "transactions.dlq"),
            feature_source=os.environ.get("FEATURE_SOURCE", "feast"),
            feast_redis=os.environ.get("FEAST_REDIS") or None,
            feast_repo_dir=Path(os.environ.get("FEAST_REPO_DIR", ARTIFACTS_DIR.parent / "feast")),
        )

    def build_store(self) -> TransactionStore:
        return PostgresStore(self.database_url) if self.database_url else InMemoryStore()

    def load_bundle(self) -> ModelBundle:
        return ModelBundle.load(self.model_dir)

    def build_scorer(self) -> Scorer:
        """Checked here, not at construction: the producer reads Settings but never scores."""
        if self.feature_source == "feast" and not self.feast_redis:
            raise ValueError(
                "FEATURE_SOURCE=feast (the default) needs FEAST_REDIS=host:port; "
                "set FEATURE_SOURCE=store to use the event-history path"
            )
        bundle = self.load_bundle()
        durable = self.build_store()
        try:
            return self._open_scorer(bundle, durable)
        except BaseException:
            durable.close()
            raise

    def _open_scorer(self, bundle: ModelBundle, durable: TransactionStore) -> Scorer:
        from src.serving.scorer import Scorer

        feast = None
        checkpoint = durable.feast_checkpoint() if isinstance(durable, PostgresStore) else None
        generation = None if checkpoint is None else checkpoint[0]
        required = os.getenv("FEAST_REQUIRE_CHECKPOINT") == "1"
        if (required or checkpoint is not None) and (
            checkpoint is None or self.feature_source != "feast" or not self.feast_redis
        ):
            raise ValueError("Scoring requires a promoted PostgreSQL/Redis Feast checkpoint")
        if self.feast_redis:  # imported only when enabled
            from src.features.feast_store import FeastFeatures, open_store

            if checkpoint is None:
                store = open_store(
                    self.feast_repo_dir, redis=self.feast_redis, config=bundle.history_config
                )
            else:
                from src.features.feast_checkpoint import load_seed

                load_seed(
                    Path(os.environ["FEAST_SEED_ARTIFACT"]),
                    checkpoint[1],
                    bundle,
                    int(os.environ["FEAST_SEED_CUTOFF"]),
                )
                store = open_store(
                    self.feast_repo_dir / checkpoint[0],
                    redis=self.feast_redis,
                    config=bundle.history_config,
                    project=checkpoint[0],
                )
            feast = FeastFeatures(store, bundle.history_config)
        production = os.getenv("FRAUD_PRODUCTION") == "1"
        scorer = Scorer(
            bundle,
            durable,
            feast=feast,
            read_from_feast=self.feature_source == "feast",
            reject_late=production,
            feast_generation=generation,
        )
        if production or generation is not None:
            scorer.recover_pending_feast()
        if generation is not None:
            from src.features.feast_checkpoint import verify_generation

            assert feast is not None and isinstance(durable, PostgresStore)
            verify_generation(durable, feast, generation)
        return scorer
