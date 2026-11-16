"""One-time move of global watchlist/Seerr options onto each content source."""
import logging

from utilities.settings import load_config, save_config


def migrate_watchlist_options_to_sources():
    """Move the old global watchlist-removal and partial-Seerr options onto each source.

    They used to be Debug settings that applied to every watchlist of a kind; each
    content source now has its own. Every existing source of the matching type gets
    the old value, so nothing changes for anyone who had them on.
    """
    moves = {
        ('plex_watchlist_removal', 'remove_when_collected'): (
            'My Plex Watchlist', 'Other Plex Watchlist', 'My Plex RSS Watchlist', 'My Friends Plex RSS Watchlist'),
        ('plex_watchlist_keep_series', 'keep_series'): (
            'My Plex Watchlist', 'Other Plex Watchlist', 'My Plex RSS Watchlist', 'My Friends Plex RSS Watchlist'),
        ('trakt_watchlist_removal', 'remove_when_collected'): ('Trakt Watchlist',),
        ('trakt_watchlist_keep_series', 'keep_series'): ('Trakt Watchlist',),
        ('allow_partial_overseerr_requests', 'allow_partial_requests'): ('Overseerr',),
    }
    try:
        config = load_config()
        debug = config.get('Debug') or {}
        scraping = config.get('Scraping') or {}
        sources = config.get('Content Sources') or {}
        changed = False
        for (old_key, new_key), types in moves.items():
            if old_key not in debug and old_key not in scraping:
                continue
            raw = debug.get(old_key, scraping.get(old_key, False))
            value = raw if isinstance(raw, bool) else str(raw).strip().lower() == 'true'
            for source_id, source in sources.items():
                if not isinstance(source, dict):
                    continue
                source_type = source.get('type') or source_id.split('_')[0]
                if source_type in types and new_key not in source:
                    source[new_key] = value
            debug.pop(old_key, None)
            scraping.pop(old_key, None)
            changed = True
            logging.info(f"Moved {old_key}={value} to '{new_key}' on every {'/'.join(types)} source")
        if changed:
            save_config(config)
    except Exception as e:
        logging.error(f"Error moving watchlist options to content sources: {e}", exc_info=True)
