/* Moved from templates/debrid_manager.html. */
// ── Battery Audit ──────────────────────────────────────────────────────────
var _battLoaded = false;
var _battData   = null;
var _battActiveGroup = null;
var _battReidImdbId  = null;  // old IMDB ID being re-identified

function _battInitOnce() {
    if (_battLoaded) return;
    loadBatteryAudit(false);
}

function _battEl(id) { return document.getElementById(id); }

async function loadBatteryAudit(force) {
    _battLoaded = true;
    const loadEl = _battEl('batt-loading');
    const errEl  = _battEl('batt-error');
    const contEl = _battEl('batt-content');
    if (loadEl) { loadEl.style.display = ''; }
    if (errEl)  { errEl.style.display = 'none'; }
    if (contEl) { contEl.style.display = 'none'; }

    try {
        const url = '/debrid_manager/api/battery_audit' + (force ? '?force=1' : '');
        const r = await fetch(url);
        const d = await r.json();
        if (!d.success) throw new Error(d.error || 'Unknown error');
        _battData = d;
        _battRenderSummary(d.summary);
        if (loadEl) loadEl.style.display = 'none';
        if (contEl) contEl.style.display = '';
        // Auto-open first non-zero group
        if (!_battActiveGroup) {
            for (const g of ['orphaned_battery','missing_battery','stale_metadata','orphaned_tvdb','orphaned_tmdb','no_imdb_id']) {
                if ((d.summary[g] || 0) > 0) { _battShowGroup(g); break; }
            }
        } else {
            _battShowGroup(_battActiveGroup);
        }
    } catch(e) {
        if (loadEl) loadEl.style.display = 'none';
        if (errEl)  { errEl.textContent = 'Battery audit failed: ' + e.message; errEl.style.display = ''; }
    }
}

function _battRenderSummary(s) {
    const total = Object.values(s).reduce((a,b) => a+b, 0);
    const badge = _battEl('batt-total-badge');
    if (badge) {
        badge.textContent = total > 0 ? total : '✓';
        badge.style.color = total > 0 ? '#ef4444' : '#22c55e';
    }
    for (const [k, v] of Object.entries(s)) {
        const el = _battEl('batt-n-' + k);
        if (el) {
            el.textContent = v;
            el.style.color = v > 0 ? '#ef4444' : '#22c55e';
        }
    }
    // Mark category pill buttons active
    document.querySelectorAll('.dm-batt-cat').forEach(btn => {
        const g = btn.dataset.battGroup;
        btn.classList.toggle('dm-batt-cat-active', g === _battActiveGroup);
        btn.classList.toggle('dm-batt-cat-zero', (s[g] || 0) === 0);
    });
}

function _battShowGroup(name) {
    _battActiveGroup = name;
    // Hide all sections
    document.querySelectorAll('.dm-batt-section').forEach(s => s.style.display = 'none');
    // Show selected
    const sec = _battEl('batt-sec-' + name);
    if (sec) sec.style.display = 'block';
    // Update category pill active state
    document.querySelectorAll('.dm-batt-cat').forEach(btn => {
        btn.classList.toggle('dm-batt-cat-active', btn.dataset.battGroup === name);
    });
    // Render table if data available
    if (_battData) _battRenderGroup(name, _battData[name] || []);
}

document.querySelectorAll('.dm-batt-cat').forEach(btn => {
    btn.addEventListener('click', () => _battShowGroup(btn.dataset.battGroup));
});

function _battRenderGroup(group, rows) {
    const tbody = _battEl('batt-tbody-' + group);
    if (!tbody) return;
    if (!rows.length) {
        tbody.innerHTML = '<tr class="dm-empty-row"><td colspan="8">No issues found</td></tr>';
        return;
    }
    tbody.innerHTML = rows.map((r, i) => {
        const dataId = group === 'orphaned_tvdb' ? (r.tvdb_id || '') :
                       group === 'orphaned_tmdb' ? (r.tmdb_id || '') :
                       (r.imdb_id || '');
        const chk = `<input type="checkbox" class="batt-row-chk" data-group="${group}" data-idx="${i}" data-id="${escHtml(dataId)}" data-type="${escHtml(r.type||'')}">`;
        if (group === 'orphaned_battery') {
            const lastFetch = r.last_trakt_fetch ? r.last_trakt_fetch.slice(0,10) : '—';
            return `<tr>
                <td class="dm-col-check">${chk}</td>
                <td>${escHtml(r.title||'—')}</td>
                <td>${r.year||'—'}</td>
                <td><a href="https://www.imdb.com/title/${escHtml(r.imdb_id||'')}" target="_blank" rel="noopener">${escHtml(r.imdb_id||'—')}</a></td>
                <td>${escHtml(r.type||'—')}</td>
                <td>${lastFetch}</td>
                <td>
                    <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="_battReidentify('${escHtml(r.imdb_id||'')}','${escHtml(r.type||'')}',this)" title="Re-identify with correct IMDB ID">Re-ID</button>
                    <button class="dm-btn dm-btn-danger dm-btn-sm" onclick="_battDeleteBattery(['${escHtml(r.imdb_id||'')}'],this)" title="Delete from battery">Delete</button>
                </td>
            </tr>`;
        }
        if (group === 'missing_battery') {
            return `<tr>
                <td class="dm-col-check">${chk}</td>
                <td>${escHtml(r.title||'—')}</td>
                <td>${r.year||'—'}</td>
                <td><a href="https://www.imdb.com/title/${escHtml(r.imdb_id||'')}" target="_blank" rel="noopener">${escHtml(r.imdb_id||'—')}</a></td>
                <td>${escHtml(r.type||'—')}</td>
                <td>
                    <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="_battFetchMetadata([{imdb_id:'${escHtml(r.imdb_id||'')}',type:'${escHtml(r.type||'')}'}],this)" title="Fetch battery metadata">Fetch</button>
                </td>
            </tr>`;
        }
        if (group === 'stale_metadata') {
            const age = r.age_days != null ? r.age_days + 'd' : '—';
            return `<tr>
                <td class="dm-col-check">${chk}</td>
                <td>${escHtml(r.title||'—')}</td>
                <td>${r.year||'—'}</td>
                <td><a href="https://www.imdb.com/title/${escHtml(r.imdb_id||'')}" target="_blank" rel="noopener">${escHtml(r.imdb_id||'—')}</a></td>
                <td>${escHtml(r.type||'—')}</td>
                <td>${escHtml(r.media_status||'—')}</td>
                <td>${age}</td>
                <td>
                    <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="_battFetchMetadata([{imdb_id:'${escHtml(r.imdb_id||'')}',type:'${escHtml(r.type||'')}'}],this)" title="Refresh metadata">Refresh</button>
                </td>
            </tr>`;
        }
        if (group === 'orphaned_tvdb') {
            const tvdbUrl = `https://thetvdb.com/series/${encodeURIComponent(r.tvdb_id||'')}`;
            const imdbUrl = `https://www.imdb.com/title/${encodeURIComponent(r.imdb_id||'')}`;
            const mt = r.media_type === 'tv' ? 'show' : (r.media_type || '—');
            return `<tr>
                <td class="dm-col-check">${chk}</td>
                <td>${escHtml(r.title||'—')}</td>
                <td>${r.year||'—'}</td>
                <td><a href="${tvdbUrl}" target="_blank" rel="noopener">${escHtml(r.tvdb_id||'—')}</a></td>
                <td><a href="${imdbUrl}" target="_blank" rel="noopener">${escHtml(r.imdb_id||'—')}</a></td>
                <td>${escHtml(mt)}</td>
                <td>
                    <button class="dm-btn dm-btn-danger dm-btn-sm" onclick="_battDeleteMapping('tvdb',['${escHtml(r.tvdb_id||'')}'],this)" title="Delete mapping">Delete</button>
                </td>
            </tr>`;
        }
        if (group === 'orphaned_tmdb') {
            const tmdbType = r.media_type === 'movie' ? 'movie' : 'tv';
            const tmdbUrl = `https://www.themoviedb.org/${tmdbType}/${encodeURIComponent(r.tmdb_id||'')}`;
            const imdbUrl = `https://www.imdb.com/title/${encodeURIComponent(r.imdb_id||'')}`;
            const mt = r.media_type === 'tv' ? 'show' : (r.media_type || '—');
            return `<tr>
                <td class="dm-col-check">${chk}</td>
                <td>${escHtml(r.title||'—')}</td>
                <td>${r.year||'—'}</td>
                <td><a href="${tmdbUrl}" target="_blank" rel="noopener">${escHtml(r.tmdb_id||'—')}</a></td>
                <td><a href="${imdbUrl}" target="_blank" rel="noopener">${escHtml(r.imdb_id||'—')}</a></td>
                <td>${escHtml(mt)}</td>
                <td>
                    <button class="dm-btn dm-btn-danger dm-btn-sm" onclick="_battDeleteMapping('tmdb',['${escHtml(r.tmdb_id||'')}'],this)" title="Delete mapping">Delete</button>
                </td>
            </tr>`;
        }
        if (group === 'no_imdb_id') {
            const rowKey = `${encodeURIComponent(r.title||'')}|${r.year||''}|${encodeURIComponent(r.type||'')}`;
            return `<tr id="batt-noimdb-row-${rowKey}">
                <td>${escHtml(r.title||'—')}</td>
                <td>${r.year||'—'}</td>
                <td>${escHtml(r.type||'—')}</td>
                <td style="font-size:0.78rem;color:#888">${escHtml(r.filled_by_file||'—')}</td>
                <td>
                    <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">
                        <input type="text" class="batt-noimdb-input" placeholder="tt1234567"
                            style="width:100px;font-size:0.78rem;padding:2px 5px;border:1px solid var(--border-color,#444);background:var(--input-bg,#2a2a2a);color:var(--text-color,#eee);border-radius:3px"
                            data-title="${escHtml(r.title||'')}" data-year="${r.year||''}" data-type="${escHtml(r.type||'')}">
                        <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="_battAutoFillImdb(this)" title="Auto-search for IMDB ID">Auto</button>
                        <button class="dm-btn dm-btn-secondary dm-btn-sm" onclick="_battSetImdbId(this)" title="Set IMDB ID">Set</button>
                        <span class="batt-noimdb-msg" style="font-size:0.75rem"></span>
                    </div>
                </td>
            </tr>`;
        }
        return '';
    }).join('');

    // Wire select-all checkbox (no_imdb_id has no checkboxes — info only)
    const chkAll = _battEl('batt-chk-all-' + ({
        orphaned_battery: 'orphaned', missing_battery: 'missing',
        stale_metadata: 'stale', orphaned_tvdb: 'tvdb', orphaned_tmdb: 'tmdb'
    }[group] || group));
    if (chkAll) {
        chkAll.checked = false;
        chkAll.onchange = () => {
            tbody.querySelectorAll('.batt-row-chk').forEach(cb => cb.checked = chkAll.checked);
            _battUpdateBulkBtns(group);
        };
    }
    // Per-row checkbox → update bulk btn
    tbody.querySelectorAll('.batt-row-chk').forEach(cb =>
        cb.addEventListener('change', () => _battUpdateBulkBtns(group))
    );
    _battUpdateBulkBtns(group);
}

function _battGetCheckedIds(group) {
    const tbody = _battEl('batt-tbody-' + group);
    if (!tbody) return [];
    return Array.from(tbody.querySelectorAll('.batt-row-chk:checked')).map(cb => cb.dataset.id).filter(Boolean);
}

function _battUpdateBulkBtns(group) {
    const checked = _battGetCheckedIds(group).length > 0;
    const map = {
        orphaned_battery: 'batt-bulk-del-orphaned',
        missing_battery:  'batt-bulk-fetch-missing',
        stale_metadata:   'batt-bulk-refresh-stale',
        orphaned_tvdb:    'batt-bulk-del-tvdb',
        orphaned_tmdb:    'batt-bulk-del-tmdb',
    };
    const btn = _battEl(map[group]);
    if (btn) btn.disabled = !checked;
}

// Bulk action wiring
_battEl('batt-bulk-del-orphaned')?.addEventListener('click', () => {
    const ids = _battGetCheckedIds('orphaned_battery');
    if (!ids.length) return;
    _battDeleteBattery(ids);
});
_battEl('batt-bulk-fetch-missing')?.addEventListener('click', function() {
    const ids = _battGetCheckedIds('missing_battery');
    if (!ids.length) return;
    _battFetchMetadata(ids, this, 'missing_battery');
});
_battEl('batt-bulk-refresh-stale')?.addEventListener('click', function() {
    const ids = _battGetCheckedIds('stale_metadata');
    if (!ids.length) return;
    _battFetchMetadata(ids, this, 'stale_metadata');
});
_battEl('batt-bulk-del-tvdb')?.addEventListener('click', () => {
    const ids = _battGetCheckedIds('orphaned_tvdb');
    if (!ids.length) return;
    showPopup({
        type: 'confirm',
        title: 'Delete Mappings',
        message: `Delete ${ids.length} TVDB mapping(s)?`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: function() { _battDeleteMapping('tvdb', ids); }
    });
});
_battEl('batt-bulk-del-tmdb')?.addEventListener('click', () => {
    const ids = _battGetCheckedIds('orphaned_tmdb');
    if (!ids.length) return;
    showPopup({
        type: 'confirm',
        title: 'Delete Mappings',
        message: `Delete ${ids.length} TMDB mapping(s)?`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: function() { _battDeleteMapping('tmdb', ids); }
    });
});

// Reload + Sync buttons
_battEl('batt-reload-btn')?.addEventListener('click', () => {
    _battData = null;
    _battLoaded = false;
    loadBatteryAudit(true);
});
_battEl('batt-sync-btn')?.addEventListener('click', async function() {
    const orig = this.innerHTML;
    this.disabled = true;
    this.textContent = 'Syncing…';
    try {
        const r = await fetch('/debrid_manager/api/battery_audit/sync', {method:'POST'});
        const d = await r.json();
        if (d.success) {
            this.textContent = 'Started';
            setTimeout(() => { this.innerHTML = orig; this.disabled = false; }, 3000);
        } else {
            showPopup({ type: 'error', title: 'Error', message: 'Sync failed: ' + (d.error || 'unknown'), autoClose: 5000 });
            this.innerHTML = orig;
            this.disabled = false;
        }
    } catch(e) {
        showPopup({ type: 'error', title: 'Error', message: 'Error: ' + e.message, autoClose: 5000 });
        this.innerHTML = orig;
        this.disabled = false;
    }
});

async function _battFetchMetadata(imdb_ids, btn, group) {
    const isBulk = !!group;
    if (btn) { btn.disabled = true; btn.textContent = isBulk ? 'Queuing…' : '…'; }
    // Build items array with type info so the backend skips unnecessary API calls
    let items;
    if (isBulk) {
        const tbody = _battEl('batt-tbody-' + group);
        items = tbody
            ? Array.from(tbody.querySelectorAll('.batt-row-chk:checked'))
                   .map(cb => ({imdb_id: cb.dataset.id, type: cb.dataset.type || ''}))
                   .filter(x => x.imdb_id)
            : [];
    } else {
        items = (imdb_ids || []).map(x => typeof x === 'string' ? {imdb_id: x} : x);
    }
    try {
        const r = await fetch('/debrid_manager/api/battery_audit/refresh', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({items})
        });
        const d = await r.json();
        if (d.success) {
            // Optimistic: remove rows from DOM immediately — fetch runs in background.
            // If it fails the item will reappear on next "Refresh Audit".
            if (isBulk) {
                const tbody = _battEl('batt-tbody-' + group);
                if (tbody) tbody.querySelectorAll('.batt-row-chk:checked').forEach(cb => cb.closest('tr')?.remove());
                if (btn) { btn.textContent = 'Fetch Selected'; btn.disabled = true; }
            } else if (btn) {
                btn.closest('tr')?.remove();
            }
        } else {
            showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + d.error, autoClose: 5000 });
            if (btn) { btn.textContent = isBulk ? 'Fetch Selected' : 'Fetch'; btn.disabled = false; }
        }
    } catch(e) {
        showPopup({ type: 'error', title: 'Error', message: 'Error: ' + e.message, autoClose: 5000 });
        if (btn) { btn.textContent = isBulk ? 'Fetch Selected' : 'Fetch'; btn.disabled = false; }
    }
}

async function _battDeleteBattery(imdb_ids, btn) {
    showPopup({
        type: 'confirm',
        title: 'Delete Battery Items',
        message: `Delete ${imdb_ids.length} battery item(s)? This cannot be undone.`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            if (btn) { btn.disabled = true; btn.textContent = '…'; }
            try {
                const r = await fetch('/debrid_manager/api/battery_audit/delete_battery', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({imdb_ids})
                });
                const d = await r.json();
                if (d.success) {
                    _battData = null; _battLoaded = false;
                    loadBatteryAudit(true);
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + d.error, autoClose: 5000 });
                    if (btn) { btn.textContent = 'Delete'; btn.disabled = false; }
                }
            } catch(e) {
                showPopup({ type: 'error', title: 'Error', message: 'Error: ' + e.message, autoClose: 5000 });
                if (btn) { btn.textContent = 'Delete'; btn.disabled = false; }
            }
        }
    });
}

async function _battDeleteMapping(type, ids, btn) {
    if (btn) { btn.disabled = true; btn.textContent = '…'; }
    try {
        const r = await fetch('/debrid_manager/api/battery_audit/delete_mapping', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({type, ids})
        });
        const d = await r.json();
        if (d.success) {
            _battData = null; _battLoaded = false;
            loadBatteryAudit(true);
        } else {
            showPopup({ type: 'error', title: 'Error', message: 'Delete failed: ' + d.error, autoClose: 5000 });
            if (btn) { btn.textContent = 'Delete'; btn.disabled = false; }
        }
    } catch(e) {
        showPopup({ type: 'error', title: 'Error', message: 'Error: ' + e.message, autoClose: 5000 });
        if (btn) { btn.textContent = 'Delete'; btn.disabled = false; }
    }
}

// ── No IMDB ID: Set IMDB ID ────────────────────────────────────────────────
async function _battSetImdbId(btn) {
    const wrap  = btn.closest('td').querySelector('.batt-noimdb-input');
    const msgEl = btn.closest('td').querySelector('.batt-noimdb-msg');
    const imdb_id = (wrap.value || '').trim();
    if (!/^tt\d{7,8}$/.test(imdb_id)) {
        msgEl.style.color = '#dc3545';
        msgEl.textContent = 'Use format tt1234567';
        return;
    }
    btn.disabled = true; btn.textContent = '…';
    msgEl.textContent = '';
    try {
        const r = await fetch('/debrid_manager/api/battery_audit/set_imdb_id', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({
                title: wrap.dataset.title,
                year:  wrap.dataset.year ? parseInt(wrap.dataset.year, 10) : null,
                type:  wrap.dataset.type,
                imdb_id
            })
        });
        const d = await r.json();
        if (d.success) {
            msgEl.style.color = '#28a745';
            msgEl.textContent = `Saved (${d.updated} row${d.updated !== 1 ? 's' : ''})`;
            btn.textContent = 'Set';
            btn.disabled = false;
            // Reload audit after short delay so user sees confirmation
            setTimeout(() => { _battData = null; _battLoaded = false; loadBatteryAudit(true); }, 1500);
        } else {
            msgEl.style.color = '#dc3545';
            msgEl.textContent = d.error || 'Failed';
            btn.textContent = 'Set'; btn.disabled = false;
        }
    } catch(e) {
        msgEl.style.color = '#dc3545';
        msgEl.textContent = e.message;
        btn.textContent = 'Set'; btn.disabled = false;
    }
}

// ── No IMDB ID: Auto-lookup single row ────────────────────────────────────
function _battImdbMatchHtml(d) {
    const label = `${escHtml(d.matched_title||'')}${d.matched_year ? ' (' + d.matched_year + ')' : ''}`;
    const src   = d.source ? ` <span style="opacity:.6;font-size:.75em">[${escHtml(d.source)}]</span>` : '';
    const lowConf = d.confidence === 'low';
    const linkColor = lowConf ? '#fd7e14' : '#17a2b8';
    const warning   = lowConf ? ' ⚠ low confidence — verify' : '';
    return `<a href="https://www.imdb.com/title/${escHtml(d.imdb_id)}/" target="_blank" rel="noopener"
        style="color:${linkColor};text-decoration:none" title="Open on IMDB to verify">${label}${warning} ↗</a>${src}`;
}

async function _battAutoFillImdb(btn) {
    const td    = btn.closest('td');
    const input = td.querySelector('.batt-noimdb-input');
    const msgEl = td.querySelector('.batt-noimdb-msg');
    btn.disabled = true; btn.textContent = '…';
    msgEl.textContent = '';
    try {
        const r = await fetch('/debrid_manager/api/battery_audit/lookup_imdb', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({
                title: input.dataset.title,
                year:  input.dataset.year ? parseInt(input.dataset.year, 10) : null,
                type:  input.dataset.type,
            })
        });
        const d = await r.json();
        if (d.success && d.imdb_id) {
            input.value = d.imdb_id;
            msgEl.innerHTML = _battImdbMatchHtml(d);
            _battUpdateSetAllFoundBtn();
        } else {
            msgEl.style.color = '#dc3545';
            msgEl.textContent = d.error || 'Not found';
        }
    } catch(e) {
        msgEl.style.color = '#dc3545';
        msgEl.textContent = e.message;
    } finally {
        btn.disabled = false; btn.textContent = 'Auto';
    }
}

// ── No IMDB ID: Auto-fill all rows ────────────────────────────────────────
async function _battAutoFillAll() {
    const tbody   = _battEl('batt-tbody-no_imdb_id');
    const fillBtn = _battEl('batt-auto-fill-all-btn');
    const msgEl   = _battEl('batt-auto-fill-msg');
    if (!tbody) return;
    const rows = Array.from(tbody.querySelectorAll('tr'));
    if (!rows.length) return;

    fillBtn.disabled = true; fillBtn.textContent = 'Searching…';
    msgEl.textContent = ''; msgEl.style.color = '#888';

    const items = rows.map(row => {
        const input = row.querySelector('.batt-noimdb-input');
        if (!input) return null;
        return {
            title: input.dataset.title,
            year:  input.dataset.year ? parseInt(input.dataset.year, 10) : null,
            type:  input.dataset.type,
            _input: input,
            _msg:   row.querySelector('.batt-noimdb-msg'),
        };
    }).filter(Boolean);

    try {
        const r = await fetch('/debrid_manager/api/battery_audit/lookup_imdb_bulk', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ items: items.map(i => ({title: i.title, year: i.year, type: i.type})) })
        });
        const d = await r.json();
        if (!d.success) throw new Error(d.error || 'Bulk lookup failed');
        let found = 0;
        d.results.forEach((res, i) => {
            const item = items[i];
            if (!item) return;
            if (res.success && res.imdb_id) {
                item._input.value = res.imdb_id;
                item._msg.innerHTML = _battImdbMatchHtml(res);
                found++;
            } else {
                item._msg.style.color = '#888';
                item._msg.textContent = res.error || 'Not found';
            }
        });
        msgEl.textContent = `Found ${found} / ${items.length}`;
        msgEl.style.color = found > 0 ? '#28a745' : '#888';
        _battUpdateSetAllFoundBtn();
    } catch(e) {
        msgEl.style.color = '#dc3545';
        msgEl.textContent = e.message;
    } finally {
        fillBtn.disabled = false; fillBtn.textContent = 'Auto Fill All';
    }
}

function _battUpdateSetAllFoundBtn() {
    const tbody  = _battEl('batt-tbody-no_imdb_id');
    const setBtn = _battEl('batt-set-all-found-btn');
    if (!tbody || !setBtn) return;
    const hasValid = Array.from(tbody.querySelectorAll('.batt-noimdb-input'))
        .some(inp => /^tt\d{7,8}$/.test(inp.value.trim()));
    setBtn.style.display = hasValid ? '' : 'none';
}

async function _battSetAllFound() {
    const tbody  = _battEl('batt-tbody-no_imdb_id');
    const setBtn = _battEl('batt-set-all-found-btn');
    const msgEl  = _battEl('batt-auto-fill-msg');
    if (!tbody) return;

    const toSet = Array.from(tbody.querySelectorAll('.batt-noimdb-input'))
        .filter(inp => /^tt\d{7,8}$/.test(inp.value.trim()));
    if (!toSet.length) return;

    setBtn.disabled = true; setBtn.textContent = `Setting ${toSet.length}…`;
    let ok = 0, fail = 0;
    for (const input of toSet) {
        const msgRow = input.closest('td').querySelector('.batt-noimdb-msg');
        try {
            const r = await fetch('/debrid_manager/api/battery_audit/set_imdb_id', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({
                    title:   input.dataset.title,
                    year:    input.dataset.year ? parseInt(input.dataset.year, 10) : null,
                    type:    input.dataset.type,
                    imdb_id: input.value.trim(),
                })
            });
            const d = await r.json();
            if (d.success) {
                ok++;
                msgRow.style.color = '#28a745';
                msgRow.textContent = `Saved (${d.updated})`;
            } else {
                fail++;
                msgRow.style.color = '#dc3545';
                msgRow.textContent = d.error || 'Failed';
            }
        } catch(e) {
            fail++;
        }
    }
    msgEl.style.color = fail > 0 ? '#fd7e14' : '#28a745';
    msgEl.textContent = `Set ${ok} / ${ok + fail}${fail > 0 ? ` (${fail} failed)` : ''}`;
    setBtn.disabled = false; setBtn.textContent = 'Set All Found';
    setBtn.style.display = 'none';
    setTimeout(() => { _battData = null; _battLoaded = false; loadBatteryAudit(true); }, 1500);
}

// ── Re-identify Modal ──────────────────────────────────────────────────────
function _battReidentify(imdb_id, item_type, btn) {
    _battReidImdbId = imdb_id;
    const overlay = _battEl('batt-reid-overlay');
    const modal   = _battEl('batt-reid-modal');
    const cur     = _battEl('batt-reid-current');
    const newId   = _battEl('batt-reid-new-id');
    const preview = _battEl('batt-reid-preview');
    const opts    = _battEl('batt-reid-options');
    const footer  = _battEl('batt-reid-footer');
    const result  = _battEl('batt-reid-result');

    if (cur)     cur.textContent = `Current IMDB ID: ${imdb_id} (${item_type || 'unknown type'})`;
    if (newId)   { newId.value = ''; newId.dataset.itemType = item_type; }
    if (preview) preview.style.display = 'none';
    if (opts)    opts.style.display = 'none';
    if (footer)  footer.style.display = 'none';
    if (result)  { result.style.display = 'none'; result.innerHTML = ''; }

    if (overlay) overlay.style.display = 'block';
    if (modal)   modal.style.display = 'block';
    if (newId)   newId.focus();
}

_battEl('batt-reid-close')?.addEventListener('click', _battCloseReid);
_battEl('batt-reid-overlay')?.addEventListener('click', _battCloseReid);
function _battCloseReid() {
    _battEl('batt-reid-overlay').style.display = 'none';
    _battEl('batt-reid-modal').style.display = 'none';
}

_battEl('batt-reid-verify-btn')?.addEventListener('click', async function() {
    const newId = (_battEl('batt-reid-new-id')?.value || '').trim();
    const itype = _battEl('batt-reid-new-id')?.dataset.itemType || '';
    const preview = _battEl('batt-reid-preview');
    const opts    = _battEl('batt-reid-options');
    const footer  = _battEl('batt-reid-footer');
    if (!newId.startsWith('tt')) { showPopup({ type: 'warning', title: 'Required', message: 'Enter a valid IMDB ID (tt…)', autoClose: 4000 }); return; }
    this.disabled = true;
    this.textContent = 'Verifying…';
    try {
        const r = await fetch('/debrid_manager/api/battery_audit/reidentify/verify', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({new_imdb_id: newId, item_type: itype})
        });
        const d = await r.json();
        if (d.success) {
            if (preview) {
                preview.innerHTML = `<strong>${escHtml(d.title)}</strong> (${d.year||'?'}) — ${escHtml(d.type||'?')} — <a href="https://www.imdb.com/title/${escHtml(d.imdb_id)}" target="_blank" rel="noopener">${escHtml(d.imdb_id)}</a>`;
                preview.style.display = '';
                preview.style.color = '#22c55e';
            }
            if (opts)   opts.style.display = '';
            if (footer) footer.style.display = '';
        } else {
            if (preview) { preview.textContent = d.error; preview.style.display = ''; preview.style.color = '#ef4444'; }
            if (opts)   opts.style.display = 'none';
            if (footer) footer.style.display = 'none';
        }
    } catch(e) {
        if (preview) { preview.textContent = 'Error: ' + e.message; preview.style.display = ''; preview.style.color = '#ef4444'; }
    } finally {
        this.disabled = false;
        this.textContent = 'Verify';
    }
});

_battEl('batt-reid-commit-btn')?.addEventListener('click', async function() {
    const newId    = (_battEl('batt-reid-new-id')?.value || '').trim();
    const syncMain = _battEl('batt-reid-sync-main')?.checked ?? true;
    if (!_battReidImdbId || !newId) return;
    const _btn = this;
    showPopup({
        type: 'confirm',
        title: 'Re-identify Item',
        message: `Re-identify ${_battReidImdbId} → ${newId}?\nThis deletes the old battery entry and fetches metadata for the new ID.`,
        confirmText: 'Commit',
        cancelText: 'Cancel',
        onConfirm: async function() {
            _btn.disabled = true;
            _btn.textContent = 'Committing…';
            const result = _battEl('batt-reid-result');
            try {
                const r = await fetch('/debrid_manager/api/battery_audit/reidentify/commit', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({old_imdb_id: _battReidImdbId, new_imdb_id: newId, sync_main_db: syncMain})
                });
                const d = await r.json();
                if (d.success) {
                    if (result) {
                        result.innerHTML = `<span style="color:#22c55e">Done. ${escHtml(_battReidImdbId)} → ${escHtml(newId)} (${escHtml(d.new_title)})${d.main_updated ? ` — ${d.main_updated} main DB row(s) updated` : ''}</span>`;
                        result.style.display = '';
                    }
                    setTimeout(() => {
                        _battCloseReid();
                        _battData = null; _battLoaded = false;
                        loadBatteryAudit(true);
                    }, 2000);
                } else {
                    if (result) { result.innerHTML = `<span style="color:#ef4444">Error: ${escHtml(d.error)}</span>`; result.style.display = ''; }
                    _btn.disabled = false;
                    _btn.textContent = 'Commit Re-identify';
                }
            } catch(e) {
                if (result) { result.innerHTML = `<span style="color:#ef4444">Error: ${e.message}</span>`; result.style.display = ''; }
                _btn.disabled = false;
                _btn.textContent = 'Commit Re-identify';
            }
        }
    });
});

// Battery audit loads on-demand when Battery sub-tab is clicked (not auto-loaded)
