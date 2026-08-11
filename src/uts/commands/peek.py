"""uts peek — see what the data looks like without fetching it.

The key choice: by default this profiles a *batch* of files rather than heading a
single one. A directory of 120 .txt files looks perfectly fine one file at a time;
only lining up all 120 line/column counts reveals that 15 of them have different
dimensions — two runs mixed into one directory. A single-file preview can never
surface that, and downstream time-series loading fails silently on it.
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import PurePosixPath

from .. import remote
from ..conn import Conn, Limits, Result, run_many
from ..inventory import Host
from ..output import (
    EXIT_BLOCKED, exit_code, fail, fold_wide_lines, human_bytes, plural, to_json,
)

LIST_CAP = 5000

_DELIM_NAME = {
    ",": "csv", "\t": "tsv", ";": "semicolon", "|": "pipe", None: "single column / free text",
}


def run(
    hosts: list[Host],
    spec: str,
    jobs: int,
    timeout: float,
    as_json: bool,
    sample_lines: int,
    max_cols: int,
    max_files: int,
) -> int:
    try:
        remote.check_path_spec(spec)
    except remote.PathSpecError as exc:
        return fail(str(exc), EXIT_BLOCKED, as_json, command="peek", kind="usage")

    results = run_many(
        hosts,
        lambda c: _peek_one(c, spec, timeout, sample_lines, max_files),
        jobs=jobs,
    )

    code = exit_code(results)
    if as_json:
        print(to_json(results, "peek", code=code))
    else:
        print("\n\n".join(_render(r, max_cols) for r in results))
    return code


def _peek_one(
    conn: Conn, spec: str, timeout: float, sample_lines: int, max_files: int
) -> Result:
    host = conn.host
    started = time.monotonic()

    listing = conn.run(
        remote.list_files(spec, cap=LIST_CAP),
        Limits(max_lines=LIST_CAP + 10, max_bytes=4 << 20, timeout=timeout),
    )
    files = remote.parse_listing(listing.stdout)
    if not files:
        return Result(
            host=host, rc=0, duration=time.monotonic() - started,
            extra={"matched": 0, "stderr": listing.stderr.strip()},
        )

    probed = files[:max_files]
    shapes = remote.parse_shape(
        conn.run(
            remote.shape_probe([f["path"] for f in probed]),
            Limits(max_lines=max_files * 2 + 10, max_bytes=4 << 20, timeout=max(timeout, 120.0)),
        ).stdout
    )

    sample_path = probed[0]["path"] if probed else None
    sample = ""
    if sample_path:
        sample = conn.run(
            remote.head_file(sample_path, sample_lines),
            Limits(max_lines=sample_lines + 2, max_bytes=256 << 10, timeout=timeout),
        ).stdout

    sizes = {f["path"]: f["size"] for f in files}
    return Result(
        host=host,
        rc=0,
        duration=time.monotonic() - started,
        extra={
            "matched": len(files),
            "total_bytes": sum(sizes.values()),
            "probed": len(probed),
            "at_list_cap": len(files) >= LIST_CAP,
            "shapes": shapes,
            "sample_path": sample_path,
            "sample": sample,
        },
    )


def _group_shapes(shapes: list[dict]) -> list[tuple[tuple, list[dict]]]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for s in shapes:
        groups[(s["lines"], s["columns"], s["delimiter"])].append(s)
    return sorted(groups.items(), key=lambda kv: -len(kv[1]))


def _name_range(items: list[dict]) -> str:
    """Sort by the number in the filename and show a compact range — far more useful
    than printing 105 paths."""
    def key(s):
        stem = PurePosixPath(s["path"]).stem
        return (0, int(stem)) if stem.lstrip("-").isdigit() else (1, 0)

    names = [PurePosixPath(s["path"]).name for s in sorted(items, key=key)]
    if len(names) <= 4:
        return ", ".join(names)
    return f"{names[0]}, {names[1]}, {names[2]}, … {names[-1]}"


def _render(r: Result, max_cols: int) -> str:
    if not r.reachable:
        return f"=== {r.host.label} · UNREACHABLE · {r.duration:.2f}s ===\n  {r.error}"

    e = r.extra
    out = [f"=== {r.host.label} · {r.duration:.2f}s ==="]
    if not e.get("matched"):
        out.append("  no files matched")
        if e.get("stderr"):
            out.append(f"  [stderr] {e['stderr']}")
        return "\n".join(out)

    out.append(f"  matched {plural(e['matched'], 'file')} / {human_bytes(e['total_bytes'])}")
    if e.get("at_list_cap"):
        out.append(f"  ⚠ listing hit the {LIST_CAP} cap, there may be more")
    if e["probed"] < e["matched"]:
        out.append(f"  shapes probed for the first {e['probed']} only (raise --max-files)")

    groups = _group_shapes(e.get("shapes", []))
    if groups:
        out.append("")
        out.append("  shapes (rows × cols → files)")
        for (nlines, ncols, delim), items in groups:
            kind = _DELIM_NAME.get(delim, repr(delim))
            out.append(
                f"    {nlines:>6} × {ncols:<6} {kind:<24} {len(items):>4}   {_name_range(items)}"
            )
        if len(groups) > 1:
            out.append(
                "    ⚠ shapes disagree — loading these by filename as a time series will "
                "jump dimensions; most likely several runs share this directory"
            )

    if e.get("sample"):
        out.append("")
        out.append(f"  sample {e['sample_path']}")
        for line in fold_wide_lines(e["sample"].rstrip("\n"), max_cols).splitlines():
            out.append(f"    {line}")
    return "\n".join(out)
