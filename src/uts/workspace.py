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
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or WORKSPACE_DIR)

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST

    @property
    def index_path(self) -> Path:
        return self.root / INDEX

    def path_for(self, host: str, remote_path: str) -> Path:
        rel = remote_path.lstrip("/")
        return self.root / host / rel

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

    def latest_per_file(self) -> dict[tuple[str, str], dict]:
        """A file may be pulled repeatedly; keep only the last one."""
        latest: dict[tuple[str, str], dict] = {}
        for e in self.entries():
            key = (e.get("host", "?"), e.get("remote_path", "?"))
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

        by_host: dict[str, list[dict]] = {}
        for (host, _), e in entries.items():
            by_host.setdefault(host, []).append(e)

        for host in sorted(by_host):
            items = sorted(by_host[host], key=lambda e: e.get("remote_path", ""))
            total = sum(e.get("size", 0) for e in items)
            lines.append(f"## {host} — {plural(len(items), 'file')} / {human_bytes(total)}")
            lines.append("")
            lines.append("| Remote path | Size | Remote mtime | Fetched | Notes |")
            lines.append("|---|---|---|---|---|")
            for e in items:
                note = []
                if e.get("truncated"):
                    note.append(f"first {e.get('lines')} lines only")
                mtime = e.get("remote_mtime")
                lines.append(
                    f"| `{e.get('remote_path', '?')}` "
                    f"| {human_bytes(e.get('size', 0))} "
                    f"| {human_time(mtime) if mtime else '?'} "
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
