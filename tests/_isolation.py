"""Keep test stubs of app modules from leaking into other tests.

Many tests load the module under test against stand-ins for its imports
(utilities.settings, routes.api_tracker, PTT, ...). Installed straight into
sys.modules at import time, those stand-ins stayed for the whole session:
pytest imports every test file during collection, so a stub from one file
replaced the real module for tests in others, and results depended on order.

Decorate the loader with @isolated_modules instead. The module it returns
keeps the stubs it imported; sys.modules is put back as it was the moment the
loader returns.
"""
import functools
import sys
from unittest import mock


def isolated_modules(loader):
    @functools.wraps(loader)
    def wrapper(*args, **kwargs):
        with mock.patch.dict(sys.modules):
            return loader(*args, **kwargs)
    return wrapper
