"""How `uts exec` turns argv into one remote command string.

Both behaviours pinned here used to fail silently, which is the worst way for a
remote command to be wrong: the command runs, it just isn't the one you typed.
"""

import pytest

from uts.cli import build_parser, join_command


def parse_exec(args):
    """argv as the shell hands it over, through argparse, into the joiner."""
    ns = build_parser().parse_args(args)
    return bool(ns.write), join_command(ns.argv)


# ------------------------------------------------------------------ flag position

# `uts exec test --write -- rm x` used to send `--write` to the remote shell and
# leave write mode off, because REMAINDER takes flags too. It was hoisted back out
# by hand. With the host selector gone from the positional slot, REMAINDER is the
# only positional and argparse claims every uts flag before it starts — so these
# cases are now argparse's job, and the tests are here to catch it changing.


@pytest.mark.parametrize(
    "args",
    [
        ["exec", "--write", "-H", "t", "--", "rm", "x"],
        ["exec", "-H", "t", "--write", "--", "rm", "x"],
    ],
)
def test_write_is_read_by_uts_wherever_it_sits_before_the_command(args):
    write, command = parse_exec(args)
    assert write
    assert command == "rm x"


def test_write_in_the_string_form():
    write, command = parse_exec(["exec", "-H", "t", "--write", "rm -f /tmp/x"])
    assert write
    assert command == "rm -f /tmp/x"


def test_write_after_separator_belongs_to_the_remote_command(capsys):
    # `--` means everything right of it is the remote program's business, so this
    # must pass through silently — warning here would cry wolf on a correct call.
    write, command = parse_exec(["exec", "-H", "t", "--", "mytool", "--write"])
    assert not write
    assert command == "mytool --write"
    assert capsys.readouterr().err == ""


def test_the_separator_survives_into_the_remainder():
    # What lets join_command tell the two forms apart. It only holds because no
    # positional precedes the REMAINDER: when a host selector did, argparse ate the
    # `--` in `exec test -- rm x` and kept it in `exec test --write -- rm x`.
    parse = build_parser().parse_args
    assert parse(["exec", "-H", "t", "--", "rm", "x"]).argv == ["--", "rm", "x"]
    assert parse(["exec", "-H", "t", "--write", "--", "rm", "x"]).argv == ["--", "rm", "x"]
    assert parse(["exec", "-a", "--", "rm", "x"]).argv == ["--", "rm", "x"]


def test_only_the_first_separator_is_uts_business():
    _, command = parse_exec(["exec", "-H", "t", "--", "sh", "-c", "--", "x"])
    assert command == "sh -c -- x"


def test_leaked_flag_is_passed_through_with_a_note(capsys):
    write, command = parse_exec(["exec", "-H", "t", "mytool", "--write"])
    assert not write
    assert command == "mytool --write"
    assert "--write" in capsys.readouterr().err


def test_leaked_target_flag_is_also_noted(capsys):
    # `uts exec 'ls' -H t` — the target went in after the command started, so uts
    # never saw it and the selection will fail. Say so rather than run `ls -H t`.
    _, command = parse_exec(["exec", "ls", "-H", "t"])
    assert command == "ls -H t"
    assert "-H" in capsys.readouterr().err


# ------------------------------------------------------------------ quoting


def test_quoted_argument_survives_as_one_argument():
    # The local shell already stripped the quotes; joining on spaces would hand
    # pgrep two patterns instead of one and it exits with a usage error.
    _, command = parse_exec(["exec", "-H", "t", "--", "pgrep", "-af", "sleep 600"])
    assert command == "pgrep -af 'sleep 600'"


def test_python_snippet_keeps_its_inner_quotes():
    _, command = parse_exec(
        ["exec", "-H", "t", "--", "python3", "-c", 'open("/tmp/x","w").write("hi")']
    )
    assert command == "python3 -c 'open(\"/tmp/x\",\"w\").write(\"hi\")'"


def test_plain_argv_is_unchanged():
    _, command = parse_exec(["exec", "-a", "--", "ls", "-la", "/var/log"])
    assert command == "ls -la /var/log"


def test_single_string_reaches_the_shell_verbatim():
    # Quoting this one would make the whole pipeline the name of a program.
    script = "ls ~/data/*.csv | wc -l"
    _, command = parse_exec(["exec", "-H", "t", script])
    assert command == script


def test_single_string_after_separator_is_also_verbatim():
    script = "nohup sleep 600 >/dev/null 2>&1 & echo $!"
    _, command = parse_exec(["exec", "-H", "t", "--", script])
    assert command == script


def test_empty_argv_yields_empty_command():
    # exec_cmd.run turns this into the usage error; nothing is sent.
    assert parse_exec(["exec", "-H", "t"]) == (False, "")


# ------------------------------------------------------------------ targeting


@pytest.mark.parametrize(
    "command, extra",
    [
        ("status", []),
        ("ls", ["~/data/"]),
        ("find", ["~/data/"]),
        ("peek", ["~/data/*.csv"]),
        ("pull", ["~/data/*.csv"]),
        ("push", ["./a", "--to", "~/bin/"]),
        ("exec", ["--", "ls"]),
        ("shell", []),
    ],
)
def test_every_networked_subcommand_takes_the_same_target_flags(command, extra):
    parse = build_parser().parse_args
    assert parse([command, "-H", "a,b", *extra]).host == ["a,b"]
    assert parse([command, "-a", *extra]).all is True
    # Absent, both are falsy and inventory.select is what refuses.
    bare = parse([command, *extra])
    assert not bare.host and not bare.all


@pytest.mark.parametrize("command", ["hosts", "index"])
def test_local_only_subcommands_have_no_target_flags(command, capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args([command, "-a"])
