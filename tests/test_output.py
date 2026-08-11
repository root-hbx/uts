import json

from uts.conn import DEFAULT_MAX_BYTES, Result, _Capped
from uts.inventory import Host
from uts.output import (
    CONTRACT,
    EXIT_ALL_FAILED,
    EXIT_BLOCKED,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_REMOTE_NONZERO,
    envelope,
    exit_code,
    fail,
    fold_wide_lines,
    host_items,
    render,
    to_json,
    truncation_note,
)


def host(name="a", ip="10.0.0.1"):
    return Host(name=name, ip=ip, user="ops", password="pw")


def ok(name="a", rc=0, **kw):
    return Result(host=host(name), rc=rc, **kw)


def dead(name="b", error="cannot connect"):
    return Result(host=host(name, "10.0.0.2"), error=error)


# ------------------------------------------------------------- capping counters


def test_capped_stops_at_line_limit_and_counts_the_rest():
    buf = _Capped(max_bytes=10_000, max_lines=3)
    buf.feed(b"".join(f"line{i}\n".encode() for i in range(10)))
    assert buf.text() == "line0\nline1\nline2\n"
    assert buf.truncated and buf.dropped_lines == 7


def test_capped_stops_at_byte_limit():
    buf = _Capped(max_bytes=10, max_lines=1000)
    buf.feed(b"x" * 100)
    assert len(buf.text()) == 10
    assert buf.dropped_bytes == 90


def test_capped_keeps_counting_across_chunks():
    buf = _Capped(max_bytes=5, max_lines=1000)
    for _ in range(4):
        buf.feed(b"abcdef\n")
    assert buf.dropped_bytes == 4 * 7 - 5


def test_capped_under_limit_is_not_truncated():
    buf = _Capped(max_bytes=DEFAULT_MAX_BYTES, max_lines=200)
    buf.feed(b"hello\n")
    assert buf.text() == "hello\n"
    assert not buf.truncated


# --------------------------------------------------------------- truncation note


def test_no_note_when_complete():
    assert truncation_note(ok()) is None


def test_note_reports_what_is_missing():
    note = truncation_note(ok(truncated=True, dropped_lines=8431, dropped_bytes=1_258_291))
    assert note is not None
    assert "8,431" in note and "1.2MB" in note


def test_aborted_note_says_exit_code_is_unknown():
    note = truncation_note(ok(aborted=True, truncated=True))
    assert note is not None and "exit code unknown" in note


# ---------------------------------------------------------------------- render


def test_render_shows_host_rc_and_stdout():
    out = render([ok(stdout="hello\n", duration=0.42)])
    assert "=== a (ops@10.0.0.1) · rc=0 · 0.42s ===" in out
    assert "hello" in out


def test_render_never_silently_swallows_a_dead_host():
    out = render([ok(stdout="hi\n"), dead(error="auth failed")])
    assert "UNREACHABLE" in out and "auth failed" in out


def test_render_marks_empty_output_explicitly():
    # "empty" and "never ran" must stay distinguishable, or the model reads empty
    # output as "no matches"
    assert "(no output)" in render([ok()])


def test_render_labels_stderr():
    assert "[stderr] boom" in render([ok(rc=1, stderr="boom\n")])


def test_json_round_trips():
    payload = json.loads(to_json([ok(stdout="hi\n"), dead()], "exec"))["hosts"]
    assert [p["host"] for p in payload] == ["a", "b"]
    assert payload[0]["reachable"] is True
    assert payload[1]["reachable"] is False and payload[1]["error"] == "cannot connect"


# ------------------------------------------------------------------ exit codes


def test_exit_codes_distinguish_unreachable_from_nonzero_rc():
    assert exit_code([ok()]) == EXIT_OK
    assert exit_code([ok(rc=1)]) == EXIT_REMOTE_NONZERO
    assert exit_code([ok(), dead()]) == EXIT_PARTIAL
    assert exit_code([dead(), dead("c")]) == EXIT_ALL_FAILED
    assert exit_code([]) == EXIT_ALL_FAILED


# ------------------------------------------------------------ column truncation


def test_fold_leaves_normal_lines_alone():
    assert fold_wide_lines("short\nalso short", max_cols=100) == "short\nalso short"


def test_fold_cuts_wide_lines_and_says_how_much_is_left():
    folded = fold_wide_lines("x" * 500, max_cols=200)
    assert folded.startswith("x" * 200)
    assert "300 more chars on this line" in folded


def test_fold_disabled_with_zero():
    assert fold_wide_lines("x" * 500, max_cols=0) == "x" * 500


def test_render_folds_wide_matrix_rows():
    # Line caps do nothing here: one row of a 104-column delay matrix fills half a screen
    wide = ",".join(["0.00"] * 104)
    out = render([ok(stdout=wide + "\n")], max_cols=120)
    assert "more chars on this line" in out
    assert len(max(out.splitlines(), key=len)) < 200


# ----------------------------------------------------------- refused vs unreachable


def refused(name="a", why="size limit"):
    return Result(host=host(name), rc=0, refused=why)


def test_refusal_is_not_the_same_as_unreachable():
    # The host is fine and uts declined; that must not share an exit code with
    # "cannot connect"
    assert exit_code([refused()]) == EXIT_BLOCKED
    assert exit_code([dead()]) == EXIT_ALL_FAILED


def test_partial_refusal_reports_partial():
    assert exit_code([ok(), refused("b")]) == EXIT_PARTIAL


def test_refusal_shows_up_in_json():
    payload = json.loads(to_json([refused(why="too big")], "pull"))["hosts"]
    assert payload[0]["refused"] == "too big"
    assert payload[0]["reachable"] is True


# ------------------------------------------------------------------- the envelope


def test_every_json_answer_carries_the_same_four_keys():
    payload = json.loads(to_json([ok()], "exec"))
    assert payload["uts"] == CONTRACT
    assert payload["command"] == "exec"
    assert payload["ok"] is True
    assert payload["exit"] == EXIT_OK


def test_ok_tracks_the_exit_code_not_reachability():
    # A host answered and the remote command failed: uts worked, the request did not.
    payload = json.loads(to_json([ok(rc=1)], "exec"))
    assert payload["ok"] is False and payload["exit"] == EXIT_REMOTE_NONZERO


def test_the_envelope_reports_the_code_it_was_given():
    # pull/push downgrade partial to OK under --dry-run, and the envelope has to say
    # what the caller will actually see rather than recomputing it.
    payload = json.loads(to_json([ok(), dead()], "pull", code=EXIT_OK))
    assert payload["exit"] == EXIT_OK and payload["ok"] is True


def test_hosts_are_unchanged_by_the_wrapping():
    results = [ok(stdout="hi\n"), dead()]
    assert json.loads(to_json(results, "exec"))["hosts"] == host_items(results)


# ----------------------------------------------------------------- error envelope


def test_a_blocked_request_is_still_json_on_stdout(capsys):
    # The whole point: exit 4 used to mean empty stdout and prose on stderr, which
    # left a program with nothing to read.
    code = fail("rm is destructive", EXIT_BLOCKED, True, command="exec", kind="blocked")
    out = capsys.readouterr()
    assert code == EXIT_BLOCKED
    assert out.err == ""
    payload = json.loads(out.out)
    assert payload["ok"] is False and payload["exit"] == EXIT_BLOCKED
    assert payload["error"] == {"kind": "blocked", "message": "rm is destructive"}
    assert payload["hosts"] == []


def test_text_mode_failures_still_read_like_prose_on_stderr(capsys):
    code = fail("no such host", EXIT_ALL_FAILED, False, command="status", kind="inventory")
    out = capsys.readouterr()
    assert code == EXIT_ALL_FAILED
    assert out.out == ""
    assert out.err.strip() == "no such host"


def test_error_and_success_envelopes_share_their_shape():
    good = set(json.loads(to_json([ok()], "exec")))
    bad = set(json.loads(envelope("exec", EXIT_BLOCKED, [], error={"kind": "usage",
                                                                  "message": "x"})))
    assert good < bad and bad - good == {"error"}


def test_extra_fields_ride_alongside_the_hosts_list():
    payload = json.loads(envelope("index", EXIT_OK, [], pulled=3, pushed=1))
    assert payload["pulled"] == 3 and payload["pushed"] == 1 and payload["hosts"] == []
