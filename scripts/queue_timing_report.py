#!/usr/bin/env python3
import argparse
import json
import math
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Tuple


DEFAULT_FILE_PATH = "/user/db_content/queue_timing_data.json"


def load_data(file_path: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        sys.exit(1)

    try:
        with open(file_path, "r") as f:
            data = json.load(f)
        queue_times = data.get("queue_times", {}) or {}
        queue_stats = data.get("queue_stats", {}) or {}
        return queue_times, queue_stats
    except json.JSONDecodeError as e:
        print(f"Failed to parse JSON: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error reading file: {e}")
        sys.exit(1)


def format_duration(seconds: float) -> str:
    try:
        seconds = float(seconds)
    except Exception:
        return "-"

    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f}m"
    hours = seconds / 3600
    if hours < 24:
        return f"{hours:.1f}h"
    days = hours / 24
    return f"{days:.1f}d"


def safe_seconds(value: Any) -> float:
    try:
        v = float(value)
        return v if math.isfinite(v) else float("nan")
    except Exception:
        return float("nan")


def print_stats(queue_stats: Dict[str, Any]) -> None:
    print("Queue Timing Statistics:\n")
    any_rows = False
    for queue_name, stats in queue_stats.items():
        count = int(stats.get("count", 0) or 0)
        total_time = safe_seconds(stats.get("total_time", 0))
        min_time = safe_seconds(stats.get("min_time", float("nan")))
        max_time = safe_seconds(stats.get("max_time", float("nan")))

        if count <= 0:
            continue
        any_rows = True

        avg_time = total_time / count if count > 0 and math.isfinite(total_time) else float("nan")

        print(f"{queue_name}:")
        print(f"  Items processed: {count}")
        print(f"  Average time:   {format_duration(avg_time) if math.isfinite(avg_time) else '-'}")
        print(f"  Min time:       {format_duration(min_time) if math.isfinite(min_time) else '-'}")
        print(f"  Max time:       {format_duration(max_time) if math.isfinite(max_time) else '-'}\n")

    if not any_rows:
        print("No completed timing stats available (yet).\n")


def build_current_items(queue_times: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    now = datetime.now().timestamp()
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item_id, per_queue in queue_times.items():
        if not isinstance(per_queue, dict):
            continue
        for queue_name, times in per_queue.items():
            if not isinstance(times, (list, tuple)) or len(times) != 2:
                continue
            entry_time, exit_time = times
            if entry_time is None or exit_time is not None:
                continue
            try:
                entry_time_float = float(entry_time)
            except Exception:
                continue
            duration = max(0.0, now - entry_time_float)
            group = grouped.setdefault(queue_name, [])
            group.append(
                {
                    "item_id": item_id,
                    "seconds": duration,
                    "entry": datetime.fromtimestamp(entry_time_float).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
    # Sort each queue's list by longest time first
    for items in grouped.values():
        items.sort(key=lambda r: r["seconds"], reverse=True)
    return grouped


def print_current_items(grouped: Dict[str, List[Dict[str, Any]]], top: int = None) -> None:
    print("Current Items In Queues (no recorded exit):\n")
    if not grouped:
        print("All queues are empty (or no in-progress timings recorded).\n")
        return

    for queue_name, items in grouped.items():
        if not items:
            continue
        print(f"{queue_name}:")
        display = items[:top] if top else items
        for row in display:
            print(
                f"  {row['item_id']}: in queue {format_duration(row['seconds'])} (since {row['entry']})"
            )
        if top and len(items) > top:
            print(f"  ... and {len(items) - top} more")
        print()


def print_item_detail(queue_times: Dict[str, Any], item_id: str) -> None:
    data = queue_times.get(item_id)
    if not data:
        print(f"Item not found: {item_id}")
        return
    print(f"Item {item_id} timeline:\n")
    for queue_name, times in data.items():
        if not isinstance(times, (list, tuple)) or len(times) != 2:
            continue
        entry_time, exit_time = times
        entry_str = (
            datetime.fromtimestamp(entry_time).strftime("%Y-%m-%d %H:%M:%S")
            if entry_time is not None
            else "-"
        )
        if exit_time is None and entry_time is not None:
            dur = datetime.now().timestamp() - float(entry_time)
            print(
                f"{queue_name}: entered {entry_str}, still in queue, elapsed {format_duration(dur)}"
            )
        else:
            exit_str = (
                datetime.fromtimestamp(exit_time).strftime("%Y-%m-%d %H:%M:%S")
                if exit_time is not None
                else "-"
            )
            dur = (
                float(exit_time) - float(entry_time)
                if exit_time is not None and entry_time is not None
                else float("nan")
            )
            print(
                f"{queue_name}: {entry_str} -> {exit_str} (duration {format_duration(dur) if math.isfinite(dur) else '-'})"
            )
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Display queue timing information from queue_timing_data.json",
    )
    parser.add_argument(
        "-f",
        "--file",
        default=DEFAULT_FILE_PATH,
        help=f"Path to queue timing JSON (default: {DEFAULT_FILE_PATH})",
    )
    parser.add_argument(
        "--current",
        action="store_true",
        help="Show only current items with time in queue",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show only aggregated per-queue timing statistics",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Limit number of current items shown per queue (longest first)",
    )
    parser.add_argument(
        "--item",
        type=str,
        default=None,
        help="Show detailed timeline for a specific item id",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    queue_times, queue_stats = load_data(args.file)

    if args.item:
        print_item_detail(queue_times, args.item)
        return

    show_current = args.current or not (args.current or args.stats)
    show_stats = args.stats or not (args.current or args.stats)

    if show_stats:
        print_stats(queue_stats)

    if show_current:
        grouped = build_current_items(queue_times)
        print_current_items(grouped, top=args.top)


if __name__ == "__main__":
    main()


