from pathlib import Path

from monitor.ingest import (
    extract_session_meta,
    iter_events,
    iter_tool_calls,
    iter_tool_results,
    render_transcript,
    to_inspect_messages,
)

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_session.jsonl"


def test_iter_events_yields_every_line():
    events = list(iter_events(FIXTURE))
    assert len(events) == 6
    assert events[0]["type"] == "permission-mode"
    assert events[-1]["type"] == "assistant"


def test_extract_session_meta_pulls_from_first_user_event():
    meta = extract_session_meta(iter_events(FIXTURE))
    assert meta["session_id"] == "test-session-001"
    assert meta["cwd"] == "/home/example/project"
    assert meta["git_branch"] == "main"
    assert meta["permission_mode"] == "default"
    assert meta["started_at"] == "2026-04-23T13:20:48.406Z"


def test_iter_tool_calls_extracts_structured_calls():
    calls = list(iter_tool_calls(iter_events(FIXTURE)))
    assert len(calls) == 1
    call = calls[0]
    assert call.tool_name == "Bash"
    assert call.tool_input == {"command": "ls *.py", "description": "List python files"}
    assert call.tool_use_id == "toolu_001"
    assert call.assistant_uuid == "a1"


def test_iter_tool_results_extracts_results_from_user_events():
    results = list(iter_tool_results(iter_events(FIXTURE)))
    assert len(results) == 1
    assert results[0].tool_use_id == "toolu_001"
    assert "main.py" in results[0].content


def test_render_transcript_includes_text_calls_and_results():
    transcript = render_transcript(iter_events(FIXTURE))
    assert "[USER]" in transcript
    assert "List the python files" in transcript
    assert "[ASSISTANT]" in transcript
    assert "I'll list the python files" in transcript
    assert "[TOOL_CALL] Bash" in transcript
    assert "ls *.py" in transcript
    assert "[TOOL_RESULT toolu_001]" in transcript
    assert "main.py" in transcript


def test_render_transcript_truncates_large_payloads():
    """Tool results larger than the cap should be truncated, not dropped."""
    huge = "X" * 100_000
    events = [
        {
            "type": "user",
            "uuid": "u",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": huge}],
            },
        }
    ]
    rendered = render_transcript(iter(events), max_chars_per_payload=200)
    assert "truncated" in rendered.lower()
    assert len(rendered) < 1000


def test_to_inspect_messages_round_trips_basic_shape():
    msgs = to_inspect_messages(iter_events(FIXTURE))
    # 2 user (one plain, one tool_result wrapper) + 2 assistant
    assert len(msgs) == 4
    # first should be user with plain string
    assert msgs[0].role == "user"
    assert "List the python files" in str(msgs[0].content)
    # second assistant with a tool call
    asst = msgs[1]
    assert asst.role == "assistant"
    assert asst.tool_calls and asst.tool_calls[0].function == "Bash"


def test_user_event_with_string_content_handled():
    events = [{"type": "user", "uuid": "u", "message": {"role": "user", "content": "hi"}}]
    msgs = to_inspect_messages(iter(events))
    assert len(msgs) == 1
    assert msgs[0].content == "hi"


def test_assistant_event_with_only_text_content_handled():
    events = [
        {
            "type": "assistant",
            "uuid": "a",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "just text"}],
            },
        }
    ]
    msgs = to_inspect_messages(iter(events))
    assert len(msgs) == 1
    assert msgs[0].content == "just text"
    assert not msgs[0].tool_calls


def test_non_message_events_are_skipped_in_messages_and_transcript():
    """system / permission-mode / file-history-snapshot etc must not produce messages."""
    msgs = to_inspect_messages(iter_events(FIXTURE))
    # 6 events in fixture, only 4 are user/assistant
    assert len(msgs) == 4
