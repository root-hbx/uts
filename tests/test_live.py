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


def test_path_spec_injection_is_blocked_before_any_connection(capsys):
    from uts.output import EXIT_BLOCKED

    assert main(["ls", "test", "~/data; rm -rf /"]) == EXIT_BLOCKED
    assert "not allowed in a path" in capsys.readouterr().err
