"""JSON-lines files for records the ETL sets aside (dead letters, rejects)."""
from __future__ import annotations

import json
import os
from pathlib import Path


def write_jsonl(path: str | os.PathLike, entries: list[dict]) -> Path:
    """Write ``entries`` as JSON lines, atomically (values JSON lacks become strings)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, path)
    return path
