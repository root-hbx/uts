"""uts push — copy local files to the selected hosts.

The mirror of pull, and it exists because the pair was asymmetric: an agent could
fetch everything off a machine and put nothing back, so "leave a script there and
run it" degenerated into hand-rolled `cat > file <<EOF` through exec.

Same transport reasoning as pull — one tar+gzip stream rather than per-file SFTP —
and the archive is built once locally, then replayed to every host.

Nothing here goes through guard.py. push is a write by definition, so a --write
flag on every invocation would carry no information; the gate that does carry
information is --force, which is only needed when something would be overwritten.
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
    EXIT_BLOCKED, EXIT_OK, EXIT_PARTIAL, exit_code, human_bytes, plural,
)
from ..workspace import Workspace

DEFAULT_MAX_SIZE = "100M"


class PushError(ValueError):
    """The local side of the request is wrong; nothing was sent."""


def run(
    hosts: list[Host],
    paths: list[str],
    jobs: int,
    timeout: float,
    as_json: bool,
    force: bool,
    dry_run: bool,
    max_size: str,
    workspace_root: str | None,
) -> int:
    try:
        if len(paths) < 2:
            raise PushError(
                "push needs at least one source and a destination.\n"
                "  uts push all ./setup.sh '~/bin/'"
            )
        *srcs, dest = paths
        remote.check_dest(dest)
        size_cap = remote.parse_size(max_size)
        files = _collect(srcs)
        total = sum(size for _, _, size in files)
        if total > size_cap:
            raise PushError(
                f"{plural(len(files), 'file')} totalling {human_bytes(total)} exceed the "
                f"{human_bytes(size_cap)} limit. Raise --max-size, or send less."
            )
    except (PushError, remote.PathSpecError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BLOCKED

    ws = Workspace(workspace_root)
    members = [member for _, member, _ in files]

    # Built once, streamed to every host. Compression is the point: a directory of
    # scripts and configs is mostly text.
    archive = None if dry_run else _build_archive(files)
    try:
        def task(conn: Conn) -> Result:
            return _push_one(
                conn, dest, files, members, archive, force, dry_run, timeout, ws, total
            )

        results = run_many(hosts, task, jobs=jobs)
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)

    if as_json:
        from ..output import to_json

        print(to_json(results))
    else:
        print("\n\n".join(_render(r, dry_run) for r in results))

    if not dry_run and any(r.reachable and r.extra.get("pushed") for r in results):
        index = ws.write_index()
        if not as_json:
            print(f"\nworkspace index updated: {index}")

    code = exit_code(results)
    return EXIT_OK if code == EXIT_PARTIAL and dry_run else code


def _collect(srcs: list[str]) -> list[tuple[Path, str, int]]:
    """(local path, archive member, size), following `cp -r` semantics.

    A source file lands directly in the destination; a source directory is
    reproduced under the destination by its own name. Two sources that would land
    on the same remote path are rejected rather than silently resolved — inside a
    tar the second one just wins, with no way to notice.
    """
    out: list[tuple[Path, str, int]] = []
    for raw in srcs:
        p = Path(raw).expanduser()
        if not p.exists():
            raise PushError(f"{raw} does not exist")
        if p.is_dir():
            base = p.name or p.resolve().name
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    out.append((f, f"{base}/{f.relative_to(p).as_posix()}", f.stat().st_size))
        elif p.is_file():
            out.append((p, p.name, p.stat().st_size))
        else:
            raise PushError(f"{raw} is neither a regular file nor a directory")

    if not out:
        raise PushError("nothing to push — the sources matched no files")

    seen: dict[str, Path] = {}
    for local, member, _ in out:
        if member in seen and seen[member] != local:
            raise PushError(
                f"{seen[member]} and {local} would both become {member!r} on the remote "
                f"side. Rename one, or push them in separate runs."
            )
        seen[member] = local
    return out


def _build_archive(files: list[tuple[Path, str, int]]) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    try:
        with tarfile.open(fileobj=tmp, mode="w:gz") as tar:
            for local, member, _ in files:
                # recursive=False: _collect already walked the tree, and letting tar
                # walk it again would add the directory contents twice.
                tar.add(local, arcname=member, recursive=False)
    finally:
        tmp.close()
    return Path(tmp.name)


def _push_one(
    conn: Conn,
    dest: str,
    files: list[tuple[Path, str, int]],
    members: list[str],
    archive: Path | None,
    force: bool,
    dry_run: bool,
    timeout: float,
    ws: Workspace,
    total: int,
) -> Result:
    host = conn.host
    started = time.monotonic()
    extra: dict = {"matched": len(files), "total_bytes": total}

    probe = conn.run(
        remote.push_probe(dest, members),
        Limits(max_lines=len(members) + 20, max_bytes=1 << 20, timeout=timeout),
    )
    if probe.rc != 0 and not probe.stdout.strip():
        return Result(
            host=host, rc=probe.rc, stderr=probe.stderr,
            duration=time.monotonic() - started, extra=extra,
            error=f"could not probe the destination: {probe.stderr.strip() or f'rc={probe.rc}'}",
        )

    info = remote.parse_push_probe(probe.stdout)
    resolved = info["dest"] or dest
    extra["dest"] = resolved
    extra["conflicts"] = info["exists"]

    if info["notdir"]:
        return Result(
            host=host, rc=0, duration=time.monotonic() - started, extra=extra,
            refused=f"{info['notdir']} exists and is not a directory",
        )

    if info["exists"] and not force:
        shown = ", ".join(info["exists"][:5])
        more = f" (+{len(info['exists']) - 5} more)" if len(info["exists"]) > 5 else ""
        return Result(
            host=host, rc=0, duration=time.monotonic() - started, extra=extra,
            refused=(
                f"{plural(len(info['exists']), 'file')} already under {resolved}: "
                f"{shown}{more}. Re-run with --force to overwrite."
            ),
        )

    if dry_run or archive is None:
        return Result(host=host, rc=0, duration=time.monotonic() - started, extra=extra)

    with archive.open("rb") as fh:
        rc, sent, err = conn.stream_stdin(
            remote.untar_stream(dest), fh, timeout=max(timeout, 300.0)
        )
    if rc != 0:
        return Result(
            host=host, rc=rc, stderr=err, duration=time.monotonic() - started, extra=extra,
            error=f"remote unpacking failed (rc={rc}): {err.strip()}",
        )

    for local, member, size in files:
        ws.record({
            "direction": "push",
            "host": host.name,
            "ip": host.ip,
            "remote_path": f"{resolved}/{member}",
            "local_path": str(local),
            "size": size,
            "sha256": ws.sha256(local),
        })

    extra["pushed"] = len(files)
    extra["transfer_bytes"] = sent
    return Result(host=host, rc=0, duration=time.monotonic() - started, extra=extra)


def _render(r: Result, dry_run: bool) -> str:
    if not r.reachable:
        return f"=== {r.host.label} · UNREACHABLE · {r.duration:.2f}s ===\n  {r.error}"
    if r.refused:
        return f"=== {r.host.label} · REFUSED · {r.duration:.2f}s ===\n  {r.refused}"

    e = r.extra
    lines = [f"=== {r.host.label} · {r.duration:.2f}s ==="]
    matched, total = e.get("matched", 0), e.get("total_bytes", 0)

    if dry_run:
        lines.append(
            f"  [dry-run] would send {plural(matched, 'file')} / {human_bytes(total)} "
            f"→ {e.get('dest')}/"
        )
        if e.get("conflicts"):
            lines.append(
                f"  {plural(len(e['conflicts']), 'file')} would be overwritten "
                f"(needs --force): {', '.join(e['conflicts'][:5])}"
            )
    else:
        sent = e.get("transfer_bytes", 0)
        lines.append(
            f"  sent {plural(e.get('pushed', 0), 'file')} / {human_bytes(total)} "
            f"→ {e.get('dest')}/ ({human_bytes(sent)} on the wire)"
        )
        if e.get("conflicts"):
            lines.append(f"  overwrote {plural(len(e['conflicts']), 'file')} (--force)")
    return "\n".join(lines)
