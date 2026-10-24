"""JSON API (API-01, план §2.14), `/health`, SSE-прогресс и статический интерфейс (UI-01).

Пути и методы закреплены снимком OpenAPI (`schemas/openapi.json`, T-CON-SCHEMA-01).
Ошибки возвращаются как `ErrorResponse`; устаревшая ревизия — 409.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from odin import __version__
from odin.app import Application, build_application
from odin.config import Settings
from odin.contracts import NORMATIVE_MODELS, SCHEMA_VERSION, TripState
from odin.contracts.api import (
    CartRequest,
    CartResponse,
    ConfirmRequest,
    ConfirmResponse,
    DeclineRequest,
    ErrorResponse,
    HealthResponse,
    MessageRequest,
    ProviderHealth,
    ReplanRequest,
    SelectionRequest,
    SessionResponse,
    TurnResponse,
)
from odin.contracts.plan import DemoReceipt
from odin.core.cart import CartError, CartStaleError
from odin.logging_setup import configure_logging
from odin.orchestrator import TurnResult
from odin.storage import ConflictError, NotFoundError
from odin.tools.policy import load_provider_policies

STATIC_DIR = Path(__file__).parent / "static"
API_PREFIX = "/api/v1"

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Недопустимая операция"},
    404: {"model": ErrorResponse, "description": "Не найдено"},
    409: {"model": ErrorResponse, "description": "Устаревшая ревизия поездки"},
    422: {"model": ErrorResponse, "description": "Ошибка валидации"},
}


def _error(status: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    body = ErrorResponse(error_code=code, message=message, details=details or {})
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def _turn_response(result: TurnResult, *, session_id: str, request_id: str, input_mode: str) -> TurnResponse:
    state = result.state
    return TurnResponse(
        request_id=request_id,
        session_id=session_id,
        trip_id=state.trip_id,
        trip_revision=state.revision,
        status=state.status,
        input_mode=input_mode,  # type: ignore[arg-type]
        assistant_text=result.assistant_text,
        questions=[question for question in (state.request.ambiguities if state.request else [])]
        or [_ambiguity(item) for item in result.questions],
        state=state,
    )


def _ambiguity(payload: dict[str, Any]):
    from odin.contracts.trip import Ambiguity

    return Ambiguity.model_validate(payload)


def create_app(settings: Settings | None = None, *, application: Application | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    holder: dict[str, Application] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        holder.setdefault("app", application or build_application(settings))
        yield

    app = FastAPI(
        title="О!дин запрос API",
        version=__version__,
        description=(
            "JSON API прототипа AI-турагента. Срез S1: только Mock-данные с маркировкой «ДЕМО». "
            "Демонстрационная покупка: реального бронирования и списания средств нет."
        ),
        lifespan=lifespan,
    )
    if application is not None:
        holder["app"] = application
    if settings.cors_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.cors_origin],
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

    def core() -> Application:
        if "app" not in holder:
            holder["app"] = build_application(settings)
        return holder["app"]

    # ------------------------------------------------------------------ ошибки

    @app.exception_handler(ConflictError)
    async def _conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return _error(409, "revision_conflict", str(exc))

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @app.exception_handler(CartStaleError)
    async def _cart_stale(_: Request, exc: CartStaleError) -> JSONResponse:
        return _error(409, "cart_stale", str(exc))

    @app.exception_handler(CartError)
    async def _cart_error(_: Request, exc: CartError) -> JSONResponse:
        return _error(400, "cart_rejected", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg")),
                "type": error.get("type"),
            }
            for error in exc.errors()
        ]
        return _error(422, "validation_error", "запрос не прошёл проверку", {"errors": errors})

    # ------------------------------------------------------------------ служебные

    @app.get("/health", response_model=HealthResponse, tags=["service"])
    def health() -> HealthResponse:
        application_ = core()
        database_ok = application_.storage.ping()
        providers = [
            ProviderHealth(
                provider_id=policy.provider_id,
                approval_status=policy.approval_status,
                live_enabled=False,
                reason=(
                    "Live не используется в S1: провайдер не утверждён"
                    if policy.approval_status != "approved"
                    else "Live в S1 выключен конфигурацией"
                ),
            )
            for policy in load_provider_policies().values()
        ]
        return HealthResponse(
            status="ok" if database_ok else "degraded",
            version=__version__,
            schema_version=SCHEMA_VERSION,
            data_mode=settings.data_mode,
            llm_mode=settings.llm.mode,
            database="ok" if database_ok else "error",
            providers=providers,
        )

    @app.get(f"{API_PREFIX}/schemas/{{name}}", tags=["contracts"], responses={404: ERROR_RESPONSES[404]})
    def get_schema(name: str) -> dict[str, Any]:
        from odin.contracts.schema_export import build_schema

        if name not in NORMATIVE_MODELS:
            raise NotFoundError(f"схема не найдена: {name}")
        return build_schema(name)

    # ------------------------------------------------------------------ сессии и ход

    @app.post(f"{API_PREFIX}/sessions", response_model=SessionResponse, status_code=201, tags=["sessions"])
    def create_session() -> SessionResponse:
        application_ = core()
        session = application_.orchestrator.create_session()
        state = application_.storage.get_state(session["trip_id"])
        return _session_response(session, state)

    @app.get(
        f"{API_PREFIX}/sessions/{{session_id}}",
        response_model=SessionResponse,
        tags=["sessions"],
        responses={404: ERROR_RESPONSES[404]},
    )
    def get_session(session_id: str) -> SessionResponse:
        application_ = core()
        session = application_.storage.get_session(session_id)
        return _session_response(session, application_.storage.get_state(session["trip_id"]))

    @app.post(
        f"{API_PREFIX}/sessions/{{session_id}}/messages",
        response_model=TurnResponse,
        tags=["sessions"],
        responses={key: ERROR_RESPONSES[key] for key in (404, 409, 422)},
    )
    def post_message(session_id: str, body: MessageRequest) -> TurnResponse:
        application_ = core()
        result = application_.orchestrator.handle_message(
            session_id=session_id,
            request_id=body.request_id,
            trip_revision=body.trip_revision,
            text=None if body.form is not None else body.text,
            form=body.form,
        )
        if body.form is not None:
            input_mode = "structured_form"
        elif result.state.request is not None and result.state.request.input_mode == "free_text_replay":
            input_mode = "free_text_replay"
        else:
            input_mode = "llm_unavailable"
        return _turn_response(
            result, session_id=session_id, request_id=body.request_id, input_mode=input_mode
        )

    @app.get(
        f"{API_PREFIX}/sessions/{{session_id}}/events",
        tags=["sessions"],
        responses={200: {"content": {"text/event-stream": {}}, "description": "SSE-поток событий сессии"}},
    )
    async def session_events(session_id: str, after: int = 0, follow: bool = False) -> StreamingResponse:
        application_ = core()
        application_.storage.get_session(session_id)

        async def stream() -> AsyncIterator[str]:
            last = after
            idle_rounds = 0
            while True:
                events = application_.storage.list_events(session_id, after_seq=last)
                for event in events:
                    last = event["seq"]
                    yield f"id: {event['seq']}\nevent: {event['event']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not follow:
                    break
                idle_rounds = 0 if events else idle_rounds + 1
                if idle_rounds > 60:
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    # ------------------------------------------------------------------ поездки

    @app.get(
        f"{API_PREFIX}/trips/{{trip_id}}",
        response_model=TripState,
        tags=["trips"],
        responses={404: ERROR_RESPONSES[404]},
    )
    def get_trip(trip_id: str) -> TripState:
        return core().storage.get_state(trip_id)

    @app.post(
        f"{API_PREFIX}/trips/{{trip_id}}/selections",
        response_model=TripState,
        tags=["trips"],
        responses={key: ERROR_RESPONSES[key] for key in (400, 404, 409, 422)},
    )
    def post_selection(trip_id: str, body: SelectionRequest) -> TripState:
        return core().orchestrator.select(
            trip_id=trip_id,
            trip_revision=body.trip_revision,
            selections={str(role): offer for role, offer in body.selections.items()},
            request_id=body.request_id,
        )

    @app.post(
        f"{API_PREFIX}/trips/{{trip_id}}/replan",
        response_model=TurnResponse,
        tags=["trips"],
        responses={key: ERROR_RESPONSES[key] for key in (400, 404, 409, 422)},
    )
    def post_replan(trip_id: str, body: ReplanRequest) -> TurnResponse:
        application_ = core()
        result = application_.orchestrator.replan(
            trip_id=trip_id,
            trip_revision=body.trip_revision,
            change_set=body.change_set,
            request_id=body.request_id,
        )
        return _turn_response(
            result,
            session_id=result.state.session_id,
            request_id=body.request_id,
            input_mode="structured_form",
        )

    @app.post(
        f"{API_PREFIX}/trips/{{trip_id}}/cart",
        response_model=CartResponse,
        status_code=201,
        tags=["cart"],
        responses={key: ERROR_RESPONSES[key] for key in (400, 404, 409, 422)},
    )
    def post_cart(trip_id: str, body: CartRequest) -> CartResponse:
        cart = core().orchestrator.open_cart(
            trip_id=trip_id, trip_revision=body.trip_revision, request_id=body.request_id
        )
        return CartResponse(trip_id=trip_id, trip_revision=cart.trip_revision, cart=cart)

    @app.get(
        f"{API_PREFIX}/trips/{{trip_id}}/cart",
        response_model=CartResponse,
        tags=["cart"],
        responses={404: ERROR_RESPONSES[404]},
    )
    def get_cart(trip_id: str) -> CartResponse:
        cart = core().orchestrator.get_cart(trip_id)
        return CartResponse(trip_id=trip_id, trip_revision=cart.trip_revision, cart=cart)

    @app.post(
        f"{API_PREFIX}/carts/{{cart_id}}/confirm",
        response_model=ConfirmResponse,
        tags=["cart"],
        responses={key: ERROR_RESPONSES[key] for key in (400, 404, 409, 422)},
    )
    def post_confirm(cart_id: str, body: ConfirmRequest) -> ConfirmResponse:
        cart, receipt, replayed = core().orchestrator.confirm_cart(
            cart_id=cart_id,
            confirmation_id=body.confirmation_id,
            trip_revision=body.trip_revision,
            purchase_consent=body.purchase_consent,
            risk_acknowledgement=body.risk_acknowledgement,
            request_id=body.request_id,
        )
        return ConfirmResponse(receipt=receipt, cart=cart, idempotent_replay=replayed)

    @app.post(
        f"{API_PREFIX}/carts/{{cart_id}}/decline",
        response_model=CartResponse,
        tags=["cart"],
        responses={key: ERROR_RESPONSES[key] for key in (400, 404, 409, 422)},
    )
    def post_decline(cart_id: str, body: DeclineRequest) -> CartResponse:
        cart = core().orchestrator.decline_cart(
            cart_id=cart_id, trip_revision=body.trip_revision, request_id=body.request_id
        )
        return CartResponse(trip_id=cart.trip_id, trip_revision=cart.trip_revision, cart=cart)

    @app.get(
        f"{API_PREFIX}/receipts/{{receipt_id}}",
        response_model=DemoReceipt,
        tags=["cart"],
        responses={404: ERROR_RESPONSES[404]},
    )
    def get_receipt(receipt_id: str) -> DemoReceipt:
        return core().orchestrator.get_receipt(receipt_id)

    # ------------------------------------------------------------------ интерфейс

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")

    return app


def _session_response(session: dict[str, Any], state: TripState) -> SessionResponse:
    from datetime import datetime

    return SessionResponse(
        session_id=session["session_id"],
        trip_id=session["trip_id"],
        trip_revision=state.revision,
        created_at=datetime.fromisoformat(session["created_at"]),
        llm_mode=session["llm_mode"],
        data_mode=session["data_mode"],
        state=state,
    )


def openapi_document(app: FastAPI | None = None) -> dict[str, Any]:
    """Документ OpenAPI из единого набора моделей (CONTRACT-03)."""
    target = app or create_app(Settings.from_env({"ODIN_DB_PATH": ":memory:"}))
    return target.openapi()


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings), host="0.0.0.0", port=settings.port, log_level=settings.log_level.lower()
    )


if __name__ == "__main__":
    main()
