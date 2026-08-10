"""Detached jobs: the launcher that is built, and the listing that is read back."""

import pytest

from uts import remote
from uts.cli import build_parser, split_exec_argv
from uts.commands.jobs import _elapsed, _render_table, _state
from uts.conn import Result
from uts.inventory import Host


def hoist(args):
    return split_exec_argv(
        build_parser().parse_args(args).argv, separator_used="--" in args
    )[0]


def host(name="a"):
    return Host(name=name, ip="10.0.0.1", user="u", password="p")


# ------------------------------------------------------------------ flag position


def test_detach_after_the_selector_is_hoisted_not_sent_remote():
    assert hoist(["exec", "test", "--detach", "--", "python", "train.py"]) == {"--detach": True}


def test_detach_and_session_hoist_together():
    assert hoist(["exec", "test", "--detach", "--session", "s", "--", "make"]) == {
        "--detach": True, "--session": "s",
    }


# ------------------------------------------------------------------ the launcher


def test_the_job_records_its_own_pid_not_the_launchers():
    # setsid forks here, so `$!` would be setsid's pid and every later kill -0 would
    # be asking about the wrong process.
    script = remote.start_job("abc123", "sleep 10")
    assert "echo $$ > \"$HOME/.uts/jobs/abc123\"/pid" in script
    assert "$!" not in script


def test_setsid_has_a_fallback():
    script = remote.start_job("abc123", "sleep 10")
    assert "command -v setsid" in script and "nohup" in script


def test_a_terminated_job_still_records_an_exit_code():
    # Otherwise "I killed it" and "the host rebooted" look identical afterwards.
    # The launcher is shlex-quoted whole, so this only checks the pieces survived;
    # that the trap really fires is a live test.
    script = remote.start_job("abc123", "sleep 10")
    assert "trap" in script and "143" in script and "TERM" in script


def test_the_command_is_quoted_into_the_launcher():
    script = remote.start_job("abc123", "echo 'a b' | wc -l")
    assert "'echo '\"'\"'a b'\"'\"' | wc -l'" in script


def test_stdin_is_detached_so_the_job_outlives_the_channel():
    assert "< /dev/null &" in remote.start_job("abc123", "sleep 10")


def test_session_state_is_replayed_into_the_job():
    script = remote.start_job("abc123", "python train.py", prefix=["cd /srv", "export V=1"])
    assert "cd /srv" in script and "export V=1" in script


# ------------------------------------------------------------------ job ids


@pytest.mark.parametrize("bad", ["x; rm -rf /", "../../etc", "a b", "", "$(whoami)"])
def test_job_ids_that_are_not_job_ids_are_rejected(bad):
    # The id is interpolated into a path inside a shell snippet, so shape is checked
    # rather than trusted.
    with pytest.raises(remote.PathSpecError):
        remote.check_job_id(bad)


def test_a_real_job_id_passes():
    assert remote.check_job_id("7f3c1a") == "7f3c1a"


# ------------------------------------------------------------------ the listing


def test_listing_parses_into_clock_and_jobs():
    now, jobs = remote.parse_jobs(
        "now\t1000\n"
        "job\t7f3c1a\trunning\t900\t4821\tpython train.py\n"
        "job\t9d22e0\texited:0\t800\t1300\tmake all\n"
    )
    assert now == 1000
    assert [j["id"] for j in jobs] == ["7f3c1a", "9d22e0"]
    assert jobs[0]["state"] == "running" and jobs[0]["pid"] == "4821"


def test_lines_that_are_not_job_records_are_ignored():
    # A login banner or a stray warning must not become a phantom job.
    _, jobs = remote.parse_jobs("Welcome to Ubuntu\nnow\t10\njob\tbroken\n")
    assert jobs == []


def test_state_words_distinguish_the_three_endings():
    assert _state("running") == "running"
    assert _state("exited:0") == "exited(0)"
    assert _state("exited:2") == "exited(2)"
    assert _state("exited:143") == "killed"
    assert _state("vanished") == "vanished"


@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (45, "45s"), (60, "1m"), (1500, "25m"), (3600, "1h00m"),
    (7860, "2h11m"), (86400, "1d00h"), (200000, "2d07h"),
])
def test_elapsed_stays_readable_at_every_scale(seconds, expected):
    assert _elapsed(1_000_000 + seconds, 1_000_000) == expected


def test_elapsed_is_measured_on_the_remote_clock():
    # Local time never enters it: clock skew across machines is real, and this tool
    # already warns about it in ping.
    assert _elapsed(0, 500) == "?"


def test_table_lists_jobs_from_every_host():
    results = [
        Result(host=host("a"), extra={"now": 1000, "jobs": [
            {"id": "7f3c1a", "state": "running", "started": 940, "pid": "1", "command": "make"},
        ]}),
        Result(host=host("b"), extra={"now": 1000, "jobs": [
            {"id": "9d22e0", "state": "exited:0", "started": 400, "pid": "2", "command": "ls"},
        ]}),
    ]
    table = _render_table(results)
    assert "7f3c1a" in table and "9d22e0" in table
    assert "1m" in table and "10m" in table


def test_an_unreachable_host_is_a_row_not_a_silence():
    results = [Result(host=host("a"), error="cannot reach 10.0.0.1:22")]
    assert "UNREACHABLE" in _render_table(results)


def test_no_jobs_says_how_to_start_one():
    assert "--detach" in _render_table([Result(host=host("a"), extra={"now": 1, "jobs": []})])
