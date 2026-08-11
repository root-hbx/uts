"""uts hosts — list the inventory. Purely local, no network."""

from __future__ import annotations

from ..inventory import Host
from ..output import EXIT_OK, envelope, plural


def run(hosts: list[Host], as_json: bool) -> int:
    if as_json:
        # No password: this is the one command whose whole output is the inventory,
        # and an agent reading it has no use for the credential.
        print(envelope("hosts", EXIT_OK, [
            {"name": h.name, "ip": h.ip, "user": h.user, "port": h.port} for h in hosts
        ]))
        return EXIT_OK

    name_w = max(len(h.name) for h in hosts)
    addr_w = max(len(f"{h.user}@{h.ip}:{h.port}") for h in hosts)
    for h in hosts:
        addr = f"{h.user}@{h.ip}:{h.port}"
        print(f"{h.name:<{name_w}}  {addr:<{addr_w}}".rstrip())
    print(f"\n{plural(len(hosts), 'host')}. Run `uts status -a` to check they are alive.")
    return 0
