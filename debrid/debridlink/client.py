"""
Debrid-Link provider — API v2
Docs: https://debrid-link.com/api_doc/v2/introduction

Key design notes:
  - Auth: Authorization: Bearer <api_key>
  - Add:  POST /seedbox/add  {"url": "magnet:..."} or multipart for .torrent
  - List: GET  /seedbox/list  (paginated, perPage max 100)
  - Del:  DELETE /seedbox/{id}/remove
  - Cache check: ENDPOINT REMOVED — not supported
  - Completion: downloadPercent == 100 OR downloaded == true
  - Hash field: hashString (not hash)
  - Size field: totalSize (not bytes)
  - Date field: created (Unix timestamp int64)
  - Status field: integer (undocumented values) — use downloadPercent/downloaded
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from ..base import DebridProvider, ProviderUnavailableError, TooManyDownloadsError, TorrentAdditionError
from ..common import extract_hash_from_magnet, is_video_file, is_unwanted_file, timed_lru_cache
from ..status import TorrentFetchStatus, TorrentInfoStatus, TorrentStatus
from .api import get_api_key, get_all_torrents, make_request
from .exceptions import DebridLinkAPIError, DebridLinkAuthError
from utilities.settings import get_setting

logger = logging.getLogger(__name__)


class DebridLinkProvider(DebridProvider):
    """Debrid-Link implementation of DebridProvider."""

    PROVIDER_NAME = "Debrid-Link"
    MAX_DOWNLOADS = 50   # Debrid-Link doesn't publish a hard limit; 50 is a safe ceiling

    def __init__(self):
        super().__init__()
        self._cached_torrent_ids: Dict[str, str] = {}   # hash → torrent id
        self._all_torrent_ids:    Dict[str, str] = {}   # hash → torrent id

    # ── Auth ────────────────────────────────────────────────────────────────

    def _load_api_key(self) -> str:
        if getattr(self, '_api_key', None):
            return self._api_key
        try:
            return get_api_key()
        except Exception as e:
            raise ProviderUnavailableError(f"Failed to load Debrid-Link API key: {e}")

    @property
    def api_key(self) -> str:
        return self._load_api_key()

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _is_complete(self, t: Dict) -> bool:
        return bool(t.get('downloaded')) or int(t.get('downloadPercent', 0)) == 100

    def _is_error(self, t: Dict) -> bool:
        return int(t.get('error', 0)) != 0

    def _normalize(self, t: Dict) -> Dict:
        """Convert a raw SeedboxTorrent dict to the standard provider format."""
        h = (t.get('hashString') or '').lower()
        complete = self._is_complete(t)
        error    = self._is_error(t)

        if complete:
            status_str = str(TorrentStatus.DOWNLOADED.value)
        elif error:
            status_str = str(TorrentStatus.ERROR.value)
        elif t.get('wait'):
            status_str = 'waiting_files_selection'
        else:
            status_str = str(TorrentStatus.DOWNLOADING.value)

        progress = 100.0 if complete else float(t.get('downloadPercent', 0))

        # created is a Unix timestamp (int64)
        created = t.get('created')
        added = None
        if created:
            try:
                added = datetime.fromtimestamp(int(created), tz=timezone.utc).isoformat()
            except Exception:
                added = str(created)

        files = []
        for f in (t.get('files') or []):
            files.append({
                'path':          f.get('name', ''),
                'name':          os.path.basename(f.get('name', '')),
                'bytes':         int(f.get('size', 0) or 0),
                'link':          f.get('downloadUrl', ''),
                'stream_link':   '',
                'selected':      1,
                'downloaded':    f.get('downloaded', False),
                'download_percent': f.get('downloadPercent', 0),
            })

        return {
            'id':                str(t.get('id', '')),
            'filename':          t.get('name', ''),
            'original_filename': t.get('name', ''),
            'hash':              h,
            'status':            status_str,
            'progress':          progress,
            'bytes':             int(t.get('totalSize', 0) or 0),
            'added':             added,
            'message':           t.get('errorString', '') if error else '',
            'debrid_folder_name': t.get('name', ''),
            'files':             files,
            'seeders':           t.get('peersConnected'),
            'speed':             t.get('downloadSpeed'),
        }

    def _find_by_hash(self, hash_value: str) -> Optional[Dict]:
        """Find an existing torrent by hash in the seedbox."""
        for t in get_all_torrents(self.api_key):
            if (t.get('hashString') or '').lower() == hash_value.lower():
                return t
        return None

    # ── Cache check ─────────────────────────────────────────────────────────

    async def is_cached(
        self,
        magnet_or_url: Union[str, List[str]],
        temp_file_path: Optional[str] = None,
        result_title: Optional[str] = None,
        result_index: Optional[str] = None,
        remove_uncached: bool = True,
        skip_phalanx_db: bool = False,
        **kwargs,
    ) -> Union[bool, Dict[str, Optional[bool]]]:
        """
        Debrid-Link has no dedicated cache check endpoint (/seedbox/cached was removed).

        Per API docs: "The hash is only added if it is already cached on our servers."
        POST /seedbox/add with a raw hash acts as an implicit cache probe:
        - downloadPercent == 100 / downloaded == true immediately → cached
        - progress < 100 → not cached; remove it (same pattern as RealDebrid)

        Follows the full RealDebrid removal pattern:
        - Remove on not cached (with update_cache_check_removal tracking)
        - Remove on error status
        - Remove on no video files
        - Remove on exception
        - Never remove pre-existing torrents
        """
        from database.not_wanted_magnets import add_to_not_wanted

        items = magnet_or_url if isinstance(magnet_or_url, list) else [magnet_or_url]
        return_single = not isinstance(magnet_or_url, list)
        results: Dict[str, Optional[bool]] = {}

        for item in items:
            torrent_id = None
            torrent_was_preexisting = False

            try:
                hash_value = extract_hash_from_magnet(item) if item.startswith('magnet:') else None
                if not hash_value and len(item) == 40 and all(c in '0123456789abcdefABCDEF' for c in item):
                    hash_value = item.lower()
                if not hash_value:
                    results[item] = None
                    continue
                hash_value = hash_value.lower()

                # Check if already in seedbox — never remove pre-existing torrents
                existing = self._find_by_hash(hash_value)
                if existing:
                    torrent_was_preexisting = True
                    torrent_id = str(existing.get('id', ''))
                    self._all_torrent_ids[hash_value] = torrent_id
                    is_complete = self._is_complete(existing)
                    if is_complete:
                        self._cached_torrent_ids[hash_value] = torrent_id
                    results[hash_value] = is_complete
                    continue

                # Add raw hash — DL only creates the entry if cached.
                # async must be False (default) so we get the real downloadPercent/downloaded
                # status back immediately rather than a pending 0% result.
                add_result = make_request(
                    'POST', '/seedbox/add', self.api_key,
                    json_data={'url': hash_value},
                )
                value = add_result.get('value', {}) if isinstance(add_result, dict) else {}
                torrent_id = str(value.get('id', '')) if value.get('id') else None

                if not torrent_id:
                    # DL rejected the hash — not cached
                    results[hash_value] = False
                    continue

                self._all_torrent_ids[hash_value] = torrent_id

                # Check for error status
                if self._is_error(value):
                    logger.warning(f"Debrid-Link cache check: error status for {hash_value}")
                    try:
                        add_to_not_wanted(hash_value)
                        self.remove_torrent(torrent_id, "Error status during cache check")
                    except Exception:
                        pass
                    results[hash_value] = None
                    continue

                # Check for video files when available
                files = value.get('files') or []
                if files:
                    video_files = [f for f in files if is_video_file(f.get('name', '') or f.get('path', ''))]
                    if not video_files:
                        logger.warning(f"Debrid-Link cache check: no video files for {hash_value}")
                        try:
                            add_to_not_wanted(hash_value)
                            self.remove_torrent(torrent_id, "No video files found during cache check")
                        except Exception:
                            pass
                        results[hash_value] = None
                        continue

                is_complete = self._is_complete(value)

                if is_complete:
                    results[hash_value] = True
                    logger.info(f"Debrid-Link cache check: CACHED {hash_value}")
                    # Remove the probe torrent — the real add_torrent call happens later
                    # if the scraper decides to use this result. Keeping it here causes duplicates.
                    try:
                        self.remove_torrent(torrent_id, "Cached probe — removed after cache check")
                    except Exception as e:
                        logger.error(f"Debrid-Link cache check: error removing cached probe {torrent_id}: {e}")
                else:
                    results[hash_value] = False
                    logger.info(f"Debrid-Link cache check: NOT CACHED {hash_value}")
                    if remove_uncached:
                        try:
                            self.remove_torrent(torrent_id, "Not cached — removed after cache check")
                            from database.torrent_tracking import update_cache_check_removal
                            update_cache_check_removal(hash_value)
                        except Exception as e:
                            logger.error(f"Debrid-Link cache check: error removing uncached {torrent_id}: {e}")

            except Exception as e:
                logger.error(f"Debrid-Link is_cached error for {item}: {e}")
                if torrent_id and not torrent_was_preexisting:
                    try:
                        self.remove_torrent(torrent_id, f"Exception during cache check: {e}")
                    except Exception:
                        pass
                results[hash_value if 'hash_value' in dir() else item] = None

        if return_single:
            return next(iter(results.values())) if results else None
        return results

    # ── Add torrent ─────────────────────────────────────────────────────────

    def add_torrent(
        self,
        magnet_link: Optional[str] = None,
        temp_file_path: Optional[str] = None,
    ) -> Optional[str]:
        """
        POST /seedbox/add
        Supports magnet link, torrent URL, info hash, or .torrent file.
        Returns the torrent ID string.
        """
        try:
            # Extract hash for de-dup check
            hash_value = None
            if magnet_link:
                hash_value = extract_hash_from_magnet(magnet_link)
                if not hash_value and len(magnet_link) == 40:
                    hash_value = magnet_link.lower()  # raw hash
            if hash_value:
                hash_value = hash_value.lower()
                existing = self._find_by_hash(hash_value)
                if existing:
                    tid = str(existing.get('id', ''))
                    self._all_torrent_ids[hash_value]    = tid
                    self._cached_torrent_ids[hash_value] = tid
                    return tid

            if temp_file_path:
                with open(temp_file_path, 'rb') as f:
                    result = make_request(
                        'POST', '/seedbox/add', self.api_key,
                        files={'file': (os.path.basename(temp_file_path), f)},
                        params={'async': 'true'},
                    )
            elif magnet_link:
                result = make_request(
                    'POST', '/seedbox/add', self.api_key,
                    json_data={'url': magnet_link, 'async': True},
                )
            else:
                raise TorrentAdditionError("No magnet link or torrent file provided")

            value = result.get('value', {}) if isinstance(result, dict) else {}
            torrent_id = str(value.get('id', ''))
            if not torrent_id:
                raise TorrentAdditionError(f"Debrid-Link returned no torrent ID: {result}")

            if hash_value:
                self._all_torrent_ids[hash_value] = torrent_id

            logger.info(f"Debrid-Link torrent added: id={torrent_id} name={value.get('name','')}")

            # Enrich tracker record after addition
            try:
                info = self._normalize(value)
                files = info.get('files', [])
                video_files = [f for f in files if is_video_file(f.get('path', ''))]
                if hash_value and (files or value.get('name')):
                    from database.torrent_tracking import update_torrent_tracking, record_torrent_addition
                    updated_item_data = {
                        'filled_by_title':   info.get('filename', ''),
                        'debrid_folder_name': info.get('filename', ''),
                    }
                    if video_files:
                        updated_item_data['filled_by_file'] = video_files[0].get('path', '')
                    update_torrent_tracking(
                        torrent_hash=hash_value,
                        item_data=updated_item_data,
                        trigger_details={
                            'source': 'debridlink',
                            'status': info.get('status', ''),
                            'selected_files': [
                                {'path': f['path'], 'bytes': f['bytes'], 'selected': True}
                                for f in video_files
                            ],
                            'user_initiated': True,
                        },
                        additional_metadata={
                            'debrid_info': {
                                'provider': 'debridlink',
                                'torrent_id': torrent_id,
                                'status': info.get('status', ''),
                                'filename': info.get('filename', ''),
                                'original_filename': info.get('original_filename', ''),
                            }
                        },
                    )
            except Exception as track_err:
                logger.debug(f"Debrid-Link tracking enrichment skipped: {track_err}")

            return torrent_id

        except TorrentAdditionError:
            raise
        except Exception as e:
            logger.error(f"Debrid-Link add_torrent error: {e}")
            raise TorrentAdditionError(f"Failed to add torrent to Debrid-Link: {e}")

    # ── Torrent info ─────────────────────────────────────────────────────────

    def get_torrent_info(self, torrent_id: str) -> Optional[Dict]:
        """GET /seedbox/list?ids={torrent_id}"""
        try:
            result = make_request('GET', '/seedbox/list', self.api_key,
                                  params={'ids': torrent_id})
            value = result.get('value', []) if isinstance(result, dict) else []
            if not isinstance(value, list) or not value:
                return None
            t = value[0]
            info = self._normalize(t)
            h = info.get('hash', '')
            if h:
                self._all_torrent_ids[h] = torrent_id
                if self._is_complete(t):
                    self._cached_torrent_ids[h] = torrent_id
            return info
        except Exception as e:
            logger.error(f"Debrid-Link get_torrent_info error for {torrent_id}: {e}")
            return None

    def get_torrent_info_with_status(self, torrent_id: str) -> TorrentInfoStatus:
        try:
            info = self.get_torrent_info(torrent_id)
            if info is None:
                return TorrentInfoStatus(status=TorrentFetchStatus.NOT_FOUND)
            return TorrentInfoStatus(status=TorrentFetchStatus.OK, data=info)
        except Exception as e:
            return TorrentInfoStatus(status=TorrentFetchStatus.UNKNOWN_ERROR, message=str(e))

    # ── Remove ───────────────────────────────────────────────────────────────

    def remove_torrent(self, torrent_id: str, removal_reason: Optional[str] = None) -> bool:
        try:
            make_request('DELETE', f'/seedbox/{torrent_id}/remove', self.api_key)
            # Clean internal caches
            self._cached_torrent_ids = {h: tid for h, tid in self._cached_torrent_ids.items()
                                        if tid != torrent_id}
            self._all_torrent_ids    = {h: tid for h, tid in self._all_torrent_ids.items()
                                        if tid != torrent_id}
            logger.info(f"Debrid-Link torrent {torrent_id} deleted ({removal_reason})")
            return True
        except Exception as e:
            logger.error(f"Debrid-Link remove_torrent error for {torrent_id}: {e}")
            return False

    # ── List ─────────────────────────────────────────────────────────────────

    def list_active_torrents(self) -> List[Dict]:
        try:
            result = []
            for t in get_all_torrents(self.api_key):
                info = self._normalize(t)
                result.append({
                    'id':                info['id'],
                    'filename':          info['filename'],
                    'original_filename': info['original_filename'],
                    'hash':              info['hash'],
                    'status':            info['status'],
                    'progress':          info['progress'],
                    'bytes':             info['bytes'],
                    'added':             info['added'],
                    'message':           info['message'],
                    'debrid_folder_name': info['debrid_folder_name'],
                })
            return result
        except Exception as e:
            logger.error(f"Debrid-Link list_active_torrents error: {e}")
            return []

    # ── Downloads ────────────────────────────────────────────────────────────

    @timed_lru_cache(seconds=2)
    def get_active_downloads(self) -> Tuple[int, int]:
        try:
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return 0, 0
            torrents = get_all_torrents(self.api_key)
            active = sum(1 for t in torrents
                         if not self._is_complete(t) and not self._is_error(t))
            return active, self.MAX_DOWNLOADS
        except Exception as e:
            raise ProviderUnavailableError(f"Failed to get Debrid-Link active downloads: {e}")

    # ── Subscription ─────────────────────────────────────────────────────────

    def get_subscription_status(self) -> Dict:
        try:
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return {'days_remaining': None, 'expiration': None, 'premium': False,
                        'username': '', 'email': '', 'points': 0, 'locale': '', 'type': 'free'}

            result = make_request('GET', '/account/infos', self.api_key)
            u = result.get('value', {}) if isinstance(result, dict) else {}

            premium_left = u.get('premiumLeft', 0) or 0
            # premiumLeft is undocumented unit — treat non-zero as premium active
            # Attempt to parse as seconds if large, else as days
            premium = bool(premium_left)
            expiration = None
            days_remaining = None
            if premium_left > 0:
                try:
                    if premium_left > 86400:  # looks like seconds
                        days_remaining = premium_left // 86400
                        exp_ts = time.time() + premium_left
                    else:
                        days_remaining = int(premium_left)
                        exp_ts = time.time() + premium_left * 86400
                    expiration = datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime('%Y-%m-%d')
                except Exception:
                    pass

            account_type = u.get('accountType', 0)
            type_label = 'Premium' if account_type else 'Free'

            return {
                'days_remaining': days_remaining,
                'expiration':     expiration,
                'premium':        premium,
                'username':       u.get('username', ''),
                'email':          u.get('email', ''),
                'points':         u.get('pts', 0),
                'locale':         '',
                'type':           type_label,
                'register_date':  u.get('registerDate', ''),
                'traffic_share':  u.get('trafficshare', 0),
            }
        except Exception as e:
            logger.error(f"Debrid-Link get_subscription_status error: {e}")
            return {'error': str(e), 'days_remaining': None, 'expiration': None,
                    'premium': None, 'username': '', 'email': '', 'points': 0,
                    'locale': '', 'type': ''}

    # ── Traffic / Limits ────────────────────────────────────────────────────

    def get_user_traffic(self) -> Dict:
        """
        Fetch /seedbox/limits for quota/usage stats and /seedbox/activity for
        live transfer rates. Returns a flat dict consumed by the usage tab route.

        /seedbox/limits response uses {current, value} pairs per quota type.
        Field names confirmed: usagePercent, nextResetSeconds, dayCount.
        Other fields inferred from UI labels — exact names resolved at runtime.
        """
        try:
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return {'downloaded': 0, 'limit': None}

            result = {}

            # Limits / quotas
            try:
                limits_resp = make_request('GET', '/seedbox/limits', self.api_key)
                limits = limits_resp.get('value', {}) if isinstance(limits_resp, dict) else {}
                result['limits'] = limits
            except Exception as e:
                logger.debug(f"Debrid-Link /seedbox/limits failed: {e}")
                limits = {}

            # Activity (live speeds + peers) — returns {torrent_id: {downloadSpeed, uploadSpeed, peersConnected, ...}}
            try:
                activity_resp = make_request('GET', '/seedbox/activity', self.api_key)
                activity = activity_resp.get('value', {}) if isinstance(activity_resp, dict) else {}
                total_dl  = sum(int(v.get('downloadSpeed', 0) or 0) for v in activity.values() if isinstance(v, dict))
                total_ul  = sum(int(v.get('uploadSpeed',   0) or 0) for v in activity.values() if isinstance(v, dict))
                total_peers = sum(int(v.get('peersConnected', 0) or 0) for v in activity.values() if isinstance(v, dict))
                result['download_speed'] = total_dl
                result['upload_speed']   = total_ul
                result['peers_connected'] = total_peers
            except Exception as e:
                logger.debug(f"Debrid-Link /seedbox/activity failed: {e}")

            result['downloaded'] = 0
            result['limit'] = None
            return result

        except Exception as e:
            logger.error(f"Debrid-Link get_user_traffic error: {e}")
            return {'downloaded': 0, 'limit': None}

    # ── Cache ID lookup ──────────────────────────────────────────────────────

    def get_cached_torrent_id(self, hash_value: str) -> Optional[str]:
        return self._cached_torrent_ids.get((hash_value or '').lower())
