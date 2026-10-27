# Next step (proposal, not implemented): link the served model to its MLflow registry version

Proposed 2026-10-27 (Part 2C). Not built. The text below is the proposal as written.

## 1. How the model is loaded today
- **Source:** a file, not a registry URI. `ModelBundle.load(MODEL_DIR)` in [src/serving/model.py](../../src/serving/model.py) is called from `Settings.load_bundle()` in [src/serving/config.py:60](../../src/serving/config.py#L60). The API and the consumers both go through `Settings.from_env()`.
- **Env var:** `MODEL_DIR=/app/artifacts/baseline`. It is set in [docker-compose.yml:12](../../docker-compose.yml#L12), [k8s/app-config.yaml:11](../../k8s/app-config.yaml#L11), `Dockerfile` and `Dockerfile.k8s`.
  - Compose bind-mounts `./artifacts/baseline` read-only.
  - Kubernetes bakes the bundle into the image (`Dockerfile.k8s` copies `artifacts/baseline/`).
- **Files:** `model.json` and `metadata.json`. On load it checks:
  - `model.json` against `metadata.model_sha256`;
  - the schema version, the variant, the feature list and the threshold.
- **Version shown:** `model_version = "baseline-<first 12 hex of the sha256>"` (baseline-9fdad27ea3b0). It appears in `/health` and is stored on every row.
- **Missing:** serving never imports mlflow, and the bundle has no registry name, version or run_id.

## 2. Served bundle vs MLflow v2
I queried a copy of mlflow.db; the original's hash was the same before and after.

| file | served (`artifacts/baseline`) | MLflow v2 (`mlruns/71c4f1bd…/artifacts/model`) |
|---|---|---|
| model.json | `9fdad27ea3b07d5c29159bae75acc3ecaa7bf671943ab543bf9385145b89ba88` | same, **identical** |
| metadata.json | `a65a6dfcb68bf899ff80ba171f9a0252aa8e2dadb97a8f0428f89f53868cf9c8` | same, **identical** |

- champion = production = **v2**, run_id `71c4f1bd820548c984341a06e51e33fc`.
- The v2 run's tags show `git_commit 7754fd4` with `git_dirty=true`.
- challenger = v7.

## 3. Proposed change (smallest)
1. **Link file, written on the host:** a new CLI, `python -m src.models.link_registry --alias production`. It runs on the host, which has mlflow.
   - It reads the alias from the registry, downloads nothing, and hashes the registry's `model.json`.
   - If that hash doesn't equal the served `model.json`, it refuses.
   - Otherwise it writes `artifacts/baseline/registry.json` with `{name, version, alias, run_id, model_sha256, linked_at}`.
   - `metadata.json` stays untouched, so the bundle stays byte-identical to v2.
2. **Loader:** `ModelBundle.load` reads `registry.json` if it exists. If its `model_sha256` doesn't match the loaded model, loading fails: a wrong link is worse than none. If the file is absent, the registry fields are `None`. Serving still never needs mlflow.
3. **API:** a new `GET /model` returns:
   - `model_name`, `registry_version`, `alias`, `run_id`, `model_sha256`, `model_version` and `linked` (true/false).
   - `/health` stays unchanged, so the probes and existing clients aren't affected.
   - The API and consumer startup log lines also gain the registry version.
4. **Per-row version: skipped. It would be a migration, and it's barely needed.**
   - `scored_transactions.model_version` already records `baseline-9fdad27ea3b0` on every row. That is the artifact hash prefix, which maps to registry v2 through `registry.json` and `/model`.
   - A new `registry_version` column would be an `ALTER TABLE … ADD COLUMN`. On PostgreSQL 16 that's cheap for a nullable column, but `store.py` runs its DDL on startup, so it would change the demo DB schema. That breaks your no-demo-DB-changes rule, so I'd leave it out.
5. **Kubernetes:** add `!artifacts/baseline/registry.json` to `Dockerfile.k8s.dockerignore`, so the link gets baked into the image.

## 4. Tests and files
**Tests:**
- `tests/test_serving.py`:
  - load with `registry.json` gives the right fields;
  - load without it gives `None`;
  - a hash mismatch raises `ModelBundleError`;
  - `/model` works both linked and unlinked;
  - the `/health` body is unchanged.
- `tests/test_tracking.py`, using the temp sqlite MLflow its tests already use:
  - register a run, link `--alias`, and check `registry.json`;
  - refuse when the registry artifact hash differs;
  - refuse an unknown alias.

**Files:**
- New: `src/models/link_registry.py`.
- Changed: `src/serving/model.py`, `src/serving/api.py`, `src/streaming/consumer.py` (log line only), `Dockerfile.k8s.dockerignore`, `docs/serving.md`, the README, and CLAUDE.md (remove the limitation).
- Applying it to the demo means writing `artifacts/baseline/registry.json` and restarting the API and consumers. There are no DB writes, so the counts stay 118108 / 5093 / 9 / 20 / 6.

## 5. Estimate
**About 60–90 minutes, which is more than 40.** That covers:
- the code and tests (about 35 min);
- docs and the k8s dockerignore line;
- the PR and a CI wait (about 15 min);
- linking and restarting the demo stack, then checking `/model` and the counts (about 15 min).

Showing the registry version on the dashboard's model tile is a separate follow-up and isn't in this estimate. The dashboard reads the DB, not the API.
