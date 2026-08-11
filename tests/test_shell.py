"""uts shell refuses before it hangs.

Both guards exist for the same reason: this is the one command written for a person,
and the two ways an agent reaches it wrongly both end in a silent wait rather than
an error. A tool that appears to freeze teaches nothing.
"""

from uts.cli import build_parser, join_command
from uts.commands import shell
from uts.inventory import Host
from uts.output import EXIT_BLOCKED


def host(name):
    return Host(name=name, ip="10.0.0.1", user="u", password="p")


def test_several_hosts_are_refused_with_the_names_listed(capsys):
    assert shell.run([host("a"), host("b")]) == EXIT_BLOCKED
    err = capsys.readouterr().err
    assert "has 2" in err and "a, b" in err
    assert "uts exec -a" in err             # points at the command that does fan out


def test_no_host_is_refused_too(capsys):
    assert shell.run([]) == EXIT_BLOCKED
    assert "has 0" in capsys.readouterr().err


def test_without_a_terminal_it_says_so_instead_of_waiting(capsys):
    # pytest captures stdin/stdout, so this is exactly the agent's situation.
    assert shell.run([host("a")]) == EXIT_BLOCKED
    err = capsys.readouterr().err
    assert "needs a terminal" in err
    assert "--pty" in err                   # and names the thing to use instead


# ------------------------------------------------------------------ pty flags


def parse(args):
    return build_parser().parse_args(args)


def test_pty_flags_are_read_by_uts_not_sent_remote():
    ns = parse(["exec", "-H", "t", "--pty", "--duration", "3", "--", "btop"])
    assert (ns.pty, ns.duration) == (True, 3.0)
    assert join_command(ns.argv) == "btop"


def test_duration_accepts_the_equals_form():
    assert parse(["exec", "-H", "t", "--duration=2.5", "--", "btop"]).duration == 2.5


def test_pty_after_the_separator_belongs_to_the_remote_command():
    ns = parse(["exec", "-H", "t", "--", "mytool", "--pty"])
    assert ns.pty is False
    assert join_command(ns.argv) == "mytool --pty"
