"""uts status — reachability, machine profile, clock skew.

Clock skew is the easily forgotten part that matters most: when correlating
timelines across machines, an unnoticed four-minute offset reverses the causal
order you infer. So it is measured every time and warned about past a threshold.
"""

from __future__ import annotations

from ..conn import Limits, Result, run_many
from ..inventory import Host
from ..output import envelope, exit_code
from ..remote import LOGDIR_PROBE_CAP, facts, parse_facts

SKEW_WARN_SECONDS = 5.0


def run(hosts: list[Host], jobs: int, timeout: float, as_json: bool) -> int:
    limits = Limits(max_lines=200, max_bytes=64 * 1024, timeout=timeout)
    results = run_many(hosts, lambda c: c.run(facts(), limits), jobs=jobs)

    for r in results:
        if r.reachable:
            r.extra["facts"] = parse_facts(r.stdout)
            r.extra["skew"] = _skew(r)

    code = exit_code(results)
    if as_json:
        print(envelope("status", code, [_json_item(r) for r in results]))
    else:
        print("\n\n".join(_render(r) for r in results))
    return code


def _skew(r: Result) -> float | None:
    """Remote clock minus local, in seconds, referenced to the midpoint of the exec
    window — accurate to a few hundred milliseconds."""
    raw = r.extra.get("facts", {}).get("epoch")
    if not raw:
        return None
    try:
        remote_epoch = float(raw)
    except ValueError:
        return None
    local_mid = (r.wall_start + r.wall_end) / 2
    return remote_epoch - local_mid


def _fmt_skew(skew: float | None) -> str:
    if skew is None:
        return "?"
    sign = "+" if skew >= 0 else "-"
    mag = abs(skew)
    base = f"{sign}{mag:.1f}s" if mag < 60 else f"{sign}{mag / 60:.1f}min"
    if mag <= SKEW_WARN_SECONDS:
        return f"{base} (matches this machine)"
    direction = "ahead of" if skew > 0 else "behind"
    return f"{base} ⚠ {direction} this machine — correct for it before comparing timelines"


def _render(r: Result) -> str:
    if not r.reachable:
        return f"=== {r.host.label} · UNREACHABLE · {r.duration:.2f}s ===\n  {r.error}"

    f = r.extra.get("facts", {})
    lines = [f"=== {r.host.label} · OK · {r.duration:.2f}s ==="]

    def row(key: str, value: str | None) -> None:
        if value:
            lines.append(f"  {key:<9}{value}")

    row("host", f.get("hostname"))
    row("os", f.get("os"))
    row("kernel", f.get("kernel"))
    hw = " · ".join(
        p for p in (f.get("arch"), _suffix(f.get("cpus"), " cpu"), _prefix(f.get("mem"), "mem ")) if p
    )
    row("hw", hw)
    row("uptime", " · ".join(p for p in (f.get("uptime"), _prefix(f.get("load"), "load ")) if p))
    row("time", f.get("time"))
    row("clock", _fmt_skew(r.extra.get("skew")))

    disk = f.get("disk") or []
    for i, line in enumerate(disk):
        lines.append(f"  {'disk' if i == 0 else '':<9}{line}")

    logdirs = f.get("logdirs") or []
    if logdirs:
        # The probe stops counting at LOGDIR_PROBE_CAP, so hitting it must read as
        # 500+ rather than an exact 500.
        summary = " · ".join(
            f"{p} ({n}+ log files)" if n == str(LOGDIR_PROBE_CAP) else f"{p} ({n} log files)"
            for p, n in logdirs
        )
        row("logs", summary)
    else:
        row("logs", "no *.log in the usual directories — look around with uts find")

    if r.stderr.strip():
        lines.append(f"  [stderr] {r.stderr.strip().splitlines()[0]}")
    return "\n".join(lines)


def _prefix(value: str | None, prefix: str) -> str | None:
    return f"{prefix}{value}" if value else None


def _suffix(value: str | None, suffix: str) -> str | None:
    return f"{value}{suffix}" if value else None


def _json_item(r: Result) -> dict:
    item = {
        "host": r.host.name,
        "ip": r.host.ip,
        "reachable": r.reachable,
        "duration": round(r.duration, 3),
        "error": r.error,
    }
    if r.reachable:
        f = dict(r.extra.get("facts", {}))
        # Text mode renders the cap as 500+; JSON needs an explicit flag so consumers
        # don't read a capped count as exact.
        f["logdirs"] = [
            {"path": p, "log_files": int(n), "at_probe_cap": int(n) >= LOGDIR_PROBE_CAP}
            for p, n in f.get("logdirs", [])
        ]
        item["facts"] = f
        skew = r.extra.get("skew")
        item["clock_skew_seconds"] = round(skew, 2) if skew is not None else None
    return item
