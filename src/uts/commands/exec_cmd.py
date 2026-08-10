"""uts exec — run one command on the selected hosts, concurrently."""

from __future__ import annotations

import sys

from .. import guard
from ..conn import Limits, run_command
from ..inventory import Host
from ..output import EXIT_BLOCKED, emit

# These are uts's global flags and only work *before* the subcommand. Written
# after `--` they are silently passed to the remote program, which produces wrong
# results with no visible cause.
_GLOBAL_FLAGS = ("--hosts", "--jobs", "--timeout", "--max-lines", "--max-bytes", "--json")


def _warn_if_global_flag_leaked(command: str) -> None:
    leaked = [f for f in _GLOBAL_FLAGS if f in command.split()]
    if leaked:
        print(
            f"note: {', '.join(leaked)} are uts's own flags and were just sent to the "
            f"remote command.\n      Put them before the subcommand: uts {leaked[0]} ... exec ...",
            file=sys.stderr,
        )


def run(
    hosts: list[Host],
    command: str,
    jobs: int,
    limits: Limits,
    as_json: bool,
    write: bool,
    max_cols: int = 200,
) -> int:
    command = command.strip()
    if not command:
        print("no command given. Usage: uts exec all -- ls -la /var/log", file=sys.stderr)
        return EXIT_BLOCKED

    _warn_if_global_flag_leaked(command)

    if not write:
        reason = guard.check(command)
        if reason:
            print(guard.explain(command, reason), file=sys.stderr)
            return EXIT_BLOCKED

    results = run_command(hosts, command, limits, jobs=jobs)
    return emit(
        results, as_json,
        hint="raise --max-lines, or narrow with grep/tail on the remote side",
        max_cols=max_cols,
    )
