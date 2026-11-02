"""JSON response envelope (design doc §8.2), errors, exit codes."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from obd_engine import __version__

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DATA = 3
EXIT_ENV = 4

# Hard cap for a serialized envelope (~4k tokens at ~4 chars/token).
MAX_CHARS = 16_000

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(value: Any, limit: int = 80) -> str:
    """Neutralise untrusted strings (column names, values) before they reach Claude."""
    s = _CTRL.sub("", str(value))
    return s if len(s) <= limit else s[: limit - 1] + "…"


class ObdError(Exception):
    def __init__(self, code: str, message: str, hint: str = "", exit_code: int = EXIT_DATA, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.hint, self.exit_code, self.data = code, message, hint, exit_code, data


class Envelope(BaseModel):
    status: str = "ok"  # ok | partial | needs_input | error
    summary: str = ""
    data: Any = None
    units: dict[str, str] = Field(default_factory=dict)
    method: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    needs_input: list[dict[str, Any]] = Field(default_factory=list)
    truncated: dict[str, Any] | None = None
    versions: dict[str, str] = Field(default_factory=lambda: {"engine": __version__})
    error: dict[str, Any] | None = None

    def render(self, max_chars: int = MAX_CHARS) -> str:
        def dump(e: "Envelope") -> str:
            return json.dumps(e.model_dump(exclude_none=True), default=str, ensure_ascii=False)

        text = dump(self)
        if len(text) <= max_chars:
            return text
        # Over budget: thin out the bulkiest list (evenly, keeping the ends) until it fits, then flag truncation.
        env = self.model_copy(deep=True)
        env.truncated = {"reason": "output exceeded the token budget", "hint": "narrow the request (fewer parameters / shorter window)"}
        for _ in range(40):
            if len(dump(env)) <= max_chars:
                break
            if not _thin_largest_list(env):
                break
        return dump(env)


def _walk_lists(obj: Any, found: list[tuple[int, list[Any]]]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            _walk_lists(v, found)
    elif isinstance(obj, list):
        found.append((len(json.dumps(obj, default=str)), obj))
        for v in obj:
            _walk_lists(v, found)


def _thin_largest_list(env: Envelope) -> bool:
    found: list[tuple[int, list[Any]]] = []
    _walk_lists(env.data, found)
    _walk_lists(env.evidence, found)
    found = [f for f in found if len(f[1]) > 3]
    if not found:
        return False
    # Never drop whole analysis modules from an answer: thin their findings instead (the module list is kept intact).
    kept = [f for f in found if not all(isinstance(x, dict) and "module" in x and "findings" in x for x in f[1])]
    found = kept or found
    lst = max(found, key=lambda f: f[0])[1]
    n_keep = max(len(lst) // 2, 2)
    keep = sorted({round(i * (len(lst) - 1) / (n_keep - 1)) for i in range(n_keep)})
    lst[:] = [lst[i] for i in keep]
    return True


def error_envelope(err: ObdError) -> Envelope:
    return Envelope(
        status="error",
        summary=err.message,
        data=err.data,
        error={"code": err.code, "message": err.message, "hint": err.hint},
    )
