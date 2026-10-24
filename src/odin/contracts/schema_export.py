"""Экспорт JSON Schema нормативных контрактов (CONTRACT-03, план S1).

Источник схем — только классы ``odin.contracts`` (``NORMATIVE_MODELS``: 17 типов ТЗ §7 + ``Price`` +
``Availability``). Каталог ``schemas/`` — закреплённый снимок: расхождение кода и снимка — FAIL
в T-CON-SCHEMA-01. Обновлять снимок нормативных схем можно только по согласованию.

Запуск из корня репозитория:
    python -m odin.contracts.schema_export          # перезаписать снимок в ./schemas
    python -m odin.contracts.schema_export --check  # только сверить; код возврата 1 при расхождении
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from odin.contracts import NORMATIVE_MODELS, SCHEMA_VERSION

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_FILE_SUFFIX = ".schema.json"
DEFAULT_SCHEMA_DIR = Path("schemas")


def schema_id(name: str) -> str:
    return f"urn:odin:contracts:{SCHEMA_VERSION}:{name}"


def schema_filename(name: str) -> str:
    return f"{name}{SCHEMA_FILE_SUFFIX}"


def build_schema(name: str) -> dict[str, Any]:
    """JSON Schema одного нормативного типа (режим валидации входа)."""
    model = NORMATIVE_MODELS[name]
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "$id": schema_id(name),
        **model.model_json_schema(mode="validation"),
    }


def build_schemas() -> dict[str, dict[str, Any]]:
    return {name: build_schema(name) for name in NORMATIVE_MODELS}


def render(schema: dict[str, Any]) -> str:
    """Детерминированное текстовое представление: UTF-8 без BOM, LF, отступ 2."""
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


def write_schemas(directory: Path = DEFAULT_SCHEMA_DIR) -> list[Path]:
    """Перезаписывает снимок; удаляет устаревшие ``*.schema.json``, которых нет в наборе."""
    directory.mkdir(parents=True, exist_ok=True)
    expected = {schema_filename(name) for name in NORMATIVE_MODELS}
    for stale in directory.glob(f"*{SCHEMA_FILE_SUFFIX}"):
        if stale.name not in expected:
            stale.unlink()
    written: list[Path] = []
    for name, schema in build_schemas().items():
        path = directory / schema_filename(name)
        path.write_bytes(render(schema).encode("utf-8"))
        written.append(path)
    return written


def diff_schemas(directory: Path = DEFAULT_SCHEMA_DIR) -> list[str]:
    """Расхождения снимка и кода. Пустой список — снимок актуален."""
    expected = {schema_filename(name): render(schema) for name, schema in build_schemas().items()}
    present = {p.name for p in directory.glob(f"*{SCHEMA_FILE_SUFFIX}")} if directory.is_dir() else set()
    problems = [f"нет файла снимка: {name}" for name in sorted(expected.keys() - present)]
    problems += [f"лишний файл снимка: {name}" for name in sorted(present - expected.keys())]
    for name in sorted(expected.keys() & present):
        if (directory / name).read_bytes() != expected[name].encode("utf-8"):
            problems.append(f"снимок расходится с кодом: {name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m odin.contracts.schema_export",
        description="Экспорт или сверка JSON Schema нормативных контрактов (CONTRACT-03).",
    )
    parser.add_argument(
        "--dir", type=Path, default=DEFAULT_SCHEMA_DIR, help="каталог снимка (по умолчанию ./schemas)"
    )
    parser.add_argument("--check", action="store_true", help="только сверить снимок с кодом")
    args = parser.parse_args(argv)

    if args.check:
        problems = diff_schemas(args.dir)
        for problem in problems:
            print(problem, file=sys.stderr)
        if problems:
            print(
                "Снимок схем неактуален. Изменение нормативных схем - только по согласованию.",
                file=sys.stderr,
            )
            return 1
        print(f"снимок актуален: {len(NORMATIVE_MODELS)} схем в {args.dir}")
        return 0

    paths = write_schemas(args.dir)
    print(f"записано схем: {len(paths)} в {args.dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
