# The `--json` envelope, per command

Every `--json` invocation prints exactly one object to stdout, whatever happens:

```json
{
  "uts": 1,
  "command": "ls",
  "ok": true,
  "exit": 0,
  "hosts": [ /* one object per selected host, in inventory order */ ]
}
```

- `uts` — contract version. Bumped only when this shape changes.
- `command` — the subcommand. `null` only when argparse rejected the arguments
  before a subcommand was identified.
- `ok` — exactly `exit == 0`. It is not "uts worked": `ok:false, exit:1` means every
  host answered and the *remote* command failed.
- `exit` — always equals the process exit code, including the `--dry-run` case where
  `pull`/`push` downgrade a partial result to `0`.
- `hosts` — `[]` on any failure that never reached the network.

On failure an `error` object appears and nothing else changes:

```json
{ "uts": 1, "command": "exec", "ok": false, "exit": 4,
  "error": { "kind": "blocked", "message": "blocked: rm deletes files\n…" },
  "hosts": [] }
```

`kind` is one of `usage`, `blocked`, `inventory`. `message` is the same prose that
text mode writes to stderr, newlines and all.

## The standard per-host object

Most commands produce this — the transport result, with that subcommand's own answer
flattened on top of it:

```
host ip user reachable rc duration stdout stderr
truncated aborted dropped_lines dropped_bytes error refused
```

- `reachable` is `error is None`. An unreachable host has `rc: -1` and prose in
  `error`; it never raises and never stops the other hosts.
- `refused` is different from `error`: the host was fine and **uts** declined — a
  size cap, a session already running. Check both.
- `truncated` / `aborted` mean the output was cut *while being read*. `aborted` also
  means the exit code is unknown.

**Two commands do not use this object.** `status` and `ps` (and `stop --clean`, which
shares `ps`'s) emit a smaller one with no `rc`, `stdout` or `stderr` — see below.

## Per-command keys

### `hosts`
`{ name, ip, user, port }` per host. No password, no transport fields.

### `status`
Custom object: `{ host, ip, reachable, duration, error, facts, clock_skew_seconds }`.

`facts`: `hostname os kernel arch cpus mem uptime load time epoch`, plus
`disk` (list of strings) and
`logdirs: [{ path, log_files, at_probe_cap }]` — `at_probe_cap` because the probe
stops counting, so a capped count is a floor, not an exact number.

`clock_skew_seconds` is remote-minus-local at the midpoint of the exec window, or
`null`. Correct for it before comparing timestamps across machines.

### `ls`
`at_cap` (the listing hit its limit — there may be more) and `summary`:

```
count total_bytes mtime_days
by_ext  [[ext, n_files, bytes], …]   ← arrays, not objects, biggest first
newest  {path, size, mtime}
oldest  {path, size, mtime}
biggest [{path, size, mtime}, …]     ← top 5
```

When nothing matched, `summary` is just `{"count": 0}`.
A wide `mtime_days` usually means several runs share the directory.

### `find`
`scanned matched total_bytes limit`, `dropped: {name, since, size}` (how many each
filter excluded), and `files: [{path, size, mtime}, …]` truncated to `limit`.

### `peek`
`matched total_bytes probed at_list_cap sample_path sample`, and
`shapes: [{path, lines, columns, delimiter}, …]`. `delimiter` is `null` for single
column / free text.

**Disagreeing shapes are the signal to look for** — it usually means several runs are
mixed into one directory, which breaks loading them as a time series.

When nothing matched: `{ matched: 0, stderr }` only.

### `pull`
Always: `matched total_bytes skipped_old at_list_cap`.
With `--dry-run`: `files` (first 20).
After a real transfer: `pulled` (count) and `dest` (local directory).
Over the size cap: no transfer, and `refused` explains.

### `push`
Always: `dest` (the resolved remote directory) and `conflicts` (remote paths that
already exist — without `--force` these are refused).
After a real transfer: `pushed` (count) and `transfer_bytes` (compressed).

### `exec`
Plain by default. With `-s NAME`: `session`, `cwd` (the directory the command *ran
in*), and `notes` — a list of human-readable strings recording what changed
(`cwd → …`, `env +VAR`). A note also appears when the session state could **not** be
read back, which means a `cd` did not take effect.

With `--pty`: `pty: true`, and `frame_after: S` when `--duration` was given. Remember
stderr is merged into stdout in this mode.

### `start`
`session`, and `pid` (a **string**) when the job started. If it did not:
`refused` says why — the name is busy, or it holds a finished run whose log would be
discarded (use `--force`).

### `ps`
Custom object: `{ host, ip, reachable, error }` plus:

- `now` — the **remote** epoch, for computing elapsed time against `started`
- `jobs: [{ id, state, started, pid, command }]`
- `idle: [name, …]` — sessions with local cwd/env state but nothing running
- `cwds: { session: cwd }`

`state` is raw here: `running`, `exited:<code>`, or `vanished`. Text mode maps
`exited:143` to `killed`; JSON does not. `vanished` means no `rc` file and no live
process — SIGKILL, OOM, or a reboot, which is not the same as "finished".

`command` is truncated to 300 bytes with tabs and newlines flattened.

### `stop --clean`
`ps`'s object plus `cleaned: [names]` and `busy: [names]`. Plain `stop` (no
`--clean`) uses the standard per-host object instead.

### `index`
No hosts at all: `hosts: []`, plus `index` (path to the rebuilt INDEX.md),
`pulled`, `pushed`, `hosts_seen`.

### `shell`
No `--json`. It is an interactive terminal and refuses without a tty.
