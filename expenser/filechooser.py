"""Pick files: a native dialog when possible, otherwise paste/drag-and-drop a path in the terminal."""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Callable

from rich.console import Console

__all__ = ["KINDS", "parse_paths", "check_paths", "native_dialog", "choose_files"]

# kind -> (label, extensions)
KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "table": ("Excel / CSV file", (".xlsx", ".xlsm", ".csv")),
    "images": ("Screenshot image", (".png", ".jpg", ".jpeg", ".webp", ".gif")),
    "any": ("File", ()),
}


# --------------------------------------------------------------------------- #
# Parsing what the user pasted / dragged into the terminal
# --------------------------------------------------------------------------- #

def _split_semicolons(line: str) -> list[str]:
    """Split on ';' that is not inside quotes."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in line:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == ";":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def _windows_tokens(segment: str) -> list[str]:
    """Quote-aware whitespace split that keeps backslashes (``C:\\a b\\c.xlsx`` style paths)."""
    tokens: list[str] = []
    buf: list[str] = []
    quote = ""
    has = False
    for ch in segment:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
        elif ch in "\"'":
            quote, has = ch, True
        elif ch.isspace():
            if has or buf:
                tokens.append("".join(buf))
                buf, has = [], False
        else:
            buf.append(ch)
            has = True
    if has or buf:
        tokens.append("".join(buf))
    return tokens


def _strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def parse_paths(text: str, *, windows: bool | None = None) -> list[str]:
    """Split pasted text into paths (not checked for existence, ``~`` expanded).

    Handles surrounding quotes, backslash-escaped spaces (macOS/Linux drag-and-drop), several
    paths separated by newlines, ``;`` or quoted tokens, and unquoted paths containing spaces.
    """
    if windows is None:
        windows = os.name == "nt"
    out: list[str] = []
    for line in text.splitlines():
        for segment in _split_semicolons(line):
            segment = segment.strip()
            if not segment:
                continue
            whole = _strip_quotes(segment)
            if Path(os.path.expanduser(whole)).exists() and (whole != segment or " " in whole):
                out.append(os.path.expanduser(whole))  # one path, spaces and all
                continue
            if windows:
                tokens = _windows_tokens(segment)
            else:
                try:
                    tokens = shlex.split(segment, posix=True)
                except ValueError:  # unbalanced quote: treat everything as one path
                    tokens = [_strip_quotes(segment)]
            out.extend(os.path.expanduser(t) for t in tokens if t.strip())
    return out


def check_paths(
    paths: list[str], extensions: tuple[str, ...] = (), *, multiple: bool = True
) -> tuple[list[Path], list[str]]:
    """Validate paths. Returns ``(good, error messages)``."""
    good: list[Path] = []
    errors: list[str] = []
    if not paths:
        errors.append("No file given.")
    if not multiple and len(paths) > 1:
        errors.append("Please give just one file.")
        return [], errors
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            errors.append(f"File not found: {raw}")
        elif not p.is_file():
            errors.append(f"Not a file: {raw}")
        elif extensions and p.suffix.lower() not in extensions:
            errors.append(f"{p.name}: expected {', '.join(extensions)}")
        else:
            good.append(p)
    return good, errors


# --------------------------------------------------------------------------- #
# Native dialog
# --------------------------------------------------------------------------- #

def native_dialog(kind: str, multiple: bool) -> list[str] | None:
    """Show a native open-file dialog. ``None`` = unavailable (use the prompt); ``[]`` = cancelled."""
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return None
    label, exts = KINDS.get(kind, KINDS["any"])
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        try:
            root.withdraw()
            root.attributes("-topmost", True)
            filetypes = [(label, " ".join(f"*{e}" for e in exts))] if exts else []
            filetypes.append(("All files", "*.*"))
            ask = filedialog.askopenfilenames if multiple else filedialog.askopenfilename
            chosen = ask(parent=root, title=f"Choose: {label}", filetypes=filetypes)
        finally:
            root.destroy()
    except Exception:  # noqa: BLE001 - no tkinter, no display, broken Tk ...
        return None
    if multiple:
        return [str(c) for c in chosen]
    return [str(chosen)] if chosen else []


# --------------------------------------------------------------------------- #
# The chooser
# --------------------------------------------------------------------------- #

def choose_files(
    kind: str,
    multiple: bool = False,
    *,
    input_fn: Callable[[str], str] = input,
    console: Console | None = None,
    use_dialog: bool = True,
) -> list[Path]:
    """Ask for file(s). Returns ``[]`` when the user cancels (empty input or ``q``)."""
    console = console or Console()
    label, exts = KINDS.get(kind, KINDS["any"])
    if use_dialog:
        console.print(f"Opening a file chooser for: {label}{'s' if multiple else ''} ...")
        picked = native_dialog(kind, multiple)
        if picked is not None:
            good, errors = check_paths(picked, exts, multiple=multiple) if picked else ([], [])
            for e in errors:
                console.print(f"[red]{e}[/]")
            return good
        console.print("[dim](No file dialog available here; type the path instead.)[/]")
    what = "path(s)" if multiple else "path"
    kinds = f" ({', '.join(exts)})" if exts else ""
    while True:
        try:
            text = input_fn(
                f"Paste or drag the file {what} here{kinds}, then press Enter (empty = cancel): "
            )
        except (EOFError, KeyboardInterrupt):
            return []
        if not text.strip() or text.strip().lower() == "q":
            return []
        good, errors = check_paths(parse_paths(text), exts, multiple=multiple)
        if not errors:
            return good
        for e in errors:
            console.print(f"[red]{e}[/]")
