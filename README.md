# UTS

> One machine runs the agent. Every machine is within its reach.

An AI agent is bounded by what it can see and that boundary is usually a single machine.

**UTS** pushes it out to the edge of the subnet: deploy Claude Code/Codex/Gemini on one machine
and it can survey, query and collect across all of them, including PC, COTS Server, **even** Proprietary Hardware Testbed.

Nothing runs on the other side. No agent, no daemon, no installed package.
SSH is the only thing required for each server.

<h3 align="center">
    Unifying The Sky: One Claude/Codex/Gemini Deployment, Multiple Backends Support
</h3>

<p align="center">
    <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./uts.svg">
    <img src="./uts.svg" alt="UTS Demo">
    </picture>
</p>

## Look around

```bash
./uts ping all                                # are the machines alive
./uts ls test '~/data/'                       # how many, how big, what types, how recent
./uts peek test '~/data/*.csv'                # right shape? several runs mixed together?
./uts pull test '~/data/*.csv' --dry-run      # what would be fetched
./uts pull test '~/data/*.csv'                # fetch into .uts/ and record it
```

Wrap path specs in **single quotes** — `~` and `*` have to reach the remote shell.

## Do something

```bash
./uts push @gpu ./setup.sh ./lib '~/bin/'     # send files out; --force to overwrite
./uts exec @gpu -- nvidia-smi                 # one command, everywhere, at once
./uts exec test --write 'rm -rf ~/scratch'    # writes are blocked until you say so
```

**Long jobs** outlive the connection that started them:

```bash
./uts exec @gpu --detach -- python train.py --epochs 100
./uts jobs @gpu                               # running / exited(0) / killed, with elapsed
./uts logs a 7f3c1a --tail 100
./uts kill a 7f3c1a
./uts jobs @gpu --clean                       # remove the finished ones' state
```

**Sessions** carry `cd` and exported variables forward. They are opt-in, because a
command whose meaning depends on invisible state is a command you cannot trust:

```bash
./uts exec test --session build 'cd ~/proj && . .venv/bin/activate'
./uts exec test --session build 'which python'     # → ~/proj/.venv/bin/python
./uts exec test 'which python'                     # → /usr/bin/python3, untouched
./uts sessions                                     # where each one currently stands
```

**Terminals**, for the programs that insist on one:

```bash
./uts exec test --pty -- sudo systemctl status ssh
./uts exec test --pty --duration 3 -- btop    # full-screen program → one text frame
./uts shell test                              # interactive, one host, for a person
```

`./uts` is a shim that uses `uv` to install dependencies, so there is no
`pip install` step.

## What it does not do

The remote side stays untouched: no agent, no daemon, no installed package. The only
thing uts leaves there is `~/.uts/jobs/` for detached jobs — plain files, removable
with `uts jobs --clean`.

`--write` and the guard behind it stop accidents, not attacks. It matches command
strings with regexes and is trivial to talk around; treat it as a seatbelt.
