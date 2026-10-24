"""Общие правила тестов (план §6.1): в unit/contract/negative/adapter/e2e_mock сеть запрещена."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from odin.app import Application, build_application
from odin.config import Settings

_NETWORK_MARKERS = ("live", "e2e_live")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class NetworkBlockedError(RuntimeError):
    """Попытка внешнего сетевого доступа в тесте без маркера live."""


def _is_loopback(address: object) -> bool:
    """Loopback разрешён: его использует self-pipe asyncio на Windows; внешняя сеть — нет."""
    if isinstance(address, tuple) and address:
        return str(address[0]) in _LOOPBACK_HOSTS
    return False


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if any(request.node.get_closest_marker(marker) for marker in _NETWORK_MARKERS):
        return
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def blocked(target: object) -> NetworkBlockedError:
        return NetworkBlockedError(
            f"внешняя сеть запрещена в тестах без маркера live (план §6.1): {target!r}"
        )

    def connect(self: socket.socket, address: object) -> None:
        if not _is_loopback(address):
            raise blocked(address)
        original_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        if not _is_loopback(address):
            raise blocked(address)
        return original_connect_ex(self, address)  # type: ignore[arg-type]

    def create_connection(address: object, *_args: object, **_kwargs: object) -> None:
        raise blocked(address)

    def getaddrinfo(host: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if str(host) not in _LOOPBACK_HOSTS:
            raise blocked(host)
        return original_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.from_env({"ODIN_DB_PATH": str(tmp_path / "odin.sqlite3")})


@pytest.fixture
def application(settings: Settings) -> Application:
    return build_application(settings)


@pytest.fixture
def client(settings: Settings, application: Application) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from odin.api import create_app

    with TestClient(create_app(settings, application=application)) as test_client:
        yield test_client


TRIP_START = date.today() + timedelta(days=30)
TRIP_END = TRIP_START + timedelta(days=2)


def trip_form(**overrides: Any) -> dict[str, Any]:
    """Типовой запрос K-03 (сокращённо): Москва → Казань, двое взрослых, туда и обратно."""
    form: dict[str, Any] = {
        "origin": "Москва",
        "destination": "Казань",
        "date_from": TRIP_START.isoformat(),
        "date_to": TRIP_END.isoformat(),
        "adults": 2,
        "return_trip": True,
    }
    form.update(overrides)
    return form
