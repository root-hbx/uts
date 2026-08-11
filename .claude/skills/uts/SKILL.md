---
name: uts
description: Reach other machines on the local network over SSH — survey, inspect, collect from, and run commands on remote hosts, servers, testbeds and GPU boxes. Use whenever a request concerns a machine that is not this one: "are my machines up", "check disk on the server", "how far along is the training run on the GPU box", "what's in ~/data on test-b", "grab the logs off that host", "run this on all the boxes", "copy this script over there". Also use when the user names a host from hosts.json, or says subnet / LAN / remote / testbed / cluster / another machine.
---

# uts — reach over the subnet

`uts` runs one-shot SSH against machines named in `hosts.json`. **Nothing is installed
on the far side** — no agent, no daemon, no package. Everything runs from here.

Start with `uts hosts` (purely local, no network) to learn the names. Everything else
takes those names.

## Five rules

Break one of these and you waste a round trip, so they come first.

**1. Hosts are named, never matched.** `-H NAME`, `-H a,b`, repeat `-H`, or `-a` for
all. There is no default and no pattern matching: **giving neither is an error**, on
every networked subcommand without exception. Globs, `@tags` and IP matching do not
exist. Get the names from `uts hosts`.

**2. The command is exactly one quoted string.**

```bash
uts exec -H test-a 'ls ~/data/*.csv | wc -l'      # right
uts exec -H test-a ls ~/data/*.csv                # error: rc=4, nothing sent
```

What you quote is what the remote shell receives, so pipes, redirection, globs and
`~` need no second set of rules — but they are expanded *there*, not here, so single
quotes are load-bearing. Inner quoting is yours to write: `"pgrep -af 'sleep 600'"`.
Several arguments is an error that prints the correctly-quoted rewrite rather than
guessing at what you meant.

**The dialect is POSIX `sh`, on every host**, whatever shell the account logs into —
uts pins it so a bash box, a zsh box and a fish box all behave the same. Write POSIX:
`VAR=$(...)`, `[ ... ]`, `. script`. For bash-only syntax ask for it explicitly, which
works the same everywhere:

```bash
uts exec -a 'bash -c "for i in {1..5}; do echo $i; done"'   # {1..5}, [[ ]], <<<
```

**3. A session name is the whole handle.** `-s NAME` names *both* a cwd/env context
*and* the one background job running in it. Sessions are opt-in — without `-s`, every
command starts from a clean login, because a command whose meaning depends on
invisible state cannot be trusted.

```bash
uts exec  -H a -s build 'cd ~/proj && . .venv/bin/activate'
uts exec  -H a -s build 'which python'        # → ~/proj/.venv/bin/python
uts start -H a -s train 'python train.py'     # same idea, now in the background
```

**4. Destructive commands need `--write`.** `rm`, `dd`, `mv`, `kill`, `systemctl
stop`, `>` and friends are refused **locally, before anything connects** (`rc=4`).
When the request plainly means to modify something, pass `--write` — that is what it
is for. This is a guard against fumbles, not a permission system.

**5. `--clean` says how far to go, never what gets stopped.** `-s NAME` is one
session, its absence is every session on the selected hosts, with or without
`--clean`. Stopping keeps the log; `--clean` is the only thing that frees a name.

## The commands

| | |
|---|---|
| `hosts` | the names, local only — start here |
| `status -H a` | reachable? machine profile, disk, **clock skew** |
| `ls -H a '~/data/'` | summary of a directory: how many, how big, what types, how recent |
| `find -H a '~/' --name '*.log' --since 24h` | filter by name/time/size, list them |
| `peek -H a '~/data/*.csv'` | structure *without* fetching: line/column/delimiter profile + sample |
| `pull -H a '~/data/*.csv'` | fetch into `./.uts/`, recorded in a manifest; `--to DIR`, `--dry-run` |
| `push -H a ./setup.sh --to '~/bin/'` | send files out; `--force` to overwrite |
| `exec -H a 'cmd'` | run it and wait; `--write`, `-s`, `--pty` |
| `start -H a -s train 'cmd'` | run it in the background, under a name |
| `ps -a` | what every session is doing: running / exited(N) / killed / vanished / idle |
| `logs -H a -s train` | its output so far; `--tail N` |
| `stop -H a -s train` | SIGTERM; `--force` for SIGKILL, `--clean` to free the name |
| `index` | rebuild `.uts/INDEX.md` and summarise the workspace |
| `shell -H a` | interactive terminal — **for a person, not for you** |

Look around before you fetch: `ls` → `peek` → `pull --dry-run` → `pull`.

## Reading the output

Pass `--json` and you get **one envelope on stdout, on every exit path**, including
failures. It works on either side of the verb (`uts --json status -a` and
`uts status -a --json` are the same).

```json
{ "uts": 1, "command": "ls", "ok": true, "exit": 0, "hosts": [ /* one object per host */ ] }
{ "uts": 1, "command": "exec", "ok": false, "exit": 4,
  "error": { "kind": "blocked", "message": "…" }, "hosts": [] }
```

`error.kind` tells you what to do about it:

- `usage` — the invocation is malformed; fix the arguments
- `blocked` — guard refused a destructive command; add `--write` if it was meant
- `inventory` — host selection or `hosts.json` is wrong; check `uts hosts`

Exit codes, which never overlap:

| | |
|---|---|
| `0` | everything worked |
| `1` | every host reachable, the remote command returned non-zero |
| `2` | **partial** — some hosts unreachable or refused, others fine |
| `3` | every host unreachable, or the inventory itself is broken |
| `4` | nothing was sent: guard, usage error, or a size cap |

`2` is the one to watch: a fan-out that half worked still exits non-zero, and the
per-host `reachable` / `error` fields say which half.

Per-host objects carry `host ip user reachable rc duration stdout stderr truncated
aborted dropped_lines dropped_bytes error refused`, plus whatever that subcommand
produced — `ls` a `summary`, `find` a `files` list, `peek` `shapes`, `ps` `jobs` /
`idle` / `cwds`, `status` `facts` / `clock_skew_seconds`. See
`references/json.md` for the per-command keys.

## What will bite you

- **A glob that matches nothing does not vanish** — POSIX sh passes the pattern
  through literally, so `ls ~/logs/*.gz` on a host with no matches "finds" a file
  named `*.gz`. Guard with `2>/dev/null || true`, or use `find`.
- **Bash-only syntax fails**, because the dialect is POSIX `sh` everywhere: `{1..5}`,
  `[[ ]]`, `<<<` and arrays need an explicit `bash -c '...'` around them.
- **`pkill -f PATTERN` can kill the SSH session running it** — that session's own
  command line contains the pattern. Use `uts stop` for uts-started work.
- **Output is capped while being read**, not after. Always check `truncated` /
  `aborted` before believing a result is complete, and never `cat` a large file —
  narrow on the remote side with `grep`/`tail`/`wc`.
- **`--pty` merges stderr into stdout.** Leave it off unless a program demands a
  terminal (`sudo` prompting, anything curses-based, a pager). With `--duration S` a
  full-screen program paints for S seconds and you get one text frame back.
- **Timestamps from a remote host are on that host's clock.** `status` reports the
  skew; correct for it before comparing timelines across machines.
- **`ps` reads, `stop` forgets.** Nothing else frees a session name, so `ps` grows
  until you `stop --clean`.
- Reusing a finished session name needs `--force`, since it discards the log that
  says how the last run went.

## Where things live

- `hosts.json` — looked up as `$UTS_HOSTS`, then `./hosts.json`, then the one in the
  uts checkout. So `uts` works from any directory.
- `./.uts/` — the local workspace: pulled files under `<host>/<remote path>`, an
  append-only `manifest.jsonl` recording every transfer, and `INDEX.md`. Created in
  the **current** directory; `--workspace DIR` moves it.
- `~/.uts/jobs/<name>/` — the only thing uts ever writes on a remote host: `cmd`,
  `started`, `pid`, `rc`, `log` for a background job. `stop --clean` removes it.
