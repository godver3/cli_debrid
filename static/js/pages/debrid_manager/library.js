// Page script for templates/debrid_manager.html.
// ── LIBRARY TAB ────────────────────────────────────────────────────────────
let LIB_PAGE_SIZE = 100;
let _libAll = [];
let _libFiltered = [];
let _libPage = 1;
let _libSort = { col: 'added', dir: -1 };
let _libTypeFilter = 'all';
let _libStatusFilter = 'downloaded';
let _libDupeFilter = 'none'; // 'none' | 'hash' | 'title'
let _libPlexTrash = false;
let _plexTrashFilenames = new Set(); // torrent folder names in plex trash
let _plexTrashOrphanedFilenames = new Set(); // subset: folders with deletedAt (orphaned)
let _hashCounts = {};
let _titleCounts = {};
let _libSelected = new Set();
window._libLoaded = false;

function detectType(filename, dbMeta) {
    // DB-enriched type takes priority
    if (dbMeta && dbMeta.type) {
        const t = dbMeta.type.toLowerCase();
        if (t === 'episode' || t === 'tv' || t === 'show') return 'tv';
        if (t === 'movie') return 'movie';
    }
    const f = (filename || '').toLowerCase();
    // Standalone Sxx also qualifies as TV (e.g. "Bridgerton S02")
    if (/\bs\d{2}e\d{2}\b|\.s\d{2}\.|\bs\d{2}\b|\bseason[\s. _-]\d|\bepisode[\s. _-]\d/i.test(f)) return 'tv';
    if (/\b(19|20)\d{2}\b/.test(f)) return 'movie';
    return 'other';
}

// Build a clean display title from DB metadata or filename fallback
function buildCleanTitle(t) {
    // Prefer DB-enriched metadata when available
    if (t._db) {
        const d = t._db;
        const title = d.title || '';  // filled_by_title is the torrent filename, not the media title
        if (!title) return parseCleanTitle(t.filename);
        const year = d.year ? ` (${d.year})` : '';
        const sn = d.season_number != null ? ` S${String(d.season_number).padStart(2,'0')}` : '';
        const ep = d.episode_number != null ? `E${String(d.episode_number).padStart(2,'0')}` : '';
        return `${title}${year}${sn}${ep}`;
    }
    return parseCleanTitle(t.filename);
}

function parseCleanTitle(filename) {
    if (!filename) return '';
    let s = filename.replace(/[._]/g, ' ').replace(/\s+/g, ' ').trim();
    s = s.replace(/\.(mkv|mp4|avi|mov|wmv|flac|zip|rar)$/i, '');

    // SxxExx
    const seMatch = s.match(/^(.*?)\s*[Ss](\d{1,2})[Ee](\d{1,2})\b/);
    if (seMatch) {
        const raw  = seMatch[1].trim();
        const se   = `S${String(seMatch[2]).padStart(2,'0')}E${String(seMatch[3]).padStart(2,'0')}`;
        const year = raw.match(/\b((?:19|20)\d{2})\b/);
        const title = raw.replace(/\s*[\[(]?\d{4}[\])]?\s*$/, '').trim();
        return year ? `${title} (${year[1]}) ${se}` : `${title} ${se}`;
    }

    // Season XX (word form, e.g. "1923 - Season 02 [2025]")
    const snMatch = s.match(/^(.*?)\s*[Ss]eason\s+(\d{1,2})\b/i);
    if (snMatch) {
        const raw  = snMatch[1].replace(/\s*[-–]\s*$/, '').trim();
        const se   = `S${String(snMatch[2]).padStart(2,'0')}`;
        // Look for year AFTER the season word (avoids show names like "1923" being treated as year)
        const afterSeason = s.slice(snMatch[0].length);
        const year = afterSeason.match(/\b((?:19|20)\d{2})\b/);
        return year ? `${raw} (${year[1]}) ${se}` : `${raw} ${se}`;
    }

    // Standalone Sxx (e.g. "Bridgerton (2020) S02 ...")
    const sMatch = s.match(/^(.*?)\s*[Ss](\d{1,2})\b(?!\s*[Ee]\d)/);
    if (sMatch) {
        const raw  = sMatch[1].trim();
        const se   = `S${String(sMatch[2]).padStart(2,'0')}`;
        const year = raw.match(/\b((?:19|20)\d{2})\b/);
        const title = raw.replace(/\s*[\[(]?\d{4}[\])]?\s*$/, '').trim();
        return year ? `${title} (${year[1]}) ${se}` : `${title} ${se}`;
    }

    // Movie: everything up to the year
    const yMatch = s.match(/^(.*?)\s*\b((?:19|20)\d{2})\b/);
    if (yMatch) {
        return `${yMatch[1].trim()} (${yMatch[2]})`;
    }

    return s.slice(0, 60);
}

function timeAgo(v) {
    if (!v) return '—';
    const d = typeof v === 'number' ? new Date(v * 1000) : new Date(v);
    if (isNaN(d)) return String(v);
    const diff = Date.now() - d.getTime();
    const mins = Math.floor(diff / 60000);
    const hours = Math.floor(mins / 60);
    const days = Math.floor(hours / 24);
    const weeks = Math.floor(days / 7);
    const months = Math.floor(days / 30);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    if (hours < 24) return hours + 'h ago';
    if (days < 7) return days + 'd ago';
    if (weeks < 5) return weeks + 'w ago';
    if (months < 12) return months + 'mo ago';
    return Math.floor(months / 12) + 'y ago';
}

function fmt_added_abs(v) {
    if (!v) return '';
    const d = typeof v === 'number' ? new Date(v * 1000) : new Date(v);
    if (isNaN(d)) return '';
    return d.toLocaleString();
}

const CHECK_SVG = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/></svg>';

function _flashCheck(el, origHtml, ms = 1500) {
    el.innerHTML = CHECK_SVG;
    el.style.color = '#4CAF50';
    setTimeout(() => { el.innerHTML = origHtml; el.style.color = ''; }, ms);
}

function copyHash(hash, btn) {
    if (!hash) return;
    navigator.clipboard.writeText(hash).catch(() => {
        const ta = document.createElement('textarea');
        ta.value = hash; document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); document.body.removeChild(ta);
    });
    if (btn) _flashCheck(btn, btn.innerHTML);
}

function buildHashCounts() {
    _hashCounts = {};
    _libAll.forEach(t => { if (t.hash) _hashCounts[t.hash] = (_hashCounts[t.hash] || 0) + 1; });

    // Title-based dupe detection: normalize to core title (strip season/episode/year)
    // so "Fallout (2024) S01" and "Fallout (2024) S01" from different torrents are caught
    _titleCounts = {};
    _libAll.forEach(t => {
        const key = _titleKey(t);
        if (key) _titleCounts[key] = (_titleCounts[key] || 0) + 1;
    });
}

function _extractSeasonEp(filename) {
    // Returns "sXXeXX" for specific episodes, "sXX" for season packs, null for movies/unknown
    if (!filename) return null;
    const epMatch = filename.match(/\bS(\d{2})E(\d{2,3})\b/i);
    if (epMatch) return `s${epMatch[1].padStart(2,'0')}e${epMatch[2].padStart(2,'0')}`;
    const seasonMatch = filename.match(/\bS(\d{2})\b/i) || filename.match(/\bSeason\s+(\d{1,2})\b/i);
    if (seasonMatch) return `s${seasonMatch[1].padStart(2,'0')}`;
    return null;
}

function _titleKey(t) {
    // Movies:        "title (year)"              — match on title+year
    // TV episode:    "title (year) s03e06"       — match exact episode
    // TV season pack:"title (year) s03"          — match season pack
    let base = '';
    let seep = null; // season+episode suffix

    if (t._db && t._db.title) {
        base = t._db.title.toLowerCase().trim();
        if (t._db.year) base += ` (${t._db.year})`;
        if (t._type === 'tv') {
            if (t._db.season_number && t._db.episode_number) {
                seep = `s${String(t._db.season_number).padStart(2,'0')}e${String(t._db.episode_number).padStart(2,'0')}`;
            } else if (t._db.season_number) {
                seep = `s${String(t._db.season_number).padStart(2,'0')}`;
            } else {
                seep = _extractSeasonEp(t.filename);
            }
        }
    } else {
        const clean = parseCleanTitle(t.filename);
        base = clean.toLowerCase()
            .replace(/\s+s\d{2}(e\d{2,3})?\b.*/i, '')
            .replace(/\s+season\s+\d+.*/i, '')
            .trim();
        if (t._type === 'tv') {
            seep = _extractSeasonEp(t.filename);
        }
    }

    if (!base) return null;
    return (t._type === 'tv' && seep) ? `${base} ${seep}` : base;
}

function updateDupesBtn() {
    const dupeHashes = new Set(Object.keys(_hashCounts).filter(h => _hashCounts[h] > 1));
    const extraCopies = [...dupeHashes].reduce((s, h) => s + _hashCounts[h] - 1, 0);
    const delDupesBtn = document.getElementById('lib-delete-dupes-btn');
    delDupesBtn.style.display = extraCopies > 0 ? '' : 'none';
    delDupesBtn.innerHTML = `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" style="width:15px;height:15px;flex-shrink:0"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg> Delete ${extraCopies} Dup${extraCopies === 1 ? 'e' : 'es'}`;
}

function _fmtCacheAge(seconds) {
    if (!seconds || seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
    if (seconds > 30 * 86400) return '--'; // guard against epoch-magnitude values
    return `${Math.round(seconds / 86400)}d ago`;
}

function _updateCacheBadge(status, ageSeconds, total, loading) {
    const label = document.getElementById('lib-reload-label');
    if (!label) return;
    if (status === 'partial') {
        label.textContent = `Loading… ${(total||0).toLocaleString()} so far`;
    } else if (loading) {
        label.textContent = `Refreshing… (${_fmtCacheAge(ageSeconds)})`;
    } else if (status === 'live') {
        label.textContent = 'Refresh Library';
    } else {
        label.textContent = `Refresh Library · ${_fmtCacheAge(ageSeconds)}`;
    }
}

// The library holds every provider's torrents; Torrents shows the one picked in the header.
function _dmPickedProvider() {
    const sel = document.getElementById('dm-provider-select');
    return sel ? sel.value : '';
}

// ?provider=<name> for a request about one Audit row's torrent: it comes before
// the one the header picker appends, so it wins. Older rows have no provider
// recorded; those went to the primary before multi-provider, so pin them there
// rather than letting the header picker redirect them.
function _dmProviderQS(name) {
    if (!name && Array.isArray(_allProviders) && _allProviders.length > 1) {
        name = _allProviders[0].name;
    }
    return name ? `?provider=${encodeURIComponent(name)}` : '';
}

function _applyLibraryData(data) {
    const picked = _dmPickedProvider();
    _libAll = data.torrents
        .filter(t => !picked || !t._provider || t._provider === picked)
        .map(t => ({ ...t, _type: detectType(t.filename, t._db) }));
    window._libLoaded = true;
    buildHashCounts();
    updateDupesBtn();
    document.getElementById('library-tab-count').textContent = _libAll.length || '';
    applyLibFilters();
    _updateCacheBadge(data.cache_status, data.cache_age || 0, data.total, data.loading);
}

async function _pollLibraryUpdate() {
    _libPollTimer = null;
    if (_dmActiveTab !== 'library') return; // stop if user switched tabs
    try {
        const r = await fetch('/debrid_manager/api/library');
        const data = await r.json();
        if (!data.success) { _libPollTimer = setTimeout(_pollLibraryUpdate, 10000); return; }
        const prevCount = _libAll.length;
        _applyLibraryData(data);
        if (data.loading && _dmActiveTab === 'library') {
            // Poll faster while data is actively growing
            const interval = (data.cache_status === 'partial' && data.total > prevCount) ? 5000 : 8000;
            _libPollTimer = setTimeout(_pollLibraryUpdate, interval);
        }
    } catch(e) {
        if (_dmActiveTab === 'library') _libPollTimer = setTimeout(_pollLibraryUpdate, 12000);
    }
}

async function loadLibrary(force) {
    const tbody = document.getElementById('library-tbody');
    if (!window._libLoaded) {
        tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="6">Loading library…</td></tr>';
    }
    if (_libPollTimer) { clearTimeout(_libPollTimer); _libPollTimer = null; }
    try {
        const url = force ? '/debrid_manager/api/library?force=1' : '/debrid_manager/api/library';
        const r = await fetch(url);
        const data = await r.json();
        if (!data.success) throw new Error(data.error);
        _libSelected.clear();
        _applyLibraryData(data);
        if (data.loading) {
            _libPollTimer = setTimeout(_pollLibraryUpdate, data.cache_status === 'partial' ? 5000 : 8000);
        }
    } catch(e) {
        tbody.innerHTML = `<tr class="dm-empty-row"><td colspan="6">Error: ${escHtml(e.message)}</td></tr>`;
    }
}

function applyLibFilters() {
    const re = make_re(document.getElementById('library-filter').value);
    _libFiltered = _libAll.filter(t => {
        if (_libTypeFilter !== 'all' && t._type !== _libTypeFilter) return false;
        if (_libStatusFilter !== 'all') {
            const s = (t.status || '').toLowerCase();
            if (_libStatusFilter === 'error' && !['error','magnet_error','virus','dead'].includes(s)) return false;
            if (_libStatusFilter === 'downloaded' && s !== 'downloaded') return false;
            if (_libStatusFilter === 'downloading' && !['downloading','queued'].includes(s)) return false;
            if (_libStatusFilter === 'no_files' && !['waiting_files_selection','magnet_conversion','compressing','uploading'].includes(s)) return false;
        }
        if (_libDupeFilter === 'hash' && (!t.hash || (_hashCounts[t.hash] || 0) < 2)) return false;
        if (_libDupeFilter === 'title' && (_titleCounts[_titleKey(t)] || 0) < 2) return false;
        if (_libPlexTrash && !_plexTrashFilenames.has((t.filename || '').split('/')[0])) return false;
        if (re && !re.test(`${t.filename||''} ${parseCleanTitle(t.filename)||''} ${t.hash||''} ${t.status||''}`)) return false;
        return true;
    });
    _libFiltered.sort((a, b) => {
        let av = a[_libSort.col], bv = b[_libSort.col];
        if (_libSort.col === 'filename') { av = (av||'').toLowerCase(); bv = (bv||'').toLowerCase(); }
        if (av == null) av = 0; if (bv == null) bv = 0;
        return av < bv ? -_libSort.dir : av > bv ? _libSort.dir : 0;
    });
    _libPage = 1;
    updateLibFilterInfo();
    renderLibPage();
    // Keep the Plex Trash button count in sync with how many RD library entries actually match
    if (_plexTrashBtn) {
        if (_libPlexTrash) {
            _plexTrashBtn.textContent = `Plex Trash (${_libFiltered.length})`;
        } else if (_plexTrashFilenames && _plexTrashFilenames.size > 0) {
            _plexTrashBtn.textContent = `Plex Trash (${_plexTrashFilenames.size})`;
        }
    }
}

function updateLibFilterInfo() {
    const el = document.getElementById('lib-filter-info');
    if (_libFiltered.length !== _libAll.length) {
        el.textContent = `${_libFiltered.length.toLocaleString()} of ${_libAll.length.toLocaleString()} shown`;
    } else {
        el.textContent = `${_libAll.length.toLocaleString()} torrents · ${fmt_bytes(_libAll.reduce((s,t) => s+(t.bytes||0), 0))}`;
    }
}

function renderLibPage() {
    const tbody = document.getElementById('library-tbody');
    const total = _libFiltered.length;
    const totalPages = Math.max(1, Math.ceil(total / LIB_PAGE_SIZE));
    if (_libPage > totalPages) _libPage = totalPages;
    const start = (_libPage - 1) * LIB_PAGE_SIZE;
    const end = Math.min(start + LIB_PAGE_SIZE, total);
    const slice = _libFiltered.slice(start, end);

    const pageInfo = total ? `Page ${_libPage} / ${totalPages}` : '';
    const rangeInfo = total ? `${total.toLocaleString()}${total !== _libAll.length ? ` (filtered from ${_libAll.length.toLocaleString()})` : ''}` : 'No results';
    ['lib-page-info','lib-page-info-bot'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = pageInfo; });
    ['lib-total-info','lib-total-info-bot'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = rangeInfo; });
    ['lib-prev','lib-prev-bot'].forEach(id => { const el = document.getElementById(id); if (el) el.disabled = _libPage <= 1; });
    ['lib-next','lib-next-bot'].forEach(id => { const el = document.getElementById(id); if (el) el.disabled = _libPage >= totalPages; });

    if (!slice.length) {
        tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="6">No results</td></tr>';
        updateSelectBar(); updateLibBatchBtn(); return;
    }

    const rows = slice.map(t => {
        const dupeCount = _hashCounts[t.hash] || 0;
        const titleDupeCount = _titleCounts[_titleKey(t)] || 0;
        const dupeBadge = dupeCount > 1
            ? `<span class="dm-dupe-badge dm-dupe-badge-click" title="Click to see all ${dupeCount} copies" onclick="event.stopPropagation();libOpenDupes('hash',${_jsArg(t.hash)},${_jsArg(t.id)})">${dupeCount}× dupe</span>`
            : titleDupeCount > 1
                ? `<span class="dm-dupe-badge dm-title-dupe-badge dm-dupe-badge-click" title="Click to see all ${titleDupeCount} title matches" onclick="event.stopPropagation();libOpenDupes('title',${_jsArg(_titleKey(t))},${_jsArg(t.id)})">${titleDupeCount}× title</span>`
                : '';
        const typeBadge = `<span class="dm-type-badge dm-type-${t._type}">${t._type === 'tv' ? 'Show' : t._type === 'movie' ? 'Movie' : 'Other'}</span>`;
        const orphanedBadge = (_libPlexTrash && _plexTrashOrphanedFilenames.has((t.filename || '').split('/')[0]))
            ? `<span class="dm-orphaned-badge" title="This file was orphaned in Plex (deletedAt)">Orphaned</span>` : '';
        const cleanTitle = buildCleanTitle(t);
        const ago = timeAgo(t.added);
        const abs = fmt_added_abs(t.added);
        const checked = _libSelected.has(t.id) ? 'checked' : '';
        const safeFile = escHtml(t.filename||'');
        const magnetHref = t.hash ? `magnet:?xt=urn:btih:${t.hash}&dn=${encodeURIComponent(t.filename||'')}` : '#';
        return `<tr data-id="${t.id}" data-hash="${t.hash||''}" style="cursor:pointer" onclick="libOpenDetail(event,'${t.id}')">
            <td class="dm-col-check" onclick="event.stopPropagation()"><input type="checkbox" class="lib-row-check" data-id="${t.id}" ${checked}></td>
            <td class="dm-col-lib-title">
                <div class="dm-lib-title-row">${cleanTitle ? `<span class="dm-clean-title">${escHtml(cleanTitle)}</span>` : ''}${typeBadge}${dupeBadge}${orphanedBadge}</div>
                <div class="dm-lib-filename" title="${safeFile}">${escHtml(t.filename||'—')}</div>
            </td>
            <td class="dm-col-size">${fmt_bytes(t.bytes)}</td>
            <td class="dm-col-status"><span class="dm-status-badge ${status_class(t.status)}">${t.status||'—'}</span></td>
            <td class="dm-col-ts" title="${abs}">${ago}</td>
            <td class="dm-col-actions dm-actions-row" onclick="event.stopPropagation()">
                <a class="dm-btn dm-btn-secondary dm-btn-sm" href="${magnetHref}" title="Open magnet" onclick="_flashCheck(this, this.innerHTML)" ${t.hash ? '' : 'style="pointer-events:none;opacity:.4"'}>
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/></svg>
                </a>
                <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="copyHash(${_jsArg(t.hash||'')}, this)" title="Copy hash">
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
                </button>
                <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="libReinsertTorrent(${_jsArg(t.id)},${_jsArg(t.hash||'')},${_jsArg(t.filename||'')})" title="Reinsert (re-add magnet to debrid)">
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
                </button>
                <button class="dm-btn dm-btn-danger dm-btn-sm" onclick="libDeleteTorrent('${t.id}', this)" title="Delete">
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                </button>
            </td>
        </tr>`;
    });

    tbody.innerHTML = rows.join('');
    const pageIds = slice.map(t => t.id);
    const allChecked = pageIds.length && pageIds.every(id => _libSelected.has(id));
    const someChecked = pageIds.some(id => _libSelected.has(id));
    const sa = document.getElementById('lib-select-all');
    sa.checked = allChecked; sa.indeterminate = !allChecked && someChecked;
    bindLibCheckboxes(); updateSelectBar(); updateLibBatchBtn();
}

function updateSelectBar() {
    const bar = document.getElementById('lib-select-bar');
    const n = _libSelected.size;
    if (!n) { bar.style.display = 'none'; return; }
    bar.style.display = '';
    document.getElementById('lib-select-bar-msg').textContent = `${n.toLocaleString()} torrent${n===1?'':'s'} selected`;
    document.getElementById('lib-select-all-count').textContent = _libFiltered.length.toLocaleString();
    document.getElementById('lib-select-all-pages').style.display = n >= _libFiltered.length ? 'none' : '';
}

function updateLibBatchBtn() {
    const n = _libSelected.size;
    document.getElementById('lib-batch-delete-btn').disabled = n === 0;
    document.getElementById('lib-selected-count').textContent = n;
}

function bindLibCheckboxes() {
    document.querySelectorAll('#library-tbody .lib-row-check').forEach(cb => {
        cb.addEventListener('change', () => {
            cb.checked ? _libSelected.add(cb.dataset.id) : _libSelected.delete(cb.dataset.id);
            const pageIds = [...document.querySelectorAll('#library-tbody .lib-row-check')].map(c => c.dataset.id);
            const all = pageIds.every(id => _libSelected.has(id));
            const some = pageIds.some(id => _libSelected.has(id));
            const sa = document.getElementById('lib-select-all');
            sa.checked = all; sa.indeterminate = !all && some;
            updateSelectBar(); updateLibBatchBtn();
        });
    });
}

document.getElementById('lib-select-all').addEventListener('change', function() {
    document.querySelectorAll('#library-tbody .lib-row-check').forEach(cb => {
        this.checked ? _libSelected.add(cb.dataset.id) : _libSelected.delete(cb.dataset.id);
        cb.checked = this.checked;
    });
    updateSelectBar(); updateLibBatchBtn();
});
document.getElementById('lib-select-all-pages').addEventListener('click', () => {
    _libFiltered.forEach(t => _libSelected.add(t.id)); renderLibPage();
});
document.getElementById('lib-clear-selection').addEventListener('click', () => {
    _libSelected.clear(); renderLibPage();
});
document.querySelectorAll('.dm-type-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        if (!btn.dataset.type) return; // dupe filter buttons have no data-type — handled separately
        document.querySelectorAll('.dm-type-btn[data-type]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active'); _libTypeFilter = btn.dataset.type; applyLibFilters();
    });
});
document.getElementById('library-filter').addEventListener('input', applyLibFilters);

function _setDupeFilter(val) {
    _libDupeFilter = val;
    document.querySelectorAll('.dm-dupe-filter-btn').forEach(b => b.classList.toggle('active', b.dataset.dupe === val));
}
document.querySelectorAll('.dm-dupe-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        const next = _libDupeFilter === btn.dataset.dupe ? 'none' : btn.dataset.dupe;
        _setDupeFilter(next);
        applyLibFilters();
    });
});

const _plexTrashBtn = document.getElementById('lib-plex-trash-btn');
const _plexTrashRescanBtn = document.getElementById('lib-plex-trash-rescan-btn');

async function _loadPlexTrash(force) {
    if (!_plexTrashBtn) return;
    const prev = _plexTrashBtn.textContent;
    _plexTrashBtn.textContent = 'Loading…';
    _plexTrashBtn.disabled = true;
    if (_plexTrashRescanBtn) _plexTrashRescanBtn.disabled = true;
    try {
        const url = force ? '/debrid_manager/api/plex_trash?force=1' : '/debrid_manager/api/plex_trash';
        const r = await fetch(url);
        const data = await r.json();
        if (data.success) {
            _plexTrashFilenames = new Set(data.filenames);
            _plexTrashOrphanedFilenames = new Set(data.orphaned_filenames || []);
            _plexTrashBtn.textContent = `Plex Trash (${data.filenames.length})`;
        } else {
            _libPlexTrash = false;
            _plexTrashBtn.classList.remove('active');
            _plexTrashBtn.textContent = 'Plex Trash';
            if (_plexTrashRescanBtn) _plexTrashRescanBtn.style.display = 'none';
            showPopup({ type: 'error', title: 'Error', message: 'Failed to load Plex trash: ' + (data.error || 'unknown'), autoClose: 5000 });
        }
    } catch(e) {
        _libPlexTrash = false;
        _plexTrashBtn.classList.remove('active');
        _plexTrashBtn.textContent = 'Plex Trash';
        if (_plexTrashRescanBtn) _plexTrashRescanBtn.style.display = 'none';
        showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + e.message, autoClose: 5000 });
    }
    _plexTrashBtn.disabled = false;
    if (_plexTrashRescanBtn) _plexTrashRescanBtn.disabled = false;
    applyLibFilters();
}

if (_plexTrashBtn) {
    _plexTrashBtn.addEventListener('click', async () => {
        _libPlexTrash = !_libPlexTrash;
        _plexTrashBtn.classList.toggle('active', _libPlexTrash);
        if (_plexTrashRescanBtn) _plexTrashRescanBtn.style.display = _libPlexTrash ? '' : 'none';
        if (_libPlexTrash && _plexTrashFilenames.size === 0) {
            await _loadPlexTrash(false);
        } else if (!_libPlexTrash) {
            _plexTrashBtn.textContent = _plexTrashFilenames.size
                ? `Plex Trash (${_plexTrashFilenames.size})`
                : 'Plex Trash';
            applyLibFilters();
        } else {
            applyLibFilters();
        }
    });
}

if (_plexTrashRescanBtn) {
    _plexTrashRescanBtn.addEventListener('click', () => _loadPlexTrash(true));
}

document.querySelectorAll('.dm-lib-stat-card-click').forEach(card => {
    card.addEventListener('click', () => {
        const type = card.dataset.type, filter = card.dataset.filter;
        if (type) {
            document.querySelectorAll('.dm-type-btn').forEach(b => b.classList.remove('active'));
            const btn = document.querySelector(`.dm-type-btn[data-type="${type}"]`);
            if (btn) btn.classList.add('active');
            _libTypeFilter = type; _setDupeFilter('none');
        } else if (filter === 'dupes') {
            _setDupeFilter('hash');
        }
        applyLibFilters();
    });
});
['lib-prev','lib-prev-bot'].forEach(id => document.getElementById(id).addEventListener('click', () => {
    if (_libPage > 1) { _libPage--; renderLibPage(); document.getElementById('library-tab').scrollIntoView({behavior:'smooth',block:'start'}); }
}));
['lib-next','lib-next-bot'].forEach(id => document.getElementById(id).addEventListener('click', () => {
    if (_libPage < Math.ceil(_libFiltered.length / LIB_PAGE_SIZE)) { _libPage++; renderLibPage(); document.getElementById('library-tab').scrollIntoView({behavior:'smooth',block:'start'}); }
}));
document.querySelectorAll('#library-table .dm-sortable').forEach(th => {
    th.addEventListener('click', () => {
        const col = th.dataset.col;
        if (_libSort.col === col) _libSort.dir *= -1; else { _libSort.col = col; _libSort.dir = col === 'added' ? -1 : 1; }
        document.querySelectorAll('#library-table .dm-sortable .sort-icon').forEach(ic => ic.textContent = '↕');
        th.querySelector('.sort-icon').textContent = _libSort.dir === 1 ? '↑' : '↓';
        applyLibFilters();
    });
});
document.getElementById('lib-reload-btn').addEventListener('click', () => { window._libLoaded = false; _libSelected.clear(); loadLibrary(true); });

document.getElementById('lib-delete-dupes-btn').addEventListener('click', async function() {
    const dupeHashes = Object.keys(_hashCounts).filter(h => _hashCounts[h] > 1);
    const toDelete = [];
    dupeHashes.forEach(hash => {
        const copies = _libAll.filter(t => t.hash === hash).sort((a, b) => {
            const da = typeof a.added === 'number' ? a.added : new Date(a.added||0).getTime()/1000;
            const db = typeof b.added === 'number' ? b.added : new Date(b.added||0).getTime()/1000;
            return db - da;
        });
        copies.slice(1).forEach(t => toDelete.push(t.id));
    });
    if (!toDelete.length) return;
    const _btn = this;
    showPopup({
        type: 'confirm',
        title: 'Delete Duplicates',
        message: `Delete ${toDelete.length} duplicate torrent(s)? The newest copy of each hash will be kept.`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            _btn.disabled = true; _btn.innerHTML = '<span class="dm-spinner"></span> Deleting…';
            try {
                const r = await fetch('/debrid_manager/api/torrent/batch-delete', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids:toDelete}) });
                const data = await r.json();
                if (data.success) {
                    _libAll = _libAll.filter(t => !data.deleted.includes(t.id));
                    data.deleted.forEach(id => _libSelected.delete(id));
                    buildHashCounts(); updateDupesBtn(); applyLibFilters();
                    if (data.failed.length) showPopup({ type: 'warning', title: 'Notice', message: `${data.deleted.length} deleted. ${data.failed.length} failed.`, autoClose: 4000 });
                } else { showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + (data.error || 'unknown'), autoClose: 5000 }); }
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + e.message, autoClose: 5000 }); }
            _btn.disabled = false;
        }
    });
});

async function libDeleteTorrent(id, btn) {
    showPopup({
        type: 'confirm',
        title: 'Delete Torrent',
        message: 'Delete this torrent from your debrid account?',
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            btn.disabled = true; btn.innerHTML = '<span class="dm-spinner"></span>';
            try {
                const r = await fetch(`/debrid_manager/api/torrent/${id}`, { method: 'DELETE' });
                const data = await r.json();
                if (data.success) {
                    btn.closest('tr').remove();
                    _libAll = _libAll.filter(t => t.id !== id);
                    _libFiltered = _libFiltered.filter(t => t.id !== id);
                    _libSelected.delete(id);
                    buildHashCounts(); updateDupesBtn(); updateLibFilterInfo(); updateLibBatchBtn(); updateSelectBar();
                } else {
                    btn.disabled = false;
                    btn.innerHTML = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>';
                    showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + (data.error || 'unknown'), autoClose: 5000 });
                }
            } catch(e) { btn.disabled = false; showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + e.message, autoClose: 5000 }); }
        }
    });
}

async function libReinsertTorrent(id, hash, filename) {
    if (!hash) { showPopup({ type: 'warning', title: 'Required', message: 'No hash available for this torrent.', autoClose: 4000 }); return; }
    const btn = event.currentTarget;
    const origHtml = btn.innerHTML;
    btn.disabled = true; btn.innerHTML = '<span class="dm-spinner"></span>';
    try {
        const r = await fetch(`/debrid_manager/api/torrent/${id}/reinsert`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hash, filename, provider_index: _getReinsertProviderIndex() }),
        });
        const data = await r.json();
        if (data.success) {
            btn.innerHTML = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>';
            setTimeout(() => { btn.disabled = false; btn.innerHTML = origHtml; }, 2000);
        } else {
            btn.disabled = false; btn.innerHTML = origHtml;
            showPopup({ type: 'error', title: 'Error', message: 'Reinsert failed: ' + (data.error || 'unknown'), autoClose: 5000 });
        }
    } catch(e) { btn.disabled = false; btn.innerHTML = origHtml; showPopup({ type: 'error', title: 'Error', message: 'Reinsert failed: ' + e.message, autoClose: 5000 }); }
}

// ── Library torrent detail modal ─────────────────────────────────────────────
function libOpenDetail(e, id) {
    // Don't open if user clicked a checkbox, button, or link
    if (e.target.closest('button,a,input')) return;
    const overlay = document.getElementById('lib-detail-overlay');
    const modal   = document.getElementById('lib-detail-modal');
    const body    = document.getElementById('lib-detail-body');
    const title   = document.getElementById('lib-detail-title');
    // Find torrent in local data for immediate title
    const t = _libAll.find(x => x.id === id);
    title.textContent = t ? (parseCleanTitle(t.filename) || t.filename || id) : id;
    body.innerHTML = '<div class="dm-spinner-wrap"><span class="dm-spinner"></span> Loading…</div>';
    overlay.classList.add('open');
    modal.classList.add('open');
    fetch(`/debrid_manager/api/torrent/${id}`)
        .then(r => r.json())
        .then(data => {
            if (!data.success) { body.innerHTML = `<p class="dm-error">Error: ${escHtml(data.error)}</p>`; return; }
            const d = data.torrent;
            const files = Array.isArray(d.files) ? d.files : [];
            const links = Array.isArray(d.links) ? d.links : [];
            const progress = d.progress != null ? `${d.progress}%` : '—';
            const size = fmt_bytes(d.bytes || d.original_filesize || 0);
            const origSize = d.original_filesize ? fmt_bytes(d.original_filesize) : size;
            const added = d.added ? fmt_added_abs(d.added) : '—';
            const statusCls = status_class(d.status);

            // Quick-action bar
            const magnetHref = d.hash ? `magnet:?xt=urn:btih:${d.hash}&dn=${encodeURIComponent(d.filename||'')}` : '#';
            const safeFilename = escHtml(d.filename||'');

            let html = `
<div class="dm-detail-actions">
    <a class="dm-btn dm-btn-secondary dm-btn-sm" href="${magnetHref}" title="Open magnet" ${d.hash?'':'style="opacity:.4;pointer-events:none"'}>
        <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/></svg>
        Magnet
    </a>
    <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="copyHash(${_jsArg(d.hash||'')}, this)">
        <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
        Copy Hash
    </button>
    <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="libCloseDetail();libReinsertTorrent(${_jsArg(id)},${_jsArg(d.hash||'')},${_jsArg(d.filename||'')})">
        <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
        Reinsert
    </button>
    <button class="dm-btn dm-btn-danger dm-btn-sm" onclick="libCloseDetail();document.querySelector('#library-tbody [data-id=\\'${id}\\'] .dm-btn-danger')?.click()">
        <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
        Delete
    </button>
</div>
<table class="dm-detail-table">
    <tr><th>Size</th><td>${size}</td></tr>
    <tr><th>ID</th><td><code>${_esc(d.id||id)}</code></td></tr>
    <tr><th>Hash</th><td><code>${_esc(d.hash||'—')}</code></td></tr>
    <tr><th>Original filename</th><td>${safeFilename}</td></tr>
    <tr><th>Original size</th><td>${origSize}</td></tr>
    <tr><th>Status</th><td><span class="dm-status-badge ${statusCls}">${_esc(d.status||'—')}</span></td></tr>
    <tr><th>Added</th><td>${added}</td></tr>
    <tr><th>Progress</th><td>${progress}</td></tr>
    ${d.seeders != null ? `<tr><th>Seeders</th><td>${d.seeders}</td></tr>` : ''}
    ${d.speed != null ? `<tr><th>Speed</th><td>${fmt_bytes(d.speed)}/s</td></tr>` : ''}
    ${d.message ? `<tr><th>Message</th><td>${_esc(d.message)}</td></tr>` : ''}
</table>`;

            if (files.length) {
                const videoExts = /\.(mkv|mp4|avi|mov|wmv|m4v|ts|m2ts|flv|webm)$/i;
                const videoFiles = files.filter(f => videoExts.test(f.path || f.name || ''));
                const displayFiles = videoFiles.length ? videoFiles : files;
                const hiddenCount = files.length - displayFiles.length;
                html += `<div class="dm-detail-files-header">
    <span>${displayFiles.length} file${displayFiles.length !== 1 ? 's' : ''}${hiddenCount > 0 ? ` <span class="dm-file-hidden-note">(${hiddenCount} non-video hidden)</span>` : ''}</span>
</div>
<div class="dm-detail-files">`;
                displayFiles.forEach(f => {
                    const isVideo = videoExts.test(f.path||'');
                    const fsize = fmt_bytes(f.bytes || f.size || 0);
                    const selected = f.selected === 1;
                    // Premiumize-specific: stream_link, link, video metadata
                    const streamLink = f.stream_link || '';
                    const directLink = f.link || '';
                    const vcodec = f.vcodec ? `<span class="dm-file-badge">${_esc(f.vcodec)}</span>` : '';
                    const acodec = f.acodec ? `<span class="dm-file-badge">${_esc(f.acodec)}</span>` : '';
                    const res    = (f.resx && f.resy) ? `<span class="dm-file-badge">${_esc(f.resx)}×${_esc(f.resy)}</span>` : '';
                    const dur    = f.duration ? `<span class="dm-file-badge">${_esc(f.duration)}</span>` : '';
                    const brate  = f.bitrate  ? `<span class="dm-file-badge">${Math.round(f.bitrate/1000)} kbps</span>` : '';
                    const safeHref = (u) => (typeof u === 'string' && /^(https?:\/\/|\/(?!\/))/i.test(u));
                    const streamBtn = safeHref(streamLink) ? `<a class="dm-btn dm-btn-secondary dm-btn-xs" href="${_esc(streamLink)}" target="_blank" title="Stream">▶</a>` : '';
                    const dlBtn     = safeHref(directLink) ? `<a class="dm-btn dm-btn-secondary dm-btn-xs" href="${_esc(directLink)}" target="_blank" title="Download">↓</a>` : '';
                    html += `<div class="dm-detail-file ${selected ? 'dm-file-selected' : 'dm-file-unselected'}">
    <span class="dm-file-sel-dot" title="${selected ? 'Selected' : 'Not selected'}"></span>
    <span class="dm-file-path">${_esc(f.path||f.name||f.id||'')}</span>
    <span class="dm-file-size">${fsize}</span>
    ${isVideo ? `<span class="dm-file-badge dm-file-badge-video">video</span>` : ''}
    ${vcodec}${acodec}${res}${dur}${brate}
    ${streamBtn}${dlBtn}
</div>`;
                });
                html += '</div>';
            }

            body.innerHTML = html;
        })
        .catch(e => { body.innerHTML = `<p class="dm-error">Failed to load: ${escHtml(e.message)}</p>`; });
}

function libCloseDetail() {
    document.getElementById('lib-detail-overlay').classList.remove('open');
    document.getElementById('lib-detail-modal').classList.remove('open');
}
document.getElementById('lib-detail-close').addEventListener('click', libCloseDetail);
document.getElementById('lib-detail-overlay').addEventListener('click', libCloseDetail);

// ── Dupes modal ───────────────────────────────────────────────────────────────
function libOpenDupes(type, key, currentId) {
    const matches = type === 'hash'
        ? _libAll.filter(t => t.hash === key)
        : _libAll.filter(t => _titleKey(t) === key);

    const overlay = document.getElementById('lib-dupes-overlay');
    const modal   = document.getElementById('lib-dupes-modal');
    const body    = document.getElementById('lib-dupes-body');
    const title   = document.getElementById('lib-dupes-title');

    title.textContent = type === 'hash'
        ? `${matches.length} copies · same hash`
        : `${matches.length} matches · "${key}"`;

    // Sort: current first, then by added desc (newest first)
    const sorted = [...matches].sort((a, b) => {
        if (a.id === currentId) return -1;
        if (b.id === currentId) return 1;
        return (b.added || 0) > (a.added || 0) ? 1 : -1;
    });

    const rows = sorted.map(t => {
        const isCurrent = t.id === currentId;
        const cleanTitle = buildCleanTitle(t);
        return `<div class="dm-dupe-entry${isCurrent ? ' dm-dupe-entry-current' : ''}">
            <div class="dm-dupe-entry-head">
                <span class="dm-dupe-entry-title">${escHtml(cleanTitle || t.filename || '—')}</span>
                ${isCurrent ? '<span class="dm-dupe-entry-tag">current</span>' : ''}
                <div class="dm-dupe-entry-head-right">
                    <span class="dm-status-badge ${status_class(t.status)}">${t.status||'—'}</span>
                    <button class="dm-btn dm-btn-danger dm-dupe-delete-btn" onclick="libDupeDelete('${t.id}', this, '${type}', '${key.replace(/'/g,"\\'")}')"><svg fill="none" stroke="currentColor" viewBox="0 0 24 24" style="width:13px;height:13px"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg> Delete</button>
                </div>
            </div>
            <div class="dm-dupe-entry-meta">
                <span><span class="dm-dupe-label">Size:</span> ${fmt_bytes(t.bytes)}</span>
                <span><span class="dm-dupe-label">Added:</span> ${timeAgo(t.added)}</span>
            </div>
            <div class="dm-dupe-entry-meta"><span class="dm-dupe-label">Hash:</span> <span class="dm-dupe-hash">${t.hash||'—'}</span></div>
            ${t.filename ? `<div class="dm-dupe-entry-meta"><span class="dm-dupe-label">File:</span> <span class="dm-dupe-entry-filename">${escHtml(t.filename)}</span></div>` : ''}
        </div>`;
    });

    body.innerHTML = rows.join('');
    overlay.classList.add('open');
    modal.classList.add('open');
}

async function libDupeDelete(id, btn, type, key) {
    showPopup({
        type: 'confirm',
        title: 'Delete Torrent',
        message: 'Delete this torrent from your debrid account?',
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const origHtml = btn.innerHTML;
            btn.disabled = true; btn.innerHTML = '<span class="dm-spinner"></span>';
            try {
                const r = await fetch(`/debrid_manager/api/torrent/${id}`, { method: 'DELETE' });
                const data = await r.json();
                if (data.success) {
                    _libAll = _libAll.filter(t => t.id !== id);
                    _libFiltered = _libFiltered.filter(t => t.id !== id);
                    _libSelected.delete(id);
                    buildHashCounts(); updateDupesBtn(); updateLibFilterInfo(); updateLibBatchBtn(); updateSelectBar();
                    // Refresh the dupes modal with updated data — close if no more dupes
                    const remaining = type === 'hash'
                        ? _libAll.filter(t => t.hash === key)
                        : _libAll.filter(t => _titleKey(t) === key);
                    if (remaining.length < 2) { libCloseDupes(); applyLibFilters(); }
                    else { libOpenDupes(type, key, null); applyLibFilters(); }
                } else {
                    btn.disabled = false; btn.innerHTML = origHtml;
                    showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + (data.error || 'unknown'), autoClose: 5000 });
                }
            } catch(e) { btn.disabled = false; btn.innerHTML = origHtml; showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + e.message, autoClose: 5000 }); }
        }
    });
}

function libCloseDupes() {
    document.getElementById('lib-dupes-overlay').classList.remove('open');
    document.getElementById('lib-dupes-modal').classList.remove('open');
}
document.getElementById('lib-dupes-close').addEventListener('click', libCloseDupes);
document.getElementById('lib-dupes-overlay').addEventListener('click', libCloseDupes);

document.getElementById('lib-batch-delete-btn').addEventListener('click', async function() {
    const ids = [..._libSelected];
    if (!ids.length) return;
    const _btn = this;
    showPopup({
        type: 'confirm',
        title: 'Delete Torrents',
        message: `Delete ${ids.length} torrent(s)?`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            _btn.disabled = true;
            _btn.innerHTML = '<span class="dm-spinner"></span> Deleting…';
            try {
                const r = await fetch('/debrid_manager/api/torrent/batch-delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ids }),
                });
                const data = await r.json();
                if (data.success) {
                    const deleted = new Set(data.deleted);
                    _libAll = _libAll.filter(t => !deleted.has(t.id));
                    _libFiltered = _libFiltered.filter(t => !deleted.has(t.id));
                    deleted.forEach(id => _libSelected.delete(id));
                    buildHashCounts(); updateDupesBtn(); updateLibFilterInfo(); renderLibPage();
                    if (data.failed.length) showPopup({ type: 'warning', title: 'Notice', message: `${data.deleted.length} deleted. ${data.failed.length} failed.`, autoClose: 4000 });
                } else { showPopup({ type: 'error', title: 'Error', message: 'Batch delete failed: ' + (data.error || 'unknown'), autoClose: 5000 }); }
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Batch delete failed: ' + e.message, autoClose: 5000 }); }
            _btn.innerHTML = `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg> Delete Selected (<span id="lib-selected-count">${_libSelected.size}</span>)`;
            _btn.disabled = _libSelected.size === 0;
        }
    });
});

document.getElementById('lib-batch-reinsert-btn').addEventListener('click', async function() {
    const torrents = [..._libSelected].map(id => {
        const t = _libAll.find(x => x.id === id);
        return t ? { id: t.id, hash: t.hash || '', filename: t.filename || '' } : null;
    }).filter(Boolean);
    if (!torrents.length) return;
    const origHtml = this.innerHTML;
    const _btn = this;
    showPopup({
        type: 'confirm',
        title: 'Reinsert Torrents',
        message: `Reinsert ${torrents.length} torrent(s) to ${_getReinsertProviderName()}?`,
        confirmText: 'Reinsert',
        cancelText: 'Cancel',
        onConfirm: async function() {
            _btn.disabled = true;
            _btn.innerHTML = '<span class="dm-spinner"></span> Reinserting…';
            try {
                const r = await fetch('/debrid_manager/api/torrent/batch-reinsert', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ torrents, provider_index: _getReinsertProviderIndex() }),
                });
                const data = await r.json();
                if (data.success) {
                    const msg = `${data.reinserted.length} reinserted${data.failed.length ? ', ' + data.failed.length + ' failed' : ''}.`;
                    showPopup({ type: 'success', title: 'Success', message: msg, autoClose: 4000 });
                    _libSelected.clear();
                    window._libLoaded = false;
                    loadLibrary(true);
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Batch reinsert failed: ' + (data.error || 'unknown'), autoClose: 5000 });
                }
            } catch(e) {
                showPopup({ type: 'error', title: 'Error', message: 'Batch reinsert failed: ' + e.message, autoClose: 5000 });
            }
            _btn.innerHTML = origHtml;
            _btn.disabled = false;
        }
    });
});
