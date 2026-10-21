FROM python:3.11.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# libgomp1: OpenMP runtime used by XGBoost.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-serving.txt .
RUN pip install -r requirements-serving.txt

COPY src/ src/
RUN useradd --create-home --uid 10001 app
USER app

# Model artifacts are mounted at runtime (never baked into the image or committed).
ENV MODEL_DIR=/app/artifacts/baseline
EXPOSE 8000
CMD ["uvicorn", "src.serving.api:app", "--host", "0.0.0.0", "--port", "8000"]
