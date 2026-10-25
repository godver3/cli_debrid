"""Log lines written while an item is processed carry "[item <id>] " (utilities.log_context)."""
import logging

import pytest

from utilities.log_context import install_log_record_factory, item_log_context, logs_for_item
from utilities.log_redaction import RedactingFormatter


@pytest.fixture
def lines():
    install_log_record_factory()
    out = []

    class Collect(logging.Handler):
        def emit(self, record):
            out.append(self.format(record))

    handler = Collect()
    handler.setFormatter(RedactingFormatter('%(item_tag)s%(message)s'))
    logger = logging.getLogger('test_log_context')
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield logger, out
    logger.removeHandler(handler)


def test_lines_inside_an_item_context_are_tagged(lines):
    logger, out = lines
    logger.info('before')
    with item_log_context({'id': 42, 'title': 'Show'}):
        logger.info('scraping')
    logger.info('after')
    assert out == ['before', '[item 42] scraping', 'after']


def test_contexts_nest_and_restore(lines):
    logger, out = lines
    with item_log_context({'id': 1}):
        with item_log_context({'id': 2}):
            logger.info('sibling')
        logger.info('back')
    assert out == ['[item 2] sibling', '[item 1] back']


def test_items_without_an_id_leave_lines_untagged(lines):
    logger, out = lines
    with item_log_context({'title': 'not saved yet'}), item_log_context(None):
        logger.info('x')
    assert out == ['x']


def test_decorator_finds_the_item_argument(lines):
    logger, out = lines

    class Queue:
        @logs_for_item
        def move(self, item, from_queue):
            logger.info(f'from {from_queue}')

    @logs_for_item
    def process(results, item_to_process=None):
        logger.info('processing')

    Queue().move({'id': 7}, 'Scraping')
    Queue().move(from_queue='Adding', item={'id': 8})
    process([], item_to_process={'id': 9})
    assert out == ['[item 7] from Scraping', '[item 8] from Adding', '[item 9] processing']


def test_decorator_needs_an_item_parameter():
    with pytest.raises(TypeError):
        logs_for_item(lambda results: None)


def test_factory_is_installed_once():
    install_log_record_factory()
    factory = logging.getLogRecordFactory()
    install_log_record_factory()
    assert logging.getLogRecordFactory() is factory


def test_formatter_works_on_records_without_the_tag():
    # Built directly, not through the record factory, so it has no item_tag.
    record = logging.LogRecord('x', logging.INFO, __file__, 1, 'plain', None, None)
    assert not hasattr(record, 'item_tag')
    assert RedactingFormatter('%(item_tag)s%(message)s').format(record) == 'plain'
