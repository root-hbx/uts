"""Smoke tests against a real host. Skipped by default: `uv run pytest -m live`.

They cover what unit tests cannot reach: that it really connects, that output
really is capped while reading, and that failures really produce a distinguishable
reason.

Environment-specific by design. The target is the host named `test` in your
hosts.json, and the assertions below reference a dataset that exists on the
author's machine. Point DATA at a directory of your own to run these.
"""

import json

import pytest

from uts.cli import main
from uts.conn import Limits, run_command
from uts.inventory import Host, load_inventory, select
from uts.output import EXIT_OK, EXIT_REMOTE_NONZERO

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def target() -> list[Host]:
    return select(load_inventory(), "test")


def run(hosts, command, **kw):
    return run_command(hosts, command, Limits(**kw))


def test_reachable_and_is_linux(target):
    (r,) = run(target, "uname -s")
    assert r.reachable, r.error
    assert r.rc == 0
    assert r.stdout.strip() == "Linux"


def test_remote_nonzero_rc_is_preserved(target):
    (r,) = run(target, "exit 42")
    assert r.rc == 42 and r.reachable


def test_stdout_and_stderr_stay_separate(target):
    (r,) = run(target, "echo out; echo err >&2")
    assert r.stdout.strip() == "out"
    assert r.stderr.strip() == "err"


def test_output_is_capped_while_reading(target):
    (r,) = run(target, "seq 1 100000", max_lines=10)
    assert r.stdout.splitlines() == [str(i) for i in range(1, 11)]
    assert r.truncated and r.dropped_lines == 99_990


def test_runaway_output_is_aborted_not_buffered(target):
    # Unbounded output must be cut off; without this one slip eats all local memory.
    (r,) = run(target, "cat /dev/urandom | base64", max_lines=5, timeout=60)
    assert r.aborted and r.reachable


def test_quoting_survives_the_round_trip(target):
    (r,) = run(target, "echo 'a  b' '$HOME' '*'")
    assert r.stdout.strip() == "a  b $HOME *"


def test_facts_probe_reports_a_clock_skew(target, capsys):
    assert main(["ping", "test"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "clock" in out and "OK" in out


def test_guard_blocks_without_touching_the_network(target, capsys):
    assert main(["exec", "test", "--", "rm", "-rf", "/tmp/nope"]) != EXIT_OK
    assert "blocked:" in capsys.readouterr().err


def test_wrong_password_reports_auth_failure(target, tmp_path):
    bad = [
        {"name": "wrongpw", "ip": target[0].ip, "user": target[0].user,
         "password": "definitely-not-it", "timeout": 6}
    ]
    p = tmp_path / "hosts.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    (r,) = run(select(load_inventory(p), "all"), "true")
    assert not r.reachable
    assert "auth failed" in (r.error or "")


def test_exit_codes_against_the_real_host(target, capsys):
    assert main(["exec", "test", "--", "true"]) == EXIT_OK
    assert main(["exec", "test", "--", "false"]) == EXIT_REMOTE_NONZERO
    capsys.readouterr()


# ------------------------------------------- browsing, shapes, and transfers

DATA = "~/starlink-10-10-550-53-grid-LeastDelay"


def test_ls_summarises_a_directory(target, capsys):
    assert main(["ls", "test", f"{DATA}/"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "files" in out and ".txt" in out


def test_glob_matching_works_not_just_directories(target, capsys):
    # Regression: GNU stat's -c does not expand \t, so the single-file/glob branch
    # parsed 0 files while the directory branch (find -printf) worked. Only a real
    # host exposes this.
    assert main(["ls", "test", f"{DATA}/*.txt"]) == EXIT_OK
    assert "120 files" in capsys.readouterr().out


def test_peek_finds_mixed_shapes(target, capsys):
    # This directory holds two runs: 110x110 (December, 10 ground stations) and
    # 104x104 (March, 4 of them)
    assert main(["peek", "test", f"{DATA}/*.txt"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "110 × 110" in out and "104 × 104" in out
    assert "shapes disagree" in out


def test_peek_folds_wide_sample_lines(target, capsys):
    main(["--max-cols", "80", "peek", "test", f"{DATA}/5.txt"])
    out = capsys.readouterr().out
    assert "more chars on this line" in out
    assert max(len(line) for line in out.splitlines()) < 200


def test_find_filters_by_size(target, capsys):
    assert main(["find", "test", f"{DATA}/", "--name", "*.txt",
                 "--min-size", "55K", "--limit", "3"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "matched 105" in out


def test_pull_round_trips_bytes_exactly(target, tmp_path, capsys):
    ws = tmp_path / "ws"
    assert main(["--workspace", str(ws), "pull", "test", f"{DATA}/595.txt"]) == EXIT_OK
    capsys.readouterr()

    local = ws / "test" / "home/bxhu/starlink-10-10-550-53-grid-LeastDelay/595.txt"
    assert local.exists()

    (remote_sum,) = run(target, "sha256sum ~/starlink-10-10-550-53-grid-LeastDelay/595.txt")
    import hashlib

    assert hashlib.sha256(local.read_bytes()).hexdigest() == remote_sum.stdout.split()[0]


def test_pull_writes_manifest_and_index(target, tmp_path, capsys):
    ws = tmp_path / "ws"
    main(["--workspace", str(ws), "pull", "test", f"{DATA}/5.txt"])
    capsys.readouterr()

    entry = json.loads((ws / "manifest.jsonl").read_text().strip())
    assert entry["host"] == "test"
    assert entry["remote_path"].endswith("/5.txt")
    assert entry["size"] == 60900 and entry["sha256"]
    assert "5.txt" in (ws / "INDEX.md").read_text()


def test_pull_refuses_oversized_transfer(target, tmp_path, capsys):
    from uts.output import EXIT_BLOCKED

    ws = tmp_path / "ws"
    code = main(["--workspace", str(ws), "pull", "test", f"{DATA}/*.txt", "--max-size", "1M"])
    assert code == EXIT_BLOCKED          # refused, not "unreachable"
    assert "exceed the" in capsys.readouterr().out
    assert not (ws / "manifest.jsonl").exists()   # a refusal must leave no trace


def test_pull_lines_mode_truncates(target, tmp_path, capsys):
    ws = tmp_path / "ws"
    main(["--workspace", str(ws), "pull", "test", f"{DATA}/1.txt", "--lines", "3"])
    capsys.readouterr()
    local = ws / "test" / "home/bxhu/starlink-10-10-550-53-grid-LeastDelay/1.txt"
    assert len(local.read_text().splitlines()) == 3
    assert json.loads((ws / "manifest.jsonl").read_text().strip())["truncated"] is True


# ------------------------------------------------------------------ push

PROBE = "~/uts-live-probe"


@pytest.fixture
def probe_dir(target):
    """A scratch directory on the remote host, gone again whichever way the test ends."""
    run(target, f"rm -rf {PROBE}")
    yield PROBE
    run(target, f"rm -rf {PROBE}")


def test_push_round_trips_bytes_exactly(target, tmp_path, probe_dir, capsys):
    import hashlib

    src = tmp_path / "payload.txt"
    src.write_bytes(b"line one\nline two\n\xc3\xa9 non-ascii\n")

    assert main(["--workspace", str(tmp_path / "ws"), "push", "test",
                 str(src), f"{probe_dir}/"]) == EXIT_OK
    capsys.readouterr()

    (r,) = run(target, f"sha256sum {probe_dir}/payload.txt")
    assert r.stdout.split()[0] == hashlib.sha256(src.read_bytes()).hexdigest()


def test_push_reproduces_a_directory_by_its_own_name(target, tmp_path, probe_dir, capsys):
    lib = tmp_path / "lib"
    (lib / "nested").mkdir(parents=True)
    (lib / "a.py").write_text("a = 1\n")
    (lib / "nested" / "b.py").write_text("b = 2\n")

    main(["--workspace", str(tmp_path / "ws"), "push", "test", str(lib), f"{probe_dir}/"])
    capsys.readouterr()

    (r,) = run(target, f"find {probe_dir} -type f | sort")
    assert r.stdout.split() == [
        "/home/bxhu/uts-live-probe/lib/a.py",
        "/home/bxhu/uts-live-probe/lib/nested/b.py",
    ]


def test_push_refuses_to_overwrite_until_forced(target, tmp_path, probe_dir, capsys):
    from uts.output import EXIT_BLOCKED

    src = tmp_path / "once.txt"
    src.write_text("first\n")
    ws = str(tmp_path / "ws")

    assert main(["--workspace", ws, "push", "test", str(src), f"{probe_dir}/"]) == EXIT_OK
    capsys.readouterr()

    src.write_text("second\n")
    assert main(["--workspace", ws, "push", "test", str(src), f"{probe_dir}/"]) == EXIT_BLOCKED
    assert "--force" in capsys.readouterr().out
    (r,) = run(target, f"cat {probe_dir}/once.txt")
    assert r.stdout == "first\n"          # the refusal really left it alone

    assert main(["--workspace", ws, "push", "test", str(src),
                 f"{probe_dir}/", "--force"]) == EXIT_OK
    capsys.readouterr()
    (r,) = run(target, f"cat {probe_dir}/once.txt")
    assert r.stdout == "second\n"


def test_push_dry_run_sends_nothing(target, tmp_path, probe_dir, capsys):
    src = tmp_path / "ghost.txt"
    src.write_text("should not arrive\n")
    ws = tmp_path / "ws"

    assert main(["--workspace", str(ws), "push", "test", str(src),
                 f"{probe_dir}/", "--dry-run"]) == EXIT_OK
    assert "dry-run" in capsys.readouterr().out

    (r,) = run(target, f"test -e {probe_dir}/ghost.txt; echo $?")
    assert r.stdout.strip() == "1"
    assert not (ws / "manifest.jsonl").exists()


def test_push_records_provenance_in_the_manifest(target, tmp_path, probe_dir, capsys):
    src = tmp_path / "recorded.txt"
    src.write_text("x\n")
    ws = tmp_path / "ws"

    main(["--workspace", str(ws), "push", "test", str(src), f"{probe_dir}/"])
    capsys.readouterr()

    entry = json.loads((ws / "manifest.jsonl").read_text().strip())
    assert entry["direction"] == "push"
    assert entry["remote_path"] == "/home/bxhu/uts-live-probe/recorded.txt"
    assert entry["local_path"] == str(src)
    assert "sent to it" in (ws / "INDEX.md").read_text()


# ------------------------------------------------------------------ sessions


def test_session_carries_cwd_and_exports_forward(target, tmp_path, capsys):
    ws = str(tmp_path / "ws")

    assert main(["--workspace", ws, "exec", "test", "--session", "s",
                 "export MY_VAR=hello; cd /tmp"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "cwd → /tmp" in out and "env +MY_VAR" in out

    assert main(["--workspace", ws, "exec", "test", "--session", "s",
                 'echo "$MY_VAR from $(pwd)"']) == EXIT_OK
    out = capsys.readouterr().out
    assert "hello from /tmp" in out
    assert "cwd /tmp" in out           # the header says where the command ran


def test_a_command_without_session_is_unaffected_by_one(target, tmp_path, capsys):
    ws = str(tmp_path / "ws")
    main(["--workspace", ws, "exec", "test", "--session", "s", "cd /tmp"])
    capsys.readouterr()

    assert main(["--workspace", ws, "exec", "test", "pwd"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "/home/bxhu" in out and "/tmp" not in out


def test_the_trailer_never_leaks_into_the_output(target, tmp_path, capsys):
    ws = str(tmp_path / "ws")
    main(["--workspace", ws, "exec", "test", "--session", "s", "echo just-this"])
    out = capsys.readouterr().out
    assert "uts-session" not in out
    assert "env -0" not in out


def test_truncated_output_says_the_session_did_not_move(target, tmp_path, capsys):
    # The trailer is appended to stdout, so a command that fills the cap takes the
    # state report down with it. Silence here would leave the agent believing a `cd`
    # landed when it did not.
    ws = str(tmp_path / "ws")
    main(["--max-lines", "5", "--workspace", ws, "exec", "test", "--session", "s",
          "cd /tmp && seq 1 100000"])
    assert "session unchanged" in capsys.readouterr().out

    main(["--workspace", ws, "exec", "test", "--session", "s", "pwd"])
    assert "/home/bxhu" in capsys.readouterr().out


def test_session_survives_across_processes(target, tmp_path, capsys):
    ws = str(tmp_path / "ws")
    main(["--workspace", ws, "exec", "test", "--session", "keep", "cd /etc"])
    capsys.readouterr()

    from uts.session import Session

    assert Session("keep", ws).cwd("test") == "/etc"


# ------------------------------------------------------------------ detached jobs


@pytest.fixture
def no_jobs(target):
    """Start and end with a clean job list, whichever way the test goes."""
    main(["jobs", "test", "--clean"])
    yield
    main(["jobs", "test", "--clean"])


def started_job_id(out: str) -> str:
    return out.split("· job ")[1].split(" ")[0]


def test_a_detached_job_outlives_the_connection(target, no_jobs, capsys):
    import time

    assert main(["exec", "test", "--detach", "--", "sh", "-c",
                 "sleep 2; echo finished-later"]) == EXIT_OK
    job = started_job_id(capsys.readouterr().out)

    main(["jobs", "test"])
    assert "running" in capsys.readouterr().out

    time.sleep(4)                          # the SSH channel is long gone by now
    main(["jobs", "test"])
    assert "exited(0)" in capsys.readouterr().out
    main(["logs", "test", job])
    assert "finished-later" in capsys.readouterr().out


def test_a_failing_job_keeps_its_exit_code(target, no_jobs, capsys):
    import time

    main(["exec", "test", "--detach", "--", "sh", "-c", "exit 7"])
    capsys.readouterr()
    time.sleep(2)
    main(["jobs", "test"])
    assert "exited(7)" in capsys.readouterr().out


def test_killing_a_job_is_distinguishable_from_it_vanishing(target, no_jobs, capsys):
    import time

    main(["exec", "test", "--detach", "--", "sleep", "120"])
    job = started_job_id(capsys.readouterr().out)
    time.sleep(1)

    assert main(["kill", "test", job]) == EXIT_OK
    assert "SIGTERM" in capsys.readouterr().out
    time.sleep(1)

    main(["jobs", "test"])
    assert "killed" in capsys.readouterr().out


def test_kill_takes_down_the_whole_process_group(target, no_jobs, capsys):
    import time

    main(["exec", "test", "--detach", "--", "sh", "-c", "sleep 300 | cat"])
    job = started_job_id(capsys.readouterr().out)
    time.sleep(1)
    main(["kill", "test", job])
    capsys.readouterr()
    time.sleep(1)

    # The `sleep 300` is a child, not the job leader: signalling only the leader
    # would leave it running.
    (r,) = run(target, "pgrep -c 'sleep 300'")
    assert r.stdout.strip() in ("", "0")


def test_a_detached_job_inherits_the_sessions_cwd(target, tmp_path, no_jobs, capsys):
    import time

    ws = str(tmp_path / "ws")
    main(["--workspace", ws, "exec", "test", "--session", "j", "cd /tmp"])
    capsys.readouterr()

    main(["--workspace", ws, "exec", "test", "--session", "j", "--detach", "--", "pwd"])
    job = started_job_id(capsys.readouterr().out)
    time.sleep(2)
    main(["logs", "test", job])
    assert "/tmp" in capsys.readouterr().out


def test_clean_removes_finished_jobs_but_not_running_ones(target, no_jobs, capsys):
    import time

    main(["exec", "test", "--detach", "--", "true"])
    capsys.readouterr()
    main(["exec", "test", "--detach", "--", "sleep", "120"])
    keep = started_job_id(capsys.readouterr().out)
    time.sleep(2)

    main(["jobs", "test", "--clean"])
    assert "removed 1 finished job" in capsys.readouterr().out
    main(["jobs", "test"])
    out = capsys.readouterr().out
    assert keep in out and out.count("\n") == 2      # header plus the survivor

    main(["kill", "test", keep])
    capsys.readouterr()


def test_the_job_footprint_is_confined_and_removable(target, no_jobs, capsys):
    import time

    main(["exec", "test", "--detach", "--", "true"])
    capsys.readouterr()
    time.sleep(1)

    (r,) = run(target, "ls -d ~/.uts/jobs/*/ 2>/dev/null | wc -l")
    assert r.stdout.strip() == "1"

    main(["jobs", "test", "--clean"])
    capsys.readouterr()
    (r,) = run(target, "ls -A ~/.uts/jobs 2>/dev/null | wc -l")
    assert r.stdout.strip() == "0"


# ------------------------------------------------------------------ pty


def test_pty_gives_the_remote_side_a_real_terminal(target, capsys):
    assert main(["exec", "test", "--pty", "tty; echo TERM=$TERM"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "/dev/pts/" in out
    assert "TERM=xterm-256color" in out


def test_without_pty_there_is_still_no_terminal(target, capsys):
    # The default path must not have quietly changed.
    main(["exec", "test", "tty"])
    assert "not a tty" in capsys.readouterr().out


def test_pty_says_in_the_header_that_stderr_was_merged(target, capsys):
    # Silence here would let an empty stderr read as "nothing went wrong".
    main(["exec", "test", "--pty", "echo err >&2"])
    out = capsys.readouterr().out
    assert "stderr merged into stdout" in out
    assert "err" in out


def test_pty_output_has_no_escape_sequences_left(target, capsys):
    main(["exec", "test", "--pty", "ls --color=always /etc | head -5"])
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\r" not in out


def test_pty_preserves_the_exit_code(target, capsys):
    assert main(["exec", "test", "--pty", "exit 9"]) == EXIT_REMOTE_NONZERO
    assert "rc=9" in capsys.readouterr().out


def test_a_full_screen_program_comes_back_as_a_readable_frame(target, capsys):
    # The original "uts cannot do this": btop refuses to start without a terminal,
    # and even with one it paints rather than prints.
    code = main(["--max-cols", "0", "exec", "test", "--pty", "--duration", "3", "--", "btop"])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "No tty detected" not in out
    assert "Load AVG" in out              # a label only the drawn screen has
    assert "cpu" in out and "mem" in out
    assert "\x1b" not in out


def test_detach_and_pty_together_are_refused(target, capsys):
    from uts.output import EXIT_BLOCKED

    assert main(["exec", "test", "--pty", "--detach", "--", "btop"]) == EXIT_BLOCKED
    assert "cannot be combined" in capsys.readouterr().err


def test_pty_runs_inside_a_session(target, tmp_path, capsys):
    ws = str(tmp_path / "ws")
    main(["--workspace", ws, "exec", "test", "--session", "p", "cd /etc"])
    capsys.readouterr()
    main(["--workspace", ws, "exec", "test", "--session", "p", "--pty", "pwd"])
    assert "/etc" in capsys.readouterr().out


# ------------------------------------------------------------------ interactive shell


def test_shell_is_a_real_interactive_terminal(target):
    """Drive `uts shell` through a pty, the way a person's terminal would.

    Nothing else covers this: the command refuses to run without a terminal, which
    is precisely what a test runner does not have. So one is made.
    """
    import os
    import pty
    import select
    import subprocess
    import time

    # openpty + subprocess rather than pty.fork(): by this point the fan-out tests
    # have left threads in this process, and forking one of those can deadlock the
    # child before it reaches exec.
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["./uts", "shell", "test"],
        stdin=slave, stdout=slave, stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"},
        start_new_session=True,
    )
    os.close(slave)

    typed = [(2.5, b"echo INTERACTIVE-OK\n"), (1.5, b"exit\n")]
    out, step = b"", 0
    next_at = time.monotonic() + typed[0][0]
    deadline = time.monotonic() + 25
    try:
        while time.monotonic() < deadline and proc.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:                     # the pty closed with the session
                    break
                if not chunk:
                    break
                out += chunk
            if step < len(typed) and time.monotonic() >= next_at:
                os.write(master, typed[step][1])
                step += 1
                if step < len(typed):
                    next_at = time.monotonic() + typed[step][0]
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    text = out.decode("utf-8", "replace")
    assert "uts: connected to" in text
    assert "INTERACTIVE-OK" in text                 # the remote shell ran what we typed
    assert proc.returncode == 0


def test_path_spec_injection_is_blocked_before_any_connection(capsys):
    from uts.output import EXIT_BLOCKED

    assert main(["ls", "test", "~/data; rm -rf /"]) == EXIT_BLOCKED
    assert "not allowed in a path" in capsys.readouterr().err
