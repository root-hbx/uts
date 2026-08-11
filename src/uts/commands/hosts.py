"""uts hosts — list the inventory. Purely local, no network."""

from __future__ import annotations

import json

from ..inventory import Host
from ..output import plural


def run(hosts: list[Host], as_json: bool) -> int:
    if as_json:
        print(
            json.dumps(
                [
                    {"name": h.name, "ip": h.ip, "user": h.user, "port": h.port}
                    for h in hosts
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    name_w = max(len(h.name) for h in hosts)
    addr_w = max(len(f"{h.user}@{h.ip}:{h.port}") for h in hosts)
    for h in hosts:
        addr = f"{h.user}@{h.ip}:{h.port}"
        print(f"{h.name:<{name_w}}  {addr:<{addr_w}}".rstrip())
    print(f"\n{plural(len(hosts), 'host')}. Run `uts status -a` to check they are alive.")
    return 0
