"""
Junk symlink audit — scan symlink library + mount for bad/split/NZB-mismatch files.

Used by Debug > Junk Files in debrid_manager and by scripts/junk_symlink_cleanup.py.

Junk detection (any of):
  - filename contains sample/trailer
  - episode symlink target below min episode threshold (default 200 MiB)
  - movie symlink target below min movie threshold (default 300 MiB)
  - file is < 5% of the largest video in the same mount folder (split junk)
  - NZB item whose actual size is < 10% of advertised scrape size

DB action when no good sibling: reset Collected/Checking rows to Wanted.
When a good Collected sibling exists, the junk duplicate row is deleted instead.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".webm", ".mpg", ".mpeg", ".m2ts"
}
RELATIVE_JUNK_RATIO = 0.05
RESET_COLLECTION_STATE_SQL = "collected_at = NULL, original_collected_at = NULL"

# Below this size, always treat as junk (trailers/scams) regardless of show age.
TINY_JUNK_BYTES = 100 * 1024 * 1024
# Absolute-threshold hits above this may be legitimate old rips — send to review.
REVIEW_FLOOR_BYTES = 80 * 1024 * 1024
OLD_SHOW_YEAR_CUTOFF = 2012
OLD_RELEASE_RE = re.compile(
    r"dvdrip|hdtv|xvid|divx|\bsd\b|480p|proper\.repack|vhs|tvrip",
    re.I,
)


def is_video_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS


def is_unwanted_name(name: str) -> bool:
    n = name.lower()
    return "sample" in n or "trailer" in n


def resolve_symlink_target(link_path: str) -> str:
    raw = os.readlink(link_path)
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(os.path.dirname(link_path), raw))


def fmt_mb(size: int) -> str:
    return f"{size / (1024 ** 2):.1f} MiB"


def fmt_gb(size: int) -> str:
    return f"{size / (1024 ** 3):.2f} GiB"


def item_label(row: Dict[str, Any]) -> str:
    if row.get("type") == "episode":
        return (
            f"{row.get('title')} S{int(row.get('season_number') or 0):02d}"
            f"E{int(row.get('episode_number') or 0):02d} ({row.get('version')})"
        )
    return f"{row.get('title')} ({row.get('version')})"


def scrape_results(item: Dict[str, Any]) -> list:
    results = item.get("scrape_results") or []
    if isinstance(results, str):
        try:
            results = json.loads(results)
        except (TypeError, ValueError):
            return []
    return results if isinstance(results, list) else []


def advertised_size_bytes(item: Dict[str, Any], nzb_title: str) -> Optional[int]:
    selected_url = item.get("filled_by_magnet") or ""
    title_match = None
    for result in scrape_results(item):
        if not isinstance(result, dict):
            continue
        result_url = result.get("nzb_url") or result.get("magnet") or ""
        result_title = result.get("title") or result.get("original_title") or ""
        if selected_url and result_url == selected_url:
            title_match = result
            break
        if result_title == nzb_title and title_match is None:
            title_match = result
    if not title_match:
        return None
    raw_size = title_match.get("total_size_gb", title_match.get("size"))
    try:
        size_gib = float(raw_size)
    except (TypeError, ValueError):
        return None
    if size_gib <= 0:
        return None
    return int(size_gib * (1024 ** 3))


def nzb_size_mismatch(item: Dict[str, Any], actual_size: int, minimum_ratio: float = 0.10) -> Optional[Tuple[int, int, float]]:
    media_type = str(item.get("type") or "").lower()
    if media_type not in ("movie", "episode"):
        return None
    nzb_title = (
        item.get("original_scraped_torrent_title")
        or item.get("filled_by_title")
        or item.get("filled_by_file")
        or ""
    )
    if media_type == "episode" and not re.search(r"[Ss]\d{2}[Ee]\d{2}(?!\d)", nzb_title or ""):
        return None
    expected = advertised_size_bytes(item, nzb_title)
    if not expected or actual_size <= 0:
        return None
    ratio = actual_size / expected
    if ratio >= minimum_ratio:
        return None
    return actual_size, expected, ratio


def db_library_size_bytes(item: Dict[str, Any]) -> Optional[int]:
    """Parse media_items.size (GiB float shown on library page)."""
    raw = item.get("size")
    if raw is None or raw == "":
        return None
    try:
        size_gib = float(raw)
    except (TypeError, ValueError):
        return None
    if size_gib <= 0:
        return None
    return int(size_gib * (1024 ** 3))


def library_vs_disk_note(item: Dict[str, Any], actual_size: int) -> Optional[str]:
    """Explain when library UI size (advertised scrape) diverges from disk."""
    library_bytes = db_library_size_bytes(item)
    if not library_bytes or actual_size <= 0:
        return None
    ratio = actual_size / library_bytes
    # Meaningful divergence: library claims at least ~500 MiB and disk is <75% of that.
    if library_bytes < 500 * 1024 * 1024 or ratio >= 0.75:
        return None
    pct = ratio * 100
    return (
        f"library page shows {float(item['size']):.2f} GiB (advertised scrape size) "
        f"but file on disk is {fmt_mb(actual_size)} ({pct:.1f}% of library size)"
    )


def content_key(item: Dict[str, Any]) -> Tuple[Any, ...]:
    """Identity for duplicate detection: same episode/movie + version."""
    if str(item.get("type") or "").lower() == "episode":
        return (
            "ep",
            item.get("imdb_id"),
            int(item.get("season_number") or 0),
            int(item.get("episode_number") or 0),
            item.get("version") or "",
        )
    return ("movie", item.get("imdb_id"), item.get("year"), item.get("version") or "")


def is_good_file_size(
    size: int,
    media_type: str,
    min_episode_bytes: int,
    min_movie_bytes: int,
) -> bool:
    if size <= 0:
        return False
    threshold = min_movie_bytes if media_type == "movie" else min_episode_bytes
    return size >= threshold


def symlink_target_size(link_path: str) -> int:
    if not link_path or not os.path.islink(link_path):
        return 0
    target = resolve_symlink_target(link_path)
    try:
        return os.path.getsize(target) if os.path.exists(target) else 0
    except OSError:
        return 0


def build_good_sibling_index(
    collected_items: List[Dict[str, Any]],
    min_episode_bytes: int,
    min_movie_bytes: int,
) -> Dict[Tuple[Any, ...], List[Dict[str, Any]]]:
    """Map content key -> Collected rows whose symlink target passes size floor."""
    index: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = defaultdict(list)
    for item in collected_items:
        if str(item.get("state") or "") != "Collected":
            continue
        loc = item.get("location_on_disk")
        if not loc:
            continue
        size = symlink_target_size(loc)
        media_type = str(item.get("type") or "episode").lower()
        if not is_good_file_size(size, media_type, min_episode_bytes, min_movie_bytes):
            continue
        index[content_key(item)].append(
            {"id": item["id"], "size": size, "label": item_label(item)}
        )
    return index


def load_collected_items(db_path: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, imdb_id, title, type, year, season_number, episode_number, version, state,
               location_on_disk
        FROM media_items
        WHERE state = 'Collected'
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def junk_confidence(item: Dict[str, Any], reason: str, size: int) -> str:
    """Return 'high' (likely junk) or 'review' (older/smaller — verify before fixing)."""
    rl = (reason or "").lower()
    if any(k in rl for k in ("sample", "trailer", "nzb size mismatch", "relative junk")):
        return "high"
    if size > 0 and size < TINY_JUNK_BYTES:
        return "high"

    if "under" in rl and "mib" in rl and size >= REVIEW_FLOOR_BYTES:
        title = (
            item.get("filled_by_title")
            or item.get("filled_by_file")
            or item.get("original_scraped_torrent_title")
            or ""
        )
        try:
            year = int(item["year"]) if item.get("year") not in (None, "") else None
        except (TypeError, ValueError):
            year = None
        media_type = str(item.get("type") or "").lower()
        season = int(item.get("season_number") or 0)
        old_show = year is not None and year <= OLD_SHOW_YEAR_CUTOFF
        old_release = bool(OLD_RELEASE_RE.search(title))
        early_season = media_type == "episode" and season > 0 and season <= 5 and old_show
        if old_release or old_show or early_season:
            return "review"

    return "high"


def classify_junk(
    path: str,
    size: int,
    media_type: str,
    folder_max: Optional[int],
    min_episode_bytes: int,
    min_movie_bytes: int,
) -> Optional[str]:
    name = os.path.basename(path)
    if is_unwanted_name(name):
        return "sample/trailer name"
    if media_type == "episode" and size < min_episode_bytes:
        return f"episode under {min_episode_bytes // (1024 * 1024)} MiB"
    if media_type == "movie" and size < min_movie_bytes:
        return f"movie under {min_movie_bytes // (1024 * 1024)} MiB"
    if folder_max and folder_max > 0 and size < folder_max * RELATIVE_JUNK_RATIO:
        if size < 500 * 1024 * 1024:
            return f"relative junk ({fmt_mb(size)} vs folder max {fmt_gb(folder_max)})"
    return None


def load_db_rows(db_path: str) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, imdb_id, title, type, year, season_number, episode_number, version, state, size,
               location_on_disk, original_path_for_symlink, filled_by_file,
               filled_by_title, filled_by_magnet, filled_by_torrent_id,
               original_scraped_torrent_title, scrape_results
        FROM media_items
        WHERE state IN ('Collected', 'Checking')
          AND location_on_disk IS NOT NULL
          AND location_on_disk != ''
        """
    ).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    by_location: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        loc = item.get("location_on_disk") or ""
        if loc:
            by_location[os.path.normpath(loc)].append(item)
    return items, by_location


def index_mount_videos(mount_path: str) -> Dict[str, List[Tuple[str, int]]]:
    """folder -> [(filename, size), ...]"""
    by_folder: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    if not os.path.isdir(mount_path):
        return by_folder
    for entry in os.scandir(mount_path):
        if not entry.is_dir(follow_symlinks=False):
            continue
        folder = entry.path
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            fp = os.path.join(folder, name)
            if not os.path.isfile(fp) or not is_video_file(fp):
                continue
            try:
                by_folder[folder].append((name, os.path.getsize(fp)))
            except OSError:
                pass
    return by_folder


def index_symlinks(symlink_root: str) -> Tuple[Dict[str, List[str]], List[str]]:
    """target -> [symlink paths], broken symlinks"""
    target_to_links: Dict[str, List[str]] = defaultdict(list)
    broken: List[str] = []
    for root, _dirs, files in os.walk(symlink_root, followlinks=False):
        for name in files:
            link = os.path.join(root, name)
            if not os.path.islink(link):
                continue
            target = resolve_symlink_target(link)
            if os.path.exists(target):
                target_to_links[os.path.normpath(target)].append(link)
            else:
                broken.append(link)
    return target_to_links, broken


def reset_item_to_wanted(conn: sqlite3.Connection, item_id: int, original_scraped: Optional[str], version: Optional[str]) -> None:
    conn.execute(
        f"""
        UPDATE media_items
           SET state = 'Wanted',
               blacklisted_date = NULL,
               ghostlisted = 0,
               sleep_cycles = 0,
               wake_count = 0,
               location_on_disk = NULL,
               original_path_for_symlink = NULL,
               filled_by_file = NULL,
               filled_by_title = NULL,
               filled_by_magnet = NULL,
               filled_by_torrent_id = NULL,
               debrid_folder_name = NULL,
               scrape_results = NULL,
               nzb_segment_id = NULL,
               real_debrid_original_title = NULL,
               location_basename = NULL,
               {RESET_COLLECTION_STATE_SQL},
               rescrape_original_torrent_title = original_scraped_torrent_title,
               original_scraped_torrent_title = NULL,
               upgrading_from = NULL,
               upgrading = NULL,
               version = ?,
               fall_back_to_single_scraper = 0,
               last_updated = ?
         WHERE id = ?
        """,
        (version, datetime.now(), item_id),
    )


def build_junk_symlink_plan(
    *,
    db_path: str,
    symlink_root: str,
    mount_path: str,
    exclude_ids: Optional[Set[int]] = None,
    min_episode_mb: int = 200,
    min_movie_mb: int = 300,
) -> Dict[str, Any]:
    """Scan symlink library + mount and return an audit plan (no mutations)."""
    exclude_ids = exclude_ids or set()
    min_episode = min_episode_mb * 1024 * 1024
    min_movie = min_movie_mb * 1024 * 1024

    db_items, by_location = load_db_rows(db_path)
    collected_items = load_collected_items(db_path)
    good_siblings = build_good_sibling_index(collected_items, min_episode, min_movie)
    mount_videos = index_mount_videos(mount_path)
    target_to_links, _broken_symlinks = index_symlinks(symlink_root)

    folder_max_size: Dict[str, int] = {}
    for folder, vids in mount_videos.items():
        if vids:
            folder_max_size[folder] = max(sz for _, sz in vids)

    bad_symlinks: List[Dict[str, Any]] = []
    review_symlinks: List[Dict[str, Any]] = []
    db_ids_to_reset: Dict[int, Dict[str, Any]] = {}
    db_ids_duplicate_delete: Dict[int, Dict[str, Any]] = {}
    review_db_ids_to_reset: Dict[int, Dict[str, Any]] = {}
    review_db_ids_duplicate_delete: Dict[int, Dict[str, Any]] = {}
    mount_files_to_delete: Dict[str, str] = {}
    bad_symlink_paths: Set[str] = set()
    junk_targets: Dict[str, str] = {}

    for item in db_items:
        if item["id"] in exclude_ids:
            continue
        loc = os.path.normpath(item["location_on_disk"])
        if not os.path.islink(loc):
            continue
        target = resolve_symlink_target(loc)
        target_norm = os.path.normpath(target)

        try:
            size = os.path.getsize(target) if os.path.exists(target) else 0
        except OSError:
            size = 0

        folder = os.path.dirname(target_norm)
        folder_max = folder_max_size.get(folder)
        media_type = str(item.get("type") or "episode").lower()

        reason = classify_junk(
            target_norm, size, media_type, folder_max, min_episode, min_movie
        )
        mismatch = nzb_size_mismatch(item, size) if size > 0 else None
        if mismatch and not reason:
            _actual, expected, ratio = mismatch
            reason = f"NZB size mismatch ({ratio * 100:.1f}% of advertised {fmt_gb(expected)})"

        if not reason:
            continue

        key = content_key(item)
        siblings = [s for s in good_siblings.get(key, []) if s["id"] != item["id"]]
        skip_wanted = bool(siblings)

        confidence = junk_confidence(item, reason, size)
        if skip_wanted:
            bucket = review_db_ids_duplicate_delete if confidence == "review" else db_ids_duplicate_delete
            bucket[item["id"]] = {
                **item,
                "good_sibling_ids": [s["id"] for s in siblings],
                "good_sibling_labels": [s["label"] for s in siblings],
            }
        else:
            (review_db_ids_to_reset if confidence == "review" else db_ids_to_reset)[item["id"]] = item
        junk_targets[target_norm] = reason

        if loc not in bad_symlink_paths:
            bad_symlink_paths.add(loc)
            divergence = library_vs_disk_note(item, size)
            entry = {
                "symlink": loc,
                "target": target_norm,
                "size": size,
                "size_str": fmt_mb(size),
                "library_size_gb": item.get("size"),
                "size_divergence_note": divergence,
                "reason": reason,
                "confidence": confidence,
                "item_id": item["id"],
                "label": item_label(item),
                "skip_wanted_reset": skip_wanted,
            }
            if skip_wanted:
                entry["good_sibling_ids"] = [s["id"] for s in siblings]
                entry["skip_reason"] = (
                    f"good sibling already Collected (id {siblings[0]['id']}, "
                    f"{fmt_mb(siblings[0]['size'])})"
                )
            if confidence == "review":
                entry["review_note"] = (
                    "Older or legacy release — file is small but may be a legitimate "
                    "old-season rip; verify before fixing."
                )
                review_symlinks.append(entry)
            else:
                bad_symlinks.append(entry)

    mount_root = os.path.normpath(mount_path)
    for target_norm, reason in junk_targets.items():
        if not (target_norm.startswith(mount_root + os.sep) or target_norm == mount_root):
            continue
        links = target_to_links.get(target_norm, [])
        if links and all(link in bad_symlink_paths for link in links):
            mount_files_to_delete[target_norm] = reason

    for folder, vids in mount_videos.items():
        if len(vids) < 2:
            continue
        max_sz = max(sz for _, sz in vids)
        for name, sz in vids:
            fp = os.path.normpath(os.path.join(folder, name))
            if fp in mount_files_to_delete:
                continue
            if fp in target_to_links:
                continue
            if is_unwanted_name(name):
                mount_files_to_delete[fp] = "sample/trailer name (orphan)"
                continue
            if max_sz > 0 and sz < max_sz * RELATIVE_JUNK_RATIO and sz < 500 * 1024 * 1024:
                mount_files_to_delete[fp] = (
                    f"relative junk orphan ({fmt_mb(sz)} vs folder max {fmt_gb(max_sz)})"
                )

    def _db_reset_rows(items_map):
        return [
            {
                "id": item["id"],
                "state": item.get("state"),
                "label": item_label(item),
                "location_on_disk": item.get("location_on_disk"),
                "filled_by_title": item.get("filled_by_title"),
                "version": item.get("version"),
                "original_scraped_torrent_title": item.get("original_scraped_torrent_title"),
            }
            for item in sorted(items_map.values(), key=lambda x: x["id"])
        ]

    def _db_dup_rows(items_map):
        return [
            {
                "id": item["id"],
                "label": item_label(item),
                "good_sibling_ids": item.get("good_sibling_ids", []),
                "good_sibling_labels": item.get("good_sibling_labels", []),
                "filled_by_title": item.get("filled_by_title"),
            }
            for item in sorted(items_map.values(), key=lambda x: x["id"])
        ]

    return {
        "bad_symlinks": bad_symlinks,
        "review_symlinks": review_symlinks,
        "mount_files_to_delete": [
            {"path": p, "reason": r} for p, r in sorted(mount_files_to_delete.items())
        ],
        "db_items_to_reset": _db_reset_rows(db_ids_to_reset),
        "db_duplicate_deletes": _db_dup_rows(db_ids_duplicate_delete),
        "review_db_items_to_reset": _db_reset_rows(review_db_ids_to_reset),
        "review_db_duplicate_deletes": _db_dup_rows(review_db_ids_duplicate_delete),
        "excluded_ids": sorted(exclude_ids),
        "symlink_root": symlink_root,
        "mount_path": mount_path,
        "stats": {
            "bad_symlinks": len(bad_symlinks),
            "review_symlinks": len(review_symlinks),
            "mount_files": len(mount_files_to_delete),
            "db_resets": len(db_ids_to_reset),
            "db_duplicate_deletes": len(db_ids_duplicate_delete),
            "review_db_resets": len(review_db_ids_to_reset),
            "review_db_duplicate_deletes": len(review_db_ids_duplicate_delete),
            "excluded_ids": len(exclude_ids),
            "library_disk_divergence": sum(
                1 for e in bad_symlinks + review_symlinks if e.get("size_divergence_note")
            ),
        },
    }


def apply_junk_symlink_plan(
    plan: Dict[str, Any],
    *,
    db_path: str,
    symlink_paths: Optional[Set[str]] = None,
    mount_paths: Optional[Set[str]] = None,
    reset_item_ids: Optional[Set[int]] = None,
    delete_item_ids: Optional[Set[int]] = None,
    use_rescrape: bool = False,
    confidence_tier: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply all or part of an audit plan. confidence_tier: 'high', 'review', or None (both)."""
    symlink_root = os.path.normpath(plan.get("symlink_root") or "")
    mount_path = os.path.normpath(plan.get("mount_path") or "")

    all_symlink_deletes: Set[str] = set()
    if confidence_tier in (None, "high"):
        all_symlink_deletes |= {e["symlink"] for e in plan.get("bad_symlinks", [])}
    if confidence_tier in (None, "review"):
        all_symlink_deletes |= {e["symlink"] for e in plan.get("review_symlinks", [])}
    if symlink_paths is not None:
        all_symlink_deletes = {p for p in all_symlink_deletes if p in symlink_paths}

    mount_files: Dict[str, str] = {}
    if confidence_tier in (None, "high"):
        mount_files = {e["path"]: e["reason"] for e in plan.get("mount_files_to_delete", [])}
    if mount_paths is not None:
        mount_files = {p: r for p, r in mount_files.items() if p in mount_paths}

    reset_ids: Set[int] = set()
    delete_ids: Set[int] = set()
    if confidence_tier in (None, "high"):
        reset_ids |= {e["id"] for e in plan.get("db_items_to_reset", [])}
        delete_ids |= {e["id"] for e in plan.get("db_duplicate_deletes", [])}
    if confidence_tier in (None, "review"):
        reset_ids |= {e["id"] for e in plan.get("review_db_items_to_reset", [])}
        delete_ids |= {e["id"] for e in plan.get("review_db_duplicate_deletes", [])}
    if reset_item_ids is not None:
        reset_ids &= reset_item_ids
    if delete_item_ids is not None:
        delete_ids &= delete_item_ids

    deleted_symlinks = deleted_mount = reset_count = delete_count = failed = 0
    errors: List[str] = []

    for link in sorted(all_symlink_deletes):
        if symlink_root and not link.startswith(symlink_root):
            errors.append(f"Refused symlink (outside tree): {link}")
            failed += 1
            continue
        try:
            os.remove(link)
            deleted_symlinks += 1
        except OSError as exc:
            errors.append(f"Symlink {link}: {exc}")
            failed += 1

    current_links, _ = index_symlinks(symlink_root) if symlink_root else ({}, [])
    linked_targets = set(current_links.keys())

    for path in sorted(mount_files):
        if mount_path and not path.startswith(mount_path):
            errors.append(f"Refused mount file (outside tree): {path}")
            failed += 1
            continue
        if path in linked_targets:
            continue
        try:
            os.remove(path)
            deleted_mount += 1
        except OSError as exc:
            errors.append(f"Mount file {path}: {exc}")
            failed += 1

    if use_rescrape:
        from routes.database_routes import rescrape_single_item
        from database.database_writing import remove_from_media_items
        for item_id in sorted(reset_ids):
            result = rescrape_single_item(item_id)
            if result.get("success"):
                reset_count += 1
            else:
                errors.append(f"Rescrape item {item_id}: {result.get('error')}")
                failed += 1
        for item_id in sorted(delete_ids):
            if remove_from_media_items(item_id):
                delete_count += 1
            else:
                errors.append(f"Delete duplicate item {item_id} failed")
                failed += 1
    else:
        conn = sqlite3.connect(db_path)
        try:
            reset_lookup = {
                e["id"]: e for e in (
                    plan.get("db_items_to_reset", [])
                    + plan.get("review_db_items_to_reset", [])
                )
            }
            for item_id in sorted(reset_ids):
                item = reset_lookup.get(item_id, {})
                reset_item_to_wanted(
                    conn,
                    item_id,
                    item.get("original_scraped_torrent_title"),
                    item.get("version"),
                )
                reset_count += 1
            for item_id in sorted(delete_ids):
                cur = conn.execute("DELETE FROM media_items WHERE id = ?", (item_id,))
                if cur.rowcount:
                    delete_count += 1
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            errors.append(f"DB error: {exc}")
            failed += 1
        finally:
            conn.close()

    return {
        "deleted_symlinks": deleted_symlinks,
        "deleted_mount": deleted_mount,
        "reset_count": reset_count,
        "delete_count": delete_count,
        "failed": failed,
        "errors": errors[:50],
    }
