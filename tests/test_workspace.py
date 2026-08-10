import json

from uts.output import human_bytes
from uts.workspace import Workspace


def test_local_path_mirrors_remote_path(tmp_path):
    ws = Workspace(tmp_path / ".uts")
    local = ws.path_for("test", "/home/ops/data/5.txt")
    # The local path states its own provenance: which host, which remote file
    assert local == tmp_path / ".uts" / "test" / "home/ops/data/5.txt"


def test_manifest_appends_and_stamps_time(tmp_path):
    ws = Workspace(tmp_path / ".uts")
    ws.record({"host": "a", "remote_path": "/x.log", "size": 10})
    ws.record({"host": "a", "remote_path": "/y.log", "size": 20})
    entries = ws.entries()
    assert [e["remote_path"] for e in entries] == ["/x.log", "/y.log"]
    assert all(e["fetched_at"] > 0 for e in entries)


def test_manifest_survives_a_corrupt_line(tmp_path):
    ws = Workspace(tmp_path / ".uts")
    ws.record({"host": "a", "remote_path": "/x.log", "size": 1})
    with ws.manifest_path.open("a") as fh:
        fh.write("not json at all\n")
    ws.record({"host": "a", "remote_path": "/y.log", "size": 2})
    assert len(ws.entries()) == 2


def test_latest_wins_when_a_file_is_pulled_twice(tmp_path):
    ws = Workspace(tmp_path / ".uts")
    ws.record({"host": "a", "remote_path": "/x.log", "size": 10, "fetched_at": 100})
    ws.record({"host": "a", "remote_path": "/x.log", "size": 99, "fetched_at": 200})
    latest = ws.latest_per_file()
    assert len(latest) == 1
    # No `direction` in these entries: they predate push and are read as pulls.
    assert latest[("a", "pull", "/x.log")]["size"] == 99


def test_pushing_and_pulling_the_same_path_stay_separate(tmp_path):
    # Collapsing the two would make the index claim one of them never happened.
    ws = Workspace(tmp_path / ".uts")
    ws.record({"direction": "pull", "host": "a", "remote_path": "/opt/run.sh", "size": 10})
    ws.record({"direction": "push", "host": "a", "remote_path": "/opt/run.sh", "size": 20})
    assert len(ws.latest_per_file()) == 2


def test_index_separates_the_two_directions(tmp_path):
    ws = Workspace(tmp_path / ".uts")
    ws.record({"direction": "pull", "host": "a", "remote_path": "/var/log/app.log", "size": 10})
    ws.record({
        "direction": "push", "host": "a", "remote_path": "/opt/run.sh",
        "size": 20, "local_path": "./run.sh",
    })
    text = ws.write_index().read_text()
    assert "fetched from it" in text
    assert "sent to it" in text
    assert "`./run.sh`" in text  # a pushed row records where it came from locally


def test_same_path_on_different_hosts_stays_separate(tmp_path):
    ws = Workspace(tmp_path / ".uts")
    ws.record({"host": "a", "remote_path": "/var/log/app.log", "size": 1})
    ws.record({"host": "b", "remote_path": "/var/log/app.log", "size": 2})
    assert len(ws.latest_per_file()) == 2


def test_index_lists_provenance(tmp_path):
    ws = Workspace(tmp_path / ".uts")
    ws.record({
        "host": "test", "remote_path": "/home/ops/5.txt",
        "size": 60900, "remote_mtime": 1766841148,
    })
    text = ws.write_index().read_text(encoding="utf-8")
    # The index must answer: what is here, how big, when did the remote change it,
    # when did I fetch it
    assert "test" in text and "/home/ops/5.txt" in text
    assert "59.5KB" in text and "2025-12" in text


def test_index_marks_truncated_pulls(tmp_path):
    ws = Workspace(tmp_path / ".uts")
    ws.record({"host": "t", "remote_path": "/big.log", "size": 100, "truncated": True, "lines": 20})
    assert "first 20 lines only" in ws.write_index().read_text(encoding="utf-8")


def test_index_on_empty_workspace_says_so(tmp_path):
    assert "empty" in Workspace(tmp_path / ".uts").write_index().read_text(encoding="utf-8")


def test_sha256_matches_hashlib(tmp_path):
    import hashlib

    p = tmp_path / "f.bin"
    p.write_bytes(b"hello uts" * 1000)
    assert Workspace(tmp_path).sha256(p) == hashlib.sha256(p.read_bytes()).hexdigest()


def test_manifest_is_valid_jsonl(tmp_path):
    # Non-ASCII remote paths must survive the manifest round trip
    ws = Workspace(tmp_path / ".uts")
    ws.record({"host": "a", "remote_path": "/data/日志.log", "size": 1})
    line = ws.manifest_path.read_text(encoding="utf-8").strip()
    assert json.loads(line)["remote_path"] == "/data/日志.log"


def test_human_bytes():
    assert human_bytes(512) == "512B"
    assert human_bytes(1024) == "1.0KB"
    assert human_bytes(6.9 * 1024**2).endswith("MB")
