from utilities.rescrape_helpers import rescrape_blocks_any_pack_reuse, rescrape_blocks_pack_reuse


def test_rescrape_blocks_any_sibling_pack_while_marker_set():
    item = {
        "title": "The Big Bang Theory",
        "rescrape_original_torrent_title": "The.Big.Bang.Theory.S04.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-NOGRP[rartv]",
    }
    assert rescrape_blocks_any_pack_reuse(item)
    assert rescrape_blocks_pack_reuse(
        item,
        "Some.Other.S04.1080p.WEB-DL-GROUP",
    )


def test_rescrape_blocks_matching_pack_title():
    item = {
        "title": "The Big Bang Theory",
        "rescrape_original_torrent_title": "The.Big.Bang.Theory.S04.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-NOGRP[rartv]",
    }
    assert rescrape_blocks_pack_reuse(
        item,
        "The.Big.Bang.Theory.S04.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-NOGRP[rartv]",
    )


def test_rescrape_blocks_different_pack_title_while_marker_set():
    item = {
        "title": "The Big Bang Theory",
        "rescrape_original_torrent_title": "The.Big.Bang.Theory.S04.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-NOGRP[rartv]",
    }
    assert rescrape_blocks_pack_reuse(
        item,
        "The.Big.Bang.Theory.S04.1080p.WEB-DL.DDP5.1.H264-NTb",
    )


def test_rescrape_allows_reuse_without_rescrape_marker():
    item = {"title": "The Big Bang Theory"}
    assert not rescrape_blocks_pack_reuse(
        item,
        "The.Big.Bang.Theory.S04.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-NOGRP[rartv]",
    )


# Move to Wanted / Plex-report replacements stash the old release in replaced_files.
# The Last of Us S01E04 moved back to Wanted was re-bound to its siblings' WiKi pack,
# the release being replaced, so the replacement never happened.
import json
from unittest import mock

from utilities.rescrape_helpers import replacement_blocks_pack_reuse

WIKI = "The.Last.Of.Us.S01.1080p.BluRay.x265.10bit.Atmos.TrueHD7.1-WiKi"
REPLACING_WIKI = {
    "id": 4, "title": "The Last of Us", "type": "episode", "imdb_id": "tt3581920",
    "season_number": 1, "episode_number": 4, "version": "1080p",
    "replaced_files": json.dumps([{"id": 4, "filled_by_torrent_id": "U2FZYQIFY4V7Y", "filled_by_title": WIKI,
                                   "filled_by_file": "The.Last.Of.Us.S01E04.1080p.BluRay.x265.10bit.Atmos.TrueHD7.1-WiKi.mkv"}]),
}


def test_replacement_blocks_the_pack_being_replaced_by_id_or_title():
    assert replacement_blocks_pack_reuse(REPLACING_WIKI, "obfuscated", "U2FZYQIFY4V7Y")
    assert replacement_blocks_pack_reuse(REPLACING_WIKI, WIKI, "OTHERID")
    assert rescrape_blocks_pack_reuse(REPLACING_WIKI, WIKI, "U2FZYQIFY4V7Y")


def test_replacement_allows_a_different_sibling_pack():
    assert not replacement_blocks_pack_reuse(REPLACING_WIKI, "The.Last.of.Us.S01.1080p.WEB-DL-NTb", "OTHERID")
    assert not rescrape_blocks_pack_reuse(REPLACING_WIKI, "The.Last.of.Us.S01.1080p.WEB-DL-NTb", "OTHERID")


def test_replacement_matches_nzb_job_ids_with_or_without_prefix():
    item = dict(REPLACING_WIKI, replaced_files=json.dumps([{"filled_by_torrent_id": "nzb:abc123", "filled_by_title": "x"}]))
    assert replacement_blocks_pack_reuse(item, "Show.S01.1080p-GRP", "nzb:abc123")
    assert replacement_blocks_pack_reuse(item, "Show.S01.1080p-GRP", "abc123")


def test_rows_without_replaced_files_reuse_as_before():
    plain = {k: v for k, v in REPLACING_WIKI.items() if k != "replaced_files"}
    assert not replacement_blocks_pack_reuse(plain, WIKI, "U2FZYQIFY4V7Y")
    assert not rescrape_blocks_pack_reuse(plain, WIKI, "U2FZYQIFY4V7Y")
    assert not replacement_blocks_pack_reuse(dict(plain, replaced_files="not json"), WIKI, "U2FZYQIFY4V7Y")


class _PackProvider:
    PROVIDER_NAME = "RD"

    def get_torrent_info(self, torrent_id):
        return {"id": torrent_id, "filename": WIKI, "files": [
            {"path": f"/The.Last.Of.Us.S01E0{e}.1080p.BluRay.x265.10bit.Atmos.TrueHD7.1-WiKi.mkv", "bytes": 1}
            for e in range(1, 10)]}


def _sibling_reuse(item):
    import database  # noqa: F401 - app import order
    from queues.torrent_processor import TorrentProcessor
    sibling = {"id": 5, "imdb_id": "tt3581920", "season_number": 1, "episode_number": 5, "version": "1080p",
               "filled_by_torrent_id": "U2FZYQIFY4V7Y", "original_scraped_torrent_title": WIKI,
               "filled_by_title": WIKI, "filled_by_magnet": "magnet:?xt=urn:btih:wiki"}
    provider = _PackProvider()
    processor = TorrentProcessor(provider)
    with mock.patch.object(TorrentProcessor, "_providers", new_callable=mock.PropertyMock, return_value=[provider]), \
         mock.patch("database.get_db_connection", side_effect=RuntimeError("no db in this test")):
        return processor._check_sibling_debrid_pack(item, adding_queue_items=[sibling])


def test_sibling_debrid_pack_still_reused_for_a_normal_episode():
    plain = {k: v for k, v in REPLACING_WIKI.items() if k != "replaced_files"}
    reused = _sibling_reuse(plain)
    assert reused and reused[0]["id"] == "U2FZYQIFY4V7Y"


def test_sibling_debrid_pack_not_reused_when_it_is_the_release_being_replaced():
    assert _sibling_reuse(dict(REPLACING_WIKI)) is None
