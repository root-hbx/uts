import pytest

from uts import guard


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp/x",
        "cd /data && rm old.log",
        "ls; rm -f a",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sdb1",
        "shutdown -h now",
        "systemctl restart nginx",
        "truncate -s 0 /var/log/app.log",
        "mv /var/log/app.log /tmp/",
        "chmod 777 /etc/passwd",
        "kill -9 1234",
        "apt-get install htop",
        "sed -i 's/a/b/' /etc/hosts",
        "echo hi > /tmp/out",
        "cat a.log >> b.log",
        "docker rm mycontainer",
        "git checkout .",
    ],
)
def test_blocks_destructive(command):
    assert guard.check(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "ls -la /var/log",
        "grep -c ERROR /var/log/syslog",
        "tail -n 200 /var/log/app.log",
        "find /data -name '*.csv' -mtime -1",
        "df -h",
        "ps aux | head -20",
        "journalctl -u nginx --since '1 hour ago'",
        "zgrep WARN /var/log/app.log.1.gz",
        "du -sh /var/log",
        "cat /etc/os-release",
        # All of these were real false positives. They show up constantly in ordinary
        # commands, and blocking them makes the guard unusable.
        "ls /nonexistent 2>/dev/null",
        "systemctl status nginx 2>&1",
        "grep foo bar.log 2>/dev/null | wc -l",
        'echo "=== rows x cols -> files ==="',      # an arrow in a title is not a redirect
        r'awk "{if (\$1 >= 100) print}" data.txt',  # nor is a comparison operator
        "wc -l < /var/log/syslog",                  # input redirection only reads
    ],
)
def test_allows_read_only(command):
    assert guard.check(command) is None, f"false positive on a read-only command: {command}"


def test_still_catches_real_redirection_next_to_quotes():
    # Stripping quotes cuts false positives; it must not let real redirection through
    assert guard.check('echo "hello" > /tmp/out') is not None
    assert guard.check("cat 'a.log' >> b.log") is not None


def test_explain_mentions_write_flag():
    reason = guard.check("rm -rf /tmp/x")
    assert reason is not None
    assert "--write" in guard.explain("rm -rf /tmp/x", reason)
