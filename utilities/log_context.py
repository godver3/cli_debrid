"""Tag every log line written while an item is being processed with that item's ID.

The queues write their per-item logs from many functions, mostly naming the item by
title. While code runs inside item_log_context(item) (or a function decorated with
@logs_for_item), every log record gets an ``item_tag`` of ``[item 123] `` that the
debug log and console formats print, so ``grep '\\[item 123\\]' debug.log`` shows one
item's whole path through Wanted, Scraping, Adding, Checking and Collected.

Contexts nest: an inner item (a sibling processed for another item) gets its own tag
for as long as it's handled, then the outer one comes back. Context variables don't
cross into new threads, so work handed to a thread pool isn't tagged.
"""
import contextlib
import contextvars
import functools
import inspect
import logging

_current_item_id = contextvars.ContextVar('cli_debrid_log_item_id', default=None)
_ITEM_ARG_NAMES = ('item', 'item_to_process', 'queue_item', 'media_item')


def current_item_tag() -> str:
    item_id = _current_item_id.get()
    return f"[item {item_id}] " if item_id is not None else ''


@contextlib.contextmanager
def item_log_context(item):
    """Tag log lines in this block with the item's ID (no-op for items without one)."""
    item_id = item.get('id') if isinstance(item, dict) else None
    if item_id is None:
        yield
        return
    token = _current_item_id.set(item_id)
    try:
        yield
    finally:
        _current_item_id.reset(token)


def logs_for_item(func):
    """Decorator: run func inside item_log_context for its item argument.

    The item is found by parameter name (item, item_to_process, queue_item, media_item).
    """
    sig = inspect.signature(func)
    names = [n for n in _ITEM_ARG_NAMES if n in sig.parameters]
    if not names:
        raise TypeError(f"{func.__qualname__} has no item parameter")
    name = names[0]

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            item = sig.bind_partial(*args, **kwargs).arguments.get(name)
        except TypeError:
            item = None  # let func raise its own argument error
        with item_log_context(item):
            return func(*args, **kwargs)
    return wrapper


def install_log_record_factory() -> None:
    """Give every log record an ``item_tag`` attribute (empty outside an item context)."""
    old_factory = logging.getLogRecordFactory()
    if getattr(old_factory, '_cli_debrid_item_tag', False):
        return

    def factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        record.item_tag = current_item_tag()
        return record

    factory._cli_debrid_item_tag = True
    logging.setLogRecordFactory(factory)
