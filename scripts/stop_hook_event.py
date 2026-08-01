#!/usr/bin/env python3
"""Normalize host-specific Stop hooks into one turn-summary contract."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from typing import Any, Mapping


SUPPORTED_HOSTS = ("claude", "codex")


class StopHookPayloadError(ValueError):
    """The host payload does not satisfy the declared Stop-hook contract."""


@dataclass(frozen=True)
class StopHookEvent:
    host: str
    session_id: str
    response_text: str
    tool_count: int | None
    tool_names: tuple[str, ...]

    @property
    def summary(self) -> str:
        excerpt = self.response_text.strip().replace("\n", " ")[:120]
        if self.tool_count is None:
            return f"Turn summary ({self.host}; tool count unavailable); response excerpt: {excerpt}"
        top = ", ".join(self.tool_names[:3]) if self.tool_names else "no tools"
        return (
            f"Turn summary: {self.tool_count} tool calls ({top}); "
            f"response excerpt: {excerpt}"
        )

    @property
    def floor_summary(self) -> str:
        excerpt = self.response_text.strip().replace("\n", " ")[:100]
        if self.tool_count is None:
            return f"Floor (un-onboarded {self.host}): tool count unavailable; {excerpt}"
        return f"Floor (un-onboarded): {self.tool_count} tool calls; {excerpt}"

    @property
    def complexity(self) -> float:
        if self.tool_count is None:
            return 0.3
        return min(self.tool_count / 10.0, 0.85)

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tool_names"] = list(self.tool_names)
        payload["summary"] = self.summary
        payload["floor_summary"] = self.floor_summary
        # The floor transport predates nullable counts. Its summary retains the
        # distinction; zero is only the wire-compatible fallback value.
        payload["floor_tool_count"] = self.tool_count if self.tool_count is not None else 0
        payload["complexity"] = self.complexity
        return payload


def _decode_payload(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        decoded = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise StopHookPayloadError("hook input is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise StopHookPayloadError("hook input must be a JSON object")
    return decoded


def normalize_claude(payload: Mapping[str, Any]) -> StopHookEvent:
    """Normalize current Claude Stop input, with a legacy payload fallback."""
    if "last_assistant_message" in payload:
        message = payload.get("last_assistant_message") or ""
    else:
        message = payload.get("final_text") or ""
    if not isinstance(message, str):
        raise StopHookPayloadError(
            "Claude Stop payload last_assistant_message must be a string or null"
        )

    # Current Claude Stop events do not include tool calls. Older plugin
    # harnesses did, so preserve their observed count without claiming zero
    # when the field is absent.
    tool_calls = payload.get("tool_calls")
    if tool_calls is None:
        count = None
        names: tuple[str, ...] = ()
    elif isinstance(tool_calls, list):
        count = len(tool_calls)
        names = tuple(
            str(call["name"])
            for call in tool_calls
            if isinstance(call, dict) and call.get("name")
        )
    else:
        raise StopHookPayloadError("legacy Claude Stop tool_calls must be a list")
    return StopHookEvent(
        host="claude",
        session_id=str(payload.get("session_id") or "").strip(),
        response_text=message[:512],
        tool_count=count,
        tool_names=names,
    )


def normalize_codex(payload: Mapping[str, Any]) -> StopHookEvent:
    message = payload.get("last_assistant_message") or ""
    if not isinstance(message, str):
        raise StopHookPayloadError("Codex Stop payload last_assistant_message must be a string or null")
    return StopHookEvent(
        host="codex",
        session_id=str(payload.get("session_id") or "").strip(),
        response_text=message[:512],
        tool_count=None,
        tool_names=(),
    )


def normalize_stop_hook(
    raw: str | Mapping[str, Any],
    *,
    host: str,
) -> StopHookEvent:
    payload = _decode_payload(raw)
    if host == "claude":
        return normalize_claude(payload)
    if host == "codex":
        return normalize_codex(payload)
    raise StopHookPayloadError(f"unsupported hook host: {host or '<missing>'}")


def main(argv: list[str] | None = None, stdin_text: str | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=SUPPORTED_HOSTS, required=True)
    args = parser.parse_args(argv)
    raw = sys.stdin.read() if stdin_text is None else stdin_text
    try:
        event = normalize_stop_hook(raw, host=args.host)
    except StopHookPayloadError as exc:
        print(f"stop_hook_event.py: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(event.to_json_dict(), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
