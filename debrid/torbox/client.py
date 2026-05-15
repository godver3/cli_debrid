import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import bencodepy

from ..base import DebridProvider, ProviderUnavailableError, TooManyDownloadsError, TorrentAdditionError
from ..common import extract_hash_from_magnet, is_unwanted_file, is_video_file, timed_lru_cache
from ..status import TorrentFetchStatus, TorrentInfoStatus, TorrentStatus
from .api import get_all_torrents, get_api_key, get_data_payload, make_request
from .exceptions import TorboxAPIError, TorboxAuthError
from database.not_wanted_magnets import add_to_not_wanted
from utilities.phalanx_db_cache_manager import PhalanxDBClassManager
from utilities.settings import get_setting


class TorboxProvider(DebridProvider):
    """Torbox implementation of the DebridProvider interface."""

    PROVIDER_NAME = "Torbox"
    API_BASE_URL = "https://api.torbox.app/v1/api"
    MAX_DOWNLOADS = 9999

    def __init__(self):
        super().__init__()
        self._cached_torrent_ids = {}
        self._cached_torrent_titles = {}
        self._all_torrent_ids = {}
        self.phalanx_enabled = get_setting('UI Settings', 'enable_phalanx_db', default=False)
        self.phalanx_cache = PhalanxDBClassManager() if self.phalanx_enabled else None

    def _load_api_key(self) -> str:
        if getattr(self, '_api_key', None):
            return self._api_key
        try:
            return get_api_key()
        except Exception as e:
            logging.error(f"Failed to load Torbox API key: {str(e)}", exc_info=True)
            raise ProviderUnavailableError(f"Failed to load API key: {str(e)}")

    @property
    def api_key(self) -> str:
        return self._load_api_key()

    def check_connectivity(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        try:
            make_request('GET', '/user/me', self.api_key)
            return True, None
        except TorboxAuthError as e:
            return False, {"service": "Debrid Provider API", "type": "AUTH_ERROR", "status_code": None, "message": str(e)}
        except (ProviderUnavailableError, TorboxAPIError) as e:
            return False, {"service": "Debrid Provider API", "type": "CONNECTION_ERROR", "status_code": None, "message": str(e)}
        except Exception as e:
            return False, {"service": "Debrid Provider API", "type": "CONNECTION_ERROR", "status_code": None, "message": str(e)}

    def _extract_hash(self, magnet_or_hash: Optional[str], temp_file_path: Optional[str] = None) -> Optional[str]:
        hash_value = None
        if temp_file_path:
            try:
                with open(temp_file_path, 'rb') as f:
                    torrent_data = bencodepy.decode(f.read())
                    info = torrent_data[b'info']
                    hash_value = hashlib.sha1(bencodepy.encode(info)).hexdigest().lower()
            except Exception as e:
                logging.error(f"Could not extract hash from torrent file: {str(e)}")
        elif magnet_or_hash:
            if magnet_or_hash.startswith('magnet:'):
                hash_value = extract_hash_from_magnet(magnet_or_hash)
            elif len(magnet_or_hash) == 40 and all(c in '0123456789abcdefABCDEF' for c in magnet_or_hash):
                hash_value = magnet_or_hash.lower()
        return hash_value.lower() if hash_value else None

    def _normalize_files(self, info: Dict[str, Any]) -> List[Dict[str, Any]]:
        files = []
        for file_info in info.get('files', []) or []:
            name = file_info.get('name') or file_info.get('short_name') or ''
            files.append({
                'id': file_info.get('id'),
                'path': name,
                'name': os.path.basename(name) if name else '',
                'bytes': file_info.get('size', 0),
                'selected': True,
            })
        return files

    def _map_status(self, info: Dict[str, Any]) -> TorrentStatus:
        state = (info.get('download_state') or '').lower()
        if state in {'cached', 'completed', 'downloaded'} or info.get('cached') or info.get('download_finished'):
            return TorrentStatus.CACHED
        if state in {'downloading', 'queued', 'meta_download', 'metadata', 'checking'} or info.get('active'):
            return TorrentStatus.DOWNLOADING
        if state in {'error', 'failed'}:
            return TorrentStatus.ERROR
        return TorrentStatus.UNKNOWN

    def _normalize_torrent_info(self, info: Dict[str, Any]) -> Dict[str, Any]:
        status = self._map_status(info)
        progress_fraction = info.get('progress', 0) or 0
        if progress_fraction <= 1:
            progress = progress_fraction * 100
        else:
            progress = progress_fraction

        normalized = dict(info)
        normalized['id'] = str(info.get('id', ''))
        normalized['hash'] = (info.get('hash') or '').lower()
        normalized['filename'] = info.get('name', '')
        normalized['debrid_folder_name'] = info.get('name', '') or info.get('download_path', '')
        normalized['files'] = self._normalize_files(info)
        normalized['bytes'] = info.get('size', 0)
        normalized['progress'] = progress
        if status == TorrentStatus.CACHED:
            normalized['status'] = 'downloaded'
        elif status == TorrentStatus.DOWNLOADING:
            normalized['status'] = 'downloading'
        elif status == TorrentStatus.ERROR:
            normalized['status'] = 'error'
        else:
            normalized['status'] = (info.get('download_state') or 'unknown').lower()
        return normalized

    def _find_existing_torrent(self, hash_value: str) -> Optional[Dict[str, Any]]:
        for torrent in get_all_torrents(self.api_key):
            if (torrent.get('hash') or '').lower() == hash_value.lower():
                return torrent
        return None

    async def is_cached(
        self,
        magnet_links: Union[str, List[str]],
        temp_file_path: Optional[str] = None,
        result_title: Optional[str] = None,
        result_index: Optional[str] = None,
        remove_uncached: bool = True,
        remove_cached: bool = False,
        skip_phalanx_db: bool = False,
        imdb_id: Optional[str] = None
    ) -> Union[bool, Dict[str, bool], None]:
        if isinstance(magnet_links, str):
            items = [magnet_links]
            return_single = True
        else:
            items = magnet_links
            return_single = False

        hashes = []
        results: Dict[str, Optional[bool]] = {}
        phalanx_misses: List[str] = []

        for magnet_link in items:
            hash_value = self._extract_hash(magnet_link, temp_file_path=temp_file_path)
            if not hash_value:
                try:
                    add_to_not_wanted(magnet_link)
                except Exception:
                    pass
                results[magnet_link] = None
                continue

            if not skip_phalanx_db and self.phalanx_enabled and self.phalanx_cache:
                try:
                    phalanx_result = self.phalanx_cache.get_cache_status(hash_value)
                    if phalanx_result is not None:
                        results[hash_value] = bool(phalanx_result.get('is_cached'))
                        continue
                except Exception as e:
                    logging.error(f"Error checking PhalanxDB cache for {hash_value}: {str(e)}")

            hashes.append(hash_value)
            phalanx_misses.append(hash_value)

        if hashes:
            try:
                params = [('hash', hash_value) for hash_value in hashes]
                response = make_request('GET', '/torrents/checkcached', self.api_key, params=params)
                cached_payload = get_data_payload(response) or {}
                for hash_value in hashes:
                    is_hit = hash_value in cached_payload
                    results[hash_value] = is_hit
                    if self.phalanx_enabled and self.phalanx_cache:
                        try:
                            self.phalanx_cache.update_cache_status(hash_value, is_hit)
                        except Exception as e:
                            logging.error(f"Failed to update PhalanxDB for {hash_value}: {str(e)}")
            except Exception as e:
                logging.error(f"Error checking Torbox cache status: {str(e)}")
                for hash_value in phalanx_misses:
                    results[hash_value] = None

        if return_single:
            return next(iter(results.values())) if results else None
        return results

    def get_subscription_status(self) -> Dict[str, Any]:
        try:
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return {'days_remaining': None, 'expiration': None, 'premium': False}

            result = make_request('GET', '/user/me', self.api_key)
            user_info = get_data_payload(result) or {}
            expiration = user_info.get('premium_expires_at')
            days_remaining = None
            if expiration:
                try:
                    exp_dt = datetime.fromisoformat(expiration.replace('Z', '+00:00')).replace(tzinfo=None)
                    days_remaining = max(0, (exp_dt - datetime.utcnow()).days)
                except Exception as e:
                    logging.warning(f"Failed parsing Torbox expiration '{expiration}': {e}")

            _PLAN_NAMES = {0: 'Free', 1: 'Essential', 2: 'Pro', 3: 'Standard'}
            plan_num = user_info.get('plan')
            plan_name = _PLAN_NAMES.get(plan_num, f'Plan {plan_num}') if plan_num is not None else 'Unknown'
            return {
                'days_remaining':        days_remaining,
                'expiration':            expiration,
                'premium':               bool(user_info.get('is_subscribed')),
                'username':              user_info.get('email', '').split('@')[0],
                'email':                 user_info.get('email', ''),
                'points':                None,   # Torbox has no points — route hides the row when None
                'locale':                '',
                'type':                  plan_name,
                'created_at':            user_info.get('created_at', ''),
                'total_bytes_downloaded': user_info.get('total_bytes_downloaded', 0),
                'cooldown_until':        user_info.get('cooldown_until', ''),
            }
        except Exception as e:
            logging.error(f"Error fetching Torbox subscription status: {str(e)}")
            return {'days_remaining': None, 'expiration': None, 'premium': None, 'error': str(e)}

    def add_torrent(self, magnet_link: Optional[str], temp_file_path: Optional[str] = None) -> Optional[str]:
        try:
            hash_value = self._extract_hash(magnet_link, temp_file_path=temp_file_path)
            if hash_value:
                existing = self._find_existing_torrent(hash_value)
                if existing:
                    torrent_id = str(existing.get('id', ''))
                    self._all_torrent_ids[hash_value] = torrent_id
                    self._cached_torrent_ids[hash_value] = torrent_id if existing.get('cached') or (existing.get('download_state') == 'cached') else self._cached_torrent_ids.get(hash_value)
                    self._cached_torrent_titles[hash_value] = existing.get('name', '')
                    return torrent_id

            if temp_file_path:
                with open(temp_file_path, 'rb') as f:
                    files = {'file': (os.path.basename(temp_file_path), f)}
                    result = make_request('POST', '/torrents/createtorrent', self.api_key, files=files)
            elif magnet_link and magnet_link.startswith('http'):
                # HTTP URL (e.g. Jackett/Prowlarr) — download torrent file or detect magnet redirect
                import tempfile as _tmp, requests as _req
                # Follow redirects manually to catch magnet: scheme redirects
                _session = _req.Session()
                _resp = _session.get(magnet_link, timeout=30, allow_redirects=False)
                # Follow redirects manually, stopping if we hit a magnet: URI
                _resolved_magnet = None
                while _resp.status_code in (301, 302, 303, 307, 308):
                    _location = _resp.headers.get('Location', '')
                    if _location.startswith('magnet:'):
                        _resolved_magnet = _location
                        break
                    _resp = _session.get(_location, timeout=30, allow_redirects=False)
                if _resolved_magnet:
                    logging.info(f"[Torbox] URL redirected to magnet link, using it directly")
                    result = make_request('POST', '/torrents/createtorrent', self.api_key, data={'magnet': _resolved_magnet})
                else:
                    _resp.raise_for_status()
                    content_type = _resp.headers.get('Content-Type', '')
                    logging.info(f"[Torbox] Downloaded URL content-type={content_type} size={len(_resp.content)} first_bytes={_resp.content[:20]!r}")
                    if _resp.content.strip().startswith(b'magnet:'):
                        _resolved_magnet = _resp.content.strip().decode('utf-8')
                        logging.info(f"[Torbox] URL body is magnet link, using it directly")
                        result = make_request('POST', '/torrents/createtorrent', self.api_key, data={'magnet': _resolved_magnet})
                    else:
                        with _tmp.NamedTemporaryFile(suffix='.torrent', delete=False) as _tf:
                            _tf.write(_resp.content)
                            _tmp_path = _tf.name
                        try:
                            with open(_tmp_path, 'rb') as f:
                                files = {'file': ('download.torrent', f)}
                                result = make_request('POST', '/torrents/createtorrent', self.api_key, files=files)
                        finally:
                            try:
                                os.remove(_tmp_path)
                            except Exception:
                                pass
            elif magnet_link:
                result = make_request('POST', '/torrents/createtorrent', self.api_key, data={'magnet': magnet_link})
            else:
                raise ValueError("Either magnet_link or temp_file_path must be provided")

            payload = get_data_payload(result) or {}
            torrent_id = payload.get('torrent_id') or payload.get('id')
            if not torrent_id and hash_value:
                existing = self._find_existing_torrent(hash_value)
                torrent_id = existing.get('id') if existing else None
            # Torbox may queue the torrent — queued means accepted, poll mylist briefly
            if not torrent_id and payload.get('queued_id'):
                queued_hash = payload.get('hash') or hash_value
                for _ in range(15):
                    time.sleep(2)
                    existing = self._find_existing_torrent(queued_hash) if queued_hash else None
                    if existing:
                        torrent_id = existing.get('id')
                        break
                if not torrent_id:
                    # Queued but not yet in mylist — treat queued_id as the handle
                    torrent_id = f"queued:{payload['queued_id']}"
                    logging.info(f"Torbox torrent queued (queued_id={payload['queued_id']}), using queued handle")

            if not torrent_id:
                raise TorrentAdditionError(f"Failed to add torrent - response: {result}")

            torrent_id = str(torrent_id)

            # Queued handle — torrent accepted but not yet in mylist, return immediately
            if torrent_id.startswith('queued:'):
                if hash_value:
                    self._all_torrent_ids[hash_value] = torrent_id
                return torrent_id

            for _ in range(30):
                info = self.get_torrent_info(torrent_id)
                if info:
                    if hash_value:
                        self._all_torrent_ids[hash_value] = torrent_id
                        self._cached_torrent_titles[hash_value] = info.get('filename', '')
                        if info.get('status') == 'downloaded':
                            self._cached_torrent_ids[hash_value] = torrent_id
                    return torrent_id
                time.sleep(1)

            raise TorrentAdditionError("Timed out waiting for Torbox torrent to become available")
        except Exception as e:
            logging.error(f"Error adding torrent to Torbox: {str(e)}")
            raise

    @timed_lru_cache(seconds=1)
    def get_active_downloads(self) -> Tuple[int, int]:
        try:
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return 0, 0

            torrents = get_all_torrents(self.api_key)
            active_count = sum(1 for torrent in torrents if (torrent.get('download_state') or '').lower() not in {'cached', 'completed', 'downloaded'})
            return active_count, max(active_count + 10, 10)
        except TooManyDownloadsError:
            raise
        except Exception as e:
            logging.error(f"Error getting Torbox active downloads: {str(e)}", exc_info=True)
            raise ProviderUnavailableError(f"Failed to get active downloads: {str(e)}")

    def get_user_traffic(self) -> Dict[str, Any]:
        try:
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return {'downloaded': 0, 'limit': None}

            result = make_request('GET', '/user/me', self.api_key)
            user_info = get_data_payload(result) or {}
            downloaded_gb = round((user_info.get('total_bytes_downloaded', 0) or 0) / (1024 ** 3), 2)
            return {
                'downloaded': downloaded_gb,
                'limit': None,
                'total_bytes_downloaded': user_info.get('total_bytes_downloaded', 0),
            }
        except Exception as e:
            logging.error(f"Error getting Torbox traffic information: {str(e)}")
            raise ProviderUnavailableError(f"Failed to get user traffic: {str(e)}")

    def get_torrent_info(self, torrent_id: str) -> Optional[Dict[str, Any]]:
        try:
            result = make_request('GET', '/torrents/mylist', self.api_key, params={'bypass_cache': 'true', 'id': torrent_id})
            payload = get_data_payload(result)
            if isinstance(payload, list):
                if not payload:
                    return None
                info = payload[0]
            elif isinstance(payload, dict):
                info = payload
            else:
                return None

            normalized = self._normalize_torrent_info(info)
            hash_value = normalized.get('hash', '')
            status = self._map_status(info)
            self.update_status(torrent_id, status)

            if hash_value:
                self._all_torrent_ids[hash_value] = str(torrent_id)
                if status == TorrentStatus.CACHED:
                    self._cached_torrent_ids[hash_value] = str(torrent_id)
                    self._cached_torrent_titles[hash_value] = normalized.get('filename', '')

            return normalized
        except Exception as e:
            if "404" in str(e):
                self.update_status(torrent_id, TorrentStatus.REMOVED)
            else:
                logging.error(f"Error getting Torbox torrent info: {str(e)}")
                self.update_status(torrent_id, TorrentStatus.ERROR)
            return None

    def get_torrent_info_with_status(self, torrent_id: str) -> TorrentInfoStatus:
        try:
            info = self.get_torrent_info(torrent_id)
            if info:
                return TorrentInfoStatus(status=TorrentFetchStatus.OK, data=info, message=None, http_status_code=200)
            return TorrentInfoStatus(status=TorrentFetchStatus.NOT_FOUND, data=None, message="Torrent not found", http_status_code=404)
        except Exception as e:
            return TorrentInfoStatus(status=TorrentFetchStatus.UNKNOWN_ERROR, data=None, message=str(e), http_status_code=None)

    def get_cached_torrent_id(self, hash_value: str) -> Optional[str]:
        return self._cached_torrent_ids.get(hash_value)

    def get_cached_torrent_title(self, hash_value: str) -> Optional[str]:
        return self._cached_torrent_titles.get(hash_value)

    def list_active_torrents(self) -> List[Dict[str, Any]]:
        try:
            torrents = []
            for torrent in get_all_torrents(self.api_key):
                normalized = self._normalize_torrent_info(torrent)
                torrents.append({
                    'id': normalized.get('id', ''),
                    'filename': normalized.get('filename', ''),
                    'original_filename': normalized.get('filename', ''),
                    'hash': normalized.get('hash', ''),
                    'status': normalized.get('status', ''),
                    'progress': normalized.get('progress', 0),
                    'bytes': normalized.get('bytes', 0),
                    'added': normalized.get('created_at'),
                    'message': normalized.get('download_state', ''),
                })
            return torrents
        except Exception as e:
            logging.error(f"Error listing Torbox torrents: {str(e)}")
            return []

    def remove_torrent(self, torrent_id: str, removal_reason: Optional[str] = None) -> bool:
        hash_value = None
        try:
            # queued: handles are not yet in mylist — nothing to remove
            if str(torrent_id).startswith('queued:'):
                logging.debug(f"[Torbox] Skipping remove for queued handle {torrent_id}")
                return True

            info = self.get_torrent_info(torrent_id)
            if info:
                hash_value = info.get('hash', '').lower()

            make_request(
                'POST',
                '/torrents/controltorrent',
                self.api_key,
                json_data={'operation': 'delete', 'torrent_id': int(torrent_id)},
            )

            self.update_status(torrent_id, TorrentStatus.REMOVED)
            if hash_value:
                from database.torrent_tracking import mark_torrent_removed
                mark_torrent_removed(hash_value, removal_reason or "Manual removal")
                self._cached_torrent_ids.pop(hash_value, None)
                self._cached_torrent_titles.pop(hash_value, None)
                self._all_torrent_ids.pop(hash_value, None)
            return True
        except Exception as e:
            logging.error(f"Error removing Torbox torrent {torrent_id}: {str(e)}", exc_info=True)
            return False

    def cleanup(self) -> None:
        try:
            self.verify_torrent_presence()
        except Exception as e:
            logging.error(f"Error during Torbox cleanup: {str(e)}")
        finally:
            super().cleanup()

    def verify_torrent_presence(self, hash_value: str = None) -> bool:
        try:
            active_hashes = {
                (torrent.get('hash') or '').lower(): str(torrent.get('id'))
                for torrent in get_all_torrents(self.api_key)
                if torrent.get('hash')
            }
            from database.torrent_tracking import mark_torrent_removed

            if hash_value:
                if hash_value.lower() not in active_hashes:
                    mark_torrent_removed(hash_value, "Torrent no longer exists in Torbox")
                    return False
                return True

            success = True
            for tracked_hash in list(self._cached_torrent_ids.keys()):
                if tracked_hash.lower() not in active_hashes:
                    mark_torrent_removed(tracked_hash, "Torrent no longer exists in Torbox")
                    success = False
            return success
        except Exception as e:
            logging.error(f"Error verifying Torbox torrent presence: {str(e)}")
            return False

    def get_torrent_file_list(self, magnet_link: str) -> Optional[Tuple[List[Dict], str, str]]:
        torrent_id = None
        info = None
        try:
            torrent_id = self.add_torrent(magnet_link)
            if not torrent_id:
                return None

            # If queued, resolve to real torrent_id by polling via hash
            if str(torrent_id).startswith('queued:'):
                from ..common import extract_hash_from_magnet
                hash_value = extract_hash_from_magnet(magnet_link) if magnet_link.startswith('magnet:') else None
                resolved_id = None
                for _ in range(20):
                    time.sleep(3)
                    existing = self._find_existing_torrent(hash_value) if hash_value else None
                    if existing:
                        resolved_id = str(existing.get('id', ''))
                        break
                if not resolved_id:
                    logging.warning(f"[Torbox] Queued torrent never appeared in mylist for file listing")
                    return None
                torrent_id = resolved_id

            time.sleep(3)
            info = self.get_torrent_info(torrent_id)
            if not info:
                return None

            files = info.get('files', [])
            if isinstance(files, dict):
                files = list(files.values())
            elif not isinstance(files, list):
                files = []
            return files, info.get('filename', 'Unknown Filename'), torrent_id
        except Exception as e:
            logging.error(f"An unexpected error occurred during Torbox torrent file listing: {str(e)}")
            return None
        finally:
            if torrent_id:
                try:
                    self.remove_torrent(torrent_id, "Temporary add for file listing")
                except Exception as e:
                    logging.error(f"Error removing temporary Torbox torrent {torrent_id}: {str(e)}")
                    self.update_status(torrent_id, TorrentStatus.UNKNOWN)

    def is_cached_sync(
        self,
        magnet_link: str,
        temp_file_path: Optional[str] = None,
        result_title: Optional[str] = None,
        result_index: Optional[str] = None,
        remove_uncached: bool = True,
        remove_cached: bool = False,
        skip_phalanx_db: bool = False,
        imdb_id: Optional[str] = None
    ) -> Union[bool, Dict[str, bool], None]:
        try:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            return loop.run_until_complete(
                self.is_cached(
                    magnet_link,
                    temp_file_path,
                    result_title,
                    result_index,
                    remove_uncached,
                    remove_cached,
                    skip_phalanx_db,
                    imdb_id
                )
            )
        except Exception as e:
            logging.error(f"Error in Torbox is_cached_sync: {str(e)}")
            return None
