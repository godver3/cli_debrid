/* Moved from templates/debrid_manager.html. */
// ── Provider label (set from backend) ──────────────────────────────────────
const providerLabel = window.pageData.debrid_manager.providerLabel;
const _allProviders = window.pageData.debrid_manager.allProviders;

// ── Global reinsert provider (persisted in localStorage) ───────────────────
function _getReinsertProviderIndex() {
    const sel = document.getElementById('global-reinsert-provider');
    if (sel) return parseInt(sel.value, 10) || 0;
    const stored = localStorage.getItem('dm_reinsert_provider_index');
    return stored !== null ? parseInt(stored, 10) : 0;
}
function _getReinsertProviderName() {
    const idx = _getReinsertProviderIndex();
    const p = _allProviders.find(p => p.index === idx);
    return p ? p.name : providerLabel;
}

// ── helpers ────────────────────────────────────────────────────────────────
function escHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function _esc(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function fmt_bytes(b) {
    if (!b) return '—';
    const units = ['B','KB','MB','GB','TB'];
    let i = 0;
    let v = b;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(1) + ' ' + units[i];
}

function make_re(raw) {
    if (!raw.trim()) return null;
    try { return new RegExp(raw, 'i'); }
    catch { return new RegExp(raw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'); }
}

function status_class(s) {
    const m = {
        downloading: 'dm-status-downloading',
        downloaded:  'dm-status-downloaded',
        queued:      'dm-status-queued',
        error:       'dm-status-error',
        magnet_error:'dm-status-error',
        virus:       'dm-status-error',
        dead:        'dm-status-error',
    };
    return m[(s||'').toLowerCase()] || 'dm-status-other';
}

// ── shared state (declared early — tab handler runs before section declarations) ──
let _libPollTimer  = null;
let _dmActiveTab   = 'active';
let _pollTimer     = null;
let _pollInterval  = 5000;
let _noChangeCount = 0;
let _bkLoaded      = false;

// ── tabs ───────────────────────────────────────────────────────────────────
document.querySelectorAll('.dm-tab').forEach(btn => {
    btn.addEventListener('click', () => {
        const prevTab = _dmActiveTab;
        _dmActiveTab = btn.dataset.tab;
        document.querySelectorAll('.dm-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.dm-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(btn.dataset.tab + '-tab').classList.add('active');
        sessionStorage.setItem('dm_tab', btn.dataset.tab);
        // Pause active polling when leaving active tab, resume when returning
        if (prevTab === 'active' && btn.dataset.tab !== 'active') {
            clearTimeout(_pollTimer);
            document.getElementById('poll-indicator').classList.remove('dm-poll-active');
        } else if (btn.dataset.tab === 'active' && prevTab !== 'active') {
            _noChangeCount = 0;
            _pollInterval = 5000;
            fetchActive();
        }
        if (btn.dataset.tab === 'history' && !window._historyLoaded) loadHistory();
        if (btn.dataset.tab === 'library' && !window._libLoaded) loadLibrary(false);
        if (btn.dataset.tab === 'maintenance' && !_bkLoaded) loadMaintenance();
        if (btn.dataset.tab === 'usenet' && !window._usenetLoaded) loadUsenetTab();
        if (btn.dataset.tab === 'usenet') afStartPolling();
        if (btn.dataset.tab !== 'usenet') afStopPolling();
        if (btn.dataset.tab === 'reconcile' && !window._rcLoaded) { loadReconcile(false); loadBatteryAudit(false); }
        if (btn.dataset.tab === 'reconcile' && typeof _slInitAudit === 'function') _slInitAudit();
    });
});
const _urlTab = new URLSearchParams(window.location.search).get('tab');
const _savedTab = _urlTab || sessionStorage.getItem('dm_tab');
if (_savedTab) {
    const btn = document.querySelector(`.dm-tab[data-tab="${_savedTab}"]`);
    if (btn) btn.click();
}

// ── ACTIVE TAB ─────────────────────────────────────────────────────────────
let _activeTorrents = [];
let _activeErrors = [];
let _activeNoFiles = [];
let _activeSort = { col: null, dir: 1 };
let _lastActiveJSON = '';
// _pollTimer, _pollInterval, _noChangeCount declared at top

function getActiveDisplayTorrents() {
    const statusVal = document.getElementById('active-status-filter').value;
    if (statusVal === 'error') return _activeErrors;
    if (statusVal === 'no_files') return _activeNoFiles;
    if (statusVal === '') return [..._activeTorrents, ..._activeErrors, ..._activeNoFiles];
    return _activeTorrents;
}

function renderActive(torrents) {
    const tbody = document.getElementById('active-tbody');
    const display = getActiveDisplayTorrents();
    const re = make_re(document.getElementById('active-filter').value);
    const checkboxStates = {};
    tbody.querySelectorAll('input[type=checkbox]').forEach(cb => {
        checkboxStates[cb.dataset.id] = cb.checked;
    });

    const statusVal = document.getElementById('active-status-filter').value;
    const emptyMsg = statusVal === 'error' ? 'No errors' :
                     statusVal === 'no_files' ? 'No torrents awaiting file selection' :
                     'No active downloads';

    if (!display.length) {
        tbody.innerHTML = `<tr class="dm-empty-row"><td colspan="6">${emptyMsg}</td></tr>`;
        document.getElementById('active-tab-count').textContent = '';
        return;
    }

    let sorted = [...display];
    if (_activeSort.col) {
        sorted.sort((a, b) => {
            let av = a[_activeSort.col], bv = b[_activeSort.col];
            if (typeof av === 'string') av = av.toLowerCase();
            if (typeof bv === 'string') bv = bv.toLowerCase();
            return av < bv ? -_activeSort.dir : av > bv ? _activeSort.dir : 0;
        });
    }

    let visible = 0;
    const rows = sorted.map(t => {
        const searchText = `${t.filename || ''} ${t.hash || ''} ${t.status || ''}`;
        const hidden = re && !re.test(searchText);
        if (!hidden) visible++;
        const progress = typeof t.progress === 'number' ? t.progress.toFixed(1) : '—';
        const checked = checkboxStates[t.id] ? 'checked' : '';
        return `<tr data-id="${t.id}" data-search="${searchText.replace(/"/g,'&quot;')}" ${hidden ? 'style="display:none"' : ''}>
            <td class="dm-col-check"><input type="checkbox" class="row-check" data-id="${t.id}" ${checked}></td>
            <td class="dm-col-name" title="${(t.filename||'').replace(/"/g,'&quot;')}">${t.filename || '—'}</td>
            <td class="dm-col-progress">
                <div class="dm-progress-wrap">
                    <div class="dm-progress-bar" style="width:${Math.min(t.progress||0,100)}%"></div>
                    <span class="dm-progress-label">${progress}%</span>
                </div>
            </td>
            <td class="dm-col-status"><span class="dm-status-badge ${status_class(t.status)}">${t.status || '—'}</span></td>
            <td class="dm-col-size">${fmt_bytes(t.bytes)}</td>
            <td class="dm-col-actions">
                <button class="dm-btn dm-btn-danger dm-btn-sm" onclick="deleteTorrent('${t.id}', this)" title="Delete">
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                </button>
            </td>
        </tr>`;
    });

    tbody.innerHTML = rows.join('');
    document.getElementById('active-tab-count').textContent = visible || '';
    bindCheckboxes();
    updateBatchBtn();
}

function updateSlotBadge(active, max) {
    const el = document.getElementById('slot-badge');
    if (active != null && max != null) {
        el.textContent = `${active} / ${max} slots`;
        el.className = 'dm-slot-badge' + (active >= max ? ' dm-slot-full' : '');
    } else {
        el.textContent = '';
    }
}

async function fetchActive() {
    try {
        const r = await fetch('/debrid_manager/api/active');
        const data = await r.json();
        if (!data.success) return;

        const newJSON = JSON.stringify(data.torrents);
        const changed = newJSON !== _lastActiveJSON;
        _activeErrors = data.errors || [];
        _activeNoFiles = data.no_files || [];
        if (changed) {
            _noChangeCount = 0;
            _lastActiveJSON = newJSON;
            _activeTorrents = data.torrents;
        } else {
            _noChangeCount++;
        }
        renderActive(_activeTorrents);
        updateSlotBadge(data.active_count, data.max_downloads);
        adjustPollInterval();
    } catch(e) {
        console.error('Active fetch error:', e);
    }
}

function adjustPollInterval() {
    if (_noChangeCount >= 4) {
        _pollInterval = 30000;
    } else if (_noChangeCount >= 2) {
        _pollInterval = 10000;
    } else {
        _pollInterval = 5000;
    }
    schedulePoll();
}

function schedulePoll() {
    clearTimeout(_pollTimer);
    if (!document.hidden && _dmActiveTab === 'active') {
        _pollTimer = setTimeout(fetchActive, _pollInterval);
        document.getElementById('poll-indicator').classList.add('dm-poll-active');
    } else {
        document.getElementById('poll-indicator').classList.remove('dm-poll-active');
    }
}

document.addEventListener('visibilitychange', () => {
    if (!document.hidden && _dmActiveTab === 'active') {
        _noChangeCount = 0;
        _pollInterval = 5000;
        fetchActive();
    } else {
        clearTimeout(_pollTimer);
    }
});

// sorting
document.querySelectorAll('#active-table .dm-sortable').forEach(th => {
    th.addEventListener('click', () => {
        const col = th.dataset.col;
        if (_activeSort.col === col) {
            _activeSort.dir *= -1;
        } else {
            _activeSort.col = col;
            _activeSort.dir = 1;
        }
        document.querySelectorAll('#active-table .dm-sortable .sort-icon').forEach(ic => ic.textContent = '↕');
        th.querySelector('.sort-icon').textContent = _activeSort.dir === 1 ? '↑' : '↓';
        renderActive(_activeTorrents);
    });
});

// filter + status
document.getElementById('active-filter').addEventListener('input', () => renderActive(_activeTorrents));
document.getElementById('active-status-filter').addEventListener('change', () => renderActive(_activeTorrents));

// checkboxes
function bindCheckboxes() {
    document.querySelectorAll('#active-tbody .row-check').forEach(cb => {
        cb.addEventListener('change', updateBatchBtn);
    });
}

function updateBatchBtn() {
    const checked = document.querySelectorAll('#active-tbody .row-check:checked').length;
    const btn = document.getElementById('batch-delete-btn');
    btn.disabled = checked === 0;
    document.getElementById('selected-count').textContent = checked;
}

document.getElementById('select-all').addEventListener('change', function() {
    document.querySelectorAll('#active-tbody .row-check').forEach(cb => {
        const row = cb.closest('tr');
        if (row.style.display !== 'none') cb.checked = this.checked;
    });
    updateBatchBtn();
});

// single delete
async function deleteTorrent(id, btn) {
    showPopup({
        type: 'confirm',
        title: 'Delete Torrent',
        message: 'Delete this torrent from your debrid account?',
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            btn.disabled = true;
            btn.innerHTML = '<span class="dm-spinner"></span>';
            try {
                const r = await fetch(`/debrid_manager/api/torrent/${id}`, { method: 'DELETE' });
                const data = await r.json();
                if (data.success) {
                    btn.closest('tr').remove();
                    _activeTorrents = _activeTorrents.filter(t => t.id !== id);
                    updateBatchBtn();
                } else {
                    btn.disabled = false;
                    btn.innerHTML = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>';
                    showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + (data.error || 'unknown error'), autoClose: 5000 });
                }
            } catch(e) {
                btn.disabled = false;
                showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + e.message, autoClose: 5000 });
            }
        }
    });
}

// batch delete
document.getElementById('batch-delete-btn').addEventListener('click', async function() {
    const ids = [...document.querySelectorAll('#active-tbody .row-check:checked')].map(cb => cb.dataset.id);
    if (!ids.length) return;
    const _btn = this;
    showPopup({
        type: 'confirm',
        title: 'Delete Torrents',
        message: `Delete ${ids.length} torrent(s) from your debrid account?`,
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
                    data.deleted.forEach(id => {
                        const row = document.querySelector(`#active-tbody tr[data-id="${id}"]`);
                        if (row) row.remove();
                    });
                    _activeTorrents = _activeTorrents.filter(t => !data.deleted.includes(t.id));
                    if (data.failed.length) {
                        showPopup({ type: 'warning', title: 'Notice', message: `${data.deleted.length} deleted. ${data.failed.length} failed.`, autoClose: 4000 });
                    }
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Batch delete failed: ' + (data.error || 'unknown'), autoClose: 5000 });
                }
            } catch(e) {
                showPopup({ type: 'error', title: 'Error', message: 'Batch delete failed: ' + e.message, autoClose: 5000 });
            }

            _btn.innerHTML = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg> Delete Selected (<span id="selected-count">0</span>)';
            updateBatchBtn();
        }
    });
});

// ── HISTORY TAB ────────────────────────────────────────────────────────────
let _historyEntries = [];
window._historyLoaded = false;

async function loadHistory() {
    const tbody = document.getElementById('history-tbody');
    tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="6">Loading…</td></tr>';
    try {
        const r = await fetch('/debrid_manager/api/history');
        const data = await r.json();
        if (!data.success) throw new Error(data.error);
        _historyEntries = data.entries;
        window._historyLoaded = true;
        renderHistory();
    } catch(e) {
        tbody.innerHTML = `<tr class="dm-empty-row"><td colspan="6">Error loading history: ${e.message}</td></tr>`;
    }
}

function renderHistory() {
    const tbody = document.getElementById('history-tbody');
    const re = make_re(document.getElementById('history-filter').value);
    let visible = 0;

    if (!_historyEntries.length) {
        tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="6">No history yet</td></tr>';
        document.getElementById('history-tab-count').textContent = '';
        return;
    }

    const rows = _historyEntries.map(e => {
        const searchText = `${e.label} ${e.torrent_hash} ${e.trigger_source} ${e.rationale}`;
        const hidden = re && !re.test(searchText);
        if (!hidden) visible++;
        const present = e.is_still_present
            ? '<span class="dm-status-badge dm-status-downloaded">Downloaded</span>'
            : '<span class="dm-status-badge dm-status-removed">Removed</span>';
        const trigger_cls = `dm-trigger-${(e.trigger_source||'').replace(/_/g,'-')}`;
        return `<tr data-id="${e.id}" ${hidden ? 'style="display:none"' : ''}>
            <td class="dm-col-ts">${e.timestamp}</td>
            <td class="dm-col-name" title="${(e.label||'').replace(/"/g,'&quot;')}">${e.label || '—'}</td>
            <td class="dm-col-trigger"><span class="dm-trigger-badge ${trigger_cls}">${e.trigger_source||'—'}</span></td>
            <td class="dm-col-rationale">${e.rationale||'—'}</td>
            <td class="dm-col-status">${present}</td>
            <td class="dm-col-actions">
                <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="showDetail(${e.id})">
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                </button>
            </td>
        </tr>`;
    });

    tbody.innerHTML = rows.join('');
    document.getElementById('history-tab-count').textContent = visible || '';
}

document.getElementById('history-filter').addEventListener('input', renderHistory);
document.getElementById('history-reload-btn').addEventListener('click', () => {
    window._historyLoaded = false;
    loadHistory();
});

// ── DETAIL MODAL ───────────────────────────────────────────────────────────
function showDetail(id) {
    const e = _historyEntries.find(x => x.id === id);
    if (!e) return;

    document.getElementById('detail-title').textContent = e.label || 'Torrent Details';

    let item = {};
    try { item = JSON.parse(e.item_data || '{}'); } catch {}
    let trigger = {};
    try { trigger = JSON.parse(e.trigger_details || '{}'); } catch {}
    let meta = {};
    try { meta = JSON.parse(e.additional_metadata || '{}'); } catch {}

    const section = (title, html) => `<div class="dm-detail-section"><h4>${title}</h4>${html}</div>`;
    const dl = (pairs) => '<dl class="dm-detail-dl">' + pairs.filter(([,v])=>v!=null&&v!=='').map(([k,v])=>`<dt>${k}</dt><dd>${v}</dd>`).join('') + '</dl>';
    const pre = (obj) => `<pre class="dm-detail-pre">${JSON.stringify(obj, null, 2)}</pre>`;

    let html = section('Basic Information', dl([
        ['Hash', `<code>${e.torrent_hash}</code>`],
        ['Added', e.timestamp],
        ['Status', e.is_still_present ? '<span class="dm-status-badge dm-status-downloaded">Present</span>' : `<span class="dm-status-badge dm-status-error">Removed</span>`],
        ['Removed At', e.removal_timestamp || ''],
        ['Removal Reason', e.removal_reason || ''],
    ]));

    html += section('Trigger', dl([
        ['Source', `<span class="dm-trigger-badge dm-trigger-${(e.trigger_source||'').replace(/_/g,'-')}">${e.trigger_source}</span>`],
        ['Rationale', e.rationale],
        ['Details', e.trigger_details ? pre(trigger) : ''],
    ]));

    if (e.item_data) {
        html += section('Media Information', dl([
            ['Title', item.title||''],
            ['Year', item.year||''],
            ['Type', item.type||item.media_type||''],
            ['Season', item.season_number!=null ? item.season_number : ''],
            ['Episode', item.episode_number!=null ? item.episode_number : ''],
            ['Version', item.version||''],
            ['Selected File', item.filled_by_file ? `<code>${item.filled_by_file}</code>` : ''],
            ['Torrent Title', item.filled_by_title ? `<code>${item.filled_by_title}</code>` : ''],
            ['TMDB ID', item.tmdb_id||''],
        ]));
    }

    if (trigger.selected_files && trigger.selected_files.length) {
        const _sfVideoExts = /\.(mkv|mp4|avi|mov|wmv|m4v|ts|m2ts|flv|webm)$/i;
        const _sfAll = trigger.selected_files;
        const _sfVideo = _sfAll.filter(f => _sfVideoExts.test(f.path||f.name||''));
        const _sfDisplay = _sfVideo.length ? _sfVideo : _sfAll;
        const rows = _sfDisplay.map(f =>
            `<tr><td><code>${f.path||f.name||''}</code></td><td>${((f.bytes||0)/1024/1024).toFixed(2)} MB</td><td>${f.selected ? '✓' : '✗'}</td></tr>`
        ).join('');
        html += section('Selected Files', `<table class="dm-table dm-table-compact"><thead><tr><th>Path</th><th>Size</th><th>Selected</th></tr></thead><tbody>${rows}</tbody></table>`);
    }

    if (meta.debrid_info) {
        const di = meta.debrid_info;
        html += section('Debrid Information', dl([
            ['Provider', (di.provider||'').toUpperCase()],
            ['Torrent ID', di.torrent_id ? `<code>${di.torrent_id}</code>` : ''],
            ['Status', di.status||''],
            ['Filename', di.filename ? `<code>${di.filename}</code>` : ''],
        ]));
    }

    if (e.additional_metadata && Object.keys(meta).length > 0) {
        html += section('Raw Metadata', pre(meta));
    }

    document.getElementById('detail-body').innerHTML = html;
    document.getElementById('detail-modal').classList.add('open');
    document.getElementById('detail-overlay').classList.add('open');
}

document.getElementById('detail-close').addEventListener('click', closeDetail);
document.getElementById('detail-overlay').addEventListener('click', closeDetail);
function closeDetail() {
    document.getElementById('detail-modal').classList.remove('open');
    document.getElementById('detail-overlay').classList.remove('open');
}

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

function _applyLibraryData(data) {
    _libAll = data.torrents.map(t => ({ ...t, _type: detectType(t.filename, t._db) }));
    window._libLoaded = true;
    buildHashCounts();
    updateDupesBtn();
    document.getElementById('library-tab-count').textContent = data.total || '';
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
        tbody.innerHTML = `<tr class="dm-empty-row"><td colspan="6">Error: ${e.message}</td></tr>`;
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
            ? `<span class="dm-dupe-badge dm-dupe-badge-click" title="Click to see all ${dupeCount} copies" onclick="event.stopPropagation();libOpenDupes('hash','${t.hash}','${t.id}')">${dupeCount}× dupe</span>`
            : titleDupeCount > 1
                ? `<span class="dm-dupe-badge dm-title-dupe-badge dm-dupe-badge-click" title="Click to see all ${titleDupeCount} title matches" onclick="event.stopPropagation();libOpenDupes('title','${_titleKey(t).replace(/'/g,"\\'")}','${t.id}')">${titleDupeCount}× title</span>`
                : '';
        const typeBadge = `<span class="dm-type-badge dm-type-${t._type}">${t._type === 'tv' ? 'Show' : t._type === 'movie' ? 'Movie' : 'Other'}</span>`;
        const orphanedBadge = (_libPlexTrash && _plexTrashOrphanedFilenames.has((t.filename || '').split('/')[0]))
            ? `<span class="dm-orphaned-badge" title="This file was orphaned in Plex (deletedAt)">Orphaned</span>` : '';
        const cleanTitle = buildCleanTitle(t);
        const ago = timeAgo(t.added);
        const abs = fmt_added_abs(t.added);
        const checked = _libSelected.has(t.id) ? 'checked' : '';
        const safeFile = (t.filename||'').replace(/"/g,'&quot;');
        const magnetHref = t.hash ? `magnet:?xt=urn:btih:${t.hash}&dn=${encodeURIComponent(t.filename||'')}` : '#';
        return `<tr data-id="${t.id}" data-hash="${t.hash||''}" style="cursor:pointer" onclick="libOpenDetail(event,'${t.id}')">
            <td class="dm-col-check" onclick="event.stopPropagation()"><input type="checkbox" class="lib-row-check" data-id="${t.id}" ${checked}></td>
            <td class="dm-col-lib-title">
                <div class="dm-lib-title-row">${cleanTitle ? `<span class="dm-clean-title">${cleanTitle}</span>` : ''}${typeBadge}${dupeBadge}${orphanedBadge}</div>
                <div class="dm-lib-filename" title="${safeFile}">${t.filename||'—'}</div>
            </td>
            <td class="dm-col-size">${fmt_bytes(t.bytes)}</td>
            <td class="dm-col-status"><span class="dm-status-badge ${status_class(t.status)}">${t.status||'—'}</span></td>
            <td class="dm-col-ts" title="${abs}">${ago}</td>
            <td class="dm-col-actions dm-actions-row" onclick="event.stopPropagation()">
                <a class="dm-btn dm-btn-secondary dm-btn-sm" href="${magnetHref}" title="Open magnet" onclick="_flashCheck(this, this.innerHTML)" ${t.hash ? '' : 'style="pointer-events:none;opacity:.4"'}>
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/></svg>
                </a>
                <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="copyHash('${t.hash||''}', this)" title="Copy hash">
                    <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
                </button>
                <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="libReinsertTorrent('${t.id}','${t.hash||''}','${(t.filename||'').replace(/'/g,"\\'")}')" title="Reinsert (re-add magnet to debrid)">
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
            if (!data.success) { body.innerHTML = `<p class="dm-error">Error: ${data.error}</p>`; return; }
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
            const safeFilename = (d.filename||'').replace(/"/g,'&quot;');

            let html = `
<div class="dm-detail-actions">
    <a class="dm-btn dm-btn-secondary dm-btn-sm" href="${magnetHref}" title="Open magnet" ${d.hash?'':'style="opacity:.4;pointer-events:none"'}>
        <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/></svg>
        Magnet
    </a>
    <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="copyHash('${d.hash||''}', this)">
        <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
        Copy Hash
    </button>
    <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="libCloseDetail();libReinsertTorrent('${id}','${d.hash||''}','${(d.filename||'').replace(/'/g,"\\'")}')">
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
    <tr><th>ID</th><td><code>${d.id||id}</code></td></tr>
    <tr><th>Hash</th><td><code>${d.hash||'—'}</code></td></tr>
    <tr><th>Original filename</th><td>${safeFilename}</td></tr>
    <tr><th>Original size</th><td>${origSize}</td></tr>
    <tr><th>Status</th><td><span class="dm-status-badge ${statusCls}">${d.status||'—'}</span></td></tr>
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
                    const streamBtn = streamLink ? `<a class="dm-btn dm-btn-secondary dm-btn-xs" href="${_esc(streamLink)}" target="_blank" title="Stream">▶</a>` : '';
                    const dlBtn     = directLink ? `<a class="dm-btn dm-btn-secondary dm-btn-xs" href="${_esc(directLink)}" target="_blank" title="Download">↓</a>` : '';
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
        .catch(e => { body.innerHTML = `<p class="dm-error">Failed to load: ${e.message}</p>`; });
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
                <span class="dm-dupe-entry-title">${cleanTitle || t.filename || '—'}</span>
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
            ${t.filename ? `<div class="dm-dupe-entry-meta"><span class="dm-dupe-label">File:</span> <span class="dm-dupe-entry-filename">${t.filename}</span></div>` : ''}
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

// ── MAINTENANCE TAB (Backup + Cleanup) ──────────────────────────────────────
function _bkPfxLabel(pfx) {
    const map = { rd: 'Real-Debrid', ad: 'AllDebrid', pm: 'Premiumize', tb: 'Torbox', dl: 'Debrid-Link' };
    return map[pfx] || pfx.toUpperCase();
}

function _bkFmtTs(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const now = Date.now();
    const diff = Math.round((now - ts * 1000) / 1000);
    let ago;
    if (diff < 60)       ago = `${diff}s ago`;
    else if (diff < 3600) ago = `${Math.round(diff/60)}m ago`;
    else if (diff < 86400) ago = `${Math.round(diff/3600)}h ago`;
    else                   ago = `${Math.round(diff/86400)}d ago`;
    const abs = d.toLocaleString();
    return `${abs} (${ago})`;
}

function _bkFmtSize(bytes) {
    if (!bytes) return '—';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
    return (bytes/1048576).toFixed(1) + ' MB';
}

function _bkRenderActivity(status) {
    const el = document.getElementById('bk-activity');
    if (!el) return;

    const lastRun     = status.last_run     ? _bkFmtTs(status.last_run)     : 'Never';
    const lastCleanup = status.last_cleanup ? _bkFmtTs(status.last_cleanup) : 'Never';
    const providers   = status.providers || {};
    const cs          = (status.cleanup_stats || {}).last || null;

    // ── Backup section ──
    let backupSection = '';
    if (Object.keys(providers).length) {
        for (const [pfx, slots] of Object.entries(providers)) {
            const label = _bkPfxLabel(pfx);
            backupSection += `<div class="dm-bk-activity-provider">${label} Backup</div>`;
            for (const [slot, info] of Object.entries(slots)) {
                const ts    = info.timestamp ? _bkFmtTs(info.timestamp) : '—';
                const count = info.count != null ? info.count.toLocaleString() + ' torrents' : '—';
                backupSection += `
<div class="dm-bk-activity-row">
    <span class="dm-bk-activity-label"><span class="dm-bk-slot-badge">${slot}</span></span>
    <span class="dm-bk-activity-val">${count} &middot; ${ts}</span>
</div>`;
            }
        }
    } else {
        backupSection = `
<div class="dm-bk-activity-row">
    <span class="dm-bk-activity-label">Last backup run</span>
    <span class="dm-bk-activity-val">${lastRun}</span>
</div>`;
    }

    // ── Cleanup section ──
    let cleanupSection = `<div class="dm-backup-activity-divider"></div>`;
    if (cs) {
        const pfxLabel = _bkPfxLabel(cs.provider);
        const total    = cs.total ?? 0;
        const errs     = cs.deleted_errors  ?? 0;
        const dupes    = cs.deleted_dupes   ?? 0;
        const stalled  = cs.deleted_stalled ?? 0;
        cleanupSection += `
<div class="dm-bk-activity-provider">${pfxLabel} Cleanup &middot; ${_bkFmtTs(cs.timestamp)}</div>
<div class="dm-bk-activity-row">
    <span class="dm-bk-activity-label">Total removed</span>
    <span class="dm-bk-activity-val">${total.toLocaleString()}</span>
</div>
<div class="dm-bk-activity-row">
    <span class="dm-bk-activity-label">Errored</span>
    <span class="dm-bk-activity-val">${errs.toLocaleString()}</span>
</div>
<div class="dm-bk-activity-row">
    <span class="dm-bk-activity-label">Duplicates</span>
    <span class="dm-bk-activity-val">${dupes.toLocaleString()}</span>
</div>
<div class="dm-bk-activity-row">
    <span class="dm-bk-activity-label">Stalled (0%)</span>
    <span class="dm-bk-activity-val">${stalled.toLocaleString()}</span>
</div>`;
    } else {
        cleanupSection += `
<div class="dm-bk-activity-row">
    <span class="dm-bk-activity-label">Last cleanup run</span>
    <span class="dm-bk-activity-val">${lastCleanup}</span>
</div>`;
    }

    el.innerHTML = backupSection + cleanupSection;
}

function _bkRenderSlots(status) {
    const el = document.getElementById('bk-slots');
    if (!el) return;

    const providers = status.providers || {};
    if (!Object.keys(providers).length) {
        el.innerHTML = '<div class="dm-bk-activity-empty">No backup files yet. Run a backup to create them.</div>';
        return;
    }

    let html = '';
    for (const [pfx, slots] of Object.entries(providers)) {
        const label = _bkPfxLabel(pfx);
        html += `<div class="dm-bk-provider-label">${label}</div>`;
        for (const [slot, info] of Object.entries(slots)) {
            if (!info.exists) continue;
            const ts = info.timestamp ? _bkFmtTs(info.timestamp) : '—';
            const count = info.count != null ? info.count.toLocaleString() + ' torrents' : '';
            const size = _bkFmtSize(info.size);
            const filename = `${pfx}_backup_${slot}.json`;
            html += `
<div class="dm-bk-slot-row">
    <div class="dm-bk-slot-row-info">
        <span class="dm-bk-slot-badge">${slot}</span>
        <div class="dm-bk-slot-meta">
            <span class="dm-bk-slot-time">${ts}</span>
            <span class="dm-bk-slot-stats">${count}${count && size !== '—' ? ' · ' : ''}${size !== '—' ? size : ''}</span>
        </div>
    </div>
    <div class="dm-bk-slot-actions">
        <button class="dm-btn dm-btn-secondary dm-btn-sm dm-bk-restore-btn"
                onclick="bkRestore('${filename}', this, true)"
                title="Preview what would be restored without making any changes">
            Dry Run
        </button>
        <button class="dm-btn dm-btn-secondary dm-btn-sm dm-bk-restore-btn"
                onclick="bkRestore('${filename}', this, false)"
                title="Restore from this backup">
            <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
            Restore
        </button>
    </div>
</div>`;
        }
    }
    // Extra files section
    const extras = status.extra_files || [];
    if (extras.length) {
        html += `<div class="dm-bk-section-label">EXTRA FILES</div>`;
        for (const f of extras) {
            const label = f.filename.replace('.json','').replace(/_/g,' ');
            const count = f.count != null ? `${f.count} torrents` : '';
            const size = _bkFmtSize(f.size);
            const date = f.modified ? new Date(f.modified * 1000).toLocaleString() : '';
            html += `<div class="dm-bk-slot-row">
    <div class="dm-bk-slot-meta">
        <span class="dm-bk-slot-label" style="font-size:10px;padding:2px 6px;">${label}</span>
        <span class="dm-bk-slot-date">${date}</span>
        <span class="dm-bk-slot-count">${[count, size].filter(Boolean).join(' · ')}</span>
    </div>
    <div class="dm-bk-slot-actions">
        <button class="dm-btn dm-btn-secondary dm-btn-sm"
                onclick="_xpPreselect('${f.filename}')">
            <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
            Restore
        </button>
    </div>
</div>`;
        }
    }

    el.innerHTML = html || '<div class="dm-bk-activity-empty">No backup files exist yet.</div>';
}

async function loadMaintenance() {
    _bkLoaded = true;
    try {
        const r = await fetch('/debrid_manager/api/backup/status');
        const data = await r.json();
        if (!data.success) throw new Error(data.error);

        // Populate backup settings
        const s = data.settings || {};
        document.getElementById('bk-enabled').checked = !!data.enabled;
        if (s.slot_1d_hours) document.getElementById('bk-slot-1d').value = s.slot_1d_hours;
        if (s.slot_3d_hours) document.getElementById('bk-slot-3d').value = s.slot_3d_hours;
        if (s.slot_7d_hours) document.getElementById('bk-slot-7d').value = s.slot_7d_hours;

        // Populate cleanup settings
        document.getElementById('cl-enabled').checked          = !!s.cleanup_enabled;
        document.getElementById('cl-delete-errors').checked    = s.cleanup_delete_errors !== false;
        document.getElementById('cl-delete-dupes').checked     = s.cleanup_delete_dupes !== false;
        document.getElementById('cl-delete-stalled').checked   = !!s.cleanup_delete_stalled;
        if (s.cleanup_stalled_days) document.getElementById('cl-stalled-days').value = s.cleanup_stalled_days;

        _bkRenderActivity(data);
        _bkRenderSlots(data);
        _updateMaintNotice();
        _xpPopulateFiles(data);
        _xpCheckOnLoad();
    } catch(e) {
        document.getElementById('bk-activity').innerHTML = `<div class="dm-error">Failed to load: ${e.message}</div>`;
    }
    // Load climount URL (non-blocking)
    try {
        const dr = await fetch('/debrid_manager/api/climount/settings');
        const dd = await dr.json();
        if (dd.success) {
            const el = document.getElementById('dcyp-url');
            if (el) el.value = dd.url || '';
        }
    } catch(_) {}
}

function _xpPreselect(filename) {
    const sel = document.getElementById('xp-filename');
    if (sel) sel.value = filename;
    const card = document.getElementById('xp-restore-card');
    if (card) card.scrollIntoView({behavior: 'smooth', block: 'start'});
}

function _xpPopulateFiles(data) {
    const sel = document.getElementById('xp-filename');
    if (!sel) return;
    sel.innerHTML = '<option value="">— Select backup file —</option>';
    const pfxNames = {rd: 'Real-Debrid', ad: 'AllDebrid', tb: 'Torbox', dl: 'Debrid-Link', pm: 'Premiumize'};
    const slotNames = {'1d': 'Daily', '3d': '3-Day', '7d': '7-Day'};
    const providers = data.providers || {};
    for (const [pfx, slots] of Object.entries(providers)) {
        for (const [slot, info] of Object.entries(slots)) {
            if (!info || !info.exists) continue;
            const filename = `${pfx}_backup_${slot}.json`;
            const provName = pfxNames[pfx] || pfx;
            const slotName = slotNames[slot] || slot;
            const count = info.count != null ? `${info.count} torrents` : '';
            const date = info.timestamp ? new Date(info.timestamp * 1000).toLocaleDateString() : '';
            const parts = [provName, slotName, count, date].filter(Boolean);
            const opt = document.createElement('option');
            opt.value = filename;
            opt.textContent = parts.join(' · ');
            sel.appendChild(opt);
        }
    }
    // Extra files (usenet failed/not-found etc.)
    const extras = data.extra_files || [];
    if (extras.length) {
        const grp = document.createElement('optgroup');
        grp.label = 'Extra Files';
        for (const f of extras) {
            const opt = document.createElement('option');
            opt.value = f.filename;
            const count = f.count != null ? `${f.count} torrents` : '';
            const label = f.filename.replace('.json','').replace(/_/g,' ');
            opt.textContent = [label, count].filter(Boolean).join(' · ');
            grp.appendChild(opt);
        }
        sel.appendChild(grp);
    }
}

function _updateMaintNotice() {
    const notice = document.getElementById('maint-scheduler-notice');
    if (!notice) return;
    const backupOn  = document.getElementById('bk-enabled')?.checked;
    const cleanupOn = document.getElementById('cl-enabled')?.checked;
    notice.style.display = (backupOn || cleanupOn) ? '' : 'none';
}

document.getElementById('bk-enabled').addEventListener('change', _updateMaintNotice);
document.getElementById('cl-enabled').addEventListener('change', _updateMaintNotice);

document.getElementById('bk-save-btn').addEventListener('click', async function() {
    const btn = this;
    const msg = document.getElementById('bk-save-msg');
    btn.disabled = true;
    btn.innerHTML = '<span class="dm-spinner"></span>';
    try {
        const payload = {
            enabled:       document.getElementById('bk-enabled').checked,
            slot_1d_hours: parseInt(document.getElementById('bk-slot-1d').value) || 24,
            slot_3d_hours: parseInt(document.getElementById('bk-slot-3d').value) || 72,
            slot_7d_hours: parseInt(document.getElementById('bk-slot-7d').value) || 168,
        };
        const r = await fetch('/debrid_manager/api/backup/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const data = await r.json();
        msg.style.display = '';
        if (data.success) {
            msg.className = 'dm-backup-msg dm-backup-msg-ok';
            msg.textContent = 'Settings saved.';
        } else {
            msg.className = 'dm-backup-msg dm-backup-msg-err';
            msg.textContent = 'Error: ' + (data.error || 'unknown');
        }
        setTimeout(() => { msg.style.display = 'none'; }, 3000);
    } catch(e) {
        msg.style.display = '';
        msg.className = 'dm-backup-msg dm-backup-msg-err';
        msg.textContent = 'Error: ' + e.message;
    }
    btn.disabled = false;
    btn.innerHTML = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg> Save Settings';
});

document.getElementById('bk-run-btn').addEventListener('click', async function() {
    const btn = this;
    const msg = document.getElementById('bk-save-msg');
    btn.disabled = true;
    btn.innerHTML = '<span class="dm-spinner"></span> Running…';
    try {
        const r = await fetch('/debrid_manager/api/backup/run', {method: 'POST'});
        const data = await r.json();
        msg.style.display = '';
        if (data.success) {
            msg.className = 'dm-backup-msg dm-backup-msg-ok';
            msg.textContent = `Backup complete: ${data.count?.toLocaleString() || '?'} torrents backed up.`;
            // Refresh the status
            setTimeout(loadMaintenance, 500);
        } else {
            msg.className = 'dm-backup-msg dm-backup-msg-err';
            msg.textContent = 'Backup failed: ' + (data.message || data.error || 'unknown');
        }
        setTimeout(() => { msg.style.display = 'none'; }, 5000);
    } catch(e) {
        msg.style.display = '';
        msg.className = 'dm-backup-msg dm-backup-msg-err';
        msg.textContent = 'Error: ' + e.message;
    }
    btn.disabled = false;
    btn.innerHTML = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg> Run Now';
});

document.getElementById('cl-save-btn').addEventListener('click', async function() {
    const btn = this;
    const msg = document.getElementById('cl-save-msg');
    btn.disabled = true;
    btn.innerHTML = '<span class="dm-spinner"></span>';
    try {
        const payload = {
            enabled:        document.getElementById('cl-enabled').checked,
            delete_errors:  document.getElementById('cl-delete-errors').checked,
            delete_dupes:   document.getElementById('cl-delete-dupes').checked,
            delete_stalled: document.getElementById('cl-delete-stalled').checked,
            stalled_days:   parseInt(document.getElementById('cl-stalled-days').value) || 3,
        };
        const r = await fetch('/debrid_manager/api/cleanup/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const data = await r.json();
        msg.style.display = '';
        msg.className = data.success ? 'dm-backup-msg dm-backup-msg-ok' : 'dm-backup-msg dm-backup-msg-err';
        msg.textContent = data.success ? 'Settings saved.' : ('Error: ' + (data.error || 'unknown'));
        setTimeout(() => { msg.style.display = 'none'; }, 3000);
    } catch(e) {
        msg.style.display = '';
        msg.className = 'dm-backup-msg dm-backup-msg-err';
        msg.textContent = 'Error: ' + e.message;
    }
    btn.disabled = false;
    btn.innerHTML = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg> Save';
});

document.getElementById('cl-run-btn').addEventListener('click', async function() {
    const btn = this;
    const msg = document.getElementById('cl-save-msg');
    showPopup({
        type: 'confirm',
        title: 'Run Cleanup',
        message: 'Run cleanup now?\n\nThis will delete errored/duplicate/stalled torrents based on your current rules. This cannot be undone.',
        confirmText: 'Run',
        cancelText: 'Cancel',
        onConfirm: async function() {
            btn.disabled = true;
            btn.innerHTML = '<span class="dm-spinner"></span> Running…';
            try {
                const r = await fetch('/debrid_manager/api/cleanup/run', {method: 'POST'});
                const data = await r.json();
                msg.style.display = '';
                if (data.success) {
                    msg.className = 'dm-backup-msg dm-backup-msg-ok';
                    msg.textContent = `Cleanup complete: ${data.total_deleted} removed (errors: ${data.deleted_errors}, dupes: ${data.deleted_dupes}, stalled: ${data.deleted_stalled}).`;
                    setTimeout(loadMaintenance, 500);
                } else {
                    msg.className = 'dm-backup-msg dm-backup-msg-err';
                    msg.textContent = 'Cleanup failed: ' + (data.message || data.error || 'unknown');
                }
                setTimeout(() => { msg.style.display = 'none'; }, 8000);
            } catch(e) {
                msg.style.display = '';
                msg.className = 'dm-backup-msg dm-backup-msg-err';
                msg.textContent = 'Error: ' + e.message;
            }
            btn.disabled = false;
            btn.innerHTML = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0f01-15.357-2m15.357 2H15"/></svg> Run Now';
        }
    });
});

// ── Global reinsert provider save ──────────────────────────────────────────
(function() {
    const sel = document.getElementById('global-reinsert-provider');
    const saveBtn = document.getElementById('global-reinsert-save-btn');
    const msg = document.getElementById('global-reinsert-msg');
    if (!sel || !saveBtn) return;
    // Restore saved value
    const stored = localStorage.getItem('dm_reinsert_provider_index');
    if (stored !== null) sel.value = stored;
    saveBtn.addEventListener('click', function() {
        localStorage.setItem('dm_reinsert_provider_index', sel.value);
        if (msg) {
            msg.style.display = 'block';
            msg.textContent = `Reinsert target set to ${_getReinsertProviderName()}`;
            msg.className = 'dm-backup-msg dm-backup-msg-ok';
            setTimeout(() => { msg.style.display = 'none'; }, 2500);
        }
    });
})();

document.getElementById('dcyp-save-btn').addEventListener('click', async function() {
    const btn = this;
    const msg = document.getElementById('dcyp-msg');
    btn.disabled = true;
    btn.innerHTML = '<span class="dm-spinner"></span>';
    try {
        const url = (document.getElementById('dcyp-url').value || '').trim();
        const r = await fetch('/debrid_manager/api/climount/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url}),
        });
        const data = await r.json();
        msg.style.display = '';
        if (data.success) {
            msg.className = 'dm-backup-msg dm-backup-msg-ok';
            msg.textContent = 'Saved.';
        } else {
            msg.className = 'dm-backup-msg dm-backup-msg-err';
            msg.textContent = 'Error: ' + (data.error || 'unknown');
        }
        setTimeout(() => { msg.style.display = 'none'; }, 3000);
    } catch(e) {
        msg.style.display = '';
        msg.className = 'dm-backup-msg dm-backup-msg-err';
        msg.textContent = 'Error: ' + e.message;
    }
    btn.disabled = false;
    btn.innerHTML = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg> Save';
});

document.getElementById('dcyp-test-btn').addEventListener('click', async function() {
    const btn = this;
    const msg = document.getElementById('dcyp-msg');
    btn.disabled = true;
    btn.innerHTML = '<span class="dm-spinner"></span> Testing…';
    try {
        const r = await fetch('/debrid_manager/api/climount/test', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url: (document.getElementById('dcyp-url').value || '').trim()}),
        });
        const data = await r.json();
        msg.style.display = '';
        if (data.success) {
            msg.className = 'dm-backup-msg dm-backup-msg-ok';
            msg.textContent = `Connected. ${data.count != null ? data.count + ' entries found.' : ''}`;
        } else {
            msg.className = 'dm-backup-msg dm-backup-msg-err';
            msg.textContent = 'Failed: ' + (data.error || 'unknown');
        }
        setTimeout(() => { msg.style.display = 'none'; }, 5000);
    } catch(e) {
        msg.style.display = '';
        msg.className = 'dm-backup-msg dm-backup-msg-err';
        msg.textContent = 'Error: ' + e.message;
    }
    btn.disabled = false;
    btn.innerHTML = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg> Test';
});

async function _xpDoRestore(dryRun) {
    const provider_id  = document.getElementById('xp-provider').value;
    const api_key      = document.getElementById('xp-api-key').value.trim();
    const filename     = document.getElementById('xp-filename').value;
    const cached_only  = document.getElementById('xp-cached-only').checked;
    const msg         = document.getElementById('xp-msg');

    if (!provider_id) { msg.style.display=''; msg.className='dm-backup-msg dm-backup-msg-err'; msg.textContent='Select a target provider.'; setTimeout(()=>msg.style.display='none',4000); return; }
    if (!api_key)     { msg.style.display=''; msg.className='dm-backup-msg dm-backup-msg-err'; msg.textContent='Enter the API key for the target provider.'; setTimeout(()=>msg.style.display='none',4000); return; }
    if (!filename)    { msg.style.display=''; msg.className='dm-backup-msg dm-backup-msg-err'; msg.textContent='Select a backup file.'; setTimeout(()=>msg.style.display='none',4000); return; }

    const providerLabel = document.getElementById('xp-provider').selectedOptions[0]?.text || provider_id;
    const action = dryRun ? 'Preview' : 'Restore';

    showPopup({
        type: 'confirm',
        title: dryRun ? 'Dry Run' : 'Cross-Provider Restore',
        message: dryRun
            ? `Preview restore of "${filename}" to ${providerLabel}?\n\nNo changes will be made.`
            : `Restore "${filename}" to ${providerLabel}?\n\nMissing torrents will be re-added to ${providerLabel}. Your current provider is not affected.`,
        confirmText: action,
        cancelText: 'Cancel',
        onConfirm: async () => {
            const dryBtn = document.getElementById('xp-dry-run-btn');
            const restBtn = document.getElementById('xp-restore-btn');
            if (dryBtn) dryBtn.disabled = true;
            if (restBtn) restBtn.disabled = true;
            msg.style.display = '';
            msg.className = 'dm-backup-msg';
            msg.innerHTML = '<span class="dm-spinner"></span> Starting…';

            const _dryBtnHtml = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg> Dry Run';
            const _restBtnHtml = '<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg> Restore';
            const _resetBtns = () => {
                if (dryBtn) { dryBtn.disabled = false; dryBtn.innerHTML = _dryBtnHtml; }
                if (restBtn) { restBtn.disabled = false; restBtn.innerHTML = _restBtnHtml; }
            };

            try {
                const r = await fetch('/debrid_manager/api/backup/restore-to-provider', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({filename, provider_id, api_key, dry_run: dryRun, cached_only}),
                });
                if (!r.ok) {
                    const ct = r.headers.get('content-type') || '';
                    const errText = ct.includes('json') ? (await r.json()).error : `Server error ${r.status}`;
                    throw new Error(errText + ' — check app logs for details');
                }
                const startData = await r.json();
                if (!startData.success) {
                    if (startData.error && startData.error.includes('already in progress')) {
                        msg.className = 'dm-backup-msg dm-backup-msg-err';
                        msg.textContent = 'A restore is already running. ';
                        const rl = document.createElement('a');
                        rl.href = '#'; rl.textContent = 'Reset stuck state.';
                        rl.style.cssText = 'color:#ff6b35;text-decoration:underline;cursor:pointer';
                        rl.onclick = async (e) => { e.preventDefault(); await fetch('/debrid_manager/api/backup/restore-to-provider/reset', {method:'POST'}); msg.style.display='none'; _resetBtns(); };
                        msg.appendChild(rl);
                        return;
                    }
                    throw new Error(startData.error || 'Failed to start');
                }

                // Poll progress every second
                const _poll = async () => {
                    try {
                        const pr = await fetch('/debrid_manager/api/backup/restore-to-provider/progress');
                        const p = await pr.json();
                        if (p.status === 'running') {
                            const name = p.current_name ? ` · ${p.current_name.slice(0, 55)}` : '';
                            msg.innerHTML = `<span class="dm-spinner"></span> ${p.current} of ${p.total}${name}`;
                            setTimeout(_poll, 1000);
                        } else if (p.status === 'done') {
                            const d = p.result || {};
                            const cachedNote = cached_only ? ' (cached only)' : '';
                            let text, isErr = false;
                            if (d.aborted) {
                                isErr = true;
                                text = `Aborted after 10 consecutive failures${cachedNote}: ${d.added} added, ${d.failed} failed. Reason: ${d.abort_reason}`;
                            } else if (d.added === 0 && d.failed > 0) {
                                isErr = true;
                                const reasons = [...new Set((d.failures || []).slice(0, 3).map(f => f.reason))];
                                text = `Restore failed${cachedNote}: 0 added, ${d.failed} failed, ${d.skipped} already present. Error: ${reasons.join('; ')}`;
                            } else {
                                const failNote = d.failed ? `, ${d.failed} failed` : '';
                                text = dryRun
                                    ? `Dry run${cachedNote}: ${d.added} would be added, ${d.skipped} already present${failNote}.`
                                    : `Restore complete${cachedNote}: ${d.added} added to ${providerLabel}, ${d.skipped} already present${failNote}.`;
                                if (d.failures && d.failures.length) {
                                    const reasons = [...new Set(d.failures.slice(0, 3).map(f => f.reason))];
                                    text += ` Errors: ${reasons.join('; ')}`;
                                }
                                isErr = d.failed > 0;
                            }
                            msg.className = isErr ? 'dm-backup-msg dm-backup-msg-err' : 'dm-backup-msg dm-backup-msg-ok';
                            msg.textContent = text;
                            setTimeout(() => { msg.style.display = 'none'; }, 12000);
                            _resetBtns();
                        } else if (p.status === 'error') {
                            msg.className = 'dm-backup-msg dm-backup-msg-err';
                            msg.textContent = 'Failed: ' + (p.error || 'unknown error');
                            _resetBtns();
                        } else {
                            setTimeout(_poll, 1000);
                        }
                    } catch(_) { setTimeout(_poll, 2000); }
                };
                setTimeout(_poll, 800);
            } catch(e) {
                msg.className = 'dm-backup-msg dm-backup-msg-err';
                msg.textContent = 'Error: ' + e.message;
                _resetBtns();
            }
        }
    });
}

document.getElementById('xp-dry-run-btn').addEventListener('click', () => _xpDoRestore(true));
document.getElementById('xp-restore-btn').addEventListener('click', () => _xpDoRestore(false));

// On maintenance tab load, check if a restore is already running and resume polling
async function _xpCheckOnLoad() {
    try {
        const pr = await fetch('/debrid_manager/api/backup/restore-to-provider/progress');
        const p = await pr.json();
        const msg = document.getElementById('xp-msg');
        if (p.status === 'running') {
            const dryBtn = document.getElementById('xp-dry-run-btn');
            const restBtn = document.getElementById('xp-restore-btn');
            if (dryBtn) dryBtn.disabled = true;
            if (restBtn) restBtn.disabled = true;
            msg.style.display = '';
            msg.className = 'dm-backup-msg';
            const name = p.current_name ? ` · ${p.current_name.slice(0, 55)}` : '';
            msg.innerHTML = `<span class="dm-spinner"></span> ${p.current} of ${p.total}${name}`;
            // Resume polling — reuse the same poll logic by triggering a fake restore start
            const _resumePoll = async () => {
                try {
                    const r2 = await fetch('/debrid_manager/api/backup/restore-to-provider/progress');
                    const p2 = await r2.json();
                    if (p2.status === 'running') {
                        const n = p2.current_name ? ` · ${p2.current_name.slice(0, 55)}` : '';
                        msg.innerHTML = `<span class="dm-spinner"></span> ${p2.current} of ${p2.total}${n}`;
                        setTimeout(_resumePoll, 1000);
                    } else {
                        if (dryBtn) dryBtn.disabled = false;
                        if (restBtn) restBtn.disabled = false;
                        if (p2.status === 'done' && p2.result) {
                            const d = p2.result;
                            let t2, e2 = false;
                            if (d.aborted) {
                                e2 = true; t2 = `Aborted: ${d.added} added, ${d.failed} failed. Reason: ${d.abort_reason}`;
                            } else if (d.added === 0 && d.failed > 0) {
                                e2 = true;
                                const r2 = [...new Set((d.failures||[]).slice(0,3).map(f=>f.reason))];
                                t2 = `Restore failed: 0 added, ${d.failed} failed. Error: ${r2.join('; ')}`;
                            } else {
                                t2 = `Restore complete: ${d.added} added, ${d.skipped} already present${d.failed ? `, ${d.failed} failed` : ''}.`;
                                e2 = d.failed > 0;
                            }
                            msg.className = e2 ? 'dm-backup-msg dm-backup-msg-err' : 'dm-backup-msg dm-backup-msg-ok';
                            msg.textContent = t2;
                        } else if (p2.status === 'error') {
                            msg.className = 'dm-backup-msg dm-backup-msg-err';
                            msg.textContent = 'Failed: ' + (p2.error || 'unknown');
                        }
                    }
                } catch(_) { setTimeout(_resumePoll, 2000); }
            };
            setTimeout(_resumePoll, 1000);
        } else if (p.status === 'error' && p.error === 'A restore is already in progress') {
            // Stale lock — show reset option
            msg.style.display = '';
            msg.className = 'dm-backup-msg dm-backup-msg-err';
            msg.textContent = 'Previous restore state is stuck. ';
            const resetLink = document.createElement('a');
            resetLink.href = '#'; resetLink.textContent = 'Click to reset.';
            resetLink.style.cssText = 'color:#ff6b35;text-decoration:underline;cursor:pointer';
            resetLink.onclick = async (e) => { e.preventDefault(); await fetch('/debrid_manager/api/backup/restore-to-provider/reset', {method:'POST'}); msg.style.display='none'; };
            msg.appendChild(resetLink);
        }
    } catch(_) {}
}

// Auto-reset stale state and show reset link when "already in progress" error appears
const _origXpDoRestore = _xpDoRestore;
// Check on maintenance tab open
document.querySelector('.dm-tab[data-tab="maintenance"]')?.addEventListener('click', () => {
    setTimeout(_xpCheckOnLoad, 500);
});

async function bkRestore(filename, btn, dryRun) {
    const _doRestore = async function() {
        // Disable all restore/dry-run buttons in this row to prevent double-clicks
        const row = btn.closest('.dm-bk-slot-row');
        const rowBtns = row ? row.querySelectorAll('button') : [btn];
        rowBtns.forEach(b => b.disabled = true);
        const origHtml = btn.innerHTML;
        btn.innerHTML = '<span class="dm-spinner"></span>';
        const msg = document.getElementById('bk-save-msg');
        try {
            const r = await fetch('/debrid_manager/api/backup/restore', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({filename, dry_run: dryRun}),
            });
            const data = await r.json();
            msg.style.display = '';
            if (data.success) {
                msg.className = 'dm-backup-msg dm-backup-msg-ok';
                if (dryRun) {
                    const failNote = data.failures && data.failures.length ? `, ${data.failures.length} would fail` : '';
                    msg.textContent = `Dry run: ${data.added} would be added, ${data.skipped} already present${failNote}. No changes made.`;
                } else {
                    const failNote = data.failed ? `, ${data.failed} failed` : '';
                    msg.textContent = `Restore complete: ${data.added} added, ${data.skipped} already present${failNote}.`;
                    if (data.failures && data.failures.length) {
                        console.warn('Restore failures:', data.failures);
                    }
                }
            } else {
                msg.className = 'dm-backup-msg dm-backup-msg-err';
                msg.textContent = (dryRun ? 'Dry run failed: ' : 'Restore failed: ') + (data.message || data.error || 'unknown');
            }
            setTimeout(() => { msg.style.display = 'none'; }, 10000);
        } catch(e) {
            msg.style.display = '';
            msg.className = 'dm-backup-msg dm-backup-msg-err';
            msg.textContent = 'Error: ' + e.message;
        }
        rowBtns.forEach(b => b.disabled = false);
        btn.innerHTML = origHtml;
    };
    if (dryRun) {
        showPopup({
            type: 'confirm',
            title: 'Dry Run',
            message: `Dry run for "${filename}"?\n\nThis will preview what would be restored without making any changes to your debrid library.`,
            confirmText: 'Run Preview',
            cancelText: 'Cancel',
            onConfirm: _doRestore
        });
    } else {
        showPopup({
            type: 'confirm',
            title: 'Restore Backup',
            message: `Restore from "${filename}"?\n\nThis will re-add any torrents missing from your current debrid library. Existing torrents are not affected.`,
            confirmText: 'Restore',
            cancelText: 'Cancel',
            onConfirm: _doRestore
        });
    }
}

// ── USAGE TAB ──────────────────────────────────────────────────────────────
let _usageLoaded = false;
let _usageResetTimer = null;

function _startResetCountdown() {
    if (_usageResetTimer) clearInterval(_usageResetTimer);
    function tick() {
        const now = new Date();
        const utcMidnight = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1));
        const diff = Math.max(0, utcMidnight - now);
        const h = Math.floor(diff / 3600000);
        const m = Math.floor((diff % 3600000) / 60000);
        const s = Math.floor((diff % 60000) / 1000);
        const el = document.getElementById('usage-reset-timer');
        if (el) el.textContent = `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    }
    tick();
    _usageResetTimer = setInterval(tick, 1000);
}

function _fmtBytes(gb) {
    if (gb >= 1000) return (gb / 1000).toFixed(2) + ' TB';
    return gb.toFixed(2) + ' GB';
}

async function loadUsage() {
    if (_usageLoaded) return;
    document.getElementById('usage-loading').style.display = '';
    document.getElementById('usage-content').style.display = 'none';
    document.getElementById('usage-error').style.display = 'none';
    try {
        const r = await fetch('/debrid_manager/api/usage');
        const data = await r.json();
        if (!data.success) throw new Error(data.error || 'Failed to load');

        const u = data.user;
        const tot = data.totals || {};

        // Account stats
        document.getElementById('usage-username').textContent = u.username || '—';

        const premEl = document.getElementById('usage-premium');
        if (u.expiration) {
            const expDate = u.expiration.substring(0, 10);
            const days = u.days_remaining != null ? ` (${u.days_remaining} D)` : '';
            premEl.innerHTML = `<span class="dm-usage-badge dm-usage-badge-green">${expDate}${days}</span>`;
        } else {
            premEl.innerHTML = `<span class="dm-usage-badge dm-usage-badge-red">Inactive</span>`;
        }

        // Points row — hide entirely for providers with no points system (null = hide)
        const pointsRow = document.getElementById('usage-points-row');
        const pointsLabel = document.getElementById('usage-points-label');
        if (u.points === null) {
            if (pointsRow) pointsRow.style.display = 'none';
        } else {
            if (pointsRow) pointsRow.style.display = '';
            if (u.limit_used != null) {
                if (pointsLabel) pointsLabel.textContent = 'Fair-Use Points';
            } else {
                if (pointsLabel) pointsLabel.textContent = 'Fidelity Points';
            }
            document.getElementById('usage-points').innerHTML =
                `<span class="dm-usage-badge dm-usage-badge-blue">${(u.points || 0).toLocaleString()}</span>`;
        }

        // Torbox extras: member since + cooldown
        const createdRow = document.getElementById('usage-created-row');
        const cooldownRow = document.getElementById('usage-cooldown-row');
        if (u.created_at && createdRow) {
            createdRow.style.display = '';
            document.getElementById('usage-created').textContent = u.created_at.substring(0, 10);
        }
        if (u.cooldown_until && cooldownRow) {
            cooldownRow.style.display = '';
            document.getElementById('usage-cooldown').textContent = u.cooldown_until.substring(0, 10);
        }

        // Premiumize extras: cloud storage + fair-use bandwidth
        const spaceRow = document.getElementById('usage-space-row');
        const limitRow = document.getElementById('usage-limit-row');
        if (u.space_used != null && spaceRow) {
            spaceRow.style.display = '';
            document.getElementById('usage-space').textContent = _fmtBytes(u.space_used / (1024 ** 3));
        }
        if (u.limit_used != null && limitRow) {
            limitRow.style.display = '';
            document.getElementById('usage-limit').textContent =
                typeof u.limit_used === 'number' ? u.limit_used.toFixed(2) + ' GB' : u.limit_used;
        }

        // Debrid-Link: live speeds + quota limits
        const dlLimits = data.dl_limits || {};
        const dlLimitsCard = document.getElementById('dl-limits-card');
        const dlQuotaCard  = document.getElementById('dl-quota-card');
        if (Object.keys(dlLimits).length > 0 || data.dl_download_speed != null) {
            if (dlLimitsCard) dlLimitsCard.style.display = '';
            if (dlQuotaCard)  dlQuotaCard.style.display  = '';

            // Live speeds
            document.getElementById('dl-download-speed').textContent = _fmtBytes((data.dl_download_speed || 0) / (1024**3)) + '/s';
            document.getElementById('dl-upload-speed').textContent   = _fmtBytes((data.dl_upload_speed   || 0) / (1024**3)) + '/s';
            document.getElementById('dl-peers').textContent          = (data.dl_peers || 0).toLocaleString();

            // Helper: render a {current, value} limit pair
            function _dlLimitRow(elId, rowId, lbl, data, fmt) {
                const row = document.getElementById(rowId);
                const el  = document.getElementById(elId);
                if (!row || !el || !data) return;
                row.style.display = '';
                const cur = fmt(data.current != null ? data.current : 0);
                const max = fmt(data.value   != null ? data.value   : 0);
                el.textContent = `${cur} / ${max}`;
            }
            function _fmtBytes2(bytes) {
                if (!bytes) return '0 B';
                const gb = bytes / (1024**3);
                if (gb >= 1000) return (gb/1000).toFixed(1) + ' TB';
                if (gb >= 1)    return gb.toFixed(1) + ' GB';
                return (bytes / (1024**2)).toFixed(1) + ' MB';
            }
            function _fmtNum(n) { return (n || 0).toLocaleString(); }

            _dlLimitRow('dl-usage',      'dl-usage-row',      'Usage',            dlLimits.usagePercent,      n => n + '%');
            _dlLimitRow('dl-active',     'dl-active-row',     'Active',           dlLimits.dayCount,          _fmtNum);
            _dlLimitRow('dl-day-size',   'dl-day-size-row',   'Data/day',         dlLimits.daySize,           _fmtBytes2);
            _dlLimitRow('dl-day-count',  'dl-day-count-row',  'Torrents/day',     dlLimits.dayTorrentCount,   _fmtNum);
            _dlLimitRow('dl-month-size', 'dl-month-size-row', 'Data/30d',         dlLimits.monthSize,         _fmtBytes2);
            _dlLimitRow('dl-month-count','dl-month-count-row','Torrents/30d',     dlLimits.monthTorrentCount, _fmtNum);

            // Reset timer
            const resetSecs = dlLimits.nextResetSeconds;
            if (resetSecs && resetSecs.value > 0) {
                const hrs = Math.floor(resetSecs.value / 3600);
                const mins = Math.floor((resetSecs.value % 3600) / 60);
                document.getElementById('usage-reset-timer').textContent = `~${hrs}h ${mins}m`;
            }

            // Render any remaining unknown limit fields generically
            const knownKeys = new Set(['usagePercent','nextResetSeconds','dayCount','daySize','dayTorrentCount','monthSize','monthTorrentCount']);
            const extraEl = document.getElementById('dl-extra-limits');
            if (extraEl) {
                let extraHtml = '';
                for (const [k, v] of Object.entries(dlLimits)) {
                    if (knownKeys.has(k) || !v || typeof v !== 'object') continue;
                    const label = k.replace(/([A-Z])/g, ' $1').trim();
                    const cur = v.current != null ? v.current : '—';
                    const max = v.value   != null ? v.value   : '—';
                    extraHtml += `<div class="dm-usage-stat-row"><span class="dm-usage-stat-label">${_esc(label)}</span><span class="dm-usage-stat-val">${_esc(String(cur))} / ${_esc(String(max))}</span></div>`;
                }
                extraEl.innerHTML = extraHtml;
            }

            // Hide the quota card entirely if no rows ended up visible
            if (dlQuotaCard) {
                const hasVisibleRows = dlQuotaCard.querySelectorAll('.dm-usage-stat-row[style*="display: none"]').length <
                                       dlQuotaCard.querySelectorAll('.dm-usage-stat-row').length ||
                                       (dlQuotaCard.querySelector('#dl-extra-limits') || {}).innerHTML;
                const anyVisible = [...dlQuotaCard.querySelectorAll('.dm-usage-stat-row')].some(
                    r => r.style.display !== 'none'
                ) || (extraEl && extraEl.innerHTML.trim().length > 0);
                dlQuotaCard.style.display = anyVisible ? '' : 'none';
            }
        }

        document.getElementById('usage-slots').textContent =
            tot.max_slots ? `${tot.active_slots} / ${tot.max_slots}` : '—';

        document.getElementById('usage-alltime').textContent =
            _fmtBytes(tot.all_time_gb || 0);

        // Today's usage bar
        const t = data.today;
        const pct = Math.min(100, (t.downloaded_gb / t.limit_gb) * 100);
        const bar = document.getElementById('usage-bar');
        bar.style.width = pct + '%';
        bar.className = 'dm-usage-bar' + (pct > 90 ? ' dm-usage-bar-warn' : '');
        document.getElementById('usage-today-text').textContent =
            `${_fmtBytes(t.downloaded_gb)} / ${_fmtBytes(t.limit_gb)}`;
        document.getElementById('usage-today-date').textContent = `UTC date: ${t.date}`;

        // Traffic history — only available for providers that track bandwidth (e.g. Real-Debrid)
        const list = document.getElementById('usage-history-list');
        const usageRight = document.querySelector('.dm-usage-right');
        const todayCard = document.getElementById('usage-today-card');
        const historyCard = document.getElementById('usage-history-card');
        const hasDlLimits = Object.keys(dlLimits).length > 0 || data.dl_download_speed != null;

        if (!data.has_traffic_history) {
            // Provider doesn't track daily traffic history
            if (todayCard) todayCard.style.display = 'none';
            if (historyCard) historyCard.style.display = 'none';
            // Show right column only if DL limits are present to fill it
            if (usageRight) usageRight.style.display = hasDlLimits ? '' : 'none';
            // Show all-time if available (e.g. Torbox total_bytes_downloaded), else N/A
            const allTimeGb = tot.all_time_gb || 0;
            document.getElementById('usage-alltime').textContent = allTimeGb > 0 ? _fmtBytes(allTimeGb) : 'N/A';
            document.getElementById('usage-reset-timer').textContent = 'N/A';
        } else {
            if (usageRight) usageRight.style.display = '';
            if (todayCard) todayCard.style.display = '';
            if (historyCard) historyCard.style.display = '';
            let tableRows = '';
            for (const day of data.history) {
                const hosters = day.hosters || [];
                if (hosters.length > 0) {
                    hosters.forEach((h, i) => {
                        tableRows += `<tr>
                            <td class="dm-usage-th-date">${i === 0 ? day.date : ''}</td>
                            <td class="dm-usage-th-hoster">${h.name}</td>
                            <td class="dm-usage-th-total">${_fmtBytes(h.gb)}</td>
                        </tr>`;
                    });
                } else {
                    tableRows += `<tr>
                        <td class="dm-usage-th-date">${day.date}</td>
                        <td class="dm-usage-th-hoster">—</td>
                        <td class="dm-usage-th-total">${_fmtBytes(day.gb)}</td>
                    </tr>`;
                }
            }
            list.innerHTML = `<table class="dm-usage-compact-table">
                <thead><tr><th>Date</th><th>Hoster</th><th>Used</th></tr></thead>
                <tbody>${tableRows}</tbody>
            </table>`;
        }

        document.getElementById('usage-loading').style.display = 'none';
        document.getElementById('usage-content').style.display = '';
        _usageLoaded = true;
        _startResetCountdown();
    } catch(e) {
        document.getElementById('usage-loading').style.display = 'none';
        const err = document.getElementById('usage-error');
        err.textContent = 'Failed to load usage: ' + e.message;
        err.style.display = '';
    }
}

// Load usage when tab is clicked (lazy)
document.querySelectorAll('.dm-tab[data-tab="usage"]').forEach(btn => {
    btn.addEventListener('click', loadUsage);
});
// If usage tab is the active tab on load, load immediately
if (_dmActiveTab === 'usage') loadUsage();

// ── USENET TAB ─────────────────────────────────────────────────────────────
window._usenetLoaded = false;
let _usenetActivityOffset = 0;
let _usenetActivityTotal = 0;
const _usenetActivityLimit = 25;
let _usenetActivityFilter = null;
let _usenetRepairPollTimer = null;
let _usenetInflightPollTimer = null;

async function loadUsenetTab() {
    window._usenetLoaded = true;
    document.getElementById('usenet-loading').style.display = 'block';
    document.getElementById('usenet-content').style.display = 'none';
    document.getElementById('usenet-error').style.display = 'none';
    try {
        const r = await fetch('/debrid_manager/api/usenet/repair/stats');
        const d = await r.json();
        if (!d.success) throw new Error(d.error || 'Failed to load stats');
        _usenetRenderStats(d.health || {}, d.activity || {}, d.versions || [], d.repair_version || '');
        document.getElementById('usenet-loading').style.display = 'none';
        document.getElementById('usenet-content').style.display = 'block';
        await Promise.all([usenetLoadActivity(null), usenetLoadInFlight()]);
        // Only start polling if a repair is already in progress
        _usenetMaybeStartPolling();
        // Check if cli_mount sweep is already running and start scan status polling if so
        _usenetScanPoll();
    } catch(e) {
        document.getElementById('usenet-loading').style.display = 'none';
        document.getElementById('usenet-error').style.display = 'block';
        document.getElementById('usenet-error').textContent = 'Failed to load: ' + e.message;
    }
}

async function _usenetMaybeStartPolling() {
    try {
        const r = await fetch('/debrid_manager/api/usenet/repair/progress');
        const d = await r.json();
        if (d.is_running || d.status === 'running') {
            document.getElementById('usenet-repair-running').style.display = 'block';
            document.getElementById('usenet-repair-status-text').textContent = 'Repair running…';
            document.getElementById('usenet-repair-bar').style.width = '60%';
            const fixBtn = document.getElementById('usenet-fix-btn');
            if (fixBtn) { fixBtn.disabled = true; fixBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin" style="color:#e8601c"></i> Repairing…'; }
            _usenetPollRepairProgress();
        }
    } catch(e) { /* ignore */ }
}

function _usenetRenderStats(health, activity, versions, savedVersion) {
    // Populate repair version dropdown and restore saved value
    const sel = document.getElementById('usenet-version-select');
    if (sel && versions && versions.length) {
        sel.innerHTML = '<option value="">Item\'s version</option>' +
            versions.map(v => `<option value="${_esc(v)}"${v === savedVersion ? ' selected' : ''}>${_esc(v)}</option>`).join('');
    } else if (sel && savedVersion === '') {
        sel.value = '';
    }
    const healthy = health.healthy || 0;
    const broken  = health.broken  || 0;
    const unknown = health.unknown || 0;
    document.getElementById('usenet-stat-healthy').textContent  = healthy.toLocaleString();
    document.getElementById('usenet-stat-broken').textContent   = broken;
    document.getElementById('usenet-stat-unknown').textContent  = unknown.toLocaleString();
    document.getElementById('usenet-stat-repaired').textContent = activity.replaced  || 0;
    document.getElementById('usenet-stat-notfound').textContent = (activity.not_found || 0) + (activity.plex_deleted || 0);
    const badge = document.getElementById('usenet-broken-badge');
    const tabBadge = document.getElementById('usenet-tab-badge');
    if (broken > 0) {
        badge.textContent = broken;
        badge.style.display = 'inline-block';
        tabBadge.textContent = broken;
        tabBadge.style.display = 'inline-flex';
    } else {
        badge.style.display = 'none';
        tabBadge.style.display = 'none';
    }
}

async function usenetLoadInFlight() {
    const section = document.getElementById('usenet-inflight-section');
    const tbody = document.getElementById('usenet-inflight-tbody');
    const badge = document.getElementById('usenet-inflight-badge');
    if (!section || !tbody) return;
    try {
        const r = await fetch('/debrid_manager/api/usenet/repair/in_flight');
        const d = await r.json();
        if (!d.success) throw new Error(d.error || 'Failed to load in-flight repairs');
        const items = d.items || [];
        if (badge) badge.textContent = String(items.length);
        if (!items.length) {
            section.style.display = 'none';
            if (_usenetInflightPollTimer) {
                clearInterval(_usenetInflightPollTimer);
                _usenetInflightPollTimer = null;
            }
            tbody.innerHTML = '';
            return;
        }
        section.style.display = 'block';
        if (!_usenetInflightPollTimer) {
            _usenetInflightPollTimer = setInterval(usenetLoadInFlight, 30000);
        }
        let html = '';
        items.forEach((item, idx) => {
            const bg = idx % 2 === 0 ? 'transparent' : 'rgba(128,128,128,0.04)';
            const title = item.item_title || item.old_entry_name || `#${item.cli_debrid_id}`;
            const ep = item.season_number ? ` S${String(item.season_number).padStart(2,'0')}E${String(item.episode_number||0).padStart(2,'0')}` : '';
            const stage = item.stage_label || item.status || '—';
            const candidate = item.candidate_title || item.candidate_info_hash || '—';
            const lastError = item.last_error || '—';
            const nextRetry = item.next_attempt_at
                ? new Date(item.next_attempt_at + (String(item.next_attempt_at).endsWith('Z') ? '' : 'Z')).toLocaleString()
                : 'Now / ASAP';
            html += `<tr style="border-bottom:1px solid var(--border-color,#2a2a2a);background:${bg}">
                <td style="padding:8px 12px">${_esc(title)}${ep ? `<br><span style="font-size:11px;color:#888">item ${item.cli_debrid_id}</span>` : ''}</td>
                <td style="padding:8px 12px;color:#ffaa44;font-size:12px">${_esc(stage)}</td>
                <td style="padding:8px 12px;font-size:12px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${_esc(candidate)}">${_esc(candidate)}</td>
                <td style="padding:8px 12px;font-size:11px;color:#aaa;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${_esc(lastError)}">${_esc(lastError)}</td>
                <td style="padding:8px 12px;font-size:11px;color:#888;white-space:nowrap">${_esc(nextRetry)}</td>
                <td style="padding:8px 12px;text-align:center;white-space:nowrap">
                    <button class="dm-btn dm-btn-secondary dm-btn-xs" onclick="usenetCancelRepair(${item.id}, 'cancel')">Cancel</button>
                    <button class="dm-btn dm-btn-secondary dm-btn-xs" style="margin-left:6px" onclick="usenetCancelRepair(${item.id}, 'wanted')">Send to Wanted</button>
                </td>
            </tr>`;
        });
        tbody.innerHTML = html;
    } catch (e) {
        console.error('usenetLoadInFlight', e);
    }
}

async function usenetCancelRepair(repairId, action) {
    const label = action === 'wanted' ? 'cancel this repair and send the item back to Wanted' : 'cancel this in-flight repair';
    if (!confirm(`Are you sure you want to ${label}?`)) return;
    try {
        const r = await fetch('/debrid_manager/api/usenet/repair/cancel', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({repair_id: repairId, action}),
        });
        const d = await r.json();
        if (!d.success) throw new Error((d.result && d.result.message) || d.error || 'Cancel failed');
        await Promise.all([usenetLoadInFlight(), usenetLoadActivity(document.getElementById('usenet-filter-select')?.value || null)]);
    } catch (e) {
        alert('Cancel failed: ' + e.message);
    }
}

async function usenetLoadActivity(filter) {
    _usenetActivityFilter = filter || null;
    _usenetActivityOffset = 0;
    // Sync the select dropdown
    const sel = document.getElementById('usenet-filter-select');
    if (sel) sel.value = filter || '';
    await _usenetFetchActivity();
}

async function usenetClearActivity() {
    if (!confirm('Clear the entire NZB repair activity log?\n\nThis only deletes the log history — it does not touch any media, Plex entries, or provider jobs.\n\nContinue?')) return;
    try {
        const r = await fetch('/debrid_manager/api/usenet/repair/activity/clear', {method: 'POST'});
        const d = await r.json();
        if (d.success) {
            _usenetActivityOffset = 0;
            await _usenetFetchActivity();
        } else {
            alert('Failed to clear activity log: ' + (d.error || 'unknown error'));
        }
    } catch(e) {
        console.error('[Usenet] activity clear error', e);
        alert('Failed to clear activity log');
    }
}

async function _usenetFetchActivity() {
    const container = document.getElementById('usenet-activity-container');
    const pagination = document.getElementById('usenet-activity-pagination');
    container.innerHTML = '<div style="color:#666;padding:20px 0">Loading activity…</div>';
    const url = `/debrid_manager/api/usenet/repair/activity?limit=${_usenetActivityLimit}&offset=${_usenetActivityOffset}` +
        (_usenetActivityFilter ? `&outcome=${_usenetActivityFilter}` : '');
    try {
        const r = await fetch(url);
        const d = await r.json();
        if (!d.success) return;
        _usenetActivityTotal = d.total;
        _usenetRenderActivity(d.rows, container, pagination);
    } catch(e) { console.error('[Usenet] activity fetch error', e); }
}

function _renderActivityBadge(nzb_id) {
    const isDebrid = (nzb_id || '').startsWith('debrid:');
    return isDebrid
        ? '<span class="dr-proto-badge dr-proto-debrid">DEBRID</span>'
        : '<span class="dr-proto-badge dr-proto-nzb">NZB</span>';
}

function _buildOutcomeTooltip(r) {
    const lines = [];
    const triggerMap = {
        'auto_detected':     'Auto-detected by health check',
        'scheduled':         'Scheduled repair task',
        'manual':            'Manually triggered',
        'cache_warm_error':  'Cache warm error (file unreadable)',
        'article_not_found': 'ARTICLE_NOT_FOUND in usenet log',
    };
    const trigger = triggerMap[r.triggered_by] || (r.triggered_by || 'Unknown trigger');
    lines.push('Trigger: ' + trigger);
    if (r.repair_attempts > 0) lines.push('Attempts: ' + r.repair_attempts);
    switch (r.outcome) {
        case 'submission_failed':
            lines.push('A replacement NZB was found but failed to submit to cli_mount.');
            lines.push('Item kept in Collected state — will retry with backoff.');
            break;
        case 'not_found':
            lines.push('No matching CLI DB record found for this entry.');
            lines.push('Entry was an orphan in cli_mount with no DB record.');
            break;
        case 'no_replacement':
            lines.push('Broken entry detected but no replacement NZB could be found on indexers.');
            lines.push('Item moved to Wanted for re-scrape. Will retry with backoff.');
            if (r.next_repair_at) lines.push('Next retry: ' + new Date(r.next_repair_at + (r.next_repair_at.endsWith('Z') ? '' : 'Z')).toLocaleString());
            break;
        case 'plex_deleted':
            lines.push('File was unreadable — deleted from Plex and cli_mount.');
            lines.push('Item moved to Wanted for fresh scrape and re-download.');
            break;
        case 'replaced':
            lines.push('Broken file successfully replaced with a new NZB download.');
            if (r.replacement_title) lines.push('New: ' + r.replacement_title);
            break;
        case 'replacement_pending':
            lines.push('Replacement submitted — waiting for cli_mount verification before old file cleanup.');
            break;
        case 'reinserted':
            lines.push('cli_mount link resolver re-triggered for this entry.');
            break;
        case 'cancelled':
            lines.push('Playback repair was cancelled manually.');
            lines.push('The item was left on its current source unless you chose Send to Wanted.');
            break;
        case 'skipped_junk_source':
            lines.push('File is junk (trailer/sample/wrong size) — repair skipped intentionally.');
            lines.push('No replacement scrape, no provider submit, and no item state change.');
            if (r.replacement_title) lines.push('Reason: ' + r.replacement_title);
            break;
        case 'skipped_max_attempts':
            if (r.current_replacement_title) {
                lines.push('Maximum repair attempts were reached, but the item has since been resolved (retried or fixed another way).');
            } else {
                lines.push('Maximum repair attempts reached — requires manual intervention.');
                if (r.repair_attempts) lines.push('Attempts made: ' + r.repair_attempts);
            }
            break;
        case 'manual_retry':
            lines.push('Manually retried after exhausting automatic repair attempts.');
            if (r.current_replacement_title) {
                lines.push('A replacement was found and collected successfully.');
            } else {
                lines.push('Item moved to Wanted for a fresh scrape.');
                if (r.current_item_state) lines.push('Current state: ' + r.current_item_state);
            }
            break;
    }
    return lines.join('\n').replace(/"/g, '&quot;');
}

function _renderActivityCandidateCell(currentTitle, replacedTitle, nzb_id) {
    const badge = _renderActivityBadge(nzb_id);
    const current = _esc(currentTitle || '—');
    if (replacedTitle && replacedTitle !== '—') {
        // Show stacked: current (grey) on top, replacement (green) below — upgrade hub style
        return `<div style="display:flex;flex-direction:column;gap:3px;min-width:0">
            <div style="font-size:12px;color:#666;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${current}">${current} ${badge}</div>
            <div style="font-size:12px;color:#00d4aa;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${_esc(replacedTitle)}">${_esc(replacedTitle)}</div>
        </div>`;
    }
    return `<div style="font-size:12px;color:#666;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${current}">${current} ${badge}</div>`;
}

function _usenetRenderActivity(rows, container, pagination) {
    if (!rows || rows.length === 0) {
        container.innerHTML = '<p style="color:var(--text-secondary,#666);padding:20px 0">No activity recorded yet.</p>';
        if (pagination) pagination.style.display = 'none';
        return;
    }

    let html = `<table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead>
            <tr style="border-bottom:2px solid var(--border-color,#333);text-align:left;">
                <th style="padding:8px 12px;white-space:nowrap;color:var(--text-secondary,#888);font-weight:600;width:145px;">Date / Time</th>
                <th style="padding:8px 12px;color:var(--text-secondary,#888);font-weight:600;width:160px;">Title</th>
                <th style="padding:8px 12px;color:var(--text-secondary,#888);font-weight:600;">Broken / Replaced</th>
                <th style="padding:8px 12px;color:var(--text-secondary,#888);font-weight:600;width:90px;">Outcome</th>
            </tr>
        </thead>
        <tbody>`;

    rows.forEach((r, idx) => {
        const bg = idx % 2 === 0 ? 'transparent' : 'rgba(128,128,128,0.04)';
        const resolvedElsewhere = (r.outcome === 'manual_retry' || r.outcome === 'skipped_max_attempts') && !!r.current_replacement_title;
        const manualRetryResolved = r.outcome === 'manual_retry' && !!r.manual_retry_resolved;
        const isJunk = r.outcome === 'skipped_junk_source';
        const isCancelled = r.outcome === 'cancelled';
        const outcomeColor = isJunk ? '#888'
            : isCancelled ? '#888'
            : r.outcome === 'replaced' ? '#00d4aa' : r.outcome === 'replacement_pending' ? '#ffaa44' : r.outcome === 'not_found' ? '#ffaa44' : r.outcome === 'plex_deleted' ? '#4488ff' : r.outcome === 'reinserted' ? '#a78bfa' : resolvedElsewhere ? '#00d4aa' : r.outcome === 'manual_retry' ? '#4488ff' : '#ff4444';
        const outcomeLabel = isJunk ? 'Junk'
            : isCancelled ? 'Cancelled'
            : r.outcome === 'replaced' ? 'Replaced' : r.outcome === 'replacement_pending' ? 'Pending verification' : r.outcome === 'not_found' ? 'Not Found' : r.outcome === 'plex_deleted' ? 'Plex Deleted' : r.outcome === 'reinserted' ? 'Re-inserted' : manualRetryResolved ? 'Re-collected' : (resolvedElsewhere && r.outcome === 'manual_retry') ? 'Retry Succeeded' : (resolvedElsewhere && r.outcome === 'skipped_max_attempts') ? 'Resolved' : r.outcome === 'manual_retry' ? 'Retrying…' : r.outcome === 'skipped_max_attempts' ? 'Max Attempts' : (r.outcome || '—');
        const outcomeTooltip = _buildOutcomeTooltip(r);
        const epSub = r.season_number ? `<br><span style="font-size:11px;color:#888">S${String(r.season_number).padStart(2,'0')}E${String(r.episode_number||0).padStart(2,'0')}</span>` : '';
        const activityTime = r.updated_at || r.created_at;
        const ts = activityTime ? new Date(activityTime + (activityTime.endsWith('Z') ? '' : 'Z')).toLocaleString() : '—';
        const brokenTitle = r.broken_nzb_title || r.broken_nzb_id || '—';
        const replTitle = isJunk
            ? null
            : (resolvedElsewhere && r.current_replacement_title
            ? r.current_replacement_title
            : (r.replacement_title && r.replacement_title !== '—') ? r.replacement_title : null);
        const candidateCell = isJunk
            ? `<div style="font-size:12px;color:#888;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${_esc(brokenTitle)}">${_esc(brokenTitle)} ${_renderActivityBadge(r.broken_nzb_id)}</div>`
              + (r.replacement_title ? `<div style="font-size:11px;color:#666;margin-top:3px" title="${_esc(r.replacement_title)}">Junk: ${_esc(r.replacement_title)}</div>` : '')
            : _renderActivityCandidateCell(brokenTitle, replTitle, r.broken_nzb_id);
        const retryBtn = (r.outcome === 'skipped_max_attempts' && !resolvedElsewhere)
            ? `<button class="dm-btn dm-btn-secondary dm-btn-xs" style="margin-left:8px"
                       data-item-id="${_esc(r.item_id)}" data-broken-id="${_esc(r.broken_nzb_id || '')}"
                       onclick="retryExhaustedRepair(this)" title="Move back to Wanted and try to find another replacement">Retry</button>`
            : '';
        html += `<tr style="border-bottom:1px solid var(--border-color,#2a2a2a);background:${bg};">
            <td style="padding:8px 12px;white-space:nowrap;font-family:monospace;font-size:11px;color:var(--text-secondary,#888)">${ts}</td>
            <td style="padding:8px 12px;font-weight:500;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_esc(r.title || '—')}${epSub}</td>
            <td style="padding:8px 12px;max-width:0;overflow:hidden">${candidateCell}</td>
            <td style="padding:8px 12px;white-space:nowrap"><span style="color:${outcomeColor};font-weight:600;font-size:12px;cursor:help" title="${outcomeTooltip}">${outcomeLabel}</span>${retryBtn}</td>
        </tr>`;
    });

    html += '</tbody></table>';
    container.innerHTML = html;

    // Pagination
    const totalPages = Math.ceil(_usenetActivityTotal / _usenetActivityLimit);
    const currentPage = Math.floor(_usenetActivityOffset / _usenetActivityLimit) + 1;
    if (pagination) {
        if (totalPages > 1) {
            pagination.style.display = 'flex';
            document.getElementById('usenet-activity-info').textContent =
                `Page ${currentPage} of ${totalPages} (${_usenetActivityTotal} events)`;
            document.getElementById('usenet-prev-btn').disabled = _usenetActivityOffset === 0;
            document.getElementById('usenet-next-btn').disabled = _usenetActivityOffset + _usenetActivityLimit >= _usenetActivityTotal;
        } else {
            pagination.style.display = 'none';
        }
    }
}

function usenetActivityPage(dir) {
    _usenetActivityOffset = Math.max(0, _usenetActivityOffset + dir * _usenetActivityLimit);
    _usenetFetchActivity();
}

async function usenetSaveVersion(version) {
    try {
        await fetch('/debrid_manager/api/usenet/repair/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({repair_version: version || ''}),
        });
    } catch(e) { console.error('[Usenet] save version error', e); }
}

async function usenetTriggerScan(full = false) {
    const btn = document.getElementById('usenet-scan-btn');
    const orig = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = full ? '<i class="fa-solid fa-rotate fa-spin" style="color:#e8601c"></i> Running full…' : '<i class="fa-solid fa-rotate fa-spin" style="color:#e8601c"></i> Running…';
    try {
        const url = '/debrid_manager/api/usenet/repair/health_scan' + (full ? '?full=true' : '');
        const r = await fetch(url, {method:'POST'});
        const d = await r.json();
        btn.innerHTML = d.success ? '<i class="fa-solid fa-check" style="color:#4ade80"></i> Triggered' : '<i class="fa-solid fa-xmark" style="color:#ef4444"></i> Failed';
        setTimeout(() => { btn.disabled = false; btn.innerHTML = orig; }, 3000);
        if (d.success) _usenetStartScanPolling();
    } catch(e) { btn.disabled = false; btn.innerHTML = orig; }
}

let _usenetScanPollTimer = null;

function _usenetSetScanningState(scanning) {
    const btn = document.getElementById('usenet-scan-btn');
    const chevron = document.getElementById('usenet-scan-chevron');
    const select = btn?.parentElement?.querySelector('select');
    if (!btn) return;
    if (scanning) {
        btn.style.paddingRight = '';
        btn.onclick = usenetStopScan;
        btn.innerHTML = '<i class="fa-solid fa-rotate fa-spin" style="color:#e8601c"></i> Running… <i class="fa-solid fa-stop" style="color:#ef4444;margin-left:4px;font-size:0.8em"></i>';
        if (chevron) chevron.style.display = 'none';
        if (select) select.style.display = 'none';
    } else {
        btn.style.paddingRight = '28px';
        btn.onclick = function(){ usenetTriggerScan(false); };
        btn.innerHTML = '<i class="fa-solid fa-rotate" style="color:#e8601c"></i> Run Health Scan';
        if (chevron) chevron.style.display = '';
        if (select) select.style.display = '';
    }
}

async function usenetStopScan() {
    const btn = document.getElementById('usenet-scan-btn');
    if (btn) btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Stopping…';
    try {
        const r = await fetch('/debrid_manager/api/usenet/repair/stop', {method: 'POST'});
        const d = await r.json();
        if (!d.success) alert('Failed to stop scan: ' + (d.error || 'Unknown error'));
        // Keep polling until scan actually stops (cli_mount finishes current entry first)
        _usenetPollUntilStopped(0);
    } catch(e) {
        console.error('[Usenet] stop scan error', e);
        _usenetSetScanningState(false);
    }
}

async function _usenetPollUntilStopped(attempts) {
    if (attempts > 20) { _usenetSetScanningState(false); return; } // safety cap ~20s
    await new Promise(res => setTimeout(res, 1000));
    try {
        const pr = await fetch('/debrid_manager/api/usenet/repair/scan_status');
        const pd = await pr.json();
        if (pd.is_scanning) {
            _usenetPollUntilStopped(attempts + 1);
        } else {
            _usenetSetScanningState(false);
        }
    } catch(e) { _usenetSetScanningState(false); }
}

function _usenetStartScanPolling() {
    clearTimeout(_usenetScanPollTimer);
    _usenetScanPoll();
}

async function _usenetScanPoll() {
    try {
        const r = await fetch('/debrid_manager/api/usenet/repair/scan_status');
        const d = await r.json();
        _usenetSetScanningState(d.is_scanning);
        if (d.is_scanning) {
            fetch('/debrid_manager/api/usenet/repair/stats').then(r => r.json()).then(sd => {
                if (sd.success) _usenetRenderStats(sd.health || {}, sd.activity || {}, sd.versions || [], sd.repair_version || '');
            }).catch(() => {});
            _usenetScanPollTimer = setTimeout(_usenetScanPoll, 2000);
        }
    } catch(e) {
        _usenetSetScanningState(false);
    }
}

async function usenetViewBroken() {
    const list = document.getElementById('usenet-broken-list');
    if (list.style.display !== 'none') { list.style.display = 'none'; return; }
    try {
        const r = await fetch('/debrid_manager/api/usenet/repair/broken');
        const d = await r.json();
        const tbody = document.getElementById('usenet-broken-tbody');
        if (!d.success || !d.items || d.items.length === 0) {
            tbody.innerHTML = '<tr><td colspan="2" style="text-align:center;color:#666;padding:20px 12px">No broken items</td></tr>';
        } else {
            tbody.innerHTML = d.items.map((i, idx) => {
                const bg = idx % 2 === 0 ? 'transparent' : 'rgba(128,128,128,0.04)';
                const name = _esc(i.entry_name || i.name || i.title || '—');
                const rawName = (i.entry_name || i.name || i.title || '').replace(/'/g, "\\'");
                const broken = i.broken_count || i.broken_files || '?';
                return `<tr style="border-bottom:1px solid var(--border-color,#2a2a2a);background:${bg}">
                    <td style="padding:8px 12px;font-weight:500">${name}</td>
                    <td style="padding:8px 12px;color:#ff4444;font-weight:600;text-align:right;white-space:nowrap">${broken}</td>
                    <td style="padding:8px 12px;text-align:center;white-space:nowrap">
                        <button onclick="usenetFixSingle('${rawName}', this)" class="un-btn" style="padding:4px 12px;font-size:12px">Fix</button>
                    </td>
                </tr>`;
            }).join('');
        }
        list.style.display = 'block';
    } catch(e) { console.error('[Usenet] view broken error', e); }
}

async function usenetFixSingle(entryName, btn) {
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const versionOverride = document.getElementById('usenet-version-select')?.value || null;
        const r = await fetch('/debrid_manager/api/usenet/repair/fix_single', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({entry_name: entryName, version_override: versionOverride})
        });
        const d = await r.json();
        if (d.success) {
            btn.textContent = '✓';
            btn.style.color = '#4caf50';
            setTimeout(() => usenetViewBroken(), 2000); // refresh list
        } else {
            btn.textContent = '✗';
            btn.style.color = '#ff4444';
            setTimeout(() => { btn.textContent = orig; btn.disabled = false; btn.style.color = ''; }, 3000);
        }
    } catch(e) {
        console.error('[Usenet] fix single error', e);
        btn.textContent = orig;
        btn.disabled = false;
    }
}

async function usenetRunRepair() {
    const btn = document.getElementById('usenet-fix-btn');
    const orig = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Starting…';
    const statusEl = document.getElementById('usenet-repair-status-text');
    const barEl = document.getElementById('usenet-repair-bar');
    const runningEl = document.getElementById('usenet-repair-running');
    try {
        const versionOverride = document.getElementById('usenet-version-select')?.value || null;
        const r = await fetch('/debrid_manager/api/usenet/repair/run', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({version_override: versionOverride || null}),
        });
        const d = await r.json();
        if (d.success) {
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin" style="color:#e8601c"></i> Repairing…';
            runningEl.style.display = 'block';
            statusEl.textContent = 'Repair running…';
            barEl.style.width = '15%';
            _usenetPollRepairProgress();
        } else {
            btn.disabled = false;
            btn.innerHTML = orig;
            runningEl.style.display = 'block';
            statusEl.textContent = d.message || 'Already running or could not start';
            barEl.style.width = '0%';
        }
    } catch(e) {
        btn.disabled = false;
        btn.innerHTML = orig;
        console.error('[Usenet] run repair error', e);
    }
}

async function usenetDeleteAllBroken() {
    const btn = document.getElementById('usenet-delete-broken-btn');
    const orig = btn.innerHTML;
    const brokenCount = parseInt(document.getElementById('usenet-stat-broken')?.textContent || '0') || 0;
    const label = brokenCount > 0 ? `${brokenCount} broken` : 'all broken';
    if (!confirm(`Delete ${label} NZBs?\n\nThis will:\n• Delete from ${window.USENET_PROVIDER_NAME || "Usenet provider"}\n• Delete from Plex\n• Reset items to Wanted (will re-scrape)\n\nContinue?`)) return;
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Deleting…';
    try {
        const r = await fetch('/debrid_manager/api/usenet/repair/delete_all_broken', {method: 'POST'});
        const d = await r.json();
        if (d.success) {
            btn.innerHTML = '<i class="fa-solid fa-check" style="color:#4ade80"></i> Done';
            setTimeout(() => { btn.disabled = false; btn.innerHTML = orig; }, 3000);
            // Refresh stats
            fetch('/debrid_manager/api/usenet/repair/stats').then(r => r.json()).then(sd => {
                if (sd.success) _usenetRenderStats(sd.health || {}, sd.activity || {}, sd.versions || [], sd.repair_version || '');
            }).catch(() => {});
            // Show result
            const msg = d.message || 'Done';
            const statusEl = document.getElementById('usenet-repair-status-text');
            const runningEl = document.getElementById('usenet-repair-running');
            if (statusEl && runningEl) {
                runningEl.style.display = 'block';
                statusEl.textContent = msg;
            }
        } else {
            btn.disabled = false;
            btn.innerHTML = orig;
            alert('Delete failed: ' + (d.error || 'Unknown error'));
        }
    } catch(e) {
        btn.disabled = false;
        btn.innerHTML = orig;
        console.error('[Usenet] delete all broken error', e);
    }
}

function _usenetPollRepairProgress() {
    clearTimeout(_usenetRepairPollTimer);
    _usenetRepairPollTimer = setTimeout(async () => {
        try {
            const r = await fetch('/debrid_manager/api/usenet/repair/progress');
            const d = await r.json();
            const runningEl = document.getElementById('usenet-repair-running');
            const fixBtn = document.getElementById('usenet-fix-btn');
            const statusEl = document.getElementById('usenet-repair-status-text');
            const barEl = document.getElementById('usenet-repair-bar');
            if (d.is_running) {
                barEl.style.width = '60%';
                fixBtn.disabled = true;
                fixBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin" style="color:#e8601c"></i> Repairing…';
                _usenetPollRepairProgress();
            } else if (d.status === 'done' && d.summary) {
                barEl.style.width = '100%';
                statusEl.textContent =
                    `Done — ${d.summary.replaced} replaced, ${d.summary.not_found} not found, ${d.summary.errors} errors`;
                fixBtn.disabled = false;
                fixBtn.innerHTML = '<i class="fa-solid fa-check" style="color:#4ade80"></i> Done';
                setTimeout(() => { fixBtn.innerHTML = '<i class="fa-solid fa-wrench" style="color:#e8601c"></i> Fix Broken'; }, 3000);
                // Refresh stats without reloading whole tab (avoid flash)
                fetch('/debrid_manager/api/usenet/repair/stats').then(r => r.json()).then(sd => {
                    if (sd.success) {
                        _usenetRenderStats(sd.health || {}, sd.activity || {}, sd.versions || [], sd.repair_version || '');
                        const c = document.getElementById('usenet-activity-container');
                        const p = document.getElementById('usenet-activity-pagination');
                        fetch(`/debrid_manager/api/usenet/repair/activity?limit=${_usenetActivityLimit}&offset=0`)
                            .then(r => r.json()).then(d => { if (d.success) { _usenetActivityOffset = 0; _usenetActivityTotal = d.total; _usenetRenderActivity(d.rows, c, p); }});
                    }
                });
            } else if (d.status === 'error') {
                barEl.style.width = '0%';
                statusEl.textContent = 'Error: ' + (d.error || 'unknown');
                fixBtn.disabled = false;
                fixBtn.innerHTML = '<i class="fa-solid fa-wrench" style="color:#e8601c"></i> Fix Broken';
            }
            // If idle/no status, leave bar visible with last message — no flash
        } catch(e) { /* ignore */ }
    }, 3000);
}

if (_dmActiveTab === 'usenet') { loadUsenetTab(); afStartPolling(); }

// ── ACTIVE FILES WIDGET ────────────────────────────────────────────────────
var _afTimer = null;

function afFmtDuration(seconds) {
    if (!seconds || seconds < 0) return '0s';
    if (seconds < 60) return seconds + 's';
    const m = Math.floor(seconds / 60), s = seconds % 60;
    if (m < 60) return m + 'm ' + s + 's';
    const h = Math.floor(m / 60), mm = m % 60;
    return h + 'h ' + mm + 'm';
}

function afFmtSize(bytes) {
    if (!bytes) return '—';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
    if (bytes < 1073741824) return (bytes/1048576).toFixed(1) + ' MB';
    return (bytes/1073741824).toFixed(2) + ' GB';
}

function afFmtSpeed(bytesPerSec) {
    if (!bytesPerSec) return '';
    return ' · ' + afFmtSize(bytesPerSec) + '/s';
}

async function afLoad() {
    try {
        const r = await fetch('/debrid_manager/api/active_files');
        const d = await r.json();
        if (!d.success) { console.warn('[ActiveFiles]', d.error); return; }

        const list = document.getElementById('af-list');
        const badge = document.getElementById('af-count-badge');
        const empty = document.getElementById('af-empty');
        if (!list) return;

        const now = Math.floor(Date.now() / 1000);
        const items = [];

        // DFS / WebDAV streams
        for (const s of (d.dfs_streams || [])) {
            const dur = s.started_at ? Math.max(0, now - s.started_at) : 0;
            items.push({
                mode: 'dfs',
                name: s.file_name || s.entry_name,
                entryName: s.entry_name,
                source: s.source || 'dfs',
                size: s.file_size,
                durationSeconds: dur,
                cliDebridId: s.cli_debrid_id,
                cliDebridIds: s.cli_debrid_ids || {},
                speed: 0,
                progress: 0,
            });
        }

        // Rclone transfers are omitted — in rclone mode the same file already
        // appears as a DFS/NZB/torrent stream above, so showing both is redundant.
        if (false) for (const t of (d.rclone_transfers || [])) {
            items.push({
                mode: 'rclone',
                name: t.name || '—',
                entryName: t.entry_name || '',
                source: 'rclone',
                size: t.size,
                durationSeconds: 0,
                cliDebridId: null, // not available in rclone mode — delete uses entry_name
                speed: t.speed || 0,
                progress: t.progress || 0,
                bytesTransferred: t.bytes,
            });
        }

        const count = items.length;
        badge.textContent = count;
        badge.className = 'af-count-badge' + (count ? ' af-has-items' : '');

        // Keep existing rows to avoid flicker — rebuild only if count changed
        const existingRows = list.querySelectorAll('.af-item');
        if (existingRows.length !== count || count === 0) {
            list.innerHTML = '';
            if (empty) list.appendChild(empty);
        }

        if (count === 0) {
            if (empty) empty.style.display = '';
            return;
        }
        if (empty) empty.style.display = 'none';

        // Rebuild rows
        const newRows = [];
        for (const item of items) {
            const srcClass = item.source === 'nzb' ? 'af-source-nzb'
                           : item.source === 'torrent' ? 'af-source-torrent'
                           : 'af-source-rclone';
            const srcLabel = item.source.toUpperCase();
            const dur = item.durationSeconds > 0 ? afFmtDuration(item.durationSeconds) : '';
            const sz = afFmtSize(item.size);
            const spd = afFmtSpeed(item.speed);
            const prog = item.progress > 0 ? `<div class="af-progress-bar"><div class="af-progress-fill" style="width:${Math.min(100,item.progress)}%"></div></div>` : '';
            const canDelete = item.cliDebridId != null || item.entryName;
            const idsJson = JSON.stringify(item.cliDebridIds || {}).replace(/"/g,'&quot;');
            const delBtn = canDelete
                ? `<button class="af-del-btn" data-ids="${idsJson}" data-entry="${(item.entryName||'').replace(/"/g,'&quot;')}" onclick="afDelete(this)">Delete</button>`
                : '';

            const div = document.createElement('div');
            div.className = 'af-item';
            div.innerHTML = `
                <div class="af-item-info">
                    <div class="af-item-name" title="${item.name.replace(/"/g,'&quot;')}">${item.name}</div>
                    <div class="af-item-meta">
                        <span class="af-source-badge ${srcClass}">${srcLabel}</span>
                        ${dur ? `<span class="af-duration">⏱ ${dur}</span>` : ''}
                        ${sz !== '—' ? `<span class="af-size">${sz}</span>` : ''}
                        ${spd ? `<span class="af-size">${spd.trim()}</span>` : ''}
                    </div>
                    ${prog}
                </div>
                ${delBtn}`;
            newRows.push(div);
        }

        // Replace content
        if (empty) empty.style.display = 'none';
        while (list.firstChild) list.removeChild(list.firstChild);
        newRows.forEach(r => list.appendChild(r));

    } catch (e) {
        console.warn('[ActiveFiles] fetch error:', e);
    }
}

async function afDelete(btn) {
    const entryName = btn.dataset.entry || '';
    let cliDebridIds = {};
    try { cliDebridIds = JSON.parse(btn.dataset.ids || '{}'); } catch(e) {}
    const siblingCount = Object.keys(cliDebridIds).length;
    const msg = siblingCount > 1
        ? `Move all ${siblingCount} episodes of this season pack back to Wanted and delete from Plex?`
        : 'Move this item back to Wanted and delete from Plex?';
    if (!confirm(msg)) return;
    btn.disabled = true;
    btn.textContent = '…';
    const body = {entry_name: entryName, cli_debrid_ids: cliDebridIds};
    try {
        const r = await fetch('/debrid_manager/api/active_files/delete', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify(body),
        });
        const d = await r.json();
        if (d.success) {
            btn.closest('.af-item').remove();
            afLoad();
        } else {
            btn.disabled = false;
            btn.textContent = 'Delete';
            alert('Delete failed: ' + (d.error || 'unknown error'));
        }
    } catch (e) {
        btn.disabled = false;
        btn.textContent = 'Delete';
        alert('Delete error: ' + e.message);
    }
}

async function retryExhaustedRepair(btn) {
    const itemId = btn.dataset.itemId || '';
    const brokenId = btn.dataset.brokenId || '';
    if (!itemId) return;
    if (!confirm('Move this item back to Wanted and try to find another replacement?')) return;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const r = await fetch('/debrid_manager/api/usenet/repair/retry_exhausted', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({item_id: itemId, broken_nzb_id: brokenId}),
        });
        const d = await r.json();
        if (d.success) {
            _usenetFetchActivity();
        } else {
            btn.disabled = false;
            btn.textContent = 'Retry';
            alert('Retry failed: ' + (d.error || d.result?.message || 'unknown error'));
        }
    } catch (e) {
        btn.disabled = false;
        btn.textContent = 'Retry';
        alert('Retry error: ' + e.message);
    }
}

// Auto-refresh every 10s while the usenet tab is visible
function afStartPolling() {
    afLoad();
    clearInterval(_afTimer);
    _afTimer = setInterval(() => { if (!document.hidden) afLoad(); }, 10000);
}
function afStopPolling() {
    clearInterval(_afTimer);
    _afTimer = null;
}

// ── cli_mount SUB-TAB SWITCHING ────────────────────────────────────────────
let _climountActiveSubtab = 'usenet';

function switchcli_mountSubtab(tab) {
    _climountActiveSubtab = tab;
    const usenetBtn  = document.getElementById('usenet-subtab-btn');
    const debridBtn  = document.getElementById('debrid-subtab-btn');
    const usenetContent = document.getElementById('usenet-subtab-content');
    const debridContent = document.getElementById('debrid-subtab-content');
    if (usenetBtn)  usenetBtn.classList.toggle('active', tab === 'usenet');
    if (debridBtn)  debridBtn.classList.toggle('active', tab === 'debrid');
    if (usenetContent) usenetContent.style.display = tab === 'usenet' ? '' : 'none';
    if (debridContent) debridContent.style.display = tab === 'debrid' ? '' : 'none';
    if (tab === 'debrid' && !window._debridLoaded) loadDebridTab();
}

// ── DEBRID REPAIR TAB ──────────────────────────────────────────────────────
window._debridLoaded = false;
let _debridActivityOffset = 0;
let _debridActivityTotal  = 0;
const _debridActivityLimit = 25;
let _debridActivityFilter  = null;
let _debridRepairPollTimer = null;

async function loadDebridTab() {
    window._debridLoaded = true;
    const loadEl  = document.getElementById('dr-loading');
    const content = document.getElementById('dr-content');
    const errEl   = document.getElementById('dr-error');
    if (loadEl)  loadEl.style.display  = 'block';
    if (content) content.style.display = 'none';
    if (errEl)   errEl.style.display   = 'none';
    try {
        const r = await fetch('/debrid_manager/api/debrid/repair/stats');
        const d = await r.json();
        if (!d.success) throw new Error(d.error || 'Failed to load debrid stats');
        _debridRenderStats(d.health || {}, d.activity || {});
        // Populate version selector from usenet settings (same version list)
        try {
            const sr = await fetch('/debrid_manager/api/usenet/repair/stats');
            const sd = await sr.json();
            if (sd.success) _debridRenderVersions(sd.versions || [], sd.repair_version || '');
        } catch(_) {}
        if (loadEl)  loadEl.style.display  = 'none';
        if (content) content.style.display = 'block';
        await debridLoadActivity(null);
        _debridMaybeStartPolling();
        _debridScanPoll();
    } catch(e) {
        if (loadEl) loadEl.style.display = 'none';
        if (errEl)  { errEl.style.display = 'block'; errEl.textContent = 'Failed to load debrid data: ' + e.message; }
    }
}

async function _debridMaybeStartPolling() {
    try {
        const r = await fetch('/debrid_manager/api/debrid/repair/progress');
        const d = await r.json();
        if (d.is_running || d.status === 'running') {
            document.getElementById('dr-repair-running').style.display = 'block';
            document.getElementById('dr-repair-status-text').textContent = 'Repair running…';
            document.getElementById('dr-repair-bar').style.width = '60%';
            const fixBtn = document.getElementById('dr-fix-btn');
            if (fixBtn) { fixBtn.disabled = true; fixBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin" style="color:#CE93D8"></i> Repairing…'; }
            _debridPollRepairProgress();
        }
    } catch(e) { /* ignore */ }
}

function _debridRenderStats(health, activity) {
    const healthy   = health.healthy  || 0;
    const broken    = health.broken   || 0;
    const unknown   = health.unknown  || 0;
    const reinserted = activity.reinserted || 0;
    const replaced  = activity.replaced   || 0;
    const setEl = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    setEl('dr-stat-healthy',    healthy.toLocaleString());
    setEl('dr-stat-broken',     broken);
    setEl('dr-stat-unknown',    unknown.toLocaleString());
    setEl('dr-stat-reinserted', reinserted);
    setEl('dr-stat-replaced',   replaced);
    const badge    = document.getElementById('dr-broken-badge');
    const tabBadge = document.getElementById('debrid-tab-badge');
    if (broken > 0) {
        if (badge)    { badge.textContent = broken; badge.style.display = 'inline-block'; }
        if (tabBadge) { tabBadge.textContent = broken; tabBadge.style.display = 'inline-block'; }
    } else {
        if (badge)    badge.style.display = 'none';
        if (tabBadge) tabBadge.style.display = 'none';
    }
}

function _debridRenderVersions(versions, savedVersion) {
    const sel = document.getElementById('debrid-version-select');
    if (!sel) return;
    sel.innerHTML = '<option value="">Item\'s version</option>' +
        versions.map(v => `<option value="${_esc(v)}"${v === savedVersion ? ' selected' : ''}>${_esc(v)}</option>`).join('');
}

async function debridSaveVersion(version) {
    try {
        await fetch('/debrid_manager/api/usenet/repair/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({repair_version: version || ''}),
        });
    } catch(e) { console.error('[Debrid] save version error', e); }
}

async function debridLoadActivity(filter) {
    _debridActivityFilter = filter || null;
    _debridActivityOffset = 0;
    const sel = document.getElementById('dr-filter-select');
    if (sel) sel.value = filter || '';
    await _debridFetchActivity();
}

async function debridClearActivity() {
    if (!confirm('Clear the entire debrid repair activity log?\n\nThis only deletes the log history — it does not touch any media, Plex entries, or provider jobs.\n\nContinue?')) return;
    try {
        const r = await fetch('/debrid_manager/api/debrid/repair/activity/clear', {method: 'POST'});
        const d = await r.json();
        if (d.success) {
            _debridActivityOffset = 0;
            await _debridFetchActivity();
        } else {
            alert('Failed to clear activity log: ' + (d.error || 'unknown error'));
        }
    } catch(e) {
        console.error('[Debrid] activity clear error', e);
        alert('Failed to clear activity log');
    }
}

async function _debridFetchActivity() {
    const container  = document.getElementById('dr-activity-container');
    const pagination = document.getElementById('dr-activity-pagination');
    if (!container) return;
    container.innerHTML = '<div style="color:#666;padding:20px 0">Loading activity…</div>';
    const url = `/debrid_manager/api/debrid/repair/activity?limit=${_debridActivityLimit}&offset=${_debridActivityOffset}` +
        (_debridActivityFilter ? `&outcome=${_debridActivityFilter}` : '');
    try {
        const r = await fetch(url);
        const d = await r.json();
        if (!d.success) return;
        _debridActivityTotal = d.total;
        _debridRenderActivity(d.rows, container, pagination);
    } catch(e) { console.error('[Debrid] activity fetch error', e); }
}

function _debridRenderActivity(rows, container, pagination) {
    if (!rows || rows.length === 0) {
        container.innerHTML = '<p style="color:var(--text-secondary,#666);padding:20px 0">No debrid repair activity recorded yet.</p>';
        if (pagination) pagination.style.display = 'none';
        return;
    }

    let html = `<table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead>
            <tr style="border-bottom:2px solid var(--border-color,#333);text-align:left;">
                <th style="padding:8px 12px;white-space:nowrap;color:var(--text-secondary,#888);font-weight:600;width:145px;">Date / Time</th>
                <th style="padding:8px 12px;color:var(--text-secondary,#888);font-weight:600;width:160px;">Title</th>
                <th style="padding:8px 12px;color:var(--text-secondary,#888);font-weight:600;">Broken / Replaced</th>
                <th style="padding:8px 12px;color:var(--text-secondary,#888);font-weight:600;width:90px;">Outcome</th>
            </tr>
        </thead>
        <tbody>`;

    rows.forEach((r, idx) => {
        const bg = idx % 2 === 0 ? 'transparent' : 'rgba(128,128,128,0.04)';
        const isJunk = r.outcome === 'skipped_junk_source';
        const outcomeColor = isJunk ? '#888'
            : r.outcome === 'replaced' ? '#00d4aa'
            : r.outcome === 'reinserted' ? '#a78bfa'
            : r.outcome === 'not_found'  ? '#ffaa44'
            : r.outcome === 'ambiguous' || r.outcome === 'kept_shared' ? '#ffaa44'
            : '#ff4444';
        const outcomeLabel = isJunk ? 'Junk'
            : r.outcome === 'replaced'   ? 'Replaced'
            : r.outcome === 'reinserted' ? 'Re-inserted'
            : r.outcome === 'not_found'  ? 'Not Found'
            : r.outcome === 'ambiguous'  ? 'Skipped'
            : r.outcome === 'kept_shared' ? 'Pack kept'
            : (r.outcome || '—');
        const outcomeTooltip = _buildOutcomeTooltip(r);
        const epSub = r.season_number
            ? `<br><span style="font-size:11px;color:#888">S${String(r.season_number).padStart(2,'0')}E${String(r.episode_number||0).padStart(2,'0')}</span>`
            : '';
        const ts = r.created_at
            ? new Date(r.created_at + (r.created_at.endsWith('Z') ? '' : 'Z')).toLocaleString()
            : '—';
        const brokenTitle = r.broken_nzb_title || r.broken_nzb_id || '—';
        const replTitle = isJunk ? null : ((r.replacement_title && r.replacement_title !== '—') ? r.replacement_title : null);
        const candidateCell = isJunk
            ? `<div style="font-size:12px;color:#888;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${_esc(brokenTitle)}">${_esc(brokenTitle)} ${_renderActivityBadge(r.broken_nzb_id)}</div>`
              + (r.replacement_title ? `<div style="font-size:11px;color:#666;margin-top:3px" title="${_esc(r.replacement_title)}">Junk: ${_esc(r.replacement_title)}</div>` : '')
            : _renderActivityCandidateCell(brokenTitle, replTitle, r.broken_nzb_id);
        html += `<tr style="border-bottom:1px solid var(--border-color,#2a2a2a);background:${bg};">
            <td style="padding:8px 12px;white-space:nowrap;font-family:monospace;font-size:11px;color:var(--text-secondary,#888)">${ts}</td>
            <td style="padding:8px 12px;font-weight:500;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_esc(r.title || '—')}${epSub}</td>
            <td style="padding:8px 12px;max-width:0;overflow:hidden">${candidateCell}</td>
            <td style="padding:8px 12px;white-space:nowrap"><span style="color:${outcomeColor};font-weight:600;font-size:12px;cursor:help" title="${outcomeTooltip}">${outcomeLabel}</span></td>
        </tr>`;
    });

    html += '</tbody></table>';
    container.innerHTML = html;

    const totalPages  = Math.ceil(_debridActivityTotal / _debridActivityLimit);
    const currentPage = Math.floor(_debridActivityOffset / _debridActivityLimit) + 1;
    if (pagination) {
        if (totalPages > 1) {
            pagination.style.display = 'flex';
            const infoEl = document.getElementById('dr-activity-info');
            const prevBtn = document.getElementById('dr-prev-btn');
            const nextBtn = document.getElementById('dr-next-btn');
            if (infoEl)  infoEl.textContent = `Page ${currentPage} of ${totalPages} (${_debridActivityTotal} events)`;
            if (prevBtn) prevBtn.disabled = _debridActivityOffset === 0;
            if (nextBtn) nextBtn.disabled = _debridActivityOffset + _debridActivityLimit >= _debridActivityTotal;
        } else {
            pagination.style.display = 'none';
        }
    }
}

function debridActivityPage(dir) {
    _debridActivityOffset = Math.max(0, _debridActivityOffset + dir * _debridActivityLimit);
    _debridFetchActivity();
}

async function debridTriggerScan(full = false) {
    const btn  = document.getElementById('dr-scan-btn');
    const orig = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = full ? '<i class="fa-solid fa-rotate fa-spin" style="color:#CE93D8"></i> Running full…' : '<i class="fa-solid fa-rotate fa-spin" style="color:#CE93D8"></i> Running…';
    try {
        const url = '/debrid_manager/api/debrid/repair/health_scan' + (full ? '?full=true' : '');
        const r = await fetch(url, {method: 'POST'});
        const d = await r.json();
        btn.innerHTML = d.success ? '<i class="fa-solid fa-check" style="color:#4ade80"></i> Triggered' : '<i class="fa-solid fa-xmark" style="color:#ef4444"></i> Failed';
        setTimeout(() => { btn.disabled = false; btn.innerHTML = orig; }, 3000);
        if (d.success) _debridStartScanPolling();
    } catch(e) { btn.disabled = false; btn.innerHTML = orig; }
}

let _debridScanPollTimer = null;

function _debridSetScanningState(scanning) {
    const btn = document.getElementById('dr-scan-btn');
    const chevron = document.getElementById('dr-scan-chevron');
    const select = btn?.parentElement?.querySelector('select');
    if (!btn) return;
    if (scanning) {
        btn.style.paddingRight = '';
        btn.onclick = debridStopScan;
        btn.innerHTML = '<i class="fa-solid fa-rotate fa-spin" style="color:#CE93D8"></i> Running… <i class="fa-solid fa-stop" style="color:#ef4444;margin-left:4px;font-size:0.8em"></i>';
        if (chevron) chevron.style.display = 'none';
        if (select) select.style.display = 'none';
    } else {
        btn.style.paddingRight = '28px';
        btn.onclick = function(){ debridTriggerScan(false); };
        btn.innerHTML = '<i class="fa-solid fa-rotate" style="color:#CE93D8"></i> Run Health Scan';
        if (chevron) chevron.style.display = '';
        if (select) select.style.display = '';
    }
}

async function debridStopScan() {
    const btn = document.getElementById('dr-scan-btn');
    if (btn) btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Stopping…';
    try {
        const r = await fetch('/debrid_manager/api/debrid/repair/stop', {method: 'POST'});
        const d = await r.json();
        if (!d.success) alert('Failed to stop scan: ' + (d.error || 'Unknown error'));
        // Keep polling until scan actually stops (cli_mount finishes current entry first)
        _debridPollUntilStopped(0);
    } catch(e) {
        console.error('[Debrid] stop scan error', e);
        _debridSetScanningState(false);
    }
}

async function _debridPollUntilStopped(attempts) {
    if (attempts > 20) { _debridSetScanningState(false); return; } // safety cap ~20s
    await new Promise(res => setTimeout(res, 1000));
    try {
        const pr = await fetch('/debrid_manager/api/debrid/repair/scan_status');
        const pd = await pr.json();
        if (pd.is_scanning) {
            _debridPollUntilStopped(attempts + 1);
        } else {
            _debridSetScanningState(false);
        }
    } catch(e) { _debridSetScanningState(false); }
}

function _debridStartScanPolling() {
    clearTimeout(_debridScanPollTimer);
    _debridScanPoll();
}

async function _debridScanPoll() {
    try {
        const r = await fetch('/debrid_manager/api/debrid/repair/scan_status');
        const d = await r.json();
        _debridSetScanningState(d.is_scanning);
        if (d.is_scanning) {
            fetch('/debrid_manager/api/debrid/repair/stats').then(r => r.json()).then(sd => {
                if (sd.success) _debridRenderStats(sd.health || {}, sd.activity || {});
            }).catch(() => {});
            _debridScanPollTimer = setTimeout(_debridScanPoll, 2000);
        }
    } catch(e) { _debridSetScanningState(false); }
}

let _debridBrokenItems = [];

async function debridViewBroken() {
    const list = document.getElementById('dr-broken-list');
    if (list.style.display !== 'none') { list.style.display = 'none'; return; }
    try {
        const r = await fetch('/debrid_manager/api/debrid/repair/broken');
        const d = await r.json();
        const tbody = document.getElementById('dr-broken-tbody');
        if (!d.success || !d.items || d.items.length === 0) {
            _debridBrokenItems = [];
            tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#666;padding:20px 12px">No broken torrent items</td></tr>';
        } else {
            _debridBrokenItems = d.items;
            tbody.innerHTML = d.items.map((item, idx) => {
                const bg = idx % 2 === 0 ? 'transparent' : 'rgba(128,128,128,0.04)';
                const name        = _esc(item.entry_name || item.name || item.title || '—');
                const rawName     = (item.entry_name || item.name || item.title || '').replace(/'/g, "\\'");
                const broken      = item.broken_count || (item.broken_files ? item.broken_files.length : '?');
                const reason      = item.failure_reason || '—';
                const isGhost     = item.is_ghost === true;
                const isMissing   = reason === 'missing_provider_link';
                const actionLabel = isMissing ? 'Re-insert' : 'Replace';
                const rowOpacity  = isGhost ? 'opacity:0.5' : '';
                const reasonCell  = isGhost
                    ? `<span style="color:#f59e0b">ghost</span>`
                    : `<span style="color:#CE93D8">${_esc(reason)}</span>`;
                const actionCell  = isGhost
                    ? `<button onclick="debridDeleteGhostSingle('${rawName}', this)" class="un-btn" style="padding:4px 12px;font-size:12px;color:#f59e0b;border-color:#f59e0b"><i class="fa-solid fa-ghost"></i> Delete</button>`
                    : `<button onclick="debridFixSingle(${idx}, this)" class="un-btn" style="padding:4px 12px;font-size:12px">${actionLabel}</button>`;
                return `<tr style="border-bottom:1px solid var(--border-color,#2a2a2a);background:${bg};${rowOpacity}">
                    <td style="padding:8px 12px;font-weight:500">${name}</td>
                    <td style="padding:8px 12px;font-size:12px">${reasonCell}</td>
                    <td style="padding:8px 12px;color:#ff4444;font-weight:600;text-align:right;white-space:nowrap">${broken}</td>
                    <td style="padding:8px 12px;text-align:center;white-space:nowrap">${actionCell}</td>
                </tr>`;
            }).join('');
        }
        list.style.display = 'block';
    } catch(e) { console.error('[Debrid] view broken error', e); }
}

async function debridFixSingle(idx, btn) {
    const item = _debridBrokenItems[idx];
    if (!item) return;
    const entryName = item.entry_name || item.name || item.title || '';
    const infoHash = item.info_hash || item.hash || (item.broken_files && item.broken_files[0] && item.broken_files[0].info_hash) || '';
    const action = item.failure_reason === 'missing_provider_link' ? 'reinsert' : 'replace';
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const r = await fetch('/debrid_manager/api/debrid/repair/fix_single', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                entry_name: entryName,
                info_hash: infoHash,
                action: action,
                broken_files: item.broken_files || [],
            })
        });
        const d = await r.json();
        const outcome = d.result && d.result.outcome;
        if (d.success && (outcome === 'replaced' || outcome === 'reinserted')) {
            btn.textContent = '✓';
            btn.style.color = '#4caf50';
            setTimeout(() => debridViewBroken(), 2000);
        } else if (d.success) {
            btn.textContent = '✗';
            btn.style.color = '#ffaa44';
            alert((d.result && d.result.message) || outcome || 'Repair left the torrent in place');
            setTimeout(() => { btn.textContent = orig; btn.disabled = false; btn.style.color = ''; }, 3000);
        } else {
            btn.textContent = '✗';
            btn.style.color = '#ff4444';
            setTimeout(() => { btn.textContent = orig; btn.disabled = false; btn.style.color = ''; }, 3000);
        }
    } catch(e) {
        console.error('[Debrid] fix single error', e);
        btn.textContent = orig;
        btn.disabled = false;
    }
}

async function debridRunRepair() {
    const btn      = document.getElementById('dr-fix-btn');
    const orig     = btn.innerHTML;
    const statusEl = document.getElementById('dr-repair-status-text');
    const barEl    = document.getElementById('dr-repair-bar');
    const runningEl = document.getElementById('dr-repair-running');
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Starting…';
    try {
        const versionOverride = document.getElementById('debrid-version-select')?.value || null;
        const r = await fetch('/debrid_manager/api/debrid/repair/run', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(versionOverride ? {version_override: versionOverride} : {}),
        });
        const d = await r.json();
        if (d.success) {
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin" style="color:#CE93D8"></i> Repairing…';
            runningEl.style.display = 'block';
            statusEl.textContent = 'Repair running…';
            barEl.style.width = '15%';
            _debridPollRepairProgress();
        } else {
            btn.disabled = false;
            btn.innerHTML = orig;
            runningEl.style.display = 'block';
            statusEl.textContent = d.message || 'Already running or could not start';
            barEl.style.width = '0%';
        }
    } catch(e) {
        btn.disabled = false;
        btn.innerHTML = orig;
        console.error('[Debrid] run repair error', e);
    }
}

async function debridDeleteAllBroken() {
    const btn  = document.getElementById('dr-delete-broken-btn');
    const orig = btn.innerHTML;
    const brokenCount = parseInt(document.getElementById('dr-stat-broken')?.textContent || '0') || 0;
    const label = brokenCount > 0 ? `${brokenCount} broken` : 'all broken';
    if (!confirm(`Repair ${label} debrid files?\n\nThis will:\n• Reset only the broken file to Wanted (it will re-scrape)\n• Remove that file from Plex\n• Delete the torrent only when no other episode still uses it\n\nContinue?`)) return;
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Deleting…';
    try {
        const r = await fetch('/debrid_manager/api/debrid/repair/delete_all_broken', {method: 'POST'});
        const d = await r.json();
        if (d.success) {
            btn.innerHTML = '<i class="fa-solid fa-check" style="color:#4ade80"></i> Done';
            setTimeout(() => { btn.disabled = false; btn.innerHTML = orig; }, 3000);
            fetch('/debrid_manager/api/debrid/repair/stats').then(r => r.json()).then(sd => {
                if (sd.success) _debridRenderStats(sd.health || {}, sd.activity || {});
            }).catch(() => {});
            const msg = d.message || 'Done';
            const statusEl  = document.getElementById('dr-repair-status-text');
            const runningEl = document.getElementById('dr-repair-running');
            if (statusEl && runningEl) { runningEl.style.display = 'block'; statusEl.textContent = msg; }
        } else {
            btn.disabled = false;
            btn.innerHTML = orig;
            alert('Delete failed: ' + (d.error || 'Unknown error'));
        }
    } catch(e) {
        btn.disabled = false;
        btn.innerHTML = orig;
        console.error('[Debrid] delete all broken error', e);
    }
}

async function debridDeleteGhostEntries() {
    const btn  = document.getElementById('dr-delete-ghosts-btn');
    const orig = btn.innerHTML;
    if (!confirm('Delete ghost entries?\n\nThis removes raw-RD-filename entries (created by cli_mount\'s own repair) from both cli_mount and RealDebrid.\n\nYour properly-named CLI entries are NOT affected.\n\nContinue?')) return;
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Cleaning…';
    try {
        const r = await fetch('/debrid_manager/api/debrid/repair/delete_ghost_entries', {method: 'POST'});
        const d = await r.json();
        if (d.success) {
            btn.innerHTML = '<i class="fa-solid fa-check" style="color:#4ade80"></i> Done';
            setTimeout(() => { btn.disabled = false; btn.innerHTML = orig; }, 3000);
            alert(d.message);
        } else {
            btn.disabled = false;
            btn.innerHTML = orig;
            alert('Failed: ' + (d.error || 'Unknown error'));
        }
    } catch(e) {
        btn.disabled = false;
        btn.innerHTML = orig;
        console.error('[Debrid] delete ghost entries error', e);
    }
}

async function debridDeleteGhostSingle(entryName, btn) {
    const orig = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
    try {
        const r = await fetch('/debrid_manager/api/debrid/repair/delete_ghost_single', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({entry_name: entryName})
        });
        const d = await r.json();
        if (d.success) {
            btn.closest('tr').remove();
        } else {
            btn.disabled = false;
            btn.innerHTML = orig;
            alert('Failed: ' + (d.error || 'Unknown error'));
        }
    } catch(e) {
        btn.disabled = false;
        btn.innerHTML = orig;
        console.error('[Debrid] delete ghost single error', e);
    }
}

function _debridPollRepairProgress() {
    clearTimeout(_debridRepairPollTimer);
    _debridRepairPollTimer = setTimeout(async () => {
        try {
            const r = await fetch('/debrid_manager/api/debrid/repair/progress');
            const d = await r.json();
            const runningEl = document.getElementById('dr-repair-running');
            const fixBtn    = document.getElementById('dr-fix-btn');
            const statusEl  = document.getElementById('dr-repair-status-text');
            const barEl     = document.getElementById('dr-repair-bar');
            if (d.is_running) {
                if (barEl) barEl.style.width = '60%';
                if (fixBtn) { fixBtn.disabled = true; fixBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin" style="color:#CE93D8"></i> Repairing…'; }
                _debridPollRepairProgress();
            } else if (d.status === 'done' && d.summary) {
                if (barEl)    barEl.style.width = '100%';
                if (statusEl) statusEl.textContent =
                    `Done — ${d.summary.reinserted || 0} re-inserted, ${d.summary.replaced || 0} replaced, ${d.summary.ambiguous || 0} skipped, ${d.summary.errors || 0} errors`;
                if (fixBtn) {
                    fixBtn.disabled = false;
                    fixBtn.innerHTML = '<i class="fa-solid fa-check" style="color:#4ade80"></i> Done';
                    setTimeout(() => { fixBtn.innerHTML = '<i class="fa-solid fa-wrench" style="color:#CE93D8"></i> Fix Broken'; }, 3000);
                }
                fetch('/debrid_manager/api/debrid/repair/stats').then(r => r.json()).then(sd => {
                    if (sd.success) {
                        _debridRenderStats(sd.health || {}, sd.activity || {});
                        const c = document.getElementById('dr-activity-container');
                        const p = document.getElementById('dr-activity-pagination');
                        fetch(`/debrid_manager/api/debrid/repair/activity?limit=${_debridActivityLimit}&offset=0`)
                            .then(r => r.json()).then(d => {
                                if (d.success) { _debridActivityOffset = 0; _debridActivityTotal = d.total; _debridRenderActivity(d.rows, c, p); }
                            });
                    }
                });
            } else if (d.status === 'error') {
                if (barEl)    barEl.style.width = '0%';
                if (statusEl) statusEl.textContent = 'Error: ' + (d.error || 'unknown');
                if (fixBtn)   { fixBtn.disabled = false; fixBtn.innerHTML = '<i class="fa-solid fa-wrench" style="color:#CE93D8"></i> Fix Broken'; }
            }
        } catch(e) { /* ignore */ }
    }, 3000);
}
