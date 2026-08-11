# Recipes

Patterns that come up repeatedly. All of them assume you have run `uts hosts` and
know the names.

## Survey: are they all alive, and is anything wrong?

```bash
uts status -a --json
```

One call answers reachability, disk, load, uptime and clock skew for every machine.
Read `hosts[].reachable` first, then `facts.disk`, then `clock_skew_seconds`.

An exit code of `2` means *some* hosts answered — report which ones did not rather
than treating the whole thing as a failure.

## Look before you fetch

Never `pull` blind. Three cheap calls first:

```bash
uts ls   -H a '~/data/'            # how many, how big, what types, how recent
uts peek -H a '~/data/*.csv'       # shapes — do they agree?
uts pull -H a '~/data/*.csv' --dry-run
uts pull -H a '~/data/*.csv'
```

`peek` is the one people skip and regret: it profiles the whole batch, and
disagreeing `shapes` is how you find out two runs were written into one directory
before you build a time series out of them.

## Find the big old files without downloading anything

```bash
uts find -a '~/' --name '*.log' --min-size 100M --sort size --limit 20 --json
```

`find` filters on the remote side and returns only metadata.

## Long-running work

`start` outlives the connection; `ps`/`logs` check on it later.

```bash
uts start -H gpu -s train 'python train.py --epochs 100'
uts ps -a                              # running / exited(N) / killed / vanished / idle
uts logs -H gpu -s train --tail 200
uts stop -H gpu -s train --clean       # stop it and free the name
```

To poll it from a script, `uts ps -a --json` and read `hosts[].jobs[].state`. There
is no push notification — nothing is installed on the far side to send one.

`logs` re-reads the last N lines every time; there is no incremental cursor, so ask
for what you need rather than tailing in a tight loop.

## Set something up, then work in it

The two halves of a session name:

```bash
uts exec  -H a -s build 'cd ~/proj && . .venv/bin/activate'
uts exec  -H a -s build 'pip install -e .' --write
uts start -H a -s build 'pytest -q'      # inherits that cwd and those exports
```

## Send a script over and run it

```bash
uts push -H a ./collect.sh --to '~/bin/'
uts exec -H a 'bash ~/bin/collect.sh'
```

`push` refuses to overwrite without `--force`; the `conflicts` key lists what would
have been clobbered.

## Compare the same thing across machines

Fan-out preserves inventory order, so two runs are diffable line for line:

```bash
uts exec -a 'nvidia-smi --query-gpu=name,memory.used --format=csv,noheader'
uts exec -a 'df -h /data | tail -1'
```

## Working around the far side's shell

The remote login shell may be zsh, which **aborts a command outright when a glob
matches nothing**:

```bash
uts exec -H a 'ls ~/data/*.csv 2>/dev/null || true'     # survives an empty match
uts find -H a '~/data/' --name '*.csv'                  # or sidestep it entirely
```

## Keeping output readable

The caps apply while reading, so a huge file costs you nothing locally — but you
still get a truncated answer. Narrow on the far side:

```bash
uts exec -H a 'tail -200 /var/log/syslog'          # not: cat /var/log/syslog
uts exec -H a 'grep -c ERROR /var/log/app.log'     # a count, not the lines
```

Then check `truncated` and `aborted` before drawing a conclusion.

## When a program insists on a terminal

```bash
uts exec -H a --pty 'sudo systemctl status ssh'
uts exec -H a --pty --duration 3 'btop'      # paint for 3s, return one text frame
```

`--pty` merges stderr into stdout, so leave it off otherwise.

## Cleaning up

```bash
uts ps -a                    # what is still holding a name
uts stop -a --clean          # stop everything and forget it, on both sides
```

`ps` only reads. Nothing frees a session name except `stop --clean`, so the table
grows until you do.
