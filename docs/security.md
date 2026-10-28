# Security scanning

CI job `security` (.github/workflows/ci.yml) runs on every push and pull request; the image is
only pushed to GHCR when it passes. The scanners gate differently:

- **Trivy** fails on any HIGH/CRITICAL finding in the image that has a fix, unless it is in
  `.trivyignore`. Findings without a fix and MEDIUM/LOW findings are skipped silently.
- **pip-audit** fails on **any** advisory, of any severity, that is not in
  `security/pip-audit-ignore.txt`. New advisories can turn CI red without a code change.
- **bandit** fails on any medium- or high-severity finding in `src/`; low findings are not shown.
- An accepted-risk entry past its expiry date also fails the job.

| Scanner | Scope | Gate |
|---|---|---|
| pip-audit 2.10.1 | `requirements.txt`, `requirements-serving.txt`, `requirements-monitoring.txt`, `requirements-dev.txt`, `requirements-gnn.txt` (with its PyTorch/PyG indexes) | any known vulnerability not in `security/pip-audit-ignore.txt` |
| bandit 1.9.4 | `src/` | medium severity and above (`-ll`) |
| Trivy 0.75.0 | the serving image built from `Dockerfile` | HIGH/CRITICAL with a fix available (`--ignore-unfixed`), minus `.trivyignore` |

Not auditable: the `+cpu` torch and pyg-lib wheels are not on PyPI, so pip-audit skips them in
`requirements-gnn.txt`; torch is audited through its PyPI-equivalent version (`--no-deps`), and
pyg-lib cannot be audited.

Fixed on 2026-10-23 (first scan):

- fastapi 0.115.6 -> 0.143.0 with starlette 0.41.3 -> 1.7.0 (7 starlette advisories: multipart
  and Range-header DoS, Host-header / URL injection, ...): PYSEC-2026-1941 (CVE-2025-54121),
  PYSEC-2026-1942 (CVE-2025-62727), PYSEC-2026-161 (CVE-2026-48710), PYSEC-2026-2280
  (CVE-2026-48817), PYSEC-2026-2281 (CVE-2026-48818), PYSEC-2026-248 (CVE-2026-54282),
  PYSEC-2026-249 (CVE-2026-54283). pip-audit reports each one twice for 0.41.3; 7 are unique.
- mlflow 3.15.2 -> 3.17.2 (3.16.1 first; 3.17.2 fixes CVE-2026-53995 and CVE-2026-115886,
  found 2026-10-24) and cryptography 49.0.0 -> 50.0.2; pytest 8.3.5 -> 9.1.1
- **`main` now requires mlflow 3.17.2+** for the local registry. Newer mlflow refuses an older
  `mlflow.db` ("out-of-date database schema"). Back it up, then run
  `mlflow db upgrade sqlite:///mlflow.db`. The migration is **one-way**: older mlflow versions
  cannot read the file afterwards (keep the backup to go back)
- serving image: base `python:3.11.9-slim` (Debian 12.7) -> `python:3.11.16-slim-bookworm`
  (Debian 12.15) plus `apt-get upgrade` and fixed setuptools/wheel: Trivy HIGH/CRITICAL findings
  133 -> 65, of which 63 have no Debian fix yet
- bandit: no medium/high findings (6 low: asserts, the `<NA>` token, the `git` subprocess)

Accepted risks (reason and expiry in `security/pip-audit-ignore.txt` / `.trivyignore`; CI fails
again after the expiry date, 2027-01-31):

| Package | Advisory | Why accepted |
|---|---|---|
| feast 0.47.0 | CVE-2025-11157 (RCE) | only in the Kubernetes materializer job, which is not used; 0.54 needs numpy 2 / newer pydantic |
| pyarrow 18.0.0 | CVE-2026-25087 | use-after-free when reading Arrow IPC files; only our own parquet is read; pinned by feast |
| torch 2.8.0 (GNN, batch only) | 8 advisories | affected operators (`nan_to_num`, `linalg.lu`, `jit.script`, `lstm_cell`, ...) are not used and nothing is `torch.load`-ed; 2.10 fails to load on Windows. torch is imported only by the batch GNN (`src/graph/`), which nothing in `src/serving/` or `src/streaming/` imports; `requirements-serving.txt` does not install it and the built serving image contains no torch (checked with `pip list` in the container) |
| zlib1g 1:1.2.13.dfsg-1 (Debian 12) | CVE-2023-45853 (CRITICAL) | Debian status `will_not_fix`: a rebuild will **not** fix it. The overflow is in minizip's `zipOpenNewFileInZip4_64`; in the serving image `libz.so.1` does not export that function and no minizip library is installed (checked 2026-10-24). Listed in `.trivyignore` |
| Debian 12 OS packages | 62 more HIGH/CRITICAL without a fix (55 `affected`, 7 `fix_deferred`) | no fixed package exists yet; a rebuild picks up Debian fixes once they are released |
