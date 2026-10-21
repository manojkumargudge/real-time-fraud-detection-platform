"""Runtime settings from environment variables (no secrets have defaults)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from src.models.train import ARTIFACTS_DIR
from src.serving.model import ModelBundle
from src.serving.store import InMemoryStore, PostgresStore, TransactionStore


@dataclass(frozen=True)
class Settings:
    model_dir: Path
    database_url: str | None
    kafka_bootstrap_servers: str
    kafka_topic: str
    kafka_group_id: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            model_dir=Path(os.environ.get("MODEL_DIR", ARTIFACTS_DIR)),
            database_url=os.environ.get("DATABASE_URL") or None,
            kafka_bootstrap_servers=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            kafka_topic=os.environ.get("KAFKA_TOPIC", "transactions"),
            kafka_group_id=os.environ.get("KAFKA_GROUP_ID", "fraud-scorer"),
        )

    def build_store(self) -> TransactionStore:
        return PostgresStore(self.database_url) if self.database_url else InMemoryStore()

    def load_bundle(self) -> ModelBundle:
        return ModelBundle.load(self.model_dir)
