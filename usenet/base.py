"""
Shared contract for usenet backends.

Every client returned by ``usenet.get_usenet_client()`` implements the whole
``UsenetBackend`` method set. Features only some backends have (cli_mount's
rename, tags, cli_ids, debrid sync, sidecar upload, exact delete) are listed in
the client's ``CAPABILITIES``. A backend without a feature still has the method,
as a no-op that returns False, so a caller that forgets to check never raises
AttributeError. Callers that want to skip the work entirely (retry loops,
threads) check ``supports(client, CAP)`` first.
"""

from typing import Any, Dict, Optional, Protocol, Tuple, runtime_checkable

# cli_mount-only features. Keep names stable: tests and callers use them.
RENAME = 'rename'                # rename_nzb / rename_nzb_with_status
TAGS = 'tags'                    # push_tags / push_tags_for_item / remove_tags
CLI_IDS = 'cli_ids'              # register_cli_ids / register_cli_ids_for_item
DEBRID_SYNC = 'debrid_sync'      # sync_debrid_torrent
EXACT_REMOVE = 'exact_remove'    # remove_nzb_exact (delete one job by hash, no name fallback)
SIDECAR = 'sidecar'              # inject_sidecar_file
QUEUE = 'queue'                  # iter_queue_jobs / find_job_hash_by_name / delete_queue_job / iter_library_nzbs

ALL_CAPABILITIES = frozenset({RENAME, TAGS, CLI_IDS, DEBRID_SYNC, EXACT_REMOVE, SIDECAR, QUEUE})


# One method per capability, for clients that don't declare CAPABILITIES
# (duck-typed test fakes, third-party subclasses): having it means support.
_PROBE_METHOD = {
    RENAME: 'rename_nzb',
    TAGS: 'push_tags',
    CLI_IDS: 'register_cli_ids',
    DEBRID_SYNC: 'sync_debrid_torrent',
    EXACT_REMOVE: 'remove_nzb_exact',
    SIDECAR: 'inject_sidecar_file',
    QUEUE: 'iter_queue_jobs',
}


def supports(client: Any, capability: str) -> bool:
    """True when ``client`` supports ``capability``.

    Clients that declare CAPABILITIES are taken at their word. Others are
    probed for the capability's method, which keeps duck-typed clients working.
    """
    if client is None:
        return False
    declared = getattr(client, 'CAPABILITIES', None)
    if declared is not None:
        return capability in declared
    method = _PROBE_METHOD.get(capability)
    return bool(method) and callable(getattr(client, method, None))


@runtime_checkable
class UsenetBackend(Protocol):
    PROVIDER_NAME: str
    CAPABILITIES: frozenset

    # --- core: every backend does these for real -------------------------
    def is_enabled(self) -> bool: ...
    def check_connectivity(self) -> tuple: ...
    def add_nzb_content(self, nzb_content: str, title: str = '', category: str = '', *a, **k): ...
    def add_nzb(self, nzb_url: str, title: str = '', category: str = '', *a, **k): ...
    def get_nzb_file_info(self, job_name: str, season: int = None, episode: int = None, *a, **k): ...
    def get_nzb_folder_all_files(self, job_name: str) -> Optional[Tuple[str, list]]: ...
    def remove_nzb(self, info_hash: str, entry_name: str = '') -> bool: ...
    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]: ...
    def resolve_job_id(self, entry_name: str) -> str: ...
    def wait_for_completion(self, job_id: str, timeout: int = 3600, poll_interval: int = 10) -> bool: ...
    def trigger_health_check(self, entry_name: str) -> bool: ...
    def poll_health_result(self, entry_name: str) -> Optional[str]: ...
    def check_entry_health(self, entry_name: str) -> Optional[str]: ...
    def fetch_broken_items(self) -> list: ...
    def get_health_summary(self) -> dict: ...
    def trigger_health_scan(self, full: bool = False, wait: bool = False, timeout: int = 300) -> bool: ...

    # --- capability-gated: no-op returning False when not supported ---------
    def rename_nzb(self, info_hash: str, new_name: str) -> bool: ...
    def rename_nzb_with_status(self, info_hash: str, new_name: str) -> tuple: ...
    def register_cli_ids(self, info_hash: str, ids: dict) -> bool: ...
    def register_cli_ids_for_item(self, info_hash: str, item_id: int) -> bool: ...
    def push_tags(self, info_hash: str, tags) -> bool: ...
    def push_tags_for_item(self, info_hash: str, item_id: int) -> bool: ...
    def remove_tags(self, info_hash: str, tags) -> bool: ...
    def sync_debrid_torrent(self, info_hash: str, provider: str, provider_id: str) -> bool: ...
    def remove_nzb_exact(self, info_hash: str) -> bool: ...
    def inject_sidecar_file(self, folder_name: str, filename: str, content: bytes) -> bool: ...
    def iter_queue_jobs(self, search: str = '', page_size: int = 50, sort: bool = True,
                        timeout: int = 10, retries: int = 1): ...
    def find_job_hash_by_name(self, name: str, page_size: int = 50, sort: bool = True) -> str: ...
    def delete_queue_job(self, info_hash: str) -> Optional[int]: ...
    def iter_library_nzbs(self, page_size: int = 100, timeout: int = 10): ...


class UnsupportedFeaturesMixin:
    """No-op versions of every capability-gated method, for backends that
    have none of them. A backend that gains one overrides it and adds the
    capability to its CAPABILITIES."""

    CAPABILITIES: frozenset = frozenset()

    def rename_nzb(self, info_hash, new_name):
        return False

    def rename_nzb_with_status(self, info_hash, new_name):
        # Same shape as CliMountClient: (ok, not_found). not_found=True tells
        # retry loops to stop: the entry can never be renamed here.
        return False, True

    def register_cli_ids(self, info_hash, ids):
        return False

    def register_cli_ids_for_item(self, info_hash, item_id):
        return False

    def push_tags(self, info_hash, tags):
        return False

    def push_tags_for_item(self, info_hash, item_id):
        return False

    def remove_tags(self, info_hash, tags):
        return False

    def sync_debrid_torrent(self, info_hash, provider, provider_id):
        return False

    def remove_nzb_exact(self, info_hash):
        return False

    def inject_sidecar_file(self, folder_name, filename, content):
        return False

    def iter_queue_jobs(self, search='', page_size=50, sort=True, timeout=10, retries=1):
        return iter(())

    def find_job_hash_by_name(self, name, page_size=50, sort=True):
        return ''

    def delete_queue_job(self, info_hash):
        return None

    def iter_library_nzbs(self, page_size=100, timeout=10):
        return iter(())
