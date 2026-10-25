// Tangerine task tiles for task_timings.html (moved out of the template so browsers cache it).

// ══════════════════════════════════════════════════════════
//  TANGERINE TASK MANAGER — parallel renderer
//  Reads the same API data as the classic layout.
//  All save/toggle/trigger actions delegate to the existing
//  classic functions (saveSettings, toggleTaskStatus, triggerTask).
// ══════════════════════════════════════════════════════════
(function() {

// ── state ────────────────────────────────────────────────
const tngState = {
    tasks: [],       // flat array: {taskName, cat, info}
    tab: 'all',
    search: '',
};
let tngCountdownTimer = null;

// ── helpers ──────────────────────────────────────────────
function tngFmtInterval(sec) {
    if (!sec || sec <= 0) return 'N/A';
    if (sec < 60) return sec + ' sec';
    const m = Math.round(sec / 60);
    if (m < 60) return m + ' min';
    const h = Math.floor(m / 60), rm = m % 60;
    return h + 'h ' + rm + 'm';
}

function tngFmtCountdown(info) {
    if (!info.enabled) return '--:--:--';
    const t = info.next_run_in;
    if (!t) return '--:--:--';
    const pad = v => String(v).padStart(2, '0');
    return pad(t.hours) + ':' + pad(t.minutes) + ':' + pad(t.seconds);
}

function tngTotalSec(info) {
    if (!info.enabled || !info.next_run_in) return -1;
    return Math.max(0, Math.floor(info.next_run_in.total_seconds || 0));
}

function tngIsLargeInterval(sec) {
    return sec > 86400; // > 1 day
}

function tngDisplayName(taskName, info) {
    if (info.display_name) return info.display_name;
    let n = taskName;
    if (n === 'task_check_plex_files')          return 'Check Local Files for Plex Update';
    if (n === 'task_refresh_library_size_cache') return 'Refresh Debrid Library Size';
    if (n === 'task_repair_broken_nzbs')         return 'Repair Broken NZBs';
    if (n === 'task_repair_broken_debrids')      return 'Repair Broken Debrids';
    if (n === 'task_backfill_nzb_torrent_ids')   return 'Backfill NZB Torrent IDs';
    if (n === 'task_scan_mount_for_external_adds') return 'Scan Mount for External Adds';
    if (n === 'final_check_queue' || n === 'Final_check_queue') return 'Final Scrape';
    if (n.endsWith('_wanted')) n = n.replace('task_', '').replace('_wanted', '');
    else                       n = n.replace('task_', '');
    return n.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function tngCatFromApiKey(apiKey) {
    const map = {
        queues: 'queues', content_sources: 'sources', system_tasks: 'system',
        library_tasks: 'library', metadata_tasks: 'metadata', feature_tasks: 'features'
    };
    return map[apiKey] || apiKey;
}

// ── card builder ─────────────────────────────────────────
function tngBuildCard(taskName, cat, info) {
    const enabled     = info.enabled;
    const cfgSec      = info.configured_interval_seconds || info.current_interval_seconds || 0;
    const defSec      = info.default_interval_seconds || 0;
    const custSec     = info.custom_interval_seconds;
    const big         = tngIsLargeInterval(cfgSec);
    const cfgFmt      = tngFmtInterval(cfgSec);
    const defFmt      = tngFmtInterval(defSec);
    const cd          = tngFmtCountdown(info);
    const cdClass     = !enabled ? 'na' : big ? 'warn' : '';
    const topClass    = !enabled ? 'stop' : big ? 'warn' : 'go';
    const inputVal    = cfgSec > 0 ? Math.round(cfgSec) : '';
    const inputClass  = (custSec !== null && custSec !== undefined) ? 'tng-custom' : '';
    const name        = tngDisplayName(taskName, info);

    const card = document.createElement('div');
    card.className = 'tng-card' + (enabled ? '' : ' tng-dim');
    card.dataset.tngTask = taskName;
    card.dataset.tngCat  = cat;

    card.innerHTML = `
      <div class="tng-ctop ${topClass}"></div>
      <div class="tng-cbody">
        <div class="tng-chdr">
          <div class="tng-ctitle-row">
            <span class="tng-sdot ${enabled ? 'on' : 'off'}"></span>
            <span class="tng-cname">${name}</span>
          </div>
          <span class="tng-cbadge ${big ? 'big' : 'norm'}">${cfgFmt}</span>
        </div>
        <div class="tng-cmeta">
          Default: <b>${defFmt} (${defSec}s)</b>${custSec != null ? ` &nbsp;|&nbsp; Custom: <b>${custSec}s</b>` : ''}
        </div>
        <div class="tng-div"></div>
        <div class="tng-cdwrap">
          <div class="tng-cd ${cdClass}" data-tng-cd="${taskName}">${cd}</div>
          <div class="tng-cdlbl">until next run</div>
        </div>
        <div class="tng-div"></div>
        <div class="tng-bottom">
          <div class="tng-irow">
            <span class="tng-blbl">Interval</span>
            <div style="display:flex;align-items:center;gap:6px">
              <input class="tng-sinput ${inputClass}" type="number"
                     value="${inputVal}" min="1"
                     data-tng-task="${taskName}"
                     data-tng-def="${defSec}"
                     data-default-interval-seconds="${defSec}">
              <span class="tng-slbl">sec</span>
            </div>
          </div>
          <div class="tng-brow">
            <span class="tng-tlbl">${enabled ? 'Enabled' : 'Disabled'}</span>
            <label class="tng-tog">
              <input type="checkbox" ${enabled ? 'checked' : ''}
                     data-tng-toggle="${taskName}">
              <div class="tng-tog-track"></div>
              <div class="tng-tog-thumb"></div>
            </label>
          </div>
        </div>
      </div>
      <div class="tng-cftr">
        <button class="tng-ibtn" data-tng-info="${taskName}">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M8 7v4M8 5.5v.01"/></svg>
          Task Info
        </button>
      </div>`;

    // toggle → reuse existing toggleTaskStatus
    card.querySelector('[data-tng-toggle]').addEventListener('change', function(e) {
        e.stopPropagation();
        if (typeof toggleTaskStatus === 'function') toggleTaskStatus(taskName, this.checked);
    });

    // interval input → keep custom class in sync
    card.querySelector('.tng-sinput').addEventListener('input', function() {
        const v = parseInt(this.value, 10);
        const isDefault = isNaN(v) || v === defSec;
        this.classList.toggle('tng-custom', !isDefault);
    });

    // info button → open modal
    card.querySelector('[data-tng-info]').addEventListener('click', function(e) {
        e.stopPropagation();
        tngOpenModal(taskName, cat, info, name, cfgFmt, defFmt, enabled);
    });

    // click card body → trigger task (enabled only, avoid controls)
    if (enabled) {
        card.addEventListener('click', function(e) {
            if (e.target.closest('.tng-tog') || e.target.closest('.tng-sinput') ||
                e.target.closest('.tng-ibtn') || e.target.closest('.tng-bval')) return;
            if (typeof triggerTask === 'function') triggerTask(taskName);
        });
    }

    return card;
}

// ── render ───────────────────────────────────────────────
function tngRender() {
    const grid = document.getElementById('tngGrid');
    if (!grid) return;

    let list = tngState.tasks;
    if (tngState.tab !== 'all') list = list.filter(t => t.cat === tngState.tab);
    if (tngState.search) {
        const q = tngState.search.toLowerCase();
        list = list.filter(t => t.name.toLowerCase().includes(q) || t.cat.toLowerCase().includes(q));
    }

    grid.innerHTML = '';
    if (!list.length) {
        grid.innerHTML = '<div class="tng-no-results">No tasks match your filter</div>';
        return;
    }
    list.forEach(t => grid.appendChild(tngBuildCard(t.taskName, t.cat, t.info)));
}

// ── ingest API data ──────────────────────────────────────
let _tngLastFingerprint = '';

function tngIngestData(tasks) {
    const flat = [];
    const catMap = {
        queues: 'queues', content_sources: 'sources', system_tasks: 'system',
        library_tasks: 'library', metadata_tasks: 'metadata', feature_tasks: 'features'
    };
    Object.entries(catMap).forEach(([apiKey, cat]) => {
        Object.entries(tasks[apiKey] || {}).forEach(([taskName, info]) => {
            flat.push({ taskName, cat, name: tngDisplayName(taskName, info), info });
        });
    });
    tngState.tasks = flat;

    // update tab counts
    const counts = { all: flat.length, queues: 0, sources: 0, system: 0, library: 0, metadata: 0, features: 0 };
    flat.forEach(t => { if (counts[t.cat] !== undefined) counts[t.cat]++; });
    document.getElementById('tngCntAll').textContent      = counts.all;
    document.getElementById('tngCntQueues').textContent   = counts.queues;
    document.getElementById('tngCntSources').textContent  = counts.sources;
    document.getElementById('tngCntSystem').textContent   = counts.system;
    document.getElementById('tngCntLibrary').textContent  = counts.library;
    document.getElementById('tngCntMeta').textContent     = counts.metadata;
    document.getElementById('tngCntFeatures').textContent = counts.features;

    // Only rebuild cards when structure changes (task list, enabled state, custom interval)
    // Countdowns update via tngTickCountdowns — no DOM rebuild needed every second
    const fingerprint = flat.map(t => `${t.taskName}:${t.info.enabled}:${t.info.custom_interval_seconds}`).join('|');
    if (fingerprint !== _tngLastFingerprint) {
        _tngLastFingerprint = fingerprint;
        tngRender();
    }
    tngStartCountdowns();
}

// ── live countdowns ──────────────────────────────────────
const tngTimers = {};

function tngStartCountdowns() {
    tngState.tasks.forEach(t => {
        tngTimers[t.taskName] = tngTotalSec(t.info);
    });
}

function tngTickCountdowns() {
    document.querySelectorAll('[data-tng-cd]').forEach(el => {
        const key = el.dataset.tngCd;
        if (!(key in tngTimers) || tngTimers[key] < 0) return;
        if (tngTimers[key] > 0) tngTimers[key]--;
        const s = tngTimers[key];
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sc = s % 60;
        const pad = v => String(v).padStart(2, '0');
        el.textContent = pad(h) + ':' + pad(m) + ':' + pad(sc);
    });
}
setInterval(() => { if (!document.hidden) tngTickCountdowns(); }, 1000);

// Expose tngIngestData so the module script can call it directly
window.tngIngestData = tngIngestData;

// ── UI wiring (tabs, search, buttons, modal) ─────────────
document.addEventListener('DOMContentLoaded', function() {

    // Tab switching
    document.querySelectorAll('.tng-tab').forEach(btn => {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.tng-tab').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            tngState.tab = this.dataset.tngTab;
            tngRender();
        });
    });

    // Search
    document.getElementById('tngSearch').addEventListener('input', function() {
        tngState.search = this.value.trim();
        tngRender();
    });

    // Save — delegate to existing saveSettings
    document.getElementById('tngSaveBtn').addEventListener('click', function() {
        // Sync tng inputs back to hidden classic tiles so saveSettings picks them up
        document.querySelectorAll('.tng-sinput[data-tng-task]').forEach(tngInput => {
            const taskName = tngInput.dataset.tngTask;
            const classicInput = document.querySelector(`.task-tile[data-task-name="${taskName}"] .interval-input`);
            if (classicInput) classicInput.value = tngInput.value;
        });
        // Sync toggles
        document.querySelectorAll('[data-tng-toggle]').forEach(tngChk => {
            const taskName = tngChk.dataset.tngToggle;
            const classicChk = document.querySelector(`.task-tile[data-task-name="${taskName}"] input[type="checkbox"]`);
            if (classicChk) classicChk.checked = tngChk.checked;
        });
        if (typeof saveSettings === 'function') saveSettings();
    });

    // Reset — delegate to existing handleResetAllSettings
    document.getElementById('tngResetBtn').addEventListener('click', function() {
        if (typeof handleResetAllSettings === 'function') handleResetAllSettings();
    });

    // Modal close
    document.getElementById('tngModalClose').addEventListener('click', tngCloseModal);
    document.getElementById('tngModalOverlay').addEventListener('click', function(e) {
        if (e.target === this) tngCloseModal();
    });
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') tngCloseModal();
    });
});

// ── flow diagram toggle ──────────────────────────────────
window.tngToggleFlow = function() {
    document.getElementById('tngFbody').classList.toggle('open');
    document.getElementById('tngFchev').classList.toggle('open');
};

// ── help data ─────────────────────────────────────────────
// Keyed by internal task name (task_*, or the queue name), not the display
// name: display names are derived and drift, and a miss used to fall through
// to the content-source text for every unlisted system task.
let _tngHelpData = null;
async function tngGetHelpData() {
    if (_tngHelpData) return _tngHelpData;
    try {
        const r = await fetch('/static/js/task_help_data.json');
        _tngHelpData = await r.json();
    } catch(e) {
        _tngHelpData = {};
    }
    return _tngHelpData;
}
function tngFindHelp(helpData, taskName) {
    const tasks = helpData.tasks || {};
    if (tasks[taskName]) return tasks[taskName];
    if (/^task_.+_wanted$/.test(taskName)) return helpData.content_source || null;
    return helpData.fallback || null;
}

// ── modal ────────────────────────────────────────────────
function tngOpenModal(taskName, cat, info, name, cfgFmt, defFmt, enabled) {
    const catColors = {
        sources: ['#3b1e5f','#a78bfa'], queues: ['#1e1e3f','#818cf8'],
        system: ['#1a2e1a','#4ade80'],  library: ['#1e1e3f','#818cf8'],
        metadata: ['#1a1a2e','#a78bfa'], features: ['#2a1500','#fb923c']
    };
    const [cbg, cfg2] = catColors[cat] || ['#222','#aaa'];
    const [sbg, sfg]  = enabled ? ['#14532d','#4ade80'] : ['#7f1d1d','#fca5a5'];

    document.getElementById('tngModalTitle').textContent = name;
    document.getElementById('tngModalMeta').innerHTML = `
      <span class="tng-modal-pill" style="background:${cbg};color:${cfg2};border:1px solid ${cfg2}33">${cat}</span>
      <span class="tng-modal-pill" style="background:${sbg};color:${sfg};border:1px solid ${sfg}33">${enabled ? 'Enabled' : 'Disabled'}</span>
      <span class="tng-modal-pill" style="background:var(--tng-accent);color:#fff">Configured: ${cfgFmt}</span>
      <span class="tng-modal-pill" style="background:var(--tng-surface2);color:var(--tng-text2);border:1px solid var(--tng-border2)">Default: ${defFmt}</span>`;
    document.getElementById('tngModalDesc').innerHTML  = '';
    document.getElementById('tngModalRuns').textContent = '';
    document.getElementById('tngModalAction').innerHTML = '';
    document.getElementById('tngModalOverlay').classList.add('open');

    tngGetHelpData().then(helpData => {
        const help = tngFindHelp(helpData, taskName);
        if (help) {
            document.getElementById('tngModalDesc').textContent = help.description || '';
            document.getElementById('tngModalRuns').textContent = help.runs ? `When it runs: ${help.runs}` : '';
            const actionEl = document.getElementById('tngModalAction');
            if (help.action) {
                actionEl.innerHTML = '<b style="color:var(--tng-accent)">RUN NOW</b>: ';
                actionEl.appendChild(document.createTextNode(help.action));
            }
        }
    });
}

function tngCloseModal() {
    document.getElementById('tngModalOverlay').classList.remove('open');
}

// ── toast (used for tangerine-specific feedback) ─────────
let _tngToastTimer;
window.tngShowToast = function(msg, color) {
    const colors = { green: '#22c55e', red: '#ef4444', blue: '#3b82f6', orange: '#f97316' };
    document.getElementById('tngToastDot').style.background = colors[color] || colors.blue;
    document.getElementById('tngToastMsg').textContent = msg;
    const t = document.getElementById('tngToast');
    t.classList.add('show');
    clearTimeout(_tngToastTimer);
    _tngToastTimer = setTimeout(() => t.classList.remove('show'), 2500);
};

})(); // end IIFE
