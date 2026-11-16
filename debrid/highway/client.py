"""HighWay implementation of the DebridProvider interface.

HighWay keeps torrents in a shared cache. Adding a known torrent links it to the
account at once (state "completed"); anything else waits in a queue with a 32-hex
queue id and gets a numeric id once it starts. Because the queue id disappears at
that point, ids handed out for queued torrents carry the info hash
("<queue id>-<hash>") so the torrent can still be found by hash.

Files are served over WebDAV (https://cloud.high-way.me/dav/TORRENT/<name>/),
so the mount for symlinks is an rclone WebDAV mount of that folder.
"""

import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import bencodepy

from ..base import DebridProvider, ProviderUnavailableError, TorrentAdditionError
from ..common import extract_hash_from_magnet
from ..status import TorrentFetchStatus, TorrentInfoStatus, TorrentStatus
from . import api as hw
from .exceptions import HighwayAPIError, HighwayAuthError

_QUEUE_ID = re.compile(r'^[a-f0-9]{32}$')


class HighwayProvider(DebridProvider):
    """HighWay implementation of the DebridProvider interface."""

    PROVIDER_NAME = "HighWay"

    def __init__(self):
        super().__init__()
        self._cached_torrent_ids: Dict[str, str] = {}
        self._cached_torrent_titles: Dict[str, str] = {}
        # hash -> transfer id of everything this provider added; read directly by
        # queues/torrent_processor.py, routes/scraper_routes.py and utilities/web_scraper.py
        self._all_torrent_ids: Dict[str, str] = {}

    def _load_api_key(self) -> str:
        if getattr(self, '_api_key', None):
            return self._api_key
        try:
            return hw.get_api_key()
        except Exception as e:
            raise ProviderUnavailableError(f"Failed to load API key: {str(e)}")

    @property
    def api_key(self) -> str:
        return self._load_api_key()

    def check_connectivity(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        try:
            hw.get_stats(self.api_key)
            return True, None
        except HighwayAuthError as e:
            return False, {"service": "Debrid Provider API", "type": "AUTH_ERROR", "status_code": None, "message": str(e)}
        except Exception as e:
            return False, {"service": "Debrid Provider API", "type": "CONNECTION_ERROR", "status_code": None, "message": str(e)}

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_hash(magnet_or_hash: Optional[str], temp_file_path: Optional[str] = None) -> Optional[str]:
        if temp_file_path:
            try:
                with open(temp_file_path, 'rb') as f:
                    info = bencodepy.decode(f.read())[b'info']
                return hashlib.sha1(bencodepy.encode(info)).hexdigest().lower()
            except Exception as e:
                logging.error(f"Could not extract hash from torrent file: {str(e)}")
                return None
        if not magnet_or_hash:
            return None
        if magnet_or_hash.startswith('magnet:'):
            value = extract_hash_from_magnet(magnet_or_hash)
            return value.lower() if value else None
        if re.fullmatch(r'[0-9a-fA-F]{40}', magnet_or_hash):
            return magnet_or_hash.lower()
        return None

    @staticmethod
    def _split_id(torrent_id: str) -> Tuple[str, Optional[str]]:
        """'<queue id>-<hash>' → (queue id, hash); numeric ids → (id, None)."""
        torrent_id = str(torrent_id)
        if '-' in torrent_id:
            queue_id, _, hash_value = torrent_id.partition('-')
            return queue_id, hash_value.lower() or None
        return torrent_id, None

    def _find_by_hash(self, hash_value: str) -> Optional[Dict[str, Any]]:
        for torrent in hw.get_all_torrents(self.api_key):
            if (torrent.get('hash') or '').lower() == hash_value.lower():
                return torrent
        return None

    @staticmethod
    def _normalize_files(info: Dict[str, Any]) -> List[Dict[str, Any]]:
        files = []
        for file_info in info.get('files') or []:
            path = file_info.get('path') or ''
            files.append({
                'id': file_info.get('id'),
                'index': file_info.get('index'),
                'path': path,
                'name': os.path.basename(path),
                'bytes': file_info.get('size', 0),
                'selected': True,
                'partial': bool(file_info.get('partial')),
                'download_url': file_info.get('download_url'),
            })
        return files

    @staticmethod
    def _map_status(state: str) -> TorrentStatus:
        state = (state or '').lower()
        if state == 'completed':
            return TorrentStatus.CACHED
        if state in ('queued', 'downloading'):
            return TorrentStatus.DOWNLOADING
        if state == 'error':
            return TorrentStatus.ERROR
        return TorrentStatus.UNKNOWN

    def _normalize_torrent_info(self, info: Dict[str, Any]) -> Dict[str, Any]:
        status = self._map_status(info.get('state'))
        normalized = dict(info)
        normalized['id'] = str(info.get('id', ''))
        normalized['hash'] = (info.get('hash') or '').lower()
        normalized['filename'] = info.get('name', '')
        normalized['original_filename'] = info.get('name', '')
        normalized['files'] = self._normalize_files(info)
        # The WebDAV folder (dav/TORRENT/<name>/) carries the transfer name.
        normalized['debrid_folder_name'] = info.get('name', '')
        normalized['bytes'] = info.get('size', 0)
        normalized['progress'] = info.get('progress', 0)
        normalized['status'] = {
            TorrentStatus.CACHED: 'downloaded',
            TorrentStatus.DOWNLOADING: 'downloading',
            TorrentStatus.ERROR: 'error',
        }.get(status, (info.get('state') or 'unknown').lower())
        return normalized

    def _remember(self, hash_value: Optional[str], torrent_id: str, info: Dict[str, Any]) -> None:
        if hash_value and info.get('state') == 'completed':
            self._cached_torrent_ids[hash_value] = torrent_id
            self._cached_torrent_titles[hash_value] = info.get('name', '')

    # ── DebridProvider ───────────────────────────────────────────────────────

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
    ) -> Union[bool, Dict[str, Optional[bool]], None]:
        """HighWay answers cache checks without adding anything, so nothing needs removing afterwards."""
        return_single = isinstance(magnet_links, str)
        items = [magnet_links] if return_single else list(magnet_links)

        results: Dict[str, Optional[bool]] = {}
        hashes: List[str] = []
        for item in items:
            hash_value = self._extract_hash(item, temp_file_path=temp_file_path)
            if not hash_value:
                results[item] = None
                continue
            hashes.append(hash_value)

        if hashes:
            try:
                states = hw.check_cached(self.api_key, hashes)
                for hash_value in hashes:
                    results[hash_value] = bool((states.get(hash_value) or {}).get('ready'))
            except Exception as e:
                logging.error(f"Error checking HighWay cache status: {str(e)}")
                for hash_value in hashes:
                    results[hash_value] = None

        if return_single:
            return next(iter(results.values())) if results else None
        return results

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
            return loop.run_until_complete(self.is_cached(magnet_link, temp_file_path, result_title, result_index,
                                                          remove_uncached, remove_cached, skip_phalanx_db, imdb_id))
        except Exception as e:
            logging.error(f"Error in HighWay is_cached_sync: {str(e)}")
            return None

    def add_torrent(self, magnet_link: Optional[str], temp_file_path: Optional[str] = None) -> Optional[str]:
        hash_value = self._extract_hash(magnet_link, temp_file_path=temp_file_path)
        try:
            if temp_file_path:
                with open(temp_file_path, 'rb') as f:
                    result = hw.add_torrent_file(self.api_key, os.path.basename(temp_file_path), f.read())
            elif magnet_link and magnet_link.startswith('http'):
                result = self._add_from_url(magnet_link)
                hash_value = hash_value or result.pop('_hash', None)
            elif magnet_link:
                magnet = magnet_link if magnet_link.startswith('magnet:') else f"magnet:?xt=urn:btih:{magnet_link}"
                result = hw.add_magnet(self.api_key, magnet)
            else:
                raise ValueError("Either magnet_link or temp_file_path must be provided")
        except (HighwayAuthError, ProviderUnavailableError):
            raise
        except HighwayAPIError as e:
            raise TorrentAdditionError(str(e))

        torrent_id = str(result.get('id') or '')
        if not torrent_id:
            raise TorrentAdditionError(f"Failed to add torrent - response: {result}")
        if _QUEUE_ID.match(torrent_id) and hash_value:
            torrent_id = f"{torrent_id}-{hash_value}"
        if hash_value:
            self._all_torrent_ids[hash_value] = torrent_id
        self._remember(hash_value, torrent_id, {'state': result.get('state'), 'name': ''})
        return torrent_id

    def _add_from_url(self, url: str) -> Dict[str, Any]:
        """Indexer links (Jackett/Prowlarr) lead to a magnet redirect or a .torrent file."""
        session = hw.api
        response = session.get(url, timeout=30, allow_redirects=False)
        for _ in range(5):
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get('Location', '')
            if location.startswith('magnet:'):
                return hw.add_magnet(self.api_key, location)
            response = session.get(location, timeout=30, allow_redirects=False)
        response.raise_for_status()
        content = response.content
        if content.strip().startswith(b'magnet:'):
            return hw.add_magnet(self.api_key, content.strip().decode('utf-8'))
        result = hw.add_torrent_file(self.api_key, 'download.torrent', content)
        try:
            info = bencodepy.decode(content)[b'info']
            result['_hash'] = hashlib.sha1(bencodepy.encode(info)).hexdigest().lower()
        except Exception:
            pass
        return result

    def get_torrent_info(self, torrent_id: str) -> Optional[Dict[str, Any]]:
        status = self.get_torrent_info_with_status(torrent_id)
        return status.data if status.status == TorrentFetchStatus.OK else None

    def get_torrent_info_with_status(self, torrent_id: str) -> TorrentInfoStatus:
        transfer_id, hash_value = self._split_id(torrent_id)
        try:
            try:
                info = hw.get_torrent(self.api_key, transfer_id)
            except HighwayAPIError as e:
                # A queued torrent got its numeric id: find it by hash.
                if ' 404 ' not in f" {e} " or not hash_value:
                    raise
                info = self._find_by_hash(hash_value)
                if info is None:
                    self.update_status(torrent_id, TorrentStatus.REMOVED)
                    return TorrentInfoStatus(status=TorrentFetchStatus.NOT_FOUND, message="Torrent not found",
                                             http_status_code=404)
                info = hw.get_torrent(self.api_key, str(info['id']))
        except HighwayAPIError as e:
            if ' 404 ' in f" {e} ":
                self.update_status(torrent_id, TorrentStatus.REMOVED)
                return TorrentInfoStatus(status=TorrentFetchStatus.NOT_FOUND, message="Torrent not found",
                                         http_status_code=404)
            return TorrentInfoStatus(status=TorrentFetchStatus.CLIENT_ERROR, message=str(e))
        except Exception as e:
            return TorrentInfoStatus(status=TorrentFetchStatus.UNKNOWN_ERROR, message=str(e))

        normalized = self._normalize_torrent_info(info)
        self.update_status(torrent_id, self._map_status(info.get('state')))
        self._remember(normalized['hash'] or hash_value, normalized['id'], info)
        return TorrentInfoStatus(status=TorrentFetchStatus.OK, data=normalized, http_status_code=200)

    def get_subscription_status(self) -> Dict[str, Any]:
        try:
            stats = hw.get_stats(self.api_key)
        except Exception as e:
            logging.error(f"Error fetching HighWay subscription status: {str(e)}")
            return {'days_remaining': None, 'expiration': None, 'premium': None, 'error': str(e)}
        expires = stats.get('plan_expires_at')
        expiration = None
        days_remaining = None
        if expires:
            expiry = datetime.fromtimestamp(int(expires), tz=timezone.utc)
            expiration = expiry.isoformat()
            days_remaining = max(0, (expiry - datetime.now(timezone.utc)).days)
        return {
            'days_remaining': days_remaining,
            'expiration': expiration,
            'premium': bool(stats.get('premium')),
            'username': stats.get('username', ''),
            'email': '',
            'points': None,
            'locale': '',
            'type': stats.get('plan') or stats.get('kind') or '',
        }

    def get_active_downloads(self) -> Tuple[int, int]:
        """Transfer slots. A full account queues new torrents instead of rejecting them."""
        try:
            slots = hw.get_stats(self.api_key).get('transfers') or {}
            return int(slots.get('active', 0)), int(slots.get('limit', 0))
        except Exception as e:
            raise ProviderUnavailableError(f"Failed to get active downloads: {str(e)}")

    def get_user_traffic(self) -> Dict[str, Any]:
        # Torrents use transfer slots, not a traffic volume.
        return {'downloaded': 0, 'limit': None}

    def get_cached_torrent_id(self, hash_value: str) -> Optional[str]:
        return self._cached_torrent_ids.get(hash_value)

    def get_cached_torrent_title(self, hash_value: str) -> Optional[str]:
        return self._cached_torrent_titles.get(hash_value)

    def list_active_torrents(self) -> List[Dict[str, Any]]:
        try:
            torrents = []
            for torrent in hw.get_all_torrents(self.api_key):
                normalized = self._normalize_torrent_info(torrent)
                added = torrent.get('added_at')
                torrents.append({
                    'id': normalized['id'],
                    'filename': normalized['filename'],
                    'original_filename': normalized['filename'],
                    'hash': normalized['hash'],
                    'status': normalized['status'],
                    'progress': normalized['progress'],
                    'bytes': normalized['bytes'],
                    'added': datetime.fromtimestamp(int(added), tz=timezone.utc).isoformat() if added else None,
                    'message': torrent.get('state', ''),
                })
            return torrents
        except Exception as e:
            logging.error(f"Error listing HighWay torrents: {str(e)}")
            return []

    def remove_torrent(self, torrent_id: str, removal_reason: Optional[str] = None) -> bool:
        transfer_id, hash_value = self._split_id(torrent_id)
        try:
            result = hw.delete_torrent(self.api_key, transfer_id)
            if not result.get('removed') and result.get('reason') == 'not_found' and hash_value:
                existing = self._find_by_hash(hash_value)
                if existing:
                    result = hw.delete_torrent(self.api_key, str(existing['id']))
            if not result.get('removed'):
                if result.get('reason') == 'not_owner':
                    logging.warning(f"HighWay token may not remove torrent {torrent_id} (added with another token)")
                    return False
                if result.get('reason') != 'not_found':
                    return False
            self.update_status(torrent_id, TorrentStatus.REMOVED)
            for known_hash, known_id in list(self._all_torrent_ids.items()):
                if known_id == str(torrent_id) or known_hash == hash_value:
                    self._all_torrent_ids.pop(known_hash, None)
            if hash_value:
                self._cached_torrent_ids.pop(hash_value, None)
                self._cached_torrent_titles.pop(hash_value, None)
            return True
        except Exception as e:
            logging.error(f"Error removing HighWay torrent {torrent_id}: {str(e)}")
            return False

    def get_torrent_file_list(self, magnet_link: str) -> Optional[Tuple[List[Dict], str, str]]:
        """Files of a cached torrent: temporary add (instant for cached content), read, remove again.
        A torrent the account already has is read in place and never removed."""
        torrent_id = None
        try:
            hash_value = self._extract_hash(magnet_link)
            existing_id = None
            if hash_value:
                existing_id = (hw.check_cached(self.api_key, [hash_value]).get(hash_value) or {}).get('id')
            if existing_id:
                info = self.get_torrent_info(str(existing_id))
                if not info or info.get('status') != 'downloaded':
                    return None
                return info.get('files', []), info.get('filename', 'Unknown Filename'), str(existing_id)
            torrent_id = self.add_torrent(magnet_link)
            if not torrent_id:
                return None
            info = self.get_torrent_info(torrent_id)
            if not info or info.get('status') != 'downloaded':
                return None
            return info.get('files', []), info.get('filename', 'Unknown Filename'), torrent_id
        except Exception as e:
            logging.error(f"Error during HighWay torrent file listing: {str(e)}")
            return None
        finally:
            if torrent_id:
                self.remove_torrent(torrent_id, "Temporary add for file listing")
