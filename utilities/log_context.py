"""Tag every log line written while an item is being processed with that item's ID.

The queues write their per-item logs from many functions, mostly naming the item by
title. While code runs inside item_log_context(item) (or a function decorated with
@logs_for_item), every log record gets an ``item_tag`` of ``[item 123] `` that the
debug log and console formats print, so ``grep '\\[item 123\\]' debug.log`` shows one
item's whole path through Wanted, Scraping, Adding, Checking and Collected.

Contexts nest: an inner item (a sibling processed for another item) gets its own tag
for as long as it's handled, then the outer one comes back. Context variables don't
cross into new threads on their own: pools that run work for an item use
ContextThreadPoolExecutor, which hands each job the submitting thread's context.

log_state_change() writes the one INFO line per state change ("A -> B"). The
database writers call it, so moves made outside the queue manager (the
release-date refresh, routes) show up too.
"""
import contextlib
import contextvars
import functools
import inspect
import logging
from concurrent.futures import ThreadPoolExecutor

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


class ContextThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor whose jobs run in a copy of the submitting thread's context,
    so their log lines keep the item tag. (map() goes through submit() too.)"""

    def submit(self, fn, /, *args, **kwargs):
        # One copy per job: a Context can't be entered by two threads at once.
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


# Set while a caller (QueueManager._move_item_to_queue) writes its own, richer
# state-change line, so the database writer doesn't log the same change again.
_state_line_by_caller = contextvars.ContextVar('cli_debrid_state_line_by_caller', default=False)


@contextlib.contextmanager
def state_change_logged_by_caller():
    token = _state_line_by_caller.set(True)
    try:
        yield
    finally:
        _state_line_by_caller.reset(token)


def item_identifier(item) -> str:
    """Same shape as QueueManager.generate_identifier, without raising on odd rows."""
    title, imdb_id, version = item.get('title') or 'Unknown', item.get('imdb_id') or 'Unknown', item.get('version') or 'Unknown'
    if item.get('type') == 'episode':
        season, episode = item.get('season_number'), item.get('episode_number')
        return (f"episode_{title}_{imdb_id}_S{season or 0:02d}E{episode or 0:02d}_{version}")
    return f"{item.get('type') or 'movie'}_{title}_{imdb_id}_{version}"


def log_state_change(item, old_state) -> None:
    """INFO "<item>: Old -> New" when a database write changed the item's state."""
    new_state = item.get('state') if item else None
    if not item or old_state == new_state or _state_line_by_caller.get():
        return
    with item_log_context(item):
        logging.info(f"{item_identifier(item)}: {old_state or 'new'} -> {new_state}")
