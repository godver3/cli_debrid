"""Replace files Plex users report as broken with Plex's "Report Issue".

Plex's Report Issue (in an item's ... menu) sends a short message to the server
owner. Plex hasn't documented an API for it, but Plex Web reads and answers the
reports through the GraphQL endpoint on community.plex.tv using the owner's
token; the queries below are the ones Plex Web sends. Each report carries the
item as server://<machineIdentifier>/com.plexapp.plugins.library/library/metadata/<ratingKey>.

For each new report about this server, the reported movie or episode is looked
up in Plex, matched to the media_items rows that hold the file Plex plays, and
sent back to Wanted to get a different release (the old file keeps playing until
the new one is Collected, see move_to_wanted_for_replacement). The reporter gets
a reply in the report thread when it's picked up and again once it's replaced.
Plex can't close or delete reports, so handled ones are tracked in the
plex_reported_issues table.

Plex has no push or webhook for reports (its pubsub channel only carries account
and Watch Together events, and Plex Web re-fetches the list too), so the task
checks every 30 seconds; a check with nothing new is one small request.
"""

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from routes.api_tracker import api
from utilities.settings import get_setting
from database import plex_reported_issues as reports_db

COMMUNITY_API_URL = 'https://community.plex.tv/api'
REQUEST_TIMEOUT = 20
MAX_REPORTS_PER_RUN = 200

REPORT_URL_RE = re.compile(
    r'^server://(?P<server>[0-9a-fA-F]+)/com\.plexapp\.plugins\.library/library/metadata/(?P<key>\d+)')

GET_REPORTS_QUERY = """
query getReportedIssues($first: PaginationInt!, $after: String) {
  reports(after: $after, first: $first) {
    nodes { id message user { id username displayName } url date commentCount }
    pageInfo { hasNextPage endCursor }
  }
}"""

CREATE_COMMENT_MUTATION = """
mutation createReportComment($input: CreateReportCommentInput!) {
  createReportComment(input: $input) { id }
}"""

# A report on these Plex types would mean replacing many episodes at once.
WHOLE_SHOW_TYPES = ('show', 'season')
# Stop replacing an item automatically after this many report-triggered replacements in
# REPLACEMENT_WINDOW, so repeated reports can't rescrape it in an endless loop.
MAX_REPLACEMENTS_PER_WINDOW = 3
REPLACEMENT_WINDOW = timedelta(hours=24)
# A report that errored (Plex unreachable, say) is tried again after this long.
FAILED_RETRY_AFTER = timedelta(minutes=5)
# Rows in these states are already on their way to a new release.
IN_PROGRESS_STATES = ('Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 'Pending Uncached')


def _plex_credentials() -> Tuple[str, str]:
    """(url, token) of the Plex server cli_debrid manages files for, or ('', '')."""
    if get_setting('File Management', 'file_collection_management') == 'Plex':
        url = get_setting('Plex', 'url', '')
        token = get_setting('Plex', 'token', '')
    else:
        if get_setting('File Management', 'media_server_type', 'plex') != 'plex':
            return '', ''
        url = get_setting('File Management', 'plex_url_for_symlink', '')
        token = get_setting('File Management', 'plex_token_for_symlink', '')
    return (url or '').rstrip('/'), token or ''


def _plex_headers(token: str) -> Dict[str, str]:
    return {
        'X-Plex-Token': token,
        'X-Plex-Client-Identifier': 'cli-debrid-reported-issues',
        'X-Plex-Product': 'cli_debrid',
        'Accept': 'application/json',
    }


def _graphql(token: str, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    response = api.post(COMMUNITY_API_URL, json={'query': query, 'variables': variables},
                        headers=_plex_headers(token), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if payload.get('errors'):
        raise RuntimeError(f"Plex GraphQL error: {payload['errors'][0].get('message')}")
    return payload.get('data') or {}


def parse_report(node: Dict[str, Any]) -> Dict[str, Any]:
    user = node.get('user') or {}
    match = REPORT_URL_RE.match(node.get('url') or '')
    return {
        'id': node.get('id'),
        'message': (node.get('message') or '').strip(),
        'reporter': user.get('displayName') or user.get('username') or 'Unknown user',
        'date': node.get('date'),
        'server_id': match.group('server').lower() if match else None,
        'rating_key': match.group('key') if match else None,
    }


def fetch_reports(token: str, known_ids: Optional[set] = None) -> List[Dict[str, Any]]:
    """The newest reports sent to (and by) this Plex account, newest first. With known_ids,
    stops at the first report already seen, so a check with nothing new is one small request."""
    reports, after = [], None
    while len(reports) < MAX_REPORTS_PER_RUN:
        data = _graphql(token, GET_REPORTS_QUERY, {'first': 20, 'after': after})
        page = data.get('reports') or {}
        for node in page.get('nodes') or []:
            if not node.get('id'):
                continue
            if known_ids is not None and node['id'] in known_ids:
                return reports
            reports.append(parse_report(node))
        info = page.get('pageInfo') or {}
        if not info.get('hasNextPage') or not info.get('endCursor'):
            break
        after = info['endCursor']
    return reports


def reply_to_report(token: str, report_id: str, message: str) -> bool:
    if not get_setting('Plex Reported Issues', 'reply_to_reporter', True):
        return False
    try:
        _graphql(token, CREATE_COMMENT_MUTATION, {'input': {'message': message, 'report': report_id}})
        return True
    except Exception as e:
        logging.warning(f"[PLEX_REPORTS] Could not reply to report {report_id}: {e}")
        return False


def _server_machine_id(url: str, token: str) -> Optional[str]:
    response = api.get(f"{url}/identity", headers=_plex_headers(token), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return ((response.json().get('MediaContainer') or {}).get('machineIdentifier') or '').lower() or None


def _plex_metadata(url: str, token: str, rating_key: str) -> Optional[Dict[str, Any]]:
    response = api.get(f"{url}/library/metadata/{rating_key}?includeGuids=1",
                       headers=_plex_headers(token), timeout=REQUEST_TIMEOUT)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    items = (response.json().get('MediaContainer') or {}).get('Metadata') or []
    return items[0] if items else None


def _guid_ids(metadata: Dict[str, Any]) -> Dict[str, str]:
    ids = {}
    for guid in metadata.get('Guid') or []:
        scheme, _, value = (guid.get('id') or '').partition('://')
        if scheme in ('imdb', 'tmdb', 'tvdb') and value:
            ids[scheme] = value
    return ids


def _part_files(metadata: Dict[str, Any]) -> List[str]:
    return [part['file'] for media in metadata.get('Media') or []
            for part in media.get('Part') or [] if part.get('file')]


def _display_title(metadata: Dict[str, Any]) -> str:
    if metadata.get('type') == 'episode':
        return (f"{metadata.get('grandparentTitle', '')} "
                f"S{int(metadata.get('parentIndex') or 0):02d}E{int(metadata.get('index') or 0):02d}").strip()
    if metadata.get('type') == 'season':
        return f"{metadata.get('parentTitle', '')} {metadata.get('title', '')}".strip()
    year = metadata.get('year')
    return f"{metadata.get('title', '')} ({year})" if year else metadata.get('title', '')


def find_reported_rows(cursor, metadata: Dict[str, Any], show_ids: Dict[str, str]) -> List[Dict[str, Any]]:
    """media_items rows for the reported movie/episode, by IMDb/TMDB ID (plus season and
    episode): Collected rows whose file is one Plex has for the item (so a copy cli_debrid
    doesn't manage never triggers a replacement of one it does), and rows already being
    replaced."""
    item_type = metadata.get('type')
    ids = show_ids if item_type == 'episode' else _guid_ids(metadata)
    id_clauses, params = [], []
    for column, key in (('imdb_id', 'imdb'), ('tmdb_id', 'tmdb')):
        if ids.get(key):
            id_clauses.append(f"{column} = ?")
            params.append(ids[key])
    if not id_clauses:
        return []
    where = f"type = ? AND ({' OR '.join(id_clauses)}) AND (ghostlisted = 0 OR ghostlisted IS NULL)"
    params.insert(0, item_type)
    if item_type == 'episode':
        where += " AND season_number = ? AND episode_number = ?"
        params += [metadata.get('parentIndex'), metadata.get('index')]
    rows = [dict(r) for r in cursor.execute(
        f"""SELECT id, state, version, location_on_disk, location_basename, filled_by_file,
                   replaced_files
            FROM media_items WHERE {where}""", params).fetchall()]

    plex_files = set(_part_files(metadata))
    plex_basenames = {os.path.basename(f) for f in plex_files}

    def plays_in_plex(row):
        location = row.get('location_on_disk') or ''
        if location in plex_files or (location and os.path.basename(location) in plex_basenames):
            return True
        filled = row.get('filled_by_file') or ''
        return bool(filled) and os.path.basename(filled) in plex_basenames

    # A row on its way to a new release still has its old files stashed in replaced_files.
    return [r for r in rows
            if (r['state'] in IN_PROGRESS_STATES and r.get('replaced_files') not in (None, '', '[]'))
            or (r['state'] in ('Collected', 'Upgrading') and plays_in_plex(r))]


def _notify_owner(message: str) -> None:
    try:
        from routes.notifications import _send_notifications, get_enabled_notifications
        enabled = get_enabled_notifications()
        if enabled:
            _send_notifications(message, enabled, notification_category='program_info')
    except Exception as e:
        logging.warning(f"[PLEX_REPORTS] Could not send notification: {e}")


def handle_report(report: Dict[str, Any], plex_url: str, token: str, machine_id: str) -> str:
    """Act on one new report and record it. Returns the status it was saved with."""
    if not report['rating_key'] or report['server_id'] != machine_id:
        reports_db.save_report(report, 'ignored', 'Not an item on this Plex server')
        return 'ignored'

    metadata = _plex_metadata(plex_url, token, report['rating_key'])
    if not metadata:
        reports_db.save_report(report, 'ignored', 'The item no longer exists in Plex')
        return 'ignored'
    title = _display_title(metadata)
    quoted = f' "{report["message"]}"' if report['message'] else ''
    log_prefix = f"[PLEX_REPORTS] {report['reporter']} reported {title}{quoted}:"

    if metadata.get('type') in WHOLE_SHOW_TYPES:
        logging.info(f"{log_prefix} whole {metadata['type']}, not replacing automatically")
        reports_db.save_report(report, 'needs_review', f"Reported the whole {metadata['type']}", title)
        reply_to_report(token, report['id'],
                        "Thanks for the report! Please report the specific episode that isn't "
                        "working so it can be replaced.")
        _notify_owner(f"Plex issue reported by {report['reporter']}: {title}{quoted}. "
                      f"It's a whole {metadata['type']}, so nothing was replaced automatically.")
        return 'needs_review'
    if metadata.get('type') not in ('movie', 'episode'):
        reports_db.save_report(report, 'no_match', f"Plex item type {metadata.get('type')!r}", title)
        return 'no_match'

    show_ids = {}
    if metadata.get('type') == 'episode' and metadata.get('grandparentRatingKey'):
        show = _plex_metadata(plex_url, token, metadata['grandparentRatingKey'])
        show_ids = _guid_ids(show) if show else {}

    from database import get_db_connection
    from database.database_writing import move_to_wanted_for_replacement
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        rows = find_reported_rows(cursor, metadata, show_ids)
        collected = [r['id'] for r in rows if r['state'] in ('Collected', 'Upgrading')]
        in_progress = [r['id'] for r in rows if r['state'] in IN_PROGRESS_STATES]
        if collected and reports_db.count_recent_replacements(
                collected, datetime.now() - REPLACEMENT_WINDOW) >= MAX_REPLACEMENTS_PER_WINDOW:
            logging.info(f"{log_prefix} already replaced {MAX_REPLACEMENTS_PER_WINDOW} times today, not again")
            reports_db.save_report(report, 'needs_review',
                                   f'Replaced {MAX_REPLACEMENTS_PER_WINDOW} times in 24 hours already', title, collected)
            reply_to_report(token, report['id'],
                            f"Thanks for the report! {title} has already been replaced several times today, "
                            "so the server owner has been asked to take a look.")
            _notify_owner(f"Plex issue reported by {report['reporter']}: {title}{quoted}. It was already "
                          f"replaced {MAX_REPLACEMENTS_PER_WINDOW} times in 24 hours, so it needs a look.")
            return 'needs_review'
        if collected:
            placeholders = ','.join('?' * len(collected))
            moved = move_to_wanted_for_replacement(cursor, f"id IN ({placeholders})", collected)
            conn.commit()
        else:
            moved = 0
    finally:
        conn.close()

    if moved:
        from routes.database_routes import sync_queues_after_rescrape
        sync_queues_after_rescrape(collected)
        logging.info(f"{log_prefix} replacing item(s) {collected}")
        reports_db.save_report(report, 'replacing', 'Looking for a different release', title, collected,
                               triggered=True)
        reply_to_report(token, report['id'],
                        f"Thanks for the report! Finding a different copy of {title} now. "
                        "The current one stays available until the new one is ready.")
        _notify_owner(f"Plex issue reported by {report['reporter']}: {title}{quoted}. Replacing it now.")
        return 'replacing'
    if in_progress:
        logging.info(f"{log_prefix} already being replaced (item(s) {in_progress})")
        reports_db.save_report(report, 'replacing', 'Already being replaced', title, in_progress)
        reply_to_report(token, report['id'],
                        f"Thanks for the report! {title} is already being replaced.")
        return 'replacing'

    logging.info(f"{log_prefix} no cli_debrid item uses the file Plex plays")
    reports_db.save_report(report, 'no_match', "cli_debrid doesn't manage the file Plex plays", title)
    _notify_owner(f"Plex issue reported by {report['reporter']}: {title}{quoted}. "
                  "cli_debrid doesn't manage that file, so nothing was replaced.")
    return 'no_match'


def check_replacing_reports(token: str) -> None:
    """Reply "done" to reports whose items are all Collected again, and flag the ones whose
    items were blacklisted (no other release could be found)."""
    from database import get_db_connection
    for report in reports_db.get_reports(status='replacing'):
        item_ids = report['item_ids']
        if not item_ids:
            continue
        conn = get_db_connection()
        try:
            placeholders = ','.join('?' * len(item_ids))
            states = [r[0] for r in conn.execute(
                f"SELECT state FROM media_items WHERE id IN ({placeholders})", item_ids).fetchall()]
        finally:
            conn.close()
        title = report.get('title') or 'The item'
        if states and all(s == 'Collected' for s in states):
            reply_to_report(token, report['report_id'],
                            f"{title} has been replaced with a different copy. "
                            "Let us know if it's still not working.")
            reports_db.set_report_status(report['report_id'], 'replaced', 'Replaced', done=True)
            logging.info(f"[PLEX_REPORTS] {title} replaced for report {report['report_id']}")
        elif not states or any(s == 'Blacklisted' for s in states):
            reports_db.set_report_status(report['report_id'], 'needs_review',
                                         'No other release was found')
            _notify_owner(f"Couldn't find a different copy of {title} "
                          f"(reported by {report.get('reporter')}). It needs a look.")


def process_reported_issues() -> Dict[str, int]:
    """Handle new Plex reports and follow up on ones being replaced. Returns counts by status."""
    counts: Dict[str, int] = {}
    plex_url, token = _plex_credentials()
    if not plex_url or not token:
        logging.debug("[PLEX_REPORTS] No Plex server configured, skipping")
        return counts

    if not reports_db.is_initialized():
        reports = fetch_reports(token)
        # First run: whatever is already in the inbox predates the feature being turned
        # on, so only remember it. Replacing a backlog of old reports would be a surprise.
        for report in reports:
            reports_db.save_report(report, 'baseline', 'Reported before this feature was enabled')
        reports_db.mark_initialized()
        logging.info(f"[PLEX_REPORTS] First run: recorded {len(reports)} existing report(s) without acting on them")
        counts['baseline'] = len(reports)
        return counts

    new_reports = list(reversed(fetch_reports(token, reports_db.get_known_report_ids())))
    to_handle = reports_db.get_failed_reports(datetime.now() - FAILED_RETRY_AFTER) + new_reports
    machine_id = _server_machine_id(plex_url, token) if to_handle else None
    for report in to_handle:
        try:
            status = handle_report(report, plex_url, token, machine_id)
        except Exception as e:
            logging.error(f"[PLEX_REPORTS] Error handling report {report['id']}: {e}", exc_info=True)
            reports_db.save_report(report, 'failed', str(e)[:500])
            status = 'failed'
        counts[status] = counts.get(status, 0) + 1

    check_replacing_reports(token)
    return counts
