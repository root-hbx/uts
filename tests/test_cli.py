"""How `uts exec` turns argv into one remote command string.

There is one form: the command is a single quoted string, and what you quote is
what the remote shell gets. The v2 `--` form is gone — it was a REMAINDER, so it
also swallowed every uts flag typed after the command and could only warn about it
afterwards. These tests pin the boundary that replaces it: flags belong to uts,
the string belongs to the far side.
"""

import pytest

from uts.cli import build_parser, one_command, quote_for_display


def parse_exec(args):
    """argv as the shell hands it over, through argparse, into the joiner."""
    ns = build_parser().parse_args(args)
    return bool(ns.write), one_command(ns.command_, "exec")


# ------------------------------------------------------------------ flag position


@pytest.mark.parametrize(
    "args",
    [
        ["exec", "--write", "-H", "t", "rm x"],
        ["exec", "-H", "t", "--write", "rm x"],
        # The case REMAINDER could not do: a uts flag after the command is uts's,
        # exactly as it is for every other subcommand.
        ["exec", "-H", "t", "rm x", "--write"],
    ],
)
def test_write_is_read_by_uts_wherever_it_sits(args):
    write, command = parse_exec(args)
    assert write
    assert command == "rm x"


def test_a_flag_inside_the_string_belongs_to_the_remote_command(capsys):
    # Inside the quotes is the remote program's business, so this must pass through
    # silently — warning here would cry wolf on a correct call.
    write, command = parse_exec(["exec", "-H", "t", "mytool --write"])
    assert not write
    assert command == "mytool --write"
    assert capsys.readouterr().err == ""


def test_the_command_is_one_positional_not_a_remainder():
    # What makes the line above work. A REMAINDER would have taken --pty too.
    ns = build_parser().parse_args(["exec", "-H", "t", "mytool --write", "--pty"])
    assert ns.command_ == ["mytool --write"]
    assert ns.pty is True


# ------------------------------------------------------------------ the old form


@pytest.mark.parametrize(
    "args, rewritten",
    [
        (["exec", "-H", "t", "--", "rm", "-rf", "/tmp/x"], "'rm -rf /tmp/x'"),
        (["exec", "-a", "--", "ls", "-la", "/var/log"], "'ls -la /var/log'"),
        # The local shell already stripped the quotes, so the rewrite has to put
        # them back or pgrep gets two patterns instead of one.
        (["exec", "-H", "t", "--", "pgrep", "-af", "sleep 600"], '"pgrep -af \'sleep 600\'"'),
    ],
)
def test_separate_arguments_are_refused_with_the_rewrite(args, rewritten, capsys):
    # Refusing beats guessing: joining on spaces would silently change the command,
    # which is the worst way for a remote command to be wrong.
    write, command = parse_exec(args)
    assert command is None
    err = capsys.readouterr().err
    assert "one quoted string" in err
    assert rewritten in err


def test_a_lone_token_after_the_separator_still_works():
    # argparse eats the first `--` itself, and one token is already the one form.
    _, command = parse_exec(["exec", "-H", "t", "--", "nvidia-smi"])
    assert command == "nvidia-smi"


@pytest.mark.parametrize(
    "command, expected",
    [
        ("ls -la", "'ls -la'"),
        ("pgrep -af 'sleep 600'", '"pgrep -af \'sleep 600\'"'),
    ],
)
def test_the_rewrite_picks_an_outer_quote_that_survives_the_inner_one(command, expected):
    assert quote_for_display(command) == expected


# ------------------------------------------------------------------ quoting


def test_the_string_reaches_the_shell_verbatim():
    # Quoting this one would make the whole pipeline the name of a program.
    script = "ls ~/data/*.csv | wc -l"
    _, command = parse_exec(["exec", "-H", "t", script])
    assert command == script


def test_a_python_snippet_keeps_its_inner_quotes():
    script = 'python3 -c \'open("/tmp/x","w").write("hi")\''
    _, command = parse_exec(["exec", "-H", "t", script])
    assert command == script


def test_backgrounding_and_redirection_survive():
    script = "nohup sleep 600 >/dev/null 2>&1 & echo $!"
    _, command = parse_exec(["exec", "-H", "t", script])
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
        ("exec", ["ls"]),
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
