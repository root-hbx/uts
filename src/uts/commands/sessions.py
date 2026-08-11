"""uts start / ps / logs / stop — work that outlives the call that started it.

Every exec opens its own channel and closes it again, so a training run started
through uts used to die with the connection. This is the part of "operate" an agent
actually needs: start something on another machine, come back twenty minutes later,
find out how it went.

**A session is one name for one piece of work.** It used to be two ideas wearing the
same word: `--session build` carried cwd and exported variables forward, while a
detached job was addressed by a generated id like `7f3c1a`. Nobody could say which
of the two `session` meant. Now the name you give is the whole handle — it selects
the environment a command runs in *and* the background process running in it, so a
session holds at most one at a time.

The two halves live in different places, which is why cleaning up touches both: the
running side is `~/.uts/jobs/<name>/` on the remote host — plain files, written by
the job itself — and the cwd/env side is `.uts/sessions/<name>.json` here.
"""

from __future__ import annotations

import json
import sys

from .. import guard, remote
from ..conn import Conn, Limits, Result, run_many
from ..inventory import Host
from ..output import EXIT_BLOCKED, emit, exit_code, plural, to_json
from ..session import Session, list_sessions

LIST_LIMITS = Limits(max_lines=2000, max_bytes=1 << 20)


# --------------------------------------------------------------------- start


def run_start(
    hosts: list[Host],
    command: str,
    name: str,
    jobs: int,
    limits: Limits,
    as_json: bool,
    write: bool,
    force: bool,
    workspace_root: str | None,
) -> int:
    command = command.strip()
    if not command:
        print(
            "no command given. Usage: uts start -H <name> -s <session> 'python train.py'",
            file=sys.stderr,
        )
        return EXIT_BLOCKED

    try:
        remote.check_session_name(name)
    except remote.PathSpecError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BLOCKED

    if not write:
        reason = guard.check(command)
        if reason:
            print(guard.explain(command, reason), file=sys.stderr)
            return EXIT_BLOCKED

    # The session's cwd and exports are replayed into the job, but the job does not
    # report state back: it outlives this call, so there is no "where did it end up"
    # to record, and its own `cd` belongs to it rather than to the session.
    session = Session(name, workspace_root)

    def task(conn: Conn) -> Result:
        host = conn.host.name
        prefix = remote.session_prefix(session.cwd(host), session.env(host))
        result = conn.run(remote.start_job(name, command, prefix, force), limits)
        result.extra["session"] = name
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if parts[0] == "job" and len(parts) == 3:
                result.extra["pid"] = parts[2]
                result.stdout = ""
            elif parts[0] == "busy" and len(parts) == 2:
                result.refused = (
                    f"session {name!r} is already running here (pid {parts[1]}). "
                    f"Stop it first: uts stop -H {host} -s {name}"
                )
                result.stdout = ""
            elif parts[0] == "finished":
                result.refused = (
                    f"session {name!r} already holds a finished run here, and starting "
                    f"over would discard its log. Keep the name with --force, or clear "
                    f"it: uts ps -H {host} -s {name} --clean"
                )
                result.stdout = ""
        return result

    results = run_many(hosts, task, jobs=jobs)
    if as_json:
        print(to_json(results))
    else:
        print("\n\n".join(render_started(r) for r in results))
    return exit_code(results)


def render_started(r: Result) -> str:
    """One host's answer to `uts start`."""
    if not r.reachable:
        return f"=== {r.host.label} · UNREACHABLE · {r.duration:.2f}s ===\n  {r.error}"
    if r.refused:
        return f"=== {r.host.label} · REFUSED · {r.duration:.2f}s ===\n  {r.refused}"

    name = r.extra.get("session")
    if not r.extra.get("pid"):
        detail = (r.stderr or r.stdout).strip().splitlines()
        return (
            f"=== {r.host.label} · FAILED TO START · {r.duration:.2f}s ===\n"
            f"  {detail[0] if detail else f'rc={r.rc}'}"
        )
    return (
        f"=== {r.host.label} · session {name} · pid {r.extra['pid']} ===\n"
        f"  uts logs -H {r.host.name} -s {name}    ·    "
        f"uts stop -H {r.host.name} -s {name}"
    )


# ------------------------------------------------------------------------ ps


def run_ps(
    hosts: list[Host],
    jobs: int,
    timeout: float,
    as_json: bool,
    clean: bool,
    name: str | None,
    workspace_root: str | None,
) -> int:
    if name:
        try:
            remote.check_session_name(name)
        except remote.PathSpecError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_BLOCKED

    if clean:
        return _clean(hosts, jobs, timeout, as_json, name, workspace_root)

    limits = Limits(max_lines=LIST_LIMITS.max_lines, max_bytes=LIST_LIMITS.max_bytes,
                    timeout=timeout)
    results = run_many(hosts, lambda c: c.run(remote.list_jobs(), limits), jobs=jobs)

    local = _local_state(workspace_root)
    for r in results:
        if not r.reachable:
            continue
        now, listed = remote.parse_jobs(r.stdout)
        if name:
            listed = [j for j in listed if j["id"] == name]
        r.extra["now"] = now
        r.extra["jobs"] = listed
        # Sessions this host has cwd/env for but nothing running in: the other half
        # of the same table, and the reason `uts sessions` no longer exists.
        running = {j["id"] for j in listed}
        r.extra["idle"] = sorted(
            s for s, hosts_seen in local.items()
            if r.host.name in hosts_seen and s not in running and (not name or s == name)
        )
        r.extra["cwds"] = {s: local[s][r.host.name] for s in local if r.host.name in local[s]}

    if as_json:
        print(json.dumps([_json_item(r) for r in results], ensure_ascii=False, indent=2))
    else:
        print(_render_table(results))
    return exit_code(results)


def _local_state(workspace_root: str | None) -> dict[str, dict[str, str]]:
    """{session: {host: cwd}} from .uts/sessions/*.json."""
    out: dict[str, dict[str, str]] = {}
    for s in list_sessions(workspace_root):
        out[s.name] = {host: s.cwd(host) or "?" for host in s.hosts()}
    return out


def _clean(
    hosts: list[Host],
    jobs: int,
    timeout: float,
    as_json: bool,
    name: str | None,
    workspace_root: str | None,
) -> int:
    limits = Limits(max_lines=200, max_bytes=64 << 10, timeout=timeout)
    command = remote.clean_jobs(name)
    results = run_many(hosts, lambda c: c.run(command, limits), jobs=jobs)

    for r in results:
        if not r.reachable:
            continue
        cleaned, busy = remote.parse_clean(r.stdout)
        # Named explicitly, a session is forgotten here even when it never started
        # anything remotely — that is how an idle session is cleared. The unnamed
        # sweep only removes what has actually finished.
        forget = set(cleaned) | ({name} if name and name not in busy else set())
        for session_name in forget:
            Session(session_name, workspace_root).forget(r.host.name)
        r.extra["cleaned"] = sorted(forget)
        r.extra["busy"] = busy
        r.stdout = ""

    if as_json:
        print(json.dumps([_json_item(r) for r in results], ensure_ascii=False, indent=2))
    else:
        for r in results:
            if not r.reachable:
                print(f"=== {r.host.label} · UNREACHABLE ===\n  {r.error}")
                continue
            done = r.extra["cleaned"]
            line = f"{r.host.name}: cleared {plural(len(done), 'session')}"
            if done:
                line += f" ({', '.join(done)})"
            if r.extra["busy"]:
                line += f"; still running: {', '.join(r.extra['busy'])}"
            print(line)
    return exit_code(results)


# ------------------------------------------------------------------ logs, stop


def run_logs(
    hosts: list[Host], name: str, jobs: int, limits: Limits, as_json: bool,
    tail: int, max_cols: int,
) -> int:
    try:
        command = remote.job_log(name, tail)
    except remote.PathSpecError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BLOCKED

    results = run_many(hosts, lambda c: c.run(command, limits), jobs=jobs)
    return emit(
        results, as_json,
        hint=f"raise --tail, e.g. uts logs -s {name} --tail 500",
        max_cols=max_cols,
    )


def run_stop(
    hosts: list[Host], name: str, jobs: int, timeout: float, as_json: bool, force: bool
) -> int:
    try:
        command = remote.kill_job(name, force)
    except remote.PathSpecError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BLOCKED

    limits = Limits(max_lines=20, max_bytes=8 << 10, timeout=timeout)
    results = run_many(hosts, lambda c: c.run(command, limits), jobs=jobs)
    return emit(results, as_json, hint="", max_cols=0)


# ------------------------------------------------------------------ rendering


def _render_table(results: list[Result]) -> str:
    rows = []
    for r in results:
        if not r.reachable:
            rows.append(("!", r.host.name, "UNREACHABLE", "", "", r.error or ""))
            continue
        cwds = r.extra.get("cwds", {})
        for job in r.extra.get("jobs", []):
            rows.append((
                job["id"],
                r.host.name,
                _state(job["state"]),
                _elapsed(r.extra.get("now", 0), job["started"]),
                cwds.get(job["id"], "-"),
                job["command"],
            ))
        for name in r.extra.get("idle", []):
            rows.append((name, r.host.name, "idle", "-", cwds.get(name, "-"), "-"))

    if not rows:
        return (
            "nothing running, and no session state.\n"
            "Start one with: uts start -H <name> -s <session> '<command>'"
        )

    headers = ("SESSION", "HOST", "STATE", "ELAPSED", "CWD")
    widths = [max(len(str(row[i])) for row in rows) for i in range(5)]
    widths = [max(w, len(h)) for w, h in zip(widths, headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)) + "  CMD"]
    for row in rows:
        lines.append(
            "  ".join(str(cell).ljust(w) for cell, w in zip(row[:5], widths)) + f"  {row[5]}"
        )
    return "\n".join(lines)


def _state(raw: str) -> str:
    if raw.startswith("exited:"):
        code = raw.split(":", 1)[1]
        return "killed" if code == "143" else f"exited({code})"
    if raw == "vanished":
        # No rc file and no live process. The job traps SIGTERM, so this means
        # SIGKILL, a reboot, or an OOM kill — worth a distinct word, because "not
        # running" is not the same as "finished".
        return "vanished"
    return raw


def _elapsed(now: float, started: float) -> str:
    if not now or not started:
        return "?"
    seconds = max(0, int(now - started))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def _json_item(r: Result) -> dict:
    item = {
        "host": r.host.name,
        "ip": r.host.ip,
        "reachable": r.reachable,
        "error": r.error,
    }
    item.update(r.extra)
    return item
