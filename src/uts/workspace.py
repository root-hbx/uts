"""Local workspace: where pulled data lands, and where it came from.

Two rules:

1. The local path mirrors the remote one (`.uts/<host>/<remote absolute path>`),
   so the path itself records the provenance.
2. Every pull is recorded in the manifest. Without it there is no answer to "when
   was this fetched, has the remote changed, should I re-pull" — questions that
   come up every time the workspace is reused.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .output import human_bytes, human_time, plural

WORKSPACE_DIR = ".uts"
MANIFEST = "manifest.jsonl"
INDEX = "INDEX.md"


class Workspace:
    """Where pulled files land, and the manifest that says where they came from.

    `data_root` splits off only where the *files* go, for `pull --to`. The manifest
    and the index stay in `.uts/` either way: one record of everything fetched is
    worth more than a record that follows the files around, and a `--to` pull still
    has to be answerable by "when did this arrive, from which machine".
    """

    def __init__(self, root: str | Path | None = None, data_root: str | Path | None = None):
        self.root = Path(root or WORKSPACE_DIR)
        self.data_root = Path(data_root).expanduser() if data_root else self.root

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST

    @property
    def index_path(self) -> Path:
        return self.root / INDEX

    def host_root(self, host: str) -> Path:
        return self.data_root / host

    def path_for(self, host: str, remote_path: str) -> Path:
        rel = remote_path.lstrip("/")
        return self.host_root(host) / rel

    def record(self, entry: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        entry = {"fetched_at": time.time(), **entry}
        with self.manifest_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self) -> list[dict]:
        if not self.manifest_path.exists():
            return []
        out = []
        for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def latest_per_file(self) -> dict[tuple[str, str, str], dict]:
        """A path may be transferred repeatedly; keep only the last one.

        Direction is part of the key: the same remote path can legitimately be both
        somewhere we pushed to and somewhere we pulled from, and collapsing the two
        would make the index claim one of them never happened. Entries written
        before push existed carry no `direction` and are read as pulls.
        """
        latest: dict[tuple[str, str, str], dict] = {}
        for e in self.entries():
            key = (e.get("host", "?"), e.get("direction", "pull"), e.get("remote_path", "?"))
            prev = latest.get(key)
            if prev is None or e.get("fetched_at", 0) >= prev.get("fetched_at", 0):
                latest[key] = e
        return latest

    def write_index(self) -> Path:
        entries = self.latest_per_file()
        self.root.mkdir(parents=True, exist_ok=True)

        lines = [
            "# uts workspace",
            "",
            f"Last updated {human_time(time.time())}. "
            f"Rebuilt automatically by `uts pull` — do not edit by hand.",
            "",
            "Local paths mirror remote ones: `.uts/<host>/<remote absolute path>`.",
            "",
        ]
        if not entries:
            lines += ["Workspace is empty. Fetch something with `uts pull <host> <path>`.", ""]
            self.index_path.write_text("\n".join(lines), encoding="utf-8")
            return self.index_path

        by_host: dict[str, dict[str, list[dict]]] = {}
        for (host, direction, _), e in entries.items():
            by_host.setdefault(host, {}).setdefault(direction, []).append(e)

        for host in sorted(by_host):
            for direction in ("pull", "push"):
                items = sorted(
                    by_host[host].get(direction, []), key=lambda e: e.get("remote_path", "")
                )
                if not items:
                    continue
                total = sum(e.get("size", 0) for e in items)
                verb = "fetched from" if direction == "pull" else "sent to"
                lines.append(
                    f"## {host} — {plural(len(items), 'file')} {verb} it / {human_bytes(total)}"
                )
                lines.append("")
                lines.append(
                    "| Remote path | Size | Remote mtime | Fetched | Notes |"
                    if direction == "pull"
                    else "| Remote path | Size | Local source | Sent | Notes |"
                )
                lines.append("|---|---|---|---|---|")
                for e in items:
                    note = []
                    if e.get("truncated"):
                        note.append(f"first {e.get('lines')} lines only")
                    third = (
                        human_time(e["remote_mtime"]) if e.get("remote_mtime") else "?"
                    ) if direction == "pull" else f"`{e.get('local_path', '?')}`"
                    lines.append(
                        f"| `{e.get('remote_path', '?')}` "
                        f"| {human_bytes(e.get('size', 0))} "
                        f"| {third} "
                        f"| {human_time(e.get('fetched_at', 0))} "
                        f"| {' / '.join(note)} |"
                    )
                lines.append("")

        self.index_path.write_text("\n".join(lines), encoding="utf-8")
        return self.index_path

    def sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
