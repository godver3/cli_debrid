"""Per-run summary for Plex watchlist content sources.

The content-source pipeline drops items at several stages (list limit, per-item cache,
metadata, source filters, add_wanted_items) and most drops were only visible at DEBUG, so
"my item never showed up" meant guessing which stage ate it. A SourceRunReport collects
what happened at each stage and logs one INFO summary line, plus one line per drop reason
naming the affected titles/ids. It does nothing for non-Plex source types.
"""
import logging
from collections import Counter
from typing import Any, Dict, Iterable, List

PLEX_SOURCE_TYPES = frozenset({
    'My Plex Watchlist',
    'Other Plex Watchlist',
    'Plex Friends Watchlist',
    'My Plex RSS Watchlist',
    'My Friends Plex RSS Watchlist',
})

_MAX_LABELS = 25


def _label(item: Dict[str, Any]) -> str:
    return str(item.get('title') or item.get('imdb_id') or item.get('tmdb_id') or 'Unknown')


class SourceRunReport:
    def __init__(self, source: str, source_type: str):
        self.source = source
        self.enabled = source_type in PLEX_SOURCE_TYPES
        self.counts: Counter = Counter()
        self.drops: Dict[str, List[str]] = {}
        self._prefilter: List[Dict[str, Any]] = []

    def _drop(self, reason: str, label: str) -> None:
        labels = self.drops.setdefault(reason, [])
        if label not in labels:
            labels.append(label)

    def fetched(self, wanted_content) -> None:
        """Count the items the fetcher returned (after the list-length limit)."""
        if not self.enabled:
            return
        total = 0
        if isinstance(wanted_content, list):
            for entry in wanted_content:
                total += len(entry[0]) if isinstance(entry, tuple) else 1
        self.counts['fetched'] += total

    def blocked(self, reason: str, raw_item: Dict[str, Any]) -> None:
        """A blacklisted/ghostlisted title dropped before metadata processing."""
        if not self.enabled:
            return
        self.counts['blocked'] += 1
        self._drop(f"{reason} (skipped before metadata processing)", _label(raw_item))

    def metadata_failed(self, raw_items: Iterable[Dict[str, Any]]) -> None:
        """process_metadata returned nothing for the whole batch."""
        if not self.enabled:
            return
        raw_items = list(raw_items)
        self.counts['sent_to_metadata'] += len(raw_items)
        for raw in raw_items:
            self._drop('metadata processing returned nothing for the batch (not cached; retried next run)', _label(raw))

    def metadata_done(self, raw_items: Iterable[Dict[str, Any]], output_ids: set, produced_items: Iterable[Dict[str, Any]], retry_hours: float) -> None:
        if not self.enabled:
            return
        from content_checkers.content_cache_management import item_has_metadata_output
        raw_items = list(raw_items)
        self.counts['sent_to_metadata'] += len(raw_items)
        for raw in raw_items:
            if not item_has_metadata_output(raw, output_ids):
                self._drop(f"no metadata produced (not in the metadata battery yet, or a show with no listed episodes; retried in {retry_hours:g}h)", _label(raw))
        self._prefilter = list(produced_items)

    def filters_done(self, kept_items: Iterable[Dict[str, Any]]) -> None:
        """Items removed by the source's media type / genre / cutoff date filters."""
        if not self.enabled:
            return
        kept_ids = {id(i) for i in kept_items}
        for item in self._prefilter:
            if id(item) not in kept_ids:
                self._drop('removed by the source filters (media type, excluded genres or cutoff date)', _label(item))
        self._prefilter = []

    def added(self, passed_to_add: int, added_count) -> None:
        if not self.enabled:
            return
        self.counts['passed_to_add'] += passed_to_add
        self.counts['added'] += added_count if isinstance(added_count, int) else 0

    def log(self, cache_skipped: int = 0) -> None:
        if not self.enabled:
            return
        c = self.counts
        not_added = max(c['passed_to_add'] - c['added'], 0)
        logging.info(
            f"[PLEX_RUN {self.source}] titles: fetched={c['fetched']} blocked={c['blocked']} cache_skipped={cache_skipped} "
            f"sent_to_metadata={c['sent_to_metadata']} | entries (movies/episodes): "
            f"passed_filters={c['passed_to_add']} newly_added={c['added']} not_added={not_added} "
            f"(see 'Plex watchlist items not added' for why)"
        )
        for reason, labels in self.drops.items():
            shown = ', '.join(repr(l) for l in labels[:_MAX_LABELS])
            more = f" (+{len(labels) - _MAX_LABELS} more)" if len(labels) > _MAX_LABELS else ''
            logging.info(f"[PLEX_RUN {self.source}] {len(labels)} item(s) not added - {reason}: {shown}{more}")
