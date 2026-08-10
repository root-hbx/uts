"""uts exec — run one command on the selected hosts, concurrently."""

from __future__ import annotations

import sys
import uuid

from .. import guard, remote
from ..conn import Conn, Limits, Result, run_command, run_many
from ..inventory import Host
from ..output import EXIT_BLOCKED, emit
from ..session import Session, env_delta

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
    session_name: str | None = None,
    workspace_root: str | None = None,
) -> int:
    command = command.strip()
    if not command:
        print("no command given. Usage: uts exec all -- ls -la /var/log", file=sys.stderr)
        return EXIT_BLOCKED

    _warn_if_global_flag_leaked(command)

    if not write:
        # The guard reads what the user typed, never the session wrapper built around
        # it — otherwise every session command would look like it exports things.
        reason = guard.check(command)
        if reason:
            print(guard.explain(command, reason), file=sys.stderr)
            return EXIT_BLOCKED

    if session_name:
        results = _run_in_session(hosts, command, jobs, limits, session_name, workspace_root)
    else:
        results = run_command(hosts, command, limits, jobs=jobs)

    return emit(
        results, as_json,
        hint="raise --max-lines, or narrow with grep/tail on the remote side",
        max_cols=max_cols,
    )


def _run_in_session(
    hosts: list[Host],
    command: str,
    jobs: int,
    limits: Limits,
    session_name: str,
    workspace_root: str | None,
) -> list[Result]:
    session = Session(session_name, workspace_root)

    def task(conn: Conn) -> Result:
        return _exec_with_state(conn, command, limits, session)

    results = run_many(hosts, task, jobs=jobs)

    # Written back on this thread: run_many fans out, and a shared session file with
    # concurrent writers would lose whichever host finished first.
    for r in results:
        pending = r.extra.pop("_session_state", None)
        if pending is None:
            continue
        changed = session.update(
            r.host.name, pending["cwd"], pending["env"], pending["baseline"]
        )
        notes = []
        if changed["cwd"]:
            notes.append(f"cwd → {changed['cwd']}")
        if changed["env_added"]:
            notes.append(f"env +{', +'.join(changed['env_added'])}")
        if changed["env_dropped"]:
            notes.append(f"env -{', -'.join(changed['env_dropped'])}")
        if notes:
            r.extra.setdefault("notes", []).extend(notes)
    return results


def _exec_with_state(conn: Conn, command: str, limits: Limits, session: Session) -> Result:
    host = conn.host.name
    baseline = session.baseline(host)
    if baseline is None:
        # One extra round trip, only the first time this session touches this host.
        # The delta needs something to be a delta against.
        probe = conn.run(remote.env_probe(), Limits(max_lines=5, max_bytes=1 << 20,
                                                    timeout=limits.timeout))
        baseline = remote.parse_env0(probe.stdout)

    nonce = uuid.uuid4().hex[:12]
    result = conn.run(
        remote.session_wrap(command, session.cwd(host), session.env(host), nonce),
        limits,
    )
    result.extra["session"] = session.name
    # The cwd the command *ran in*, not the one it ended in. That is what decides how
    # relative paths in the output should be read; where the session moved to is
    # reported separately as a `cwd →` note.
    result.extra["cwd"] = session.cwd(host)

    body, cwd, env = remote.parse_session_trailer(result.stdout, nonce)
    result.stdout = body
    if cwd is None:
        # No trailer. Either the command exited the shell itself, or its output was
        # long enough that the cap ate the trailer. Saying so beats letting the agent
        # believe a `cd` took effect when it did not.
        result.extra.setdefault("notes", []).append(
            "session unchanged: the command ended the shell, or its output hit the "
            "cap before the state could be reported"
        )
        return result

    result.extra["_session_state"] = {
        "cwd": cwd,
        "env": env_delta(env, baseline),
        "baseline": baseline,
    }
    return result
