"""Общие части Live-адаптеров: происхождение, доступность, актуальность, вызов через Replay (план §2.8).

Режим данных присваивается **только** по факту: ``live`` — реальный вызов сейчас, ``replay`` — снимок
со строгим ключом (с датой получения). Mock здесь не создаётся (DEC-23: Mock — отдельный явный путь).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from odin.contracts import Availability, SourceReference, Validity
from odin.tools.replay import ReplayStore
from odin.tools.router import ModeRouter


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def to_money(value: Any) -> Decimal | None:
    """Сумма провайдера → ``Decimal`` с копейками; float переводится через строку (без двоичной ошибки)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dict):
        value = value.get("amount")
        if isinstance(value, bool) or value is None:
            return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if amount < 0 or amount > Decimal("100000000"):
        return None
    return amount.quantize(Decimal("0.01"))


def to_aware(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def live_source(
    *,
    provider: str,
    tool: str,
    source_type: str,
    data_mode: str,
    retrieved_at: datetime,
    external_id: str | None = None,
    url: str | None = None,
    request_parameters: dict[str, Any] | None = None,
    limitations: list[str] | None = None,
) -> SourceReference:
    """Источник Live/Replay: Live — подтверждение конкретным источником сейчас, Replay — ``stale``."""
    return SourceReference(
        source_type=source_type,  # type: ignore[arg-type]
        provider=provider,
        tool=tool,
        url=url,
        retrieved_at=retrieved_at,
        generated_at=None,
        published_at=None,
        external_id=external_id,
        verification_status="verified" if data_mode == "live" else "stale",
        data_mode=data_mode,  # type: ignore[arg-type]
        limitations=list(limitations or []),
        request_parameters=request_parameters,
    )


def listing_availability(
    *, subject: str, data_mode: str, retrieved_at: datetime, parameters: dict[str, Any]
) -> Availability:
    """Поисковая выдача не доказывает наличие (AVAIL-01, DEC-15 строго): ``unknown``."""
    return Availability(
        subject=subject,  # type: ignore[arg-type]
        availability_status="unknown",
        evidence_level="search_listing" if data_mode == "live" else "historical",
        verification_status="unverified" if data_mode == "live" else "stale",
        data_mode=data_mode,  # type: ignore[arg-type]
        checked_at=retrieved_at,
        parameters=parameters,
        unknown_reason="not_provided_by_source" if data_mode == "live" else "stale",
        seats_left=None,
    )


def ttl_validity(observed_at: datetime, ttl_minutes: int, data_mode: str) -> Validity:
    if data_mode == "replay":
        return Validity(
            observed_at=observed_at,
            valid_until=observed_at,
            basis="none",
            note="снимок Replay: цена на дату получения, не текущая",
        )
    return Validity(
        observed_at=observed_at,
        valid_until=observed_at + timedelta(minutes=ttl_minutes),
        basis="provisional_ttl",
        note="TTL provisional (DEC-21)",
    )


@dataclass(frozen=True, slots=True)
class RawOutcome:
    """Результат одного сетевого вызова после маршрутизатора Live → Replay."""

    ok: bool
    payload: Any
    data_mode: str | None
    retrieved_at: datetime | None
    error_code: str | None
    warnings: tuple[str, ...] = ()


class RawCall:
    """Один вызов провайдера через ``ModeRouter``: Live, при сбое — строгий Replay, иначе — честный отказ."""

    def __init__(
        self,
        *,
        provider: str,
        tool: str,
        fetch: Callable[[dict[str, Any]], Any],
        store: ReplayStore | None,
        offline: bool = False,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.provider = provider
        self.tool = tool
        self.fetch = fetch
        self.offline = offline
        self.clock = clock
        self.router = ModeRouter(provider=provider, tool=tool, live=self._live, store=store, clock=clock)

    def _live(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.offline:
            return {"status": "unavailable", "error_code": "offline_replay_mode", "data": None}
        from odin.tools.mcp_client import McpError

        try:
            payload = self.fetch(arguments)
        except McpError as exc:
            return {"status": exc.status, "error_code": exc.code, "data": None}
        if isinstance(payload, dict) and payload.get("__status__"):
            return {"status": payload["__status__"], "error_code": payload.get("__error__"), "data": None}
        return {"status": "ok", "data": payload}

    def __call__(self, arguments: dict[str, Any]) -> RawOutcome:
        started = self.clock()
        routed = self.router(arguments)
        mode = routed.get("data_mode")
        if mode == "live":
            return RawOutcome(True, routed.get("data"), "live", started, None)
        if mode == "replay":
            retrieved = to_aware(routed.get("replay_retrieved_at")) or started
            return RawOutcome(
                True, routed.get("data"), "replay", retrieved, None, tuple(routed.get("warnings") or ())
            )
        return RawOutcome(
            False,
            None,
            None,
            None,
            str(routed.get("error_code") or "unavailable"),
            tuple(routed.get("warnings") or ()),
        )
