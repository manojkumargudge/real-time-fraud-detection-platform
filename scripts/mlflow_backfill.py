"""Back-fill MLflow with runs whose results exist only as reports or PostgreSQL rows.

Usage::

    MLFLOW_TRACKING_URI=sqlite:///<path>/mlflow.db DATABASE_URL=... \\
        python -m scripts.mlflow_backfill [--only evaluation,drift,gnn] [--dry-run]

``MLFLOW_TRACKING_URI`` is required (no default): rehearse on a copy of ``mlflow.db`` first.

* ``fraud-evaluation``: the evaluation reports under ``reports/`` (compare, robustness,
  evaluation_study, robust_eval pilot + full, leakage_audit, v1 vs v2, imbalance decision).
  JSON reports give their numeric leaves as metrics; Markdown-only reports are artifacts with
  tags linking the training runs they describe.
* ``fraud-drift``: ``drift_reports`` ids 1-9, read from ``DATABASE_URL`` with SELECT only, in a
  read-only transaction. Each row is stored as a JSON artifact.
* ``fraud-gnn``: the stacking report (tagged with the c233ecd0 run and the sha256 of
  ``artifacts/gnn/scores.parquet``) and the fraud-rings report.

Every run is tagged ``backfilled=true``, ``source_report``, ``report_sha256`` and
``report_git_commit`` (the last commit that touched the file). ``report_sha256`` plus the run
name make it idempotent: a report already logged (by an earlier back-fill or by the module
itself, see ``src.models.tracking.log_report_run``) is skipped. Tracking calls only: the
model registry (versions, aliases) is never called.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mlflow.tracking import MlflowClient

from src.models.robust_eval import RUN_DETAIL
from src.models.tracking import (
    EVALUATION_EXPERIMENT,
    file_sha256,
    flatten_metrics,
    log_report_run,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DRIFT_EXPERIMENT = "fraud-drift"
GNN_EXPERIMENT = "fraud-gnn"
XGB_EXPERIMENT = "fraud-xgboost"
DRIFT_IDS = range(1, 10)  # the rows in the demo database (ids 10-11 exist only in evidence)
STACK_RUN_PREFIX = "c233ecd0"  # the reproduced GNN stacking run (docs/gnn.md)
GNN_SCORES = Path("artifacts") / "gnn" / "scores.parquet"
# Registry v1 and v2 (fraud-xgboost); fixed here so the back-fill never calls the registry.
V1_V2_RUN_IDS = ("ebcc31668174421d93715dd71891d3b0", "71c4f1bd820548c984341a06e51e33fc")
RUN_ID = re.compile(r"\b[0-9a-f]{32}\b")
GROUPS = ("evaluation", "drift", "gnn")


@dataclass
class Item:
    experiment: str
    run_name: str
    files: list[Path]
    metrics: dict[str, float] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    report_sha256: str | None = None  # default: sha256 of the first file


def git_commit_of(path: Path, root: Path = ROOT) -> str:
    """Last commit that touched ``path`` ("untracked" if none)."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", str(path)],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out or "untracked"


def _json_item(
    root: Path, name: str, files: Sequence[str], exclude: tuple[str, ...] = (), **tags: str
) -> Item:
    paths = [root / "reports" / f for f in files]
    report = json.loads(paths[0].read_text(encoding="utf-8"))
    return Item(EVALUATION_EXPERIMENT, name, paths, flatten_metrics(report, exclude), dict(tags))


def evaluation_items(root: Path, client: MlflowClient) -> list[Item]:
    reports = root / "reports"
    items = [
        _json_item(root, "model-comparison", ["model_comparison.json", "model_comparison.md"]),
        _json_item(root, "model-comparison-seeds", ["model_comparison_seeds.json"]),
        _json_item(root, "evaluation-study", ["evaluation_study.json", "evaluation_study.md"]),
        _json_item(
            root,
            "robust-evaluation-pilot",
            ["robust_evaluation_pilot.json", "robust_evaluation_pilot.md"],
            RUN_DETAIL,
            mode="pilot",
        ),
        _json_item(
            root,
            "robust-evaluation-full",
            ["robust_evaluation.json", "robust_evaluation.md"],
            RUN_DETAIL,
            mode="full",
        ),
        _json_item(root, "leakage-audit", ["leakage_audit.json"]),
        Item(
            EVALUATION_EXPERIMENT,
            "model-v1-vs-v2",
            [reports / "model_v1_vs_v2.md"],
            tags={"model_versions": "1,2", "linked_run_ids": ",".join(V1_V2_RUN_IDS)},
        ),
    ]
    imbalance = reports / "imbalance_comparison.md"
    linked = existing_runs(client, XGB_EXPERIMENT, RUN_ID.findall(imbalance.read_text("utf-8")))
    items.append(
        Item(
            EVALUATION_EXPERIMENT,
            "imbalance-decision",
            [imbalance],
            tags={"linked_run_ids": ",".join(linked), "linked_experiment": XGB_EXPERIMENT},
        )
    )
    return items


def existing_runs(client: MlflowClient, experiment: str, run_ids: Sequence[str]) -> list[str]:
    """The ids (deduplicated, in order) that are runs of ``experiment``; others are dropped."""
    exp = client.get_experiment_by_name(experiment)
    known = (
        {r.info.run_id for r in client.search_runs([exp.experiment_id], max_results=1000)}
        if exp
        else set()
    )
    return [r for r in dict.fromkeys(run_ids) if r in known]


def run_by_prefix(client: MlflowClient, experiment: str, prefix: str) -> str:
    exp = client.get_experiment_by_name(experiment)
    runs = client.search_runs([exp.experiment_id], max_results=1000) if exp else []
    matches = [r.info.run_id for r in runs if r.info.run_id.startswith(prefix)]
    if len(matches) != 1:
        raise RuntimeError(f"expected one {experiment} run starting {prefix!r}, got {matches}")
    return str(matches[0])


def gnn_items(root: Path, client: MlflowClient, stack_prefix: str = STACK_RUN_PREFIX) -> list[Item]:
    reports = root / "reports"
    scores_sha = file_sha256(root / GNN_SCORES)
    stack_run = run_by_prefix(client, GNN_EXPERIMENT, stack_prefix)
    common = {"scores_sha256": scores_sha, "scores_path": GNN_SCORES.as_posix()}
    return [
        Item(
            GNN_EXPERIMENT,
            "gnn-stacking-report",
            [reports / "gnn_stacking.md"],
            tags={**common, "linked_run_ids": stack_run, "stage": "stacking-report"},
        ),
        Item(
            GNN_EXPERIMENT,
            "fraud-rings-report",
            [reports / "fraud_rings.md"],
            tags={**common, "linked_run_ids": stack_run, "stage": "rings"},
        ),
    ]


def read_drift_rows(dsn: str, ids: range = DRIFT_IDS) -> list[dict[str, Any]]:
    """``drift_reports`` rows by id, SELECT only, in a read-only transaction."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(
        dsn, row_factory=dict_row, options="-c default_transaction_read_only=on"
    ) as conn:
        rows = conn.execute(
            "SELECT * FROM drift_reports WHERE id BETWEEN %s AND %s ORDER BY id",
            (ids.start, ids.stop - 1),
        ).fetchall()
        conn.rollback()
    print(f"drift_reports rows read: {len(rows)} (ids {ids.start}-{ids.stop - 1})")
    if [r["id"] for r in rows] != list(ids):
        raise RuntimeError(f"expected drift_reports ids {list(ids)}, got {[r['id'] for r in rows]}")
    return [dict(r) for r in rows]


def drift_items(rows: list[dict[str, Any]], out_dir: Path) -> list[Item]:
    items = []
    for row in rows:
        text = json.dumps(row, indent=2, sort_keys=True, default=str) + "\n"
        path = out_dir / f"drift_report_{row['id']}.json"
        path.write_text(text, encoding="utf-8")
        tags = {
            "drift_report_id": str(row["id"]),
            "run_at": str(row["run_at"]),
            "triggered": str(row["triggered"]).lower(),
            "forced": str(row.get("forced", False)).lower(),
            "synthetic": str(row["synthetic"]).lower(),
            "source_report": f"postgres:drift_reports/{row['id']}",
            "report_git_commit": "n/a (database row)",
        }
        for key in ("promotion_decision", "retrain_run_id", "challenger_version"):
            if row.get(key) is not None:
                tags[key] = str(row[key])
        metrics = flatten_metrics({k: v for k, v in row.items() if k != "id"})
        items.append(
            Item(
                DRIFT_EXPERIMENT,
                f"drift-report-{row['id']}",
                [path],
                metrics,
                tags,
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    return items


def log_items(items: list[Item], uri: str, root: Path, *, dry_run: bool) -> list[str]:
    run_ids = []
    for item in items:
        first = item.files[0].resolve()
        tags = {
            "backfilled": "true",
            "source_report": first.relative_to(root.resolve()).as_posix()
            if first.is_relative_to(root.resolve())
            else first.name,
            "report_git_commit": git_commit_of(item.files[0], root),
            **item.tags,
        }
        if dry_run:
            print(f"[dry-run] {item.experiment} {item.run_name}: {len(item.metrics)} metrics")
            continue
        run_ids.append(
            log_report_run(
                item.run_name,
                item.files,
                metrics=item.metrics,
                tags=tags,
                experiment=item.experiment,
                report_sha256=item.report_sha256,
                uri=uri,
            )
        )
    return run_ids


def main(argv: list[str] | None = None) -> list[str]:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--only", default=",".join(GROUPS), help="comma list of " + "/".join(GROUPS)
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="repo with reports/ and artifacts/gnn/"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    groups = [g for g in args.only.split(",") if g]
    if unknown := set(groups) - set(GROUPS):
        parser.error(f"unknown groups {sorted(unknown)}")
    uri = os.environ.get("MLFLOW_TRACKING_URI")
    if not uri:
        parser.error("set MLFLOW_TRACKING_URI explicitly (rehearse on a copy of mlflow.db first)")
    client = MlflowClient(tracking_uri=uri)
    items: list[Item] = []
    with tempfile.TemporaryDirectory() as tmp:
        if "evaluation" in groups:
            items += evaluation_items(args.root, client)
        if "drift" in groups:
            dsn = os.environ.get("DATABASE_URL")
            if not dsn:
                parser.error("the drift group needs DATABASE_URL")
            items += drift_items(read_drift_rows(dsn), Path(tmp))
        if "gnn" in groups:
            items += gnn_items(args.root, client)
        run_ids = log_items(items, uri, args.root, dry_run=args.dry_run)
    print(f"items {len(items)}, runs (new or already logged) {len(run_ids)}")
    return run_ids


if __name__ == "__main__":
    main()
