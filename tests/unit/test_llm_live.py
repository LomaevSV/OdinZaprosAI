"""LLM Live-путь на синтетических ответах: клиент Chat Completions, конфигурация, Gateway (ARCH-06, DEC-07).

Сеть заблокирована (conftest): используется ``httpx.MockTransport``; платных вызовов нет.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from odin.config import ConfigError, LlmSettings, Settings, load_env_file
from odin.llm.gateway import LlmGateway, default_client_factory, extract_json_object, replay_key
from odin.llm.openai_client import (
    ChatCompletionsClient,
    ChatResult,
    LlmEndpointError,
    chat_completions_url,
    validate_endpoint,
)
from tests.llm_fakes import LIVE_ENV, ScriptedLlm

pytestmark = [pytest.mark.unit, pytest.mark.req("ARCH-06", "DEC-07")]

KEY = "test-key"


def _reply(content: Any, *, finish: str = "stop", status: int = 200) -> httpx.Response:
    body = {
        "id": "x",
        "model": "fake-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    return httpx.Response(status, json=body)


def _client(handler: Any, **overrides: Any) -> tuple[ChatCompletionsClient, list[float]]:
    sleeps: list[float] = []
    options: dict[str, Any] = {
        "base_url": "https://llm.example.test/v1",
        "api_key": KEY,
        "model": "fake-model",
        "timeout_s": 5,
        "max_output_tokens": 300,
        "transport": httpx.MockTransport(handler),
        "sleep": sleeps.append,
    }
    options.update(overrides)
    return ChatCompletionsClient(**options), sleeps


MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


# --------------------------------------------------------------------- клиент


def test_success_sends_openai_compatible_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _reply('{"ok": true}')

    client, _ = _client(handler)
    result = client.complete(MESSAGES)
    assert result.status == "ok" and result.content == '{"ok": true}'
    assert (result.prompt_tokens, result.completion_tokens) == (11, 7)
    request = seen[0]
    assert str(request.url) == "https://llm.example.test/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "fake-model" and body["messages"] == MESSAGES
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0.0
    assert body["max_tokens"] == 300 and body["stream"] is False
    assert "tools" not in body and "functions" not in body
    assert KEY not in repr(result)


def test_http_400_retries_once_in_compatible_form() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format unsupported"}})
        return _reply("{}")

    client, _ = _client(handler)
    result = client.complete(MESSAGES)
    assert result.status == "ok" and result.attempts == 2
    assert "response_format" not in bodies[1] and "temperature" not in bodies[1]


@pytest.mark.req("T-NEG-TOOL-01", "T-NEG-TOOL-02")
@pytest.mark.parametrize("code", [401, 403])
def test_auth_errors_are_not_retried(code: int) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(code, json={"error": "no"})

    client, _ = _client(handler)
    result = client.complete(MESSAGES)
    assert result.status == "error" and result.error_code == f"auth_{code}" and len(calls) == 1


@pytest.mark.req("T-NEG-TOOL-03")
def test_rate_limit_respects_retry_after_with_cap() -> None:
    responses = iter([httpx.Response(429, headers={"Retry-After": "30"}), _reply("{}")])
    client, sleeps = _client(lambda request: next(responses))
    result = client.complete(MESSAGES)
    assert result.status == "ok" and result.attempts == 2 and sleeps == [2.0]


@pytest.mark.req("T-NEG-TOOL-04", "T-NEG-TOOL-05")
def test_server_errors_and_timeouts_are_bounded() -> None:
    calls = []

    def failing(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    client, _ = _client(failing)
    result = client.complete(MESSAGES)
    assert result.status == "unavailable" and result.error_code == "server_503" and len(calls) == 2

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = _client(timeout)
    assert client.complete(MESSAGES).status == "timeout"


def test_truncated_empty_and_part_list_content() -> None:
    client, _ = _client(lambda request: _reply('{"a": 1', finish="length"))
    assert client.complete(MESSAGES).error_code == "truncated"
    client, _ = _client(lambda request: _reply(""))
    assert client.complete(MESSAGES).error_code == "empty_content"
    client, _ = _client(
        lambda request: _reply([{"type": "text", "text": '{"a":'}, {"type": "text", "text": "1}"}])
    )
    assert client.complete(MESSAGES).content == '{"a":1}'
    client, _ = _client(lambda request: httpx.Response(200, json={"choices": []}))
    assert client.complete(MESSAGES).error_code == "no_choices"


@pytest.mark.req("SECURITY-01")
def test_endpoint_rules() -> None:
    assert chat_completions_url("https://h.test/v1/") == "https://h.test/v1/chat/completions"
    assert chat_completions_url("https://h.test/v1/chat/completions") == "https://h.test/v1/chat/completions"
    assert validate_endpoint("https://api.example.test/v1") == "api.example.test"
    assert validate_endpoint("http://localhost:11434/v1") == "localhost"
    assert validate_endpoint("http://host.docker.internal:8000/v1") == "host.docker.internal"
    for bad in ("http://api.example.test/v1", "ftp://x/v1", "https:///v1"):
        with pytest.raises(LlmEndpointError):
            validate_endpoint(bad)


def test_redirects_are_not_followed() -> None:
    client, _ = _client(lambda request: httpx.Response(302, headers={"Location": "https://evil.test/"}))
    result = client.complete(MESSAGES)
    assert result.status == "error" and result.error_code == "http_302"


# --------------------------------------------------------------------- конфигурация


@pytest.mark.req("SECURITY-01")
def test_live_config_requires_key_url_model_and_hides_key() -> None:
    settings = Settings.from_env(LIVE_ENV)
    assert settings.llm.live_enabled and settings.llm.model == "fake-model"
    assert KEY not in repr(settings) and KEY not in repr(settings.llm)
    for missing in ("ODIN_LLM_BASE_URL", "ODIN_LLM_MODEL", "ODIN_LLM_API_KEY"):
        with pytest.raises(ConfigError):
            Settings.from_env({**LIVE_ENV, missing: ""})
    with pytest.raises(ConfigError):
        Settings.from_env({**LIVE_ENV, "ODIN_LLM_BASE_URL": "http://remote.example.test/v1"})
    with pytest.raises(ConfigError):
        Settings.from_env({**LIVE_ENV, "ODIN_LLM_RECORD": "true"})  # запись без каталога


def test_openai_aliases_and_options() -> None:
    settings = Settings.from_env(
        {
            "ODIN_LLM_MODE": "live",
            "OPENAI_BASE_URL": "https://alias.example.test/v1",
            "OPENAI_API_KEY": KEY,
            "OPENAI_MODEL": "alias-model",
            "ODIN_LLM_TEMPERATURE": "none",
            "ODIN_LLM_JSON_MODE": "false",
            "ODIN_LLM_COMPOSER": "off",
        }
    )
    llm = settings.llm
    assert llm.live_enabled and llm.base_url == "https://alias.example.test/v1" and llm.model == "alias-model"
    assert llm.temperature is None and llm.json_mode is False and llm.composer is False
    explicit = Settings.from_env({**LIVE_ENV, "OPENAI_MODEL": "ignored"})
    assert explicit.llm.model == "fake-model"  # ODIN_LLM_* приоритетнее
    assert Settings.from_env({"OPENAI_API_KEY": KEY}).llm.mode == "off"  # ключ сам LLM не включает


def test_env_file_loader_does_not_override(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# комментарий\nODIN_LLM_MODE=live\nODIN_LLM_MODEL="m-1"\nexport ODIN_PORT=9090 # порт\nBROKEN\n',
        encoding="utf-8",
    )
    environ = {"ODIN_LLM_MODE": "off"}
    loaded = load_env_file(env_file, environ)
    assert environ == {"ODIN_LLM_MODE": "off", "ODIN_LLM_MODEL": "m-1", "ODIN_PORT": "9090"}
    assert loaded == ["ODIN_LLM_MODEL", "ODIN_PORT"]
    assert load_env_file(tmp_path / "missing.env", environ) == []


# --------------------------------------------------------------------- Gateway


def _gateway(fake: Any, **settings: Any) -> LlmGateway:
    llm = Settings.from_env({**LIVE_ENV, **settings}).llm
    gateway = LlmGateway(settings=llm, client_factory=lambda _: fake)
    gateway.set_call_limit(4)
    return gateway


def _interpret(gateway: LlmGateway, text: str = "в Казань") -> Any:
    return gateway.complete_structured(
        role="interpreter", text=text, reference_date="2026-10-25", schema_version="1.0.0"
    )


DELTA = {"destination": {"value": "Казань", "span": "в Казань"}}


def test_gateway_live_validates_structured_output() -> None:
    fake = ScriptedLlm({"в Казань": DELTA})
    result = _interpret(_gateway(fake))
    assert result.status == "ok" and result.structured_output["destination"]["value"] == "Казань"
    assert result.evidence_refs == ["llm:live:fake-model"]
    assert fake.requests[0][0]["role"] == "system" and "<<<\nв Казань\n>>>" in fake.requests[0][1]["content"]


def test_gateway_repairs_once_then_degrades() -> None:
    replies = iter(["это не JSON", json.dumps(DELTA)])
    fake = ScriptedLlm(lambda text, messages: next(replies))
    gateway = _gateway(fake)
    result = _interpret(gateway)
    assert result.status == "ok" and len(fake.requests) == 2
    assert fake.requests[1][-1]["role"] == "user" and "не прошёл проверку" in fake.requests[1][-1]["content"]
    assert [call.status for call in gateway.calls] == ["invalid_output", "ok"]
    assert gateway.calls[-1].repaired is True

    always_bad = _gateway(ScriptedLlm(lambda text, messages: '{"adults": {"value": 99, "span": "x"}}'))
    failed = _interpret(always_bad)
    assert failed.status == "error" and failed.structured_output is None


def test_gateway_budget_and_provider_failure_are_honest() -> None:
    gateway = _gateway(ScriptedLlm({"в Казань": DELTA}))
    gateway.set_call_limit(1)
    gateway.start_turn()
    assert _interpret(gateway).status == "ok"
    exhausted = _interpret(gateway)
    assert exhausted.status == "unavailable" and "бюджет" in exhausted.uncertainties[0]

    failing = ScriptedLlm({}, failure=ChatResult("error", None, "auth_401", 401, 1, 3))
    result = _interpret(_gateway(failing))
    assert result.status == "unavailable" and "auth_401" in result.uncertainties[0]


@pytest.mark.req("T-LLM-MODE-03", "DEC-24")
def test_recorded_live_answer_is_replayable_with_strict_key(tmp_path: Path) -> None:
    fake = ScriptedLlm({"в Казань": DELTA})
    live = _gateway(fake, ODIN_LLM_RECORD="true", ODIN_LLM_REPLAY_DIR=str(tmp_path))
    assert _interpret(live).status == "ok"
    records = list(tmp_path.glob("*.json"))
    assert len(records) == 1
    stored = json.loads(records[0].read_text(encoding="utf-8"))
    assert "в Казань" not in json.dumps(stored, ensure_ascii=False).replace('"span": "в Казань"', "")
    assert stored["key"] == replay_key(
        text="в Казань", reference_date="2026-10-25", schema_version="1.0.0", role="interpreter"
    )
    replay = LlmGateway(settings=LlmSettings(mode="replay", replay_dir=tmp_path))
    replayed = _interpret(replay)
    assert replayed.status == "ok" and replayed.structured_output == stored["structured_output"]
    assert _interpret(replay, "в Сочи").status == "unavailable"  # другой текст не подставляется


def test_extract_json_object_variants() -> None:
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('Ответ: {"a": 2} конец') == {"a": 2}
    with pytest.raises(ValueError):
        extract_json_object("нет json")


def test_default_client_factory_uses_settings() -> None:
    llm = Settings.from_env(LIVE_ENV).llm
    client = default_client_factory(llm, transport=httpx.MockTransport(lambda request: _reply("{}")))
    assert client.url == "https://llm.example.test/v1/chat/completions" and client.model == "fake-model"
    assert client.complete(MESSAGES).status == "ok"
