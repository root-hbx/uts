"""How `uts exec` turns argv into one remote command string.

Both behaviours pinned here used to fail silently, which is the worst way for a
remote command to be wrong: the command runs, it just isn't the one you typed.
"""

from uts.cli import build_parser, split_exec_argv


def parse_exec(args):
    """argv as the shell hands it over, through argparse, into the splitter.

    Mirrors main(): the `--` check reads the untouched argv, because argparse
    removes that separator in some positions and keeps it in others.
    """
    return split_exec_argv(
        build_parser().parse_args(args).argv, separator_used="--" in args
    )


# ------------------------------------------------------------------ --write position


def test_write_before_selector():
    assert build_parser().parse_args(["exec", "--write", "test", "--", "rm", "x"]).write


def test_write_after_selector_is_hoisted_not_sent_remote():
    # argparse.REMAINDER swallows it; without hoisting, write mode stays off and
    # `--write` becomes the first word of the remote command.
    write, command = parse_exec(["exec", "test", "--write", "--", "rm", "x"])
    assert write
    assert command == "rm x"


def test_write_hoisted_from_string_form():
    write, command = parse_exec(["exec", "test", "--write", "rm -f /tmp/x"])
    assert write
    assert command == "rm -f /tmp/x"


def test_write_after_separator_belongs_to_the_remote_command(capsys):
    # `--` means everything right of it is the remote program's business, so this
    # must pass through silently — warning here would cry wolf on a correct call.
    write, command = parse_exec(["exec", "test", "--", "mytool", "--write"])
    assert not write
    assert command == "mytool --write"
    assert capsys.readouterr().err == ""


def test_separator_is_not_recoverable_from_the_remainder():
    # Why split_exec_argv cannot work this out for itself: argparse drops the `--`
    # in the first form and keeps it in the second.
    parse = build_parser().parse_args
    assert parse(["exec", "test", "--", "rm", "x"]).argv == ["rm", "x"]
    assert parse(["exec", "test", "--write", "--", "rm", "x"]).argv == [
        "--write", "--", "rm", "x",
    ]


def test_leaked_write_is_passed_through_with_a_note(capsys):
    write, command = parse_exec(["exec", "test", "mytool", "--write"])
    assert not write
    assert command == "mytool --write"
    assert "--write" in capsys.readouterr().err


# ------------------------------------------------------------------ quoting


def test_quoted_argument_survives_as_one_argument():
    # The local shell already stripped the quotes; joining on spaces would hand
    # pgrep two patterns instead of one and it exits with a usage error.
    _, command = parse_exec(["exec", "test", "--", "pgrep", "-af", "sleep 600"])
    assert command == "pgrep -af 'sleep 600'"


def test_python_snippet_keeps_its_inner_quotes():
    _, command = parse_exec(
        ["exec", "test", "--", "python3", "-c", 'open("/tmp/x","w").write("hi")']
    )
    assert command == "python3 -c 'open(\"/tmp/x\",\"w\").write(\"hi\")'"


def test_plain_argv_is_unchanged():
    _, command = parse_exec(["exec", "all", "--", "ls", "-la", "/var/log"])
    assert command == "ls -la /var/log"


def test_single_string_reaches_the_shell_verbatim():
    # Quoting this one would make the whole pipeline the name of a program.
    script = "ls ~/data/*.csv | wc -l"
    _, command = parse_exec(["exec", "test", script])
    assert command == script


def test_single_string_after_separator_is_also_verbatim():
    script = "nohup sleep 600 >/dev/null 2>&1 & echo $!"
    _, command = parse_exec(["exec", "test", "--", script])
    assert command == script


def test_empty_argv_yields_empty_command():
    # exec_cmd.run turns this into the usage error; nothing is sent.
    assert parse_exec(["exec", "test"]) == (False, "")
