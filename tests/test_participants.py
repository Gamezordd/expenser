import pytest

from expenser.participants import load_participants, parse_participants_cell, resolve_name

K = ["Alice", "Bob", "Carol", "Albert"]


def test_load_txt(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("# c\nAlice\n\n  Bob \nalice\nCarol\n")
    assert load_participants(f) == ["Alice", "Bob", "Carol"]


def test_load_csv_header_and_plain(tmp_path):
    f = tmp_path / "p.csv"
    f.write_text("id,name\n1,Alice\n2,Bob\n")
    assert load_participants(f) == ["Alice", "Bob"]
    g = tmp_path / "q.csv"
    g.write_text("Dave,x\nEve,y\n")
    assert load_participants(g) == ["Dave", "Eve"]


def test_load_empty(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("# nothing\n")
    with pytest.raises(ValueError):
        load_participants(f)


def test_resolve():
    assert resolve_name("alice", K) == "Alice"
    assert resolve_name("bo", K) == "Bob"
    assert resolve_name("al", K) is None  # ambiguous
    assert resolve_name("zed", K) is None


def test_cell():
    assert parse_participants_cell("", K) == K
    assert parse_participants_cell("all", K) == K
    assert parse_participants_cell("Alice; bob", K) == ["Alice", "Bob"]
    assert parse_participants_cell("ALL -Bob -Carol", K) == ["Alice", "Albert"]
    assert parse_participants_cell("ALL except Bob, Carol", K) == ["Alice", "Albert"]


def test_cell_unknown():
    with pytest.raises(ValueError, match="Zed"):
        parse_participants_cell("Alice, Zed", K)
