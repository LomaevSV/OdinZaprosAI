"""Проверка тестовой среды: внешняя сеть в тестах без маркера live запрещена (план §6.1)."""

from __future__ import annotations

import socket

import pytest

from tests.conftest import NetworkBlockedError

pytestmark = pytest.mark.unit


def test_external_network_is_blocked_without_live_marker() -> None:
    with pytest.raises(NetworkBlockedError):
        socket.create_connection(("93.184.216.34", 80))
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("mcp.tutu.ru", 443)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, pytest.raises(NetworkBlockedError):
        sock.connect(("93.184.216.34", 443))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, pytest.raises(NetworkBlockedError):
        sock.connect_ex(("10.0.0.1", 443))


def test_create_connection_is_blocked_even_for_loopback() -> None:
    """Приложение и адаптеры не должны открывать соединения даже к локальным сервисам в тестах."""
    with pytest.raises(NetworkBlockedError):
        socket.create_connection(("127.0.0.1", 9))
