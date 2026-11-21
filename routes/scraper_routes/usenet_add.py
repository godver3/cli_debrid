"""Add NZBs (single, pack) to the usenet backend."""

from flask import jsonify
import logging
from usenet import get_usenet_provider_display_name as _usenet_pname
from utilities.local_library_scan import extract_resolution_from_filename
import json
from ._common import _record_manual_grab

def _get_content_source_display_name(content_source_id):
    """Resolve content source ID to its display_name from settings config."""
    if not content_source_id:
        return None
    try:
        from utilities.settings import get_setting as _gs2
        sources = _gs2('Content Sources', None, {})
        if not sources:
            from utilities.settings import load_config as _lc
            cfg = _lc()
            sources = cfg.get('Content Sources', {})
        source_cfg = sources.get(content_source_id, {})
        return source_cfg.get('display_name', '').strip() or None
    except Exception:
        return None

def _build_nzb_title(title, year, imdb_id, version, original_scraped_torrent_title,
                     media_type=None, season=None, episode=None, episode_title=None,
                     pack_original=None, tags=None, content_source_display_name=None):
    """
    Build a structured NZB job title when 'Enable NZB File Naming' is on.
    This becomes both the cli_mount folder name and filename.
    Falls back to original_scraped_torrent_title if setting is off or data missing.

    pack_original: if provided, used for the (original) bracket instead of
                   original_scraped_torrent_title. Used by NZB aggregate packs so
                   the bracket always shows the pack quality tags regardless of
                   per-episode filename availability.
    """
    from utilities.settings import get_setting as _gs
    if not _gs('Usenet Provider', 'enable_nzb_naming', False):
        return original_scraped_torrent_title

    import re as _re
    # Sanitize components for use in filenames
    def _san(s):
        return _re.sub(r'[\\/*?:"<>|]', '', str(s or '')).strip()

    _title = _san(title)
    _year = _san(year)
    _imdb = _san(imdb_id) if imdb_id else ''
    # Version toggle
    _include_version = _gs('Usenet Provider', 'include_version_in_nzb_naming', True)
    _version = _san(version).strip('*') if (version and _include_version) else ''
    # Content source toggle
    _include_cs = _gs('Usenet Provider', 'include_content_source_in_nzb_naming', False)
    _cs_part = _san(content_source_display_name) if (_include_cs and content_source_display_name) else ''
    # (original) bracket uses pack_original when provided, else original_scraped_torrent_title
    _orig_full = _san(pack_original or original_scraped_torrent_title or '')
    # Strip redundant prefix — pass 1: year-based, pass 2: resolution-based for episodes.
    _orig = _orig_full
    if _orig_full and _year:
        _strip_pat = _re.compile(
            r'^.*?[\s.]' + _re.escape(str(_year)) + r'[\s.]+',
            _re.IGNORECASE
        )
        _stripped = _strip_pat.sub('', _orig_full).strip(' .')
        if _stripped and _stripped != _orig_full:
            _orig = _stripped
    if _orig == _orig_full and _orig_full:
        _ep_strip = _re.compile(r'^.*?((?:2160|1080|720)[pP][\s.])', _re.IGNORECASE)
        _em = _ep_strip.match(_orig_full)
        if _em:
            _orig = _orig_full[_em.start(1):].strip(' .')
    # Tags are no longer embedded in the title — they're pushed to cli_mount
    # directly via CliMountClient.push_tags() instead. Kept as a no-op var so
    # the _assemble(...) call sites below don't need to change.
    _tags_part = ''

    is_episode = media_type in ('tv', 'show', 'episode') and season is not None and episode is not None

    # NZB name is used as both folder AND filename: folder/folder.nzb
    # Full path = folder + '/' + folder + '.nzb' = 2*folder + 5
    # Keep full path <= 240: folder <= (240-5)/2 = 117
    _MAX_TITLE = 117

    def _assemble(*p):
        return ' - '.join(x for x in p if x)

    if is_episode:
        _ep_title = _san(episode_title or '')
        base = _assemble(f'{_title} ({_year})', f'S{int(season):02d}E{int(episode):02d}')
        _imdb_part = f'{{imdb-{_imdb}}}' if _imdb else ''

        # Try full title first: base - ep_title - imdb - tags - version - cs - (orig)
        full = _assemble(base, _ep_title, _imdb_part, _tags_part, _version, _cs_part, f'({_orig})' if _orig else '')
        if len(full) <= _MAX_TITLE:
            return full

        # Drop episode title
        without_ep = _assemble(base, _imdb_part, _tags_part, _version, _cs_part, f'({_orig})' if _orig else '')
        if len(without_ep) <= _MAX_TITLE:
            return without_ep

        # Drop content source
        without_cs = _assemble(base, _imdb_part, _tags_part, _version, f'({_orig})' if _orig else '')
        if len(without_cs) <= _MAX_TITLE:
            return without_cs

        # Truncate (original) to fit — always keep it, just shorter. Drop
        # tags/version from fixed_part first if even that alone leaves no room,
        # so imdb_part (the field cli_debrid uses to re-match this entry later)
        # is the last thing ever dropped, never the first.
        if _orig:
            for fixed_part in (
                _assemble(base, _imdb_part, _tags_part, _version),
                _assemble(base, _imdb_part, _version),
                _assemble(base, _imdb_part),
            ):
                # " - (" prefix (4 chars) + ")" suffix (1 char) = 5 chars overhead
                available = _MAX_TITLE - len(fixed_part) - 5
                if available > 10:
                    truncated_orig = _orig[:available]
                    return f'{fixed_part} - ({truncated_orig})'

        # Last resort: no (original), but imdb is still present.
        without_orig = _assemble(base, _imdb_part)
        if len(without_orig) <= _MAX_TITLE:
            return without_orig
        return base[:_MAX_TITLE]
    else:
        _season_part = f'S{int(season):02d}' if season is not None else ''
        base = _assemble(f'{_title} ({_year})', _season_part)
        _imdb_str = f'{{imdb-{_imdb}}}' if _imdb else ''
        _orig_part = f'({_orig})' if _orig else ''

        # imdb_id must never be the thing dropped to fit _MAX_TITLE — it is the
        # only field cli_debrid uses to re-match this entry later (see
        # repair_engine.py Strategy 4). Drop content-source and tags first, then
        # truncate the (original) bracket, which is decorative. The version goes
        # only after that: the in-flight check in torrent_processor reuses a
        # cli_mount job whose name minus " - (original)" matches, so a 1080p and
        # a 2160p job named without their version looked like the same job and
        # the second version was never submitted. Mirrors the episode branch.
        for attempt in [
            _assemble(base, _imdb_str, _tags_part, _version, _cs_part, _orig_part),
            _assemble(base, _imdb_str, _tags_part, _version, _orig_part),
            _assemble(base, _imdb_str, _version, _orig_part),
        ]:
            if len(attempt) <= _MAX_TITLE:
                return attempt

        # Truncate (original) to fit — always keep it, just shorter.
        if _orig:
            for fixed_part in (_assemble(base, _imdb_str, _version), _assemble(base, _imdb_str)):
                # " - (" prefix (4 chars) + ")" suffix (1 char) = 5 chars overhead
                available = _MAX_TITLE - len(fixed_part) - 5
                if available > 10:
                    truncated_orig = _orig[:available]
                    return f'{fixed_part} - ({truncated_orig})'

        # Last resort: no (original), but imdb is still present.
        without_orig = _assemble(base, _imdb_str)
        if len(without_orig) <= _MAX_TITLE:
            return without_orig
        return base[:_MAX_TITLE]

def _build_debrid_title(title, year, imdb_id, version, original_scraped_torrent_title,
                        media_type=None, season=None, episode=None, episode_title=None,
                        tags=None, content_source_display_name=None):
    """
    Build a structured debrid folder name when 'Enable Debrid File Naming' is on.
    This becomes the cli_mount DFS mount folder/file name for the debrid torrent.
    Completely separate from _build_nzb_title — reads Debrid Provider settings only.
    Falls back to original_scraped_torrent_title if setting is off or data missing.
    Tags are pushed to cli_mount directly via CliMountClient.push_tags(), not embedded here.
    """
    from utilities.settings import get_setting as _gs
    if not _gs('Debrid Provider', 'enable_debrid_naming', False):
        return original_scraped_torrent_title

    import re as _re

    def _san(s):
        return _re.sub(r'[\\/*?:"<>|]', '', str(s or '')).strip()

    _title = _san(title)
    _year = _san(year)
    _imdb = _san(imdb_id) if imdb_id else ''
    _include_version = _gs('Debrid Provider', 'include_version_in_debrid_naming', True)
    _version = _san(version).strip('*') if (version and _include_version) else ''
    _include_cs = _gs('Debrid Provider', 'include_content_source_in_debrid_naming', False)
    _cs_part = _san(content_source_display_name) if (_include_cs and content_source_display_name) else ''
    _orig_full = _san(original_scraped_torrent_title or '')
    # Strip redundant prefix from the original torrent name so only quality tags remain.
    # Pass 1: strip everything up to and including the year (movies/shows with year).
    # Pass 2: strip SxxExx + optional episode title (episode filenames without year).
    _orig = _orig_full
    if _orig_full and _year:
        _strip_pat = _re.compile(
            r'^.*?[\s.]' + _re.escape(str(_year)) + r'[\s.]+',
            _re.IGNORECASE
        )
        _stripped = _strip_pat.sub('', _orig_full).strip(' .')
        if _stripped and _stripped != _orig_full:
            _orig = _stripped
    if _orig == _orig_full and _orig_full:
        # No year found — try stripping everything up to resolution/quality tag.
        # Handles episode filenames like "Show S10E04 Title 1080p TrueHD..." → "1080p TrueHD..."
        _ep_strip = _re.compile(r'^.*?((?:2160|1080|720)[pP][\s.])', _re.IGNORECASE)
        _em = _ep_strip.match(_orig_full)
        if _em:
            _orig = _orig_full[_em.start(1):].strip(' .')
    # Tags are no longer embedded in the title — they're pushed to cli_mount
    # directly via CliMountClient.push_tags() instead. Kept as a no-op var so
    # the _assemble(...) call sites below don't need to change.
    _tags_part = ''

    is_episode = media_type in ('tv', 'show', 'episode') and season is not None and episode is not None

    # Debrid folder name is used as both folder AND filename: folder/folder.mkv
    # Full path = folder + '/' + folder + '.mkv' = 2*folder + 5
    # Keep full path <= 240: folder <= (240-5)/2 = 117
    _MAX_TITLE = 117

    def _assemble(*p):
        return ' - '.join(x for x in p if x)

    # (original) is mandatory — drop cs/version/tags before dropping it
    _orig_part = f'({_orig})' if _orig else ''

    if is_episode:
        _ep_title = _san(episode_title or '')
        base = _assemble(f'{_title} ({_year})', f'S{int(season):02d}E{int(episode):02d}')
        _imdb_part = f'{{imdb-{_imdb}}}' if _imdb else ''

        # imdb_id must never be the thing dropped to fit _MAX_TITLE — it is the
        # only field cli_debrid uses to re-match this entry later (see
        # repair_engine.py Strategy 4). Drop ep_title/cs/tags/version first;
        # only then truncate — and finally drop — the (original) bracket,
        # which is decorative.
        for attempt in [
            _assemble(base, _ep_title, _imdb_part, _tags_part, _version, _cs_part, _orig_part),
            _assemble(base, _imdb_part, _tags_part, _version, _cs_part, _orig_part),
            _assemble(base, _imdb_part, _tags_part, _version, _orig_part),
            _assemble(base, _imdb_part, _orig_part),
        ]:
            if len(attempt) <= _MAX_TITLE:
                return attempt

        # Truncate (original) to fit — always keep it, just shorter.
        if _orig:
            fixed_part = _assemble(base, _imdb_part)
            # " - (" prefix (4 chars) + ")" suffix (1 char) = 5 chars overhead
            available = _MAX_TITLE - len(fixed_part) - 5
            if available > 10:
                truncated_orig = _orig[:available]
                return f'{fixed_part} - ({truncated_orig})'

        # Last resort: no (original), but imdb is still present.
        without_orig = _assemble(base, _imdb_part)
        if len(without_orig) <= _MAX_TITLE:
            return without_orig
        return base[:_MAX_TITLE]
    else:
        _season_part = f'S{int(season):02d}' if season is not None else ''
        base = _assemble(f'{_title} ({_year})', _season_part)
        _imdb_str = f'{{imdb-{_imdb}}}' if _imdb else ''

        # imdb_id must never be the thing dropped to fit _MAX_TITLE — it is the
        # only field cli_debrid uses to re-match this entry later (see
        # repair_engine.py Strategy 4). Drop content-source and tags first;
        # only then truncate — and finally drop — the (original) bracket,
        # which is decorative.
        for attempt in [
            _assemble(base, _imdb_str, _tags_part, _version, _cs_part, _orig_part),
            _assemble(base, _imdb_str, _tags_part, _version, _orig_part),
            _assemble(base, _imdb_str, _version, _orig_part),
            _assemble(base, _imdb_str, _orig_part),
        ]:
            if len(attempt) <= _MAX_TITLE:
                return attempt

        # Truncate (original) to fit — always keep it, just shorter.
        if _orig:
            fixed_part = _assemble(base, _imdb_str)
            # " - (" prefix (4 chars) + ")" suffix (1 char) = 5 chars overhead
            available = _MAX_TITLE - len(fixed_part) - 5
            if available > 10:
                truncated_orig = _orig[:available]
                return f'{fixed_part} - ({truncated_orig})'

        # Last resort: no (original), but imdb is still present.
        without_orig = _assemble(base, _imdb_str)
        if len(without_orig) <= _MAX_TITLE:
            return without_orig
        return base[:_MAX_TITLE]

def _submit_single_episode_nzb(client, nzb_url, ep_label, is_anime=False, media_type='',
                                tags=None, tags_exclusive=False):
    """Fetch + submit one episode NZB.
    Returns (job_id, nzb_text, missing_segments) where missing_segments=True means
    cli_mount's server couldn't find the articles (abort pack, don't blacklist)."""
    from routes.api_tracker import api as _nzb_api
    from database.not_wanted_magnets import is_nzb_segment_not_wanted
    nzb_text = None
    try:
        r = _nzb_api.get(nzb_url, timeout=15, allow_redirects=True)
        if r.status_code != 200 or '<nzb' not in r.text.lower():
            logging.warning(f'[NZBPack] {ep_label}: bad NZB response (status={r.status_code})')
            return None, None, False
        nzb_text = r.text
        if is_nzb_segment_not_wanted(nzb_text):
            logging.info(f'[NZBPack] {ep_label}: segment in not-wanted list, skipping')
            return None, None, False
        job_id = client.add_nzb_content(nzb_content=nzb_text, title=ep_label,
                                        is_anime=is_anime, media_type=media_type,
                                        tags=tags, tags_exclusive=tags_exclusive)
        if not job_id and client.last_missing_segments:
            logging.warning(f'[NZBPack] {ep_label}: cli_mount server missing segments — aborting pack (NZB not blacklisted)')
            return None, None, True
        return job_id, nzb_text, False
    except Exception as e:
        logging.warning(f'[NZBPack] {ep_label}: submit error: {e}')
        return None, None, False

def _add_nzb_pack_to_usenet(episode_nzb_urls, fallback_nzb_urls, title, year, media_type,
                             season, version, tmdb_id, original_scraped_torrent_title=None,
                             episode_filenames=None,
                             genres=None, current_score=0.0,
                             selected_folder=None, selected_folder_is_custom=False,
                             selected_tags=None,
                             existing_items=None,
                             waterfall_level=None):
    """Submit a virtual NZB season pack — one NZB per episode with health-check + retry.

    existing_items: optional dict {ep_num: item_dict} of existing DB items to reuse instead of
                    creating new ones. When provided, those items are updated in-place (state →
                    Adding) preserving their IDs and history. Used by scraping_queue batch path.

    waterfall_level: the group-matching level scrape_newznab_season_aggregate used to build this
                    pack (1 = same release group for every episode; 2/3 = episodes may come from
                    different groups merged under a looser key). Only level 1 guarantees that an
                    expired episode's retention issue applies to the rest of the pack — None
                    (unknown) is treated the same as level 1 for safety.
    """
    from usenet import get_usenet_client, reset_usenet_client
    from metadata.metadata import get_metadata
    from database.database_writing import add_media_item, update_media_item_state, update_media_item

    reset_usenet_client()
    client = get_usenet_client()
    if not client.is_enabled():
        return jsonify({'error': f'Usenet provider ({_usenet_pname()}) is not enabled.'}), 503

    # Resolve metadata once
    imdb_id = None
    episode_titles = {}
    episode_imdb_ids = {}
    try:
        meta = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
        imdb_id = meta.get('imdb_id')
        # Fallback: resolve imdb_id directly if get_metadata didn't return it
        if not imdb_id and tmdb_id:
            try:
                from metadata.metadata import get_imdb_id_if_missing
                _api_type = 'show' if media_type in ('tv', 'show') else media_type
                imdb_id = get_imdb_id_if_missing({'tmdb_id': int(tmdb_id), 'media_type': _api_type})
            except Exception as _ie:
                logging.warning(f'[NZBPack] imdb_id fallback failed: {_ie}')
        # Episode titles and IMDb IDs via DirectAPI (has full season data)
        if imdb_id:
            try:
                from metadata.metadata import DirectAPI
                show_meta, _ = DirectAPI.get_show_metadata(imdb_id)
                season_data = (show_meta.get('seasons') or {}).get(str(season), {}) or \
                              (show_meta.get('seasons') or {}).get(season, {})
                episode_titles = {int(k): v.get('title', f'Episode {k}')
                                 for k, v in (season_data.get('episodes') or {}).items()}
                episode_imdb_ids = {int(k): v['imdb_id']
                                    for k, v in (season_data.get('episodes') or {}).items()
                                    if v.get('imdb_id')}
            except Exception:
                pass
    except Exception as _me:
        logging.warning(f'[NZBPack] Metadata fetch failed: {_me}')

    if not imdb_id:
        logging.warning(f'[NZBPack] Could not resolve imdb_id for tmdb_id={tmdb_id} — replace cleanup will not fire')

    # Pre-load existing cli_mount NZB names to skip already-submitted episodes
    _existing_nzb_names = set()
    try:
        from usenet import get_usenet_client as _guc2
        for _n2 in _guc2().iter_library_nzbs(page_size=100, timeout=10):
            _name2 = _n2.get('name') or _n2.get('title') or _n2.get('filename') or ''
            if _name2:
                _existing_nzb_names.add(_name2.strip())
        logging.info(f'[NZBPack] Loaded {len(_existing_nzb_names)} existing cli_mount NZBs for dedup check')
    except Exception as _de2:
        logging.warning(f'[NZBPack] Could not load existing cli_mount NZBs: {_de2}')

    # Pre-load already-collected episodes to avoid duplicates
    # Exclude episodes marked manual_replace=1 — those are intentional replacements
    _collected_eps = set()
    if imdb_id:
        try:
            from database import get_db_connection as _get_db
            _conn = _get_db()
            _rows = _conn.execute(
                "SELECT episode_number FROM media_items WHERE imdb_id=? AND season_number=? AND type='episode' AND state IN ('Collected','Upgrading') AND (manual_replace IS NULL OR manual_replace=0)",
                (imdb_id, season)
            ).fetchall()
            _collected_eps = {r[0] for r in _rows}
            _conn.close()
            if _collected_eps:
                logging.info(f'[NZBPack] Skipping already-collected episodes (no replace flag): {sorted(_collected_eps)}')
        except Exception as _ce:
            logging.warning(f'[NZBPack] Could not check collected episodes: {_ce}')

    submitted = []
    failed_eps = []
    pack_expired = False  # set True when missing segments detected — abort remaining episodes

    for ep_num in sorted(episode_nzb_urls.keys()):
        if pack_expired:
            failed_eps.append(ep_num)
            continue

        if ep_num in _collected_eps:
            logging.info(f'[NZBPack] Skipping S{season:02d}E{ep_num:02d} — already Collected')
            continue

        import re as _re_label
        # Aggregate pack display title — always has quality tags, used for (original) bracket
        _pack_orig = _re_label.sub(r'\s*\[NZB Pack[^\]]*\]', '', original_scraped_torrent_title or title).strip()

        # Per-episode raw filename — used only for the cli_mount job name (has SxxExx)
        _ep_raw = (episode_filenames or {}).get(ep_num, '') or ''
        _ep_raw_clean = _re_label.sub(r'\.(mkv|mp4|avi|m4v|nfo|nzb)$', '', _ep_raw, flags=_re_label.IGNORECASE).strip()
        if not _ep_raw_clean:
            _ep_raw_clean = _pack_orig  # fallback job name still has quality tags

        ep_label = _build_nzb_title(
            title=title, year=year, imdb_id=imdb_id,
            version=version, original_scraped_torrent_title=f'{_ep_raw_clean}-[NZB Pack]',
            media_type='episode', season=season, episode=ep_num,
            episode_title=episode_titles.get(ep_num),
            pack_original=f'{_pack_orig}-[NZB Pack]',
            tags=selected_tags or None,
        ) or f'{_ep_raw_clean}-[NZB Pack]'
        primary_url = episode_nzb_urls[ep_num]
        fallbacks = fallback_nzb_urls.get(ep_num, [])

        job_id = None
        nzb_text = None

        _is_anime = any('anime' in (g or '').lower() for g in (genres or []))
        # Try primary then fallbacks
        for url in [primary_url] + fallbacks:
            if not url:
                continue
            job_id, nzb_text, expired = _submit_single_episode_nzb(
                client, url, ep_label, is_anime=_is_anime, media_type='episode',
                tags=selected_tags, tags_exclusive=False)
            if expired:
                # Missing segments on this URL — no point trying more fallbacks from same release.
                # Only abort the rest of the pack when every episode came from the same release
                # group (waterfall level 1) — otherwise a different episode's release may have
                # entirely different retention and shouldn't be given up on.
                if waterfall_level is None or waterfall_level == 1:
                    pack_expired = True
                break
            if not job_id:
                continue

            # Health check
            try:
                health = client.check_entry_health(ep_label)
                if health == 'broken':
                    logging.warning(f'[NZBPack] {ep_label}: broken — removing and trying next')
                    # Add to not-wanted (guid-based, works at scrape time)
                    try:
                        from database.not_wanted_magnets import add_to_not_wanted_nzb_guid as _add_guid
                        _add_guid(primary_url)
                    except Exception:
                        pass
                    # Delete from cli_mount
                    try:
                        from usenet import get_usenet_client as _guc_del
                        _client_del = _guc_del()
                        _real_hash = _client_del.find_job_hash_by_name(ep_label, sort=False)
                        if _real_hash:
                            _client_del.delete_queue_job(_real_hash)
                    except Exception as _de:
                        logging.warning(f'[NZBPack] Could not delete broken entry: {_de}')
                    job_id = None
                    nzb_text = None
                    continue  # try next fallback
                else:
                    logging.info(f'[NZBPack] {ep_label}: health={health or "inconclusive"} — keeping')
            except Exception as _he:
                logging.debug(f'[NZBPack] {ep_label}: health check error: {_he} — keeping')

            break  # submitted and passed (or inconclusive)

        if job_id:
            submitted.append((ep_num, job_id))
            # Create queue item for this episode
            try:
                checking_id = f"nzb:{job_id}" if not str(job_id).startswith('nzb:') else str(job_id)
                # ep_label is what was submitted to cli_mount — adding_queue uses
                # filled_by_file to look up the entry, so it must match exactly
                # Reuse existing item if provided (scraping_queue batch path)
                # otherwise create a new item (scraper UI path)
                existing_item = (existing_items or {}).get(ep_num)
                if existing_item:
                    item_id = existing_item['id']
                    update_media_item_state(item_id, 'Adding')
                else:
                    ep_item = {
                        'title': title, 'year': year, 'type': 'episode',
                        'version': version, 'tmdb_id': tmdb_id, 'imdb_id': imdb_id,
                        'season_number': season, 'episode_number': ep_num,
                        'episode_title': episode_titles.get(ep_num, f'Episode {ep_num}'),
                        'release_date': 'Unknown',
                        'genres': json.dumps(genres or []),
                        'current_score': current_score,
                        'original_scraped_torrent_title': ep_label,
                        'content_source': 'content_requester',
                        'selected_folder': selected_folder,
                        'selected_folder_is_custom': selected_folder_is_custom,
                        'tags': selected_tags,
                    }
                    item_id = add_media_item(ep_item)
                if item_id:
                    update_media_item_state(item_id, 'Adding')
                    _ep_seg_id = ''
                    if nzb_text:
                        try:
                            from database.not_wanted_magnets import extract_nzb_segment_id as _ext_seg
                            _ep_seg_id = _ext_seg(nzb_text)
                        except Exception:
                            pass
                    _ep_seg_kwargs = {'nzb_segment_id': _ep_seg_id} if _ep_seg_id else {}
                    update_media_item(item_id,
                        filled_by_torrent_id=checking_id,
                        filled_by_file=ep_label,
                        filled_by_magnet=primary_url,
                        original_scraped_torrent_title=ep_label,
                        **_ep_seg_kwargs,
                    )
                    _record_manual_grab(item_id, 'nzb', ep_label)
            except Exception as _qe:
                logging.warning(f'[NZBPack] Queue tracking failed for {ep_label}: {_qe}')
        else:
            failed_eps.append(ep_num)
            logging.warning(f'[NZBPack] {ep_label}: all URLs exhausted — episode will need normal queue fill')

    ep_total = len(episode_nzb_urls)
    if pack_expired:
        msg = f'Pack aborted — missing segments detected (Usenet retention exceeded). {len(submitted)}/{ep_total} episodes submitted before abort.'
    else:
        msg = f'{len(submitted)}/{ep_total} episodes submitted to {_usenet_pname()}.'
    if failed_eps and not pack_expired:
        msg += f' Episodes {failed_eps} not found — will be filled by normal queue.'

    logging.info(f'[NZBPack] {title} S{season:02d}: {msg}')
    return jsonify({
        'success': True,
        'message': msg,
        'submitted': len(submitted),
        'failed_episodes': failed_eps,
        'provider': 'cli_mount',
    })

def _add_nzb_to_usenet(nzb_url, title, year, media_type, season, episode, version, tmdb_id,
                       original_scraped_torrent_title=None, genres=None, current_score=0.0,
                       selected_folder=None, selected_folder_is_custom=False, selected_tags=None):
    """Submit an NZB URL to the configured Usenet provider and track it through the queue like a debrid add."""
    from usenet import get_usenet_client, reset_usenet_client
    from metadata.metadata import get_metadata, get_release_date
    reset_usenet_client()
    client = get_usenet_client()

    if not client.is_enabled():
        return jsonify({'error': f'Usenet provider ({_usenet_pname()}) is not enabled. Configure it in Required Settings.'}), 503

    if not nzb_url:
        return jsonify({'error': 'No NZB URL provided'}), 400

    # Pre-fetch NZB XML to check not-wanted list before submitting
    from routes.api_tracker import api as _nzb_api
    from database.not_wanted_magnets import is_nzb_segment_not_wanted as _is_not_wanted
    _nzb_xml = None
    try:
        _nr = _nzb_api.get(nzb_url, timeout=15, allow_redirects=True)
        if _nr.status_code == 200 and '<nzb' in _nr.text.lower():
            _nzb_xml = _nr.text
    except Exception:
        pass

    if _nzb_xml and _is_not_wanted(_nzb_xml):
        logging.info(f'[NZB] Skipping {title!r} — segment ID in not-wanted list (previously broken)')
        return jsonify({'error': f'This NZB is known broken and has been blacklisted: {title}'}), 400

    logging.info(f'[NZB] Submitting to {_usenet_pname()}: {title} ({year})')

    # Build job title — uses structured naming template when enabled
    _imdb_id_for_title = None
    _ep_title = None
    if tmdb_id:
        try:
            _title_meta = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
            _imdb_id_for_title = _title_meta.get('imdb_id')
            if media_type in ('tv', 'show') and season is not None and episode is not None:
                _s = ((_title_meta.get('seasons') or {}).get(season) or
                      (_title_meta.get('seasons') or {}).get(str(season)) or {})
                _ep_title = ((_s.get('episodes') or {}).get(episode) or
                             (_s.get('episodes') or {}).get(str(episode)) or {}).get('title')
        except Exception:
            pass
    _job_title = _build_nzb_title(
        title=title, year=year, imdb_id=_imdb_id_for_title,
        version=version, original_scraped_torrent_title=original_scraped_torrent_title,
        media_type=media_type, season=season, episode=episode, episode_title=_ep_title,
        tags=selected_tags or None,
    )

    _is_anime = any('anime' in (g or '').lower() for g in (genres or []))
    _submit_title = str(_job_title or title or '')
    # Submit — use pre-fetched content directly if available to avoid double-fetch
    if _nzb_xml:
        job_id = client.add_nzb_content(nzb_content=_nzb_xml, title=_submit_title,
                                        is_anime=_is_anime, media_type=media_type or '',
                                        tags=selected_tags, tags_exclusive=False)
        if not job_id and client.last_missing_segments:
            logging.warning(f'[NZB] cli_mount server missing segments for {title!r}')
        elif job_id:
            logging.info(f'[NZB] Submitted via content upload: {title}')
    else:
        job_id = client.add_nzb(nzb_url=nzb_url, title=_submit_title,
                                is_anime=_is_anime, media_type=media_type or '',
                                tags=selected_tags, tags_exclusive=False)

    if not job_id:
        return jsonify({'error': f'Failed to submit NZB to {_usenet_pname()}'}), 500

    logging.info(f'[NZB] Submitted successfully, job_id={job_id}')

    # --- Queue tracking: create media item and move through queue like debrid ---
    try:
        nzb_title = _job_title or original_scraped_torrent_title or title or ''
        checking_id = f"nzb:{job_id}" if not str(job_id).startswith('nzb:') else str(job_id)

        # Get metadata
        imdb_id = None
        release_date = 'Unknown'
        if tmdb_id:
            try:
                meta = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
                imdb_id = meta.get('imdb_id')
                if media_type not in ['tv', 'show']:
                    release_date = get_release_date(meta, imdb_id) or 'Unknown'
            except Exception as _me:
                logging.warning(f'[NZB] Could not fetch metadata for queue tracking: {_me}')

        item_type = 'episode' if media_type in ['tv', 'show'] else 'movie'
        resolution = extract_resolution_from_filename(nzb_title) if nzb_title else None

        base_item = {
            'title': title,
            'year': year,
            'type': item_type,
            'version': version,
            'resolution': resolution,
            'tmdb_id': tmdb_id,
            'imdb_id': imdb_id,
            'release_date': release_date,
            'genres': json.dumps(genres or []),
            'current_score': current_score,
            'original_scraped_torrent_title': nzb_title,
            'content_source': 'content_requester',
            'selected_folder': selected_folder,
            'selected_folder_is_custom': selected_folder_is_custom,
            'tags': selected_tags,
        }

        from queues.queue_manager import QueueManager
        from database.database_writing import add_media_item
        queue_manager = QueueManager()

        from database.database_writing import update_media_item_state, update_media_item

        # Extract segment ID from NZB XML already fetched above (zero extra HTTP call)
        _nzb_seg_id = ''
        if _nzb_xml:
            try:
                from database.not_wanted_magnets import extract_nzb_segment_id as _ext_seg2
                _nzb_seg_id = _ext_seg2(_nzb_xml)
            except Exception:
                pass

        def _place_nzb_in_adding(item_id_to_place):
            """Put a freshly-created item into Adding state with NZB fields set for health-check polling.

            Fields first, state last: the Adding queue can pick the row up the
            moment it is Adding, and a row there without its NZB id looked like
            a duplicate of the episode's old copy and was marked Collected with
            no health check and no symlink (Dragon Ball Kai S02E03, 2026-11-21)."""
            _place_seg_kwargs = {'nzb_segment_id': _nzb_seg_id} if _nzb_seg_id else {}
            update_media_item(item_id_to_place,
                filled_by_torrent_id=checking_id,
                filled_by_file=nzb_title,
                filled_by_magnet=nzb_url,
                original_scraped_torrent_title=nzb_title,
                **_place_seg_kwargs,
            )
            update_media_item_state(item_id_to_place, 'Adding')
            _record_manual_grab(item_id_to_place, 'nzb', nzb_title)
            logging.info(f'[NZB] Item {item_id_to_place} placed in Adding queue for health check (checking_id={checking_id})')

        if item_type == 'episode' and season is not None and episode is None:
            # Season pack — create items per episode
            try:
                meta = get_metadata(tmdb_id=int(tmdb_id), item_media_type=media_type)
                season_data = (meta.get('seasons') or {}).get(season, {})
                episodes = season_data.get('episodes', {})
                for ep_num_str, ep_data in episodes.items():
                    ep_item = base_item.copy()
                    ep_item.update({'season_number': season, 'episode_number': int(ep_num_str),
                                    'episode_title': ep_data.get('title', f'Episode {ep_num_str}')})
                    item_id = add_media_item(ep_item)
                    if item_id:
                        _place_nzb_in_adding(item_id)
            except Exception as _spe:
                logging.warning(f'[NZB] Season pack queue tracking failed: {_spe}')
        else:
            if item_type == 'episode':
                base_item.update({'season_number': season, 'episode_number': episode})
            item_id = add_media_item(base_item)
            if item_id:
                _place_nzb_in_adding(item_id)
    except Exception as _qe:
        logging.error(f'[NZB] Queue tracking failed for {title}: {_qe}', exc_info=True)
        # Don't fail the response — NZB was submitted successfully to cli_mount

    return jsonify({
        'success': True,
        'message': f'NZB submitted to {_usenet_pname()} (job: {job_id}). Tracking through queue.',
        'job_id': job_id,
        'provider': 'cli_mount',
    })
