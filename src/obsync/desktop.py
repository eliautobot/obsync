from __future__ import annotations

from pathlib import Path


def choose_directory(title: str, initial: str = "") -> Path:
    """Open the operating system's folder chooser from a native Obsync process."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:  # pragma: no cover - platform packaging guard
        raise ValueError("A desktop folder picker is not available on this installation") from exc

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            parent=root,
            title=title,
            initialdir=initial or None,
            mustexist=True,
        )
    finally:
        root.destroy()
    if not selected:
        raise ValueError("No folder was selected")
    return Path(selected).expanduser().resolve()
