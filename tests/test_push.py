"""uts push: what gets collected locally, and what the remote side is asked to do.

The transfer itself is covered by the live tests. What is worth pinning here is
everything that decides *which bytes land where*, because a push that silently
puts a file in the wrong place is worse than one that fails.
"""

import tarfile

import pytest

from uts import remote
from uts.cli import build_parser
from uts.commands.push import PushError, _build_archive, _collect


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "setup.sh").write_text("#!/bin/sh\necho hi\n")
    lib = tmp_path / "lib"
    (lib / "nested").mkdir(parents=True)
    (lib / "a.py").write_text("a = 1\n")
    (lib / "nested" / "b.py").write_text("b = 2\n")
    return tmp_path


# ------------------------------------------------------------------ argv shape


def test_sources_are_positional_and_the_destination_is_named():
    # It used to read like cp -- every path but the last one local. The path that
    # behaves differently now says so instead of relying on where it sits.
    args = build_parser().parse_args(
        ["push", "-H", "a", "./a.sh", "./lib", "--to", "~/bin/"]
    )
    assert args.srcs == ["./a.sh", "./lib"]
    assert args.to == "~/bin/"


def test_the_destination_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["push", "-H", "a", "./a.sh"])


def test_a_destination_that_is_a_source_is_no_longer_possible():
    # `uts push -H a ./a.sh ./b.sh` used to send a.sh to the directory ./b.sh.
    args = build_parser().parse_args(["push", "-H", "a", "./a.sh", "./b.sh", "--to", "~/bin/"])
    assert args.srcs == ["./a.sh", "./b.sh"]


# ------------------------------------------------------------------ collection


def test_file_lands_directly_in_dest(tree):
    files = _collect([str(tree / "setup.sh")])
    assert [member for _, member, _ in files] == ["setup.sh"]


def test_directory_keeps_its_own_name(tree):
    # `cp -r ./lib ~/bin/` produces ~/bin/lib/..., not ~/bin/a.py.
    members = sorted(m for _, m, _ in _collect([str(tree / "lib")]))
    assert members == ["lib/a.py", "lib/nested/b.py"]


def test_trailing_slash_does_not_change_the_member_names(tree):
    with_slash = sorted(m for _, m, _ in _collect([str(tree / "lib") + "/"]))
    without = sorted(m for _, m, _ in _collect([str(tree / "lib")]))
    assert with_slash == without


def test_sizes_come_from_the_local_files(tree):
    files = _collect([str(tree / "setup.sh")])
    assert files[0][2] == (tree / "setup.sh").stat().st_size


def test_missing_source_is_rejected_before_connecting(tree):
    with pytest.raises(PushError, match="does not exist"):
        _collect([str(tree / "nope.sh")])


def test_two_sources_colliding_on_one_remote_path_are_rejected(tmp_path):
    # Inside a tar the second one just wins, and nothing says so afterwards.
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "same.txt").write_text("from a\n")
    (b / "same.txt").write_text("from b\n")
    with pytest.raises(PushError, match="would both become"):
        _collect([str(a / "same.txt"), str(b / "same.txt")])


# ------------------------------------------------------------------ the archive


def test_archive_members_match_what_was_collected(tree):
    files = _collect([str(tree / "setup.sh"), str(tree / "lib")])
    archive = _build_archive(files)
    try:
        with tarfile.open(archive) as tar:
            names = sorted(tar.getnames())
        assert names == ["lib/a.py", "lib/nested/b.py", "setup.sh"]
    finally:
        archive.unlink(missing_ok=True)


def test_archive_holds_no_absolute_paths(tree):
    # An absolute member would unpack outside the destination.
    files = _collect([str(tree / "lib")])
    archive = _build_archive(files)
    try:
        with tarfile.open(archive) as tar:
            assert all(not n.startswith("/") and ".." not in n for n in tar.getnames())
    finally:
        archive.unlink(missing_ok=True)


# ------------------------------------------------------------------ remote side


def test_dest_is_left_unquoted_so_tilde_expands():
    # Same rule as every other path spec: `~` has to reach the remote shell.
    assert "d=~/bin/" in remote.untar_stream("~/bin/")


def test_member_names_are_quoted_in_the_probe():
    probe = remote.push_probe("~/bin/", ["a b.txt"])
    assert "'a b.txt'" in probe
    assert "d=~/bin/" in probe


def test_glob_destination_is_rejected():
    with pytest.raises(remote.PathSpecError, match="glob"):
        remote.check_dest("~/da*/")


def test_command_separators_in_dest_are_rejected():
    with pytest.raises(remote.PathSpecError):
        remote.check_dest("~/bin/; rm -rf /")


def test_probe_output_parses_into_dest_and_conflicts():
    info = remote.parse_push_probe(
        "dest\t/home/bxhu/bin\nexists\tsetup.sh\nexists\tlib/a.py\n"
    )
    assert info["dest"] == "/home/bxhu/bin"
    assert info["exists"] == ["setup.sh", "lib/a.py"]
    assert info["notdir"] is None


def test_probe_reports_a_destination_that_is_a_file():
    info = remote.parse_push_probe("dest\t/etc/hosts\nnotdir\t/etc/hosts\n")
    assert info["notdir"] == "/etc/hosts"
    assert info["exists"] == []
