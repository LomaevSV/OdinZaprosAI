"""Regression: independent selfcheck runs must not share a confirmation id."""

from fastapi.testclient import TestClient

from odin import selfcheck
from odin.api import create_app
from odin.config import Settings


def test_two_selfchecks_same_database(monkeypatch, tmp_path):
    settings = Settings.from_env({"ODIN_DATA_MODE": "mock", "ODIN_DB_PATH": str(tmp_path / "same.sqlite3")})
    with TestClient(create_app(settings)) as client:

        def call(base, method, path, body=None):
            response = client.request(method, base.replace("http://testserver", "") + path, json=body)
            assert response.status_code < 300, response.text
            return response.json()

        monkeypatch.setattr(selfcheck, "_call", call)
        first = selfcheck.run("http://testserver")
        second = selfcheck.run("http://testserver")
    assert first["idempotent_replay"] is True
    assert second["idempotent_replay"] is True
    assert first["receipt_id"] != second["receipt_id"]


def test_selfcheck_reuses_same_confirmation_id_only_inside_one_run(monkeypatch, tmp_path):
    settings = Settings.from_env({"ODIN_DATA_MODE": "mock", "ODIN_DB_PATH": str(tmp_path / "single.sqlite3")})
    seen = []
    with TestClient(create_app(settings)) as client:

        def call(base, method, path, body=None):
            if path.endswith("/confirm"):
                seen.append(body["confirmation_id"])
            response = client.request(method, base.replace("http://testserver", "") + path, json=body)
            assert response.status_code < 300, response.text
            return response.json()

        monkeypatch.setattr(selfcheck, "_call", call)
        selfcheck.run("http://testserver")
        selfcheck.run("http://testserver")
    assert len(seen) == 4
    assert seen[0] == seen[1]
    assert seen[2] == seen[3]
    assert seen[0] != seen[2]
