"""Reconcile queues, step 1: a Checking row only absorbs rows of the same title and version.

Step 1 paired a Checking row with every other row that had the same filled_by_file.
For a movie, a 1080p row in Checking and a 2160p row already Collected with the same
filename counted as one item, and the 2160p row was deleted. A different title whose
release used the same generic filename was marked Collected as a "season pack sibling".
"""
import pytest

import database  # noqa: F401  (app import order; the runner first is circular)
from queues.run_program import ProgramRunner


def _add(conn, **row):
    cols = ', '.join(row)
    cur = conn.execute(f"INSERT INTO media_items ({cols}) VALUES ({', '.join('?' * len(row))})",
                       list(row.values()))
    conn.commit()
    return cur.lastrowid


def _states(conn):
    return {r['id']: r['state'] for r in conn.execute("SELECT id, state FROM media_items")}


@pytest.fixture
def reconcile(app_db, app_settings):
    def run():
        ProgramRunner.task_reconcile_queues(object())
        return _states(app_db())
    return run


MOVIE = dict(imdb_id='tt1234567', tmdb_id='111', title='Some Movie', type='movie')


def test_other_version_with_same_filename_is_kept(app_db, reconcile):
    conn = app_db()
    uhd = _add(conn, **MOVIE, version='2160p', state='Collected', filled_by_file='Some.Movie.2024.mkv')
    hd = _add(conn, **MOVIE, version='1080p', state='Checking', filled_by_file='Some.Movie.2024.mkv')

    states = reconcile()

    assert states[uhd] == 'Collected'
    assert states[hd] == 'Checking'  # left to the Checking queue, not promoted off another version's file


def test_other_title_with_same_filename_is_untouched(app_db, reconcile):
    conn = app_db()
    other = _add(conn, imdb_id='tt7654321', tmdb_id='222', title='Other Film', type='movie',
                 version='1080p', state='Wanted', filled_by_file='movie.mkv')
    mine = _add(conn, **MOVIE, version='1080p', state='Checking', filled_by_file='movie.mkv')

    states = reconcile()

    assert states[other] == 'Wanted'
    assert states[mine] == 'Checking'


def test_same_version_duplicate_still_reconciled(app_db, reconcile):
    conn = app_db()
    # A library scan's row for the same file, version guessed by the reverse parser ('1080p*').
    scanned = _add(conn, **MOVIE, version='1080p*', state='Collected', filled_by_file='Some.Movie.2024.mkv')
    checking = _add(conn, **MOVIE, version='1080p', state='Checking', filled_by_file='Some.Movie.2024.mkv')

    states = reconcile()

    assert states == {checking: 'Collected'}
    assert scanned not in states


def test_season_pack_siblings_of_same_version_still_collected(app_db, reconcile):
    conn = app_db()
    show = dict(imdb_id='tt0000001', tmdb_id='333', title='Some Show', type='episode', season_number=1)
    pack = 'Some.Show.S01.1080p.mkv'
    sibling = _add(conn, **show, episode_number=2, version='1080p', state='Adding', filled_by_file=pack)
    other_version = _add(conn, **show, episode_number=2, version='2160p', state='Adding', filled_by_file=pack)
    checking = _add(conn, **show, episode_number=1, version='1080p', state='Checking', filled_by_file=pack)

    states = reconcile()

    assert states[checking] == 'Collected'
    assert states[sibling] == 'Collected'
    assert states[other_version] == 'Adding'
