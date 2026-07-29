"""빌드 컨텍스트의 Python 파일을 바이트코드 생성 없이 구문 검사합니다."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("raw_solver", "anomaly", "risk", "ci")


def main() -> None:
    checked = 0
    for directory_name in SOURCE_DIRS:
        for path in sorted((ROOT / directory_name).rglob("*.py")):
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            checked += 1
    print(f"[VALIDATE PASS] Python files={checked}")


if __name__ == "__main__":
    main()
