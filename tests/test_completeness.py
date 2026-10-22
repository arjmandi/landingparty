import json

from analysis.completeness import audit_transcript, parse_transcript


def test_claude_stream_deduplicates_message_chunks_and_counts_compaction():
    transcript = [
        {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}},
        {
            "type": "assistant",
            "message": {"id": "m1", "content": [{"type": "text", "text": "one"}]},
        },
        {
            "type": "assistant",
            "message": {"id": "m1", "content": [{"type": "text", "text": "two"}]},
        },
        {"type": "system", "subtype": "compact_boundary"},
    ]
    result = parse_transcript(transcript)
    assert result["model_calls_transcript"] == 1
    assert result["compaction_count"] == 1
    assert (
        audit_transcript([{"kind": "model_call"}], transcript, shell_audited=True)["verdict"]
        == "complete"
    )
    assert audit_transcript([], transcript, shell_audited=True)["verdict"] == "partial"


def test_network_shell_candidates_do_not_echo_secret_commands():
    transcript = [
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {
                            "command": "curl --noproxy '*' -H 'secret: do-not-log' https://example.org"
                        },
                    },
                    {"type": "tool_use", "name": "Bash", "input": {"command": "cat notes.md"}},
                ],
            },
        }
    ]
    result = audit_transcript([{"kind": "model_call"}], transcript, shell_audited=True)
    assert result["verdict"] == "partial"
    assert result["shell_commands"] == 2
    assert result["shell_candidates"][0]["explicit_proxy_bypass"]
    assert "do-not-log" not in json.dumps(result)


def test_malformed_or_unidentified_transcript_cannot_be_complete():
    text = '{"type":"assistant","message":{"content":[]}}\nnot-json\n[]\n'
    result = parse_transcript(text)
    assert result["malformed_lines"] == 2
    assert result["unidentified_model_messages"] == 1
    assert audit_transcript([], text, shell_audited=True)["verdict"] == "partial"


def test_no_transcript_model_calls_is_not_completeness_evidence():
    assert audit_transcript([], [], shell_audited=True)["verdict"] == "partial"
