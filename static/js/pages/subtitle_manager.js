// Tools → Subtitle Manager: library status, manual search/pick, history.
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));

  const MATCH_LABELS = {
    hash: 'file hash', release_group: 'group', streaming_service: 'service', source: 'source',
    resolution: 'resolution', video_codec: 'codec', audio_codec: 'audio', fps: 'fps',
    series: 'series', title: 'title', season: 'season', episode: 'episode', year: 'year',
    country: 'country', imdb_id: 'imdb', series_imdb_id: 'imdb', tmdb_id: 'tmdb',
    series_tmdb_id: 'tmdb', tvdb_id: 'tvdb', series_tvdb_id: 'tvdb'
  };
  const RELEASE_KEYS = ['hash', 'release_group', 'streaming_service', 'source', 'resolution', 'video_codec'];

  let state = { page: 1, hpage: 1, item: null, token: null };

  function toast(msg, err) {
    const t = $('sm-toast');
    t.textContent = msg;
    t.className = 'sm-toast' + (err ? ' err' : '');
    t.style.display = 'block';
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { t.style.display = 'none'; }, 4500);
  }

  async function api(url, opts) {
    const r = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
    let data = {};
    try { data = await r.json(); } catch (e) { /* non-JSON error page */ }
    if (!r.ok) throw new Error(data.error || data.message || ('HTTP ' + r.status));
    return data;
  }

  function fmtDate(s) {
    if (!s) return '';
    const d = new Date(String(s).replace(' ', 'T'));
    return isNaN(d) ? esc(s) : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }

  function matchChips(matches) {
    const keys = RELEASE_KEYS.filter((k) => (matches || []).includes(k));
    if (!keys.length) return '<span class="sm-muted">identity only</span>';
    return keys.map((k) => `<span class="sm-chip${k === 'hash' ? ' hash' : ''}">${esc(MATCH_LABELS[k] || k)}</span>`).join('');
  }

  // ---- library list: movies, and shows grouped by show → season → episode
  const GROUPS_PER_PAGE = 40;
  const open = new Set();          // 'show:<imdb>' / 'season:<imdb>:<n>'
  let listData = null;

  function chips(langs) {
    return langs.map((l) => `<span class="sm-chip ${l.has ? 'ok' : 'miss'}"${l.embedded ? ' title="Embedded in the file"' : ''}>${esc(l.lang)}${l.embedded ? ' · emb' : ''}</span>`).join('');
  }
  function summary(eps) {
    const miss = eps.filter((e) => e.langs.some((l) => !l.has)).length;
    return `<span class="sm-muted">${eps.length} episode${eps.length === 1 ? '' : 's'}</span>` +
      (miss ? ` <span class="sm-chip miss">${miss} missing</span>` : ' <span class="sm-chip ok">all have subtitles</span>');
  }
  function actions(attrs, anyMissing) {
    return `<span class="sm-actions">
      ${anyMissing ? `<button class="sm-btn sm-btn-accent sm-mini" data-act="search" ${attrs}>Search</button>` : ''}
      <button class="sm-btn sm-mini" data-act="sync" ${attrs}>Sync</button></span>`;
  }

  async function loadList() {
    const params = new URLSearchParams({ q: $('sm-q').value.trim(), status: $('sm-status').value, type: $('sm-type').value });
    const body = $('sm-rows');
    try {
      listData = await api('/api/subtitles/overview?' + params);
      renderList();
      watchEmbeddedScan(listData.embedded_scan);
    } catch (e) {
      body.innerHTML = `<tr><td colspan="3" class="sm-empty">Could not load: ${esc(e.message)}</td></tr>`;
    }
  }

  let scanTimer = null;
  let scanReloaded = false;
  function watchEmbeddedScan(scan) {
    const el = $('sm-scan');
    if (!el) return;
    clearTimeout(scanTimer);
    if (!scan || !scan.running) { el.style.display = 'none'; return; }
    el.style.display = '';
    el.textContent = `Checking the media server for embedded subtitles… ${scan.done || 0}/${scan.total || '?'} ` +
      '(good embedded tracks count as having the language; the list updates when this finishes)';
    scanTimer = setTimeout(async () => {
      try {
        const st = await api('/api/subtitles/embedded_scan');
        if (st.running) watchEmbeddedScan(st);
        else {
          el.style.display = 'none';
          // reload once to show the result; never chain into another scan
          if ((st.done || 0) > 0 && !scanReloaded) { scanReloaded = true; loadList(); }
        }
      } catch (e) { el.style.display = 'none'; }
    }, 3000);
  }

  function renderList() {
    const d = listData;
    const body = $('sm-rows');
    // build groups: each movie is its own group; episodes group by show
    const groups = [];
    const shows = new Map();
    for (const it of d.items) {
      if (it.type === 'episode') {
        const key = it.imdb_id || it.title;
        if (!shows.has(key)) { const g = { kind: 'show', key, title: it.title, eps: [] }; shows.set(key, g); groups.push(g); }
        shows.get(key).eps.push(it);
      } else {
        groups.push({ kind: 'movie', item: it });
      }
    }
    const pages = Math.max(1, Math.ceil(groups.length / GROUPS_PER_PAGE));
    state.page = Math.min(state.page, pages);
    const nShows = shows.size, nMovies = groups.length - nShows;
    $('sm-count').textContent = `${nMovies} movie${nMovies === 1 ? '' : 's'}, ${nShows} show${nShows === 1 ? '' : 's'}` +
      `${d.capped ? ' (first 5000 items)' : ''} · languages: ${d.languages.join(', ') || 'none set'}`;
    $('sm-page').textContent = `Page ${state.page} of ${pages}`;
    $('sm-prev').disabled = state.page <= 1;
    $('sm-next').disabled = state.page >= pages;
    if (!groups.length) { body.innerHTML = '<tr><td colspan="3" class="sm-empty">Nothing here.</td></tr>'; return; }
    const html = [];
    for (const g of groups.slice((state.page - 1) * GROUPS_PER_PAGE, state.page * GROUPS_PER_PAGE)) {
      if (g.kind === 'movie') {
        const it = g.item;
        html.push(`<tr class="sm-item"><td><a href="#" class="sm-open" data-id="${it.id}">${esc(it.label)}</a>
          <span class="sm-muted">${esc(it.version || '')}</span></td><td>${chips(it.langs)}</td>
          <td>${actions(`data-ids="${it.id}"`, it.langs.some((l) => !l.has))}</td></tr>`);
        continue;
      }
      const sk = 'show:' + g.key;
      const showOpen = open.has(sk);
      const imdbAttr = g.eps[0].imdb_id ? `data-imdb="${esc(g.eps[0].imdb_id)}"` : `data-ids="${g.eps.map((e) => e.id).join(',')}"`;
      html.push(`<tr class="sm-group"><td><a href="#" class="sm-toggle" data-key="${esc(sk)}">${showOpen ? '▾' : '▸'} <b>${esc(g.title)}</b></a></td>
        <td>${summary(g.eps)}</td><td>${actions(imdbAttr, g.eps.some((e) => e.langs.some((l) => !l.has)))}</td></tr>`);
      if (!showOpen) continue;
      const seasons = new Map();
      for (const e of g.eps) { const sn = e.season ?? 0; if (!seasons.has(sn)) seasons.set(sn, []); seasons.get(sn).push(e); }
      for (const [sn, eps] of [...seasons.entries()].sort((x, y) => x[0] - y[0])) {
        const k = `season:${g.key}:${sn}`;
        const so = open.has(k);
        const sAttr = g.eps[0].imdb_id ? `data-imdb="${esc(g.eps[0].imdb_id)}" data-season="${sn}"` : `data-ids="${eps.map((e) => e.id).join(',')}"`;
        html.push(`<tr class="sm-season"><td style="padding-left:28px"><a href="#" class="sm-toggle" data-key="${esc(k)}">${so ? '▾' : '▸'} Season ${sn}</a></td>
          <td>${summary(eps)}</td><td>${actions(sAttr, eps.some((e) => e.langs.some((l) => !l.has)))}</td></tr>`);
        if (!so) continue;
        for (const e of eps.sort((x, y) => (x.episode || 0) - (y.episode || 0))) {
          html.push(`<tr class="sm-item"><td style="padding-left:52px"><a href="#" class="sm-open" data-id="${e.id}">E${String(e.episode || 0).padStart(2, '0')}</a>
            <span class="sm-muted">${esc(e.episode_title || '')}</span></td><td>${chips(e.langs)}</td>
            <td>${actions(`data-ids="${e.id}"`, e.langs.some((l) => !l.has))}</td></tr>`);
        }
      }
    }
    body.innerHTML = html.join('');
  }

  async function rowAction(btn) {
    const body = { kind: btn.dataset.act };
    if (btn.dataset.ids) body.ids = btn.dataset.ids.split(',').map(Number);
    if (btn.dataset.imdb) body.imdb_id = btn.dataset.imdb;
    if (btn.dataset.season !== undefined) body.season = Number(btn.dataset.season);
    btn.disabled = true;
    try {
      const d = await api('/api/subtitles/bulk/start', { method: 'POST', body: JSON.stringify(body) });
      toast(d.message, !d.success);
      pollBulk();
    } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  }

  // ---- history
  async function loadHistory() {
    const body = $('sm-hrows');
    try {
      const d = await api('/api/subtitles/history?page=' + state.hpage);
      $('sm-hpage').textContent = `Page ${d.page}`;
      $('sm-hprev').disabled = d.page <= 1;
      $('sm-hnext').disabled = d.rows.length < 50;
      body.innerHTML = d.rows.length ? d.rows.map(historyRow).join('')
        : '<tr><td colspan="8" class="sm-empty">No subtitles downloaded yet.</td></tr>';
    } catch (e) {
      body.innerHTML = `<tr><td colspan="8" class="sm-empty">Could not load: ${esc(e.message)}</td></tr>`;
    }
  }

  function historyRow(h, withItem = true) {
    return `<tr${h.item_id && withItem ? ` class="sm-row" data-id="${h.item_id}"` : ''}>
      <td class="sm-muted">${esc(h.created_at || '')}</td>
      ${withItem ? `<td>${esc(h.title || '')}</td>` : ''}
      <td>${esc(h.language || '')}</td>
      <td>${esc(h.provider || '')}${h.source === 'manual' ? ' <span class="sm-chip">manual</span>' : ''}</td>
      <td class="sm-score">${h.score == null ? '' : esc(h.score)}</td>
      <td>${matchChips(h.matches)}</td>
      <td class="sm-muted">${esc(h.saved_via || '')}${h.format && h.format !== 'srt' ? ' · ' + esc(h.format) : ''}</td>
      <td class="sm-muted">${esc(h.sync || '')}</td>
    </tr>`;
  }

  // ---- item panel
  async function openItem(id) {
    state.item = id;
    state.token = null;
    $('sm-panel').classList.add('open');
    $('sm-d-title').textContent = 'Loading…';
    $('sm-d-sub').textContent = '';
    $('sm-d-results').innerHTML = '';
    $('sm-d-parsed').textContent = '';
    $('sm-d-status').textContent = '';
    try {
      const d = await api('/api/subtitles/item/' + id);
      $('sm-d-title').textContent = d.label;
      $('sm-d-sub').textContent = d.plex_mode ? 'Plex mode: subtitles are uploaded to the Plex item' : 'Subtitles are saved next to the library file';
      $('sm-d-file').innerHTML = `<span>Release</span><span>${esc(d.release || '—')}</span>
        <span>File</span><span>${esc(d.file || '—')}</span><span>Location</span><span>${esc(d.location || '—')}</span>`;
      $('sm-d-lang').innerHTML = '<option value="">All configured languages</option>' +
        d.languages.map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join('');
      renderCurrent(d);
      $('sm-d-history').innerHTML = d.history.length
        ? `<table class="sm-table"><tbody>${d.history.map((h) => historyRow(h, false)).join('')}</tbody></table>`
        : '<span class="sm-muted">Nothing downloaded by cli_debrid yet.</span>';
    } catch (e) {
      $('sm-d-title').textContent = 'Could not load item';
      $('sm-d-sub').textContent = e.message;
    }
  }

  function renderCurrent(d) {
    const el = $('sm-d-current');
    const parts = [];
    for (const sc of d.sidecars || []) {
      const ext = sc.name.split('.').pop().toLowerCase();
      parts.push(`<div style="display:flex;gap:8px;align-items:center;margin:3px 0;flex-wrap:wrap">
        <span class="sm-chip ok">${esc(sc.language)}</span><span class="sm-chip${ext === 'srt' ? '' : ' warn'}">${esc(ext)}</span>
        <span class="sm-chip">file</span><span style="word-break:break-all">${esc(sc.name)}</span>
        <button class="sm-btn sm-mini" data-sync-path="${esc(sc.path)}">Sync</button>
        <button class="sm-btn sm-btn-red sm-mini" data-del="${esc(sc.path)}">Delete</button></div>`);
    }
    if (d.plex_mode) {
      if (d.plex_streams == null) {
        parts.push('<span class="sm-muted">Could not ask Plex (no rating key yet, or Plex unreachable).</span>');
      } else {
        const haveFile = new Set((d.sidecars || []).map((sc) => sc.language.split('-')[0]));
        for (const st of d.plex_streams) {
          if (st.external && haveFile.has((st.language || 'und').split('-')[0])) continue;   // the file above
          const heavy = ['ass', 'ssa', 'pgs', 'vobsub', 'dvd_subtitle', 'hdmv_pgs_subtitle', 'dvb_subtitle'].includes((st.codec || '').toLowerCase());
          parts.push(`<div style="display:flex;gap:6px;align-items:center;margin:3px 0;flex-wrap:wrap"><span class="sm-chip ok">${esc(st.language || 'und')}</span>
            <span class="sm-chip${heavy ? ' warn' : ''}" title="${heavy ? 'Many clients transcode to show this format' : ''}">${esc(st.codec)}</span>
            ${st.forced ? '<span class="sm-chip">forced</span>' : ''}<span class="sm-chip">${st.external ? 'Plex upload' : 'embedded'}</span>
            <span class="sm-muted">${esc(st.title)}</span>
            ${st.external && st.id != null ? `<button class="sm-btn sm-mini" data-sync-stream="${esc(st.id)}">Sync</button>
            <button class="sm-btn sm-btn-red sm-mini" data-del-stream="${esc(st.id)}">Delete</button>` : ''}</div>`);
        }
      }
    }
    el.innerHTML = parts.join('') || '<span class="sm-muted">No subtitles for this item.</span>';
  }

  async function search() {
    const btn = $('sm-d-search');
    btn.disabled = true;
    $('sm-d-status').textContent = 'Searching…';
    $('sm-d-results').innerHTML = '';
    try {
      const d = await api(`/api/subtitles/item/${state.item}/search`, {
        method: 'POST', body: JSON.stringify({ language: $('sm-d-lang').value || null })
      });
      state.token = d.token;
      const p = d.parsed;
      $('sm-d-status').textContent = `${d.candidates.length} found in ${d.took}s from ${d.providers.join(', ')}`;
      $('sm-d-parsed').innerHTML = `Matching as <b>${esc(p.name)}</b> · group ${esc(p.group || '?')} · source ${esc(p.source || '?')}` +
        `${p.service ? ' · ' + esc(p.service) : ''} · ${d.hashed ? '<span class="sm-chip hash">file hashed</span>' : '<span class="sm-chip">no hash</span>'}` +
        ` · auto-download needs score ≥ ${d.min_score}`;
      if (!d.candidates.length) {
        $('sm-d-results').innerHTML = '<span class="sm-muted">No provider had anything for this item.</span>';
        return;
      }
      const top = Math.max(...d.candidates.map((c) => c.score), 1);
      $('sm-d-results').innerHTML = `<div class="sm-card"><table class="sm-table">
        <thead><tr><th>Score</th><th>Lang</th><th>Provider</th><th>Release</th><th>Matched on</th><th></th></tr></thead><tbody>
        ${d.candidates.map((c, i) => {
          const ok = c.score >= d.min_score;
          const color = c.matches.includes('hash') ? 'var(--sm-blue)' : ok ? 'var(--sm-green)' : 'var(--sm-red)';
          return `<tr>
            <td><span class="sm-score" style="color:${color}">${c.score}</span>
              <div class="sm-bar-score"><i style="width:${Math.round(100 * c.score / top)}%;background:${color}"></i></div></td>
            <td>${esc(c.language)}${c.hi ? ' <span class="sm-chip" title="Hearing impaired">HI</span>' : ''}</td>
            <td>${esc(c.provider)}</td>
            <td class="sm-release">${esc(c.release || '—')}</td>
            <td>${matchChips(c.matches)}${ok ? '' : ' <span class="sm-chip miss" title="Would not be auto-downloaded">below threshold</span>'}</td>
            <td><button class="sm-btn sm-btn-accent" data-pick="${i}">Download</button></td></tr>`;
        }).join('')}</tbody></table></div>`;
    } catch (e) {
      $('sm-d-status').textContent = '';
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  async function pick(index, btn) {
    btn.disabled = true;
    btn.textContent = 'Downloading…';
    try {
      await api(`/api/subtitles/item/${state.item}/download`, {
        method: 'POST', body: JSON.stringify({ token: state.token, index: Number(index) })
      });
      btn.textContent = 'Saved';
      toast('Subtitle saved');
      refreshItem();
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Download';
      toast(e.message, true);
    }
  }

  async function refreshItem() {
    try {
      const d = await api('/api/subtitles/item/' + state.item);
      renderCurrent(d);
      $('sm-d-history').innerHTML = d.history.length
        ? `<table class="sm-table"><tbody>${d.history.map((h) => historyRow(h, false)).join('')}</tbody></table>` : '';
    } catch (e) { /* panel keeps the old view */ }
  }

  async function autoPick() {
    const btn = $('sm-d-auto');
    btn.disabled = true;
    $('sm-d-status').textContent = 'Picking best match…';
    try {
      const d = await api(`/api/subtitles/item/${state.item}/auto`, { method: 'POST' });
      $('sm-d-status').textContent = d.message;
      toast(d.message, !d.success);
      refreshItem();
    } catch (e) {
      $('sm-d-status').textContent = '';
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  async function del(path) {
    if (!confirm('Delete this subtitle file?')) return;
    try {
      await api(`/api/subtitles/item/${state.item}/delete`, { method: 'POST', body: JSON.stringify({ path }) });
      toast('Deleted');
      refreshItem();
    } catch (e) { toast(e.message, true); }
  }

  async function syncOne(body, btn) {
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = 'Syncing…';
    try {
      const d = await api(`/api/subtitles/item/${state.item}/sync`, { method: 'POST', body: JSON.stringify(body) });
      toast(d.message || 'Done', false);
      if (d.changed) refreshItem();
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  async function delStream(id) {
    if (!confirm('Remove this subtitle from Plex?')) return;
    try {
      await api(`/api/subtitles/item/${state.item}/plex_delete`, { method: 'POST', body: JSON.stringify({ stream_id: id }) });
      toast('Removed from Plex');
      refreshItem();
    } catch (e) { toast(e.message, true); }
  }

  // ---- bulk runs
  let bulkTimer = null;
  let bulkWasRunning = false;
  async function bulkStart(kind) {
    const filt = { q: $('sm-q').value.trim(), type: $('sm-type').value };
    const scope = (filt.q ? `matching "${filt.q}"` : 'in the library') +
      (filt.type !== 'all' ? ` (${filt.type}s only)` : '');
    const msg = kind === 'sync'
      ? `Fix every existing subtitle ${scope}?\n\nEach one is replaced with a subtitle made for this release (file hash, same release group, or same Blu-ray/WEB source) when a provider has one, otherwise kept. The file itself isn't read (only its 64 KB hash). Subtitles checked before are skipped.`
      : `Search subtitles for every item missing a language ${scope}?\n\nProviders that hit their daily download limit are paused and the others carry on.`;
    if (!confirm(msg)) return;
    try {
      const d = await api('/api/subtitles/bulk/start', { method: 'POST', body: JSON.stringify(Object.assign({ kind }, filt)) });
      if (!d.success) { toast(d.message, true); return; }
      toast(d.message);
      pollBulk();
    } catch (e) { toast(e.message, true); }
  }

  function laneHtml(kind, d) {
    const sync = kind === 'sync';
    const title = sync ? 'Fixing existing subtitles' : 'Searching for missing subtitles';
    const state = d.running ? '' : d.stopped ? ' — stopped' : ' — done';
    const pct = d.total ? Math.round(100 * d.done / d.total) : 0;
    const rows = (d.recent || []).map((r) => {
      const chip = sync ? (r.ok ? 'replaced' : 'kept') : (r.ok ? 'saved' : 'none');
      return `<div class="sm-lane-row"><span class="sm-chip ${r.ok ? 'ok' : 'miss'}">${chip}</span>
        <a href="#" class="sm-open" data-id="${r.id}">${esc(r.label)}</a>
        <span class="sm-muted" style="word-break:break-word">${esc(r.note || '')}</span></div>`;
    }).join('');
    return `<div class="sm-lane">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <b>${title}${state}</b>
        <span class="sm-muted">${d.done}/${d.total} · ${d.found} ${sync ? 'fixed' : 'saved'}${d.queued ? ` · ${d.queued} waiting` : ''}</span>
        <span style="flex:1"></span>
        ${d.running ? `<button class="sm-btn sm-btn-red sm-mini" data-lane-stop="${kind}">Stop</button>`
                    : `<button class="sm-btn sm-mini" data-lane-hide="${kind}">Hide</button>`}
      </div>
      <div class="sm-bar-score" style="height:6px;margin:8px 0"><i style="width:${pct}%;background:var(--sm-accent)"></i></div>
      <div class="sm-muted">${d.running ? (d.current ? (sync ? 'Looking for a better subtitle for ' : 'Searching ') + esc(d.current) + '…' : 'Starting…') : esc(d.stopped || '')}</div>
      <div class="sm-lane-recent">${rows || '<span class="sm-muted">No results yet.</span>'}</div>
    </div>`;
  }

  const hiddenLanes = new Set();
  async function pollBulk() {
    clearTimeout(bulkTimer);
    let d;
    try { d = await api('/api/subtitles/bulk/status'); } catch (e) { bulkTimer = setTimeout(pollBulk, 5000); return; }
    const lanes = ['search', 'sync'].filter((k) => d[k] && (d[k].running || (d[k].total && !hiddenLanes.has(k))));
    for (const k of ['search', 'sync']) if (d[k] && d[k].running) hiddenLanes.delete(k);
    $('sm-bulk').style.display = lanes.length ? '' : 'none';
    $('sm-lanes').innerHTML = lanes.map((k) => laneHtml(k, d[k])).join('');
    const paused = Object.entries(d.paused_providers || {});
    $('sm-bulk-paused').textContent = paused.length ? 'Paused providers: ' + paused.map(([k, v]) => `${k} (${v})`).join(', ') : '';
    const anyRunning = ['search', 'sync'].some((k) => d[k] && d[k].running);
    if (anyRunning) bulkTimer = setTimeout(pollBulk, 2000);
    else if (bulkWasRunning) loadList();
    bulkWasRunning = anyRunning;
  }
  document.addEventListener('click', async (e) => {
    const st = e.target.closest('[data-lane-stop]');
    if (st) {
      try { await api('/api/subtitles/bulk/stop', { method: 'POST', body: JSON.stringify({ kind: st.dataset.laneStop }) });
        toast('Stopping after the current item; its queue is cleared'); } catch (err) { toast(err.message, true); }
      return;
    }
    const hd = e.target.closest('[data-lane-hide]');
    if (hd) { hiddenLanes.add(hd.dataset.laneHide); pollBulk(); }
  });
  $('sm-bulk-search').addEventListener('click', () => bulkStart('search'));
  $('sm-bulk-sync').addEventListener('click', () => bulkStart('sync'));

  // ---- Bazarr import
  function bzBody() {
    return { url: $('sm-bz-url').value.trim(), api_key: $('sm-bz-key').value.trim() };
  }
  async function bzPreview() {
    $('sm-bz-plan').style.display = 'none';
    $('sm-bz-status').textContent = '';
    try {
      const p = await api('/api/subtitles/bazarr/preview', { method: 'POST', body: JSON.stringify(bzBody()) });
      const cur = p.current || {};
      const row = (k, v) => `<span>${esc(k)}</span><span>${v}</span>`;
      $('sm-bz-kv').innerHTML = [
        row('Profiles', esc((p.profiles || []).join(', ') || '—')),
        row('Languages', `${esc(p.subtitle_languages || '—')} <span class="sm-muted">(now ${esc(cur.subtitle_languages || '—')})</span>`),
        row('Providers', `${esc((p.subtitle_providers || []).join(', ') || '—')} <span class="sm-muted">(now ${esc((cur.subtitle_providers || []).join(', ') || 'default')})</span>`),
        row('OpenSubtitles.com', p.opensubtitles_username ? `${esc(p.opensubtitles_username)} · password ${p.opensubtitles_password ? 'set' : 'missing'}` : 'not set in Bazarr'),
        row('Skip embedded languages', p.probe_for_embedded_subtitles ? 'on' : 'off'),
        row('Avoid transcoding formats', p.text_subtitles_only ? 'on (Bazarr ignores PGS/ASS)' : 'off'),
        row('Sync', p.sync_subtitles === 'off' ? 'off' : `unless matched by file hash · max ${esc(p.sync_max_offset)}s`),
      ].join('');
      $('sm-bz-not').innerHTML = (p.not_carried || []).length
        ? '<b>Not carried over:</b><br>' + p.not_carried.map(esc).join('<br>') : '';
      $('sm-bz-stopfeed').checked = !!cur.bazarr_spoof_enabled;
      $('sm-bz-stopfeed').parentElement.style.display = cur.bazarr_spoof_enabled ? '' : 'none';
      $('sm-bz-plan').style.display = '';
    } catch (e) { toast(e.message, true); }
  }
  async function bzApply() {
    const btn = $('sm-bz-apply');
    btn.disabled = true;
    try {
      const body = Object.assign(bzBody(), {
        enable_downloads: $('sm-bz-enable').checked, stop_bazarr_feed: $('sm-bz-stopfeed').checked
      });
      const d = await api('/api/subtitles/bazarr/import', { method: 'POST', body: JSON.stringify(body) });
      $('sm-bz-status').textContent = 'Imported: ' + d.applied.join(' · ');
      toast('Bazarr settings imported');
      loadList();
    } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  }
  $('sm-bz-open').addEventListener('click', () => {
    if (!$('sm-bz-url').value) $('sm-bz-url').value = 'http://' + location.hostname + ':6767';
    $('sm-bz').classList.add('open');
  });
  $('sm-bz-close').addEventListener('click', () => $('sm-bz').classList.remove('open'));
  $('sm-bz').addEventListener('click', (e) => { if (e.target === $('sm-bz')) $('sm-bz').classList.remove('open'); });
  $('sm-bz-preview').addEventListener('click', bzPreview);
  $('sm-bz-apply').addEventListener('click', bzApply);

  function closePanel() {
    $('sm-panel').classList.remove('open');
    state.item = null;
    loadList();
  }

  // ---- wiring
  document.querySelectorAll('.sm-tab').forEach((t) => t.addEventListener('click', () => {
    document.querySelectorAll('.sm-tab').forEach((x) => x.classList.toggle('active', x === t));
    const hist = t.dataset.tab === 'history';
    $('sm-library').style.display = hist ? 'none' : '';
    $('sm-history').style.display = hist ? '' : 'none';
    if (hist) loadHistory();
  }));
  let qTimer;
  $('sm-q').addEventListener('input', () => { clearTimeout(qTimer); qTimer = setTimeout(() => { state.page = 1; loadList(); }, 300); });
  $('sm-status').addEventListener('change', () => { state.page = 1; loadList(); });
  $('sm-type').addEventListener('change', () => { state.page = 1; loadList(); });
  $('sm-prev').addEventListener('click', () => { state.page = Math.max(1, state.page - 1); renderList(); });
  $('sm-next').addEventListener('click', () => { state.page += 1; renderList(); });
  $('sm-hprev').addEventListener('click', () => { state.hpage = Math.max(1, state.hpage - 1); loadHistory(); });
  $('sm-hnext').addEventListener('click', () => { state.hpage += 1; loadHistory(); });
  document.addEventListener('click', (e) => {
    const tg = e.target.closest('.sm-toggle');
    if (tg) { e.preventDefault(); open.has(tg.dataset.key) ? open.delete(tg.dataset.key) : open.add(tg.dataset.key); renderList(); return; }
    const op = e.target.closest('.sm-open');
    if (op) { e.preventDefault(); openItem(op.dataset.id); return; }
    const act = e.target.closest('[data-act]');
    if (act) { rowAction(act); return; }
    const row = e.target.closest('.sm-row');
    if (row && !e.target.closest('button')) { openItem(row.dataset.id); return; }
    const p = e.target.closest('[data-pick]');
    if (p) { pick(p.dataset.pick, p); return; }
    const d = e.target.closest('[data-del]');
    if (d) { del(d.dataset.del); return; }
    const sp = e.target.closest('[data-sync-path]');
    if (sp) { syncOne({ path: sp.dataset.syncPath }, sp); return; }
    const ss = e.target.closest('[data-sync-stream]');
    if (ss) { syncOne({ stream_id: ss.dataset.syncStream }, ss); return; }
    const ds = e.target.closest('[data-del-stream]');
    if (ds) { delStream(ds.dataset.delStream); }
  });
  $('sm-d-search').addEventListener('click', search);
  $('sm-d-auto').addEventListener('click', autoPick);
  $('sm-close').addEventListener('click', closePanel);
  $('sm-panel').addEventListener('click', (e) => { if (e.target === $('sm-panel')) closePanel(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && state.item) closePanel(); });

  loadList();
  pollBulk();
})();
