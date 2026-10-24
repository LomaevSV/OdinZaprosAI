"""Проверки поставки S1: секреты, README, .env.example, манифест данных, реестр провайдеров, Docker-файлы,
структура интерфейса (T-DLV-SECRETS-01, T-DLV-README-01, T-DLV-MANIFEST-01, T-DLV-REG-01, T-UI-01 авто-часть)."""

from __future__ import annotations

import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

from odin.config import ENV_VARS

pytestmark = pytest.mark.delivery

ROOT = Path(__file__).resolve().parents[2]

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),  # формат ключа 2ГИС
]


def tracked_and_candidate_files() -> list[Path]:
    """Файлы, которые попадут в коммит: отслеживаемые и неотслеживаемые, но не игнорируемые."""
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    return [ROOT / line for line in output.splitlines() if line and (ROOT / line).is_file()]


@pytest.mark.req("T-DLV-SECRETS-01", "SECURITY-01")
def test_no_secrets_in_repository_files() -> None:
    offenders: list[str] = []
    for path in tracked_and_candidate_files():
        if path.suffix in {".png", ".ico", ".lock"} or path.name == "uv.lock":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)[:12]}…")
    assert offenders == []


@pytest.mark.req("T-DLV-SECRETS-01")
def test_env_files_and_runtime_data_are_ignored() -> None:
    names = {path.relative_to(ROOT).as_posix() for path in tracked_and_candidate_files()}
    assert ".env" not in names
    assert not any(name.startswith(("var/", "replay_store/", ".venv/")) for name in names)
    assert not any(name.endswith((".sqlite3", ".db", ".sqlite")) for name in names)
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "var/", "replay_store/", "*.sqlite"):
        assert pattern in gitignore


@pytest.mark.req("DELIVERY-01", "SECURITY-01")
def test_env_example_lists_all_variables_without_values_for_secrets() -> None:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    pairs = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
    assert set(pairs) == set(ENV_VARS)
    assert pairs["ODIN_LLM_API_KEY"] == ""
    assert pairs["ODIN_DATA_MODE"] == "mock" and pairs["ODIN_LLM_MODE"] == "off"


@pytest.mark.req("T-DLV-README-01", "DELIVERY-01")
def test_readme_explains_start_checks_and_demo_limits() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for fragment in (
        "uv sync",
        "python -m odin.api",
        "docker compose up",
        "pytest",
        "ДЕМО",
        "Реального бронирования и списания средств нет",
        "DEC-08",
        "BLOCKED",
    ):
        assert fragment in readme, fragment


@pytest.mark.req("T-DLV-MANIFEST-01", "GOAL-02")
def test_data_manifest_describes_every_included_dataset() -> None:
    manifest = yaml.safe_load((ROOT / "docs" / "data_manifest.yaml").read_text(encoding="utf-8"))
    included = [item for item in manifest["datasets"] if item["status"] == "included"]
    assert included
    for item in included:
        assert (ROOT / item["location"]).is_file(), item["location"]
        for key in ("origin", "license", "limitations", "data_mode"):
            assert item.get(key), f"{item['id']}: {key}"
    assert all(
        item["rights_confirmed"] is False for item in manifest["datasets"] if item["status"] == "not_included"
    )


@pytest.mark.req("T-DLV-REG-01", "PROVIDER-01")
def test_provider_registry_has_no_unapproved_approval() -> None:
    registry = yaml.safe_load((ROOT / "config" / "provider_registry.yaml").read_text(encoding="utf-8"))
    for card in registry["providers"]:
        if card["approval_status"] == "approved":
            assert card["owner_decision"] == "approved", card["id"]
        assert card.get("decision_ref"), card["id"]


@pytest.mark.req("DELIVERY-01")
def test_docker_files_follow_plan() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.startswith("#") and "FROM python:3.12-slim" in dockerfile
    assert "uv sync --frozen" in dockerfile and "USER odin" in dockerfile and "HEALTHCHECK" in dockerfile
    assert "ODIN_DATA_MODE=mock" in dockerfile and "ODIN_LLM_MODE=off" in dockerfile
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert list(compose["services"]) == ["odin"]
    assert any("/data" in volume for volume in compose["services"]["odin"]["volumes"])
    check = (ROOT / "docs" / "DOCKER_CHECK.md").read_text(encoding="utf-8")
    assert "BLOCKED" in check and "/health" in check


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.testids: set[str] = set()
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("data-testid"):
            self.testids.add(values["data-testid"] or "")
        if tag == "script" and values.get("src"):
            self.scripts.append(values["src"] or "")


@pytest.mark.req("UI-01", "T-UI-01", "DEC-36")
def test_interface_structure_has_required_elements() -> None:
    """Автоматическая структурная проверка (DEC-36); визуальный чек-лист — вручную."""
    html = (ROOT / "src" / "odin" / "static" / "index.html").read_text(encoding="utf-8")
    parser = _Collector()
    parser.feed(html)
    required = {
        "messages",
        "questions",
        "structured-form",
        "trip-panel",
        "progress",
        "cards",
        "constraints",
        "cart",
        "receipt",
        "confirm-dialog",
        "consent-purchase",
        "consent-risk",
        "global-mode",
        "demo-notice",
    }
    assert required <= parser.testids
    assert parser.scripts == ["/static/app.js"]
    script = (ROOT / "src" / "odin" / "static" / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script  # без вставки недоверенного HTML
    assert "mode-badge" in script and "актуально на" in script and "risk_acknowledgement" in script


@pytest.mark.req("SECURITY-01")
def test_text_files_are_utf8_lf_without_bom() -> None:
    offenders = []
    for path in tracked_and_candidate_files():
        if "archive" in path.parts or path.suffix in {".png", ".ico"}:
            continue
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf") or b"\r\n" in raw:
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
