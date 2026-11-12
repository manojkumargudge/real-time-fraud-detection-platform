"""Link the served model bundle to its MLflow registry version (host only).

Usage::

    python -m src.models.link_registry --alias production [--bundle artifacts/baseline] [--check]

Reads the alias from the registry (never moves it), hashes the registry version's
``model.json`` and compares it with the served ``model.json``. Only if they are identical does
it write ``<bundle>/registry.json`` = ``{name, version, alias, run_id, model_sha256,
linked_at}``; ``metadata.json`` and ``model.json`` stay untouched, so the bundle stays
byte-identical to the registry version. ``--check`` compares and reports without writing.

Serving never imports mlflow: ``ModelBundle.load`` reads ``registry.json`` and refuses to start
if its ``model_sha256`` differs from the loaded model (a wrong link is worse than none).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.models.train import ARTIFACTS_DIR, MODEL_FILE

logger = logging.getLogger(__name__)

REGISTRY_FILE = "registry.json"


class LinkError(RuntimeError):
    """The registry version does not match the served bundle (or cannot be read)."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def registry_link(alias: str, bundle_dir: Path, uri: str | None = None) -> dict[str, Any]:
    """The link record for ``fraud-xgboost@alias`` if its ``model.json`` equals the served one.

    Read-only on the registry: resolves the alias, copies the version's ``model.json`` into a
    temporary directory and hashes it. Raises ``LinkError`` on any mismatch."""
    from mlflow.exceptions import MlflowException
    from mlflow.tracking import MlflowClient

    from src.models.tracking import ARTIFACT_PATH, MODEL_NAME, tracking_uri

    uri = uri or tracking_uri()
    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    try:
        mv = client.get_model_version_by_alias(MODEL_NAME, alias)
    except MlflowException as exc:
        raise LinkError(f"{MODEL_NAME}@{alias} not found in {uri}") from exc
    if not mv.run_id:
        raise LinkError(f"{MODEL_NAME}@{alias} (v{mv.version}) has no run")
    served = bundle_dir / MODEL_FILE
    if not served.is_file():
        raise LinkError(f"missing served model {served}")
    with tempfile.TemporaryDirectory() as tmp:
        path = client.download_artifacts(mv.run_id, f"{ARTIFACT_PATH}/{MODEL_FILE}", tmp)
        registry_sha = _sha256(Path(path))
    served_sha = _sha256(served)
    if registry_sha != served_sha:
        raise LinkError(
            f"{MODEL_NAME}@{alias} is v{mv.version} with model.json sha256 {registry_sha[:12]}..., "
            f"but the served model.json is {served_sha[:12]}...: not linking"
        )
    return {
        "name": MODEL_NAME,
        "version": str(mv.version),
        "alias": alias,
        "run_id": str(mv.run_id),
        "model_sha256": served_sha,
        "linked_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def write_link(record: dict[str, Any], bundle_dir: Path) -> Path:
    path = bundle_dir / REGISTRY_FILE
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--alias", default="production")
    parser.add_argument("--bundle", type=Path, default=ARTIFACTS_DIR)
    parser.add_argument("--check", action="store_true", help="compare only, write nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        record = registry_link(args.alias, args.bundle)
    except LinkError as exc:
        logger.error("%s", exc)
        return 1
    if args.check:
        print(json.dumps({**record, "written": False}))
        return 0
    path = write_link(record, args.bundle)
    print(json.dumps({**record, "written": str(path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
