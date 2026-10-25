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


def test_pool_jobs_keep_the_submitting_items_tag(lines):
    from utilities.log_context import ContextThreadPoolExecutor
    logger, out = lines
    with item_log_context({'id': 5}):
        with ContextThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda n: logger.info(f'scraper {n}'), (1, 2)))
            pool.submit(logger.info, 'submitted').result()
    with ContextThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(logger.info, 'no item').result()
    assert sorted(out) == ['[item 5] scraper 1', '[item 5] scraper 2', '[item 5] submitted', 'no item']


@pytest.fixture
def state_lines(app_db, caplog):
    install_log_record_factory()
    conn = app_db()
    conn.execute("INSERT INTO media_items (id, imdb_id, title, type, season_number, episode_number, state, version) "
                 "VALUES (31, 'tt1', 'Show', 'episode', 1, 2, 'Wanted', '1080p')")
    conn.commit()
    conn.close()
    caplog.set_level(logging.INFO)

    def changes():
        return [(r.item_tag, r.getMessage()) for r in caplog.records if ' -> ' in r.getMessage()]
    return changes


def test_database_writers_log_each_state_change(state_lines):
    from database.database_writing import update_media_item, update_media_item_state, update_release_date_and_state
    update_media_item_state(31, 'Scraping')
    update_media_item_state(31, 'Scraping')  # no change, no line
    update_release_date_and_state(31, '2030-01-01', 'Unreleased')
    update_media_item(31, state='Wanted')
    assert state_lines() == [
        ('[item 31] ', 'episode_Show_tt1_S01E02_1080p: Wanted -> Scraping'),
        ('[item 31] ', 'episode_Show_tt1_S01E02_1080p: Scraping -> Unreleased'),
        ('[item 31] ', 'episode_Show_tt1_S01E02_1080p: Unreleased -> Wanted'),
    ]


def test_no_line_when_the_caller_logs_the_change_itself(state_lines):
    from database.database_writing import update_media_item_state
    from utilities.log_context import state_change_logged_by_caller
    with state_change_logged_by_caller():
        update_media_item_state(31, 'Scraping')
    assert state_lines() == []
