from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from obsync.desktop import choose_directory


class FakeRoot:
    def withdraw(self) -> None:
        pass

    def attributes(self, *_args) -> None:
        pass

    def destroy(self) -> None:
        pass


def test_native_directory_picker_returns_selected_folder(tmp_path, monkeypatch) -> None:
    filedialog = SimpleNamespace(
        askdirectory=lambda **kwargs: str(tmp_path) if kwargs["mustexist"] else ""
    )
    tkinter = SimpleNamespace(Tk=FakeRoot, filedialog=filedialog)
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", filedialog)
    assert choose_directory("Choose folder", str(tmp_path)) == tmp_path.resolve()


def test_native_directory_picker_rejects_cancel(tmp_path, monkeypatch) -> None:
    filedialog = SimpleNamespace(askdirectory=lambda **_kwargs: "")
    tkinter = SimpleNamespace(Tk=FakeRoot, filedialog=filedialog)
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", filedialog)
    with pytest.raises(ValueError, match="No folder"):
        choose_directory("Choose folder", str(tmp_path))
