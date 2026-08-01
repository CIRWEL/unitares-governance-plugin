#!/usr/bin/env python3
"""Normalize host-specific edit hooks into one internal event contract."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from typing import Any, Mapping


SUPPORTED_HOSTS = ("claude", "codex")
MAX_EDIT_PATHS = 256


class EditHookPayloadError(ValueError):
    """The host payload does not satisfy the declared edit-hook contract."""


@dataclass(frozen=True)
class EditHookEvent:
    host: str
    session_id: str
    tool_name: str
    tool_use_id: str
    paths: tuple[str, ...]

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["paths"] = list(self.paths)
        return payload


def _decode_payload(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        decoded = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise EditHookPayloadError("hook input is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise EditHookPayloadError("hook input must be a JSON object")
    return decoded


def _tool_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("tool_input", payload.get("input", {}))
    if not isinstance(value, dict):
        raise EditHookPayloadError("tool_input must be a JSON object")
    return value


def _event(
    payload: Mapping[str, Any],
    *,
    host: str,
    tool_name: str,
    paths: list[str],
) -> EditHookEvent:
    ordered_paths = tuple(dict.fromkeys(path.strip() for path in paths if path.strip()))
    if not ordered_paths:
        raise EditHookPayloadError(f"{host} edit payload did not name any file paths")
    if len(ordered_paths) > MAX_EDIT_PATHS:
        raise EditHookPayloadError(
            f"{host} edit payload names {len(ordered_paths)} paths; maximum is {MAX_EDIT_PATHS}"
        )
    return EditHookEvent(
        host=host,
        session_id=str(payload.get("session_id") or "").strip(),
        tool_name=tool_name,
        tool_use_id=str(payload.get("tool_use_id") or payload.get("tool_call_id") or "").strip(),
        paths=ordered_paths,
    )


def normalize_claude(payload: Mapping[str, Any]) -> EditHookEvent:
    """Normalize Claude Code Edit, Write, or MultiEdit hook input."""
    tool_name = str(payload.get("tool_name") or "").strip()
    if tool_name not in {"Edit", "Write", "MultiEdit"}:
        raise EditHookPayloadError(f"unsupported Claude edit tool: {tool_name or '<missing>'}")
    tool_input = _tool_input(payload)
    path = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(path, str):
        raise EditHookPayloadError("Claude edit payload requires string tool_input.file_path")
    return _event(payload, host="claude", tool_name=tool_name, paths=[path])


def _codex_patch_paths(command: str) -> list[str]:
    lines = command.splitlines()
    begin = [index for index, line in enumerate(lines) if line == "*** Begin Patch"]
    end = [index for index, line in enumerate(lines) if line == "*** End Patch"]
    if len(begin) != 1 or len(end) != 1 or begin[0] >= end[0]:
        raise EditHookPayloadError(
            "Codex apply_patch command requires exactly one ordered Begin/End Patch envelope"
        )

    markers = (
        ("*** Add File: ", "add"),
        ("*** Update File: ", "update"),
        ("*** Delete File: ", "delete"),
        ("*** Move to: ", "move"),
    )
    paths: list[str] = []
    active_operation = ""
    for line in lines[begin[0] + 1 : end[0]]:
        match = next(((prefix, kind) for prefix, kind in markers if line.startswith(prefix)), None)
        if match is None:
            continue
        prefix, operation = match
        if operation == "move" and active_operation != "update":
            raise EditHookPayloadError("Codex Move to header must belong to an Update File block")
        path = line[len(prefix) :].strip()
        if not path:
            raise EditHookPayloadError(f"Codex {operation} header has an empty path")
        if path != "/dev/null":
            paths.append(path)
        if operation != "move":
            active_operation = operation
    return paths


def _response_is_explicit_failure(value: object, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(value, list):
        return any(_response_is_explicit_failure(item, depth + 1) for item in value)
    if not isinstance(value, Mapping):
        return False
    if value.get("isError") is True or value.get("success") is False:
        return True
    return any(
        _response_is_explicit_failure(value.get(key), depth + 1)
        for key in ("result", "structuredContent", "content")
        if key in value
    )


def normalize_codex(payload: Mapping[str, Any]) -> EditHookEvent:
    """Normalize Codex's canonical apply_patch hook input."""
    tool_name = str(payload.get("tool_name") or "").strip()
    if tool_name != "apply_patch":
        raise EditHookPayloadError(f"unsupported Codex edit tool: {tool_name or '<missing>'}")
    # Current Codex only emits apply_patch PostToolUse after
    # ToolOutput.success_for_logging(). Keep PreToolUse payloads (which have no
    # response) valid, but fail closed if a host version supplies an explicit
    # structured failure. The lease release happens before this normalizer.
    if _response_is_explicit_failure(payload.get("tool_response")):
        raise EditHookPayloadError("Codex apply_patch response reports failure")
    command = _tool_input(payload).get("command")
    if not isinstance(command, str):
        raise EditHookPayloadError("Codex apply_patch requires string tool_input.command")
    event = _event(
        payload,
        host="codex",
        tool_name=tool_name,
        paths=_codex_patch_paths(command),
    )
    if not event.tool_use_id:
        raise EditHookPayloadError("Codex apply_patch payload requires tool_use_id")
    return event


def normalize_edit_hook(
    raw: str | Mapping[str, Any],
    *,
    host: str,
) -> EditHookEvent:
    """Normalize an edit payload using the explicitly selected host adapter."""
    payload = _decode_payload(raw)
    if host == "claude":
        return normalize_claude(payload)
    if host == "codex":
        return normalize_codex(payload)
    raise EditHookPayloadError(f"unsupported hook host: {host or '<missing>'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=SUPPORTED_HOSTS, required=True)
    parser.add_argument("--output", choices=("event", "paths"), default="event")
    return parser


def main(argv: list[str] | None = None, stdin_text: str | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw = sys.stdin.read() if stdin_text is None else stdin_text
    try:
        event = normalize_edit_hook(raw, host=args.host)
    except EditHookPayloadError as exc:
        print(f"edit_hook_event.py: {exc}", file=sys.stderr)
        return 2
    output: Any = list(event.paths) if args.output == "paths" else event.to_json_dict()
    print(json.dumps(output, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
