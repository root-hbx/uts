"""Background sessions: the launcher that is built, and the table that is read back.

The merge these pin: one name selects both the environment a command runs in and
the background process running in it. What used to be a generated job id (`7f3c1a`)
and a separate `--session build` is now a single handle.
"""

import pytest

from uts import remote
from uts.cli import build_parser, one_command
from uts.commands.sessions import _elapsed, _render_table, _state
from uts.conn import Result
from uts.inventory import Host
from uts.session import Session


def host(name="a"):
    return Host(name=name, ip="10.0.0.1", user="u", password="p")


def parse(args):
    return build_parser().parse_args(args)


# ------------------------------------------------------------------ argv shape


def test_start_names_the_session_and_keeps_the_command_intact():
    ns = parse(["start", "-H", "t", "-s", "train", "python train.py --epochs 100"])
    assert ns.session == "train"
    assert one_command(ns.command_, "start") == "python train.py --epochs 100"


def test_start_takes_the_command_the_same_way_exec_does():
    # One rule across both, which is the whole point of the single positional:
    # --epochs is the remote program's because it is inside the quotes, and
    # --force is uts's because it is not.
    ns = parse(["start", "-H", "t", "-s", "train", "python train.py --epochs 100", "--force"])
    assert ns.force is True
    assert one_command(ns.command_, "start") == "python train.py --epochs 100"


def test_start_without_a_name_is_refused():
    # The name is the handle for logs and stop; there is nothing to fall back to.
    with pytest.raises(SystemExit):
        parse(["start", "-H", "t", "python train.py"])


@pytest.mark.parametrize("command", ["logs", "stop"])
def test_logs_and_stop_address_a_session_not_an_id(command):
    assert parse([command, "-H", "t", "-s", "train"]).session == "train"
    with pytest.raises(SystemExit):
        parse([command, "-H", "t"])


def test_exec_can_no_longer_detach():
    # `uts exec --detach` became `uts start`, so the flag has to fail loudly rather
    # than land in the remote command.
    with pytest.raises(SystemExit):
        parse(["exec", "-H", "t", "--detach", "--", "python", "train.py"])


# ------------------------------------------------------------------ the launcher


def test_the_job_records_its_own_pid_not_the_launchers():
    # setsid forks here, so `$!` would be setsid's pid and every later kill -0 would
    # be asking about the wrong process.
    script = remote.start_job("train", "sleep 10")
    assert "echo $$ > \"$HOME/.uts/jobs/train\"/pid" in script
    assert "$!" not in script


def test_setsid_has_a_fallback():
    script = remote.start_job("train", "sleep 10")
    assert "command -v setsid" in script and "nohup" in script


def test_a_terminated_job_still_records_an_exit_code():
    # Otherwise "I killed it" and "the host rebooted" look identical afterwards.
    # The launcher is shlex-quoted whole, so this only checks the pieces survived;
    # that the trap really fires is a live test.
    script = remote.start_job("train", "sleep 10")
    assert "trap" in script and "143" in script and "TERM" in script


def test_the_command_is_quoted_into_the_launcher():
    script = remote.start_job("train", "echo 'a b' | wc -l")
    assert "'echo '\"'\"'a b'\"'\"' | wc -l'" in script


def test_stdin_is_detached_so_the_job_outlives_the_channel():
    assert "< /dev/null &" in remote.start_job("train", "sleep 10")


def test_session_state_is_replayed_into_the_job():
    script = remote.start_job("train", "python train.py", prefix=["cd /srv", "export V=1"])
    assert "cd /srv" in script and "export V=1" in script


def test_a_running_session_is_never_started_over():
    # Checked in the same round trip that would start it, so nothing races.
    script = remote.start_job("train", "sleep 10")
    assert "kill -0" in script and "busy" in script


def test_a_finished_session_needs_force_before_its_log_is_discarded():
    assert "finished" in remote.start_job("train", "x") and "rm -rf" not in remote.start_job("train", "x")
    assert "rm -rf" in remote.start_job("train", "x", force=True)


# ------------------------------------------------------------------ session names


@pytest.mark.parametrize("bad", ["x; rm -rf /", "../../etc", "a b", "", "$(whoami)", ".hidden"])
def test_names_that_would_escape_the_jobs_directory_are_rejected(bad):
    # The name is the user's now and it is interpolated into a path inside a shell
    # snippet, so its shape is checked rather than trusted.
    with pytest.raises(remote.PathSpecError):
        remote.check_session_name(bad)


@pytest.mark.parametrize("good", ["train", "build-2", "eval.v3", "a", "A_b1"])
def test_readable_names_are_allowed(good):
    assert remote.check_session_name(good) == good


# ------------------------------------------------------------------ the listing


def test_listing_parses_into_clock_and_jobs():
    now, jobs = remote.parse_jobs(
        "now\t1000\n"
        "job\ttrain\trunning\t900\t4821\tpython train.py\n"
        "job\teval\texited:0\t800\t1300\tmake all\n"
    )
    assert now == 1000
    assert [j["id"] for j in jobs] == ["train", "eval"]
    assert jobs[0]["state"] == "running" and jobs[0]["pid"] == "4821"


def test_lines_that_are_not_job_records_are_ignored():
    # A login banner or a stray warning must not become a phantom session.
    _, jobs = remote.parse_jobs("Welcome to Ubuntu\nnow\t10\njob\tbroken\n")
    assert jobs == []


def test_clean_reports_what_it_removed_and_what_it_left():
    cleaned, busy = remote.parse_clean("cleaned\teval\ncleaned\told\nbusy\ttrain\n")
    assert cleaned == ["eval", "old"]
    assert busy == ["train"]


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
    # already warns about it in status.
    assert _elapsed(0, 500) == "?"


# ------------------------------------------------------------------ the table


def test_table_lists_sessions_from_every_host():
    results = [
        Result(host=host("a"), extra={"now": 1000, "jobs": [
            {"id": "train", "state": "running", "started": 940, "pid": "1", "command": "make"},
        ]}),
        Result(host=host("b"), extra={"now": 1000, "jobs": [
            {"id": "eval", "state": "exited:0", "started": 400, "pid": "2", "command": "ls"},
        ]}),
    ]
    table = _render_table(results)
    assert "train" in table and "eval" in table
    assert "1m" in table and "10m" in table


def test_a_session_with_state_but_nothing_running_shows_as_idle():
    # The half that `uts sessions` used to print on its own.
    results = [Result(host=host("a"), extra={
        "now": 1000, "jobs": [], "idle": ["build"], "cwds": {"build": "/srv/proj"},
    })]
    table = _render_table(results)
    assert "build" in table and "idle" in table and "/srv/proj" in table


def test_a_running_session_shows_the_cwd_it_inherited():
    results = [Result(host=host("a"), extra={
        "now": 1000,
        "jobs": [{"id": "train", "state": "running", "started": 900,
                  "pid": "1", "command": "python train.py"}],
        "idle": [],
        "cwds": {"train": "/srv/proj"},
    })]
    assert "/srv/proj" in _render_table(results)


def test_an_unreachable_host_is_a_row_not_a_silence():
    results = [Result(host=host("a"), error="cannot reach 10.0.0.1:22")]
    assert "UNREACHABLE" in _render_table(results)


def test_nothing_at_all_says_how_to_start_something():
    empty = Result(host=host("a"), extra={"now": 1, "jobs": [], "idle": []})
    assert "uts start" in _render_table([empty])


# ------------------------------------------------------------------ forgetting


def test_clean_forgets_one_host_without_touching_the_others(tmp_path):
    # A session can be finished on one machine and still running on another.
    s = Session("train", tmp_path)
    s.update("a", "/srv/a", {}, {})
    s.update("b", "/srv/b", {}, {})
    Session("train", tmp_path).forget("a")
    reloaded = Session("train", tmp_path)
    assert reloaded.cwd("a") is None
    assert reloaded.cwd("b") == "/srv/b"


def test_forgetting_the_last_host_removes_the_file(tmp_path):
    s = Session("train", tmp_path)
    s.update("a", "/srv", {}, {})
    s.forget("a")
    assert not s.path.exists()
