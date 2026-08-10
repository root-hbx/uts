"""Block obviously destructive remote commands.

This is a fumble guard, not a security boundary — it regex-matches a command
string and is trivial to bypass. The reason it exists is practical: A/B/C are
actively producing data, and one slipped `rm` destroys the very thing you meant
to analyse. Pass --write when you really do mean to write.
"""

from __future__ import annotations

import re

# Strip harmless redirections first, or `grep foo 2>/dev/null` trips the rule.
_BENIGN_REDIRECTS = re.compile(r"\d?>\s*/dev/null|\d?>&\s*\d")

# Deciding whether `>` is a redirection needs two more strips, otherwise the false
# positive rate makes the guard unusable: quoted text (`echo "rows -> files"`) and
# comparison/arrow operators (-> => >=). Only the `>` rule sees the stripped
# string; rm/dd and friends still scan the raw one, so `sh -c "rm -rf /"` cannot
# slip through by hiding in quotes.
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_ARROWS = re.compile(r"->|=>|>=")

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(^|[\s;&|(])rm\b"), "rm deletes files"),
    (re.compile(r"(^|[\s;&|(])(shred|wipe)\b"), "shred/wipe destroys data"),
    (re.compile(r"(^|[\s;&|(])dd\b"), "dd writes raw devices"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "mkfs formats a filesystem"),
    (re.compile(r"\b(fdisk|parted|mkswap)\b"), "disk partitioning"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+[06])\b"), "shutdown/reboot"),
    (re.compile(r"(^|[\s;&|(])truncate\b"), "truncate empties files"),
    (re.compile(r"(^|[\s;&|(])(mv|chown|chmod|ln)\b"), "changes file location/owner/mode"),
    (re.compile(r"(^|[\s;&|(])(kill|killall|pkill)\b"), "kills processes"),
    (re.compile(r"\bsystemctl\s+(stop|restart|disable|mask|kill)\b"), "changes service state"),
    (re.compile(r"\b(service)\s+\S+\s+(stop|restart)\b"), "changes service state"),
    (re.compile(r"\b(apt|apt-get|yum|dnf|pacman|apk|pip|pip3)\s+(install|remove|purge|erase)\b"),
     "installs/removes packages"),
    (re.compile(r"(^|[\s;&|(])(tee|dd)\b"), "tee writes files"),
    (re.compile(r"\bsed\b[^|;]*\s-\w*i"), "sed -i rewrites in place"),
    (re.compile(r"\b(crontab|useradd|userdel|usermod|passwd)\b"), "changes users/cron"),
    (re.compile(r"\bgit\s+(reset|clean|checkout|restore)\b"), "destructive git operation"),
    (re.compile(r"(^|[\s;&|(])(docker|podman)\s+(rm|rmi|stop|kill|prune|down)\b"),
     "destructive container operation"),
]

_REDIRECT = (re.compile(r">"), "output redirection writes a remote file")


def check(command: str) -> str | None:
    """Return the reason for blocking, or None if the command looks safe."""
    probe = _BENIGN_REDIRECTS.sub(" ", command)
    for pattern, reason in _RULES:
        if pattern.search(probe):
            return reason

    pattern, reason = _REDIRECT
    if pattern.search(_ARROWS.sub(" ", _QUOTED.sub(" ", probe))):
        return reason
    return None


def explain(command: str, reason: str) -> str:
    return (
        f"blocked: {reason}\n"
        f"  {command}\n"
        f"This machine is producing data; writing to it destroys what you came to analyse.\n"
        f"Re-run with --write if you meant it."
    )
