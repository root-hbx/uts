# UTS

> One machine runs the AI agent. Every machine is within its reach.

An AI agent is bounded by what it can see and that boundary is usually a single machine.

**UTS** pushes it out to the edge of the subnet: deploy Claude Code/Codex/Gemini on one machine
and it can survey, query and collect across all of them, including PC, COTS Server, **even** Proprietary Hardware Testbed.

Nothing runs on the other side. No agent/daemon/package. SSH is ALL YOU NEED.

<h3 align="center">
    Unifying The Sky: One Claude/Codex/Gemini Deployment, Multiple Backends Support
</h3>

<p align="center">
    <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./uts.svg">
    <img src="./uts.svg" alt="UTS Demo">
    </picture>
</p>

## Quick Start

**(0) Before Everything:**

```bash
# rename
mv host.example.json host.json
# then customize host.json
```

Every command that touches the network says who it is talking to, by the `name` in
`hosts.json`. There is no default: `-H` for some, `-a` for all.

**(1) Basic Commands:**

```bash
./uts hosts                                   # what the names are
./uts status -a                               # are the machines alive
./uts status -H test-a                        # just that one
./uts ls -H test-a,test-b '~/data/'           # several
```

```bash
./uts ls -H test-a '~/data/'                  # how many, how big, what types, how recent
./uts peek -H test-a '~/data/*.csv'           # right shape? several runs mixed together?
./uts pull -H test-a '~/data/*.csv' --dry-run # what would be fetched
./uts pull -H test-a '~/data/*.csv'           # fetch into .uts/ and record it
./uts pull -H test-a '~/data/*.csv' --to ./raw
```

```bash
./uts push -H test-a ./setup.sh ./lib --to '~/bin/'   # --force to overwrite
```

**(2) Further Development:**

See [User Manual](./docs/manual.md).

