"""Parse hosts.json and resolve `-H` / `-a` into host lists."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_USER = "root"
DEFAULT_PORT = 22
DEFAULT_TIMEOUT = 8.0


class InventoryError(Exception):
    """hosts.json is broken, or no host was selected."""


@dataclass
class Host:
    name: str
    ip: str
    user: str
    password: str
    port: int = DEFAULT_PORT
    timeout: float = DEFAULT_TIMEOUT

    @property
    def label(self) -> str:
        return f"{self.name} ({self.user}@{self.ip})"


def default_hosts_path() -> Path:
    """Where to look when --hosts is absent: env var > cwd > repo root."""
    env = os.environ.get("UTS_HOSTS")
    if env:
        return Path(env).expanduser()
    cwd = Path.cwd() / "hosts.json"
    if cwd.exists():
        return cwd
    return Path(__file__).resolve().parents[2] / "hosts.json"


def load_inventory(path: str | Path | None = None) -> list[Host]:
    p = Path(path).expanduser() if path else default_hosts_path()
    if not p.exists():
        raise InventoryError(
            f"no host inventory at {p}\n"
            f"Copy hosts.example.json to hosts.json and fill it in:\n"
            f'  [ {{ "name": "a", "ip": "192.168.1.11", "user": "ops", "password": "..." }} ]'
        )
    try:
        entries = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InventoryError(f"{p} is not valid JSON: {exc}") from exc

    if not isinstance(entries, list):
        raise InventoryError(f"{p} must hold an array of hosts, got {type(entries).__name__}")

    hosts: list[Host] = []
    seen: dict[str, int] = {}
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise InventoryError(f"host entry {idx + 1} in {p} must be an object")
        host = _build_host(entry, p, idx)
        if host.name in seen:
            raise InventoryError(
                f"duplicate host name {host.name!r} in {p} "
                f"(entries {seen[host.name] + 1} and {idx + 1})"
            )
        seen[host.name] = idx
        hosts.append(host)

    if not hosts:
        raise InventoryError(f"{p} lists no hosts")
    return hosts


def _build_host(entry: dict, path: Path, idx: int) -> Host:
    where = f"host entry {idx + 1} in {path}"

    ip = entry.get("ip")
    if not ip:
        raise InventoryError(f'{where} has no "ip"')

    password = entry.get("password")
    if password is None:
        raise InventoryError(
            f'{where} ({ip}) has no "password". '
            f"This tool only does password auth, so it must be in the inventory."
        )

    return Host(
        name=str(entry.get("name") or ip),
        ip=str(ip),
        user=str(entry.get("user") or DEFAULT_USER),
        password=str(password),
        port=int(entry.get("port") or DEFAULT_PORT),
        timeout=float(entry.get("timeout") or DEFAULT_TIMEOUT),
    )


def select(hosts: list[Host], names: list[str] | None, all_: bool = False) -> list[Host]:
    """Resolve `-H NAME` / `-a` against the inventory.

    A host is named, never matched. Tags, globs and IP lookups were all ways of
    saying "some of them" without saying which, and every one of them made the
    reader work out at a glance whether `web` was a machine, a label or a pattern.
    One spelling, one meaning: the "name" field in hosts.json.

    Neither flag is an error rather than a default. "Every machine I own" is not
    something to arrive at by forgetting to type something.
    """
    terms = [t.strip() for value in (names or []) for t in value.split(",") if t.strip()]

    if all_ and terms:
        raise InventoryError("-a and -H cannot be combined: -a already means every host.")
    if all_:
        return list(hosts)
    if not terms:
        raise InventoryError(
            f"no host selected. Name one with -H <name>, or take all of them with -a.\n"
            f"Known hosts: {_known(hosts)}"
        )

    known = {h.name for h in hosts}
    for term in terms:
        if term not in known:
            raise InventoryError(f"no host named {term!r}. Known hosts: {_known(hosts)}")

    # Inventory order, not argument order: `-H c,a` and `-H a,c` are the same
    # question, and two runs of it should be diffable line for line.
    wanted = set(terms)
    return [h for h in hosts if h.name in wanted]


def _known(hosts: list[Host]) -> str:
    return ", ".join(h.name for h in hosts)
