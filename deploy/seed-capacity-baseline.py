"""Materialize the verified seed under main's fixed Feast project."""

import json
import os
from pathlib import Path

from src.features import feast_store
from src.features.feast_checkpoint import load_seed
from src.serving.model import ModelBundle

bundle = ModelBundle.load(Path(os.environ["MODEL_DIR"]))
seed = load_seed(
    Path(os.environ["FEAST_SEED_ARTIFACT"]),
    os.environ["FEAST_SEED_SHA256"],
    bundle,
    int(os.environ["FEAST_SEED_CUTOFF"]),
)
repo = Path(os.environ["FEAST_REPO_DIR"])
config = bundle.history_config
users, cards = feast_store.fold_states(seed, config, snapshots=False)
online = feast_store.open_store(
    repo, redis=os.environ["FEAST_REDIS"], config=config, project="fraud"
)
feast_store.write_offline(repo, users, cards, config=config)
feast_store.materialize_all(online)
features = feast_store.FeastFeatures(online, config)
for entity_type, key_field, fields, rows in (
    ("uid", feast_store.USER_ID, feast_store.uid_fields(), users),
    ("card", feast_store.CARD_ID, feast_store.card_fields(config.change_columns), cards),
):
    for row in rows:
        digest = feast_store.online_digest(row, fields)
        if digest is None or features.entity_digest(entity_type, row[key_field]) != digest:
            raise RuntimeError("Main's initial Redis state failed read-back verification")
print(json.dumps({"generation": "fraud", "users": len(users), "cards": len(cards)}))
