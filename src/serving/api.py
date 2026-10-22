"""FastAPI scoring service.

Run locally: ``uvicorn src.serving.api:app`` (env: ``MODEL_DIR``, optional ``DATABASE_URL``;
without it an in-memory store is used and nothing is persisted).
"""

from __future__ import annotations

import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.serving.config import Settings
from src.serving.schema import EVENT_SCHEMA_VERSION, ScoreResponse, TransactionEvent
from src.serving.scorer import Scorer

logger = logging.getLogger(__name__)


def _json_safe(o: Any) -> Any:
    """Replace non-finite floats (``NaN``/``Infinity``) with their string form."""
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_json_safe(v) for v in o]
    if isinstance(o, float) and not math.isfinite(o):
        return str(o)
    return o


async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's default handler echoes the rejected input; a JSON body with NaN/Infinity then
    # fails to render (strict JSON) and the client got HTTP 500 instead of 422.
    return JSONResponse({"detail": _json_safe(jsonable_encoder(exc.errors()))}, status_code=422)


def create_app(scorer: Scorer | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if scorer is None:
            # uvicorn configures only its own loggers; without this, every src.* INFO record
            # (model version, store, scores, shutdown) was silently dropped. No-op if the root
            # logger already has handlers.
            logging.basicConfig(
                level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
            )
            settings = Settings.from_env()
            app.state.scorer = Scorer(settings.load_bundle(), settings.build_store())
        else:
            app.state.scorer = scorer
        logger.info(
            "api ready model=%s store=%s",
            app.state.scorer.bundle.model_version,
            app.state.scorer.store.name,
        )
        try:
            yield
        finally:
            if scorer is None:  # close only what this app opened; injected scorers are the caller's
                app.state.scorer.store.close()
                logger.info("api stopped; store closed")

    app = FastAPI(title="Fraud scoring API", version="0.1.0", lifespan=lifespan)
    app.exception_handler(RequestValidationError)(_validation_error)

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
        s: Scorer = request.app.state.scorer
        return s.score_event(event)

    return app


app = create_app()
