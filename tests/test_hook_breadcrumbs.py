"""Diagnostic breadcrumb tests for post-identity and post-edit.

The breadcrumb mechanism (UNITARES_HOOK_DEBUG=1) is a read-only diagnostic:
it must record one line per silent-exit and must NOT change which exits
happen. These tests verify both halves — the line is written when debug
is on, and the hook still exits 0 on every silent path.

Operator workflow that depends on this: enable UNITARES_HOOK_DEBUG=1 for a
day across the fleet, then read ~/.unitares/hook-skips.log to learn which
gate is making sessions go dark.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


PLUGIN_ROOT = Path(__file__).parent.parent
POST_IDENTITY = PLUGIN_ROOT / "hooks" / "post-identity"
POST_EDIT = PLUGIN_ROOT / "hooks" / "post-edit"
PRE_GOVERNANCE = PLUGIN_ROOT / "hooks" / "pre-governance-call"


def _run(hook: Path, hook_input: dict, workspace: Path, debug_log: Path):
    env = os.environ.copy()
    env["UNITARES_HOOK_DEBUG"] = "1"
    env["UNITARES_HOOK_DEBUG_LOG"] = str(debug_log)
    return subprocess.run(
        [str(hook)],
        input=json.dumps(hook_input),
        text=True,
        capture_output=True,
        timeout=10,
        cwd=str(workspace),
        env=env,
    )


def _run_no_debug(hook: Path, hook_input: dict, workspace: Path, debug_log: Path):
    env = {k: v for k, v in os.environ.items() if k != "UNITARES_HOOK_DEBUG"}
    env["UNITARES_HOOK_DEBUG_LOG"] = str(debug_log)
    return subprocess.run(
        [str(hook)],
        input=json.dumps(hook_input),
        text=True,
        capture_output=True,
        timeout=10,
        cwd=str(workspace),
        env=env,
    )


class TestPostIdentityBreadcrumbs:
    def test_tool_name_mismatch_breadcrumbs(self, tmp_path):
        log = tmp_path / "skips.log"
        hook_input = {
            "session_id": "s1",
            "tool_name": "mcp__unitares-governance__process_agent_update",
            "tool_response": {"content": [{"type": "text", "text": "{}"}]},
        }
        result = _run(POST_IDENTITY, hook_input, tmp_path, log)
        assert result.returncode == 0
        contents = log.read_text() if log.exists() else ""
        assert "hook=post-identity" in contents
        assert "tool_name_mismatch" in contents
        assert "process_agent_update" in contents

    def test_no_uuid_breadcrumbs(self, tmp_path):
        log = tmp_path / "skips.log"
        # onboard call but response has no uuid (e.g. a server error response)
        inner = {"success": True}  # no uuid
        hook_input = {
            "session_id": "s1",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_response": [{"type": "text", "text": json.dumps(inner)}],
        }
        result = _run(POST_IDENTITY, hook_input, tmp_path, log)
        assert result.returncode == 0
        contents = log.read_text() if log.exists() else ""
        assert "no_uuid_in_response" in contents

    def test_response_success_false_breadcrumbs(self, tmp_path):
        log = tmp_path / "skips.log"
        inner = {"success": False, "uuid": "u-1"}
        hook_input = {
            "session_id": "s1",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_response": [{"type": "text", "text": json.dumps(inner)}],
        }
        result = _run(POST_IDENTITY, hook_input, tmp_path, log)
        assert result.returncode == 0
        contents = log.read_text() if log.exists() else ""
        assert "response_success_false" in contents

    def test_disabled_by_default(self, tmp_path):
        """Without UNITARES_HOOK_DEBUG=1, no log file is created."""
        log = tmp_path / "skips.log"
        hook_input = {
            "session_id": "s1",
            "tool_name": "mcp__unitares-governance__process_agent_update",
            "tool_response": {"content": [{"type": "text", "text": "{}"}]},
        }
        result = _run_no_debug(POST_IDENTITY, hook_input, tmp_path, log)
        assert result.returncode == 0
        assert not log.exists(), "debug log must not be created when flag is off"

    def test_happy_path_writes_no_breadcrumb(self, tmp_path):
        """A successful onboard write must not breadcrumb a skip."""
        log = tmp_path / "skips.log"
        inner = {
            "success": True,
            "uuid": "u-happy-1",
            "agent_id": "Test",
            "client_session_id": "csid",
            "display_name": "Test",
            "continuity_token_supported": True,
        }
        hook_input = {
            "session_id": "slot-happy",
            "tool_use_id": "toolu_happy_identity",
            "tool_name": "mcp__unitares-governance__onboard",
            "tool_response": [{"type": "text", "text": json.dumps(inner)}],
        }
        assert _run(PRE_GOVERNANCE, hook_input, tmp_path, log).returncode == 0
        result = _run(POST_IDENTITY, hook_input, tmp_path, log)
        assert result.returncode == 0
        contents = log.read_text() if log.exists() else ""
        assert "post-identity" not in contents, (
            f"happy path must not write a skip breadcrumb; got: {contents!r}"
        )


class TestPostEditBreadcrumbs:
    def test_no_session_payload_breadcrumbs(self, tmp_path):
        """Fresh workspace, no slot file — post-edit should breadcrumb and exit."""
        log = tmp_path / "skips.log"
        hook_input = {
            "session_id": "fresh-slot",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "x.py")},
        }
        result = _run(POST_EDIT, hook_input, tmp_path, log)
        assert result.returncode == 0
        contents = log.read_text() if log.exists() else ""
        assert "hook=post-edit" in contents
        assert "no_session_payload" in contents

    def test_disabled_by_default(self, tmp_path):
        log = tmp_path / "skips.log"
        hook_input = {
            "session_id": "fresh-slot",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(tmp_path / "x.py")},
        }
        result = _run_no_debug(POST_EDIT, hook_input, tmp_path, log)
        assert result.returncode == 0
        assert not log.exists()
