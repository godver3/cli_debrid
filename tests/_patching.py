"""Patch a name everywhere it is bound inside a split package.

Large modules (routes/*_routes.py, utilities/plex_functions.py) became packages:
the package re-exports every name, and each submodule imports the names it uses.
mock.patch.object(package, name) only replaces the package's copy, so code in a
submodule would still call the original. These helpers patch the package and
every submodule that holds the same object.
"""
import contextlib
import sys
from unittest import mock


def _targets(module, name):
    original = getattr(module, name)
    found = [module]
    prefix = module.__name__ + '.'
    for mod_name, mod in list(sys.modules.items()):
        if mod_name.startswith(prefix) and mod is not None and getattr(mod, name, None) is original:
            found.append(mod)
    return found


class patch_in_package:
    """Like mock.patch.object, applied to every copy of `name` in the package.

    Works as a context manager or with start()/stop(), like a mock patcher.
    """

    def __init__(self, module, name, new=mock.DEFAULT, **kwargs):
        self.module, self.name, self.new, self.kwargs = module, name, new, kwargs
        self._stack = None

    def start(self):
        new = mock.MagicMock(**self.kwargs) if self.new is mock.DEFAULT else self.new
        self._stack = contextlib.ExitStack()
        for target in _targets(self.module, self.name):
            self._stack.enter_context(mock.patch.object(target, self.name, new))
        return new

    def stop(self):
        if self._stack is not None:
            self._stack.close()
            self._stack = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def setattr_in_package(monkeypatch, module, name, value):
    """monkeypatch.setattr on the package and every submodule holding the same object."""
    for target in _targets(module, name):
        monkeypatch.setattr(target, name, value)
