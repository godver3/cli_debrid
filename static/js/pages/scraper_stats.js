/* Moved from templates/scraper_stats.html. */
(function () {
  const state = { days: '30', kind: 'all', sort: 'grabs', desc: true, open: new Set(), data: null, sel: null };
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  function when(ts) {
    if (!ts) return '—';
    const d = new Date(String(ts).replace(' ', 'T'));
    if (isNaN(d)) return esc(ts);
    const mins = Math.round((Date.now() - d) / 60000);
    if (mins < 60) return `${Math.max(mins, 0)}m ago`;
    if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
    return `${Math.round(mins / 1440)}d ago`;
  }

  function rate(v) {
    if (v === null || v === undefined) return '<span class="muted">—</span>';
    const color = v >= 80 ? 'var(--green)' : v >= 50 ? 'var(--yellow)' : 'var(--red)';
    return `<span class="rate">${v.toFixed(0)}%<span class="bar"><span style="width:${v}%;background:${color}"></span></span></span>`;
  }

  function num(v, cls) {
    return v ? `<span class="${cls || ''}">${v}</span>` : '<span class="muted">0</span>';
  }

  function cells(r) {
    return `<td>${num(r.grabs)}</td><td class="hide-sm">${num(r.in_library)}</td><td>${rate(r.success_rate)}</td>` +
      `<td>${num(r.failed, 'bad')}</td><td class="hide-sm">${num(r.repaired, 'warn')}</td>` +
      `<td class="hide-sm">${num(r.replaced)}</td><td class="hide-sm">${num(r.removed)}</td>` +
      `<td class="hide-sm">${num(r.pending)}</td>` +
      `<td class="hide-sm">${when(r.last_grab)}</td>`;
  }

  function sorted(rows) {
    const k = state.sort, dir = state.desc ? -1 : 1;
    return [...rows].sort((a, b) => {
      const x = a[k] ?? -1, y = b[k] ?? -1;
      return (x > y ? 1 : x < y ? -1 : 0) * dir;
    });
  }

  function renderTiles(scrapers) {
    const t = scrapers.reduce((acc, s) => {
      for (const k of ['grabs', 'in_library', 'failed', 'repaired', 'pending', 'ever_collected']) acc[k] += s[k] || 0;
      return acc;
    }, { grabs: 0, in_library: 0, failed: 0, repaired: 0, pending: 0, ever_collected: 0 });
    const settled = t.grabs - t.pending;
    const success = settled ? `${(100 * t.ever_collected / settled).toFixed(0)}%` : '—';
    $('ss-tiles').innerHTML = [
      ['Grabs', t.grabs], ['In library', t.in_library], ['Success', success],
      ['Failed', t.failed], ['Repaired', t.repaired], ['Scrapers', scrapers.length],
    ].map(([l, v]) => `<div class="tile"><div class="tile-val">${esc(v)}</div><div class="tile-lbl">${l}</div></div>`).join('');
  }

  function render() {
    const d = state.data;
    document.querySelectorAll('#ss-head th').forEach(th => th.classList.toggle('sorted', th.dataset.k === state.sort));
    $('ss-since').textContent = d && d.tracking_since
      ? `Tracking since ${new Date(String(d.tracking_since).replace(' ', 'T')).toLocaleDateString()}` : '';
    if (!d || !d.scrapers.length) {
      $('ss-tiles').innerHTML = '';
      $('ss-body').innerHTML = `<tr><td colspan="10" class="empty">${d && d.tracking_since
        ? 'No grabs in this range.'
        : 'No grabs recorded yet. Stats start with the next release cli_debrid grabs — earlier history isn’t available.'}</td></tr>`;
      return;
    }
    renderTiles(d.scrapers);
    let html = '';
    for (const s of sorted(d.scrapers)) {
      const open = state.open.has(s.instance);
      const hasIdx = s.indexers.length > 0;
      const sel = state.sel && state.sel.instance === s.instance && state.sel.indexer == null;
      html += `<tr class="scraper${sel ? ' selected' : ''}" data-i="${esc(s.instance)}">` +
        `<td><span class="caret">${hasIdx ? (open ? '▼' : '▶') : ''}</span>${esc(s.instance)}` +
        `${s.scraper_type && s.scraper_type !== s.instance ? `<span class="type">${esc(s.scraper_type)}</span>` : ''}</td>` +
        cells(s) + '</tr>';
      if (open) {
        for (const ix of sorted(s.indexers)) {
          const isel = state.sel && state.sel.instance === s.instance && state.sel.indexer === ix.indexer;
          html += `<tr class="indexer${isel ? ' selected' : ''}" data-i="${esc(s.instance)}" data-x="${esc(ix.indexer)}">` +
            `<td>${esc(ix.indexer)}</td>${cells(ix)}</tr>`;
        }
      }
    }
    $('ss-body').innerHTML = html;
  }

  async function load() {
    try {
      const r = await fetch(`/statistics/api/scraper_stats?days=${state.days}&kind=${state.kind}`);
      const j = await r.json();
      if (!j.success) throw new Error(j.error || 'request failed');
      state.data = j;
    } catch (e) {
      state.data = null;
      $('ss-body').innerHTML = `<tr><td colspan="10" class="empty">Could not load stats: ${esc(e.message)}</td></tr>`;
      return;
    }
    render();
    if (state.sel) loadRecent(state.sel.instance, state.sel.indexer);
  }

  async function loadRecent(instance, indexer) {
    state.sel = { instance, indexer };
    render();
    const q = new URLSearchParams({ instance, days: state.days, kind: state.kind });
    if (indexer != null) q.set('indexer', indexer);
    $('ss-recent').hidden = false;
    $('ss-recent-title').textContent = `Recent grabs — ${instance}${indexer != null ? ' / ' + indexer : ''}`;
    $('ss-recent-body').innerHTML = '<tr><td colspan="6" class="muted">Loading…</td></tr>';
    try {
      const j = await (await fetch(`/statistics/api/scraper_stats/recent?${q}`)).json();
      if (!j.success) throw new Error(j.error || 'request failed');
      $('ss-recent-body').innerHTML = j.grabs.length ? j.grabs.map(g => {
        const ep = g.season != null && g.episode != null
          ? ` S${String(g.season).padStart(2, '0')}E${String(g.episode).padStart(2, '0')}` : '';
        const split = g.source_detail ? Object.values(g.source_detail).reduce((acc, s) => {
          const k = s.indexer || s.scraper_instance || '?'; acc[k] = (acc[k] || 0) + 1; return acc;
        }, {}) : null;
        const idx = split ? Object.entries(split).map(([k, n]) => `${esc(k)} ×${n}`).join(', ') : esc(g.indexer || '—');
        return `<tr><td>${when(g.grabbed_at)}</td><td>${esc(g.title)}${ep}</td>` +
          `<td class="rel" title="${esc(g.release_title)}">${esc(g.release_title)}</td><td>${idx}</td>` +
          `<td>${esc(g.trigger)}</td>` +
          `<td><span class="badge b-${esc(g.outcome)}" title="${esc(g.outcome_reason || '')}">${esc(g.outcome)}</span></td></tr>`;
      }).join('') : '<tr><td colspan="6" class="muted">No grabs in this range.</td></tr>';
    } catch (e) {
      $('ss-recent-body').innerHTML = `<tr><td colspan="6" class="muted">Could not load: ${esc(e.message)}</td></tr>`;
    }
  }

  function pills(id, key) {
    $(id).addEventListener('click', e => {
      const b = e.target.closest('.pill');
      if (!b) return;
      $(id).querySelectorAll('.pill').forEach(p => p.classList.toggle('active', p === b));
      state[key] = b.dataset.v;
      try { localStorage.setItem('sstats-' + key, b.dataset.v); } catch (_) {}
      load();
    });
    let saved = null;
    try { saved = localStorage.getItem('sstats-' + key); } catch (_) {}
    const match = saved && $(id).querySelector(`.pill[data-v="${saved}"]`);
    if (match) {
      state[key] = saved;
      $(id).querySelectorAll('.pill').forEach(p => p.classList.toggle('active', p === match));
    }
  }

  $('ss-head').addEventListener('click', e => {
    const th = e.target.closest('th');
    if (!th) return;
    if (state.sort === th.dataset.k) state.desc = !state.desc;
    else { state.sort = th.dataset.k; state.desc = th.dataset.k !== 'instance'; }
    render();
  });

  $('ss-body').addEventListener('click', e => {
    const tr = e.target.closest('tr');
    if (!tr || !tr.dataset.i) return;
    const instance = tr.dataset.i;
    if (tr.classList.contains('scraper')) {
      const s = state.data.scrapers.find(x => x.instance === instance);
      if (s && s.indexers.length && e.target.closest('.caret')) {
        state.open.has(instance) ? state.open.delete(instance) : state.open.add(instance);
        render();
        return;
      }
      if (s && s.indexers.length) state.open.add(instance);
      loadRecent(instance, null);
    } else {
      loadRecent(instance, tr.dataset.x);
    }
  });

  pills('ss-days', 'days');
  pills('ss-kind', 'kind');
  load();
})();
