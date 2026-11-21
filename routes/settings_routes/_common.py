"""Settings blueprint, settings-key migrations and the post-save component reload."""

from flask import Blueprint
from utilities.settings import load_config, save_config
from utilities.settings_schema import SETTINGS_SCHEMA
import logging
from ..utils import is_user_system_enabled

settings_bp = Blueprint('settings', __name__)

# Migration function to auto-assign priorities to scrapers
def migrate_scraper_priorities():
    """
    Auto-assign priority scores to scrapers that don't have them.
    Last enabled scraper gets priority 0, incrementing by 10 going up the list.
    """
    try:
        config = load_config()
        scrapers = config.get('Scrapers', {})

        # Check if migration is needed
        needs_migration = False
        for scraper_id, scraper_config in scrapers.items():
            if 'priority' not in scraper_config:
                needs_migration = True
                break

        if not needs_migration:
            return

        logging.info("Migrating scraper priorities...")

        # Get list of all scrapers in current order
        scraper_list = list(scrapers.items())

        # Get enabled scrapers in current order
        enabled_scrapers = [(scraper_id, cfg) for scraper_id, cfg in scraper_list if cfg.get('enabled', False)]

        # Assign priorities: last enabled = 0, increment by 10 going up
        if enabled_scrapers:
            for index, (scraper_id, _) in enumerate(reversed(enabled_scrapers)):
                priority = index * 10
                scrapers[scraper_id]['priority'] = priority
                logging.info(f"Assigned priority {priority} to scraper '{scraper_id}'")

        # Assign priority 0 to disabled scrapers
        for scraper_id, scraper_config in scrapers.items():
            if 'priority' not in scraper_config:
                scrapers[scraper_id]['priority'] = 0
                logging.info(f"Assigned default priority 0 to disabled scraper '{scraper_id}'")

        # Save the config
        config['Scrapers'] = scrapers
        save_config(config)
        logging.info("Scraper priority migration completed successfully")

    except Exception as e:
        logging.error(f"Error during scraper priority migration: {e}", exc_info=True)

def migrate_clean_version_filter_keys():
    """
    Remove spurious scalar filter keys that were written to version configs by
    older code. These keys are: 'preferred-filter-out-term',
    'preferred-filter-out-weight', 'preferred-filter-in-term',
    'preferred-filter-in-weight'. The correct data lives in the
    'preferred_filter_out' / 'preferred_filter_in' list fields.
    """
    SPURIOUS_KEYS = {
        'preferred-filter-out-term',
        'preferred-filter-out-weight',
        'preferred-filter-in-term',
        'preferred-filter-in-weight',
    }
    try:
        config = load_config()
        versions = config.get('Scraping', {}).get('versions', {})
        changed = False
        for version_id, version_config in versions.items():
            if not isinstance(version_config, dict):
                continue
            for k in SPURIOUS_KEYS & version_config.keys():
                logging.info(f"Removing spurious key '{k}' from version '{version_id}'")
                del version_config[k]
                changed = True
        if changed:
            save_config(config)
            logging.info("migrate_clean_version_filter_keys: cleaned and saved config")
    except Exception as e:
        logging.error(f"Error during version filter key cleanup migration: {e}", exc_info=True)

def migrate_clean_stale_scraper_keys_from_versions():
    """
    Remove stale per-scraper score keys from version configs that belong to
    scrapers that have been disabled or fully removed. These keys look like
    'TorrentioScraper_1', 'Prowlarr_2', etc. and are written by the scraper
    priority UI. Valid version config keys are those defined in the version
    schema plus 'display_name' and currently active scraper IDs.
    """
    try:
        config = load_config()
        active_scraper_ids = {
            k for k, v in config.get('Scrapers', {}).items()
            if isinstance(v, dict) and v.get('enabled', False)
        }
        valid_keys = (
            set(SETTINGS_SCHEMA.get('Scraping', {}).get('versions', {}).get('schema', {}).keys())
            | {'display_name'}
            | active_scraper_ids
        )
        versions = config.get('Scraping', {}).get('versions', {})
        changed = False
        for version_id, version_config in versions.items():
            if not isinstance(version_config, dict):
                continue
            stale = [k for k in version_config if k not in valid_keys]
            for k in stale:
                logging.info(f"Removing stale key '{k}' from version '{version_id}'")
                del version_config[k]
                changed = True
        if changed:
            save_config(config)
            logging.info("migrate_clean_stale_scraper_keys_from_versions: cleaned and saved config")
    except Exception as e:
        logging.error(f"Error during stale scraper key cleanup migration: {e}", exc_info=True)

# Helper to reload components and restart if program was running
def _reload_components_after_settings_change(change_type='full'):
    """
    Reload components after settings change with granular control.

    Args:
        change_type (str): Type of change that occurred. Options:
            - 'full': Complete reinitialization (default, for major setting changes)
            - 'scraper': Only reload scraping components (for scraper add/delete/reorder)
            - 'content_source': Only reload content source tasks (for content source add/delete)
            - 'version': Only reload version settings (for version add/delete/rename/duplicate)
            - 'notification': Minimal reload for notification changes
            - 'ui': No component reload needed (for UI-only settings)
    """
    logging.info(f"Reloading components after settings change (type: {change_type})")

    # For UI-only changes, no reinitialization needed
    if change_type == 'ui':
        logging.info("UI-only change, skipping component reinitialization")
        return

    # For notification changes, minimal reinitialization
    if change_type == 'notification':
        logging.info("Notification change, no component reinitialization needed")
        return

    # For version changes, settings are loaded on-demand during scraping, so no reinit needed
    if change_type == 'version':
        logging.info("Version change, settings will be picked up on next scrape")
        return

    # For scraper changes, only reinitialize scraping-related components
    if change_type == 'scraper':
        try:
            from queues.queue_manager import QueueManager
            # Only reinitialize the Scraping queue, not all queues
            QueueManager().reinitialize_queue('Scraping')
            logging.info("Scraping queue reinitialized after scraper change")
            return
        except Exception as e:
            logging.error(f"Error reinitializing Scraping queue: {e}", exc_info=True)
            # Fall through to full reinit on error

    # For content source changes, reinitialize ProgramRunner to reload content source tasks
    if change_type == 'content_source':
        try:
            from routes.program_operation_routes import get_program_runner
            runner = get_program_runner()
            was_running = bool(runner and runner.is_running())
        except Exception as e:
            logging.warning(f"Could not check program status before content source reinitialization: {e}")
            was_running = False

        try:
            from queues.run_program import ProgramRunner
            # Reinitialize ProgramRunner to invalidate content source cache and reload tasks
            # This is necessary because content sources are cached in memory
            ProgramRunner().reinitialize()
            logging.info("ProgramRunner reinitialized after content source change")
        except Exception as reinit_e:
            logging.error(f"Error reinitializing ProgramRunner for content source change: {reinit_e}", exc_info=True)
            # Fall through to full reinit on error
            pass  # Will fall through to full reinit below

        # Restart program if it was running before
        if was_running:
            try:
                from routes.program_operation_routes import _execute_start_program
                _execute_start_program(skip_connectivity_check=True, is_restart=True)
                logging.info("Program restarted successfully after content source change.")
                return
            except Exception as restart_e:
                logging.error(f"Error restarting program after content source change: {restart_e}", exc_info=True)
                # Fall through to full reinit on error
        else:
            # Program wasn't running, we're done
            return

    # Full reinitialization for 'full' type or if targeted reload failed
    logging.info("Performing full component reinitialization")
    try:
        from routes.program_operation_routes import get_program_runner
        runner = get_program_runner()
        was_running = bool(runner and runner.is_running())
    except Exception as e:
        logging.warning(f"Could not check program status before reinitialization: {e}")
        was_running = False

    try:
        from debrid import reset_provider
        reset_provider()
        from queues.queue_manager import QueueManager
        QueueManager().reinitialize()
        from queues.run_program import ProgramRunner
        ProgramRunner().reinitialize()
        logging.info("Relevant components reinitialized after settings change.")
    except Exception as reinit_e:
        logging.error(f"Error during component reinitialization after settings change: {reinit_e}", exc_info=True)

    if was_running:
        try:
            from routes.program_operation_routes import _execute_start_program
            _execute_start_program(skip_connectivity_check=True, is_restart=True)
            logging.info("Program restarted successfully after settings change.")
        except Exception as restart_e:
            logging.error(f"Error restarting program after settings change: {restart_e}", exc_info=True)
