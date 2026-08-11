"""uts pull — fetch remote files into the local workspace.

Stage two of the workflow, and it assumes you already narrowed things down with
ls/peek. It does not judge whether you should fetch something, only fetches fast
and keeps the result traceable.

Transfers go over a single remote tar+gzip stream: logs and numeric matrices both
compress hard, so per-file SFTP wastes time and bandwidth. Everything streams to
disk rather than through memory.

`--to` moves where the files land without changing their shape: the
`<host>/<remote absolute path>` mirror is kept underneath it, because two machines
can hold the same path and flattening them would let one quietly overwrite the
other. The manifest stays in `.uts/` regardless — see workspace.Workspace.
"""

from __future__ import annotations

import sys
import tarfile
import tempfile
import time
from pathlib import Path

from .. import remote
from ..conn import Conn, Limits, Result, run_many
from ..inventory import Host
from ..output import (
    EXIT_BLOCKED, EXIT_OK, EXIT_PARTIAL, exit_code, human_bytes, human_time, plural,
)
from ..workspace import Workspace

DEFAULT_MAX_SIZE = "100M"
LIST_CAP = 5000
HEAD_MODE_MAX_FILES = 50


def run(
    hosts: list[Host],
    spec: str,
    jobs: int,
    timeout: float,
    as_json: bool,
    max_size: str,
    since: str | None,
    lines: int | None,
    dry_run: bool,
    workspace_root: str | None,
    to: str | None = None,
) -> int:
    try:
        remote.check_path_spec(spec)
        size_cap = remote.parse_size(max_size)
        cutoff = time.time() - remote.parse_since(since) if since else None
    except (remote.PathSpecError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BLOCKED

    ws = Workspace(workspace_root, data_root=to)

    def task(conn: Conn) -> Result:
        return _pull_one(conn, spec, size_cap, cutoff, lines, timeout, dry_run, ws)

    results = run_many(hosts, task, jobs=jobs)

    if as_json:
        from ..output import to_json

        print(to_json(results))
    else:
        print("\n\n".join(_render(r, dry_run) for r in results))

    if not dry_run and any(r.reachable and r.extra.get("pulled") for r in results):
        index = ws.write_index()
        if not as_json:
            print(f"\nworkspace index updated: {index}")

    code = exit_code(results)
    return EXIT_OK if code == EXIT_PARTIAL and dry_run else code


def _pull_one(
    conn: Conn,
    spec: str,
    size_cap: int,
    cutoff: float | None,
    lines: int | None,
    timeout: float,
    dry_run: bool,
    ws: Workspace,
) -> Result:
    host = conn.host
    started = time.monotonic()

    # Enumerate first. The size decision has to happen before any transfer.
    listing = conn.run(
        remote.list_files(spec, cap=LIST_CAP),
        Limits(max_lines=LIST_CAP + 10, max_bytes=4 << 20, timeout=timeout),
    )
    if listing.rc != 0 and not listing.stdout.strip():
        return Result(
            host=host, rc=listing.rc, stderr=listing.stderr,
            duration=time.monotonic() - started,
            error=f"could not list remote files: {listing.stderr.strip() or f'rc={listing.rc}'}",
        )

    files = remote.parse_listing(listing.stdout)
    skipped_old = 0
    if cutoff is not None:
        before = len(files)
        files = [f for f in files if f["mtime"] >= cutoff]
        skipped_old = before - len(files)

    total = sum(f["size"] for f in files)
    extra = {
        "matched": len(files),
        "total_bytes": total,
        "skipped_old": skipped_old,
        "at_list_cap": len(remote.parse_listing(listing.stdout)) >= LIST_CAP,
    }

    if not files:
        return Result(host=host, rc=0, duration=time.monotonic() - started, extra=extra)

    if total > size_cap and lines is None:
        return Result(
            host=host, rc=0, duration=time.monotonic() - started, extra=extra,
            refused=(
                f"{plural(len(files), 'file')} totalling {human_bytes(total)} exceed the "
                f"{human_bytes(size_cap)} limit. Raise --max-size, or narrow with "
                f"--since / --lines / a tighter path."
            ),
        )

    if dry_run:
        extra["files"] = files[:20]
        return Result(host=host, rc=0, duration=time.monotonic() - started, extra=extra)

    if lines is not None and len(files) > HEAD_MODE_MAX_FILES:
        return Result(
            host=host, rc=0, duration=time.monotonic() - started, extra=extra,
            refused=(
                f"--lines mode handles at most {HEAD_MODE_MAX_FILES} files per run "
                f"(each is headed separately) and {len(files)} matched. "
                f"Narrow with a tighter path or --since."
            ),
        )

    pulled = (
        _pull_head(conn, host, files, lines, timeout, ws)
        if lines is not None
        else _pull_tar(conn, host, files, timeout, ws, size_cap)
    )
    extra["pulled"] = pulled
    extra["dest"] = str(ws.host_root(host.name))
    return Result(host=host, rc=0, duration=time.monotonic() - started, extra=extra)


def _pull_tar(
    conn: Conn, host: Host, files: list[dict], timeout: float, ws: Workspace, size_cap: int
) -> int:
    dest_root = ws.host_root(host.name)
    dest_root.mkdir(parents=True, exist_ok=True)
    paths = [f["path"] for f in files]

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        rc, written, err = conn.stream_stdout(
            remote.tar_stream(paths),
            tmp,
            timeout=max(timeout, 300.0),
            max_bytes=size_cap * 2,  # compressed should be far smaller; this is a runaway guard
        )

    try:
        if rc != 0 and written == 0:
            raise RuntimeError(f"remote archiving failed (rc={rc}): {err.strip()}")
        with tarfile.open(tmp_path, "r:gz") as tar:
            tar.extractall(dest_root, filter="data")
    finally:
        tmp_path.unlink(missing_ok=True)

    for f in files:
        local = ws.path_for(host.name, f["path"])
        ws.record({
            "direction": "pull",
            "host": host.name,
            "ip": host.ip,
            "remote_path": f["path"],
            "local_path": str(local),
            "size": f["size"],
            "remote_mtime": f["mtime"],
            "sha256": ws.sha256(local) if local.exists() else None,
            "truncated": False,
            "transfer_bytes": written if f is files[0] else None,
        })
    return len(files)


def _pull_head(
    conn: Conn, host: Host, files: list[dict], lines: int, timeout: float, ws: Workspace
) -> int:
    """--lines mode: head each file separately (this mode implies few files)."""
    pulled = 0
    for f in files:
        res = conn.run(
            remote.head_file(f["path"], lines),
            Limits(max_lines=lines + 10, max_bytes=64 << 20, timeout=timeout),
        )
        if res.rc != 0:
            continue
        local = ws.path_for(host.name, f["path"])
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(res.stdout, encoding="utf-8")
        ws.record({
            "direction": "pull",
            "host": host.name,
            "ip": host.ip,
            "remote_path": f["path"],
            "local_path": str(local),
            "size": local.stat().st_size,
            "remote_size": f["size"],
            "remote_mtime": f["mtime"],
            "sha256": ws.sha256(local),
            "truncated": True,
            "lines": lines,
        })
        pulled += 1
    return pulled


def _render(r: Result, dry_run: bool) -> str:
    if not r.reachable:
        return f"=== {r.host.label} · UNREACHABLE · {r.duration:.2f}s ===\n  {r.error}"
    if r.refused:
        return f"=== {r.host.label} · REFUSED · {r.duration:.2f}s ===\n  {r.refused}"

    e = r.extra
    head = f"=== {r.host.label} · {r.duration:.2f}s ==="
    lines = [head]
    matched, total = e.get("matched", 0), e.get("total_bytes", 0)

    if matched == 0:
        lines.append("  no files matched")
        if e.get("skipped_old"):
            lines.append(f"  ({e['skipped_old']} excluded by --since)")
        return "\n".join(lines)

    lines.append(f"  matched {plural(matched, 'file')} / {human_bytes(total)}")
    if e.get("skipped_old"):
        lines.append(f"  --since excluded {e['skipped_old']} older files")
    if e.get("at_list_cap"):
        lines.append(f"  ⚠ listing hit the {LIST_CAP} cap — narrow the path before pulling")

    if dry_run:
        lines.append("  [dry-run] nothing was transferred. Sample:")
        for f in e.get("files", [])[:10]:
            lines.append(f"    {human_bytes(f['size']):>8}  {human_time(f['mtime'])}  {f['path']}")
        if matched > 10:
            lines.append(f"    … {matched - 10} more")
    else:
        lines.append(f"  wrote {plural(e.get('pulled', 0), 'file')} → {e.get('dest')}/")
    return "\n".join(lines)
