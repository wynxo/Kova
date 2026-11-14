"""Disposable regex scanner; newline-delimited JSON preserves partial matches."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from .code_files import _search_python_scan


def main() -> None:
    base, query, glob = json.load(sys.stdin)

    def report(match: str) -> None:
        print(json.dumps({"match": match}), flush=True)

    result = _search_python_scan(Path(base), query, True, glob, on_match=report)
    print(json.dumps({"result": result}), flush=True)


if __name__ == "__main__":
    main()
