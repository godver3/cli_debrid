// Page script for templates/debrid_manager.html.
// Server-rendered values come from window.TPL_DEBRID_MANAGER, set inline in the template.
// ── Provider label (set from backend) ──────────────────────────────────────
const providerLabel = TPL_DEBRID_MANAGER.debrid_provider_name;
const _allProviders = TPL_DEBRID_MANAGER.debrid_all_providers;

// ── Global reinsert provider (persisted in localStorage) ───────────────────
function _getReinsertProviderIndex() {
    const sel = document.getElementById('global-reinsert-provider');
    if (sel) return parseInt(sel.value, 10) || 0;
    const stored = localStorage.getItem('dm_reinsert_provider_index');
    return stored !== null ? parseInt(stored, 10) : 0;
}
function _getReinsertProviderName() {
    const idx = _getReinsertProviderIndex();
    const p = _allProviders.find(p => p.index === idx);
    return p ? p.name : providerLabel;
}

// ── helpers ────────────────────────────────────────────────────────────────
function escHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function _esc(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function fmt_bytes(b) {
    if (!b) return '—';
    const units = ['B','KB','MB','GB','TB'];
    let i = 0;
    let v = b;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(1) + ' ' + units[i];
}

function make_re(raw) {
    if (!raw.trim()) return null;
    try { return new RegExp(raw, 'i'); }
    catch { return new RegExp(raw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'); }
}

function status_class(s) {
    const m = {
        downloading: 'dm-status-downloading',
        downloaded:  'dm-status-downloaded',
        queued:      'dm-status-queued',
        error:       'dm-status-error',
        magnet_error:'dm-status-error',
        virus:       'dm-status-error',
        dead:        'dm-status-error',
    };
    return m[(s||'').toLowerCase()] || 'dm-status-other';
}

// ── shared state (declared early — tab handler runs before section declarations) ──
let _libPollTimer  = null;
// Debrid Manager and the cli_mount page share this template; each shows its own tabs.
const DM_MODE = TPL_DEBRID_MANAGER.dm_mode;
// Set by the first tab click, which waits until every page script has loaded.
let _dmActiveTab   = '';
let _pollTimer     = null;
let _pollInterval  = 5000;
let _noChangeCount = 0;
let _bkLoaded      = false;

// ── tabs ───────────────────────────────────────────────────────────────────
document.querySelectorAll('.dm-tab').forEach(btn => {
    btn.addEventListener('click', () => {
        const prevTab = _dmActiveTab;
        _dmActiveTab = btn.dataset.tab;
        document.querySelectorAll('.dm-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.dm-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(btn.dataset.tab + '-tab').classList.add('active');
        sessionStorage.setItem('dm_tab_' + DM_MODE, btn.dataset.tab);
        // Pause active polling when leaving active tab, resume when returning
        if (prevTab === 'active' && btn.dataset.tab !== 'active') {
            clearTimeout(_pollTimer);
            document.getElementById('poll-indicator').classList.remove('dm-poll-active');
        } else if (btn.dataset.tab === 'active' && prevTab !== 'active') {
            _noChangeCount = 0;
            _pollInterval = 5000;
            fetchActive();
        }
        if (btn.dataset.tab === 'history' && !window._historyLoaded) loadHistory();
        if (btn.dataset.tab === 'library' && !window._libLoaded) loadLibrary(false);
        if (btn.dataset.tab === 'maintenance' && !_bkLoaded) loadMaintenance();
        if (btn.dataset.tab === 'usenet' && !window._usenetLoaded) loadUsenetTab();
        if (btn.dataset.tab === 'debrid-repair' && !window._debridLoaded) loadDebridTab();
        if (btn.dataset.tab === 'usenet') afStartPolling();
        if (btn.dataset.tab !== 'usenet') afStopPolling();
        if (btn.dataset.tab === 'reconcile' && !window._rcLoaded) { loadReconcile(false); loadBatteryAudit(false); }
        if (btn.dataset.tab === 'reconcile' && typeof _slInitAudit === 'function') _slInitAudit();
    });
});
// Opens the first tab. debrid_manager.html calls this after the last page
// script: the tab loaders live in the later files (repair.js, library.js,
// usenet.js ...), so clicking from here threw and left the tab on "Loading…".
function dmOpenInitialTab() {
    const saved = new URLSearchParams(window.location.search).get('tab')
        || sessionStorage.getItem('dm_tab_' + DM_MODE);
    const pick = name => name && document.querySelector(`.dm-tab[data-tab="${name}"]`);
    const btn = pick(saved) || pick(TPL_DEBRID_MANAGER.dm_default_tab) || document.querySelector('.dm-tab');
    if (btn) btn.click();
}
