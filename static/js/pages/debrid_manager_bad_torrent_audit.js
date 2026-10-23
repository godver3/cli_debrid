/* Moved from templates/debrid_manager.html. */
// ── Bad Torrent Audit ──────────────────────────────────────────────────────
(function () {
    let _btaData = null;
    let _btaPollTimer = null;

    function _bta(id) { return document.getElementById(id); }

    function _btaEsc(s) {
        return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    function _btaSetState(state) {
        _bta('bta-idle').style.display     = state === 'idle'              ? '' : 'none';
        _bta('bta-scanning').style.display = state === 'scanning'          ? '' : 'none';
        _bta('bta-results').style.display  = (state === 'done' || state === 'stopped') ? '' : 'none';
        // Resume btn label: "Resume Scan" if stopped/partial, "Re-scan" if done
        const resumeBtn = _bta('bta-resume-btn');
        if (resumeBtn) resumeBtn.textContent = state === 'stopped' ? '▶ Resume Scan' : '↺ Re-scan';
    }

    function _btaStatusBadge(status) {
        const cls = status === 'broken' ? 'bta-status-badge bta-status-broken'
                  : status === 'stale'  ? 'bta-status-badge bta-status-stale'
                  :                       'bta-status-badge bta-status-unknown';
        return `<span class="${cls}">${_btaEsc(status)}</span>`;
    }

    window._btaRender = function () {
        if (!_btaData) return;
        const search = (_bta('bta-search') || {value:''}).value.toLowerCase();
        const filterStatus = (_bta('bta-filter-status') || {value:''}).value;
        const rows = (_btaData.results || []).filter(r => {
            if (filterStatus && r.status !== filterStatus) return false;
            if (search && !r.name.toLowerCase().includes(search)) return false;
            return true;
        });
        const tbody = _bta('bta-tbody');
        if (!tbody) return;
        if (!rows.length) {
            tbody.innerHTML = '<tr class="bta-empty-row"><td colspan="4">No entries match.</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map(r => `
            <tr class="bta-row bta-row-${_btaEsc(r.status)}">
                <td class="bta-col-check"><input type="checkbox" class="bta-row-check" data-name="${_btaEsc(r.name)}"></td>
                <td class="bta-col-name" title="${_btaEsc(r.name)}">${_btaEsc(r.name)}</td>
                <td class="bta-col-status">${_btaStatusBadge(r.status)}</td>
                <td class="bta-col-detail">${_btaEsc(r.detail || '')}</td>
            </tr>`).join('');
        // Update delete button state
        _btaOnCheckChange();
    };

    window._btaToggleAll = function (checked) {
        document.querySelectorAll('.bta-row-check').forEach(cb => { cb.checked = checked; });
        const th = _bta('bta-thead-check');
        if (th) th.checked = checked;
        const sa = _bta('bta-select-all');
        if (sa) sa.checked = checked;
        _btaOnCheckChange();
    };

    function _btaOnCheckChange() {
        const checked = document.querySelectorAll('.bta-row-check:checked').length;
        const delBtn = _bta('bta-bulk-delete-btn');
        if (delBtn) delBtn.disabled = checked === 0;
        const reinsertBtn = _bta('bta-bulk-reinsert-btn');
        if (reinsertBtn) reinsertBtn.disabled = checked === 0;
    }

    document.addEventListener('change', function (e) {
        if (e.target && e.target.classList.contains('bta-row-check')) _btaOnCheckChange();
    });

    // Which folder is currently active in the results view: 'bad' or 'all'
    let _btaActiveSource = 'bad';

    window._btaResumeActive   = () => _btaActiveSource === 'all' ? _ataStartScan(false) : _btaStartScan(false);
    window._btaFullRescanActive = () => _btaActiveSource === 'all' ? _ataStartScan(true)  : _btaStartScan(true);

    window._ataStartScan = async function (fresh) {
        _btaActiveSource = 'all';
        const errEl = _bta('bta-error-msg');
        if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
        _btaSetState('scanning');
        if (fresh) _btaData = null;
        // Update scanning label to show __all__
        const scanLabel = document.querySelector('#bta-scanning .bta-card-title');
        if (scanLabel) scanLabel.innerHTML = 'Scanning <code class="bta-code">__all__</code> folder…';
        try {
            const r = await fetch('/debrid_manager/api/all_torrent_audit/scan', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({fresh: !!fresh}),
            });
            const d = await r.json();
            if (!d.success) throw new Error(d.error || 'Scan failed');
            _ataPoll();
        } catch (e) {
            _btaSetState('idle');
            if (errEl) { errEl.textContent = e.message; errEl.style.display = ''; }
        }
    };

    window._ataStopScan = async function () {
        await fetch('/debrid_manager/api/all_torrent_audit/stop', {method: 'POST'});
    };

    async function _ataPoll() {
        try {
            const r = await fetch('/debrid_manager/api/all_torrent_audit');
            const d = await r.json();
            if (!d.success) throw new Error(d.error);
            const s = d.status;
            if (s === 'scanning') {
                const pbar = _bta('bta-progress-bar');
                const pmsg = _bta('bta-progress-msg');
                if (pbar) pbar.style.width = (d.progress || 0) + '%';
                if (pmsg) pmsg.textContent = d.progress_msg || 'Scanning…';
                const stopBtn = _bta('bta-stop-btn');
                if (stopBtn) stopBtn.onclick = _ataStopScan;
                setTimeout(_ataPoll, 1200);
            } else {
                // done / stopped / error — load results
                _btaData = d.data;
                _btaSetState(s === 'error' ? 'error' : 'done');
                if (d.error) { const e = _bta('bta-error-msg'); if(e){e.textContent=d.error;e.style.display='';} }
                if (d.data) _btaApplyResults(d.data, s);
                // Update source badge
                const badge = _bta('bta-partial-badge');
                if (badge) { badge.textContent = d.data?.partial ? 'partial · __all__' : '__all__'; badge.style.display = ''; }
            }
        } catch(e) { setTimeout(_ataPoll, 2000); }
    }

    window._btaStartScan = async function (fresh) {
        _btaActiveSource = 'bad';
        const errEl = _bta('bta-error-msg');
        if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
        _btaSetState('scanning');
        if (fresh) _btaData = null;
        const scanLabel = document.querySelector('#bta-scanning .bta-card-title');
        if (scanLabel) scanLabel.innerHTML = 'Scanning <code class="bta-code">__bad__</code> folder…';
        // Restore stop button to default
        const stopBtn = _bta('bta-stop-btn');
        if (stopBtn) stopBtn.onclick = _btaStopScan;
        try {
            const r = await fetch('/debrid_manager/api/bad_torrent_audit/scan', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({fresh: !!fresh}),
            });
            const d = await r.json();
            if (!d.success) throw new Error(d.error || 'Scan failed');
            _btaPoll();
        } catch (e) {
            _btaSetState('idle');
            if (errEl) { errEl.textContent = e.message; errEl.style.display = ''; }
        }
    };

    window._btaStopScan = async function () {
        const btn = _bta('bta-stop-btn');
        if (btn) { btn.disabled = true; btn.textContent = 'Stopping…'; }
        try {
            await fetch('/debrid_manager/api/bad_torrent_audit/stop', {method: 'POST'});
        } catch (_) {}
        // Poll will handle transition to stopped state
    };

    function _btaResetStopBtn() {
        const btn = _bta('bta-stop-btn');
        if (btn) { btn.disabled = false; btn.textContent = 'Stop'; }
    }

    function _btaPoll() {
        clearTimeout(_btaPollTimer);
        _btaPollTimer = setTimeout(async function () {
            try {
                const r = await fetch('/debrid_manager/api/bad_torrent_audit');
                const d = await r.json();
                if (!d.success) { _btaSetState('idle'); return; }
                const pbar = _bta('bta-progress-bar');
                const pmsg = _bta('bta-progress-msg');
                if (pbar) pbar.style.width = (d.progress || 0) + '%';
                if (pmsg) pmsg.textContent = d.progress_msg || 'Scanning…';
                if (d.status === 'scanning') {
                    _btaPoll();
                } else if ((d.status === 'done' || d.status === 'stopped') && d.data) {
                    _btaResetStopBtn();
                    _btaData = d.data;
                    _btaApplyResults(d.data, d.status);
                } else if (d.status === 'error') {
                    _btaResetStopBtn();
                    _btaSetState('idle');
                    const errEl = _bta('bta-error-msg');
                    if (errEl) { errEl.textContent = d.error || 'Scan error'; errEl.style.display = ''; }
                } else if (d.status === 'idle') {
                    _btaResetStopBtn();
                    _btaSetState('idle');
                }
            } catch (_) { _btaPoll(); }
        }, 2000);
    }

    function _btaApplyResults(data, status) {
        const s = data.stats || {};
        const setText = (id, v) => { const el = _bta(id); if (el) el.textContent = v; };
        setText('bta-stat-total',   s.total   ?? '—');
        setText('bta-stat-broken',  s.broken  ?? '—');
        setText('bta-stat-stale',   s.stale   ?? '—');
        setText('bta-stat-unknown', s.unknown ?? '—');
        const badge = _bta('bta-total-badge');
        if (badge) badge.textContent = s.broken ?? '—';
        if (data.scanned_at) {
            const el = _bta('bta-last-scanned');
            if (el) el.textContent = 'Scanned ' + new Date(data.scanned_at * 1000).toLocaleTimeString();
        }
        const partial = data.partial || status === 'stopped';
        const pb = _bta('bta-partial-badge');
        if (pb) pb.style.display = partial ? '' : 'none';
        _btaSetState(partial ? 'stopped' : 'done');
        _btaRender();
    }

    // ── BTA confirm modal ────────────────────────────────────────────────────
    async function _btaDoDelete(names, btnEl, btnRestoreText) {
        if (btnEl) { btnEl.disabled = true; btnEl.textContent = `Deleting ${names.length}…`; }
        try {
            const r = await fetch('/debrid_manager/api/bad_torrent_audit/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({names}),
            });
            const d = await r.json();
            if (!d.success) { _btaShowMsg('Delete error: ' + (d.error || 'Unknown error'), true); return; }
            const parts = [`Deleted ${d.deleted_rd} from RD.`];
            if (d.failed_rd) parts.push(`${d.failed_rd} failed. Error: ${d.first_error || 'unknown'}`);
            // Refresh results from server so remaining entries stay visible
            const refresh = await fetch('/debrid_manager/api/bad_torrent_audit');
            const rd = await refresh.json();
            if (rd.success && rd.data) {
                _btaData = rd.data;
                _btaApplyResults(rd.data, rd.status);
            }
            _btaShowMsg(parts.join(' '), false);
        } catch (e) {
            _btaShowMsg('Delete failed: ' + e.message, true);
        } finally {
            if (btnEl) { btnEl.disabled = false; btnEl.textContent = btnRestoreText; }
        }
    }

    function _btaShowMsg(msg, isError) {
        const errEl = _bta('bta-error-msg');
        if (errEl) {
            errEl.textContent = msg;
            errEl.style.color = isError ? '#e57373' : '#81c784';
            errEl.style.display = '';
            setTimeout(() => { if (errEl) errEl.style.display = 'none'; }, 6000);
        }
    }

    function _btaShowResult(msg, isError) {
        _btaSetState('idle');
        const errEl = _bta('bta-error-msg');
        if (errEl) {
            errEl.textContent = msg;
            errEl.style.color = isError ? '#e57373' : '#81c784';
            errEl.style.display = '';
            setTimeout(() => { if (errEl) errEl.style.display = 'none'; }, 6000);
        }
    }

    window._btaDeleteSelected = function () {
        const checked = [...document.querySelectorAll('.bta-row-check:checked')];
        const names = checked.map(cb => cb.dataset.name).filter(Boolean);
        if (!names.length) return;
        const btn = _bta('bta-bulk-delete-btn');
        showPopup({
            type: 'confirm',
            title: 'Delete Torrents',
            message: `Delete ${names.length} selected torrent(s) from Real-Debrid?`,
            confirmText: 'Delete',
            cancelText: 'Cancel',
            onConfirm: () => _btaDoDelete(names, btn, 'Delete Selected'),
        });
    };

    window._btaDeleteAllBroken = function () {
        if (!_btaData) return;
        const names = (_btaData.results || []).filter(r => r.status === 'broken').map(r => r.name);
        if (!names.length) return;
        const btn = _bta('bta-delete-all-broken-btn');
        showPopup({
            type: 'confirm',
            title: 'Delete All Broken',
            message: `Delete all ${names.length} broken torrent(s) from Real-Debrid?`,
            confirmText: 'Delete',
            cancelText: 'Cancel',
            onConfirm: () => _btaDoDelete(names, btn, 'Delete All Broken'),
        });
    };

    async function _btaDoReinsert(names, btnEl, btnRestoreText) {
        if (btnEl) { btnEl.disabled = true; btnEl.textContent = `Reinserting ${names.length}…`; }
        try {
            const r = await fetch('/debrid_manager/api/bad_torrent_audit/reinsert', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({names, provider_index: _getReinsertProviderIndex()}),
            });
            const d = await r.json();
            if (!d.success) { _btaShowMsg('Reinsert error: ' + (d.error || 'Unknown error'), true); return; }
            const parts = [`Reinserted ${d.reinserted} torrent(s).`];
            if (d.failed) parts.push(`${d.failed} failed.`);
            if (d.no_hash) parts.push(`${d.no_hash} no longer in __bad__ (hash unavailable).`);
            if (d.first_error) parts.push(`First error: ${d.first_error}`);
            _btaShowMsg(parts.join(' '), d.failed > 0);
        } catch (e) {
            _btaShowMsg('Reinsert failed: ' + e.message, true);
        } finally {
            if (btnEl) { btnEl.disabled = false; btnEl.textContent = btnRestoreText; }
        }
    }

    window._btaReinsertSelected = function () {
        const checked = [...document.querySelectorAll('.bta-row-check:checked')];
        const names = checked.map(cb => cb.dataset.name).filter(Boolean);
        if (!names.length) return;
        const btn = _bta('bta-bulk-reinsert-btn');
        showPopup({
            type: 'confirm',
            title: 'Reinsert Torrents',
            message: `Reinsert ${names.length} selected torrent(s) to ${_getReinsertProviderName()}?`,
            confirmText: 'Reinsert',
            cancelText: 'Cancel',
            onConfirm: () => _btaDoReinsert(names, btn, '↩ Reinsert Selected'),
        });
    };

    window._btaReinsertAllBroken = function () {
        if (!_btaData) return;
        const names = (_btaData.results || []).filter(r => r.status === 'broken').map(r => r.name);
        if (!names.length) return;
        const btn = _bta('bta-reinsert-all-broken-btn');
        showPopup({
            type: 'confirm',
            title: 'Reinsert All Broken',
            message: `Reinsert all ${names.length} broken torrent(s) to ${_getReinsertProviderName()}?`,
            confirmText: 'Reinsert',
            cancelText: 'Cancel',
            onConfirm: () => _btaDoReinsert(names, btn, '↩ Reinsert All Broken'),
        });
    };

    // Load existing results when tab is first clicked
    document.addEventListener('click', function (e) {
        const tab = e.target.closest('[data-rc-group="bad-torrents"]');
        if (!tab) return;
        if (_btaData) return; // already loaded
        fetch('/debrid_manager/api/bad_torrent_audit').then(r => r.json()).then(d => {
            if (!d.success) return;
            if ((d.status === 'done' || d.status === 'stopped') && d.data) {
                _btaData = d.data;
                _btaApplyResults(d.data, d.status);
            } else if (d.status === 'scanning') {
                _btaSetState('scanning');
                _btaPoll();
            }
            // else stays idle
        }).catch(() => {});
    });

    // Update tab badge on initial tab load
    const tabBadge = _bta('bta-total-badge');
    if (tabBadge) tabBadge.textContent = '—';
})();
