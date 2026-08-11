"""uts shell — an interactive terminal on one host.

The one command here meant for a person rather than an agent, which is exactly why
it needs guarding: called without a terminal it would connect, forward nothing, and
hang until the timeout. An agent that reaches for it must be told to use
`exec --pty` instead, immediately and in words, not by appearing to freeze.
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import socket
import sys

from ..conn import Conn, describe_failure
from ..inventory import Host
from ..output import EXIT_ALL_FAILED, EXIT_BLOCKED

READY = "uts: connected to {label}. Ctrl-D or `exit` to leave.\n"


def run(hosts: list[Host]) -> int:
    if len(hosts) != 1:
        names = ", ".join(h.name for h in hosts) or "none"
        print(
            f"shell needs exactly one host, this selection has {len(hosts)}: {names}\n"
            f"Name one with -H, or run something on all of them at once with: "
            f"uts exec -a '<command>'",
            file=sys.stderr,
        )
        return EXIT_BLOCKED

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "shell needs a terminal, and this one is a pipe.\n"
            "For a one-shot command with a pty behind it: "
            "uts exec -H <name> --pty '<command>'\n"
            "For a full-screen program rendered as one frame: "
            "uts exec -H <name> --pty --duration 3 'btop'",
            file=sys.stderr,
        )
        return EXIT_BLOCKED

    host = hosts[0]
    try:
        with Conn(host) as conn:
            return _interact(conn, host)
    except Exception as exc:  # noqa: BLE001 — one connection, and its reason is the output
        print(describe_failure(host, exc), file=sys.stderr)
        return EXIT_ALL_FAILED


def _interact(conn: Conn, host: Host) -> int:
    import termios
    import tty

    transport = conn.client().get_transport()
    if transport is None:
        raise OSError("connection dropped")

    cols, rows = _size()
    chan = transport.open_session()
    chan.get_pty(term=_term(), width=cols, height=rows)
    # invoke_shell, so this is the one path that does *not* go through
    # remote.posix_wrap: a person asking for a terminal wants their own login shell,
    # fish prompt and all. Everything else in uts is pinned to POSIX sh precisely so
    # that it does not depend on that choice. Do not unify the two.
    chan.invoke_shell()

    stdin_fd = sys.stdin.fileno()
    saved = termios.tcgetattr(stdin_fd)
    previous_winch = None

    def on_resize(*_):
        try:
            chan.resize_pty(*_size())
        except OSError:
            pass

    try:
        # Raw mode, or the local terminal would eat Ctrl-C and line-buffer everything
        # the remote shell is waiting for.
        tty.setraw(stdin_fd)
        previous_winch = signal.signal(signal.SIGWINCH, on_resize)
        chan.settimeout(0.0)
        sys.stdout.write(READY.format(label=host.label))
        sys.stdout.flush()
        _pump(chan, stdin_fd)
    finally:
        # Restoring the terminal has to happen whatever went wrong. Skipping it
        # leaves the user's shell in raw mode with no echo and no working Ctrl-C.
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        if previous_winch is not None:
            signal.signal(signal.SIGWINCH, previous_winch)
        chan.close()

    print()
    return chan.recv_exit_status() if chan.exit_status_ready() else 0


def _pump(chan, stdin_fd: int) -> None:
    while True:
        try:
            ready, _, _ = select.select([chan, stdin_fd], [], [], 0.2)
        except InterruptedError:
            continue                      # a window resize interrupted the wait

        if chan in ready:
            try:
                data = chan.recv(32768)
            except socket.timeout:
                data = b""
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            elif chan.eof_received or chan.exit_status_ready():
                return

        if stdin_fd in ready:
            keys = os.read(stdin_fd, 4096)
            if not keys:                  # local Ctrl-D at the outermost level
                chan.shutdown_write()
                continue
            chan.sendall(keys)

        if chan.exit_status_ready() and not chan.recv_ready():
            return


def _size() -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(120, 40))
    return size.columns, size.lines


def _term() -> str:
    return os.environ.get("TERM") or "xterm-256color"
