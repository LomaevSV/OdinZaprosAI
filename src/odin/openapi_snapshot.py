"""Снимок OpenAPI (API-01, CONTRACT-03): `schemas/openapi.json` закрепляет пути, методы и модели.

python -m odin.openapi_snapshot          # перезаписать снимок
python -m odin.openapi_snapshot --check  # сверить; код 1 при расхождении
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from odin.contracts.schema_export import render

DEFAULT_PATH = Path("schemas") / "openapi.json"


def build_openapi() -> dict:
    from odin.api import openapi_document

    return openapi_document()


def rendered() -> str:
    return render(build_openapi())


def diff_openapi(path: Path = DEFAULT_PATH) -> list[str]:
    if not path.is_file():
        return [f"нет снимка OpenAPI: {path.name}"]
    if path.read_bytes() != rendered().encode("utf-8"):
        return [f"снимок OpenAPI расходится с кодом: {path.name}"]
    return []


def write_openapi(path: Path = DEFAULT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(rendered().encode("utf-8"))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m odin.openapi_snapshot")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        problems = diff_openapi(args.path)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    write_openapi(args.path)
    print(f"снимок OpenAPI записан: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
