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

## Quick Start

Every command that touches the network says who it is talking to, by the `name` in
`hosts.json`. There is no default: `-H` for some, `-a` for all.

**(1) Basic Commands:**

```bash
./uts hosts                                   # what the names are
./uts status -a                               # are the machines alive
./uts status -H gpu-01                        # just that one
./uts ls -H gpu-01,gpu-02 '~/data/'           # several
```

```bash
./uts ls -H gpu-01 '~/data/'                  # how many, how big, what types, how recent
./uts peek -H gpu-01 '~/data/*.csv'           # right shape? several runs mixed together?
./uts pull -H gpu-01 '~/data/*.csv' --dry-run # what would be fetched
./uts pull -H gpu-01 '~/data/*.csv'           # fetch into .uts/ and record it
./uts pull -H gpu-01 '~/data/*.csv' --to ./raw
```

```bash
./uts push -H gpu-01 ./setup.sh ./lib --to '~/bin/'   # --force to overwrite
```

**(2) Remote Execution:**

```bash
./uts exec -a 'nvidia-smi'                            # one command, everywhere, at once
./uts exec -a 'ls ~/data/*.csv | wc -l'               # the pipe runs on that side
./uts exec -H gpu-01 --write 'rm -rf ~/scratch'       # writes are blocked until you say so
```

**(3) Session Management:**

`Sessions` are the handle for anything longer than one command.

```bash
./uts exec -H gpu-01 -s build 'cd ~/proj && . .venv/bin/activate'
./uts exec -H gpu-01 -s build 'which python'   # → ~/proj/.venv/bin/python
./uts exec -H gpu-01 'which python'            # → /usr/bin/python3, untouched
```

Sessions are opt-in, because a command whose meaning depends on invisible state is
a command you cannot trust. Without `-s`, every command starts from a clean login.

**(4) Tasks/Jobs Management:**

**Work that outlives the connection** starts in a session and stays there:

```bash
./uts start -H gpu-01 -s train 'python train.py --epochs 100'
./uts ps -a                                    # running / exited(0) / killed / idle
./uts logs -H gpu-01 -s train --tail 100
./uts stop -H gpu-01 -s train
./uts ps -a --clean                            # forget the finished ones
```

```
SESSION  HOST    STATE       ELAPSED  CWD      CMD
build    gpu-01  idle        -        ~/proj   -
train    gpu-01  running     42m      ~/proj   python train.py --epochs 100
eval     gpu-02  exited(0)   1h03m    ~/work   python eval.py
```

A session runs one thing at a time: `uts start` on a name that is already running
is refused rather than doubled, and reusing a finished name needs `--force`,
because it discards the log that says how the last run went.

**(5) Interactive Terminals:**

For the programs that insist on one:

```bash
./uts exec -H gpu-01 --pty 'sudo systemctl status ssh'
./uts exec -H gpu-01 --pty --duration 3 'btop'    # full-screen program → one frame
./uts shell -H gpu-01                             # interactive, one host, for a person
```
