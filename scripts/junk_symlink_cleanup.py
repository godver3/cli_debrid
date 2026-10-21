#!/usr/bin/env python3
"""CLI wrapper for utilities.junk_symlink_audit — see that module for details."""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utilities.junk_symlink_audit import (  # noqa: E402
    apply_junk_symlink_plan,
    build_junk_symlink_plan,
    fmt_gb,
    fmt_mb,
    item_label,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", required=True, help="Path to media_items.db")
    parser.add_argument("--symlinks", required=True, help="Symlink library root")
    parser.add_argument("--mount", required=True, help="original_files_path / mount __all__ root")
    parser.add_argument("--min-episode-mb", type=int, default=200)
    parser.add_argument("--min-movie-mb", type=int, default=300)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--exclude-id", type=int, action="append", default=[])
    args = parser.parse_args()

    exclude_ids = set(args.exclude_id)
    for label, path in (("db", args.db), ("symlinks", args.symlinks), ("mount", args.mount)):
        if not os.path.exists(path):
            print(f"ERROR: {label} path not found: {path}", file=sys.stderr)
            return 1

    plan = build_junk_symlink_plan(
        db_path=args.db,
        symlink_root=args.symlinks,
        mount_path=args.mount,
        exclude_ids=exclude_ids,
        min_episode_mb=args.min_episode_mb,
        min_movie_mb=args.min_movie_mb,
    )
    plan["dry_run"] = not args.execute
    stats = plan["stats"]
    bad_symlinks = plan["bad_symlinks"]
    review_symlinks = plan.get("review_symlinks") or []
    divergent = [e for e in bad_symlinks + review_symlinks if e.get("size_divergence_note")]

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
        print(f"Wrote plan to {args.json_out}")

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"\n=== Junk cleanup ({mode}) ===\n")
    print(f"Likely junk:       {stats['bad_symlinks']}")
    print(f"Review (older):    {stats.get('review_symlinks', 0)}")
    print(f"Mount junk files:  {stats['mount_files']}")
    print(f"DB rows -> Wanted: {stats['db_resets']}")
    print(f"DB duplicate del:  {stats['db_duplicate_deletes']}")
    if exclude_ids:
        print(f"Excluded (skipped): {len(exclude_ids)} item id(s)")
    print(f"Library vs disk:   {stats['library_disk_divergence']}")
    print()

    if plan["db_duplicate_deletes"]:
        print("--- Duplicate junk rows (delete row, keep good sibling) ---")
        for item in plan["db_duplicate_deletes"][:30]:
            sib_ids = item.get("good_sibling_ids") or []
            sib_lbl = (item.get("good_sibling_labels") or [""])[0]
            print(
                f"  id={item['id']} {item['label']}\n"
                f"    keep sibling id={sib_ids[0]} ({sib_lbl})\n"
                f"    junk was: {item.get('filled_by_title')}"
            )
        print()

    if not args.execute:
        print("\nNo changes made. Re-run with --execute to apply.")
        return 0

    result = apply_junk_symlink_plan(plan, db_path=args.db, use_rescrape=False)
    print(
        f"\nDone. Symlinks removed: {result['deleted_symlinks']}, "
        f"mount files removed: {result['deleted_mount']}, "
        f"DB rows reset to Wanted: {result['reset_count']}, "
        f"duplicate rows deleted: {result['delete_count']}, "
        f"failures: {result['failed']}"
    )
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
