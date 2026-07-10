"""Robust reader for the self-contained dashboard's embedded JSON payload."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DATA_MARKER = "const DATA ="


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def decode_dashboard_payload(text: str, *, source: str = "dashboard") -> dict[str, Any]:
    """Decode exactly one JSON value after ``const DATA =``.

    Regular expressions are unsafe here because valid user/research strings may
    contain braces, semicolons, or text resembling later JavaScript statements.
    ``JSONDecoder.raw_decode`` stops at the structural end of the JSON value.
    """
    start = text.find(DATA_MARKER)
    if start < 0:
        raise ValueError(f"DATA payload not found in {source}")
    fragment = text[start + len(DATA_MARKER):].lstrip()
    try:
        payload, end = json.JSONDecoder(parse_constant=_reject_constant).raw_decode(fragment)
    except (json.JSONDecodeError, ValueError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise ValueError(f"invalid DATA payload in {source}: {detail}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"DATA payload is not an object in {source}")
    if fragment[end:].lstrip()[:1] != ";":
        raise ValueError(f"DATA payload terminator not found in {source}")
    return payload


def read_dashboard_payload(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    return decode_dashboard_payload(
        source.read_text(encoding="utf-8"), source=str(source),
    )
