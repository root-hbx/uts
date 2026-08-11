"""One dialect on every host, whatever the account's login shell is.

sshd hands an exec request to the user's login shell. bash and zsh are close enough
to POSIX that the snippets in remote.py mostly worked; fish is not a POSIX shell and
rejected `n=$(...)` outright, which took `uts status` on a fish host down to
`clock ?` plus a stderr blurt. remote.posix_wrap pins every command to sh.

Nothing here mocks paramiko — the house style is to assert on the string a builder
produced. The two things worth pinning are that wrapping is *lossless* (the far side
recovers the snippet byte for byte, however quote-dense it is) and that it is applied
at every exec_command site but never to `uts shell`.
"""

import shlex
from pathlib import Path

from uts import remote

SRC = Path(__file__).resolve().parent.parent / "src" / "uts"

# One of each shape that reaches the wire, weighted towards the quote-dense ones:
# SHAPE embeds an awk program, and start_job is already `setsid sh -c {q(inner)}`
# before wrapping makes it three levels deep.
BUILDERS = {
    "facts": remote.facts(),
    "list_files": remote.list_files("~/data/*.csv"),
    "shape_probe": remote.shape_probe(["/var/log/a b.log", "/data/x'y.csv"]),
    "tar_stream": remote.tar_stream(["/var/log/syslog", "/data/it's.csv"]),
    "head_file": remote.head_file("/var/log/my app.log", 20),
    "push_probe": remote.push_probe("~/dest", ["a b.txt", "c'd.txt"]),
    "untar_stream": remote.untar_stream("~/dest"),
    "start_job": remote.start_job("train", "python train.py | tee out.log"),
    "list_jobs": remote.list_jobs(),
    "job_log": remote.job_log("train", 50),
    "kill_job_named": remote.kill_job("train", force=False),
    "kill_job_sweep": remote.kill_job(None, force=True),
    "stop_and_clean": remote.stop_and_clean("train", force=False),
    "session_wrap": remote.session_wrap(
        "echo $HOME", "/srv/proj", {"TOKEN": "a'b c"}, "deadbeef1234"
    ),
    "env_probe": remote.env_probe(),
}


def test_wrap_is_sh_dash_c():
    assert remote.posix_wrap("uname -s") == "sh -c 'uname -s'"


def test_wrap_escapes_embedded_single_quotes():
    # The failure this guards against is silent: a broken escape does not error, it
    # runs a *different* command.
    wrapped = remote.posix_wrap("awk '{print $1}' f")
    assert shlex.split(wrapped)[2] == "awk '{print $1}' f"


def test_every_builder_survives_the_wrap_byte_for_byte():
    """The far side must recover exactly what the builder wrote.

    shlex.split applies POSIX word-splitting rules, so this is the same recovery the
    remote sh performs — an exact round trip, not a syntax approximation.
    """
    for name, snippet in BUILDERS.items():
        assert shlex.split(remote.posix_wrap(snippet)) == ["sh", "-c", snippet], name


def test_wrapping_is_not_applied_twice():
    once = remote.posix_wrap("ls")
    assert shlex.split(remote.posix_wrap(once)) == ["sh", "-c", once]


def test_every_exec_command_in_conn_is_wrapped():
    """A fifth exec_command() site added later must not slip through unwrapped.

    Source-level because conn.py cannot be exercised without a network, and mocking
    paramiko is not how this suite works.
    """
    source = (SRC / "conn.py").read_text()
    sites = [ln.strip() for ln in source.splitlines() if "chan.exec_command(" in ln]
    assert len(sites) == 4, sites
    assert all(ln.startswith("chan.exec_command(posix_wrap(") for ln in sites), sites


def test_uts_shell_keeps_the_users_own_login_shell():
    """The deliberate exception: an interactive terminal stays fish if that is what
    the account uses. Only invoke_shell can do that, and it never takes a command."""
    code = "\n".join(
        ln for ln in (SRC / "commands" / "shell.py").read_text().splitlines()
        if not ln.lstrip().startswith("#")      # the exemption is *explained* there
    )
    assert "chan.invoke_shell()" in code
    assert "exec_command" not in code
    assert "posix_wrap(" not in code
