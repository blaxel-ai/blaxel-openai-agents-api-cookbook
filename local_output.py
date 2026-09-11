"""Save locally verified reader artifacts under one run directory."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

OUTPUT_ROOT = Path(__file__).resolve().parent / "outputs"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FILENAMES = frozenset({"summary.md", "review.md"})


def save_verified_output(run_id: str, filename: str, contents: str) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"invalid output run ID {run_id!r}")
    if filename not in _FILENAMES:
        raise ValueError(f"unsupported verified output filename {filename!r}")
    destination = OUTPUT_ROOT / run_id / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{filename}.", dir=destination.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(contents)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(f"saved verified local {filename}: {destination}")
    return destination
