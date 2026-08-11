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
# Safe commands are always ready to go.
./uts exec -a 'nvidia-smi'
./uts exec -a 'ls ~/data/*.csv | wc -l'

# Destructive commands (rm, dd, mv, kill, systemctl stop, >, ...) are refused
# locally, rc=4, before anything connects. Say `--write` when you mean it.
./uts exec -H gpu-01 --write 'rm -rf ~/scratch'
```

**(3) Session Management:**

`Sessions` are the handle for anything longer than one command.

```bash
./uts exec -H gpu-01 -s build_session 'cd ~/proj && . .venv/bin/activate'
./uts exec -H gpu-01 -s build_session 'which python'   # → ~/proj/.venv/bin/python
./uts exec -H gpu-01 'which python'            # → /usr/bin/python3, untouched
```

Sessions are opt-in, because a command whose meaning depends on invisible state is
a command you cannot trust. Without `-s`, every command starts from a clean login.

**(4) Task/Job Management:**

**\[1\] Start / PS / Logs / Stop: only job; log kept**

In **UTS**, a `job` starts within a `session` and stays there:

```bash
./uts start -H gpu-01 -s train_session 'python train.py --epochs 100' # one job per session name

./uts ps -a                                    # every host: running / exited(0) / killed / idle
./uts ps -H gpu-01                             # just that host
./uts logs -H gpu-01 -s train_session          # last 50 lines; --tail N for more

./uts stop -H gpu-01 -s train_session          # SIGTERM that one; --force for SIGKILL
./uts stop -a                                  # SIGTERM every session still running
```

A session runs one thing at a time: `start` on a name already running is refused
rather than doubled, and reusing a finished name needs `--force`, because it
discards the log that says how the last run went.

**\[2\] With `--clean`: job, log and the session behind it**

Stopping keeps the log — how a run ended is usually why you stopped it. `--clean`
is the other half, and the only thing that frees a name:

```bash
./uts stop -H gpu-01 -s train_session --clean  # stop it, then forget it
./uts stop -a --clean                          # stop them all, then forget them all
```

`--clean` says how far to go, never what gets stopped: `-s NAME` is one session,
its absence is all of them, exactly as without the flag. It forgets both homes of a
session — `~/.uts/jobs/<name>/` there, the cwd and exports recorded here — so it
also clears one that is merely idle and never ran a job.

**(5) Interactive Terminals:**

For the programs that insist on one:

```bash
./uts exec -H gpu-01 --pty 'sudo systemctl status ssh'
./uts exec -H gpu-01 --pty --duration 3 'btop'    # full-screen program → one frame
./uts shell -H gpu-01                             # interactive, one host, for a person
```
