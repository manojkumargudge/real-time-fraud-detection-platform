"""FastAPI scoring service.

Run locally: ``uvicorn src.serving.api:app`` (env: ``MODEL_DIR``, optional ``DATABASE_URL``;
without it an in-memory store is used and nothing is persisted).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.serving.config import Settings
from src.serving.schema import EVENT_SCHEMA_VERSION, ScoreResponse, TransactionEvent
from src.serving.scorer import Scorer

logger = logging.getLogger(__name__)


def create_app(scorer: Scorer | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if scorer is None:
            settings = Settings.from_env()
            app.state.scorer = Scorer(settings.load_bundle(), settings.build_store())
        else:
            app.state.scorer = scorer
        logger.info(
            "api ready model=%s store=%s",
            app.state.scorer.bundle.model_version,
            app.state.scorer.store.name,
        )
        yield

    app = FastAPI(title="Fraud scoring API", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health(request: Request) -> JSONResponse:
        s: Scorer = request.app.state.scorer
        store_ok = s.store.ping()
        body = {
            "status": "ok" if store_ok else "degraded",
            "model_version": s.bundle.model_version,
            "event_schema_version": EVENT_SCHEMA_VERSION,
            "store": s.store.name,
            "store_ok": store_ok,
        }
        return JSONResponse(body, status_code=200 if store_ok else 503)

    @app.post("/score", response_model=ScoreResponse)
    def score(event: TransactionEvent, request: Request) -> ScoreResponse:
        return request.app.state.scorer.score_event(event)

    return app


app = create_app()
