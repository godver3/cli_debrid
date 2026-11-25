"""Memory snapshot and trim."""

from flask import jsonify
import logging
from routes import admin_required
from ._common import analysis_progress, debug_bp, rclone_scan_progress, scan_progress

@debug_bp.route('/api/memory_snapshot', methods=['GET'])
@admin_required
def memory_snapshot():
    """
    Capture a memory diagnostic snapshot of the running process.
    Hit this endpoint WHILE memory is high to identify the actual cause.
    Returns: RSS/VMS, sizes of known module-level caches, top Python object counts by type.
    """
    import gc
    import sys
    try:
        import resource  # Unix only
    except ImportError:
        resource = None
    from collections import Counter

    report = {}

    # --- Process-level RSS / VMS ---
    try:
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        # On Linux ru_maxrss is in kilobytes
        report['process_rss_mb'] = round(rusage.ru_maxrss / 1024, 1)
    except Exception:
        pass

    try:
        with open('/proc/self/status') as fh:
            for line in fh:
                if line.startswith('VmRSS:'):
                    report['vmrss_mb'] = round(int(line.split()[1]) / 1024, 1)
                elif line.startswith('VmSize:'):
                    report['vmsize_mb'] = round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass

    # --- Known module-level data structures ---
    known_structures = {}

    # Session store
    try:
        from routes.extensions import InMemorySessionInterface
        store = InMemorySessionInterface.session_store
        known_structures['session_store_count'] = len(store)
        known_structures['session_store_size_kb'] = round(sys.getsizeof(store) / 1024, 1)
    except Exception as e:
        known_structures['session_store'] = f'error: {e}'

    # base_routes function cache
    try:
        from routes.base_routes import _function_cache
        total_entries = sum(len(v) for v in _function_cache.values())
        total_size = sys.getsizeof(_function_cache)
        known_structures['function_cache_functions'] = list(_function_cache.keys())
        known_structures['function_cache_total_entries'] = total_entries
        known_structures['function_cache_size_kb'] = round(total_size / 1024, 1)
    except Exception as e:
        known_structures['function_cache'] = f'error: {e}'

    # debug_routes progress dicts
    try:
        known_structures['scan_progress_keys'] = len(scan_progress)
        known_structures['analysis_progress_keys'] = len(analysis_progress)
        known_structures['rclone_scan_progress_keys'] = len(rclone_scan_progress)
    except Exception:
        pass

    # Notification queues
    try:
        from routes.base_routes import _notification_queues
        known_structures['notification_queues_count'] = len(_notification_queues)
    except Exception:
        pass

    # SimpleTaskQueue
    try:
        from routes.extensions import task_queue
        known_structures['task_queue_count'] = len(task_queue.tasks)
        done = sum(1 for t in task_queue.tasks.values() if t['status'] in ('SUCCESS', 'FAILURE'))
        known_structures['task_queue_completed'] = done
    except Exception:
        pass

    # queue_times (QueueTimer in-memory dict)
    try:
        from queues.queue_manager import QueueManager
        qt = QueueManager().queue_timer
        known_structures['queue_times_count'] = len(qt.queue_times)
    except Exception as e:
        known_structures['queue_times_count'] = f'error: {e}'

    # _last_scan_results (upgrade hub in-memory scan cache)
    try:
        from database.zilean_upgrade import _last_scan_results, _queued_magnets
        if _last_scan_results:
            candidates = len(_last_scan_results.get('upgrade_candidates', []))
            packs = len(_last_scan_results.get('pack_candidates', []))
            known_structures['last_scan_results_candidates'] = candidates
            known_structures['last_scan_results_packs'] = packs
            known_structures['last_scan_results_size_kb'] = round(sys.getsizeof(_last_scan_results) / 1024, 1)
        else:
            known_structures['last_scan_results_candidates'] = 0
        known_structures['queued_magnets_count'] = len(_queued_magnets) if _queued_magnets else 0
    except Exception as e:
        known_structures['last_scan_results'] = f'error: {e}'

    # _search_cache (web_scraper module-level cache)
    try:
        from utilities.web_scraper import _search_cache
        info = _search_cache.get_stats() if hasattr(_search_cache, 'get_stats') else {}
        known_structures['search_cache_size'] = info.get('size', len(_search_cache._cache))
        known_structures['search_cache_max'] = info.get('max_size', _search_cache.max_size)
    except Exception as e:
        known_structures['search_cache'] = f'error: {e}'

    # Open SQLite connections (file handles to .db files)
    try:
        import os as _os
        db_handles = []
        fd_dir = '/proc/self/fd'
        for fd_name in _os.listdir(fd_dir):
            try:
                target = _os.readlink(_os.path.join(fd_dir, fd_name))
                if target.endswith('.db') or target.endswith('.db-wal') or target.endswith('.db-shm'):
                    db_handles.append(_os.path.basename(target))
            except Exception:
                pass
        known_structures['open_db_file_handles'] = len(db_handles)
        known_structures['open_db_files'] = sorted(set(db_handles))
    except Exception as e:
        known_structures['open_db_file_handles'] = f'error: {e}'

    # Poster cache entry count
    try:
        from routes.poster_cache import _cache as _poster_cache
        known_structures['poster_cache_entries'] = len(_poster_cache)
        known_structures['poster_cache_size_kb'] = round(sys.getsizeof(_poster_cache) / 1024, 1)
    except Exception as e:
        known_structures['poster_cache'] = f'error: {e}'

    report['known_structures'] = known_structures

    # --- Top Python object counts by type (gc) ---
    try:
        gc.collect()
        type_counts = Counter(type(obj).__name__ for obj in gc.get_objects())
        report['top_object_types'] = type_counts.most_common(30)
    except Exception as e:
        report['top_object_types_error'] = str(e)

    # --- Thread count ---
    try:
        import threading
        report['thread_count'] = threading.active_count()
        report['thread_names'] = [t.name for t in threading.enumerate()]
    except Exception:
        pass

    return jsonify({'success': True, 'snapshot': report})

@debug_bp.route('/api/trim_memory', methods=['POST'])
@admin_required
def trim_memory():
    """Run gc.collect() + malloc_trim(0) synchronously and return before/after RSS."""
    import gc
    import ctypes

    def _read_rss_mb():
        try:
            with open('/proc/self/status') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        return int(line.split()[1]) / 1024
        except Exception:
            pass
        return None

    before = _read_rss_mb()

    # Clear idle RD library cache to free ~200+ MB from torrent list
    lib_torrents_freed = 0
    try:
        from routes.debrid_manager_routes import _lib
        with _lib['lock']:
            if _lib['stable'] is not None:
                lib_torrents_freed = len(_lib['stable'].get('torrents', []))
                _lib['stable'] = None
        logging.info(f"[TRIM_MEMORY] Cleared RD library cache ({lib_torrents_freed} torrents)")
    except Exception as e:
        logging.debug(f"[TRIM_MEMORY] Could not clear RD library cache: {e}")

    collected = gc.collect()
    trim_ok = False
    try:
        ctypes.CDLL('libc.so.6').malloc_trim(0)
        trim_ok = True
    except Exception:
        pass
    after = _read_rss_mb()

    freed = round(before - after, 1) if before and after else None
    logging.info(f"[TRIM_MEMORY] Manual trigger: gc collected {collected} objects; malloc_trim={'ok' if trim_ok else 'unavailable'}; RSS {before:.0f} MB → {after:.0f} MB (freed {freed} MB)")
    return jsonify({
        'success': True,
        'gc_collected': collected,
        'malloc_trim': trim_ok,
        'before_mb': round(before, 1) if before else None,
        'after_mb': round(after, 1) if after else None,
        'freed_mb': freed,
        'lib_torrents_freed': lib_torrents_freed,
    })
