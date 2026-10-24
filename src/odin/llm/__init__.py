"""LLM-роли S1: интерпретатор и Composer. Ни одна роль не подтверждает факты (ARCH-04)."""

from __future__ import annotations

from odin.llm.composer import ComposerFacts, compose_answer, verify_facts
from odin.llm.gateway import LlmBudget, LlmBudgetExceeded, LlmGateway, replay_key
from odin.llm.interpreter import InterpretationOutcome, interpret_structured_form

__all__ = [
    "ComposerFacts",
    "InterpretationOutcome",
    "LlmBudget",
    "LlmBudgetExceeded",
    "LlmGateway",
    "compose_answer",
    "interpret_structured_form",
    "replay_key",
    "verify_facts",
]
