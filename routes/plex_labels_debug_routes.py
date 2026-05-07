"""
API routes for Plex Labels debug utilities
"""

import logging
from flask import Blueprint, request, jsonify, Response, stream_with_context
from utilities.plex_label_manager import (
    get_labels_for_item,
    add_label_to_item,
    remove_label_from_item,
    apply_labels_for_item,
    get_label_config_for_source,
    determine_labels_for_item,
    parse_plex_labels,
    sanitize_label
)
from utilities.settings import get_all_settings
import sqlite3
import os
import json
import time

# Initialize blueprint
plex_labels_debug_bp = Blueprint('plex_labels_debug', __name__)

# Database path
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')
DB_PATH = os.path.join(DB_CONTENT_DIR, 'media_items.db')

# Built-in labels that are always active (created by core app features)
# These should NEVER be considered orphaned regardless of content source state
BUILTIN_LABELS = {'cd-library', 'cd-discover'}


@plex_labels_debug_bp.route('/debug/plex-labels/search')
def search_by_label():
    """Search for items by label"""
    try:
        label = request.args.get('label', '').strip()
        if not label:
            return jsonify({'success': False, 'message': 'Label parameter is required'}), 400

        # Sanitize the label for search
        sanitized_label = sanitize_label(label)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Search for items with this label using SQLite JSON functions
        # OPTIMIZED: Uses database-level filtering instead of Python loop
        # Previous implementation fetched all items and parsed JSON in Python (slow)
        # This uses json_each() to filter at database level (10-100x faster)
        cursor.execute('''
            SELECT DISTINCT m.id, m.title, m.type
            FROM media_items m, json_each(m.plex_labels) j
            WHERE j.key = ?
        ''', (sanitized_label,))

        matching_items = [dict(row) for row in cursor.fetchall()]

        cursor.close()
        conn.close()

        return jsonify({
            'success': True,
            'items': matching_items,
            'count': len(matching_items)
        })

    except Exception as e:
        logging.error(f"Error searching by label: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/bulk-apply-preview')
def bulk_apply_preview():
    """Preview bulk label application"""
    try:
        from utilities.settings import get_all_settings

        source_id = request.args.get('source_id', '').strip()
        if not source_id:
            return jsonify({'success': False, 'message': 'source_id parameter is required'}), 400

        # Get source display name for user-friendly output
        all_settings = get_all_settings()
        content_sources = all_settings.get('Content Sources', {})
        source_config = content_sources.get(source_id, {})
        display_name = source_config.get('display_name', source_id)

        # Get label config for this source
        label_config = get_label_config_for_source(source_id)
        if not label_config:
            return jsonify({'success': False, 'message': 'No label configuration found for this source'}), 400

        # Count items from this source in Collected state —
        # primary source OR listed in content_sources JSON
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT COUNT(DISTINCT id) as count
            FROM media_items
            WHERE state = 'Collected'
              AND (ghostlisted IS NULL OR ghostlisted != 1)
              AND (
                content_source = ?
                OR EXISTS (
                    SELECT 1 FROM json_each(content_sources) e
                    WHERE json_extract(e.value, '$.source') = ?
                )
              )
        ''', (source_id, source_id))

        count = cursor.fetchone()[0]
        cursor.close()
        conn.close()

        # Determine sample labels based on label mode
        sample_labels = []
        label_mode = label_config.get('label_mode', 'requester')

        if label_mode == 'fixed':
            fixed_label = label_config.get('fixed_label', '')
            if fixed_label:
                # Split by comma and sanitize each label individually
                for label in fixed_label.split(','):
                    label = label.strip()
                    if label:
                        sample_labels.append(sanitize_label(label))
        elif label_mode == 'list_name':
            # Show the display name that will be used as the label
            if display_name:
                sample_labels.append(sanitize_label(display_name))
        else:
            sample_labels.append(f"Mode: {label_mode}")

        return jsonify({
            'success': True,
            'count': count,
            'labels': sample_labels,
            'source_id': source_id,
            'source_name': display_name  # For display in UI
        })

    except Exception as e:
        logging.error(f"Error previewing bulk apply: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/bulk-apply', methods=['POST'])
def bulk_apply():
    """Execute bulk label application"""
    conn = None
    cursor = None
    try:
        # Get JSON data with explicit error handling
        data = request.get_json()
        if data is None:
            logging.error("bulk_apply: request.get_json() returned None - invalid JSON or content-type")
            return jsonify({'success': False, 'message': 'Invalid JSON data'}), 400

        source_id = data.get('source_id', '').strip()

        if not source_id:
            logging.error("bulk_apply: source_id parameter is missing or empty")
            return jsonify({'success': False, 'message': 'source_id is required'}), 400

        logging.info(f"bulk_apply: Starting bulk label application for source_id: {source_id}")

        # Get items from this source in Collected state —
        # primary source OR listed in content_sources JSON
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT DISTINCT id, title, type, content_source, content_source_detail, content_sources
            FROM media_items
            WHERE state = 'Collected'
              AND (
                content_source = ?
                OR EXISTS (
                    SELECT 1 FROM json_each(content_sources) e
                    WHERE json_extract(e.value, '$.source') = ?
                )
              )
        ''', (source_id, source_id))

        items = cursor.fetchall()
        cursor.close()
        conn.close()
        cursor = None
        conn = None

        logging.info(f"bulk_apply: Found {len(items)} items to process")

        applied_count = 0
        failed_items = []
        for row in items:
            item = dict(row)
            # For items where source_id is secondary, temporarily treat it as primary
            # so apply_labels_for_item applies that source's label
            if item.get('content_source') != source_id:
                item = dict(item)
                item['content_source'] = source_id
                item['content_source_detail'] = None
            try:
                labels_applied = apply_labels_for_item(item)
                if labels_applied > 0:
                    applied_count += 1
            except Exception as e:
                logging.error(f"Error applying labels to item {item['id']}: {e}", exc_info=True)
                failed_items.append({'id': item['id'], 'title': item.get('title'), 'error': str(e)})

        result = {
            'success': True,
            'count': applied_count,
            'total': len(items)
        }

        if failed_items:
            result['failed_items'] = failed_items
            result['failed_count'] = len(failed_items)

        logging.info(f"bulk_apply: Completed - {applied_count}/{len(items)} items processed successfully")
        return jsonify(result)

    except sqlite3.Error as e:
        logging.error(f"Database error in bulk_apply: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logging.error(f"Error executing bulk apply: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Internal error: {str(e)}'}), 500
    finally:
        # Ensure database resources are cleaned up
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@plex_labels_debug_bp.route('/debug/plex-labels/bulk-apply-stream', methods=['POST'])
def bulk_apply_stream():
    """Execute bulk label application with streaming progress updates"""
    def generate():
        conn = None
        cursor = None
        try:
            # Get JSON data
            data = request.get_json()
            if data is None:
                yield f"data: {json.dumps({'error': 'Invalid JSON data'})}\n\n"
                return

            source_id = data.get('source_id', '').strip()
            if not source_id:
                yield f"data: {json.dumps({'error': 'source_id is required'})}\n\n"
                return

            # Get items from this source — primary source OR listed in content_sources JSON
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute('''
                SELECT DISTINCT id, title, type, content_source, content_source_detail, content_sources
                FROM media_items
                WHERE state = 'Collected'
                  AND (
                    content_source = ?
                    OR EXISTS (
                        SELECT 1 FROM json_each(content_sources) e
                        WHERE json_extract(e.value, '$.source') = ?
                    )
                  )
            ''', (source_id, source_id))

            items = cursor.fetchall()
            cursor.close()
            conn.close()
            cursor = None
            conn = None

            total = len(items)

            # Send initial status
            yield f"data: {json.dumps({'status': 'started', 'total': total})}\n\n"

            applied_count = 0
            failed_items = []
            start_time = time.time()

            for index, row in enumerate(items, 1):
                item = dict(row)
                # For items where source_id is secondary, treat it as primary for this apply
                if item.get('content_source') != source_id:
                    item = dict(item)
                    item['content_source'] = source_id
                    item['content_source_detail'] = None

                try:
                    labels_applied = apply_labels_for_item(item)
                    if labels_applied > 0:
                        applied_count += 1

                    # Calculate progress
                    elapsed = time.time() - start_time
                    avg_time_per_item = elapsed / index
                    remaining_items = total - index
                    estimated_remaining = avg_time_per_item * remaining_items

                    # Send progress update
                    progress_data = {
                        'status': 'progress',
                        'current': index,
                        'total': total,
                        'applied': applied_count,
                        'item_title': item.get('title', 'Unknown'),
                        'labels_applied': labels_applied,
                        'elapsed_seconds': int(elapsed),
                        'estimated_remaining_seconds': int(estimated_remaining)
                    }
                    yield f"data: {json.dumps(progress_data)}\n\n"

                except Exception as e:
                    logging.error(f"Error applying labels to item {item['id']}: {e}", exc_info=True)
                    failed_items.append({'id': item['id'], 'title': item.get('title'), 'error': str(e)})

                    # Send error update
                    error_data = {
                        'status': 'item_error',
                        'current': index,
                        'total': total,
                        'item_id': item['id'],
                        'item_title': item.get('title'),
                        'error': str(e)
                    }
                    yield f"data: {json.dumps(error_data)}\n\n"

            # Send completion status
            result = {
                'status': 'complete',
                'success': True,
                'applied': applied_count,
                'total': total,
                'elapsed_seconds': int(time.time() - start_time)
            }

            if failed_items:
                result['failed_items'] = failed_items
                result['failed_count'] = len(failed_items)

            yield f"data: {json.dumps(result)}\n\n"

        except Exception as e:
            logging.error(f"Error in bulk_apply_stream: {e}", exc_info=True)
            yield f"data: {json.dumps({'status': 'error', 'error': str(e)})}\n\n"
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@plex_labels_debug_bp.route('/debug/plex-labels/bulk-remove-stream', methods=['POST'])
def bulk_remove_stream():
    """Execute bulk label removal with streaming progress"""
    def generate():
        conn = None
        cursor = None
        try:
            data = request.get_json()
            if data is None:
                yield f"data: {json.dumps({'error': 'Invalid JSON data'})}\n\n"
                return

            label = data.get('label', '').strip()
            if not label:
                yield f"data: {json.dumps({'error': 'label is required'})}\n\n"
                return

            sanitized_label = sanitize_label(label)

            # Get items with this label using DB-level json_each filtering
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute('''
                SELECT DISTINCT m.id, m.title, m.plex_labels
                FROM media_items m, json_each(m.plex_labels) je
                WHERE m.plex_labels IS NOT NULL AND je.key = ?
            ''', (sanitized_label,))

            items_to_remove = [dict(row) for row in cursor.fetchall()]

            cursor.close()
            conn.close()
            cursor = None
            conn = None

            total = len(items_to_remove)
            yield f"data: {json.dumps({'status': 'started', 'total': total, 'label': sanitized_label})}\n\n"

            removed_count = 0
            failed_items = []
            start_time = time.time()

            for index, item in enumerate(items_to_remove, 1):
                try:
                    # Remove from all sources
                    labels_dict = get_labels_for_item(item['id'])
                    if sanitized_label in labels_dict:
                        sources = labels_dict[sanitized_label]
                        for source in sources:
                            remove_label_from_item(item['id'], sanitized_label, source, remove_from_plex=True)
                        removed_count += 1

                    # Calculate progress
                    elapsed = time.time() - start_time
                    avg_time_per_item = elapsed / index
                    remaining_items = total - index
                    estimated_remaining = avg_time_per_item * remaining_items

                    progress_data = {
                        'status': 'progress',
                        'current': index,
                        'total': total,
                        'removed': removed_count,
                        'item_title': item.get('title', 'Unknown'),
                        'elapsed_seconds': int(elapsed),
                        'estimated_remaining_seconds': int(estimated_remaining)
                    }
                    yield f"data: {json.dumps(progress_data)}\n\n"

                except Exception as e:
                    logging.error(f"Error removing label from item {item['id']}: {e}", exc_info=True)
                    failed_items.append({'id': item['id'], 'title': item.get('title'), 'error': str(e)})

            result = {
                'status': 'complete',
                'success': True,
                'removed': removed_count,
                'total': total,
                'elapsed_seconds': int(time.time() - start_time)
            }

            if failed_items:
                result['failed_items'] = failed_items
                result['failed_count'] = len(failed_items)

            yield f"data: {json.dumps(result)}\n\n"

        except Exception as e:
            logging.error(f"Error in bulk_remove_stream: {e}", exc_info=True)
            yield f"data: {json.dumps({'status': 'error', 'error': str(e)})}\n\n"
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@plex_labels_debug_bp.route('/debug/plex-labels/sync-all-stream', methods=['POST'])
def sync_all_stream():
    """Sync all labels from content sources with streaming progress"""
    def generate():
        conn = None
        cursor = None
        try:
            from utilities.plex_label_manager import sync_labels_to_plex_for_item

            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Get movies directly, and one representative episode per show (using MIN(id))
            cursor.execute('''
                SELECT id, title, type, content_source
                FROM media_items
                WHERE state = 'Collected'
                AND content_source IS NOT NULL
                AND type = 'movie'

                UNION

                SELECT MIN(id) as id, title, type, content_source
                FROM media_items
                WHERE state = 'Collected'
                AND content_source IS NOT NULL
                AND type = 'episode'
                GROUP BY title, tmdb_id
            ''')

            items = cursor.fetchall()
            cursor.close()
            conn.close()
            cursor = None
            conn = None

            total = len(items)
            yield f"data: {json.dumps({'status': 'started', 'total': total})}\n\n"

            synced_count = 0
            failed_items = []
            start_time = time.time()

            for index, row in enumerate(items, 1):
                item = dict(row)

                try:
                    labels_synced = sync_labels_to_plex_for_item(item['id'])
                    if labels_synced > 0:
                        synced_count += 1

                    elapsed = time.time() - start_time
                    avg_time_per_item = elapsed / index
                    remaining_items = total - index
                    estimated_remaining = avg_time_per_item * remaining_items

                    progress_data = {
                        'status': 'progress',
                        'current': index,
                        'total': total,
                        'synced': synced_count,
                        'item_title': item.get('title', 'Unknown'),
                        'labels_synced': labels_synced,
                        'elapsed_seconds': int(elapsed),
                        'estimated_remaining_seconds': int(estimated_remaining)
                    }
                    yield f"data: {json.dumps(progress_data)}\n\n"

                except Exception as e:
                    logging.error(f"Error syncing labels for item {item['id']}: {e}", exc_info=True)
                    failed_items.append({'id': item['id'], 'title': item.get('title'), 'error': str(e)})

            result = {
                'status': 'complete',
                'success': True,
                'synced': synced_count,
                'total': total,
                'elapsed_seconds': int(time.time() - start_time)
            }

            if failed_items:
                result['failed_items'] = failed_items
                result['failed_count'] = len(failed_items)

            yield f"data: {json.dumps(result)}\n\n"

        except Exception as e:
            logging.error(f"Error in sync_all_stream: {e}", exc_info=True)
            yield f"data: {json.dumps({'status': 'error', 'error': str(e)})}\n\n"
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@plex_labels_debug_bp.route('/debug/plex-labels/cleanup-orphaned-stream', methods=['POST'])
def cleanup_orphaned_stream():
    """Cleanup orphaned labels and items from unconfigured sources with streaming progress"""
    def generate():
        conn = None
        cursor = None
        try:
            # Get active and configured sources
            all_settings = get_all_settings()
            content_sources = all_settings.get('Content Sources', {})

            # Build active sources set (lowercase for case-insensitive matching)
            active_sources = set()
            all_configured_sources = set()
            for source_id, config in content_sources.items():
                all_configured_sources.add(source_id.lower())
                if config.get('plex_labels', {}).get('enabled', False):
                    active_sources.add(source_id.lower())

            # PHASE 1: Clean orphaned labels
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute('SELECT id, title, plex_labels FROM media_items WHERE plex_labels IS NOT NULL')
            items = cursor.fetchall()

            cursor.close()
            conn.close()
            cursor = None
            conn = None

            total_phase1 = len(items)
            yield f"data: {json.dumps({'status': 'started', 'phase': 1, 'total': total_phase1, 'message': 'Cleaning orphaned labels'})}\n\n"

            labels_cleaned_count = 0
            start_time = time.time()

            for index, row in enumerate(items, 1):
                item = dict(row)
                labels_dict = parse_plex_labels(item['plex_labels'])
                removed_any = False

                for label, info in list(labels_dict.items()):
                    # Skip built-in application labels (always active)
                    if label in BUILTIN_LABELS:
                        continue

                    # Check for orphaned sources (case-insensitive)
                    source_list = info.get('sources', [])
                    orphaned_sources = [s for s in source_list if s.lower() not in active_sources]
                    if orphaned_sources:
                        for source in orphaned_sources:
                            try:
                                remove_label_from_item(item['id'], label, source, remove_from_plex=True)
                                removed_any = True
                            except Exception as e:
                                logging.error(f"Error removing orphaned label '{label}' (source: {source}) from item {item['id']}: {e}")

                if removed_any:
                    labels_cleaned_count += 1

                elapsed = time.time() - start_time
                avg_time_per_item = elapsed / index if index > 0 else 0
                remaining_items = total_phase1 - index
                estimated_remaining = avg_time_per_item * remaining_items

                progress_data = {
                    'status': 'progress',
                    'phase': 1,
                    'current': index,
                    'total': total_phase1,
                    'labels_cleaned': labels_cleaned_count,
                    'item_title': item.get('title', 'Unknown'),
                    'elapsed_seconds': int(elapsed),
                    'estimated_remaining_seconds': int(estimated_remaining)
                }
                yield f"data: {json.dumps(progress_data)}\n\n"

            # PHASE 2: Clear orphaned source metadata from items with unconfigured sources
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, title, content_source, content_source_detail
                FROM media_items
                WHERE content_source IS NOT NULL
                AND content_source NOT IN ('content_requestor', 'content_requester', 'Collected_1')
            ''')

            items_with_sources = cursor.fetchall()

            # Collect items that need cleaning
            items_to_clean = []
            for row in items_with_sources:
                content_source = row['content_source']
                if content_source.lower() not in all_configured_sources:
                    items_to_clean.append(dict(row))

            total_phase2 = len(items_to_clean)
            yield f"data: {json.dumps({'status': 'started', 'phase': 2, 'total': total_phase2, 'message': 'Clearing orphaned source metadata'})}\n\n"

            sources_cleaned_count = 0
            phase2_start = time.time()

            for index, item in enumerate(items_to_clean, 1):
                item_id = item['id']
                content_source = item['content_source']
                title = item['title']

                try:
                    # Clear the orphaned source metadata
                    cursor.execute('''
                        UPDATE media_items
                        SET content_source = NULL, content_source_detail = NULL
                        WHERE id = ?
                    ''', (item_id,))
                    conn.commit()
                    sources_cleaned_count += 1
                    logging.info(f"Cleared orphaned source metadata from item {item_id} ('{title}'): {content_source}")
                except Exception as e:
                    logging.error(f"Error clearing source metadata for item {item_id} from unconfigured source {content_source}: {e}")

                elapsed = time.time() - phase2_start
                avg_time_per_item = elapsed / index if index > 0 else 0
                remaining_items = total_phase2 - index
                estimated_remaining = avg_time_per_item * remaining_items

                progress_data = {
                    'status': 'progress',
                    'phase': 2,
                    'current': index,
                    'total': total_phase2,
                    'sources_cleaned': sources_cleaned_count,
                    'item_title': title,
                    'source': content_source,
                    'elapsed_seconds': int(elapsed),
                    'estimated_remaining_seconds': int(estimated_remaining)
                }
                yield f"data: {json.dumps(progress_data)}\n\n"

            cursor.close()
            conn.close()
            cursor = None
            conn = None

            result = {
                'status': 'complete',
                'success': True,
                'labels_cleaned': labels_cleaned_count,
                'sources_cleaned': sources_cleaned_count,
                'total_phase1': total_phase1,
                'total_phase2': total_phase2,
                'elapsed_seconds': int(time.time() - start_time)
            }

            yield f"data: {json.dumps(result)}\n\n"

        except Exception as e:
            logging.error(f"Error in cleanup_orphaned_stream: {e}", exc_info=True)
            yield f"data: {json.dumps({'status': 'error', 'error': str(e)})}\n\n"
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@plex_labels_debug_bp.route('/debug/plex-labels/bulk-remove-preview')
def bulk_remove_preview():
    """Preview bulk label removal"""
    try:
        label = request.args.get('label', '').strip()
        if not label:
            return jsonify({'success': False, 'message': 'label parameter is required'}), 400

        sanitized_label = sanitize_label(label)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Count items with this label using DB-level json_each filtering
        cursor.execute('''
            SELECT COUNT(DISTINCT m.id)
            FROM media_items m, json_each(m.plex_labels) je
            WHERE m.plex_labels IS NOT NULL AND je.key = ?
        ''', (sanitized_label,))

        count = cursor.fetchone()[0]

        cursor.close()
        conn.close()

        return jsonify({
            'success': True,
            'count': count,
            'label': sanitized_label
        })

    except Exception as e:
        logging.error(f"Error previewing bulk remove: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/bulk-remove', methods=['POST'])
def bulk_remove():
    """Execute bulk label removal"""
    conn = None
    cursor = None
    try:
        # Get JSON data with explicit error handling
        data = request.get_json()
        if data is None:
            logging.error("bulk_remove: request.get_json() returned None - invalid JSON or content-type")
            return jsonify({'success': False, 'message': 'Invalid JSON data'}), 400

        label = data.get('label', '').strip()

        if not label:
            logging.error("bulk_remove: label parameter is missing or empty")
            return jsonify({'success': False, 'message': 'label is required'}), 400

        sanitized_label = sanitize_label(label)
        logging.info(f"bulk_remove: Starting bulk removal of label: {sanitized_label}")

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Get items with this label using DB-level json_each filtering
        cursor.execute('''
            SELECT DISTINCT m.id
            FROM media_items m, json_each(m.plex_labels) je
            WHERE m.plex_labels IS NOT NULL AND je.key = ?
        ''', (sanitized_label,))

        items_to_remove = [row['id'] for row in cursor.fetchall()]

        cursor.close()
        conn.close()
        cursor = None
        conn = None

        logging.info(f"bulk_remove: Found {len(items_to_remove)} items with label '{sanitized_label}'")

        # Remove label from each item
        removed_count = 0
        failed_items = []
        for item_id in items_to_remove:
            try:
                # Remove from all sources (force removal)
                labels_dict = get_labels_for_item(item_id)
                if sanitized_label in labels_dict:
                    sources = labels_dict[sanitized_label]
                    for source in sources:
                        remove_label_from_item(item_id, sanitized_label, source, remove_from_plex=True)
                    removed_count += 1
            except Exception as e:
                logging.error(f"Error removing label from item {item_id}: {e}", exc_info=True)
                failed_items.append({'id': item_id, 'error': str(e)})

        result = {
            'success': True,
            'count': removed_count
        }

        if failed_items:
            result['failed_items'] = failed_items
            result['failed_count'] = len(failed_items)

        logging.info(f"bulk_remove: Completed - removed label from {removed_count}/{len(items_to_remove)} items")
        return jsonify(result)

    except sqlite3.Error as e:
        logging.error(f"Database error in bulk_remove: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logging.error(f"Error executing bulk remove: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Internal error: {str(e)}'}), 500
    finally:
        # Ensure database resources are cleaned up
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@plex_labels_debug_bp.route('/debug/plex-labels/find-orphaned')
def find_orphaned():
    """Find orphaned labels and items from unconfigured content sources"""
    try:
        # Get all configured content sources (both with and without labels enabled for comprehensive check)
        all_settings = get_all_settings()
        content_sources = all_settings.get('Content Sources', {})

        # Build active sources set (lowercase for case-insensitive matching)
        active_sources = set()
        for source_id, config in content_sources.items():
            if config.get('plex_labels', {}).get('enabled', False):
                active_sources.add(source_id.lower())

        # Also track ALL configured sources (even if labels disabled) for content_source check
        all_configured_sources = set(source_id.lower() for source_id in content_sources.keys())

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # TYPE 1: Find orphaned labels (items with labels from inactive sources)
        cursor.execute('''
            SELECT id, plex_labels
            FROM media_items
            WHERE plex_labels IS NOT NULL
        ''')

        orphaned_labels = {}
        for row in cursor.fetchall():
            plex_labels = parse_plex_labels(row['plex_labels'])
            for label, info in plex_labels.items():
                # Skip built-in application labels (always active)
                if label in BUILTIN_LABELS:
                    continue

                # Check if any sources for this label are still active (case-insensitive)
                source_list = info.get('sources', [])
                has_active_source = any(source.lower() in active_sources for source in source_list)
                if not has_active_source:
                    if label not in orphaned_labels:
                        orphaned_labels[label] = 0
                    orphaned_labels[label] += 1

        # TYPE 2: Find items with content_source pointing to unconfigured sources
        cursor.execute('''
            SELECT content_source, COUNT(*) as count
            FROM media_items
            WHERE content_source IS NOT NULL
            AND content_source NOT IN ('content_requestor', 'content_requester', 'Collected_1')
            GROUP BY content_source
        ''')

        orphaned_sources = {}
        for row in cursor.fetchall():
            source = row['content_source']
            count = row['count']
            # Check if source is in config (case-insensitive)
            if source.lower() not in all_configured_sources:
                orphaned_sources[source] = count

        cursor.close()
        conn.close()

        # Format results
        orphaned_labels_list = [
            {'label': label, 'item_count': count, 'type': 'label'}
            for label, count in orphaned_labels.items()
        ]

        orphaned_sources_list = [
            {'source': source, 'item_count': count, 'type': 'content_source'}
            for source, count in orphaned_sources.items()
        ]

        # Combine both types
        total_orphaned_labels = sum(orphaned_labels.values())
        total_orphaned_sources = sum(orphaned_sources.values())

        return jsonify({
            'success': True,
            'orphaned_labels': orphaned_labels_list,
            'orphaned_sources': orphaned_sources_list,
            'labels_count': len(orphaned_labels_list),
            'sources_count': len(orphaned_sources_list),
            'total_items_with_orphaned_labels': total_orphaned_labels,
            'total_items_from_orphaned_sources': total_orphaned_sources,
            'count': len(orphaned_labels_list) + len(orphaned_sources_list)  # Total unique orphans
        })

    except Exception as e:
        logging.error(f"Error finding orphaned labels: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/cleanup-orphaned', methods=['POST'])
def cleanup_orphaned():
    """Remove all orphaned labels and items from unconfigured sources"""
    conn = None
    cursor = None
    try:
        logging.info("cleanup_orphaned: Starting cleanup of orphaned labels and items")

        # Get active sources first
        all_settings = get_all_settings()
        content_sources = all_settings.get('Content Sources', {})

        # Build active sources set (lowercase for case-insensitive matching)
        active_sources = set()
        all_configured_sources = set()
        for source_id, config in content_sources.items():
            all_configured_sources.add(source_id.lower())
            if config.get('plex_labels', {}).get('enabled', False):
                active_sources.add(source_id.lower())

        logging.info(f"cleanup_orphaned: Found {len(active_sources)} active content sources, {len(all_configured_sources)} total configured")

        # TYPE 1: Find and remove orphaned labels (labels from inactive sources)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, plex_labels
            FROM media_items
            WHERE plex_labels IS NOT NULL
        ''')

        all_items = cursor.fetchall()
        cursor.close()
        conn.close()
        cursor = None
        conn = None

        logging.info(f"cleanup_orphaned: Processing {len(all_items)} items for orphaned labels")

        labels_cleaned_count = 0
        failed_label_removals = []
        for row in all_items:
            item_id = row['id']
            plex_labels = parse_plex_labels(row['plex_labels'])

            for label, info in plex_labels.items():
                # Skip built-in application labels (always active)
                if label in BUILTIN_LABELS:
                    continue

                # Remove sources that are no longer active (case-insensitive)
                source_list = info.get('sources', [])
                orphaned_sources = [s for s in source_list if s.lower() not in active_sources]
                for source in orphaned_sources:
                    try:
                        remove_label_from_item(item_id, label, source, remove_from_plex=True)
                        labels_cleaned_count += 1
                    except Exception as e:
                        logging.error(f"Error removing orphaned label {label} from item {item_id}: {e}", exc_info=True)
                        failed_label_removals.append({'item_id': item_id, 'label': label, 'source': source, 'error': str(e)})

        # TYPE 2: Clear orphaned source metadata from items with unconfigured sources
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, title, content_source, content_source_detail
            FROM media_items
            WHERE content_source IS NOT NULL
            AND content_source NOT IN ('content_requestor', 'content_requester', 'Collected_1')
        ''')

        items_with_sources = cursor.fetchall()

        logging.info(f"cleanup_orphaned: Checking {len(items_with_sources)} items for unconfigured sources")

        sources_cleaned_count = 0
        failed_source_cleanups = []

        # Collect items to update
        items_to_clean = []
        for row in items_with_sources:
            content_source = row['content_source']
            if content_source.lower() not in all_configured_sources:
                items_to_clean.append((row['id'], row['title'], content_source))

        # Update all in one transaction
        if items_to_clean:
            try:
                for item_id, title, source in items_to_clean:
                    cursor.execute('''
                        UPDATE media_items
                        SET content_source = NULL, content_source_detail = NULL
                        WHERE id = ?
                    ''', (item_id,))
                    sources_cleaned_count += 1
                    logging.info(f"Cleared orphaned source metadata from item {item_id} ('{title}'): {source}")

                conn.commit()
            except Exception as e:
                conn.rollback()
                logging.error(f"Error clearing orphaned source metadata: {e}", exc_info=True)
                failed_source_cleanups.append({'error': str(e)})

        cursor.close()
        conn.close()
        cursor = None
        conn = None

        result = {
            'success': True,
            'labels_cleaned': labels_cleaned_count,
            'sources_cleaned': sources_cleaned_count
        }

        if failed_label_removals:
            result['failed_label_removals'] = failed_label_removals
            result['failed_label_count'] = len(failed_label_removals)

        if failed_source_cleanups:
            result['failed_source_cleanups'] = failed_source_cleanups
            result['failed_source_count'] = len(failed_source_cleanups)

        logging.info(f"cleanup_orphaned: Completed - cleaned {labels_cleaned_count} orphaned label associations, cleared {sources_cleaned_count} orphaned source metadata")
        return jsonify(result)

    except sqlite3.Error as e:
        logging.error(f"Database error in cleanup_orphaned: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logging.error(f"Error cleaning up orphaned labels: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Internal error: {str(e)}'}), 500
    finally:
        # Ensure database resources are cleaned up
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@plex_labels_debug_bp.route('/debug/plex-labels/sync-all', methods=['POST'])
def sync_all():
    """Sync all labels from content sources"""
    conn = None
    cursor = None
    try:
        logging.info("sync_all: Starting sync of all labels")

        # Get all collected items
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Get movies directly, and one representative episode per show (using MIN(id))
        cursor.execute('''
            SELECT id, title, type, content_source, content_source_detail, state
            FROM media_items
            WHERE state = 'Collected'
            AND content_source IS NOT NULL
            AND type = 'movie'

            UNION

            SELECT MIN(id) as id, title, type, content_source, content_source_detail, state
            FROM media_items
            WHERE state = 'Collected'
            AND content_source IS NOT NULL
            AND type = 'episode'
            GROUP BY title, tmdb_id
        ''')

        items = cursor.fetchall()
        cursor.close()
        conn.close()
        cursor = None
        conn = None

        logging.info(f"sync_all: Found {len(items)} items to sync")

        synced_count = 0
        failed_items = []
        for row in items:
            item = dict(row)
            try:
                # Use sync_labels_to_plex_for_item to ensure all DB labels are synced to Plex
                from utilities.plex_label_manager import sync_labels_to_plex_for_item
                labels_synced = sync_labels_to_plex_for_item(item['id'])
                if labels_synced > 0:
                    synced_count += 1
            except Exception as e:
                logging.error(f"Error syncing labels for item {item['id']}: {e}", exc_info=True)
                failed_items.append({'id': item['id'], 'title': item.get('title'), 'error': str(e)})

        result = {
            'success': True,
            'count': synced_count,
            'total': len(items)
        }

        if failed_items:
            result['failed_items'] = failed_items
            result['failed_count'] = len(failed_items)

        logging.info(f"sync_all: Completed - {synced_count}/{len(items)} items synced successfully")
        return jsonify(result)

    except sqlite3.Error as e:
        logging.error(f"Database error in sync_all: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logging.error(f"Error syncing all labels: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Internal error: {str(e)}'}), 500
    finally:
        # Ensure database resources are cleaned up
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@plex_labels_debug_bp.route('/debug/plex-labels/backfill-content-source-detail', methods=['POST'])
def backfill_content_source_detail_endpoint():
    """Backfill content_source_detail for items with NULL value"""
    from utilities.plex_label_manager import backfill_content_source_detail

    try:
        result = backfill_content_source_detail()

        if result['success']:
            api_queries = result.get('api_queries', 0)
            message = f'Backfilled {result["total_updated"]} items'
            if api_queries > 0:
                message += f' ({api_queries} Overseerr API queries)'

            return jsonify({
                'success': True,
                'message': message,
                'total_updated': result['total_updated'],
                'by_source': result['by_source'],
                'api_queries': api_queries
            })
        else:
            return jsonify({
                'success': False,
                'message': f'Error: {result.get("error", "Unknown error")}'
            }), 500

    except Exception as e:
        logging.error(f"Error in backfill endpoint: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/backfill-resolution', methods=['POST'])
def backfill_resolution_endpoint():
    """Backfill resolution for Collected items with NULL resolution from stored paths"""
    from queues.run_program import backfill_resolution_from_stored_paths

    try:
        result = backfill_resolution_from_stored_paths()

        if result.get('success'):
            message = f'Backfilled resolution for {result["updated"]} items'
            if result.get('failed', 0) > 0:
                message += f' ({result["failed"]} failed)'

            return jsonify({
                'success': True,
                'message': message,
                'total_items': result['total_items'],
                'updated': result['updated'],
                'failed': result['failed'],
                'by_resolution': result.get('by_resolution', {})
            })
        else:
            return jsonify({
                'success': False,
                'message': f'Error: {result.get("error", "Unknown error")}'
            }), 500

    except Exception as e:
        logging.error(f"Error in resolution backfill endpoint: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/sources-list')
def sources_list():
    """Get list of content sources with Plex labels enabled"""
    try:
        all_settings = get_all_settings()
        content_sources = all_settings.get('Content Sources', {})

        sources_with_labels = []
        for source_id, config in content_sources.items():
            plex_labels_config = config.get('plex_labels', {})
            if plex_labels_config.get('enabled', False):
                sources_with_labels.append({
                    'id': source_id,
                    'name': config.get('display_name', source_id),
                    'type': config.get('type', 'unknown'),
                    'label_mode': plex_labels_config.get('label_mode', 'unknown')
                })

        return jsonify({
            'success': True,
            'sources': sources_with_labels
        })

    except Exception as e:
        logging.error(f"Error getting sources list: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/check-config')
def check_config():
    """Check content source configuration for debugging red overlay issue"""
    try:
        all_settings = get_all_settings()
        content_sources = all_settings.get('Content Sources', {})

        results = []
        for source_id, config in content_sources.items():
            results.append({
                'source_id': source_id,
                'type': config.get('type', 'unknown'),
                'enabled': config.get('enabled', 'MISSING'),
                'has_plex_labels': 'plex_labels' in config,
                'plex_labels_enabled': config.get('plex_labels', {}).get('enabled', 'MISSING') if 'plex_labels' in config else 'N/A'
            })

        return jsonify({
            'success': True,
            'sources': results,
            'total': len(results)
        })

    except Exception as e:
        logging.error(f"Error checking config: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/fix-enabled-field', methods=['POST'])
def fix_enabled_field():
    """Fix corrupted enabled fields in content sources"""
    try:
        from utilities.settings import load_config, save_config

        data = request.get_json()
        sources_to_enable = data.get('sources', [])

        if not sources_to_enable:
            return jsonify({'success': False, 'message': 'No sources specified'}), 400

        config = load_config()
        content_sources = config.get('Content Sources', {})

        fixed_count = 0
        for source_id in sources_to_enable:
            if source_id in content_sources:
                content_sources[source_id]['enabled'] = True
                fixed_count += 1
                logging.info(f"Re-enabled content source: {source_id}")

        config['Content Sources'] = content_sources
        save_config(config)

        return jsonify({
            'success': True,
            'fixed_count': fixed_count,
            'message': f'Successfully re-enabled {fixed_count} content source(s)'
        })

    except Exception as e:
        logging.error(f"Error fixing enabled field: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/test-item-lookup/<int:item_id>')
def test_item_lookup(item_id):
    """Test Plex item lookup for debugging"""
    try:
        from utilities.plex_functions import get_plex_item
        from database.database_reading import get_media_item_by_id

        # Get item from database
        item_data = get_media_item_by_id(item_id)

        if not item_data:
            return jsonify({
                'success': False,
                'message': f'Item {item_id} not found in database'
            }), 404

        # Try to get Plex item
        plex_item = get_plex_item(item_id)

        result = {
            'success': True,
            'item_id': item_id,
            'database_info': {
                'title': item_data.get('title'),
                'year': item_data.get('year'),
                'type': item_data.get('type'),
                'imdb_id': item_data.get('imdb_id'),
                'tmdb_id': item_data.get('tmdb_id'),
                'state': item_data.get('state'),
                'content_source': item_data.get('content_source'),
                'plex_labels': item_data.get('plex_labels')
            },
            'plex_found': plex_item is not None
        }

        if plex_item:
            result['plex_info'] = {
                'title': plex_item.title,
                'year': getattr(plex_item, 'year', None),
                'guid': plex_item.guid,
                'labels': [tag.tag for tag in plex_item.labels] if hasattr(plex_item, 'labels') else []
            }

        return jsonify(result)

    except Exception as e:
        logging.error(f"Error testing item lookup for {item_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/find-item-by-title')
def find_item_by_title():
    """Find items by title search"""
    try:
        title_search = request.args.get('title', '').strip()
        if not title_search:
            return jsonify({'success': False, 'message': 'title parameter is required'}), 400

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Search for items with title containing the search string
        cursor.execute('''
            SELECT id, title, year, type, state, content_source, plex_labels, imdb_id, tmdb_id
            FROM media_items
            WHERE title LIKE ?
            ORDER BY id DESC
            LIMIT 20
        ''', (f'%{title_search}%',))

        items = []
        for row in cursor.fetchall():
            items.append({
                'id': row['id'],
                'title': row['title'],
                'year': row['year'],
                'type': row['type'],
                'state': row['state'],
                'content_source': row['content_source'],
                'has_plex_labels': row['plex_labels'] is not None,
                'plex_labels': row['plex_labels'],
                'imdb_id': row['imdb_id'],
                'tmdb_id': row['tmdb_id']
            })

        cursor.close()
        conn.close()

        return jsonify({
            'success': True,
            'search': title_search,
            'count': len(items),
            'items': items
        })

    except Exception as e:
        logging.error(f"Error finding items by title: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/fix-imdb-by-tmdb/<tmdb_id>/<imdb_id>', methods=['POST'])
def fix_imdb_id_by_tmdb(tmdb_id, imdb_id):
    """Fix IMDb ID for all items matching a TMDB ID"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT id, title, imdb_id FROM media_items WHERE tmdb_id = ?', (tmdb_id,))
        rows = cursor.fetchall()
        if not rows:
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': f'No items found with TMDB ID {tmdb_id}'}), 404
        cursor.execute('UPDATE media_items SET imdb_id = ? WHERE tmdb_id = ?', (imdb_id, tmdb_id))
        conn.commit()
        updated = [{'id': r[0], 'title': r[1], 'imdb_id_before': r[2]} for r in rows]
        cursor.close()
        conn.close()
        logging.info(f"Updated IMDb ID to {imdb_id} for {len(updated)} items with TMDB ID {tmdb_id}")
        return jsonify({
            'success': True,
            'updated_count': len(updated),
            'imdb_id_after': imdb_id,
            'items': updated
        })
    except Exception as e:
        logging.error(f"Error fixing IMDb ID by TMDB ID {tmdb_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/fix-imdb/<int:item_id>/<imdb_id>', methods=['POST', 'GET'])
def fix_imdb_id(item_id, imdb_id):
    """Fix missing IMDb ID for an item"""
    try:
        from database.database_reading import get_media_item_by_id

        # Get item from database
        item_data = get_media_item_by_id(item_id)

        if not item_data:
            return jsonify({
                'success': False,
                'message': f'Item {item_id} not found in database'
            }), 404

        logging.info(f"Updating IMDb ID for item {item_id} ({item_data.get('title')}) to {imdb_id}")

        # Update the database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute('UPDATE media_items SET imdb_id = ? WHERE id = ?', (imdb_id, item_id))
        conn.commit()

        # Verify the update
        cursor.execute('SELECT imdb_id FROM media_items WHERE id = ?', (item_id,))
        row = cursor.fetchone()
        updated_imdb_id = row[0] if row else None

        cursor.close()
        conn.close()

        return jsonify({
            'success': True,
            'message': f'Updated IMDb ID for item {item_id}',
            'item_id': item_id,
            'imdb_id_before': item_data.get('imdb_id'),
            'imdb_id_after': updated_imdb_id,
            'title': item_data.get('title')
        })

    except Exception as e:
        logging.error(f"Error fixing IMDb ID for {item_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500


@plex_labels_debug_bp.route('/debug/plex-labels/test-plex-search/<imdb_id>')
def test_plex_search(imdb_id):
    """Test searching Plex directly by IMDb ID"""
    try:
        from utilities.settings import get_setting
        from plexapi.server import PlexServer

        plex_url = get_setting('Plex', 'url')
        plex_token = get_setting('Plex', 'token')

        if not plex_url or not plex_token:
            return jsonify({'success': False, 'message': 'Plex not configured'}), 500

        plex = PlexServer(plex_url, plex_token, timeout=10)

        results = []
        for section in plex.library.sections():
            if section.type == 'movie':
                logging.info(f"Searching section '{section.title}' for IMDb ID: {imdb_id}")
                search_results = section.search(guid__contains=imdb_id)

                if search_results:
                    for result in search_results:
                        results.append({
                            'section': section.title,
                            'title': result.title,
                            'year': getattr(result, 'year', None),
                            'guid': result.guid,
                            'labels': [tag.tag for tag in result.labels] if hasattr(result, 'labels') else []
                        })

        return jsonify({
            'success': True,
            'imdb_id': imdb_id,
            'found': len(results) > 0,
            'results': results
        })

    except Exception as e:
        logging.error(f"Error testing Plex search for {imdb_id}: {e}", exc_info=True)
        return jsonify({'success': False, 'message': str(e)}), 500
