/* Moved from templates/debrid_manager.html. */
// ── PLEX TRASH TAB ─────────────────────────────────────────────────────────
let _ptSelected = new Set();
let _ptInRd = [];
let _ptNotInRd = [];
let _ptLoaded = false;
let _ptActiveSubtab = 'debrid';

function _ptSwitchSubtab(group) {
    _ptActiveSubtab = group;
    document.querySelectorAll('.dm-pt-subtab').forEach(btn => {
        btn.classList.toggle('dm-rc-tab-active', btn.dataset.ptGroup === group);
    });
    const debridPanel = document.getElementById('pt-panel-debrid');
    const usenetPanel = document.getElementById('pt-panel-usenet');
    if (debridPanel) debridPanel.style.display = group === 'debrid' ? '' : 'none';
    if (usenetPanel) usenetPanel.style.display = group === 'usenet' ? '' : 'none';
}

document.querySelectorAll('.dm-pt-subtab').forEach(btn => {
    btn.addEventListener('click', () => _ptSwitchSubtab(btn.dataset.ptGroup));
});

function _ptUpdatePurgeBtn() {
    const btn = document.getElementById('pt-purge-btn');
    const countEl = document.getElementById('pt-selected-count');
    if (!btn) return;
    countEl.textContent = _ptSelected.size;
    btn.disabled = _ptSelected.size === 0;
}

function _ptRenderGroup(tbodyId, items, groupId) {
    const tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    if (!items.length) {
        tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="5">None</td></tr>';
        return;
    }
    const isInRd = groupId === 'in_rd';
    tbody.innerHTML = items.map((item, idx) => {
        const checked = _ptSelected.has(item.rating_key) ? 'checked' : '';
        const typeLabel = item.media_type === 'show' ? '<span class="dm-type-badge dm-type-tv">Show</span>' : '<span class="dm-type-badge dm-type-movie">Movie</span>';
        const year = item.year ? ` (${item.year})` : '';
        const titleMatchCount = item.title_match_count || 0;
        const titleMatchBadge = titleMatchCount > 0
            ? `<span class="dm-dupe-badge dm-title-dupe-badge" title="${titleMatchCount} torrent(s) with matching title in RD library">${titleMatchCount}× title</span>`
            : '';

        // Build Files column: bad file (red, top) + good file (green, bottom) when both known
        // Each file row gets inline action buttons
        let filesHtml = '';
        const _shorten = (s, n) => s && s.length > n ? s.substring(0, n) + '…' : (s || '');
        const rk = escHtml(item.rating_key);
        if (item.bad_file && item.has_good_file) {
            const goodName = item.good_file || item.rd_filename || item.folder_name || '';
            const goodHash = escHtml(item.rd_hash || '');
            const goodFname = escHtml(goodName);
            // Bad file: Reinsert disabled (no RD hash for orphaned file) + Delete
            // Look up part IDs for per-file deletion
            const _pids = item.part_ids || {};
            const badPart = _pids[item.bad_file] || {};
            const goodPart = _pids[goodName] || {};
            const badMid = escHtml(badPart.media_id || '');
            const badPid = escHtml(badPart.part_id || '');
            const goodMid = escHtml(goodPart.media_id || '');
            const goodPid = escHtml(goodPart.part_id || '');
            // Bad file: Reinsert disabled (no RD hash for orphaned file) + Delete (per-part if IDs known)
            const badBtns = `<span class="dm-pt-file-btns">`
                + `<button class="dm-btn dm-btn-xs dm-pt-fbtn" data-faction="reinsert" data-hash="" data-fname="" data-rk="${rk}" disabled title="No RD hash available for this file">Reinsert</button>`
                + `<button class="dm-btn dm-btn-xs dm-pt-fbtn dm-pt-fbtn-del" data-faction="delete-part" data-rk="${rk}" data-mid="${badMid}" data-pid="${badPid}" data-protected="1" title="Remove this file from Plex">Delete</button>`
                + `</span>`;
            // Good file: Reinsert (if hash available) + Delete (per-part if IDs known)
            const goodReinsert = goodHash
                ? `<button class="dm-btn dm-btn-xs dm-pt-fbtn" data-faction="reinsert" data-hash="${goodHash}" data-fname="${goodFname}" data-rk="${rk}" title="Re-add to ${providerLabel}">Reinsert</button>`
                : '';
            const goodBtns = `<span class="dm-pt-file-btns">${goodReinsert}`
                + `<button class="dm-btn dm-btn-xs dm-pt-fbtn dm-pt-fbtn-del" data-faction="delete-part" data-rk="${rk}" data-mid="${goodMid}" data-pid="${goodPid}" data-protected="1" title="Remove this file from Plex">Delete</button>`
                + `</span>`;
            const orphanLabel = item.bad_file_orphaned ? `<span class="dm-pt-badge-orphaned">Orphaned</span>` : '';
            filesHtml = `<div class="dm-pt-file-row"><span class="dm-pt-file-bad" title="${escHtml(item.bad_file)}">${escHtml(item.bad_file)}</span>${orphanLabel}${badBtns}</div>`
                      + `<div class="dm-pt-file-row"><span class="dm-pt-file-good" title="${escHtml(goodName)}">${escHtml(goodName)}</span>${goodBtns}</div>`;
        } else if (item.bad_file) {
            filesHtml = `<div class="dm-pt-file-row"><span class="dm-pt-file-bad" title="${escHtml(item.bad_file)}">${escHtml(item.bad_file)}</span></div>`;
        } else {
            filesHtml = `<div class="dm-pt-file-row"><span class="dm-pt-folder" title="${escHtml(item.folder_name)}">${escHtml(item.folder_name)}</span></div>`;
        }

        let actions = '';
        if (isInRd && item.file_dir) {
            actions = `<button class="dm-btn dm-btn-secondary dm-btn-sm dm-pt-action-btn" data-action="scan" data-idx="${idx}" title="Trigger Plex scan on this path">Scan</button>`;
        }
        if (!isInRd && item.rd_hash) {
            actions = `<button class="dm-btn dm-btn-secondary dm-btn-sm dm-pt-action-btn" data-action="reinsert" data-idx="${idx}" title="Re-add torrent to ${providerLabel}">Reinsert</button>`;
        }
        return `<tr>
            <td class="dm-col-check"><input type="checkbox" data-rk="${escHtml(item.rating_key)}" data-group="${groupId}" ${checked}></td>
            <td><span class="dm-pt-title">${escHtml(item.title)}${escHtml(year)}</span>${titleMatchBadge}</td>
            <td class="dm-pt-col-type">${typeLabel}</td>
            <td class="dm-pt-col-files">${filesHtml}</td>
            <td class="dm-col-actions">${actions}</td>
        </tr>`;
    }).join('');

    tbody.querySelectorAll('input[type=checkbox][data-rk]').forEach(cb => {
        cb.addEventListener('change', () => {
            if (cb.checked) _ptSelected.add(cb.dataset.rk);
            else _ptSelected.delete(cb.dataset.rk);
            _ptUpdatePurgeBtn();
        });
    });

    // Action button handlers
    tbody.querySelectorAll('.dm-pt-action-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const action = btn.dataset.action;
            const idx = parseInt(btn.dataset.idx);
            const item = isInRd ? _ptInRd[idx] : _ptNotInRd[idx];
            if (!item) return;
            const origHtml = btn.innerHTML;
            btn.disabled = true;
            if (action === 'scan') {
                btn.textContent = 'Scanning…';
                try {
                    const r = await fetch('/debrid_manager/api/plex_force_scan', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({paths: [item.file_dir]}),
                    });
                    const data = await r.json();
                    if (data.success) {
                        btn.textContent = 'Done';
                        btn.classList.add('dm-pt-action-ok');
                        setTimeout(() => { btn.textContent = 'Scan'; btn.classList.remove('dm-pt-action-ok'); btn.disabled = false; }, 3000);
                    } else {
                        showPopup({ type: 'error', title: 'Error', message: 'Scan failed: ' + (data.errors?.join(', ') || data.error || 'unknown'), autoClose: 5000 });
                        btn.innerHTML = origHtml; btn.disabled = false;
                    }
                } catch(e) {
                    showPopup({ type: 'error', title: 'Error', message: 'Scan failed: ' + e.message, autoClose: 5000 });
                    btn.innerHTML = origHtml; btn.disabled = false;
                }
            } else if (action === 'reinsert') {
                btn.textContent = 'Reinserting…';
                try {
                    const r = await fetch('/debrid_manager/api/torrent/batch-reinsert', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({torrents: [{id: '', hash: item.rd_hash, filename: item.rd_filename || item.folder_name}], provider_index: _getReinsertProviderIndex()}),
                    });
                    const data = await r.json();
                    if (data.success && data.reinserted?.length) {
                        btn.textContent = 'Done';
                        btn.classList.add('dm-pt-action-ok');
                        setTimeout(() => { btn.textContent = 'Reinsert'; btn.classList.remove('dm-pt-action-ok'); btn.disabled = false; }, 3000);
                    } else {
                        const err = data.failed?.[0]?.error || data.error || 'unknown';
                        showPopup({ type: 'error', title: 'Error', message: 'Reinsert failed: ' + err, autoClose: 5000 });
                        btn.innerHTML = origHtml; btn.disabled = false;
                    }
                } catch(e) {
                    showPopup({ type: 'error', title: 'Error', message: 'Reinsert failed: ' + e.message, autoClose: 5000 });
                    btn.innerHTML = origHtml; btn.disabled = false;
                }
            }
        });
    });

    // Per-file inline button handlers (.dm-pt-fbtn)
    tbody.querySelectorAll('.dm-pt-fbtn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const faction = btn.dataset.faction;
            const origHtml = btn.innerHTML;
            if (btn.disabled) return;
            btn.disabled = true;
            if (faction === 'reinsert') {
                const hash = btn.dataset.hash;
                const fname = btn.dataset.fname;
                if (!hash) { btn.disabled = false; return; }
                btn.textContent = 'Reinserting…';
                try {
                    const r = await fetch('/debrid_manager/api/torrent/batch-reinsert', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({torrents: [{id: '', hash, filename: fname}], provider_index: _getReinsertProviderIndex()}),
                    });
                    const data = await r.json();
                    if (data.success && data.reinserted?.length) {
                        btn.textContent = 'Done';
                        btn.classList.add('dm-pt-action-ok');
                        setTimeout(() => { btn.textContent = 'Reinsert'; btn.classList.remove('dm-pt-action-ok'); btn.disabled = false; }, 3000);
                    } else {
                        const err = data.failed?.[0]?.error || data.error || 'unknown';
                        showPopup({ type: 'error', title: 'Error', message: 'Reinsert failed: ' + err, autoClose: 5000 });
                        btn.innerHTML = origHtml; btn.disabled = false;
                    }
                } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Reinsert failed: ' + e.message, autoClose: 5000 }); btn.innerHTML = origHtml; btn.disabled = false; }
            } else if (faction === 'delete-part') {
                const ratingKey = btn.dataset.rk;
                const mediaId = btn.dataset.mid;
                const partId = btn.dataset.pid;
                showPopup({
                    type: 'confirm',
                    title: 'Remove File',
                    message: 'Remove this file from Plex? This cannot be undone.',
                    confirmText: 'Remove',
                    cancelText: 'Cancel',
                    onConfirm: async function() {
                        btn.textContent = 'Deleting…';
                        try {
                            const scanItem = [..._ptInRd, ..._ptNotInRd].find(i => i.rating_key === ratingKey);
                            const r = await fetch('/debrid_manager/api/plex_delete_part', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({rating_key: ratingKey, media_id: mediaId, part_id: partId}),
                            });
                            const data = await r.json();
                            if (!data.success) { showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + (data.error || 'unknown'), autoClose: 5000 }); btn.innerHTML = origHtml; btn.disabled = false; return; }
                            const inRdItem = _ptInRd.find(i => i.rating_key === ratingKey);
                            if (inRdItem?.has_good_file) {
                                inRdItem.bad_file = null; inRdItem.has_good_file = false; inRdItem.bad_file_orphaned = false;
                                _ptRenderGroup('pt-in-rd-tbody', _ptInRd, 'in_rd');
                            } else {
                                _ptInRd = _ptInRd.filter(i => i.rating_key !== ratingKey);
                                _ptNotInRd = _ptNotInRd.filter(i => i.rating_key !== ratingKey);
                                _ptSelected.delete(ratingKey);
                                _ptRenderGroup('pt-in-rd-tbody', _ptInRd, 'in_rd');
                                _ptRenderGroup('pt-not-in-rd-tbody', _ptNotInRd, 'not_in_rd');
                            }
                            _ptUpdateCounts(); _ptUpdatePurgeBtn();
                            if (scanItem?.file_dir) {
                                fetch('/debrid_manager/api/plex_force_scan', {
                                    method: 'POST', headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({paths: [scanItem.file_dir], empty_trash: false}),
                                }).catch(() => {});
                            }
                        } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + e.message, autoClose: 5000 }); btn.innerHTML = origHtml; btn.disabled = false; }
                    },
                    onCancel: function() { btn.disabled = false; }
                });
                return;
            }
        });
    });
}

function _ptUpdateCounts() {
    const inCount = document.getElementById('pt-in-rd-count');
    const outCount = document.getElementById('pt-not-in-rd-count');
    const tabCount = document.getElementById('plex-trash-tab-count');
    const debridTotal = _ptInRd.length + _ptNotInRd.length;
    if (inCount) inCount.textContent = _ptInRd.length;
    if (outCount) outCount.textContent = _ptNotInRd.length;
    // Update debrid sub-tab badge
    const debridSubtabBadge = document.getElementById('pt-subtab-debrid-count');
    if (debridSubtabBadge) debridSubtabBadge.textContent = debridTotal || '';
    // Total tab badge = all items across both sub-tabs
    const usenetTotal = _ptInUsenet.length + _ptNotUsenet.length;
    const grand = debridTotal + usenetTotal;
    if (tabCount) tabCount.textContent = grand ? grand : '';
    const info = document.getElementById('pt-info-text');
    if (info) info.textContent = grand ? `${grand} item${grand !== 1 ? 's' : ''} in Plex Trash` : '';
}

async function loadPlexTrash(force) {
    const loading = document.getElementById('pt-loading');
    const content = document.getElementById('pt-content');
    const errEl = document.getElementById('pt-error');
    if (!loading) return;
    loading.style.display = '';
    content.style.display = 'none';
    errEl.style.display = 'none';
    try {
        const url = '/debrid_manager/api/plex_trash_items' + (force ? '?force=1' : '');
        const r = await fetch(url);
        const data = await r.json();
        if (!data.success) throw new Error(data.error || 'Failed to load');
        _ptInRd = data.in_rd || [];
        _ptNotInRd = data.not_in_rd || [];
        _ptInUsenet = data.in_usenet || [];
        _ptNotUsenet = data.not_in_usenet || [];
        _ptSelected.clear();
        _ptRenderGroup('pt-in-rd-tbody', _ptInRd, 'in_rd');
        _ptRenderGroup('pt-not-in-rd-tbody', _ptNotInRd, 'not_in_rd');
        _ptRenderUsenetGroup('pt-in-usenet-tbody', _ptInUsenet);
        _ptRenderUsenetGroup('pt-not-usenet-tbody', _ptNotUsenet);
        _ptUpdateCounts();
        _ptUpdateUsenetCounts();
        _ptUpdatePurgeBtn();
        loading.style.display = 'none';
        content.style.display = '';
        _ptLoaded = true;
        // Auto-switch to usenet tab if there are usenet items but no debrid items
        const hasDebrid = (_ptInRd.length + _ptNotInRd.length) > 0;
        const hasUsenet = (_ptInUsenet.length + _ptNotUsenet.length) > 0;
        if (!hasDebrid && hasUsenet) _ptSwitchSubtab('usenet');
        else _ptSwitchSubtab(_ptActiveSubtab);
        // If RD library was not ready, show a notice and auto-refresh in 15s
        const rdBanner = document.getElementById('pt-rd-loading-banner');
        if (data.rd_library_status === 'loading') {
            if (rdBanner) {
                rdBanner.style.display = '';
                rdBanner.textContent = 'RD library is loading in the background — classification will update automatically.';
            }
            setTimeout(() => loadPlexTrash(true), 15000);
        } else {
            if (rdBanner) rdBanner.style.display = 'none';
        }
    } catch(e) {
        loading.style.display = 'none';
        errEl.textContent = 'Failed to load Plex Trash: ' + e.message;
        errEl.style.display = '';
    }
}

// Select all checkboxes in a group
document.getElementById('pt-select-in-rd')?.addEventListener('click', () => {
    _ptInRd.forEach(item => _ptSelected.add(item.rating_key));
    _ptRenderGroup('pt-in-rd-tbody', _ptInRd, 'in_rd');
    _ptUpdatePurgeBtn();
});
document.getElementById('pt-select-not-in-rd')?.addEventListener('click', () => {
    _ptNotInRd.forEach(item => _ptSelected.add(item.rating_key));
    _ptRenderGroup('pt-not-in-rd-tbody', _ptNotInRd, 'not_in_rd');
    _ptUpdatePurgeBtn();
});

// Group select-all checkboxes
document.getElementById('pt-in-rd-select-all')?.addEventListener('change', function() {
    if (this.checked) _ptInRd.forEach(item => _ptSelected.add(item.rating_key));
    else _ptInRd.forEach(item => _ptSelected.delete(item.rating_key));
    _ptRenderGroup('pt-in-rd-tbody', _ptInRd, 'in_rd');
    _ptUpdatePurgeBtn();
});
document.getElementById('pt-not-in-rd-select-all')?.addEventListener('change', function() {
    if (this.checked) _ptNotInRd.forEach(item => _ptSelected.add(item.rating_key));
    else _ptNotInRd.forEach(item => _ptSelected.delete(item.rating_key));
    _ptRenderGroup('pt-not-in-rd-tbody', _ptNotInRd, 'not_in_rd');
    _ptUpdatePurgeBtn();
});

document.getElementById('pt-reload-btn')?.addEventListener('click', () => loadPlexTrash(true));

document.getElementById('pt-purge-btn')?.addEventListener('click', async function() {
    const keys = [..._ptSelected];
    if (!keys.length) return;
    const total = _ptInRd.length + _ptNotInRd.length;
    const deletingItems = [..._ptInRd, ..._ptNotInRd].filter(i => _ptSelected.has(i.rating_key));
    const titles = deletingItems.map(i => i.title + (i.year ? ` (${i.year})` : ''));
    const preview = titles.slice(0, 5).join('\n') + (titles.length > 5 ? `\n…and ${titles.length - 5} more` : '');
    const origHtml = this.innerHTML;
    const _btn = this;
    showPopup({
        type: 'confirm',
        title: 'Delete from Plex',
        message: `Permanently delete ${keys.length} item${keys.length !== 1 ? 's' : ''} from Plex?\n\nThis cannot be undone.\n\n${preview}`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            _btn.disabled = true;
            _btn.innerHTML = '<span class="dm-spinner"></span> Deleting…';
            try {
                const r = await fetch('/debrid_manager/api/plex_trash_purge', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({rating_keys: keys}),
                });
                const data = await r.json();
                if (data.success) {
                    const deletedSet = new Set(data.deleted);
                    // Collect file_dir paths of successfully deleted items for auto-scan
                    const scanPaths = deletingItems
                        .filter(i => deletedSet.has(i.rating_key) && i.file_dir)
                        .map(i => i.file_dir);
                    _ptInRd = _ptInRd.filter(i => !deletedSet.has(i.rating_key));
                    _ptNotInRd = _ptNotInRd.filter(i => !deletedSet.has(i.rating_key));
                    data.deleted.forEach(rk => _ptSelected.delete(rk));
                    _ptRenderGroup('pt-in-rd-tbody', _ptInRd, 'in_rd');
                    _ptRenderGroup('pt-not-in-rd-tbody', _ptNotInRd, 'not_in_rd');
                    _ptUpdateCounts();
                    if (data.failed.length) {
                        showPopup({ type: 'warning', title: 'Notice', message: `${data.deleted.length} deleted. ${data.failed.length} failed:\n` +
                            data.failed.map(f => `${f.key}: ${f.error}`).join('\n'), autoClose: 4000 });
                    }
                    // Auto-scan the affected directories so Plex re-discovers working files
                    if (scanPaths.length) {
                        fetch('/debrid_manager/api/plex_force_scan', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({paths: scanPaths, empty_trash: false}),
                        }).catch(() => {});
                    }
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Purge failed: ' + (data.error || 'unknown'), autoClose: 5000 });
                }
            } catch(e) {
                showPopup({ type: 'error', title: 'Error', message: 'Purge failed: ' + e.message, autoClose: 5000 });
            }
            _ptUpdatePurgeBtn();
            _btn.innerHTML = origHtml;
            _btn.disabled = _ptSelected.size === 0;
        }
    });
});

// ── BULK ACTION HELPERS ─────────────────────────────────────────────────────
async function _ptBulkDelete(items, label) {
    if (!items.length) { showPopup({ type: 'warning', title: 'Notice', message: `No ${label} items to delete.`, autoClose: 4000 }); return; }
    const preview = items.slice(0, 5).map(i => i.title + (i.year ? ` (${i.year})` : '')).join('\n')
        + (items.length > 5 ? `\n…and ${items.length - 5} more` : '');
    const btn = event.currentTarget;
    const origHtml = btn.innerHTML;
    showPopup({
        type: 'confirm',
        title: 'Delete from Plex',
        message: `Permanently delete ${items.length} ${label} item${items.length !== 1 ? 's' : ''} from Plex?\n\nThis cannot be undone.\n\n${preview}`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            btn.disabled = true;
            btn.innerHTML = '<span class="dm-spinner"></span>';
            try {
                const keys = items.map(i => i.rating_key);
                const r = await fetch('/debrid_manager/api/plex_trash_purge', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({rating_keys: keys}),
                });
                const data = await r.json();
                if (data.success) {
                    const deletedSet = new Set(data.deleted);
                    const scanPaths = items.filter(i => deletedSet.has(i.rating_key) && i.file_dir).map(i => i.file_dir);
                    _ptInRd = _ptInRd.filter(i => !deletedSet.has(i.rating_key));
                    _ptNotInRd = _ptNotInRd.filter(i => !deletedSet.has(i.rating_key));
                    data.deleted.forEach(rk => _ptSelected.delete(rk));
                    _ptRenderGroup('pt-in-rd-tbody', _ptInRd, 'in_rd');
                    _ptRenderGroup('pt-not-in-rd-tbody', _ptNotInRd, 'not_in_rd');
                    _ptUpdateCounts(); _ptUpdatePurgeBtn();
                    if (scanPaths.length) fetch('/debrid_manager/api/plex_force_scan', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({paths: scanPaths, empty_trash: false}),
                    }).catch(() => {});
                    if (data.failed.length) showPopup({ type: 'warning', title: 'Notice', message: `${data.deleted.length} deleted. ${data.failed.length} failed.`, autoClose: 4000 });
                } else { showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + (data.error || 'unknown'), autoClose: 5000 }); }
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + e.message, autoClose: 5000 }); }
            btn.innerHTML = origHtml; btn.disabled = false;
        }
    });
}

async function _ptBulkReinsert(items, label) {
    const withHash = items.filter(i => i.rd_hash);
    if (!withHash.length) { showPopup({ type: 'warning', title: 'Notice', message: `No ${label} items with a known hash to reinsert.`, autoClose: 4000 }); return; }
    const btn = event.currentTarget;
    const origHtml = btn.innerHTML;
    showPopup({
        type: 'confirm',
        title: 'Reinsert Torrents',
        message: `Reinsert ${withHash.length} ${label} torrent${withHash.length !== 1 ? 's' : ''} to ${_getReinsertProviderName()}?`,
        confirmText: 'Reinsert',
        cancelText: 'Cancel',
        onConfirm: async function() {
            btn.disabled = true;
            btn.innerHTML = '<span class="dm-spinner"></span>';
            try {
                const torrents = withHash.map(i => ({id: '', hash: i.rd_hash, filename: i.rd_filename || i.folder_name || ''}));
                const r = await fetch('/debrid_manager/api/torrent/batch-reinsert', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({torrents, provider_index: _getReinsertProviderIndex()}),
                });
                const data = await r.json();
                const doneCount = data.reinserted?.length || 0;
                const failCount = data.failed?.length || 0;
                showPopup({ type: 'success', title: 'Success', message: `Reinserted: ${doneCount}${failCount ? `, Failed: ${failCount}` : ''}`, autoClose: 4000 });
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Reinsert failed: ' + e.message, autoClose: 5000 }); }
            btn.innerHTML = origHtml; btn.disabled = false;
        }
    });
}

document.getElementById('pt-bulk-scan-all')?.addEventListener('click', async function() {
    const paths = _ptInRd.filter(i => i.file_dir).map(i => i.file_dir);
    if (!paths.length) { showPopup({ type: 'warning', title: 'Notice', message: 'No scan paths available.', autoClose: 4000 }); return; }
    this.disabled = true;
    const origHtml = this.innerHTML;
    this.innerHTML = '<span class="dm-spinner"></span>';
    try {
        await fetch('/debrid_manager/api/plex_force_scan', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({paths: [...new Set(paths)], empty_trash: false}),
        });
        this.textContent = 'Done';
        setTimeout(() => { this.innerHTML = origHtml; this.disabled = false; }, 2500);
    } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Scan failed: ' + e.message, autoClose: 5000 }); this.innerHTML = origHtml; this.disabled = false; }
});

document.getElementById('pt-bulk-reinsert-good')?.addEventListener('click', function() {
    _ptBulkReinsert(_ptInRd.filter(i => i.rd_hash && i.has_good_file), 'good-file');
});
document.getElementById('pt-bulk-del-bad')?.addEventListener('click', async function() {
    const items = _ptInRd.filter(i => i.bad_file);
    if (!items.length) { showPopup({ type: 'warning', title: 'Notice', message: 'No bad-file items to delete.', autoClose: 4000 }); return; }
    const preview = items.slice(0, 5).map(i => i.title + (i.year ? ` (${i.year})` : '')).join('\n')
        + (items.length > 5 ? `\n…and ${items.length - 5} more` : '');
    const btn = event.currentTarget;
    const origHtml = btn.innerHTML;
    showPopup({
        type: 'confirm',
        title: 'Delete Bad Files',
        message: `Delete bad (orphaned) file from ${items.length} item${items.length !== 1 ? 's' : ''}?\n\nItems with a good file will have only the orphaned version removed. Items with no good file will be fully deleted from Plex.\n\n${preview}`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            btn.disabled = true;
            btn.innerHTML = '<span class="dm-spinner"></span>';
            let deleted = 0, failed = 0;
            try {
                for (const item of items) {
                    const partInfo = (item.part_ids || {})[item.bad_file] || {};
                    const mediaId = partInfo.media_id;
                    const partId = partInfo.part_id;
                    try {
                        const r = await fetch('/debrid_manager/api/plex_delete_part', {
                            method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({rating_key: item.rating_key, media_id: mediaId, part_id: partId}),
                        });
                        const d = await r.json();
                        if (d.success) {
                            item.bad_file = null; item.has_good_file = false; item.bad_file_orphaned = false;
                            deleted++;
                        } else { failed++; }
                    } catch(e) { failed++; }
                }
                _ptRenderGroup('pt-in-rd-tbody', _ptInRd, 'in_rd');
                _ptUpdateCounts(); _ptUpdatePurgeBtn();
                if (failed) showPopup({ type: 'warning', title: 'Notice', message: `${deleted} deleted. ${failed} failed.`, autoClose: 4000 });
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + e.message, autoClose: 5000 }); }
            btn.innerHTML = origHtml; btn.disabled = false;
        }
    });
});
document.getElementById('pt-bulk-del-good')?.addEventListener('click', function() {
    _ptBulkDelete(_ptInRd.filter(i => i.has_good_file), 'good-file');
});
document.getElementById('pt-bulk-reinsert-notrd')?.addEventListener('click', function() {
    _ptBulkReinsert(_ptNotInRd, 'not-in-RD');
});
document.getElementById('pt-bulk-del-notrd')?.addEventListener('click', function() {
    _ptBulkDelete(_ptNotInRd, 'not-in-RD');
});

// Lazy load when tab is activated
document.querySelectorAll('.dm-tab[data-tab="plex-trash"]').forEach(btn => {
    btn.addEventListener('click', () => { if (!_ptLoaded) loadPlexTrash(false); });
});
if (_dmActiveTab === 'plex-trash') loadPlexTrash(false);

// ── USENET (NZB) SECTION ───────────────────────────────────────────────────
let _ptInUsenet = [];
let _ptNotUsenet = [];

function _ptRenderUsenetGroup(tbodyId, items) {
    const tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    if (!items.length) {
        tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="5">None</td></tr>';
        return;
    }
    tbody.innerHTML = items.map((item, idx) => {
        const checked = _ptSelected.has(item.rating_key) ? 'checked' : '';
        const typeLabel = item.media_type === 'show'
            ? '<span class="dm-type-badge dm-type-tv">Show</span>'
            : '<span class="dm-type-badge dm-type-movie">Movie</span>';
        const year = item.year ? ` (${item.year})` : '';
        const rk = escHtml(item.rating_key);
        const _shorten = (s, n) => s && s.length > n ? s.substring(0, n) + '…' : (s || '');

        // Build Files column — mirrors debrid _ptRenderGroup logic exactly
        let filesHtml = '';
        if (item.bad_file && item.has_good_file) {
            const goodName = item.good_file || item.nzb_title || item.folder_name || '';
            const _pids = item.part_ids || {};
            const badPart = _pids[item.bad_file] || {};
            const goodPart = _pids[goodName] || {};
            const badMid = escHtml(badPart.media_id || '');
            const badPid = escHtml(badPart.part_id || '');
            const goodMid = escHtml(goodPart.media_id || '');
            const goodPid = escHtml(goodPart.part_id || '');
            const orphanLabel = item.bad_file_orphaned ? `<span class="dm-pt-badge-orphaned">Orphaned</span>` : '';
            // Bad file row: Re-NZB disabled (no point reinserting the broken file) + Delete
            const badBtns = `<span class="dm-pt-file-btns">`
                + `<button class="dm-btn dm-btn-xs dm-pt-fbtn" data-faction="renzb-file" data-idx="${idx}" data-tbody="${tbodyId}" disabled title="Use Re-NZB button to resubmit">Re-NZB</button>`
                + `<button class="dm-btn dm-btn-xs dm-pt-fbtn dm-pt-fbtn-del" data-faction="delete-part" data-rk="${rk}" data-mid="${badMid}" data-pid="${badPid}" data-protected="1" title="Remove this file from Plex">Delete</button>`
                + `</span>`;
            // Good file row: Re-NZB enabled + Delete
            const goodBtns = `<span class="dm-pt-file-btns">`
                + `<button class="dm-btn dm-btn-xs dm-pt-fbtn" data-faction="renzb-file" data-idx="${idx}" data-tbody="${tbodyId}" title="Re-submit this item to ${window.USENET_PROVIDER_NAME || "Usenet provider"}">Re-NZB</button>`
                + `<button class="dm-btn dm-btn-xs dm-pt-fbtn dm-pt-fbtn-del" data-faction="delete-part" data-rk="${rk}" data-mid="${goodMid}" data-pid="${goodPid}" data-protected="1" title="Remove this file from Plex">Delete</button>`
                + `</span>`;
            filesHtml = `<div class="dm-pt-file-row"><span class="dm-pt-file-bad" title="${escHtml(item.bad_file)}">${escHtml(item.bad_file)}</span>${orphanLabel}${badBtns}</div>`
                      + `<div class="dm-pt-file-row"><span class="dm-pt-file-good" title="${escHtml(goodName)}">${escHtml(goodName)}</span>${goodBtns}</div>`;
        } else if (item.bad_file) {
            filesHtml = `<div class="dm-pt-file-row"><span class="dm-pt-file-bad" title="${escHtml(item.bad_file)}">${escHtml(item.bad_file)}</span></div>`;
        } else {
            const releaseTitle = escHtml(item.nzb_title || item.folder_name || '');
            const hasUrl = !!(item.nzb_url);
            const urlBadge = hasUrl
                ? '<span class="dm-pt-badge-nzb-url" title="Original NZB URL available">URL</span>'
                : '<span class="dm-pt-badge-nzb-nourl" title="No stored NZB URL — will re-scrape">Re-scrape</span>';
            filesHtml = `<div class="dm-pt-file-row"><span class="dm-pt-folder" title="${releaseTitle}">${releaseTitle}</span>${urlBadge}</div>`;
        }

        const action = `<button class="dm-btn dm-btn-secondary dm-btn-sm dm-pt-action-btn" data-action="renzb" data-idx="${idx}" data-group="${tbodyId}" title="Re-submit to ${window.USENET_PROVIDER_NAME || "Usenet provider"}">Re-NZB</button>`;
        return `<tr>
            <td class="dm-col-check"><input type="checkbox" data-rk="${rk}" data-group="usenet" ${checked}></td>
            <td><span class="dm-pt-title">${escHtml(item.title)}${escHtml(year)}</span></td>
            <td class="dm-pt-col-type">${typeLabel}</td>
            <td class="dm-pt-col-files">${filesHtml}</td>
            <td class="dm-col-actions">${action}</td>
        </tr>`;
    }).join('');

    tbody.querySelectorAll('input[type=checkbox][data-rk]').forEach(cb => {
        cb.addEventListener('change', () => {
            if (cb.checked) _ptSelected.add(cb.dataset.rk);
            else _ptSelected.delete(cb.dataset.rk);
            _ptUpdatePurgeBtn();
        });
    });

    tbody.querySelectorAll('.dm-pt-action-btn[data-action="renzb"]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const idx = parseInt(btn.dataset.idx);
            const isIn = btn.dataset.group === 'pt-in-usenet-tbody';
            const item = isIn ? _ptInUsenet[idx] : _ptNotUsenet[idx];
            if (!item) return;
            const origHtml = btn.innerHTML;
            btn.disabled = true;
            btn.textContent = 'Submitting…';
            try {
                const r = await fetch('/debrid_manager/api/nzb/reinsert', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        item_id: item.nzb_item_id,
                        nzb_url: item.nzb_url || '',
                        nzb_title: item.nzb_title || '',
                        rating_key: item.rating_key,
                    }),
                });
                const data = await r.json();
                if (data.success) {
                    const methodLabel = data.method === 'rescrape' ? 'Re-scraped & submitted' : 'Re-submitted';
                    // Update item's nzb_title with new release so Scan All uses the right path
                    if (data.new_release_title) item.nzb_title = data.new_release_title;
                    btn.textContent = 'Done';
                    btn.classList.add('dm-pt-action-ok');
                    showPopup({ type: 'success', title: 'Re-NZB', message: `${methodLabel} — job ID: ${data.job_id}`, autoClose: 5000 });
                    // Auto-scan the new path
                    const parentDir = item.file_dir ? item.file_dir.substring(0, item.file_dir.lastIndexOf('/')) : '';
                    const newPath = (parentDir && item.nzb_title) ? `${parentDir}/${item.nzb_title}` : '';
                    if (newPath) {
                        fetch('/debrid_manager/api/plex_force_scan', {
                            method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({paths: [newPath], empty_trash: false}),
                        }).catch(() => {});
                    }
                    setTimeout(() => { btn.innerHTML = origHtml; btn.classList.remove('dm-pt-action-ok'); btn.disabled = false; }, 4000);
                } else {
                    showPopup({ type: 'error', title: 'Re-NZB Failed', message: data.error || 'Unknown error', autoClose: 6000 });
                    btn.innerHTML = origHtml; btn.disabled = false;
                }
            } catch(e) {
                showPopup({ type: 'error', title: 'Re-NZB Failed', message: e.message, autoClose: 5000 });
                btn.innerHTML = origHtml; btn.disabled = false;
            }
        });
    });

    // Per-file inline button handlers (renzb-file and delete-part)
    tbody.querySelectorAll('.dm-pt-fbtn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const faction = btn.dataset.faction;
            const origHtml = btn.innerHTML;
            if (btn.disabled) return;
            btn.disabled = true;
            if (faction === 'renzb-file') {
                const idx = parseInt(btn.dataset.idx);
                const isIn = btn.dataset.tbody === 'pt-in-usenet-tbody';
                const item = isIn ? _ptInUsenet[idx] : _ptNotUsenet[idx];
                if (!item) { btn.disabled = false; return; }
                btn.textContent = 'Submitting…';
                try {
                    const r = await fetch('/debrid_manager/api/nzb/reinsert', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ item_id: item.nzb_item_id, nzb_url: item.nzb_url || '', nzb_title: item.nzb_title || '', rating_key: item.rating_key }),
                    });
                    const data = await r.json();
                    if (data.success) {
                        if (data.new_release_title) item.nzb_title = data.new_release_title;
                        btn.textContent = 'Done';
                        btn.classList.add('dm-pt-action-ok');
                        setTimeout(() => { btn.innerHTML = origHtml; btn.classList.remove('dm-pt-action-ok'); btn.disabled = false; }, 3000);
                    } else {
                        showPopup({ type: 'error', title: 'Re-NZB Failed', message: data.error || 'Unknown error', autoClose: 6000 });
                        btn.innerHTML = origHtml; btn.disabled = false;
                    }
                } catch(e) { showPopup({ type: 'error', title: 'Re-NZB Failed', message: e.message, autoClose: 5000 }); btn.innerHTML = origHtml; btn.disabled = false; }
            } else if (faction === 'delete-part') {
                const ratingKey = btn.dataset.rk;
                const mediaId = btn.dataset.mid;
                const partId = btn.dataset.pid;
                showPopup({
                    type: 'confirm', title: 'Remove File', message: 'Remove this file from Plex? This cannot be undone.',
                    confirmText: 'Remove', cancelText: 'Cancel',
                    onConfirm: async function() {
                        btn.textContent = 'Deleting…';
                        try {
                            const scanItem = [..._ptInUsenet, ..._ptNotUsenet].find(i => i.rating_key === ratingKey);
                            const r = await fetch('/debrid_manager/api/plex_delete_part', {
                                method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({ rating_key: ratingKey, media_id: mediaId, part_id: partId }),
                            });
                            const data = await r.json();
                            if (!data.success) { showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + (data.error || 'unknown'), autoClose: 5000 }); btn.innerHTML = origHtml; btn.disabled = false; return; }
                            const inItem = _ptInUsenet.find(i => i.rating_key === ratingKey);
                            if (inItem?.has_good_file) {
                                inItem.bad_file = null; inItem.has_good_file = false; inItem.bad_file_orphaned = false;
                                _ptRenderUsenetGroup('pt-in-usenet-tbody', _ptInUsenet);
                            } else {
                                _ptInUsenet = _ptInUsenet.filter(i => i.rating_key !== ratingKey);
                                _ptNotUsenet = _ptNotUsenet.filter(i => i.rating_key !== ratingKey);
                                _ptSelected.delete(ratingKey);
                                _ptRenderUsenetGroup('pt-in-usenet-tbody', _ptInUsenet);
                                _ptRenderUsenetGroup('pt-not-usenet-tbody', _ptNotUsenet);
                            }
                            _ptUpdateUsenetCounts(); _ptUpdatePurgeBtn();
                            if (scanItem?.file_dir) {
                                fetch('/debrid_manager/api/plex_force_scan', {
                                    method: 'POST', headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify({ paths: [scanItem.file_dir], empty_trash: false }),
                                }).catch(() => {});
                            }
                        } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + e.message, autoClose: 5000 }); btn.innerHTML = origHtml; btn.disabled = false; }
                    },
                    onCancel: function() { btn.disabled = false; }
                });
                return;
            }
        });
    });
}

function _ptUpdateUsenetCounts() {
    const inEl = document.getElementById('pt-in-usenet-count');
    const outEl = document.getElementById('pt-not-usenet-count');
    if (inEl) inEl.textContent = _ptInUsenet.length;
    if (outEl) outEl.textContent = _ptNotUsenet.length;
    // Update sub-tab badge
    const usenetTotal = _ptInUsenet.length + _ptNotUsenet.length;
    const subtabBadge = document.getElementById('pt-subtab-usenet-count');
    if (subtabBadge) subtabBadge.textContent = usenetTotal || '';
}


document.getElementById('pt-in-usenet-select-all')?.addEventListener('change', function() {
    if (this.checked) _ptInUsenet.forEach(i => _ptSelected.add(i.rating_key));
    else _ptInUsenet.forEach(i => _ptSelected.delete(i.rating_key));
    _ptRenderUsenetGroup('pt-in-usenet-tbody', _ptInUsenet);
    _ptUpdatePurgeBtn();
});
document.getElementById('pt-not-usenet-select-all')?.addEventListener('change', function() {
    if (this.checked) _ptNotUsenet.forEach(i => _ptSelected.add(i.rating_key));
    else _ptNotUsenet.forEach(i => _ptSelected.delete(i.rating_key));
    _ptRenderUsenetGroup('pt-not-usenet-tbody', _ptNotUsenet);
    _ptUpdatePurgeBtn();
});
document.getElementById('pt-select-in-usenet')?.addEventListener('click', () => {
    _ptInUsenet.forEach(i => _ptSelected.add(i.rating_key));
    _ptRenderUsenetGroup('pt-in-usenet-tbody', _ptInUsenet);
    _ptUpdatePurgeBtn();
});
document.getElementById('pt-select-not-usenet')?.addEventListener('click', () => {
    _ptNotUsenet.forEach(i => _ptSelected.add(i.rating_key));
    _ptRenderUsenetGroup('pt-not-usenet-tbody', _ptNotUsenet);
    _ptUpdatePurgeBtn();
});

async function _ptBulkReNzb(items, label) {
    if (!items.length) { showPopup({ type: 'warning', title: 'Notice', message: `No ${label} items to re-submit.`, autoClose: 4000 }); return; }
    const btn = event.currentTarget;
    const origHtml = btn.innerHTML;
    showPopup({
        type: 'confirm',
        title: 'Re-NZB All',
        message: `Re-submit ${items.length} ${label} item${items.length !== 1 ? 's' : ''} to ${window.USENET_PROVIDER_NAME || "Usenet provider"}?\n\nItems without a stored URL will fall back to re-scraping.`,
        confirmText: 'Re-NZB All',
        cancelText: 'Cancel',
        onConfirm: async function() {
            btn.disabled = true;
            btn.innerHTML = '<span class="dm-spinner"></span>';
            let done = 0, failed = 0;
            for (const item of items) {
                try {
                    const r = await fetch('/debrid_manager/api/nzb/reinsert', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ item_id: item.nzb_item_id, nzb_url: item.nzb_url || '', nzb_title: item.nzb_title || '', rating_key: item.rating_key }),
                    });
                    const d = await r.json();
                    if (d.success) done++; else failed++;
                } catch(e) { failed++; }
            }
            showPopup({ type: done ? 'success' : 'error', title: 'Re-NZB All', message: `Submitted: ${done}${failed ? `, Failed: ${failed}` : ''}`, autoClose: 5000 });
            btn.innerHTML = origHtml; btn.disabled = false;
        }
    });
}

function _ptUsenetScanPaths(items) {
    // Build new-download paths: parent(file_dir) + nzb_title
    // Falls back to just parent(file_dir) if no title available
    const paths = [];
    for (const i of items) {
        if (!i.file_dir) continue;
        const parent = i.file_dir.substring(0, i.file_dir.lastIndexOf('/'));
        if (!parent) continue;
        const newPath = i.nzb_title ? `${parent}/${i.nzb_title}` : parent;
        paths.push(newPath);
    }
    return [...new Set(paths)];
}

document.getElementById('pt-bulk-scan-in-usenet')?.addEventListener('click', async function() {
    const paths = _ptUsenetScanPaths(_ptInUsenet);
    if (!paths.length) { showPopup({ type: 'warning', title: 'Notice', message: 'No scan paths available.', autoClose: 4000 }); return; }
    this.disabled = true;
    const origHtml = this.innerHTML;
    this.innerHTML = '<span class="dm-spinner"></span>';
    try {
        await fetch('/debrid_manager/api/plex_force_scan', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({paths, empty_trash: false}),
        });
        this.textContent = 'Done';
        setTimeout(() => { this.innerHTML = origHtml; this.disabled = false; }, 2500);
    } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Scan failed: ' + e.message, autoClose: 5000 }); this.innerHTML = origHtml; this.disabled = false; }
});
document.getElementById('pt-bulk-scan-not-usenet')?.addEventListener('click', async function() {
    const paths = _ptUsenetScanPaths(_ptNotUsenet);
    if (!paths.length) { showPopup({ type: 'warning', title: 'Notice', message: 'No scan paths available.', autoClose: 4000 }); return; }
    this.disabled = true;
    const origHtml = this.innerHTML;
    this.innerHTML = '<span class="dm-spinner"></span>';
    try {
        await fetch('/debrid_manager/api/plex_force_scan', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({paths, empty_trash: false}),
        });
        this.textContent = 'Done';
        setTimeout(() => { this.innerHTML = origHtml; this.disabled = false; }, 2500);
    } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Scan failed: ' + e.message, autoClose: 5000 }); this.innerHTML = origHtml; this.disabled = false; }
});
document.getElementById('pt-bulk-renzb-in')?.addEventListener('click', function() {
    _ptBulkReNzb(_ptInUsenet, 'in-Usenet');
});
document.getElementById('pt-bulk-renzb-not')?.addEventListener('click', function() {
    _ptBulkReNzb(_ptNotUsenet, 'not-in-Usenet');
});
document.getElementById('pt-bulk-del-in-usenet')?.addEventListener('click', function() {
    _ptBulkDelete(_ptInUsenet, 'in-Usenet');
});
document.getElementById('pt-bulk-del-not-usenet')?.addEventListener('click', function() {
    _ptBulkDelete(_ptNotUsenet, 'not-in-Usenet');
});
