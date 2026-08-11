"""Render fan-out results and decide the exit code.

Everything here is tuned for an LLM reader: one block per host, failures are never
swallowed, and truncation always leaves a visible marker — otherwise the model
treats a truncated output as the whole truth.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from .conn import Result

EXIT_OK = 0
EXIT_REMOTE_NONZERO = 1  # all hosts reachable, remote command returned non-zero
EXIT_PARTIAL = 2  # some hosts unreachable or refused
EXIT_ALL_FAILED = 3  # every host unreachable, or the inventory itself is broken
EXIT_BLOCKED = 4  # guard / usage error / size cap — nothing was sent

# Bumped only when the envelope below changes shape. An agent that reads this can
# tell "uts is older than I expect" from "the command failed".
CONTRACT = 1

# Why a request never reached the network. Each one points at a different fix:
#   usage      — the invocation is malformed; correct the arguments
#   blocked    — guard refused a destructive command; pass --write if it was meant
#   inventory  — host selection or hosts.json is wrong; check `uts hosts`
ERROR_KINDS = ("usage", "blocked", "inventory")

DEFAULT_HINT = "raise --max-lines, or narrow the query on the remote side"

# Line caps do nothing for *wide* lines: a 104-column delay matrix packs 4KB of
# unreadable digits into 8 lines. Column truncation is what's actually needed.
DEFAULT_MAX_COLS = 200


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def human_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def fold_wide_lines(text: str, max_cols: int = DEFAULT_MAX_COLS) -> str:
    """Cut lines at max_cols, noting what's left. max_cols <= 0 disables it."""
    if max_cols <= 0:
        return text
    folded = []
    for line in text.split("\n"):
        if len(line) > max_cols:
            folded.append(f"{line[:max_cols]} … [{len(line) - max_cols:,} more chars on this line]")
        else:
            folded.append(line)
    return "\n".join(folded)


def truncation_note(result: Result, hint: str = DEFAULT_HINT) -> str | None:
    if result.aborted:
        return (
            f"… [output passed the hard limit, transfer aborted, exit code unknown "
            f"— don't cat large files; {hint}]"
        )
    if not result.truncated:
        return None
    parts = []
    if result.dropped_lines:
        parts.append(f"{result.dropped_lines:,} more lines")
    if result.dropped_bytes:
        parts.append(human_bytes(result.dropped_bytes))
    return f"… [truncated: {' / '.join(parts)} not shown — {hint}]"


def _head_tags(r: Result) -> list[str]:
    """The block header. Anything that changes how the output should be read belongs
    here rather than buried in the body — a session's cwd decides what a relative
    path meant, and a PTY silently folds stderr into stdout."""
    tags = [r.host.label, f"rc={r.rc}", f"{r.duration:.2f}s"]
    if r.extra.get("session"):
        tags.append(f"session={r.extra['session']}")
        if r.extra.get("cwd"):
            tags.append(f"cwd {r.extra['cwd']}")
    if r.extra.get("pty"):
        tags.append("pty · stderr merged into stdout")
    return tags


def render(
    results: list[Result],
    hint: str = DEFAULT_HINT,
    max_cols: int = DEFAULT_MAX_COLS,
) -> str:
    blocks = []
    for r in results:
        if not r.reachable:
            blocks.append(
                f"=== {r.host.label} · UNREACHABLE · {r.duration:.2f}s ===\n  {r.error}"
            )
            continue

        head = "=== " + " · ".join(_head_tags(r)) + " ==="
        body: list[str] = [f"  {line}" for line in r.extra.get("notes", [])]
        stdout = fold_wide_lines(r.stdout.rstrip("\n"), max_cols)
        if stdout:
            body.append(stdout)
        stderr = r.stderr.rstrip("\n")
        if stderr:
            body.append("\n".join(f"  [stderr] {line}" for line in stderr.splitlines()))
        note = truncation_note(r, hint)
        if note:
            body.append(note)
        if not body:
            body.append("  (no output)")
        blocks.append(head + "\n" + "\n".join(body))
    return "\n\n".join(blocks)


def host_items(results: list[Result]) -> list[dict]:
    """One object per host: the Result fields, with `extra` flattened on top.

    `extra` is splatted rather than nested because it is where each subcommand puts
    its own answer — `ls` its summary, `ps` its jobs — and one flat object per host
    reads the same way whichever subcommand produced it.
    """
    payload = []
    for r in results:
        item = {
            "host": r.host.name,
            "ip": r.host.ip,
            "user": r.host.user,
            "reachable": r.reachable,
            "rc": r.rc,
            "duration": round(r.duration, 3),
            "stdout": r.stdout,
            "stderr": r.stderr,
            "truncated": r.truncated,
            "aborted": r.aborted,
            "dropped_lines": r.dropped_lines,
            "dropped_bytes": r.dropped_bytes,
            "error": r.error,
            "refused": r.refused,
        }
        if r.extra:
            item.update(r.extra)
        payload.append(item)
    return payload


def envelope(
    command: str | None,
    code: int,
    hosts: list[dict],
    *,
    error: dict | None = None,
    **fields,
) -> str:
    """The one shape every `--json` invocation returns, success or failure.

    Success used to be a bare array and failure was prose on stderr, which left a
    caller with "exit 4 and empty stdout" and nothing to read. One envelope means a
    consumer parses the same keys either way and finds out *why* from `error.kind`.
    """
    payload = {
        "uts": CONTRACT,
        "command": command,
        "ok": code == EXIT_OK,
        "exit": code,
    }
    if error is not None:
        payload["error"] = error
    payload.update(fields)
    payload["hosts"] = hosts
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_json(results: list[Result], command: str, *, code: int | None = None) -> str:
    return envelope(command, exit_code(results) if code is None else code,
                    host_items(results))


def fail(
    message: str, code: int, as_json: bool, *, command: str | None, kind: str
) -> int:
    """The only way out of a request that never reached the network.

    Every such path used to `print(..., file=sys.stderr)` and return a code, so under
    --json the caller got valid-looking silence. Routing them all through here is what
    makes the contract "there is always JSON on stdout" rather than "usually".
    """
    if as_json:
        print(envelope(command, code, [], error={"kind": kind, "message": message}))
    else:
        print(message, file=sys.stderr)
    return code


def exit_code(results: list[Result]) -> int:
    """Unreachable, refused, and non-zero rc must stay distinguishable to callers."""
    if not results:
        return EXIT_ALL_FAILED
    unreachable = [r for r in results if not r.reachable]
    refused = [r for r in results if r.reachable and r.refused]

    if len(unreachable) == len(results):
        return EXIT_ALL_FAILED
    if len(refused) == len(results):
        return EXIT_BLOCKED
    if unreachable or refused:
        return EXIT_PARTIAL
    if any(r.rc != 0 for r in results):
        return EXIT_REMOTE_NONZERO
    return EXIT_OK


def emit(
    results: list[Result],
    as_json: bool,
    *,
    command: str,
    hint: str = DEFAULT_HINT,
    max_cols: int = DEFAULT_MAX_COLS,
) -> int:
    code = exit_code(results)
    # No column folding under --json: that output feeds programs, and the byte cap
    # already bounds its size.
    print(to_json(results, command, code=code) if as_json
          else render(results, hint, max_cols))
    if code in (EXIT_PARTIAL, EXIT_ALL_FAILED) and not as_json:
        bad = [r.host.name for r in results if not r.reachable]
        print(f"\n{len(bad)}/{len(results)} unreachable: {', '.join(bad)}", file=sys.stderr)
    return code
