"""Логические инструменты в режимах ``live``, ``mixed`` и ``replay`` (DEC-09, DEC-23, план §2.8).

Цепочка одного логического вызова: проверка политики (до сети) → Live-вызов провайдера →
при сбое строгий Replay → в ``mixed`` — явно маркированный Mock **по компоненту** → иначе честный
``unavailable``. Пустой Live-ответ (``empty``) Mock-ом не подменяется: «у источника нет вариантов» —
тоже факт.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from odin.tools import mock_adapters
from odin.tools.http_client import HttpJsonClient
from odin.tools.live_common import RawCall, RawOutcome, now_utc
from odin.tools.mcp_client import McpClient
from odin.tools.mock_data import mock_hotels, mock_transfer, mock_transport, normalize_city
from odin.tools.places import ResolvedPlace
from odin.tools.policy import LIVE_ROUTES, ToolPolicy
from odin.tools.replay import ReplayStore
from odin.tools.rest_adapters import (
    GEOCODING_URL,
    OPEN_METEO,
    OPEN_METEO_HOSTS,
    OSRM,
    OSRM_HOSTS,
    USER_AGENT,
    geocoding_params,
    normalize_geocoding,
    normalize_transfers,
    osrm_table_request,
    place_to_dict,
)
from odin.tools.tutu import (
    ALLOWED_HOSTS as TUTU_HOSTS,
)
from odin.tools.tutu import (
    ENDPOINT as TUTU_ENDPOINT,
)
from odin.tools.tutu import (
    PROVIDER as TUTU,
)
from odin.tools.tutu import (
    TutuGateway,
    avia_arguments,
    hotel_arguments,
    normalize_hotels,
    normalize_transport,
    rail_arguments,
)

Handler = Callable[[dict[str, Any]], dict[str, Any]]


def _fallback_note(code: str | None) -> str:
    return f"Live-источник недоступен ({code or 'ошибка'}), совместимого Replay нет: показан ДЕМО-вариант (DEC-23)"


def _place_from(data: Any, fallback_name: str) -> ResolvedPlace:
    if isinstance(data, dict):
        return ResolvedPlace(
            name=str(data.get("name") or fallback_name),
            country_code=data.get("country_code"),
            timezone=data.get("timezone"),
            lat=data.get("lat"),
            lon=data.get("lon"),
        )
    known = normalize_city(fallback_name)
    if known is not None:
        return ResolvedPlace(known.name, known.country_code, known.timezone, known.lat, known.lon)
    return ResolvedPlace(fallback_name, None, None, None, None)


def _iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


@dataclass(slots=True)
class LiveToolset:
    """Набор Live-обработчиков. Сеть — только через утверждённых провайдеров (``ToolPolicy``)."""

    policy: ToolPolicy
    data_mode: str  # live | mixed | replay
    store: ReplayStore | None
    tutu: TutuGateway | None
    geo_http: HttpJsonClient
    osrm_http: HttpJsonClient

    @property
    def mixed(self) -> bool:
        return self.data_mode == "mixed"

    @property
    def offline(self) -> bool:
        return self.data_mode == "replay"

    def _raw(self, provider: str, tool: str, fetch: Callable[[dict[str, Any]], Any]) -> RawCall:
        return RawCall(provider=provider, tool=tool, fetch=fetch, store=self.store, offline=self.offline)

    # ------------------------------------------------------------------ транспорт

    def search_transport(self, arguments: dict[str, Any]) -> dict[str, Any]:
        origin_name, destination_name = (
            str(arguments.get("origin") or ""),
            str(arguments.get("destination") or ""),
        )
        outbound_day = _iso(arguments.get("date"))
        if not origin_name or not destination_name or outbound_day is None:
            return mock_adapters._error("нужны города и дата отправления")
        adults = int(arguments.get("adults") or 0)
        if adults < 1:
            return mock_adapters._error("нужен состав группы: adults >= 1 (UC-03)")
        children = [int(age) for age in arguments.get("children_ages") or []]
        origin = _place_from(arguments.get("origin_place"), origin_name)
        destination = _place_from(arguments.get("destination_place"), destination_name)
        return_day = _iso(arguments.get("return_date"))
        modes = [
            mode for mode in ("rail", "flight") if not arguments.get("modes") or mode in arguments["modes"]
        ]
        ttl = int(arguments.get("ttl_minutes") or 30)
        legs = [("outbound", origin, destination, outbound_day)]
        if return_day is not None:
            legs.append(("return", destination, origin, return_day))
        for kind in modes:
            self.policy.check_provider_tool(TUTU, "search_rail" if kind == "rail" else "search_avia")
        tasks = [(kind, *leg) for kind in modes for leg in legs]

        def run(task: tuple[Any, ...]) -> tuple[tuple[Any, ...], RawOutcome, dict[str, Any]]:
            kind, _direction, start, end, day = task
            tool = "search_rail" if kind == "rail" else "search_avia"
            if kind == "rail":
                mcp_args = rail_arguments(start.name, end.name, day.isoformat(), adults)
            else:
                mcp_args = avia_arguments(start.name, end.name, day.isoformat(), adults, children)
            assert self.tutu is not None
            gateway = self.tutu
            outcome = self._raw(TUTU, tool, lambda a, tool=tool: gateway.call(tool, a))(mcp_args)
            return task, outcome, mcp_args

        with ThreadPoolExecutor(max_workers=max(1, min(4, len(tasks)))) as pool:
            results = list(pool.map(run, tasks))

        offers: list[Any] = []
        sources: list[Any] = []
        warnings: list[str] = []
        missing: list[str] = []
        failures: list[str] = []
        replayed = False
        for (kind, direction, start, end, day), outcome, mcp_args in results:
            label = (
                f"{'ж/д' if kind == 'rail' else 'авиа'} {'туда' if direction == 'outbound' else 'обратно'}"
            )
            warnings.extend(outcome.warnings)
            if outcome.ok and isinstance(outcome.payload, dict):
                assert outcome.data_mode is not None and outcome.retrieved_at is not None
                replayed = replayed or outcome.data_mode == "replay"
                normalized, notes = normalize_transport(
                    outcome.payload,
                    kind=kind,
                    direction=direction,
                    origin=start,
                    destination=end,
                    adults=adults,
                    children_ages=children,
                    request_parameters={
                        "tool": "search_rail" if kind == "rail" else "search_avia",
                        **mcp_args,
                    },
                    data_mode=outcome.data_mode,
                    retrieved_at=outcome.retrieved_at,
                    ttl_minutes=ttl,
                )
                warnings.extend(notes)
                offers.extend(normalized)
                if normalized:
                    sources.append(normalized[0].source)
                else:
                    warnings.append(
                        f"Tutu: нет вариантов ({label}) — это ответ источника, а не отсутствие рейсов вообще"
                    )
                continue
            failures.append(outcome.error_code or "unavailable")
            fallback = self._mock_transport(kind, direction, start, end, day, adults, children, ttl)
            if fallback:
                offers.extend(fallback)
                sources.append(fallback[0].source)
                warnings.append(_fallback_note(outcome.error_code) + f": {label}")
                missing.append(f"Live: {label}")
            else:
                missing.append(label)
                warnings.append(f"Tutu недоступен ({outcome.error_code}): {label} — результат неизвестен")
        return self._response("offers", offers, sources, warnings, missing, failures, replayed)

    def _mock_transport(self, kind, direction, start, end, day, adults, children, ttl) -> list[Any]:
        if not self.mixed:
            return []
        demo_start, demo_end = normalize_city(start.name), normalize_city(end.name)
        if demo_start is None or demo_end is None:
            return []
        generated = mock_transport(
            origin=demo_start,
            destination=demo_end,
            outbound_date=day,
            return_date=None,
            adults=adults,
            children_ages=children,
            modes=[kind],
            generated_at=now_utc(),
            ttl_minutes=ttl,
        )
        return [offer.evolve(direction=direction) for offer in generated]

    # ------------------------------------------------------------------ гостиницы

    def search_hotels(self, arguments: dict[str, Any]) -> dict[str, Any]:
        city_name = str(arguments.get("city") or "")
        check_in, check_out = _iso(arguments.get("check_in")), _iso(arguments.get("check_out"))
        if not city_name or check_in is None or check_out is None or check_out <= check_in:
            return mock_adapters._error("нужны город и корректные даты заезда и выезда")
        adults = int(arguments.get("adults") or 0)
        if adults < 1:
            return mock_adapters._error("нужен состав группы: adults >= 1 (UC-03)")
        if int(arguments.get("rooms") or 1) > 1:
            return {
                "status": "error",
                "data": None,
                "sources": [],
                "error_code": "rooms_not_supported_by_source",
                "warnings": [
                    "Tutu search_hotels не принимает число номеров: поиск нескольких номеров недоступен"
                ],
            }
        children = [int(age) for age in arguments.get("children_ages") or []]
        ttl = int(arguments.get("ttl_minutes") or 30)
        city = _place_from(arguments.get("city_place"), city_name)
        self.policy.check_provider_tool(TUTU, "search_hotels")
        mcp_args = hotel_arguments(city.name, check_in.isoformat(), check_out.isoformat(), adults, children)
        assert self.tutu is not None
        gateway = self.tutu
        outcome = self._raw(TUTU, "search_hotels", lambda a: gateway.call("search_hotels", a))(mcp_args)
        if outcome.ok and isinstance(outcome.payload, dict):
            assert outcome.data_mode is not None and outcome.retrieved_at is not None
            hotels, offers, notes = normalize_hotels(
                outcome.payload,
                city=city,
                check_in=check_in,
                check_out=check_out,
                adults=adults,
                children_ages=children,
                request_parameters={"tool": "search_hotels", **mcp_args},
                data_mode=outcome.data_mode,
                retrieved_at=outcome.retrieved_at,
                ttl_minutes=ttl,
            )
            response = self._response(
                "offers",
                offers,
                [offers[0].source] if offers else [],
                [*outcome.warnings, *notes],
                [],
                [],
                outcome.data_mode == "replay",
            )
            if offers:
                response["data"]["hotels"] = hotels
            return response
        demo = normalize_city(city.name) if self.mixed else None
        if demo is not None:
            hotels, offers = mock_hotels(
                city=demo,
                check_in=check_in,
                check_out=check_out,
                adults=adults,
                children_ages=children,
                rooms=1,
                generated_at=now_utc(),
                ttl_minutes=ttl,
            )
            return {
                "status": "partial",
                "data": {"offers": offers, "hotels": hotels},
                "sources": [offers[0].source],
                "warnings": [*outcome.warnings, _fallback_note(outcome.error_code) + ": гостиницы"],
                "missing": ["Live: гостиницы"],
            }
        return {
            "status": "unavailable",
            "data": None,
            "sources": [],
            "error_code": outcome.error_code or "unavailable",
            "warnings": [
                *outcome.warnings,
                f"Tutu недоступен ({outcome.error_code}): гостиницы — результат неизвестен",
            ],
        }

    # ------------------------------------------------------------------ геокодинг

    def geocode(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = str(arguments.get("name") or "").strip()
        if not name:
            return mock_adapters._error("нужно название места")
        self.policy.check_provider_tool(OPEN_METEO, "geocoding")
        params = geocoding_params(name)

        def fetch(args: dict[str, Any]) -> Any:
            outcome = self.geo_http.get_json(GEOCODING_URL, args)
            if outcome.status in ("ok", "empty"):
                return outcome.data or {}
            return {"__status__": outcome.status, "__error__": outcome.error_code}

        outcome = self._raw(OPEN_METEO, "geocoding", fetch)(params)
        if not outcome.ok:
            return {
                "status": "unavailable",
                "data": None,
                "sources": [],
                "error_code": outcome.error_code or "unavailable",
                "warnings": list(outcome.warnings),
            }
        assert outcome.data_mode is not None and outcome.retrieved_at is not None
        places = normalize_geocoding(
            outcome.payload, data_mode=outcome.data_mode, retrieved_at=outcome.retrieved_at, query=name
        )
        if not places:
            return {
                "status": "empty",
                "data": {"places": []},
                "sources": [],
                "warnings": ["место не найдено геокодером"],
            }
        assert places[0].source is not None
        return {
            "status": "ok" if outcome.data_mode == "live" else "partial",
            "data": {"places": [place_to_dict(place) for place in places]},
            "sources": [places[0].source],
            "warnings": list(outcome.warnings),
            "missing": [] if outcome.data_mode == "live" else ["текущие данные геокодера"],
        }

    # ------------------------------------------------------------------ трансфер

    def get_transfer_options(self, arguments: dict[str, Any]) -> dict[str, Any]:
        city = str(arguments.get("city") or "")
        points = [
            p for p in arguments.get("points") or [] if isinstance(p, dict) and p.get("lat") is not None
        ]
        hotels = [
            h for h in arguments.get("hotels") or [] if isinstance(h, dict) and h.get("lat") is not None
        ]
        ttl = int(arguments.get("ttl_minutes") or 30)
        failure: str | None = None
        warnings: list[str] = []
        if points and hotels:
            self.policy.check_provider_tool(OSRM, "table")
            coordinates = [(float(p["lat"]), float(p["lon"])) for p in points] + [
                (float(h["lat"]), float(h["lon"])) for h in hotels
            ]
            url, params = osrm_table_request(coordinates)

            def fetch(args: dict[str, Any]) -> Any:
                result = self.osrm_http.get_json(url, {key: args[key] for key in ("annotations",)})
                if result.status == "ok":
                    return result.data
                return {"__status__": result.status, "__error__": result.error_code}

            outcome = self._raw(OSRM, "table", fetch)(
                {**params, "coordinates": [list(c) for c in coordinates]}
            )
            if outcome.ok:
                assert outcome.data_mode is not None and outcome.retrieved_at is not None
                offers = normalize_transfers(
                    outcome.payload,
                    points=points,
                    hotels=hotels,
                    city=city,
                    data_mode=outcome.data_mode,
                    retrieved_at=outcome.retrieved_at,
                    ttl_minutes=ttl,
                )
                if offers:
                    return {
                        "status": "partial",
                        "data": {"offers": offers},
                        "sources": [offers[0].source],
                        "warnings": [
                            *outcome.warnings,
                            "время — оценка OSRM без пробок; стоимость не оценивается",
                        ],
                        "missing": ["стоимость трансфера"],
                    }
                failure = "osrm_no_routes"
            else:
                failure = outcome.error_code
                warnings.extend(outcome.warnings)
        else:
            failure = "no_coordinates"
            warnings.append("нет координат вокзала/аэропорта или отеля: время трансфера не оценено")
        demo = normalize_city(city) if self.mixed else None
        if demo is not None:
            directions = [
                str(item) for item in arguments.get("directions") or ["transfer_in", "transfer_out"]
            ]
            offers = [
                mock_transfer(city=demo, direction=direction, generated_at=now_utc(), ttl_minutes=ttl)
                for direction in directions
            ]
            return {
                "status": "partial",
                "data": {"offers": offers},
                "sources": [offers[0].source],
                "warnings": [*warnings, _fallback_note(failure) + ": трансфер"],
                "missing": ["стоимость трансфера", "Live: трансфер"],
            }
        return {
            "status": "empty",
            "data": {"offers": []},
            "sources": [],
            "warnings": warnings or [f"трансфер не оценён ({failure})"],
        }

    # ------------------------------------------------------------------ общий ответ

    @staticmethod
    def _response(
        key: str,
        items: list[Any],
        sources: list[Any],
        warnings: list[str],
        missing: list[str],
        failures: list[str],
        replayed: bool,
    ) -> dict[str, Any]:
        if not items:
            if failures:
                return {
                    "status": "unavailable",
                    "data": None,
                    "sources": [],
                    "error_code": failures[0],
                    "warnings": warnings,
                }
            return {"status": "empty", "data": {key: []}, "sources": [], "warnings": warnings}
        partial = bool(missing) or replayed
        if replayed:
            missing = [*missing, "текущие цены и наличие (Replay)"]
        return {
            "status": "partial" if partial else "ok",
            "data": {key: items},
            "sources": sources,
            "warnings": warnings,
            "missing": missing,
        }

    def handlers(self) -> dict[str, Handler]:
        approved = {provider for provider, policy in self.policy.policies.items() if policy.live_allowed}
        live: dict[str, Handler] = {
            "search_transport": self.search_transport,
            "search_hotels": self.search_hotels,
            "geocode": self.geocode,
            "get_transfer_options": self.get_transfer_options,
        }
        result: dict[str, Handler] = {"suggest_destinations": mock_adapters.suggest_destinations}
        for tool, handler in live.items():
            if LIVE_ROUTES[tool] in approved:
                result[tool] = handler
            elif self.mixed and tool in mock_adapters.MOCK_HANDLERS:
                result[tool] = mock_adapters.MOCK_HANDLERS[tool]
        return result


def build_live_toolset(
    *,
    policy: ToolPolicy,
    data_mode: str,
    replay_dir: Path | None,
    timeout_s: float,
    retries: int,
    mcp_transport: httpx.BaseTransport | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> LiveToolset:
    store = ReplayStore(replay_dir) if replay_dir is not None else None
    tutu = None
    tutu_policy = policy.policies.get(TUTU)
    if tutu_policy is not None and tutu_policy.live_allowed:
        client = McpClient(
            endpoint=TUTU_ENDPOINT, allowed_hosts=TUTU_HOSTS, timeout_s=timeout_s, transport=mcp_transport
        )
        tutu = TutuGateway(client=client, policy=policy)
    headers = {"User-Agent": USER_AGENT}
    return LiveToolset(
        policy=policy,
        data_mode=data_mode,
        store=store,
        tutu=tutu,
        geo_http=HttpJsonClient(
            allowed_hosts=OPEN_METEO_HOSTS,
            timeout_s=timeout_s,
            retries=retries,
            transport=http_transport,
            headers=headers,
        ),
        osrm_http=HttpJsonClient(
            allowed_hosts=OSRM_HOSTS,
            timeout_s=timeout_s,
            retries=retries,
            transport=http_transport,
            headers=headers,
        ),
    )
