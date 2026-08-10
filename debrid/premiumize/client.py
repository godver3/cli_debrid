"""
Premiumize provider — correct endpoint usage per API docs:
  https://app.swaggerhub.com/apis-docs/premiumize.me/api/1.7.2

Endpoint summary:
  POST /transfer/create    src=magnet|torrent  → {status, id, name, type}
  POST /transfer/directdl  src=magnet          → {status, location, filename, filesize, content[{path,size,link,stream_link}]}
  GET  /transfer/list                          → {status, transfers[{id,name,message,status,progress,src,folder_id,file_id}]}
  POST /transfer/delete    id                  → {}
  POST /transfer/clearfinished                 → {}
  GET  /folder/list        id                  → {status, content[item{id,name,type,size,created_at,mime_type,link,stream_link}]}
  GET  /item/listall                           → {status, files[{id,name,created_at,size,mime_type,virus_scan,path}]}
  GET  /item/details       id                  → {id,name,type,size,created_at,folder_id,acodec,vcodec,link,mime_type,
                                                   opensubtitles_hash,resx,resy,duration,transcode_status,stream_link,
                                                   audio_track_names,bitrate}
  GET  /cache/check        items[]=hash        → {status, response[], transcoded[], filename[], filesize[]}
  GET  /account/info                           → {status, customer_id, premium_until, limit_used, space_used}

Key design notes:
  - /transfer/directdl  = instant availability check + file list in ONE call
  - /transfer/list      has no size/hash fields — enrich via /item/listall join
  - /item/listall       files joined by transfer.file_id or folder items by transfer.folder_id
  - /folder/list        content uses 'item' schema with full metadata incl. created_at
  - Hash only available from src magnet URL, never returned directly
"""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union, Any

from ..base import (
    DebridProvider, TooManyDownloadsError, ProviderUnavailableError,
    TorrentAdditionError, RateLimitError
)
from ..common import extract_hash_from_magnet, timed_lru_cache, is_video_file, is_unwanted_file
from ..status import TorrentStatus, TorrentInfoStatus, TorrentFetchStatus
from .api import make_request, get_api_key, TRANSFER_STATUS_MAP
from .exceptions import PremiumizeAuthError, PremiumizeAPIError
from utilities.settings import get_setting

logger = logging.getLogger(__name__)

# Transfer status → TorrentStatus mapping
_STATUS_MAP = {
    'queued':     TorrentStatus.QUEUED,
    'waiting':    TorrentStatus.QUEUED,
    'running':    TorrentStatus.DOWNLOADING,
    'finished':   TorrentStatus.DOWNLOADED,
    'seeding':    TorrentStatus.DOWNLOADED,
    'error':      TorrentStatus.ERROR,
    'deleted':    TorrentStatus.ERROR,
    'timeout':    TorrentStatus.ERROR,
}


def _to_status_str(raw: str) -> str:
    enum = _STATUS_MAP.get((raw or '').lower(), TorrentStatus.UNKNOWN)
    return str(enum.value) if hasattr(enum, 'value') else str(enum)


class PremiumizeProvider(DebridProvider):
    """
    Premiumize implementation of DebridProvider.
    Auth: ?apikey=X on every request.
    """

    PROVIDER_NAME = "Premiumize"
    API_BASE_URL  = "https://www.premiumize.me/api"
    MAX_DOWNLOADS = 25

    def __init__(self):
        super().__init__()
        self._cached_torrent_ids: Dict[str, str] = {}   # hash → transfer_id
        self._all_torrent_ids:    Dict[str, str] = {}   # hash → transfer_id (alias expected by torrent_processor)
        self._cached_torrent_titles: Dict[str, str] = {}  # hash → filename, from /cache/check

    # ── Auth ────────────────────────────────────────────────────────────────

    def _load_api_key(self) -> str:
        if getattr(self, '_api_key', None):
            return self._api_key
        try:
            return get_api_key()
        except Exception as e:
            raise ProviderUnavailableError(f"Failed to load Premiumize API key: {e}")

    @property
    def api_key(self) -> str:
        return self._load_api_key()

    def _req(self, method: str, endpoint: str, **kwargs) -> Any:
        return make_request(method, endpoint, self.api_key, **kwargs)

    # ── Subscription ────────────────────────────────────────────────────────

    def get_subscription_status(self) -> Dict[str, Any]:
        """GET /account/info → customer_id, premium_until (Unix int), limit_used, space_used"""
        try:
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return {'days_remaining': None, 'expiration': None, 'premium': False,
                        'username': '', 'email': '', 'points': 0, 'locale': '', 'type': 'free'}

            result = self._req('GET', '/account/info')
            logger.info(f"Premiumize /account/info: {result}")
            if not result or result.get('status') == 'error':
                return {'days_remaining': None, 'expiration': None, 'premium': False,
                        'username': '', 'email': '', 'points': 0, 'locale': '', 'type': 'free'}

            premium_until = result.get('premium_until')  # Unix integer
            days_remaining = None
            expiration     = None
            premium        = False

            logger.info(f"Premiumize premium_until raw: {premium_until!r} (type={type(premium_until).__name__})")

            if premium_until and premium_until != 0:
                try:
                    ts     = float(premium_until)
                    exp_dt = datetime.utcfromtimestamp(ts)
                    expiration = exp_dt.isoformat()
                    now    = datetime.utcnow()
                    delta  = exp_dt - now
                    days_remaining = max(0, delta.days)
                    premium = delta.total_seconds() > 0
                    logger.info(f"Premiumize premium_until={premium_until} → days={days_remaining}, premium={premium}")
                except Exception as e:
                    logger.warning(f"Failed to parse premium_until '{premium_until}': {e}")

            customer_id = str(result.get('customer_id', ''))
            limit_used  = result.get('limit_used', 0)
            space_used  = result.get('space_used', 0)

            return {
                'days_remaining': days_remaining,
                'expiration':     expiration,
                'premium':        premium,
                'username':       customer_id,
                'email':          '',
                'points':         float(limit_used or 0),
                'locale':         '',
                'type':           'premium' if premium else 'free',
                'space_used':     space_used,
                'limit_used':     limit_used,
            }
        except Exception as e:
            logger.error(f"Error fetching Premiumize subscription: {e}")
            return {'days_remaining': None, 'expiration': None, 'premium': None, 'error': str(e)}

    # ── Cache check ─────────────────────────────────────────────────────────

    async def is_cached(
        self,
        magnet_links: Union[str, List[str]],
        temp_file_path: Optional[str] = None,
        result_title: Optional[str] = None,
        result_index: Optional[str] = None,
        remove_uncached: bool = True,
        skip_phalanx_db: bool = False,
        imdb_id: Optional[str] = None,
        **kwargs
    ) -> Union[bool, Dict[str, Optional[bool]]]:
        """
        GET /cache/check?items[]=hash1&items[]=hash2
        Returns parallel arrays: response[], filename[], filesize[]
        """
        log_prefix = f"[{result_index}][{result_title}]" if result_title else ""

        if isinstance(magnet_links, str):
            magnet_links = [magnet_links]
            return_single = True
        else:
            return_single = False

        results: Dict[str, Optional[bool]] = {}
        hashes: List[str] = []

        for magnet_link in magnet_links:
            h = None
            if temp_file_path:
                try:
                    import bencodepy, hashlib as _hl
                    with open(temp_file_path, 'rb') as f:
                        td = bencodepy.decode(f.read())
                        h = _hl.sha1(bencodepy.encode(td[b'info'])).hexdigest()
                except Exception as e:
                    logger.error(f"{log_prefix} Cannot extract hash from torrent: {e}")
            elif magnet_link and magnet_link.startswith('magnet:'):
                h = extract_hash_from_magnet(magnet_link)
            elif magnet_link and len(magnet_link) == 40:
                h = magnet_link.lower()

            if not h:
                results[magnet_link or ''] = None
                continue

            h = h.lower()
            hashes.append(h)

        if not hashes:
            return (None if return_single else results)

        try:
            # /cache/check requires items[] as repeated params: [('items[]',h1),('items[]',h2)]
            from routes.api_tracker import api as _api
            param_list = [('items[]', h) for h in hashes]
            param_list.append(('apikey', self.api_key))
            raw = _api.get('https://www.premiumize.me/api/cache/check',
                           params=param_list, timeout=30)
            resp = raw.json() if raw.status_code == 200 else None
            logger.info(f"{log_prefix} Premiumize /cache/check response: {resp}")

            if not resp or resp.get('status') == 'error':
                for h in hashes:
                    results[h] = None
            else:
                cached_arr   = resp.get('response',  [False] * len(hashes))
                filename_arr = resp.get('filename',  ['']    * len(hashes))
                filesize_arr = resp.get('filesize',  [0]     * len(hashes))
                for i, h in enumerate(hashes):
                    is_hit = bool(cached_arr[i]) if i < len(cached_arr) else False
                    results[h] = is_hit
                    if is_hit:
                        _filename = filename_arr[i] if i < len(filename_arr) else ''
                        if _filename:
                            self._cached_torrent_titles[h.lower()] = _filename
                        logger.info(f"{log_prefix} CACHED: {h} → {_filename}")
                    else:
                        logger.debug(f"{log_prefix} NOT cached: {h}")
        except Exception as e:
            logger.error(f"{log_prefix} cache/check error: {e}")
            for h in hashes:
                results[h] = None

        if return_single:
            return results.get(hashes[0]) if hashes else None
        return results

    # ── Add torrent ─────────────────────────────────────────────────────────

    def add_torrent(
        self,
        magnet_link: Optional[str] = None,
        temp_file_path: Optional[str] = None
    ) -> Optional[str]:
        """
        POST /transfer/create src=magnet|file → {status, id, name, type}
        Polls until finished or 30s timeout.
        """
        try:
            if temp_file_path:
                with open(temp_file_path, 'rb') as f:
                    resp = self._req('POST', '/transfer/create', files={'file': f})
            elif magnet_link:
                resp = self._req('POST', '/transfer/create', data={'src': magnet_link})
            else:
                raise TorrentAdditionError("No magnet link or torrent file provided")

            if not resp or resp.get('status') == 'error':
                msg = resp.get('message', 'Unknown error') if resp else 'No response'
                raise TorrentAdditionError(f"Premiumize /transfer/create failed: {msg}")

            transfer_id = str(resp.get('id', ''))
            if not transfer_id:
                raise TorrentAdditionError("Premiumize returned no transfer ID")

            logger.info(f"Premiumize transfer created: id={transfer_id} name={resp.get('name','')}")

            # Store hash→id mapping (both dicts kept in sync)
            if magnet_link:
                h = extract_hash_from_magnet(magnet_link)
                if h:
                    self._cached_torrent_ids[h.lower()] = transfer_id
                    self._all_torrent_ids[h.lower()]    = transfer_id

            # Poll up to 30s for completion
            finished_transfer = None
            for attempt in range(30):
                transfers = self._list_transfers_raw()
                current = next((t for t in transfers if str(t.get('id', '')) == transfer_id), None)
                if not current:
                    time.sleep(1)
                    continue
                raw_status = (current.get('status') or '').lower()
                logger.debug(f"Premiumize transfer {transfer_id} status={raw_status} attempt={attempt+1}")
                if raw_status in ('error', 'deleted', 'timeout'):
                    raise TorrentAdditionError(
                        f"Premiumize transfer failed: {current.get('message') or raw_status}")
                if raw_status in ('finished', 'seeding'):
                    logger.info(f"Premiumize transfer {transfer_id} ready after {attempt+1}s")
                    finished_transfer = current
                    break
                time.sleep(1)
            else:
                logger.warning(f"Premiumize transfer {transfer_id} not finished after 30s — handing to checking queue")

            # Enrich torrent tracking record with debrid info + file list
            hash_for_tracking = None
            if magnet_link:
                h = extract_hash_from_magnet(magnet_link)
                if h:
                    hash_for_tracking = h.lower()
            if hash_for_tracking and finished_transfer:
                try:
                    info = self.get_torrent_info(transfer_id)
                    if info:
                        files = info.get('files', [])
                        selected_files = [
                            {'path': f.get('path') or f.get('name', ''), 'bytes': f.get('bytes', 0), 'selected': True}
                            for f in files
                        ]
                        from database.torrent_tracking import update_torrent_tracking, record_torrent_addition
                        updated_trigger = {
                            'source': 'premiumize',
                            'status': (finished_transfer.get('status') or '').lower(),
                            'selected_files': selected_files,
                            'user_initiated': True,
                        }
                        updated_metadata = {
                            'debrid_info': {
                                'provider': 'premiumize',
                                'torrent_id': transfer_id,
                                'status': (finished_transfer.get('status') or '').lower(),
                                'filename': info.get('filename', ''),
                                'original_filename': info.get('original_filename', ''),
                            }
                        }
                        if not update_torrent_tracking(
                            torrent_hash=hash_for_tracking,
                            trigger_details=updated_trigger,
                            additional_metadata=updated_metadata,
                        ):
                            record_torrent_addition(
                                torrent_hash=hash_for_tracking,
                                trigger_source='premiumize',
                                rationale='Added via Premiumize',
                                item_data={},
                                trigger_details=updated_trigger,
                                additional_metadata=updated_metadata,
                            )
                except Exception as track_err:
                    logger.debug(f"Premiumize tracking enrichment failed (non-critical): {track_err}")

            return transfer_id

        except TorrentAdditionError:
            raise
        except RateLimitError as e:
            logger.warning(f"Premiumize add_torrent rate-limited (429): {e}")
            raise ProviderUnavailableError(f"429 Too Many Requests: {e}")
        except Exception as e:
            logger.error(f"Premiumize add_torrent error: {e}")
            raise TorrentAdditionError(f"Failed to add torrent: {e}")

    # ── Torrent info ─────────────────────────────────────────────────────────

    def get_torrent_info(self, torrent_id: str) -> Optional[Dict]:
        """
        Get info for a specific transfer and resolve its files.

        File resolution strategy (per API docs):
        1. transfer.file_id set  → GET /item/details?id={file_id}  (single file, richest data)
        2. transfer.folder_id set → GET /folder/list?id={folder_id} (multi-file pack)
        3. Fallback               → synthetic entry from transfer name
        """
        try:
            transfers = self._list_transfers_raw()
            raw = next((t for t in transfers if str(t.get('id', '')) == str(torrent_id)), None)
            if not raw:
                logger.debug(f"Premiumize transfer {torrent_id} not found")
                return None

            info = self._build_torrent_dict(raw)

            file_id   = raw.get('file_id')
            folder_id = raw.get('folder_id')

            if file_id:
                # Single file — GET /item/details for full metadata
                try:
                    det = self._req('GET', '/item/details', params={'id': file_id})
                    if det and det.get('status') != 'error':
                        info['files'] = [{
                            'path':               det.get('name', ''),
                            'name':               det.get('name', ''),
                            'bytes':              int(det.get('size', 0) or 0),
                            'link':               det.get('link', ''),
                            'stream_link':        det.get('stream_link', ''),
                            'mime_type':          det.get('mime_type', ''),
                            'vcodec':             det.get('vcodec', ''),
                            'acodec':             det.get('acodec', ''),
                            'resx':               det.get('resx', ''),
                            'resy':               det.get('resy', ''),
                            'duration':           det.get('duration', ''),
                            'bitrate':            det.get('bitrate', 0),
                            'opensubtitles_hash': det.get('opensubtitles_hash', ''),
                            'transcode_status':   det.get('transcode_status', ''),
                            'selected':           1,
                        }]
                        if not info.get('bytes'):
                            info['bytes'] = int(det.get('size', 0) or 0)
                        if not info.get('added'):
                            info['added'] = det.get('created_at')
                except Exception as e:
                    logger.warning(f"Premiumize /item/details failed for {file_id}: {e}")

            if not info.get('files') and folder_id:
                # Multi-file — GET /folder/list uses 'item' schema
                try:
                    folder_resp = self._req('GET', '/folder/list', params={'id': folder_id})
                    if folder_resp and folder_resp.get('status') != 'error':
                        files = []
                        total_folder_bytes = 0
                        earliest_added = None
                        for item in (folder_resp.get('content') or []):
                            if item.get('type') == 'file':
                                fsize = int(item.get('size', 0) or 0)
                                total_folder_bytes += fsize
                                if not earliest_added and item.get('created_at'):
                                    earliest_added = item.get('created_at')
                                files.append({
                                    'path':             item.get('name', ''),
                                    'name':             item.get('name', ''),
                                    'bytes':            fsize,
                                    'link':             item.get('link', ''),
                                    'stream_link':      item.get('stream_link', ''),
                                    'mime_type':        item.get('mime_type', ''),
                                    'transcode_status': item.get('transcode_status', ''),
                                    'created_at':       item.get('created_at'),
                                    'selected':         1,
                                })
                        info['files'] = files
                        if not info.get('bytes'):
                            info['bytes'] = total_folder_bytes
                        if not info.get('added'):
                            info['added'] = earliest_added
                        logger.debug(f"Premiumize folder {folder_id}: {len(files)} files, {total_folder_bytes} bytes")
                except Exception as e:
                    logger.warning(f"Premiumize /folder/list failed for {folder_id}: {e}")

            # Fallback — synthetic entry so process_content doesn't fail
            if not info.get('files') and info.get('filename'):
                info['files'] = [{
                    'path': info['filename'], 'name': info['filename'],
                    'size': info.get('bytes', 0), 'link': '', 'selected': 1,
                }]

            return info
        except Exception as e:
            logger.error(f"Premiumize get_torrent_info error for {torrent_id}: {e}")
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
        """POST /transfer/delete id={torrent_id}"""
        try:
            resp = self._req('POST', '/transfer/delete', data={'id': torrent_id})
            if resp and resp.get('status') != 'error':
                logger.info(f"Premiumize transfer {torrent_id} deleted ({removal_reason})")
                self._cached_torrent_ids = {
                    h: tid for h, tid in self._cached_torrent_ids.items()
                    if tid != torrent_id
                }
                return True
            logger.warning(f"Premiumize delete {torrent_id}: {resp}")
            return False
        except Exception as e:
            logger.error(f"Premiumize remove_torrent error for {torrent_id}: {e}")
            return False

    # ── List & counts ────────────────────────────────────────────────────────

    def list_active_torrents(self) -> List[Dict]:
        """
        Returns ALL transfers (pending + completed) for the Torrents/Active tabs.
        Uses /transfer/list as the source, enriched with /item/listall for
        size + created_at on completed items.

        Only FINISHED transfers contribute to the reconcile audit — the route
        filters by status='downloaded' when building the stable library snapshot.
        """
        try:
            transfers = self._list_transfers_raw()
            item_map  = self._fetch_item_map()
            result = []
            for t in transfers:
                info = self._build_torrent_dict(t)
                lookup_id = str(t.get('file_id') or t.get('folder_id') or '')
                if lookup_id and lookup_id in item_map:
                    item = item_map[lookup_id]
                    if not info['bytes']:
                        info['bytes'] = int(item.get('size', 0) or 0)
                    if not info['added']:
                        info['added'] = item.get('created_at')
                result.append(info)
            return result
        except Exception as e:
            logger.error(f"Premiumize list_active_torrents error: {e}")
            return []

    def list_completed_torrents(self) -> List[Dict]:
        """
        Returns one entry per completed TRANSFER (not per file).

        Iterates /transfer/list as the primary source so season packs
        (folder-based transfers) appear as a single row — matching RealDebrid.
        Only finished/seeding transfers are included; pending ones are skipped.

        Size is sourced from /item/listall for single-file transfers (file_id)
        and from /folder/list for folder transfers (folder_id).
        """
        try:
            item_map  = self._fetch_item_map()   # file_id → item (from /item/listall)
            transfers = self._list_transfers_raw()

            result = []
            for t in transfers:
                raw_status = (t.get('status') or '').lower()
                if raw_status not in ('finished', 'seeding'):
                    continue   # skip pending / error transfers

                tid      = str(t.get('id', ''))
                name     = t.get('name', '')
                hash_val = self._resolve_src_hash(t.get('src', '') or '')
                file_id  = str(t.get('file_id',   '') or '')
                folder_id = str(t.get('folder_id', '') or '')
                added    = None
                total_bytes = 0

                if file_id and file_id in item_map:
                    # Single file — size and date from /item/listall
                    item = item_map[file_id]
                    total_bytes = int(item.get('size', 0) or 0)
                    added       = item.get('created_at')
                elif folder_id:
                    # Folder/season pack — sum sizes from /folder/list
                    try:
                        folder_resp = self._req('GET', '/folder/list', params={'id': folder_id})
                        if folder_resp and folder_resp.get('status') != 'error':
                            for fi in (folder_resp.get('content') or []):
                                if fi.get('type') == 'file':
                                    total_bytes += int(fi.get('size', 0) or 0)
                                    if not added:
                                        added = fi.get('created_at')
                    except Exception as fe:
                        logger.debug(f"Premiumize folder size fetch failed for {folder_id}: {fe}")

                result.append({
                    'id':                tid,
                    'filename':          name,
                    'original_filename': name,
                    'hash':              hash_val,
                    'status':            str(TorrentStatus.DOWNLOADED.value),
                    'progress':          100.0,
                    'bytes':             total_bytes,
                    'added':             added,
                    'src':               t.get('src', ''),
                    'folder_id':         folder_id,
                    'file_id':           file_id,
                    'message':           '',
                    'debrid_folder_name': name,
                })

            return result
        except Exception as e:
            logger.error(f"Premiumize list_completed_torrents error: {e}")
            return []

    @timed_lru_cache(seconds=2)
    def get_active_downloads(self) -> Tuple[int, int]:
        """Count active transfers from /transfer/list"""
        try:
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return 0, 0
            transfers = self._list_transfers_raw()
            active = sum(1 for t in transfers
                         if (t.get('status') or '').lower() in ('waiting', 'queued', 'running'))
            if active >= self.MAX_DOWNLOADS:
                raise TooManyDownloadsError(
                    f"Too many active Premiumize downloads ({active}/{self.MAX_DOWNLOADS})")
            return active, self.MAX_DOWNLOADS
        except TooManyDownloadsError:
            raise
        except Exception as e:
            raise ProviderUnavailableError(f"Failed to get Premiumize active downloads: {e}")

    def get_user_traffic(self) -> Dict:
        """GET /account/info for limit_used (fair-use) and space_used"""
        try:
            if get_setting("Debrid Provider", "api_key") == "demo_key":
                return {'downloaded': 0, 'limit': None}
            result = self._req('GET', '/account/info')
            if not result or result.get('status') == 'error':
                return {'downloaded': 0, 'limit': None}
            return {
                'downloaded':  0,
                'limit':       None,
                'limit_used':  result.get('limit_used', 0),
                'space_used':  result.get('space_used', 0),
            }
        except Exception as e:
            raise ProviderUnavailableError(f"Failed to get Premiumize traffic: {e}")

    def get_cached_torrent_id(self, hash_value: str) -> Optional[str]:
        return self._cached_torrent_ids.get((hash_value or '').lower())

    def get_cached_torrent_title(self, hash_value: str) -> Optional[str]:
        return self._cached_torrent_titles.get((hash_value or '').lower())

    def check_connectivity(self) -> Tuple[bool, Optional[Dict]]:
        try:
            result = self._req('GET', '/account/info')
            if result and result.get('status') != 'error':
                return True, None
            return False, {'service': 'Premiumize', 'type': 'API_ERROR',
                           'message': result.get('message', 'Unexpected response') if result else 'No response'}
        except PremiumizeAuthError as e:
            return False, {'service': 'Premiumize', 'type': 'AUTH_ERROR', 'message': str(e)}
        except Exception as e:
            return False, {'service': 'Premiumize', 'type': 'CONNECTION_ERROR', 'message': str(e)}

    # ── Private helpers ──────────────────────────────────────────────────────

    def _resolve_src_hash(self, src: str) -> str:
        """
        Extract infohash from a transfer src value.
        - magnet: → parse btih directly
        - https://www.premiumize.me/api/job/src?id=... → follow redirect to magnet,
          then extract 40-char hex hash (handles mangled colons/slashes in final URL)
        """
        import re as _re
        _HASH_RE = _re.compile(r'btih[_:]([a-fA-F0-9]{40})', _re.IGNORECASE)

        if not src:
            return ''
        # Direct magnet or inline btih
        m = _HASH_RE.search(src)
        if m:
            return m.group(1).lower()
        if src.startswith('magnet:'):
            h = extract_hash_from_magnet(src)
            return h.lower() if h else ''
        # HTTP src — Premiumize /api/job/src returns 200 with the magnet in
        # the Content-Disposition filename field, empty body.
        if src.startswith('http'):
            try:
                import requests as _requests
                r = _requests.get(src, allow_redirects=True, timeout=10)
                cd = r.headers.get('Content-Disposition', '')
                m = _HASH_RE.search(cd)
                if m:
                    return m.group(1).lower()
                # Fallback: scan final URL in case of a real redirect
                m = _HASH_RE.search(r.url)
                if m:
                    return m.group(1).lower()
            except Exception as e:
                logger.debug(f"Premiumize src fetch failed for {src}: {e}")
        return ''

    def _list_transfers_raw(self) -> List[Dict]:
        """GET /transfer/list → transfers[{id,name,message,status,progress,src,folder_id,file_id}]"""
        resp = self._req('GET', '/transfer/list')
        if not resp or resp.get('status') == 'error':
            logger.warning(f"Premiumize /transfer/list failed: {resp}")
            return []
        transfers = resp.get('transfers', [])
        return transfers if isinstance(transfers, list) else []

    def _fetch_item_map(self) -> Dict[str, Dict]:
        """GET /item/listall → {item_id: item} for size/created_at enrichment"""
        item_map: Dict[str, Dict] = {}
        try:
            resp = self._req('GET', '/item/listall')
            if resp and resp.get('status') != 'error':
                for item in (resp.get('files') or []):
                    iid = str(item.get('id', ''))
                    if iid:
                        item_map[iid] = item
        except Exception as e:
            logger.debug(f"Premiumize /item/listall non-critical error: {e}")
        return item_map

    def _build_torrent_dict(self, t: Dict) -> Dict:
        """Convert a raw transfer dict to the standard provider format."""
        raw_status = (t.get('status') or '').lower()
        status_str = _to_status_str(raw_status)
        progress   = float(t.get('progress', 0) or 0) * 100  # API gives 0.0–1.0
        # Premiumize reports progress=0 even for finished transfers; normalise to 100
        if raw_status in ('finished', 'seeding') and progress == 0:
            progress = 100.0

        # Extract hash from src — may be a magnet or a redirect URL
        src = t.get('src', '') or ''
        hash_val = self._resolve_src_hash(src)

        return {
            'id':                str(t.get('id', '')),
            'filename':          t.get('name', ''),
            'original_filename': t.get('name', ''),
            'hash':              hash_val,
            'status':            status_str,
            'progress':          progress,
            'bytes':             0,      # populated by enrichment or item/details
            'added':             None,   # populated by enrichment or item/details
            'src':               src,
            'folder_id':         t.get('folder_id', ''),
            'file_id':           t.get('file_id', ''),
            'message':           t.get('message', ''),
            'debrid_folder_name': t.get('name', ''),
        }
