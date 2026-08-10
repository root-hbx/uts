## UTS

> One machine runs the agent. Every machine is within its reach.

An AI agent is bounded by what it can see and that boundary is usually a single machine.

**UTS** pushes it out to the edge of the subnet: deploy Claude Code/Codex/Gemini on one machine
and it can survey, query and collect across all of them, including PC, COTS Server, **even** Proprietary Hardware Testbed.

Nothing runs on the other side. No agent, no daemon, no installed package.
SSH is the only thing required for each server.

<h3 align="center">
    Unifying The Sky: One Claude/Codex/Gemini Deployment, Multiple Backends Support
</h3>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="./uts.svg">
  <img src="./uts.svg" alt="UTS Demo">
</picture>

## Quick start

```bash
./uts ping all                                # are the machines alive
./uts ls test '~/data/'                       # how many, how big, what types, how recent
./uts peek test '~/data/*.csv'                # right shape? several runs mixed together?
./uts pull test '~/data/*.csv' --dry-run      # what would be fetched
./uts pull test '~/data/*.csv'                # fetch into .uts/ and record it
```

Wrap path specs in **single quotes** — `~` and `*` have to reach the remote shell.

`./uts` is a shim that uses `uv` to install dependencies, so there is no
`pip install` step.
