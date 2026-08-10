"""Named session state: what `cd` and `export` left behind, kept locally.

Each exec opens a fresh SSH channel, so nothing survives between calls. A session
carries the two things people actually expect to persist — the working directory
and exported variables — by replaying them in front of the next command.

Two decisions shape everything here.

**State is opt-in.** Without --session a command runs in a clean login environment,
byte for byte what it did before sessions existed. Ambient state would mean the
same command yields different answers at different times with nothing on screen to
say why, which is precisely the class of bug an agent cannot detect.

**State is per host.** `~/proj` on one machine is not `~/proj` on another, and a
venv activated on one says nothing about the others.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SESSIONS_DIR = "sessions"

# Never replayed, even when they change. SSH_* belong to the connection that is
# already gone; PWD/OLDPWD are cwd's business; the rest are per-process noise that
# would grow without bound.
_ENV_DENYLIST = frozenset({
    "_", "PWD", "OLDPWD", "SHLVL", "RANDOM", "LINES", "COLUMNS", "TERM",
})
_ENV_DENY_PREFIXES = ("SSH_", "XDG_SESSION")

# A variable name we would be willing to `export`. Anything stranger (bash exports
# functions as `BASH_FUNC_x%%`) is dropped rather than quoted into a broken command.
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Session:
    """One named session; `.uts/sessions/<name>.json` holds every host it has touched."""

    def __init__(self, name: str, root: str | Path | None = None) -> None:
        self.name = name
        self.root = Path(root or ".uts") / SESSIONS_DIR
        self._state: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._state = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # A corrupt session file must not make the host unreachable; starting
                # over from a clean environment is the recoverable failure.
                self._state = {}

    @property
    def path(self) -> Path:
        return self.root / f"{self.name}.json"

    def for_host(self, host: str) -> dict:
        return self._state.get(host, {})

    def cwd(self, host: str) -> str | None:
        return self.for_host(host).get("cwd")

    def env(self, host: str) -> dict[str, str]:
        return self.for_host(host).get("env", {})

    def baseline(self, host: str) -> dict[str, str] | None:
        return self.for_host(host).get("baseline")

    def update(self, host: str, cwd: str, env: dict[str, str], baseline: dict[str, str]) -> dict:
        """Store the state a command ended in. Returns what changed, for display.

        The baseline is stored alongside so later calls can diff against the login
        environment without paying for a second round trip every time.
        """
        before = self.for_host(host)
        changed = {
            "cwd": cwd if cwd != before.get("cwd") else None,
            "env_added": sorted(set(env) - set(before.get("env", {}))),
            "env_dropped": sorted(set(before.get("env", {})) - set(env)),
        }
        self._state[host] = {"cwd": cwd, "env": env, "baseline": baseline}
        self.save()
        return changed

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def hosts(self) -> list[str]:
        return sorted(self._state)


def list_sessions(root: str | Path | None = None) -> list[Session]:
    directory = Path(root or ".uts") / SESSIONS_DIR
    if not directory.is_dir():
        return []
    return [Session(p.stem, root) for p in sorted(directory.glob("*.json"))]


def env_delta(current: dict[str, str], baseline: dict[str, str]) -> dict[str, str]:
    """The variables worth carrying forward: new or changed, minus the noise.

    Replaying a whole environment would drag the previous connection's SSH_* into
    the next one and pin a stale TERM; a delta against the login environment is
    what actually captures "the user activated a venv".
    """
    out = {}
    for key, value in current.items():
        if key in _ENV_DENYLIST or key.startswith(_ENV_DENY_PREFIXES):
            continue
        if not _NAME.match(key):
            continue
        if baseline.get(key) != value:
            out[key] = value
    return out
