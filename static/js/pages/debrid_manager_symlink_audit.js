/* Moved from templates/debrid_manager.html. */
// ── SYMLINK AUDIT ───────────────────────────────────────────────────────────
let _slData        = null;
let _slPollTimer   = null;

function _slEl(id) { return document.getElementById(id); }
function _slFmt(n)  { return (n||0).toLocaleString(); }

function _slFmtAge(ts) {
    if (!ts) return '';
    const secs = Math.floor((Date.now() / 1000) - ts);
    if (secs < 60)   return 'just now';
    if (secs < 3600) return `${Math.floor(secs/60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs/3600)}h ago`;
    return `${Math.floor(secs/86400)}d ago`;
}

function _slShowState(state) {
    // In stale mode we keep results visible while scanning happens in background
    const isStaleRefresh = state === 'scanning' && _slData !== null;
    _slEl('sl-idle').style.display     = state === 'idle'     ? '' : 'none';
    _slEl('sl-scanning').style.display = (!isStaleRefresh && state === 'scanning') ? '' : 'none';
    _slEl('sl-results').style.display  = (state === 'done' || isStaleRefresh) ? '' : 'none';
    const staleBadge = _slEl('sl-stale-badge');
    if (staleBadge) staleBadge.style.display = isStaleRefresh ? '' : 'none';
}

async function _slPoll() {
    try {
        const r = await fetch('/debrid_manager/api/symlink_audit');
        const d = await r.json();
        if (!d.success) { _slShowError(d.error); return; }
        if (d.status === 'scanning') {
            // May have stale data to show while refreshing
            if (d.data && !_slData) { _slData = d.data; _slRender(d.data); }
            _slShowState('scanning');
            _slPollTimer = setTimeout(_slPoll, 3000);
            return;
        }
        clearTimeout(_slPollTimer);
        if (d.status === 'done' && d.data) {
            _slData = d.data;
            _slRender(d.data);
            _slShowState('done');
        } else if (d.status === 'error') {
            _slShowError(d.error || 'Scan failed');
        } else {
            _slShowState('idle');
        }
    } catch(e) {
        clearTimeout(_slPollTimer);
        _slShowError(e.message);
    }
}

function _slShowError(msg) {
    _slShowState('idle');
    const el = _slEl('sl-error-msg');
    if (el) { el.textContent = msg; el.style.display = ''; }
}

async function _slStartScan() {
    const errEl = _slEl('sl-error-msg');
    if (errEl) errEl.style.display = 'none';
    _slShowState('scanning');
    try {
        const r = await fetch('/debrid_manager/api/symlink_audit/scan', {method:'POST'});
        const d = await r.json();
        if (!d.success) { _slShowError(d.error); return; }
        _slPollTimer = setTimeout(_slPoll, 3000);
    } catch(e) {
        _slShowError(e.message);
    }
}

function _slRender(data) {
    const s = data.stats;

    // Stats bar
    _slEl('sl-stat-scanned').textContent   = _slFmt(s.rclone_scanned);
    _slEl('sl-stat-indexed').textContent   = _slFmt(s.symlinks_indexed);
    _slEl('sl-stat-unlinked').textContent  = _slFmt(s.unlinked_count);
    _slEl('sl-stat-broken').textContent    = _slFmt(s.broken_count);
    _slEl('sl-stat-incorrect').textContent = _slFmt(s.incorrect_count);
    _slEl('sl-stat-correct').textContent   = _slFmt(s.correct_count);

    // Unlinked label: add size if non-zero
    const lblUnlinked = _slEl('sl-lbl-unlinked');
    if (lblUnlinked) lblUnlinked.textContent = s.unlinked_count > 0 && s.unlinked_size_str
        ? `Unlinked (${s.unlinked_size_str})`
        : 'Unlinked';

    // Health pill
    const issues = (s.broken_count || 0) + (s.incorrect_count || 0);
    const pill   = _slEl('sl-health-pill');
    const icon   = _slEl('sl-health-icon');
    const count  = _slEl('sl-health-count');
    const label  = _slEl('sl-health-label');
    if (pill) {
        if (issues === 0) {
            pill.style.cssText  = '';
            icon.textContent    = '✓';
            count.textContent   = 'Clean';
            label.textContent   = '';
            count.style.color   = '#4ade80';
            icon.style.color    = '#4ade80';
        } else {
            icon.textContent    = '✗';
            count.textContent   = issues.toLocaleString();
            label.textContent   = 'Issues';
            count.style.color   = '#ef4444';
            icon.style.color    = '#ef4444';
        }
    }

    // Tab badge
    const badge = _slEl('rc-count-symlinks');
    if (badge) badge.textContent = issues ? issues.toLocaleString() : '✓';

    // Last scanned + paths hint
    const lastEl = _slEl('sl-last-scanned');
    if (lastEl) lastEl.textContent = data.scanned_at ? `Last scanned: ${_slFmtAge(data.scanned_at)}` : '';
    const pathsEl = _slEl('sl-paths-hint');
    if (pathsEl && data.rclone_dir && data.symlink_dir)
        pathsEl.textContent = `${data.rclone_dir} → ${data.symlink_dir}`;

    // Sections — only show when they have data
    _slEl('sl-section-broken')?.style &&
        (_slEl('sl-section-broken').style.display    = s.broken_count    > 0 ? '' : 'none');
    _slEl('sl-section-unlinked')?.style &&
        (_slEl('sl-section-unlinked').style.display  = s.unlinked_count  > 0 ? '' : 'none');
    _slEl('sl-section-incorrect')?.style &&
        (_slEl('sl-section-incorrect').style.display = s.incorrect_count > 0 ? '' : 'none');

    _slRenderBroken(data.broken || []);
    _slRenderUnlinked(data.unlinked || []);
    _slRenderIncorrect(data.incorrect_dupes || []);
}

function _slParsePath(path) {
    const parts = path.replace(/\\/g, '/').split('/').filter(Boolean);
    const basename = parts[parts.length - 1] || path;
    const parent    = parts[parts.length - 2] || '';
    const grandparent = parts[parts.length - 3] || '';
    // If parent looks like a season folder, use grandparent as the title
    const title = /^season[\s._\-]?\d+$|^s\d{2}$/i.test(parent) ? grandparent : parent;
    // Infer type from path segments
    let type = '';
    if (/\/shows\/|\/series\/|\/tv\/|\/television\//i.test(path) || /[._\s]s\d{2}e\d{2}[._\s]/i.test(basename)) {
        type = 'tv';
    } else if (/\/movies\/|\/films\/|\/movie\//i.test(path)) {
        type = 'movie';
    }
    return { title, type, basename };
}

function _slTypeBadge(type) {
    if (!type) return '';
    return `<span class="dm-type-badge dm-type-${type}" data-type="${type}">${type === 'tv' ? 'TV' : 'Movie'}</span>`;
}

function _slRenderBroken(items) {
    const tbody = _slEl('sl-tbody-broken');
    if (!tbody) return;
    if (!items.length) { tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="4">None found</td></tr>'; return; }
    tbody.innerHTML = items.map(p => {
        const { title, type, basename } = _slParsePath(p);
        return `<tr>
            <td>${_esc(title)}</td>
            <td>${_slTypeBadge(type)}</td>
            <td class="dm-rc-col-file" title="${_esc(p)}">${_esc(basename)}</td>
            <td class="dm-col-actions">
                <button class="dm-btn dm-btn-danger dm-btn-sm" onclick="_slDeleteOne('broken', ${JSON.stringify(p)}, this)">Delete</button>
            </td>
        </tr>`;
    }).join('');
}

function _slRenderUnlinked(items) {
    const tbody = _slEl('sl-tbody-unlinked');
    if (!tbody) return;
    if (!items.length) { tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="3">None found</td></tr>'; return; }
    tbody.innerHTML = items.map(f => `
        <tr>
            <td class="dm-rc-col-file" title="${_esc(f.path)}">${_esc(f.path)}</td>
            <td style="white-space:nowrap">${_esc(f.size_str)}</td>
            <td class="dm-col-actions">
                <button class="dm-btn dm-btn-danger dm-btn-sm" onclick="_slDeleteOne('unlinked', ${JSON.stringify(f.path)}, this)">Delete</button>
            </td>
        </tr>`).join('');
}

function _slFilterBroken() {
    const section = _slEl('sl-section-broken');
    if (!section) return;
    const search = section.querySelector('.dm-rc-search')?.value.toLowerCase() || '';
    const type   = section.querySelector('.dm-rc-type-filter')?.value || '';
    section.querySelectorAll('#sl-tbody-broken tr').forEach(row => {
        if (row.classList.contains('dm-empty-row')) return;
        const badge    = row.querySelector('.dm-type-badge');
        const rowType  = badge?.dataset.type || '';
        const rowText  = row.textContent.toLowerCase();
        const typeMatch   = !type   || rowType === type;
        const searchMatch = !search || rowText.includes(search);
        row.style.display = (typeMatch && searchMatch) ? '' : 'none';
    });
}

function _slRenderIncorrect(items) {
    const tbody = _slEl('sl-tbody-incorrect');
    if (!tbody) return;
    if (!items.length) { tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="5">None found</td></tr>'; return; }
    tbody.innerHTML = items.map(d => {
        const { title, type, basename } = _slParsePath(d.target);
        const linksHtml = d.links.map(l => `<div class="sl-link-path" title="${_esc(l)}">${_esc(l.split('/').pop())}</div>`).join('');
        const allLinkPaths = JSON.stringify(d.links);
        return `<tr>
            <td>${_esc(title)}</td>
            <td>${_slTypeBadge(type)}</td>
            <td class="dm-rc-col-file" title="${_esc(d.target)}">${_esc(basename)}</td>
            <td>${linksHtml}</td>
            <td class="dm-col-actions">
                <button class="dm-btn dm-btn-danger dm-btn-sm" onclick="_slDeletePaths(${allLinkPaths}, this)">Delete Links</button>
            </td>
        </tr>`;
    }).join('');
}

// _esc defined below (unconditionally, outside symlink_mode block)

async function _slDeleteOne(kind, path, btnEl) {
    showPopup({
        type: 'confirm',
        title: 'Delete File',
        message: `Delete this file?\n${path}`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const orig = btnEl.innerHTML; btnEl.disabled = true; btnEl.textContent = '…';
            try {
                const r = await fetch('/debrid_manager/api/symlink_audit/delete', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({paths: [path]})
                });
                const d = await r.json();
                if (d.success && d.deleted > 0) {
                    btnEl.closest('tr').remove();
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + (d.errors?.[0] || 'unknown'), autoClose: 5000 });
                    btnEl.innerHTML = orig; btnEl.disabled = false;
                }
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Error: ' + e.message, autoClose: 5000 }); btnEl.innerHTML = orig; btnEl.disabled = false; }
        }
    });
}

async function _slDeletePaths(paths, btnEl) {
    showPopup({
        type: 'confirm',
        title: 'Delete Symlinks',
        message: `Delete ${paths.length} symlink(s)?`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const orig = btnEl.innerHTML; btnEl.disabled = true; btnEl.textContent = '…';
            try {
                const r = await fetch('/debrid_manager/api/symlink_audit/delete', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({paths})
                });
                const d = await r.json();
                if (d.success && d.deleted > 0) {
                    btnEl.textContent = `Deleted ${d.deleted}`;
                    btnEl.closest('tr').remove();
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + (d.errors?.[0] || 'unknown'), autoClose: 5000 });
                    btnEl.innerHTML = orig; btnEl.disabled = false;
                }
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Error: ' + e.message, autoClose: 5000 }); btnEl.innerHTML = orig; btnEl.disabled = false; }
        }
    });
}

async function _slDeleteAll(kind) {
    if (!_slData) return;
    let paths = [];
    if      (kind === 'broken')    paths = _slData.broken_all || _slData.broken || [];
    else if (kind === 'unlinked')  paths = _slData.unlinked_all || (_slData.unlinked || []).map(f => f.path);
    else if (kind === 'incorrect') paths = (_slData.incorrect_dupes || []).flatMap(d => d.links);
    if (!paths.length) return;
    showPopup({
        type: 'confirm',
        title: 'Delete All Files',
        message: `Delete all ${paths.length} ${kind} file(s)? This cannot be undone.`,
        confirmText: 'Delete All',
        cancelText: 'Cancel',
        onConfirm: async function() {
            try {
                const r = await fetch('/debrid_manager/api/symlink_audit/delete', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({paths})
                });
                const d = await r.json();
                if (d.success) {
                    showPopup({ type: 'success', title: 'Success', message: `Deleted ${d.deleted}${d.skipped ? `, skipped ${d.skipped} (now in use)` : ''}${d.failed ? `, failed ${d.failed}` : ''}`, autoClose: 4000 });
                    // Re-scan to refresh
                    await _slStartScan();
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + d.error, autoClose: 5000 });
                }
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Error: ' + e.message, autoClose: 5000 }); }
        }
    });
}

// Called when the Audit tab opens — fires in parallel with loadReconcile()
// Must use var (not let) — function is called earlier in the script before this
// declaration is reached; let has a temporal dead zone that causes silent failures.
var _slAuditStarted = false;
async function _slInitAudit() {
    if (_slAuditStarted) return;
    _slAuditStarted = true;
    const r = await fetch('/debrid_manager/api/symlink_audit').catch(() => null);
    if (!r) return;
    const d = await r.json().catch(() => null);
    if (!d?.success) return;
    if (d.data) { _slData = d.data; _slRender(d.data); }
    if (d.status === 'scanning') {
        _slShowState('scanning');
        if (!_slPollTimer) _slPollTimer = setTimeout(_slPoll, 3000);
    } else if (d.status === 'done') {
        _slShowState('done');
    } else {
        // No cache yet — auto-start scan immediately
        _slStartScan();
    }
}

// ── JUNK SYMLINK AUDIT ────────────────────────────────────────────────────
let _jaData = null;
let _jaPollTimer = null;

function _jaEl(id) { return document.getElementById(id); }

function _jaShowState(state) {
    _jaEl('ja-idle').style.display     = state === 'idle'     ? '' : 'none';
    _jaEl('ja-scanning').style.display = state === 'scanning' ? '' : 'none';
    _jaEl('ja-results').style.display  = state === 'done'     ? '' : 'none';
}

function _jaShowError(msg) {
    _jaShowState('idle');
    const el = _jaEl('ja-error-msg');
    if (el) { el.textContent = msg; el.style.display = ''; }
}

async function _jaPoll() {
    try {
        const r = await fetch('/debrid_manager/api/junk_symlink_audit');
        const d = await r.json();
        if (!d.success) { _jaShowError(d.error || 'Scan failed'); return; }
        if (d.status === 'scanning') {
            _jaShowState('scanning');
            _jaPollTimer = setTimeout(_jaPoll, 3000);
            return;
        }
        clearTimeout(_jaPollTimer);
        if (d.status === 'done' && d.data) {
            _jaData = d.data;
            _jaRender(d.data, d.ts);
            _jaShowState('done');
        } else if (d.status === 'error') {
            _jaShowError(d.error || 'Scan failed');
        } else {
            _jaShowState('idle');
        }
    } catch (e) {
        clearTimeout(_jaPollTimer);
        _jaShowError(e.message);
    }
}

async function _jaStartScan() {
    const errEl = _jaEl('ja-error-msg');
    if (errEl) errEl.style.display = 'none';
    _jaShowState('scanning');
    try {
        const r = await fetch('/debrid_manager/api/junk_symlink_audit/scan', {method: 'POST'});
        const d = await r.json();
        if (!d.success) { _jaShowError(d.error); return; }
        _jaPollTimer = setTimeout(_jaPoll, 2000);
    } catch (e) {
        _jaShowError(e.message);
    }
}

function _jaFixTooltip(e) {
    if (e.skip_wanted_reset) {
        return 'Remove this junk symlink and delete this duplicate DB row (a good sibling is already Collected).';
    }
    return 'Remove this junk symlink, clean up Plex/cli_mount if linked, and reset this item to Wanted for re-scrape.';
}

function _jaRowHtml(e) {
    const tip = escHtml(_jaFixTooltip(e));
    const siblingNote = e.skip_wanted_reset && e.skip_reason
        ? `<br><span style="font-size:11px;color:#4ade80">${escHtml(e.skip_reason)} — will NOT reset to Wanted</span>`
        : '';
    const fixLabel = e.skip_wanted_reset ? 'Delete dup' : 'Fix';
    return `<tr>
        <td>${escHtml(e.label || '')}<br><span style="font-size:11px;color:#888">${escHtml(String(e.item_id || ''))}</span></td>
        <td>${escHtml(e.size_str || '')}</td>
        <td>${escHtml(e.reason || '')}${siblingNote}${e.review_note ? `<br><span style="font-size:11px;color:#fa0">${escHtml(e.review_note)}</span>` : ''}${e.size_divergence_note ? `<br><span style="font-size:11px;color:#fa0">${escHtml(e.size_divergence_note)}</span>` : ''}</td>
        <td class="dm-col-actions"><button class="dm-btn dm-btn-danger dm-btn-sm" title="${tip}" onclick="_jaFixItem(${e.item_id})">${fixLabel}</button></td>
    </tr>`;
}

function _jaRender(data, ts) {
    const s = data.stats || {};
    _jaEl('ja-stat-bad').textContent    = (s.bad_symlinks || 0).toLocaleString();
    _jaEl('ja-stat-review').textContent = (s.review_symlinks || 0).toLocaleString();
    _jaEl('ja-stat-mount').textContent  = (s.mount_files || 0).toLocaleString();
    _jaEl('ja-stat-reset').textContent  = (s.db_resets || 0).toLocaleString();
    _jaEl('ja-stat-dupdel').textContent  = ((s.db_duplicate_deletes || 0) + (s.review_db_duplicate_deletes || 0)).toLocaleString();
    const badge = _jaEl('ja-total-badge');
    if (badge) badge.textContent = ((s.bad_symlinks || 0) + (s.review_symlinks || 0)) || '—';
    const last = _jaEl('ja-last-scanned');
    if (last && ts) {
        const age = Math.floor((Date.now() / 1000) - ts);
        last.textContent = age < 120 ? 'Scanned just now' : `Scanned ${Math.floor(age / 60)}m ago`;
    }
    const applyBtn = _jaEl('ja-apply-all-btn');
    const reviewBtn = _jaEl('ja-apply-review-btn');
    if (applyBtn) {
        applyBtn.disabled = !(s.bad_symlinks || s.mount_files);
        applyBtn.title = [
            'Remove likely-junk symlinks (' + (s.bad_symlinks || 0) + ')',
            'delete orphan mount files (' + (s.mount_files || 0) + ')',
            'reset ' + (s.db_resets || 0) + ' item(s) to Wanted',
            'delete ' + (s.db_duplicate_deletes || 0) + ' duplicate DB row(s)',
            'Skips Review (older/smaller) items.',
        ].join(', ') + '.';
    }
    if (reviewBtn) {
        reviewBtn.disabled = !(s.review_symlinks);
        reviewBtn.title = [
            'Remove review-tier symlinks (' + (s.review_symlinks || 0) + ')',
            'reset ' + (s.review_db_resets || 0) + ' item(s) to Wanted',
            'delete ' + (s.review_db_duplicate_deletes || 0) + ' duplicate DB row(s)',
            'Only for items you verified are junk',
            'does not delete mount orphans',
        ].join(', ') + '.';
    }

    const badRows = (data.bad_symlinks || []).slice(0, 500);
    const badSec = _jaEl('ja-section-bad');
    const badTbody = _jaEl('ja-tbody-bad');
    if (badSec && badTbody) {
        badSec.style.display = badRows.length ? '' : 'none';
        badTbody.innerHTML = badRows.map(_jaRowHtml).join('');
    }

    const reviewRows = (data.review_symlinks || []).slice(0, 500);
    const reviewSec = _jaEl('ja-section-review');
    const reviewTbody = _jaEl('ja-tbody-review');
    if (reviewSec && reviewTbody) {
        reviewSec.style.display = reviewRows.length ? '' : 'none';
        reviewTbody.innerHTML = reviewRows.map(_jaRowHtml).join('');
    }
}

async function _jaApply(body) {
    const r = await fetch('/debrid_manager/api/junk_symlink_audit/apply', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!d.success) throw new Error(d.error || 'Apply failed');
    return d;
}

function _jaApplyAll() {
    showPopup({
        type: 'confirm',
        title: 'Fix Likely Junk',
        message: 'Remove high-confidence junk symlinks, orphan mount files, and reset affected DB items to Wanted? Review (older/smaller) items are skipped.',
        confirmText: 'Fix Likely Junk',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const btn = _jaEl('ja-apply-all-btn');
            const orig = btn?.innerHTML;
            if (btn) { btn.disabled = true; btn.textContent = 'Working…'; }
            try {
                const d = await _jaApply({apply_all: true, include_mount: true, confidence_tier: 'high'});
                showPopup({
                    type: 'success',
                    title: 'Done',
                    message: `Symlinks: ${d.deleted_symlinks}, mount: ${d.deleted_mount}, reset: ${d.reset_count}, dup deleted: ${d.delete_count}`,
                    autoClose: 6000,
                });
                _jaStartScan();
            } catch (e) {
                showPopup({type: 'error', title: 'Error', message: e.message, autoClose: 5000});
            } finally {
                if (btn) { btn.innerHTML = orig; btn.disabled = false; }
            }
        },
    });
}

function _jaApplyReview() {
    showPopup({
        type: 'confirm',
        title: 'Fix Review Items',
        message: 'Fix older/smaller items you have verified are junk? These were held back because they may be legitimate legacy rips.',
        confirmText: 'Fix Review Items',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const btn = _jaEl('ja-apply-review-btn');
            const orig = btn?.innerHTML;
            if (btn) { btn.disabled = true; btn.textContent = 'Working…'; }
            try {
                const d = await _jaApply({apply_all: true, include_mount: false, confidence_tier: 'review'});
                showPopup({
                    type: 'success',
                    title: 'Done',
                    message: `Symlinks: ${d.deleted_symlinks}, reset: ${d.reset_count}, dup deleted: ${d.delete_count}`,
                    autoClose: 6000,
                });
                _jaStartScan();
            } catch (e) {
                showPopup({type: 'error', title: 'Error', message: e.message, autoClose: 5000});
            } finally {
                if (btn) { btn.innerHTML = orig; btn.disabled = false; }
            }
        },
    });
}

function _jaFixItem(itemId) {
    showPopup({
        type: 'confirm',
        title: 'Fix Junk Item',
        message: `Fix item #${itemId}? Removes its symlink and resets to Wanted (or deletes duplicate row).`,
        confirmText: 'Fix',
        cancelText: 'Cancel',
        onConfirm: async function() {
            try {
                await _jaApply({apply_all: false, item_ids: [itemId], include_mount: true, confidence_tier: 'all'});
                _jaStartScan();
            } catch (e) {
                showPopup({type: 'error', title: 'Error', message: e.message, autoClose: 5000});
            }
        },
    });
}

var _jaAuditStarted = false;
async function _jaInitAudit() {
    if (_jaAuditStarted) return;
    _jaAuditStarted = true;
    const r = await fetch('/debrid_manager/api/junk_symlink_audit').catch(() => null);
    if (!r) return;
    const d = await r.json().catch(() => null);
    if (!d?.success) return;
    if (d.data) { _jaData = d.data; _jaRender(d.data, d.ts); }
    if (d.status === 'scanning') {
        _jaShowState('scanning');
        if (!_jaPollTimer) _jaPollTimer = setTimeout(_jaPoll, 3000);
    } else if (d.status === 'done') {
        _jaShowState('done');
    } else {
        _jaStartScan();
    }
}
