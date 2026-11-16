"""Library search: accent folding and battery alias matching."""
import json
import sqlite3

import pytest

from utilities import title_search
from utilities.title_search import (
    alias_imdb_ids, fold_title, like_pattern, register_fold_function, reset_alias_cache,
)


def test_fold_title_strips_accents_and_case():
    assert fold_title('Pokémon') == 'pokemon'
    assert fold_title('AMÉLIE') == 'amelie'
    assert fold_title('Ærø Łódź') == 'aero lodz'
    assert fold_title(None) == ''


def test_like_pattern_escapes_wildcards():
    assert like_pattern('100%_Done') == '%100\\%\\_done%'


def _media_conn():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE media_items (title TEXT, imdb_id TEXT)')
    conn.executemany('INSERT INTO media_items VALUES (?, ?)', [
        ('Pokémon: Detective Pikachu', 'tt5884052'),
        ('La casa de papel', 'tt6468322'),
        ('100% Wolf', 'tt1'),
        ('1000 Wolves', 'tt2'),
    ])
    register_fold_function(conn)
    return conn


@pytest.mark.parametrize('term,expected', [
    ('pokemon', ['Pokémon: Detective Pikachu']),
    ('POKÉMON', ['Pokémon: Detective Pikachu']),
    ('casa de papel', ['La casa de papel']),
    ('100%', ['100% Wolf']),
])
def test_folded_like_matches(term, expected):
    conn = _media_conn()
    rows = conn.execute(
        "SELECT title FROM media_items WHERE FOLD_TITLE(title) LIKE ? ESCAPE '\\'",
        (like_pattern(term),),
    ).fetchall()
    assert [r[0] for r in rows] == expected


@pytest.fixture
def battery_db(tmp_path, monkeypatch):
    path = tmp_path / 'cli_battery.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, imdb_id TEXT)')
    conn.execute('CREATE TABLE metadata (id INTEGER PRIMARY KEY, item_id INTEGER, key TEXT, value TEXT)')
    conn.execute("INSERT INTO items VALUES (1, 'tt6468322'), (2, 'tt0000002')")
    # Battery double-encodes JSON values.
    aliases = json.dumps(json.dumps({'us': ['Money Heist'], 'es': ['La casa de papel']}))
    conn.executemany('INSERT INTO metadata (item_id, key, value) VALUES (?, ?, ?)', [
        (1, 'aliases', aliases),
        (1, 'title', json.dumps('La casa de papel')),
        (2, 'title', json.dumps('Amélie')),
    ])
    conn.commit()
    conn.close()
    monkeypatch.setenv('USER_DB_CONTENT', str(tmp_path))
    reset_alias_cache()
    yield path
    reset_alias_cache()


@pytest.mark.parametrize('term,expected', [
    ('pokemon', ['Pokémon: Detective Pikachu']),
    ('Pokémon', ['Pokémon: Detective Pikachu']),
    ('money heist', ['La casa de papel']),
    ('100%', ['100% Wolf']),
    ('wolf', ['100% Wolf']),
])
def test_title_search_clause(battery_db, term, expected):
    from utilities.title_search import title_search_clause
    conn = _media_conn()
    conn.execute("INSERT INTO media_items VALUES ('Pokemon the Movie 2000', 'tt3')")
    clause, params = title_search_clause(term)
    rows = conn.execute(f'SELECT title FROM media_items WHERE {clause}', params).fetchall()
    titles = [r[0] for r in rows]
    if term.lower().startswith('pok'):
        expected = expected + ['Pokemon the Movie 2000']
    assert titles == expected


def test_alias_lookup_finds_english_title(battery_db):
    assert alias_imdb_ids('money heist') == ['tt6468322']
    assert alias_imdb_ids('MONEY') == ['tt6468322']
    assert alias_imdb_ids('amelie') == ['tt0000002']
    assert alias_imdb_ids('nothing like this') == []


def test_alias_lookup_ignores_short_terms(battery_db):
    assert alias_imdb_ids('mo') == []


def test_alias_lookup_without_battery_db(tmp_path, monkeypatch):
    monkeypatch.setenv('USER_DB_CONTENT', str(tmp_path / 'missing'))
    reset_alias_cache()
    assert alias_imdb_ids('money heist') == []


def test_alias_index_is_cached(battery_db, monkeypatch):
    alias_imdb_ids('money heist')
    calls = []
    monkeypatch.setattr(title_search, '_load_alias_index', lambda p: calls.append(p) or [])
    assert alias_imdb_ids('money heist') == ['tt6468322']
    assert calls == []
