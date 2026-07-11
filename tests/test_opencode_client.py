from backend.audit import opencode_client


def _session_with_replies(monkeypatch, *texts):
    replies = iter(texts)
    session = opencode_client.AuditSession(system=None)
    session.session_id = "session-test"
    monkeypatch.setattr(
        session,
        "say",
        lambda _text: opencode_client.Reply(text=next(replies)),
    )
    return session


def test_collect_verdict_rejects_unrelated_json_and_retries(monkeypatch):
    session = _session_with_replies(
        monkeypatch,
        '{"error": "insufficient evidence"}',
        '{"decision": "approve", "reason": "checked"}',
    )

    result = session.collect_verdict(
        "review this",
        validator=lambda obj: (
            obj.get("decision") in {"approve", "reject"}
            and isinstance(obj.get("reason"), str)
        ),
        max_nudges=1,
    )

    assert isinstance(result, opencode_client.Verdict)
    assert result.args["decision"] == "approve"


def test_collect_verdict_fails_closed_when_json_never_matches_schema(monkeypatch):
    session = _session_with_replies(
        monkeypatch,
        '{"error": "insufficient evidence"}',
        '{"concept": "A"}',
    )

    result = session.collect_verdict(
        "review this",
        validator=lambda obj: "decision" in obj and "reason" in obj,
        max_nudges=1,
    )

    assert isinstance(result, opencode_client.NoVerdict)
    assert result.reason == "no valid JSON verdict after 2 attempt(s)"


def test_collect_verdict_fails_closed_when_validator_raises(monkeypatch):
    session = _session_with_replies(monkeypatch, '{"decision": "approve"}')

    def broken_validator(_obj):
        raise ValueError("rubric configuration is broken")

    result = session.collect_verdict(
        "review this",
        validator=broken_validator,
        max_nudges=0,
    )

    assert isinstance(result, opencode_client.NoVerdict)
