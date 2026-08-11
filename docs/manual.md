# For Developers

**(1) Remote Execution:**

```bash
# Safe commands are always ready to go.
./uts exec -a 'nvidia-smi'
./uts exec -a 'ls ~/data/*.csv | wc -l'

# Destructive commands (rm, dd, mv, kill, systemctl stop, >, ...) are refused
# locally, rc=4, before anything connects. Say `--write` when you mean it.
./uts exec -H test-a --write 'rm -rf ~/scratch'
```

**(2) Session Management:**

`Sessions` are the handle for anything longer than one command.

```bash
./uts exec -H test-a -s build_session 'cd ~/proj && . .venv/bin/activate'
./uts exec -H test-a -s build_session 'which python'   # → ~/proj/.venv/bin/python
./uts exec -H test-a 'which python'            # → /usr/bin/python3, untouched
```

Sessions are opt-in, because a command whose meaning depends on invisible state is
a command you cannot trust. Without `-s`, every command starts from a clean login.

**(3) Task/Job Management:**

**\[1\] Start / PS / Logs / Stop: only job; log kept**

In **UTS**, a `job` starts within a `session` and stays there:

```bash
./uts start -H test-a -s train_session 'python train.py --epochs 100' # one job per session name

./uts ps -a                                    # every host: running / exited(0) / killed / idle
./uts ps -H test-a                             # just that host
./uts logs -H test-a -s train_session          # last 50 lines; --tail N for more

./uts stop -H test-a -s train_session          # SIGTERM that one; --force for SIGKILL
./uts stop -a                                  # SIGTERM every session still running
```

A session runs one thing at a time: Reusing a finished name needs `--force`, since it
discards the log that says how the last run went.

**\[2\] With `--clean`: job, log and the session behind it**

Stopping keeps the log — how a run ended is usually why you stopped it. `--clean`
is the other half, and the only thing that frees a name:

```bash
./uts stop -H test-a -s train_session --clean  # stop it, then forget it
./uts stop -a --clean                          # stop them all, then forget them all
```

`--clean` says how far to go, never what gets stopped: `-s NAME` is one session,
its absence is all of them, exactly as without the flag. It forgets both homes of a
session — `~/.uts/jobs/<name>/` there.

**(4) Interactive Terminals:**

For the programs that insist on one:

```bash
./uts exec -H test-a --pty 'sudo systemctl status ssh'
./uts exec -H test-a --pty --duration 3 'btop'    # full-screen program → one frame
./uts shell -H test-a                             # interactive, one host, for a person
```

`--pty` attaches a terminal, so a program that checks for one — `sudo` asking for a
password, anything curses-based, a pager — behaves as it would if you had typed the
command yourself.

The cost is that a terminal is a single stream: `stderr` comes back
merged into `stdout`, so leave `--pty` off for daily use.

