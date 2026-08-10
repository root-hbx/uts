"""uts shell refuses before it hangs.

Both guards exist for the same reason: this is the one command written for a person,
and the two ways an agent reaches it wrongly both end in a silent wait rather than
an error. A tool that appears to freeze teaches nothing.
"""

from uts.cli import build_parser, split_exec_argv
from uts.commands import shell
from uts.inventory import Host
from uts.output import EXIT_BLOCKED


def host(name):
    return Host(name=name, ip="10.0.0.1", user="u", password="p")


def hoist(args):
    return split_exec_argv(
        build_parser().parse_args(args).argv, separator_used="--" in args
    )[0]


def test_several_hosts_are_refused_with_the_names_listed(capsys):
    assert shell.run([host("a"), host("b")], "@prod") == EXIT_BLOCKED
    err = capsys.readouterr().err
    assert "matched 2" in err and "a, b" in err
    assert "uts exec @prod" in err          # points at the command that does fan out


def test_no_host_is_refused_too(capsys):
    assert shell.run([], "nope") == EXIT_BLOCKED
    assert "matched 0" in capsys.readouterr().err


def test_without_a_terminal_it_says_so_instead_of_waiting(capsys):
    # pytest captures stdin/stdout, so this is exactly the agent's situation.
    assert shell.run([host("a")], "a") == EXIT_BLOCKED
    err = capsys.readouterr().err
    assert "needs a terminal" in err
    assert "--pty" in err                   # and names the thing to use instead


# ------------------------------------------------------------------ pty flags


def test_pty_after_the_selector_is_hoisted_not_sent_remote():
    assert hoist(["exec", "test", "--pty", "--", "btop"]) == {"--pty": True}


def test_duration_carries_its_value_across_the_remainder():
    assert hoist(["exec", "test", "--pty", "--duration", "3", "--", "btop"]) == {
        "--pty": True, "--duration": "3",
    }


def test_duration_accepts_the_equals_form():
    assert hoist(["exec", "test", "--duration=2.5", "--", "btop"]) == {"--duration": "2.5"}


def test_pty_after_the_separator_belongs_to_the_remote_command():
    hoisted, command = split_exec_argv(["--", "mytool", "--pty"], separator_used=True)
    assert hoisted == {}
    assert command == "mytool --pty"
