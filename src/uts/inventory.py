"""Parse hosts.json and resolve selector strings into host lists."""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_USER = "root"
DEFAULT_PORT = 22
DEFAULT_TIMEOUT = 8.0

_GLOB_CHARS = set("*?[")


class InventoryError(Exception):
    """hosts.json is broken, or a selector matched nothing."""


@dataclass
class Host:
    name: str
    ip: str
    user: str
    password: str
    port: int = DEFAULT_PORT
    timeout: float = DEFAULT_TIMEOUT
    tags: tuple[str, ...] = ()

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

    tags = entry.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]

    return Host(
        name=str(entry.get("name") or ip),
        ip=str(ip),
        user=str(entry.get("user") or DEFAULT_USER),
        password=str(password),
        port=int(entry.get("port") or DEFAULT_PORT),
        timeout=float(entry.get("timeout") or DEFAULT_TIMEOUT),
        tags=tuple(str(t) for t in tags),
    )


def select(hosts: list[Host], selector: str | None) -> list[Host]:
    """Accepts: all / name / a,b / @tag / 192.168.1.* glob."""
    if not selector or selector == "all":
        return list(hosts)

    picked: list[Host] = []
    taken: set[str] = set()
    for term in (t.strip() for t in selector.split(",")):
        if not term:
            continue
        matched = _match_term(hosts, term)
        if not matched:
            known = ", ".join(h.name for h in hosts)
            raise InventoryError(f"selector {term!r} matched no host. Known hosts: {known}")
        for host in matched:
            if host.name not in taken:
                taken.add(host.name)
                picked.append(host)
    return picked


def _match_term(hosts: list[Host], term: str) -> list[Host]:
    if term == "all":
        return list(hosts)
    if term.startswith("@"):
        tag = term[1:]
        return [h for h in hosts if tag in h.tags]
    if _GLOB_CHARS & set(term):
        return [
            h for h in hosts if fnmatch.fnmatch(h.name, term) or fnmatch.fnmatch(h.ip, term)
        ]
    return [h for h in hosts if h.name == term or h.ip == term]
