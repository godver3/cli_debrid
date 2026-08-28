from utilities.rescrape_helpers import rescrape_blocks_pack_reuse


def test_rescrape_blocks_matching_pack_title():
    item = {
        "title": "The Big Bang Theory",
        "rescrape_original_torrent_title": "The.Big.Bang.Theory.S04.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-NOGRP[rartv]",
    }
    assert rescrape_blocks_pack_reuse(
        item,
        "The.Big.Bang.Theory.S04.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-NOGRP[rartv]",
    )


def test_rescrape_allows_different_pack_title():
    item = {
        "title": "The Big Bang Theory",
        "rescrape_original_torrent_title": "The.Big.Bang.Theory.S04.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-NOGRP[rartv]",
    }
    assert not rescrape_blocks_pack_reuse(
        item,
        "The.Big.Bang.Theory.S04.1080p.WEB-DL.DDP5.1.H264-NTb",
    )


def test_rescrape_allows_reuse_without_rescrape_marker():
    item = {"title": "The Big Bang Theory"}
    assert not rescrape_blocks_pack_reuse(
        item,
        "The.Big.Bang.Theory.S04.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-NOGRP[rartv]",
    )
