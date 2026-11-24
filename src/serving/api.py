"""FastAPI scoring service.

Run locally: ``uvicorn src.serving.api:app`` (env: ``MODEL_DIR``, optional ``DATABASE_URL``;
without it an in-memory store is used and nothing is persisted).
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from src.serving.auth import API_KEY_HEADER, key_matches, load_api_key
from src.serving.config import Settings
from src.serving.schema import EVENT_SCHEMA_VERSION, ScoreResponse, TransactionEvent
from src.serving.scorer import LateEventError, Scorer

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


_FROM_ENV = object()  # sentinel: read the key from API_KEY
# auto_error=False: the dependency answers 401 itself (FastAPI's default would be 403); the
# scheme makes /openapi.json document the header on POST /score.
_API_KEY_SCHEME = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


def create_app(
    scorer: Scorer | None = None,
    *,
    api_key: str | None | object = _FROM_ENV,
    production: bool | None = None,
) -> FastAPI:
    """``api_key``: the key ``POST /score`` requires (default: ``load_api_key()``; None = no key
    configured, so every ``/score`` call gets 401)."""
    key: str | None = load_api_key() if api_key is _FROM_ENV else api_key  # type: ignore[assignment]
    production = os.getenv("FRAUD_PRODUCTION") == "1" if production is None else production
    if production and (key is None or len(key) < 32):
        raise ValueError("Production API requires an API_KEY of at least 32 characters")
    requests_per_minute = int(os.getenv("API_REQUESTS_PER_MINUTE", "600")) if production else 0
    if production and requests_per_minute < 1:
        raise ValueError("API_REQUESTS_PER_MINUTE must be positive")
    rate_lock = Lock()
    tokens = float(requests_per_minute)
    last_refill = time.monotonic()

    def require_api_key(given: str | None = Security(_API_KEY_SCHEME)) -> None:
        if not key_matches(key, given):
            raise HTTPException(
                status_code=401,
                detail=f"missing or invalid {API_KEY_HEADER} header",
                headers={"WWW-Authenticate": API_KEY_HEADER},
            )

    def limit_scoring() -> None:
        nonlocal tokens, last_refill
        if not requests_per_minute:
            return
        with rate_lock:
            now = time.monotonic()
            tokens = min(
                requests_per_minute, tokens + (now - last_refill) * requests_per_minute / 60
            )
            last_refill = now
            if tokens < 1:
                retry_after = math.ceil((1 - tokens) * 60 / requests_per_minute)
                raise HTTPException(
                    status_code=429,
                    detail="Scoring rate limit exceeded",
                    headers={"Retry-After": str(retry_after)},
                )
            tokens -= 1

    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        if production:
            return JSONResponse({"detail": "Invalid transaction"}, status_code=422)
        return await _validation_error(request, exc)

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
            if production and not settings.database_url:
                raise ValueError("Production API requires DATABASE_URL")
            app.state.scorer = settings.build_scorer()
        else:
            app.state.scorer = scorer
        if key is None:
            logger.error("no API key configured (API_KEY): every POST /score gets 401")
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

    app = FastAPI(
        title="Fraud scoring API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    app.exception_handler(RequestValidationError)(validation_error)

    @app.get("/health")
    def health(request: Request) -> JSONResponse:
        s: Scorer = request.app.state.scorer
        store_ok = s.store.ping()
        if production:
            return JSONResponse(
                {"status": "ok" if store_ok else "degraded"},
                status_code=200 if store_ok else 503,
            )
        body = {
            "status": "ok" if store_ok else "degraded",
            "model_version": s.bundle.model_version,
            "event_schema_version": EVENT_SCHEMA_VERSION,
            "store": s.store.name,
            "store_ok": store_ok,
        }
        return JSONResponse(body, status_code=200 if store_ok else 503)

    @app.get("/model", dependencies=[Security(require_api_key)] if production else [])
    def model(request: Request) -> dict[str, Any]:
        """The served model and, if linked, its MLflow registry version (``registry.json``)."""
        bundle = request.app.state.scorer.bundle
        link = bundle.registry
        return {
            "model_version": bundle.model_version,
            "model_sha256": bundle.metadata["model_sha256"],
            "threshold": bundle.threshold,
            "linked": link is not None,
            "model_name": link.name if link else None,
            "registry_version": link.version if link else None,
            "alias": link.alias if link else None,
            "run_id": link.run_id if link else None,
            "linked_at": link.linked_at if link else None,
        }

    @app.post(
        "/score",
        response_model=ScoreResponse,
        dependencies=[Security(require_api_key), Security(limit_scoring)],
        responses={401: {"description": "Missing or invalid X-API-Key header"}},
    )
    def score(event: TransactionEvent, request: Request) -> ScoreResponse:
        s: Scorer = request.app.state.scorer
        try:
            return s.score_event(event)
        except LateEventError:
            raise HTTPException(status_code=409, detail="Late transaction") from None
        except Exception as exc:
            if not production:
                raise
            logger.error("scoring failed error=%s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Scoring unavailable") from None

    return app


app = create_app()
