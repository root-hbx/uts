import json

import pytest

from uts.inventory import InventoryError, load_inventory, select


def write(tmp_path, data):
    p = tmp_path / "hosts.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_minimal_array_form(tmp_path):
    p = write(tmp_path, [{"ip": "10.0.0.1", "user": "ops", "password": "pw"}])
    (host,) = load_inventory(p)
    assert (host.name, host.ip, host.user, host.port) == ("10.0.0.1", "10.0.0.1", "ops", 22)


def test_user_defaults_to_root(tmp_path):
    p = write(tmp_path, [{"ip": "10.0.0.1", "password": "pw"}])
    assert load_inventory(p)[0].user == "root"


def test_optional_fields(tmp_path):
    p = write(
        tmp_path,
        [
            {"name": "a", "ip": "10.0.0.1", "user": "ops", "password": "pw",
             "port": 2222, "timeout": 3},
            {"name": "b", "ip": "10.0.0.2", "password": "pw"},
        ],
    )
    a, b = load_inventory(p)
    assert (a.user, a.port, a.timeout) == ("ops", 2222, 3.0)
    assert (b.user, b.port) == ("root", 22)


def test_unknown_fields_are_ignored(tmp_path):
    # `tags` was a selector feature once. An inventory that still carries one has
    # to keep working rather than fail to load.
    p = write(tmp_path, [{"name": "a", "ip": "10.0.0.1", "password": "pw",
                          "tags": ["prod"], "nickname": "old faithful"}])
    assert load_inventory(p)[0].name == "a"


@pytest.mark.parametrize(
    "data, needle",
    [
        ([{"user": "ops", "password": "pw"}], 'has no "ip"'),
        ([{"ip": "10.0.0.1"}], 'has no "password"'),
        ([{"ip": "10.0.0.1", "password": "pw"}, {"ip": "10.0.0.1", "password": "pw"}],
         "duplicate host name"),
        ({"hosts": []}, "must hold an array"),
        ([], "lists no hosts"),
    ],
)
def test_rejects_broken_inventory(tmp_path, data, needle):
    with pytest.raises(InventoryError, match=needle):
        load_inventory(write(tmp_path, data))


def test_rejects_invalid_json(tmp_path):
    p = tmp_path / "hosts.json"
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(InventoryError, match="not valid JSON"):
        load_inventory(p)


def test_missing_file_message_shows_how_to_fix(tmp_path):
    with pytest.raises(InventoryError, match="hosts.example.json"):
        load_inventory(tmp_path / "nope.json")


@pytest.fixture
def hosts(tmp_path):
    return load_inventory(
        write(
            tmp_path,
            [
                {"name": "a", "ip": "192.168.1.11", "password": "pw"},
                {"name": "b", "ip": "192.168.1.12", "password": "pw"},
                {"name": "c", "ip": "10.0.0.9", "password": "pw"},
            ],
        )
    )


@pytest.mark.parametrize(
    "names, expected",
    [
        (["b"], ["b"]),
        (["a,c"], ["a", "c"]),                # comma-separated
        (["a", "c"], ["a", "c"]),             # repeated -H
        (["c,a"], ["a", "c"]),                # inventory order, not argument order
        (["c", "a"], ["a", "c"]),
        (["b,a,b"], ["a", "b"]),              # deduplicated
        ([" a , c "], ["a", "c"]),            # whitespace tolerated
    ],
)
def test_named_selection(hosts, names, expected):
    assert [h.name for h in select(hosts, names)] == expected


def test_all_takes_everything(hosts):
    assert [h.name for h in select(hosts, None, all_=True)] == ["a", "b", "c"]


def test_no_target_is_an_error_not_a_default(hosts):
    # "every machine I own" must be asked for, never arrived at by omission.
    with pytest.raises(InventoryError, match="Known hosts: a, b, c"):
        select(hosts, None)
    with pytest.raises(InventoryError, match="no host selected"):
        select(hosts, [])


def test_all_and_host_together_are_rejected(hosts):
    with pytest.raises(InventoryError, match="cannot be combined"):
        select(hosts, ["a"], all_=True)


def test_unknown_name_lists_known_hosts(hosts):
    with pytest.raises(InventoryError, match="no host named 'nope'. Known hosts: a, b, c"):
        select(hosts, ["nope"])


@pytest.mark.parametrize("gone", ["@prod", "192.168.1.*", "10.0.0.9", "all"])
def test_tags_globs_ips_and_all_are_no_longer_selectors(hosts, gone):
    # One spelling, one meaning: the "name" field. Each of these used to resolve to
    # something, and each made a bare word ambiguous at a glance.
    with pytest.raises(InventoryError, match="no host named"):
        select(hosts, [gone])
