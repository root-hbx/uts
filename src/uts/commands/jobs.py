"""uts jobs / logs / kill — long-running work that outlives the call that started it.

Every exec opens its own channel and closes it again, so a training run started
through uts used to die with the connection. This is the part of "operate" an agent
actually needs: start something on another machine, come back twenty minutes later,
find out how it went.

The state lives in `~/.uts/jobs/<id>/` on the remote host — plain files, written by
the job itself. Nothing is installed and nothing stays resident; `uts jobs --clean`
removes what is finished.
"""

from __future__ import annotations

import json
import sys

from .. import remote
from ..conn import Limits, Result, run_many
from ..inventory import Host
from ..output import EXIT_BLOCKED, emit, exit_code, plural

LIST_LIMITS = Limits(max_lines=2000, max_bytes=1 << 20)


def run_jobs(hosts: list[Host], jobs: int, timeout: float, as_json: bool, clean: bool) -> int:
    if clean:
        return _clean(hosts, jobs, timeout, as_json)

    limits = Limits(max_lines=LIST_LIMITS.max_lines, max_bytes=LIST_LIMITS.max_bytes,
                    timeout=timeout)
    results = run_many(hosts, lambda c: c.run(remote.list_jobs(), limits), jobs=jobs)

    for r in results:
        if r.reachable:
            now, listed = remote.parse_jobs(r.stdout)
            r.extra["now"] = now
            r.extra["jobs"] = listed

    if as_json:
        print(json.dumps([_json_item(r) for r in results], ensure_ascii=False, indent=2))
    else:
        print(_render_table(results))
    return exit_code(results)


def run_logs(
    hosts: list[Host], job_id: str, jobs: int, limits: Limits, as_json: bool,
    tail: int, max_cols: int,
) -> int:
    try:
        remote.check_job_id(job_id)
    except remote.PathSpecError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BLOCKED

    command = remote.job_log(job_id, tail)
    results = run_many(hosts, lambda c: c.run(command, limits), jobs=jobs)
    return emit(
        results, as_json,
        hint=f"raise --tail, or narrow it: uts logs <host> {job_id} --tail 500",
        max_cols=max_cols,
    )


def run_kill(
    hosts: list[Host], job_id: str, jobs: int, timeout: float, as_json: bool, force: bool
) -> int:
    try:
        remote.check_job_id(job_id)
    except remote.PathSpecError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BLOCKED

    command = remote.kill_job(job_id, force)
    limits = Limits(max_lines=20, max_bytes=8 << 10, timeout=timeout)
    results = run_many(hosts, lambda c: c.run(command, limits), jobs=jobs)
    return emit(results, as_json, hint="", max_cols=0)


def _clean(hosts: list[Host], jobs: int, timeout: float, as_json: bool) -> int:
    limits = Limits(max_lines=20, max_bytes=8 << 10, timeout=timeout)
    results = run_many(hosts, lambda c: c.run(remote.clean_jobs(), limits), jobs=jobs)
    for r in results:
        if r.reachable:
            r.extra["cleaned"] = int(r.stdout.strip() or 0)

    if as_json:
        print(json.dumps([_json_item(r) for r in results], ensure_ascii=False, indent=2))
    else:
        for r in results:
            if not r.reachable:
                print(f"=== {r.host.label} · UNREACHABLE ===\n  {r.error}")
            else:
                print(f"{r.host.name}: removed {plural(r.extra['cleaned'], 'finished job')}")
    return exit_code(results)


def _render_table(results: list[Result]) -> str:
    rows = []
    for r in results:
        if not r.reachable:
            rows.append(("!", r.host.name, "UNREACHABLE", "", r.error or ""))
            continue
        for job in r.extra.get("jobs", []):
            rows.append((
                job["id"],
                r.host.name,
                _state(job["state"]),
                _elapsed(r.extra.get("now", 0), job["started"]),
                job["command"],
            ))

    if not rows:
        return "no jobs. Start one with: uts exec <host> --detach -- <command>"

    widths = [max(len(str(row[i])) for row in rows) for i in range(4)]
    widths = [max(w, len(h)) for w, h in zip(widths, ("ID", "HOST", "STATE", "ELAPSED"))]
    header = "  ".join(h.ljust(w) for h, w in zip(("ID", "HOST", "STATE", "ELAPSED"), widths))
    lines = [f"{header}  CMD"]
    for row in rows:
        lines.append(
            "  ".join(str(cell).ljust(w) for cell, w in zip(row[:4], widths)) + f"  {row[4]}"
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


def render_started(r: Result) -> str:
    """One host's answer to `exec --detach`."""
    if not r.reachable:
        return f"=== {r.host.label} · UNREACHABLE · {r.duration:.2f}s ===\n  {r.error}"
    job = r.extra.get("job")
    if not job:
        detail = (r.stderr or r.stdout).strip().splitlines()
        return (
            f"=== {r.host.label} · FAILED TO START · {r.duration:.2f}s ===\n"
            f"  {detail[0] if detail else f'rc={r.rc}'}"
        )
    return (
        f"=== {r.host.label} · job {job} · pid {r.extra.get('pid', '?')} ===\n"
        f"  uts logs {r.host.name} {job}    ·    uts kill {r.host.name} {job}"
    )
