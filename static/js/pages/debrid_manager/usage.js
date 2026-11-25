// Page script for templates/debrid_manager.html.
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
                            <td class="dm-usage-th-hoster">${escHtml(h.name)}</td>
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
