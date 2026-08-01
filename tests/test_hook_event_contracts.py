from __future__ import annotations

import pytest

from scripts import edit_hook_event, stop_hook_event


def test_claude_edit_contract_uses_scalar_file_path():
    event = edit_hook_event.normalize_edit_hook(
        {
            "session_id": "claude-slot",
            "tool_name": "MultiEdit",
            "tool_use_id": "toolu_123",
            "tool_input": {"file_path": "src/a.py", "edits": [{"old": "a", "new": "b"}]},
        },
        host="claude",
    )

    assert event.host == "claude"
    assert event.tool_use_id == "toolu_123"
    assert event.paths == ("src/a.py",)


def test_codex_edit_contract_parses_patch_envelope_in_order():
    event = edit_hook_event.normalize_edit_hook(
        {
            "session_id": "codex-slot",
            "tool_name": "apply_patch",
            "tool_use_id": "call_456",
            "tool_input": {
                "command": """*** Begin Patch
*** Add File: docs/new file.md
+new
*** Update File: src/a.py
*** Move to: src/b.py
@@
-old
+new
*** Delete File: old.py
*** Update File: src/a.py
@@
+again
*** End Patch"""
            },
        },
        host="codex",
    )

    assert event.host == "codex"
    assert event.tool_name == "apply_patch"
    assert event.tool_use_id == "call_456"
    assert event.paths == ("docs/new file.md", "src/a.py", "src/b.py", "old.py")


@pytest.mark.parametrize(
    "command",
    [
        "*** Update File: a.py\n*** End Patch",
        "*** Begin Patch\n*** Move to: b.py\n*** End Patch",
        "*** Begin Patch\n*** Add File: \n*** End Patch",
    ],
)
def test_codex_edit_contract_rejects_malformed_patch_envelopes(command: str):
    with pytest.raises(edit_hook_event.EditHookPayloadError):
        edit_hook_event.normalize_edit_hook(
            {
                "session_id": "codex-slot",
                "tool_name": "apply_patch",
                "tool_use_id": "call_bad",
                "tool_input": {"command": command},
            },
            host="codex",
        )


def test_codex_edit_contract_requires_tool_use_id_for_per_edit_ownership():
    with pytest.raises(edit_hook_event.EditHookPayloadError, match="tool_use_id"):
        edit_hook_event.normalize_edit_hook(
            {
                "session_id": "codex-slot",
                "tool_name": "apply_patch",
                "tool_input": {
                    "command": "*** Begin Patch\n*** Update File: a.py\n*** End Patch"
                },
            },
            host="codex",
        )


def test_codex_edit_contract_rejects_explicit_failed_post_tool_payload():
    with pytest.raises(edit_hook_event.EditHookPayloadError, match="reports failure"):
        edit_hook_event.normalize_edit_hook(
            {
                "session_id": "codex-slot",
                "tool_name": "apply_patch",
                "tool_use_id": "call_failed",
                "tool_input": {
                    "command": "*** Begin Patch\n*** Update File: a.py\n*** End Patch"
                },
                "tool_response": {"result": {"success": False}},
            },
            host="codex",
        )


def test_host_is_explicit_and_never_inferred_from_payload_shape():
    with pytest.raises(edit_hook_event.EditHookPayloadError, match="Claude"):
        edit_hook_event.normalize_edit_hook(
            {
                "session_id": "slot",
                "tool_name": "apply_patch",
                "tool_use_id": "call_1",
                "tool_input": {
                    "command": "*** Begin Patch\n*** Update File: a.py\n*** End Patch"
                },
            },
            host="claude",
        )


def test_edit_contract_caps_path_fanout(monkeypatch):
    monkeypatch.setattr(edit_hook_event, "MAX_EDIT_PATHS", 2)
    command = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: a.py",
            "*** Add File: b.py",
            "*** Add File: c.py",
            "*** End Patch",
        ]
    )

    with pytest.raises(edit_hook_event.EditHookPayloadError, match="maximum is 2"):
        edit_hook_event.normalize_edit_hook(
            {
                "session_id": "slot",
                "tool_name": "apply_patch",
                "tool_use_id": "call_cap",
                "tool_input": {"command": command},
            },
            host="codex",
        )


def test_claude_stop_contract_reads_last_message_without_inventing_tool_count():
    event = stop_hook_event.normalize_stop_hook(
        {
            "session_id": "claude-slot",
            "stop_hook_active": False,
            "last_assistant_message": "Finished the change.",
        },
        host="claude",
    )

    assert event.tool_count is None
    assert event.tool_names == ()
    assert "tool count unavailable" in event.summary
    assert "Finished the change" in event.summary


def test_claude_stop_contract_accepts_legacy_summary_fields():
    event = stop_hook_event.normalize_stop_hook(
        {
            "session_id": "claude-slot",
            "tool_calls": [{"name": "Read"}, {"name": "Edit"}],
            "final_text": "Finished the legacy change.",
        },
        host="claude",
    )

    assert event.tool_count == 2
    assert event.tool_names == ("Read", "Edit")
    assert "Finished the legacy change" in event.summary


def test_codex_stop_contract_reads_last_message_without_inventing_tool_count():
    event = stop_hook_event.normalize_stop_hook(
        {
            "session_id": "codex-slot",
            "turn_id": "turn_1",
            "stop_hook_active": False,
            "last_assistant_message": "Implemented and verified the change.",
        },
        host="codex",
    )

    assert event.tool_count is None
    assert event.tool_names == ()
    assert event.complexity == 0.3
    assert "tool count unavailable" in event.summary
    assert "Implemented and verified" in event.summary
