import ast
from pathlib import Path


CLIENT = Path(__file__).parents[2] / 'debrid' / 'torbox' / 'client.py'


def _add_torrent_tree():
    tree = ast.parse(CLIENT.read_text())
    provider = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == 'TorboxProvider'
    )
    return next(
        node for node in provider.body
        if isinstance(node, ast.FunctionDef) and node.name == 'add_torrent'
    )


def test_torrent_url_redirect_recovers_hash_for_targeted_climount_sync():
    source = ast.unparse(_add_torrent_tree())

    assert "hash_value = self._extract_hash(_resolved_magnet) or hash_value" in source
    assert "sync_hash = hash_value or info.get('hash')" in source
    assert 'self._notify_climount(sync_hash, torrent_id)' in source


def test_existing_torrent_is_also_repaired_in_climount():
    source = ast.unparse(_add_torrent_tree())

    assert 'self._notify_climount(hash_value, torrent_id)' in source
