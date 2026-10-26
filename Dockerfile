# Debian 12 (bookworm) slim. Security: a current base tag plus `apt-get upgrade` at build time
# picks up Debian security fixes (Trivy on the old 3.11.9 base: 128 HIGH/CRITICAL OS findings).
FROM python:3.11.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# libgomp1: OpenMP runtime used by XGBoost.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# REQUIREMENTS: requirements-serving.txt (default: API / consumer image, the one CI scans and
# pushes), requirements-monitoring.txt (the Compose drift job, which adds Evidently) or
# requirements-dashboard.txt (the analyst dashboard, which adds Streamlit; also scanned/pushed).
ARG REQUIREMENTS=requirements-serving.txt
COPY requirements-serving.txt requirements-monitoring.txt requirements-dashboard.txt ./
# The base image's setuptools / wheel have HIGH advisories (CVE-2024-6345, CVE-2025-47273,
# CVE-2026-24049); they are build tools, upgraded to fixed releases.
RUN pip install --upgrade "setuptools==84.0.0" "wheel==0.48.0" \
    && pip install -r "${REQUIREMENTS}"

COPY src/ src/
COPY configs/drift.toml configs/drift.toml
RUN useradd --create-home --uid 10001 app
USER app

# Model artifacts are mounted at runtime (never baked into the image or committed).
ENV MODEL_DIR=/app/artifacts/baseline
EXPOSE 8000
CMD ["uvicorn", "src.serving.api:app", "--host", "0.0.0.0", "--port", "8000"]
