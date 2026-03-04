#!/usr/bin/env python3
"""
Test script for Plex overlay system.

Usage:
    python test_overlay.py <media_item_id>
    python test_overlay.py --batch <id1,id2,id3>
    python test_overlay.py --list-pending
"""

import os
import sys
import argparse
import json
import logging
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from overlays.overlay_manager import OverlayManager


def setup_logging(verbose=False):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )


def get_config():
    """Get configuration from environment or defaults."""
    config = {
        'db_path': os.getenv('DATABASE_PATH', '/user/config/media_items.db'),
        'plex_url': os.getenv('PLEX_URL', 'http://localhost:32400'),
        'plex_token': os.getenv('PLEX_TOKEN', ''),
        'asset_dir': os.getenv('OVERLAY_ASSETS_DIR', '/user/config/overlay_assets')
    }

    # Validate required config
    if not config['plex_token']:
        print("ERROR: PLEX_TOKEN environment variable not set")
        sys.exit(1)

    return config


def test_single_item(manager, item_id, force=False):
    """Test overlay generation for a single item."""
    print(f"\n{'='*60}")
    print(f"Testing overlay generation for item ID: {item_id}")
    print(f"{'='*60}\n")

    result = manager.generate_overlay_for_item(item_id, force=force)

    print(f"\nResult:")
    print(f"  Status: {result['status']}")
    print(f"  Success: {result['success']}")
    print(f"  Message: {result['message']}")

    if result.get('details'):
        print(f"\nDetails:")
        for key, value in result['details'].items():
            if key == 'media_info' and isinstance(value, dict):
                print(f"  Media Info:")
                for k, v in value.items():
                    print(f"    {k}: {v}")
            else:
                print(f"  {key}: {value}")

    return result


def test_batch(manager, item_ids, force=False):
    """Test overlay generation for multiple items."""
    print(f"\n{'='*60}")
    print(f"Testing batch overlay generation for {len(item_ids)} items")
    print(f"{'='*60}\n")

    results = manager.batch_generate_overlays(item_ids, force=force)

    print(f"\nBatch Results:")
    print(f"  Total: {results['total']}")
    print(f"  Applied: {results['applied']}")
    print(f"  Analyzing: {results['analyzing']}")
    print(f"  Failed: {results['failed']}")
    print(f"  Skipped: {results['skipped']}")

    print(f"\nIndividual Results:")
    for item_result in results['items']:
        item_id = item_result['item_id']
        status = item_result['status']
        title = item_result.get('details', {}).get('title', 'Unknown')
        print(f"  [{status:10s}] ID {item_id:5d}: {title}")

    return results


def list_pending_items(config):
    """List items pending overlay generation."""
    import sqlite3

    print(f"\n{'='*60}")
    print(f"Items pending overlay generation")
    print(f"{'='*60}\n")

    try:
        conn = sqlite3.connect(config['db_path'])
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Find items with media server item ID but no overlay or failed overlay
        cursor.execute('''
            SELECT
                m.id,
                m.title,
                m.year,
                m.type,
                m.ms_item_id,
                o.status,
                o.reason,
                o.retry_count,
                o.updated_at
            FROM media_items m
            LEFT JOIN media_overlay_state o ON m.id = o.media_item_id
            WHERE m.ms_item_id IS NOT NULL
              AND (o.status IS NULL OR o.status IN ('pending', 'analyzing', 'failed'))
            ORDER BY m.id
            LIMIT 50
        ''')

        rows = cursor.fetchall()
        conn.close()

        if not rows:
            print("No pending items found.")
            return

        print(f"Found {len(rows)} pending items:\n")
        for row in rows:
            status = row['status'] or 'not_started'
            print(f"ID {row['id']:5d} | {status:12s} | {row['title']} ({row['year']})")
            if row['reason']:
                print(f"           | Reason: {row['reason']}")
            if row['retry_count'] and row['retry_count'] > 0:
                print(f"           | Retries: {row['retry_count']}")

    except Exception as e:
        print(f"ERROR: Failed to list pending items: {e}")


def main():
    """Main test function."""
    parser = argparse.ArgumentParser(description='Test Plex overlay generation')
    parser.add_argument('item_id', type=int, nargs='?', help='Media item ID to test')
    parser.add_argument('--batch', type=str, help='Comma-separated list of item IDs')
    parser.add_argument('--force', action='store_true', help='Force regeneration even if already applied')
    parser.add_argument('--list-pending', action='store_true', help='List pending items')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose logging')

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.verbose)

    # Get configuration
    config = get_config()

    # List pending items
    if args.list_pending:
        list_pending_items(config)
        return

    # Validate arguments
    if not args.item_id and not args.batch:
        parser.print_help()
        print("\nERROR: Either provide item_id or --batch")
        sys.exit(1)

    # Create overlay manager
    manager = OverlayManager(
        db_path=config['db_path'],
        plex_base_url=config['plex_url'],
        plex_token=config['plex_token'],
        asset_dir=config['asset_dir']
    )

    # Run test
    if args.batch:
        # Batch mode
        item_ids = [int(x.strip()) for x in args.batch.split(',')]
        result = test_batch(manager, item_ids, force=args.force)
    else:
        # Single item mode
        result = test_single_item(manager, args.item_id, force=args.force)

    # Exit with appropriate code
    if isinstance(result, dict) and result.get('success'):
        sys.exit(0)
    elif isinstance(result, dict) and result.get('status') in ('analyzing', 'skipped'):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()
