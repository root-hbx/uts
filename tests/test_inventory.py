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
             "port": 2222, "timeout": 3, "tags": ["prod", "web"]},
            {"name": "b", "ip": "10.0.0.2", "password": "pw"},
        ],
    )
    a, b = load_inventory(p)
    assert (a.user, a.port, a.timeout) == ("ops", 2222, 3.0)
    assert a.tags == ("prod", "web")
    assert (b.user, b.port, b.tags) == ("root", 22, ())


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
                {"name": "a", "ip": "192.168.1.11", "password": "pw", "tags": ["prod", "web"]},
                {"name": "b", "ip": "192.168.1.12", "password": "pw", "tags": ["prod"]},
                {"name": "c", "ip": "10.0.0.9", "password": "pw"},
            ],
        )
    )


@pytest.mark.parametrize(
    "selector, expected",
    [
        (None, ["a", "b", "c"]),
        ("all", ["a", "b", "c"]),
        ("b", ["b"]),
        ("10.0.0.9", ["c"]),          # selecting by IP works too
        ("a,c", ["a", "c"]),
        ("@prod", ["a", "b"]),
        ("192.168.1.*", ["a", "b"]),
        ("@prod,b,a", ["a", "b"]),    # deduplicated, inventory order preserved
    ],
)
def test_selectors(hosts, selector, expected):
    assert [h.name for h in select(hosts, selector)] == expected


def test_unknown_selector_lists_known_hosts(hosts):
    with pytest.raises(InventoryError, match="Known hosts: a, b, c"):
        select(hosts, "nope")
