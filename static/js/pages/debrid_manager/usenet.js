// Page script for templates/debrid_manager.html.
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
                // broken_files is a list of {file_name, reason} (cli_mount and
                // the built-in mount); older backends sent a count.
                const files = Array.isArray(i.broken_files) ? i.broken_files : [];
                const broken = i.broken_count || (Array.isArray(i.broken_files) ? files.length : i.broken_files) || '?';
                const tip = _esc(files.map(f => (f && f.file_name ? f.file_name : '?') + (f && f.reason ? ' — ' + f.reason : '')).join('\n'));
                return `<tr style="border-bottom:1px solid var(--border-color,#2a2a2a);background:${bg}">
                    <td style="padding:8px 12px;font-weight:500">${name}</td>
                    <td style="padding:8px 12px;color:#ff4444;font-weight:600;text-align:right;white-space:nowrap" title="${tip}">${_esc(String(broken))}${files.length ? ' file' + (files.length === 1 ? '' : 's') : ''}</td>
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
                // The automatic repair holds the same lock: a click while it runs
                // returns {skipped: 'already_running'} with no counts.
                statusEl.textContent = d.summary.skipped === 'already_running'
                    ? 'The automatic repair was already running, so this click did nothing. Its results are in the activity list below.'
                    : `Done — ${d.summary.replaced || 0} replaced, ${d.summary.not_found || 0} not found, ${d.summary.errors || 0} errors`;
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
