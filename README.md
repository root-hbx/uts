# uts

Let the one laptop that has Claude on it reach the logs and data on every other
machine in the subnet, over SSH — no more logging in one by one, downloading, and
carrying files around on a USB stick.

Nothing is installed on those machines. SSH is the handle.

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
