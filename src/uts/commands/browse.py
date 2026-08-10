"""uts ls / uts find — finding your way around.

`ls` prints a *summary*, not a dump of `ls -la`. In practice a directory fills the
screen with 130 near-identical entries while the only useful information is how
many, how big, what types, and how recent — so print exactly that.

`find` is the case where you do want the entries listed, so it has a limit and a
sort order.
"""

from __future__ import annotations

import fnmatch
import sys
import time
from collections import defaultdict
from pathlib import PurePosixPath

from .. import remote
from ..conn import Conn, Limits, Result, run_many
from ..inventory import Host
from ..output import EXIT_BLOCKED, exit_code, human_bytes, human_time, plural

LIST_CAP = 20000


def _collect(conn: Conn, spec: str, timeout: float) -> list[dict]:
    listing = conn.run(
        remote.list_files(spec, cap=LIST_CAP),
        Limits(max_lines=LIST_CAP + 10, max_bytes=16 << 20, timeout=timeout),
    )
    return remote.parse_listing(listing.stdout)


def _filter(
    files: list[dict],
    name: str | None,
    since_seconds: float | None,
    min_size: int | None,
) -> tuple[list[dict], dict]:
    dropped = {"name": 0, "since": 0, "size": 0}
    cutoff = time.time() - since_seconds if since_seconds else None
    kept = []
    for f in files:
        if name and not fnmatch.fnmatch(PurePosixPath(f["path"]).name, name):
            dropped["name"] += 1
            continue
        if cutoff is not None and f["mtime"] < cutoff:
            dropped["since"] += 1
            continue
        if min_size is not None and f["size"] < min_size:
            dropped["size"] += 1
            continue
        kept.append(f)
    return kept, dropped


# ------------------------------------------------------------------------ ls


def run_ls(hosts: list[Host], spec: str, jobs: int, timeout: float, as_json: bool) -> int:
    try:
        remote.check_path_spec(spec)
    except remote.PathSpecError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BLOCKED

    def task(conn: Conn) -> Result:
        started = time.monotonic()
        files = _collect(conn, spec, timeout)
        return Result(
            host=conn.host, rc=0, duration=time.monotonic() - started,
            extra={"summary": _summarize(files), "at_cap": len(files) >= LIST_CAP},
        )

    results = run_many(hosts, task, jobs=jobs)
    if as_json:
        from ..output import to_json

        print(to_json(results))
    else:
        print("\n\n".join(_render_ls(r) for r in results))
    return exit_code(results)


def _summarize(files: list[dict]) -> dict:
    if not files:
        return {"count": 0}
    by_ext: dict[str, list[int]] = defaultdict(list)
    for f in files:
        ext = PurePosixPath(f["path"]).suffix.lower() or "(no extension)"
        by_ext[ext].append(f["size"])
    newest = max(files, key=lambda f: f["mtime"])
    oldest = min(files, key=lambda f: f["mtime"])
    biggest = sorted(files, key=lambda f: -f["size"])[:5]
    return {
        "count": len(files),
        "total_bytes": sum(f["size"] for f in files),
        "by_ext": sorted(
            ((ext, len(sizes), sum(sizes)) for ext, sizes in by_ext.items()),
            key=lambda t: -t[2],
        ),
        "newest": newest,
        "oldest": oldest,
        "biggest": biggest,
        "mtime_days": (newest["mtime"] - oldest["mtime"]) / 86400,
    }


def _render_ls(r: Result) -> str:
    if not r.reachable:
        return f"=== {r.host.label} · UNREACHABLE · {r.duration:.2f}s ===\n  {r.error}"
    s = r.extra["summary"]
    out = [f"=== {r.host.label} · {r.duration:.2f}s ==="]
    if not s["count"]:
        out.append("  no files matched")
        return "\n".join(out)

    out.append(f"  {plural(s['count'], 'file')} / {human_bytes(s['total_bytes'])}")
    if r.extra.get("at_cap"):
        out.append(f"  ⚠ listing hit the {LIST_CAP} cap, there may be more")

    out.append("")
    out.append("  types")
    for ext, n, total in s["by_ext"][:8]:
        out.append(f"    {ext:<16} {n:>6}  {human_bytes(total):>9}")
    if len(s["by_ext"]) > 8:
        out.append(f"    … {len(s['by_ext']) - 8} more types")

    out.append("")
    out.append(f"  newest  {human_time(s['newest']['mtime'])}  {s['newest']['path']}")
    out.append(f"  oldest  {human_time(s['oldest']['mtime'])}  {s['oldest']['path']}")
    if s["mtime_days"] >= 1:
        out.append(
            f"  span    {s['mtime_days']:.1f} days — a wide span usually means several "
            f"runs are mixed together"
        )

    out.append("")
    out.append("  biggest")
    for f in s["biggest"]:
        out.append(f"    {human_bytes(f['size']):>9}  {human_time(f['mtime'])}  {f['path']}")
    return "\n".join(out)


# ---------------------------------------------------------------------- find


def run_find(
    hosts: list[Host],
    spec: str,
    jobs: int,
    timeout: float,
    as_json: bool,
    name: str | None,
    since: str | None,
    min_size: str | None,
    limit: int,
    sort: str,
) -> int:
    try:
        remote.check_path_spec(spec)
        since_seconds = remote.parse_since(since) if since else None
        size_floor = remote.parse_size(min_size) if min_size else None
    except (remote.PathSpecError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BLOCKED

    def task(conn: Conn) -> Result:
        started = time.monotonic()
        files = _collect(conn, spec, timeout)
        kept, dropped = _filter(files, name, since_seconds, size_floor)
        kept.sort(key=(lambda f: -f["mtime"]) if sort == "time" else (lambda f: -f["size"]))
        return Result(
            host=conn.host, rc=0, duration=time.monotonic() - started,
            extra={
                "scanned": len(files),
                "matched": len(kept),
                "dropped": dropped,
                "total_bytes": sum(f["size"] for f in kept),
                "files": kept[:limit],
                "limit": limit,
            },
        )

    results = run_many(hosts, task, jobs=jobs)
    if as_json:
        from ..output import to_json

        print(to_json(results))
    else:
        print("\n\n".join(_render_find(r) for r in results))
    return exit_code(results)


def _render_find(r: Result) -> str:
    if not r.reachable:
        return f"=== {r.host.label} · UNREACHABLE · {r.duration:.2f}s ===\n  {r.error}"
    e = r.extra
    out = [f"=== {r.host.label} · {r.duration:.2f}s ==="]
    out.append(
        f"  scanned {e['scanned']}, matched {e['matched']} / {human_bytes(e['total_bytes'])}"
    )
    d = e["dropped"]
    excluded = [f"{k}={v}" for k, v in (("name", d["name"]), ("since", d["since"]),
                                        ("size", d["size"])) if v]
    if excluded:
        out.append(f"  excluded: {', '.join(excluded)}")
    if not e["files"]:
        out.append("  nothing matched")
        return "\n".join(out)

    out.append("")
    for f in e["files"]:
        out.append(f"    {human_bytes(f['size']):>9}  {human_time(f['mtime'])}  {f['path']}")
    if e["matched"] > len(e["files"]):
        out.append(f"    … {e['matched'] - len(e['files'])} more (raise --limit)")
    return "\n".join(out)
