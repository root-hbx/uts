from uts.remote import LOGDIR_PROBE_CAP, facts, parse_facts, q


def test_facts_script_substitutes_the_probe_cap():
    script = facts()
    assert "__CAP__" not in script
    assert f"head -{LOGDIR_PROBE_CAP}" in script


def test_parse_facts_splits_sections():
    parsed = parse_facts(
        "epoch\t1700000000\n"
        "hostname\tbox\n"
        "os\tUbuntu 24.04\n"
        "---disk---\n"
        "/dev/sda1  100G  40G  60G  40% /\n"
        "---logdirs---\n"
        "/var/log\t500\n"
        "/data\t7\n"
    )
    assert parsed["epoch"] == "1700000000"
    assert parsed["os"] == "Ubuntu 24.04"
    assert parsed["disk"] == ["/dev/sda1  100G  40G  60G  40% /"]
    assert parsed["logdirs"] == [("/var/log", "500"), ("/data", "7")]


def test_parse_facts_tolerates_missing_sections():
    parsed = parse_facts("hostname\tbox\n")
    assert parsed["hostname"] == "box"
    assert parsed["disk"] == [] and parsed["logdirs"] == []


def test_parse_facts_keeps_values_containing_spaces_and_tabs():
    parsed = parse_facts("uptime\tup 3 days, 4 hours\n")
    assert parsed["uptime"] == "up 3 days, 4 hours"


def test_quote_neutralises_shell_metacharacters():
    # Correctness, not security: spaces and * in a log path silently break results
    assert q("/var/log/my app/*.log") == "'/var/log/my app/*.log'"
    assert q("$HOME/a.log") == "'$HOME/a.log'"
    assert q("a'b") == "'a'\"'\"'b'"
    assert q("plain.log") == "plain.log"
