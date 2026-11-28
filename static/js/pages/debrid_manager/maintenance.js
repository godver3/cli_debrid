// Page script for templates/debrid_manager.html.
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
        document.getElementById('bk-activity').innerHTML = `<div class="dm-error">Failed to load: ${escHtml(e.message)}</div>`;
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
                            const name = p.current_name ? ` · ${escHtml(p.current_name.slice(0, 55))}` : '';
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
            const name = p.current_name ? ` · ${escHtml(p.current_name.slice(0, 55))}` : '';
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
