/* Moved from templates/debrid_manager.html. */
// ── RECONCILE TAB ──────────────────────────────────────────────────────────
window._rcLoaded = false;
let _rcData = null;
const _dmMsLabel = window.pageData.debrid_manager.mediaServerLabel;

function _rcTypeBadge(type) {
    const cls = type === 'movie' ? 'dm-type-movie' : 'dm-type-tv';
    const lbl = type === 'movie' ? 'Movie' : type === 'episode' ? 'TV' : (type || '—');
    return `<span class="dm-type-badge ${cls}">${escHtml(lbl)}</span>`;
}

function _rcFileCell(item) {
    const f = item.filled_by_file;
    if (!f) return '<span class="dm-pt-folder">—</span>';
    const parts = f.split('/').filter(Boolean);
    const name = parts[parts.length - 1] || f;
    return `<span class="dm-pt-folder" title="${escHtml(f)}">${escHtml(name)}</span>`;
}

function _rcTitleCell(item) {
    const yr = item.year ? ` <span class="dm-pt-folder">(${escHtml(String(item.year))})</span>` : '';
    return `<span class="dm-pt-title">${escHtml(item.title)}${yr}</span>`;
}

// ── Reconcile sort / filter state ──────────────────────────────────────
const _rcState = {
    'not-in-plex':   { sort: 'title', dir: 1, search: '', type: '' },
    'torrent-gone':  { sort: 'title', dir: 1, search: '', type: '' },
    'lost':          { sort: 'title', dir: 1, search: '', type: '' },
    'untracked':     { sort: 'title', dir: 1, search: '', type: '' },
    'nas':           { sort: 'title', dir: 1, search: '', type: '' },
};

function _rcApplyFilter(group, items) {
    const s = _rcState[group] || {};
    let out = items.slice();
    if (s.search) {
        const q = s.search.toLowerCase();
        out = out.filter(i => (i.title||'').toLowerCase().includes(q) || (i.filled_by_file||'').toLowerCase().includes(q) || (i.rd_filename||'').toLowerCase().includes(q));
    }
    if (s.type) {
        out = out.filter(i => s.type === 'movie' ? i.type === 'movie' : i.type !== 'movie');
    }
    if (s.sort) {
        const key = s.sort, dir = s.dir;
        out.sort((a, b) => {
            const av = (a[key]||'').toString().toLowerCase();
            const bv = (b[key]||'').toString().toLowerCase();
            return dir * av.localeCompare(bv);
        });
    }
    return out;
}

function _rcFilterTable(el) {
    const panel = el.closest('.dm-rc-group-panel');
    const group = panel?.id?.replace('rc-panel-', '');
    if (!group || !_rcState[group]) return;
    const s = _rcState[group];
    if (el.classList.contains('dm-rc-search')) s.search = el.value;
    if (el.classList.contains('dm-rc-type-filter')) s.type = el.value;
    _rcRerender(group);
}

function _rcSortBy(group, key, th) {
    const s = _rcState[group];
    if (!s) return;
    s.dir = (s.sort === key) ? -s.dir : 1;
    s.sort = key;
    th.closest('thead').querySelectorAll('.dm-rc-th-sort').forEach(h => h.classList.remove('dm-rc-sort-asc', 'dm-rc-sort-desc'));
    th.classList.add(s.dir === 1 ? 'dm-rc-sort-asc' : 'dm-rc-sort-desc');
    _rcRerender(group);
}

function _rcRerender(group) {
    if (!_rcData) return;
    const map = {
        'not-in-plex':  () => _rcRenderNotInPlex(_rcApplyFilter('not-in-plex', _rcData.not_in_plex||[])),
        'torrent-gone': () => _rcRenderTorrentGone(_rcApplyFilter('torrent-gone', _rcData.torrent_gone||[])),
        'lost':         () => _rcRenderLost(_rcApplyFilter('lost', _rcData.lost||[])),
        'untracked':    () => _rcRenderUntracked(_rcApplyFilter('untracked', _rcData.untracked_rd||[])),
        'nas':          () => _rcRenderNas(_rcApplyFilter('nas', _rcData.nas_items||[])),
    };
    map[group]?.();
}

function _rcRenderNotInPlex(items) {
    const tbody = document.getElementById('rc-tbody-not-in-plex');
    if (!items.length) { tbody.innerHTML = `<tr class="dm-empty-row"><td colspan="4">All collected content is indexed in ${_dmMsLabel}.</td></tr>`; return; }
    tbody.innerHTML = items.map(item => {
        const _ff = item.filled_by_file || '';
        const _loc = (item.location_on_disk || '').replace(/\\/g, '/');
        // filled_by_file is often a bare filename (no slashes); fall back to location_on_disk parent dir
        const dir = _ff.includes('/') ? _ff.split('/').slice(0,-1).join('/')
                  : _loc.includes('/') ? _loc.split('/').slice(0,-1).join('/')
                  : '';
        return `<tr>
            <td>${_rcTitleCell(item)}</td>
            <td>${_rcTypeBadge(item.type)}</td>
            <td class="dm-rc-col-file">${_rcFileCell(item)}</td>
            <td class="dm-col-actions" style="white-space:nowrap">
                <button class="dm-btn dm-btn-secondary dm-btn-sm dm-rc-scan-btn" data-dir="${escHtml(dir)}" title="Trigger ${_dmMsLabel} scan">Scan</button>
                <button class="dm-btn dm-btn-danger dm-btn-sm dm-rc-del-db-btn" data-id="${escHtml(String(item.id))}" title="Delete from database">Delete</button>
            </td>
        </tr>`;
    }).join('');
    tbody.querySelectorAll('.dm-rc-scan-btn').forEach(btn => btn.addEventListener('click', async function() {
        const dir = this.dataset.dir;
        if (!dir) { showPopup({ type: 'warning', title: 'Notice', message: 'No directory path available for this item.', autoClose: 4000 }); return; }
        const orig = this.innerHTML; this.disabled = true; this.innerHTML = '<span class="dm-spinner"></span>';
        try {
            const r = await fetch('/debrid_manager/api/plex_force_scan', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({paths:[dir], empty_trash:false})});
            const d = await r.json();
            if (d.success) {
                this.textContent = 'Sent';
                setTimeout(() => { this.innerHTML = orig; this.disabled = false; }, 3000);
            } else {
                this.innerHTML = orig; this.disabled = false;
                showPopup({ type: 'error', title: 'Error', message: 'Scan failed: ' + (d.error || (d.errors||[]).join(', ') || 'unknown error'), autoClose: 5000 });
            }
        } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Scan failed: ' + e.message, autoClose: 5000 }); this.innerHTML = orig; this.disabled = false; }
    }));
    _rcBindActions(tbody);
    tbody.closest('table').querySelectorAll('.dm-rc-th-sort').forEach(th => {
        th.style.cursor = 'pointer';
        th.onclick = () => _rcSortBy('not-in-plex', th.dataset.sort, th);
    });
}

function _rcRenderTorrentGone(items) {
    const tbody = document.getElementById('rc-tbody-torrent-gone');
    if (!items.length) { tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="5">All collected torrents are present in RD.</td></tr>'; return; }
    tbody.innerHTML = items.map(item => {
        const hash = item.magnet_hash || '';
        const reinsertBtn = hash
            ? `<button class="dm-btn dm-btn-secondary dm-btn-sm dm-rc-reinsert-btn" data-hash="${escHtml(hash)}" data-fname="${escHtml(item.rd_filename||item.title)}" title="Re-add to ${providerLabel}">Reinsert</button>`
            : '';
        const affectedIds = JSON.stringify(item.affected_ids || [item.id]);
        const cnt = (item.affected_count > 1) ? `<span class="dm-rc-affected-count" title="${item.affected_count} DB items share this torrent">${item.affected_count}</span>` : '1';
        return `<tr>
            <td>${_rcTitleCell(item)}</td>
            <td>${_rcTypeBadge(item.type)}</td>
            <td class="dm-rc-col-file">${_rcFileCell(item)}</td>
            <td style="text-align:center">${cnt}</td>
            <td class="dm-col-actions" style="white-space:nowrap">
                ${reinsertBtn}
                <button class="dm-btn dm-btn-secondary dm-btn-sm dm-rc-requeue-btn" data-ids="${escHtml(affectedIds)}" title="Move all ${item.affected_count||1} affected item(s) back to Wanted">Re-queue</button>
                <button class="dm-btn dm-btn-danger dm-btn-sm dm-rc-del-db-btn" data-ids="${escHtml(affectedIds)}" title="Delete all ${item.affected_count||1} affected item(s) from database">Delete</button>
            </td>
        </tr>`;
    }).join('');
    _rcBindActions(tbody);
    tbody.closest('table').querySelectorAll('.dm-rc-th-sort').forEach(th => {
        th.style.cursor = 'pointer';
        th.onclick = () => _rcSortBy('torrent-gone', th.dataset.sort, th);
    });
}

function _rcRenderLost(items) {
    const tbody = document.getElementById('rc-tbody-lost');
    if (!items.length) { tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="4">No lost items.</td></tr>'; return; }
    tbody.innerHTML = items.map(item => `<tr>
        <td>${_rcTitleCell(item)}</td>
        <td>${_rcTypeBadge(item.type)}</td>
        <td class="dm-rc-col-file">${_rcFileCell(item)}</td>
        <td class="dm-col-actions" style="white-space:nowrap">
            <button class="dm-btn dm-btn-secondary dm-btn-sm dm-rc-requeue-btn" data-id="${escHtml(String(item.id))}" title="Move back to Wanted">Re-queue</button>
            <button class="dm-btn dm-btn-danger dm-btn-sm dm-rc-del-db-btn" data-id="${escHtml(String(item.id))}" title="Delete from database">Delete</button>
        </td>
    </tr>`).join('');
    _rcBindActions(tbody);
    tbody.closest('table').querySelectorAll('.dm-rc-th-sort').forEach(th => {
        th.style.cursor = 'pointer';
        th.onclick = () => _rcSortBy('lost', th.dataset.sort, th);
    });
}

function _rcRenderUntracked(items) {
    const tbody = document.getElementById('rc-tbody-untracked');
    if (!items.length) { tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="4">No untracked RD torrents.</td></tr>'; return; }
    tbody.innerHTML = items.map(item => {
        const plexBadge = item.in_plex === true
            ? `<span class="dm-rc-plex-yes" title="Found in ${_dmMsLabel} library">Yes</span>`
            : item.in_plex === false
                ? `<span class="dm-rc-plex-no" title="Not found in ${_dmMsLabel} library">No</span>`
                : '—';
        return `<tr>
        <td class="dm-col-name" title="${escHtml(item.rd_filename)}">${escHtml(item.rd_filename || '—')}</td>
        <td class="dm-rc-col-id"><code class="dm-rc-torrent-id">${escHtml(item.filled_by_torrent_id)}</code></td>
        <td style="text-align:center">${plexBadge}</td>
        <td class="dm-col-actions">
            <button class="dm-btn dm-btn-danger dm-btn-sm dm-rc-del-btn"
                data-tid="${escHtml(item.filled_by_torrent_id)}" title="Delete from debrid provider">Delete from Debrid</button>
        </td>
    </tr>`;
    }).join('');
    tbody.querySelectorAll('.dm-rc-del-btn').forEach(btn => btn.addEventListener('click', async function() {
        const tid = this.dataset.tid;
        const _self = this;
        showPopup({
            type: 'confirm',
            title: 'Delete Torrent',
            message: `Delete torrent ${tid} from ${providerLabel}?`,
            confirmText: 'Delete',
            cancelText: 'Cancel',
            onConfirm: async function() {
                const orig = _self.innerHTML; _self.disabled = true; _self.innerHTML = '<span class="dm-spinner"></span>';
                try {
                    const r = await fetch(`/debrid_manager/api/torrent/${encodeURIComponent(tid)}`, {method:'DELETE'});
                    const d = await r.json();
                    if (d.success) {
                        _self.closest('tr')?.remove();
                        const el = document.getElementById('rc-group-count-untracked');
                        if (el) el.textContent = Math.max(0, parseInt(el.textContent||'0')-1);
                        _rcUpdateBadge();
                    } else { showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + (d.error||'unknown'), autoClose: 5000 }); _self.innerHTML = orig; _self.disabled = false; }
                } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + e.message, autoClose: 5000 }); _self.innerHTML = orig; _self.disabled = false; }
            }
        });
    }));
    tbody.closest('table').querySelectorAll('.dm-rc-th-sort').forEach(th => {
        th.style.cursor = 'pointer';
        th.onclick = () => _rcSortBy('untracked', th.dataset.sort, th);
    });
}

function _rcRenderNas(items) {
    const tbody = document.getElementById('rc-tbody-nas');
    if (!items.length) { tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="4">No NAS items found.</td></tr>'; return; }
    tbody.innerHTML = items.map(item => `<tr>
        <td>${_rcTitleCell(item)}</td>
        <td>${_rcTypeBadge(item.type)}</td>
        <td class="dm-rc-col-file" style="max-width:600px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><span class="dm-pt-folder" title="${escHtml(item.location_on_disk||item.filled_by_file||'')}">${escHtml(item.location_on_disk||item.filled_by_file||'—')}</span></td>
        <td class="dm-col-actions" style="white-space:nowrap">
            <button class="dm-btn dm-btn-danger dm-btn-sm dm-rc-del-db-btn" data-id="${escHtml(String(item.id))}" title="Delete from database">Delete</button>
        </td>
    </tr>`).join('');
    _rcBindActions(tbody);
    tbody.closest('table').querySelectorAll('.dm-rc-th-sort').forEach(th => {
        th.style.cursor = 'pointer';
        th.onclick = () => _rcSortBy('nas', th.dataset.sort, th);
    });
}

function _rcGetIds(btn) {
    // Returns an array of numeric IDs from either data-ids (JSON array) or data-id (single)
    if (btn.dataset.ids) { try { return JSON.parse(btn.dataset.ids).map(Number).filter(Boolean); } catch(e) {} }
    const id = parseInt(btn.dataset.id); return id ? [id] : [];
}

function _rcBindActions(tbody) {
    tbody.querySelectorAll('.dm-rc-reinsert-btn').forEach(btn => btn.addEventListener('click', async function() {
        const hash = this.dataset.hash, fname = this.dataset.fname;
        const orig = this.innerHTML; this.disabled = true; this.textContent = 'Reinserting…';
        try {
            const r = await fetch('/debrid_manager/api/torrent/batch-reinsert', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({torrents:[{id:'',hash,filename:fname}], provider_index: _getReinsertProviderIndex()})});
            const d = await r.json();
            if (d.success && d.reinserted?.length) { this.textContent = 'Done'; setTimeout(()=>{this.innerHTML=orig;this.disabled=false;},3000); }
            else { showPopup({ type: 'error', title: 'Error', message: 'Reinsert failed: ' + (d.failed?.[0]?.error||d.error||'unknown'), autoClose: 5000 }); this.innerHTML=orig; this.disabled=false; }
        } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Reinsert failed: ' + e.message, autoClose: 5000 }); this.innerHTML=orig; this.disabled=false; }
    }));
    tbody.querySelectorAll('.dm-rc-requeue-btn').forEach(btn => btn.addEventListener('click', async function() {
        const ids = _rcGetIds(this);
        if (!ids.length) return;
        const label = ids.length > 1 ? `${ids.length} items` : `item #${ids[0]}`;
        const _self = this;
        showPopup({
            type: 'confirm',
            title: 'Re-queue Items',
            message: `Move ${label} back to Wanted for re-scraping?`,
            confirmText: 'Re-queue',
            cancelText: 'Cancel',
            onConfirm: async function() {
                const orig = _self.innerHTML; _self.disabled = true; _self.textContent = 'Queueing…';
                try {
                    const r = await fetch('/debrid_manager/api/reconcile/bulk_requeue', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({item_ids: ids})});
                    const d = await r.json();
                    if (d.success) { _self.textContent = `Queued (${d.updated})`; setTimeout(()=>{_self.innerHTML=orig;_self.disabled=false;},3000); }
                    else { showPopup({ type: 'error', title: 'Error', message: 'Re-queue failed: ' + (d.error||'unknown'), autoClose: 5000 }); _self.innerHTML=orig; _self.disabled=false; }
                } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Re-queue failed: ' + e.message, autoClose: 5000 }); _self.innerHTML=orig; _self.disabled=false; }
            }
        });
    }));
    tbody.querySelectorAll('.dm-rc-del-db-btn').forEach(btn => btn.addEventListener('click', async function() {
        const ids = _rcGetIds(this);
        if (!ids.length) { showPopup({ type: 'warning', title: 'Notice', message: 'Could not determine item ID (data-id="' + (this.dataset.id||'') + '", data-ids="' + (this.dataset.ids||'') + '")', autoClose: 4000 }); return; }
        const label = ids.length > 1 ? `${ids.length} items` : 'this item';
        const _self = this;
        showPopup({
            type: 'confirm',
            title: 'Delete from Database',
            message: `Permanently delete ${label} from the database? This cannot be undone.`,
            confirmText: 'Delete',
            cancelText: 'Cancel',
            onConfirm: async function() {
                const orig = _self.innerHTML; _self.disabled = true; _self.innerHTML = '<span class="dm-spinner"></span>';
                try {
                    const r = await fetch('/debrid_manager/api/reconcile/bulk_delete_db', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({item_ids: ids})});
                    const d = await r.json();
                    if (d.success) { _self.closest('tr')?.remove(); _rcUpdateBadge(); }
                    else { showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + (d.error||'unknown'), autoClose: 5000 }); _self.innerHTML=orig; _self.disabled=false; }
                } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + e.message, autoClose: 5000 }); _self.innerHTML=orig; _self.disabled=false; }
            }
        });
    }));
}

function _rcShowGroup(name) {
    document.querySelectorAll('.dm-rc-group-panel').forEach(p => p.style.display = 'none');
    document.querySelectorAll('.dm-rc-tab').forEach(t => t.classList.remove('dm-rc-tab-active'));
    const panel = document.getElementById('rc-panel-' + name);
    if (panel) panel.style.display = '';
    const tab = document.querySelector(`.dm-rc-tab[data-rc-group="${name}"]`);
    if (tab) tab.classList.add('dm-rc-tab-active');
    if (name === 'battery') _battInitOnce();
    if (name === 'junk-files' && typeof _jaInitAudit === 'function') _jaInitAudit();
}

function _rcUpdateBadge() {
    if (!_rcData) return;
    const issues = (_rcData.not_in_plex?.length||0) + (_rcData.torrent_gone?.length||0)
                 + (_rcData.lost?.length||0) + (_rcData.untracked_rd?.length||0);
    const badge = document.getElementById('reconcile-tab-badge');
    if (badge) badge.textContent = issues ? issues : '';
}

// ── Reconcile bulk actions ─────────────────────────────────────────────
async function _rcBulkRequeue(itemIds, btnEl) {
    if (!itemIds.length) return;
    showPopup({
        type: 'confirm',
        title: 'Re-queue Items',
        message: `Re-queue ${itemIds.length} item(s) for re-scraping?`,
        confirmText: 'Re-queue',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const orig = btnEl.innerHTML; btnEl.disabled = true; btnEl.textContent = 'Working…';
            try {
                const r = await fetch('/debrid_manager/api/reconcile/bulk_requeue', {
                    method: 'POST', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({item_ids: itemIds})
                });
                const d = await r.json();
                btnEl.textContent = d.success ? `Done (${d.updated})` : 'Failed';
                if (d.success) { window._rcLoaded = false; setTimeout(() => loadReconcile(true), 2000); }
                else setTimeout(() => { btnEl.innerHTML = orig; btnEl.disabled = false; }, 3000);
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + e.message, autoClose: 5000 }); btnEl.innerHTML = orig; btnEl.disabled = false; }
        }
    });
}

async function _rcBulkReinsert(items, btnEl) {
    const torrents = items.filter(i => i.magnet_hash).map(i => ({id:'', hash: i.magnet_hash, filename: i.rd_filename||i.title}));
    if (!torrents.length) { showPopup({ type: 'warning', title: 'Notice', message: 'No items have a torrent hash available.', autoClose: 4000 }); return; }
    showPopup({
        type: 'confirm',
        title: 'Reinsert Torrents',
        message: `Reinsert ${torrents.length} torrent(s) to ${_getReinsertProviderName()}?`,
        confirmText: 'Reinsert',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const orig = btnEl.innerHTML; btnEl.disabled = true; btnEl.textContent = 'Working…';
            try {
                const r = await fetch('/debrid_manager/api/torrent/batch-reinsert', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({torrents, provider_index: _getReinsertProviderIndex()})
                });
                const d = await r.json();
                btnEl.textContent = d.success ? `Done (${d.reinserted?.length||0})` : 'Failed';
                setTimeout(() => { btnEl.innerHTML = orig; btnEl.disabled = false; }, 3000);
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + e.message, autoClose: 5000 }); btnEl.innerHTML = orig; btnEl.disabled = false; }
        }
    });
}

async function _rcBulkDeleteDB(items, btnEl) {
    const ids = items.flatMap(i => i.affected_ids || (i.id ? [i.id] : []));
    if (!ids.length) { showPopup({ type: 'warning', title: 'Notice', message: 'No items to delete.', autoClose: 4000 }); return; }
    showPopup({
        type: 'confirm',
        title: 'Delete from Database',
        message: `Permanently delete ${ids.length} item(s) from the database? This cannot be undone.`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const orig = btnEl.innerHTML; btnEl.disabled = true; btnEl.textContent = 'Deleting…';
            try {
                const r = await fetch('/debrid_manager/api/reconcile/bulk_delete_db', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({item_ids: ids})
                });
                const d = await r.json();
                btnEl.textContent = d.success ? `Done (${d.deleted})` : 'Failed';
                if (d.success) { window._rcLoaded = false; setTimeout(() => loadReconcile(true), 2000); }
                else setTimeout(() => { btnEl.innerHTML = orig; btnEl.disabled = false; }, 3000);
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + e.message, autoClose: 5000 }); btnEl.innerHTML = orig; btnEl.disabled = false; }
        }
    });
}

async function _rcBulkDeleteRD(items, btnEl) {
    if (!items.length) { showPopup({ type: 'warning', title: 'Notice', message: 'Nothing to delete.', autoClose: 4000 }); return; }
    showPopup({
        type: 'confirm',
        title: 'Delete from Debrid',
        message: `Delete ${items.length} torrent(s) from ${providerLabel}? This cannot be undone.`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const orig = btnEl.innerHTML; btnEl.disabled = true; btnEl.textContent = 'Deleting…';
            const tids = items.map(i => i.filled_by_torrent_id).filter(Boolean);
            try {
                const r = await fetch('/debrid_manager/api/torrent/batch-delete', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({ids: tids})
                });
                const d = await r.json();
                btnEl.textContent = d.success ? `Done (${d.deleted?.length||0})` : 'Failed';
                if (d.success) { window._rcLoaded = false; setTimeout(() => loadReconcile(true), 2000); }
                else setTimeout(() => { btnEl.innerHTML = orig; btnEl.disabled = false; }, 3000);
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + e.message, autoClose: 5000 }); btnEl.innerHTML = orig; btnEl.disabled = false; }
        }
    });
}

async function _rcBulkScan(items, btnEl) {
    const dirs = [...new Set(items.map(i => {
        // Prefer location_on_disk (full symlink path) over filled_by_file (may be bare filename)
        const src = i.location_on_disk || i.filled_by_file || '';
        if (!src) return '';
        const normalized = src.replace(/\\/g, '/');
        const parts = normalized.split('/');
        return parts.length >= 2 ? parts.slice(0,-1).join('/') : '';
    }).filter(Boolean))];
    if (!dirs.length) { showPopup({ type: 'warning', title: 'Notice', message: 'No directory paths available.', autoClose: 4000 }); return; }
    showPopup({
        type: 'confirm',
        title: 'Trigger Scan',
        message: `Trigger ${_dmMsLabel} scan for ${dirs.length} ${dirs.length === 1 ? 'directory' : 'directories'}?`,
        confirmText: 'Scan',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const orig = btnEl.innerHTML; btnEl.disabled = true; btnEl.textContent = 'Scanning…';
            try {
                const r = await fetch('/debrid_manager/api/plex_force_scan', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({paths: dirs, empty_trash: false})
                });
                const d = await r.json();
                if (d.success) {
                    btnEl.textContent = 'Sent';
                    setTimeout(() => { btnEl.innerHTML = orig; btnEl.disabled = false; }, 3000);
                } else {
                    btnEl.innerHTML = orig; btnEl.disabled = false;
                    showPopup({ type: 'error', title: 'Error', message: 'Scan failed: ' + (d.error || (d.errors||[]).join(', ') || 'unknown error'), autoClose: 5000 });
                }
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + e.message, autoClose: 5000 }); btnEl.innerHTML = orig; btnEl.disabled = false; }
        }
    });
}

function _rcRenderAll(data) {
    _rcData = data;
    // Stat header
    const _fmt = n => n ? n.toLocaleString() : '—';
    document.getElementById('rc-count-healthy').textContent      = _fmt(data.healthy_count);
    const _pillEl = document.getElementById('rc-healthy-pill-toolbar');
    if (_pillEl) _pillEl.style.display = '';
    document.getElementById('rc-count-not-in-plex').textContent  = _fmt(data.not_in_plex?.length);
    document.getElementById('rc-count-torrent-gone').textContent = _fmt(data.torrent_gone?.length);
    document.getElementById('rc-count-lost').textContent         = _fmt(data.lost?.length);
    document.getElementById('rc-count-untracked').textContent    = _fmt(data.untracked_rd?.length);
    const _s = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = _fmt(val); };
    _s('rc-stat-rd',     data.rd_total);
    _s('rc-stat-db',     data.db_total);
    _s('rc-stat-plex',   data.plex_total);
    _s('rc-stat-rclone', data.rclone_total);
    _s('rc-stat-nas',    data.nas_total);
    _s('rc-count-nas',   data.nas_items?.length);
    const nasWrap = document.getElementById('rc-stat-nas-wrap');
    if (nasWrap) nasWrap.style.display = data.nas_total > 0 ? '' : 'none';
    const nasTab = document.getElementById('rc-tab-nas');
    if (nasTab) nasTab.style.display = (data.nas_items?.length > 0) ? '' : 'none';
    // Toolbar info (hidden now but keep for compat)
    const info = document.getElementById('rc-info-text');
    if (info) info.textContent = '';
    _rcRenderNotInPlex(data.not_in_plex||[]);
    _rcRenderTorrentGone(data.torrent_gone||[]);
    _rcRenderLost(data.lost||[]);
    _rcRenderUntracked(data.untracked_rd||[]);
    _rcRenderNas(data.nas_items||[]);
    _rcUpdateBadge();

    // Wire bulk buttons (use onclick to avoid duplicate listeners on refresh)
    const btnScanAll = document.getElementById('rc-bulk-scan-all');
    if (btnScanAll) btnScanAll.onclick = function() { _rcBulkScan(_rcData.not_in_plex||[], this); };
    const btnReinsertAll = document.getElementById('rc-bulk-reinsert-all');
    if (btnReinsertAll) btnReinsertAll.onclick = function() { _rcBulkReinsert(_rcData.torrent_gone||[], this); };
    const btnRequeueTg = document.getElementById('rc-bulk-requeue-tg');
    if (btnRequeueTg) btnRequeueTg.onclick = function() { _rcBulkRequeue((_rcData.torrent_gone||[]).map(i=>i.id).filter(Boolean), this); };
    const btnRequeueLost = document.getElementById('rc-bulk-requeue-lost');
    if (btnRequeueLost) btnRequeueLost.onclick = function() { _rcBulkRequeue((_rcData.lost||[]).map(i=>i.id).filter(Boolean), this); };
    const btnDelNip = document.getElementById('rc-bulk-del-nip');
    if (btnDelNip) btnDelNip.onclick = function() { _rcBulkDeleteDB(_rcData.not_in_plex||[], this); };
    const btnDelTg = document.getElementById('rc-bulk-del-tg');
    if (btnDelTg) btnDelTg.onclick = function() { _rcBulkDeleteDB(_rcData.torrent_gone||[], this); };
    const btnDelLost = document.getElementById('rc-bulk-del-lost');
    if (btnDelLost) btnDelLost.onclick = function() { _rcBulkDeleteDB(_rcData.lost||[], this); };
    const btnDelNoplex = document.getElementById('rc-bulk-del-noplex');
    if (btnDelNoplex) btnDelNoplex.onclick = function() { _rcBulkDeleteRD((_rcData.untracked_rd||[]).filter(i=>!i.in_plex), this); };
    const btnDelAllRd = document.getElementById('rc-bulk-del-all-rd');
    if (btnDelAllRd) btnDelAllRd.onclick = function() { _rcBulkDeleteRD(_rcData.untracked_rd||[], this); };

    // Wire tab clicks
    document.querySelectorAll('.dm-rc-tab').forEach(tab => {
        tab.onclick = () => _rcShowGroup(tab.dataset.rcGroup);
        tab.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _rcShowGroup(tab.dataset.rcGroup); } };
    });

    // Auto-select first non-empty group
    const order = ['not-in-plex','torrent-gone','lost','untracked','nas'];
    const counts = {
        'not-in-plex':   data.not_in_plex?.length||0,
        'torrent-gone':  data.torrent_gone?.length||0,
        'lost':          data.lost?.length||0,
        'untracked':     data.untracked_rd?.length||0,
        'nas':           data.nas_items?.length||0,
    };
    const first = order.find(g => counts[g] > 0) || 'not-in-plex';
    _rcShowGroup(first);
}

async function loadReconcile(force) {
    const loading = document.getElementById('rc-loading');
    const content = document.getElementById('rc-content');
    const errEl   = document.getElementById('rc-error');
    if (!loading) return;
    loading.style.display = ''; content.style.display = 'none'; errEl.style.display = 'none';
    try {
        const r = await fetch('/debrid_manager/api/reconcile' + (force ? '?force=1' : ''));
        const data = await r.json();
        if (!data.success) throw new Error(data.error || 'Failed to load');
        _rcRenderAll(data);
        loading.style.display = 'none'; content.style.display = ''; window._rcLoaded = true;
    } catch(e) {
        loading.style.display = 'none';
        errEl.textContent = 'Failed to load: ' + e.message; errEl.style.display = '';
    }
}

document.getElementById('rc-reload-btn')?.addEventListener('click', () => { window._rcLoaded = false; loadReconcile(true); });
document.getElementById('rc-clear-cache-btn')?.addEventListener('click', async function() {
    const orig = this.innerHTML; this.disabled = true; this.textContent = 'Clearing…';
    try {
        await fetch('/debrid_manager/api/clear_cache', {method: 'POST'});
        this.textContent = 'Cleared';
        window._rcLoaded = false;
        setTimeout(() => { this.innerHTML = orig; this.disabled = false; loadReconcile(true); }, 800);
    } catch(e) { this.innerHTML = orig; this.disabled = false; }
});
