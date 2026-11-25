// Page script for templates/debrid_manager.html.
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
            <td class="dm-col-name" title="${escHtml(t.filename||'')}">${escHtml(t.filename || '—')}</td>
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
        tbody.innerHTML = `<tr class="dm-empty-row"><td colspan="6">Error loading history: ${escHtml(e.message)}</td></tr>`;
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
            <td class="dm-col-name" title="${escHtml(e.label||'')}">${escHtml(e.label || '—')}</td>
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
        ['Hash', `<code>${escHtml(e.torrent_hash)}</code>`],
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
            ['Torrent Title', item.filled_by_title ? `<code>${escHtml(item.filled_by_title)}</code>` : ''],
            ['TMDB ID', item.tmdb_id||''],
        ]));
    }

    if (trigger.selected_files && trigger.selected_files.length) {
        const _sfVideoExts = /\.(mkv|mp4|avi|mov|wmv|m4v|ts|m2ts|flv|webm)$/i;
        const _sfAll = trigger.selected_files;
        const _sfVideo = _sfAll.filter(f => _sfVideoExts.test(f.path||f.name||''));
        const _sfDisplay = _sfVideo.length ? _sfVideo : _sfAll;
        const rows = _sfDisplay.map(f =>
            `<tr><td><code>${escHtml(f.path||f.name||'')}</code></td><td>${((f.bytes||0)/1024/1024).toFixed(2)} MB</td><td>${f.selected ? '✓' : '✗'}</td></tr>`
        ).join('');
        html += section('Selected Files', `<table class="dm-table dm-table-compact"><thead><tr><th>Path</th><th>Size</th><th>Selected</th></tr></thead><tbody>${rows}</tbody></table>`);
    }

    if (meta.debrid_info) {
        const di = meta.debrid_info;
        html += section('Debrid Information', dl([
            ['Provider', (di.provider||'').toUpperCase()],
            ['Torrent ID', di.torrent_id ? `<code>${escHtml(di.torrent_id)}</code>` : ''],
            ['Status', di.status||''],
            ['Filename', di.filename ? `<code>${escHtml(di.filename)}</code>` : ''],
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
