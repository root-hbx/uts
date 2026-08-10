"""Named sessions: the wrapper built around a command, and the state read back.

The adversarial case is the one worth having: the trailer is appended to the
command's own stdout, so anything that decides where the command's output ends and
the state report begins has to survive output that looks like a state report.
"""

import pytest

from uts import remote
from uts.cli import UsageError, build_parser, split_exec_argv
from uts.session import Session, env_delta


def hoist(args):
    return split_exec_argv(
        build_parser().parse_args(args).argv, separator_used="--" in args
    )[0]


def wrap_and_parse(command, cwd=None, env=None, tail="/srv\036HOME=/root\0"):
    """Build the wrapper, then feed it a fabricated reply as if the host had run it."""
    nonce = "abc123"
    wrapped = remote.session_wrap(command, cwd, env or {}, nonce)
    sentinel = remote.SESSION_SENTINEL.format(nonce=nonce)
    return wrapped, remote.parse_session_trailer(f"output\n\n{sentinel}\n{tail}", nonce)


# ------------------------------------------------------------------ flag position


def test_session_after_the_selector_is_hoisted_not_sent_remote():
    # Same trap --write fell into: REMAINDER takes flags too, so `--session s1`
    # would otherwise become the first two words of the remote command.
    assert hoist(["exec", "test", "--session", "s1", "--", "pwd"]) == {"--session": "s1"}


def test_session_accepts_the_equals_form():
    assert hoist(["exec", "test", "--session=s1", "--", "pwd"]) == {"--session": "s1"}


def test_session_before_the_selector_is_argparse_business():
    assert build_parser().parse_args(["exec", "--session", "s1", "test", "--", "pwd"]).session == "s1"


def test_session_and_write_hoist_together():
    assert hoist(["exec", "test", "--write", "--session", "s1", "--", "rm", "x"]) == {
        "--write": True, "--session": "s1",
    }


def test_session_after_the_separator_belongs_to_the_remote_command():
    hoisted, command = split_exec_argv(["--", "mytool", "--session", "x"], separator_used=True)
    assert hoisted == {}
    assert command == "mytool --session x"


def test_dangling_session_flag_is_a_usage_error():
    with pytest.raises(UsageError, match="needs a value"):
        split_exec_argv(["--session"])


# ------------------------------------------------------------------ the wrapper


def test_saved_cwd_is_replayed_and_quoted():
    wrapped, _ = wrap_and_parse("pwd", cwd="/home/a b")
    assert "cd '/home/a b' 2>/dev/null" in wrapped


def test_a_deleted_cwd_falls_back_to_home_instead_of_failing():
    wrapped, _ = wrap_and_parse("pwd", cwd="/gone")
    assert 'cd /gone 2>/dev/null || cd "$HOME"' in wrapped


def test_saved_env_is_replayed_as_quoted_exports():
    wrapped, _ = wrap_and_parse("true", env={"V": "a b", "W": "$x"})
    assert "export V='a b'" in wrapped
    assert "export W='$x'" in wrapped


def test_the_users_rc_survives_the_trailer():
    wrapped, _ = wrap_and_parse("false")
    assert "__uts_rc=$?" in wrapped
    assert wrapped.rstrip().endswith("exit $__uts_rc")


def test_no_session_means_the_command_string_is_untouched():
    # The whole point of opt-in: without --session nothing wraps anything.
    _, command = split_exec_argv(["--", "echo", "hi"], separator_used=True)
    assert command == "echo hi"


# ------------------------------------------------------------------ reading it back


def test_state_is_read_back_from_the_trailer():
    _, (body, cwd, env) = wrap_and_parse("echo output", tail="/srv/app\036A=1\0B=two\0")
    assert body == "output\n"
    assert cwd == "/srv/app"
    assert env == {"A": "1", "B": "two"}


def test_output_that_looks_like_a_trailer_does_not_win():
    # `cat` a previous uts transcript and the sentinel appears in the command's own
    # output. Splitting on the last occurrence is what keeps that harmless.
    nonce = "abc123"
    sentinel = remote.SESSION_SENTINEL.format(nonce=nonce)
    stdout = f"replaying: {sentinel}\n/wrong\036A=bad\0\n{sentinel}\n/right\036A=good\0"
    body, cwd, env = remote.parse_session_trailer(stdout, nonce)
    assert cwd == "/right"
    assert env == {"A": "good"}
    assert "replaying" in body


def test_a_different_nonce_is_not_our_trailer():
    stdout = "x\n" + remote.SESSION_SENTINEL.format(nonce="other") + "\n/etc\036A=1\0"
    body, cwd, _ = remote.parse_session_trailer(stdout, "abc123")
    assert cwd is None
    assert body == stdout          # nothing was stripped from the user's output


def test_missing_trailer_leaves_the_state_untouched():
    # What a truncated output looks like: the command ran, the report never arrived.
    body, cwd, env = remote.parse_session_trailer("just output, cut short", "abc123")
    assert (cwd, env) == (None, {})
    assert body == "just output, cut short"


def test_env_values_containing_newlines_survive():
    _, (_, _, env) = wrap_and_parse("true", tail="/srv\036MULTI=one\ntwo\0NEXT=3\0")
    assert env == {"MULTI": "one\ntwo", "NEXT": "3"}


# ------------------------------------------------------------------ the env delta


def test_only_changed_variables_are_carried_forward():
    baseline = {"PATH": "/usr/bin", "HOME": "/home/a"}
    current = {"PATH": "/usr/bin", "HOME": "/home/a", "VIRTUAL_ENV": "/p/.venv"}
    assert env_delta(current, baseline) == {"VIRTUAL_ENV": "/p/.venv"}


def test_a_changed_value_counts_as_a_delta():
    assert env_delta({"PATH": "/new"}, {"PATH": "/old"}) == {"PATH": "/new"}


def test_connection_scoped_variables_are_never_replayed():
    # Replaying these would drag a dead connection's identity into the next one.
    current = {"SSH_CLIENT": "10.0.0.1", "SSH_TTY": "/dev/pts/0", "TERM": "xterm", "OK": "1"}
    assert env_delta(current, {}) == {"OK": "1"}


def test_per_process_noise_is_dropped():
    assert env_delta({"_": "/bin/ls", "SHLVL": "2", "PWD": "/tmp", "KEEP": "y"}, {}) == {"KEEP": "y"}


def test_exported_shell_functions_are_not_turned_into_exports():
    # bash names these `BASH_FUNC_foo%%`, which is not something we can `export`.
    assert env_delta({"BASH_FUNC_foo%%": "() { :; }", "REAL": "1"}, {}) == {"REAL": "1"}


# ------------------------------------------------------------------ the store


def test_state_is_kept_per_host(tmp_path):
    s = Session("build", tmp_path)
    s.update("a", "/srv/a", {"V": "1"}, {})
    s.update("b", "/srv/b", {}, {})
    assert s.cwd("a") == "/srv/a" and s.cwd("b") == "/srv/b"
    assert s.env("a") == {"V": "1"} and s.env("b") == {}


def test_state_survives_a_new_process(tmp_path):
    Session("build", tmp_path).update("a", "/srv", {"V": "1"}, {"PATH": "/usr/bin"})
    reloaded = Session("build", tmp_path)
    assert reloaded.cwd("a") == "/srv"
    assert reloaded.baseline("a") == {"PATH": "/usr/bin"}


def test_update_reports_what_changed(tmp_path):
    s = Session("build", tmp_path)
    s.update("a", "/one", {"KEEP": "1", "GONE": "1"}, {})
    changed = s.update("a", "/two", {"KEEP": "1", "NEW": "1"}, {})
    assert changed["cwd"] == "/two"
    assert changed["env_added"] == ["NEW"]
    assert changed["env_dropped"] == ["GONE"]


def test_an_unchanged_cwd_is_not_reported_as_a_change(tmp_path):
    s = Session("build", tmp_path)
    s.update("a", "/one", {}, {})
    assert s.update("a", "/one", {}, {})["cwd"] is None


def test_a_corrupt_session_file_starts_over_rather_than_failing(tmp_path):
    # Losing the state is recoverable; making every host unreachable is not.
    s = Session("build", tmp_path)
    s.update("a", "/srv", {}, {})
    s.path.write_text("{ not json", encoding="utf-8")
    assert Session("build", tmp_path).cwd("a") is None


def test_unknown_session_is_simply_empty(tmp_path):
    assert Session("never-used", tmp_path).hosts() == []
