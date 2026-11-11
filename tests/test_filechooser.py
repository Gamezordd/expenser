from pathlib import Path

import pytest

from expenser import filechooser as fc


def test_parse_quotes_and_escapes():
    assert fc.parse_paths('"/a/b c/d.xlsx"', windows=False) == ["/a/b c/d.xlsx"]
    assert fc.parse_paths("'/a/b c/d.xlsx'", windows=False) == ["/a/b c/d.xlsx"]
    assert fc.parse_paths(r"/Users/me/My\ File.xlsx ", windows=False) == ["/Users/me/My File.xlsx"]
    assert fc.parse_paths("/a/one.png", windows=False) == ["/a/one.png"]


def test_parse_multiple():
    assert fc.parse_paths("/a/1.png\n/a/2.png", windows=False) == ["/a/1.png", "/a/2.png"]
    assert fc.parse_paths("/a/1.png; /a/2.png", windows=False) == ["/a/1.png", "/a/2.png"]
    assert fc.parse_paths(r'"/a/x y.png" /a/2.png /b/My\ Pic.png', windows=False) == [
        "/a/x y.png", "/a/2.png", "/b/My Pic.png"]
    assert fc.parse_paths('"/a;b/1.png";/a/2.png', windows=False) == ["/a;b/1.png", "/a/2.png"]


def test_parse_windows_style():
    assert fc.parse_paths(r'"C:\Users\Me\My Docs\a.xlsx"', windows=True) == [r"C:\Users\Me\My Docs\a.xlsx"]
    assert fc.parse_paths(r'"C:\a b\1.png" "C:\a b\2.png"', windows=True) == [r"C:\a b\1.png", r"C:\a b\2.png"]
    assert fc.parse_paths(r"C:\a\1.png;C:\a\2.png", windows=True) == [r"C:\a\1.png", r"C:\a\2.png"]


def test_parse_unquoted_path_with_spaces_that_exists(tmp_path):
    d = tmp_path / "My Docs"
    d.mkdir()
    f = d / "e x.xlsx"
    f.write_text("x")
    assert fc.parse_paths(str(f), windows=False) == [str(f)]
    assert fc.parse_paths(f"'{f}'", windows=False) == [str(f)]


def test_parse_expands_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert fc.parse_paths("~/a.xlsx", windows=False) == [str(tmp_path / "a.xlsx")]


def test_check_paths(tmp_path):
    ok = tmp_path / "a.XLSX"
    ok.write_text("x")
    bad = tmp_path / "a.txt"
    bad.write_text("x")
    good, errs = fc.check_paths([str(ok)], (".xlsx",))
    assert good == [ok] and not errs
    _, errs = fc.check_paths([str(bad)], (".xlsx",))
    assert "expected" in errs[0]
    _, errs = fc.check_paths([str(tmp_path / "nope.xlsx")], (".xlsx",))
    assert "not found" in errs[0]
    _, errs = fc.check_paths([str(tmp_path)], ())
    assert "Not a file" in errs[0]
    _, errs = fc.check_paths([str(ok), str(ok)], (), multiple=False)
    assert "just one" in errs[0]


def test_choose_files_prompt_reasks_then_accepts(tmp_path, capsys):
    f = tmp_path / "my file.xlsx"
    f.write_text("x")
    answers = iter(["/nope/x.xlsx", str(tmp_path / "x.png"), f'"{f}"'])
    prompts = []

    def inp(p):
        prompts.append(p)
        return next(answers)

    got = fc.choose_files("table", False, input_fn=inp, use_dialog=False)
    assert got == [f] and len(prompts) == 3


def test_choose_files_multiple_and_cancel(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.jpg"
    a.write_text("x")
    b.write_text("x")
    assert fc.choose_files("images", True, input_fn=lambda _: f"{a}\n{b}", use_dialog=False) == [a, b]
    assert fc.choose_files("images", True, input_fn=lambda _: "", use_dialog=False) == []
    assert fc.choose_files("images", True, input_fn=lambda _: "q", use_dialog=False) == []


def test_dialog_unavailable_falls_back_to_prompt(monkeypatch, tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("x")
    monkeypatch.setattr(fc, "native_dialog", lambda kind, multiple: None)
    assert fc.choose_files("table", False, input_fn=lambda _: str(f)) == [f]
    monkeypatch.setattr(fc, "native_dialog", lambda kind, multiple: [str(f)])
    assert fc.choose_files("table", False, input_fn=lambda _: pytest.fail("no prompt")) == [f]
    monkeypatch.setattr(fc, "native_dialog", lambda kind, multiple: [])
    assert fc.choose_files("table", False, input_fn=lambda _: pytest.fail("no prompt")) == []


def test_native_dialog_without_display_or_tk(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert fc.native_dialog("table", False) is None
    monkeypatch.setenv("DISPLAY", ":99")
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("tkinter"):
            raise ImportError("no tk")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert fc.native_dialog("table", False) is None
