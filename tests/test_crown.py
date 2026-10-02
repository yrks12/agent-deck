from server import manager


def test_crown_round_trips(short_tmp, monkeypatch):
    monkeypatch.setattr(manager, "MANAGER_FILE", short_tmp / "manager.json")
    manager.crown("sid-a", 84795, "orion-4b")
    got = manager.read_crown()
    assert got["session_id"] == "sid-a"
    assert got["pid"] == 84795
    assert got["name"] == "orion-4b"
    assert got["since"] > 0


def test_crowning_someone_else_replaces_the_first(short_tmp, monkeypatch):
    monkeypatch.setattr(manager, "MANAGER_FILE", short_tmp / "manager.json")
    manager.crown("sid-a", 1, "first")
    manager.crown("sid-b", 2, "second")
    assert manager.read_crown()["session_id"] == "sid-b"


def test_uncrown_clears(short_tmp, monkeypatch):
    monkeypatch.setattr(manager, "MANAGER_FILE", short_tmp / "manager.json")
    manager.crown("sid-a", 84795, "orion-4b")
    manager.uncrown()
    assert manager.read_crown() == {}


def test_uncrown_is_safe_when_nobody_is_crowned(short_tmp, monkeypatch):
    monkeypatch.setattr(manager, "MANAGER_FILE", short_tmp / "manager.json")
    assert manager.uncrown() == {}


def test_a_corrupt_file_reads_as_no_manager(short_tmp, monkeypatch):
    path = short_tmp / "manager.json"
    path.write_text("{not json")
    monkeypatch.setattr(manager, "MANAGER_FILE", path)
    assert manager.read_crown() == {}


def test_missing_file_reads_as_no_manager(short_tmp, monkeypatch):
    monkeypatch.setattr(manager, "MANAGER_FILE", short_tmp / "nope.json")
    assert manager.read_crown() == {}


def test_a_json_list_reads_as_no_manager(short_tmp, monkeypatch):
    path = short_tmp / "manager.json"
    path.write_text("[1, 2, 3]")
    monkeypatch.setattr(manager, "MANAGER_FILE", path)
    assert manager.read_crown() == {}
