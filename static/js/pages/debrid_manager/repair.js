// Page script for templates/debrid_manager.html.
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
        _debridRenderVersions(d.versions || [], d.repair_version || '');
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
                const rawEntry    = item.entry_name || item.name || item.title || '';
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
                    ? `<button onclick="debridDeleteGhostSingle(${_jsArg(rawEntry)}, this)" class="un-btn" style="padding:4px 12px;font-size:12px;color:#f59e0b;border-color:#f59e0b"><i class="fa-solid fa-ghost"></i> Delete</button>`
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
