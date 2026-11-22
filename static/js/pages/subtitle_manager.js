// Tools → Subtitles: library status, manual search/pick, history.
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

  // ---- library list
  async function loadList() {
    const params = new URLSearchParams({
      q: $('sm-q').value.trim(), status: $('sm-status').value, type: $('sm-type').value, page: state.page
    });
    const body = $('sm-rows');
    try {
      const d = await api('/api/subtitles/overview?' + params);
      const pages = Math.max(1, Math.ceil(d.total / d.per_page));
      $('sm-count').textContent = `${d.total}${d.capped ? '+' : ''} item${d.total === 1 ? '' : 's'} · languages: ${d.languages.join(', ') || 'none set'}`;
      $('sm-page').textContent = `Page ${d.page} of ${pages}`;
      $('sm-prev').disabled = d.page <= 1;
      $('sm-next').disabled = d.page >= pages;
      if (!d.items.length) {
        body.innerHTML = '<tr><td colspan="4" class="sm-empty">Nothing here.</td></tr>';
        return;
      }
      body.innerHTML = d.items.map((it) => `
        <tr class="sm-row" data-id="${it.id}">
          <td>${esc(it.label)}</td>
          <td class="sm-muted">${esc(it.version || '')}</td>
          <td>${it.langs.map((l) => `<span class="sm-chip ${l.has ? 'ok' : 'miss'}">${esc(l.lang)}</span>`).join('')}</td>
          <td class="sm-muted">${fmtDate(it.collected_at)}</td>
        </tr>`).join('');
    } catch (e) {
      body.innerHTML = `<tr><td colspan="4" class="sm-empty">Could not load: ${esc(e.message)}</td></tr>`;
    }
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
        : '<tr><td colspan="7" class="sm-empty">No subtitles downloaded yet.</td></tr>';
    } catch (e) {
      body.innerHTML = `<tr><td colspan="7" class="sm-empty">Could not load: ${esc(e.message)}</td></tr>`;
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
    if (d.plex_mode) {
      if (d.plex_streams == null) {
        el.innerHTML = '<span class="sm-muted">Could not ask Plex (no rating key yet, or Plex unreachable).</span>';
        return;
      }
      if (!d.plex_streams.length) { el.innerHTML = '<span class="sm-muted">Plex has no subtitles for this item.</span>'; return; }
      el.innerHTML = d.plex_streams.map((s) => {
        const heavy = ['ass', 'ssa', 'pgs', 'vobsub', 'dvd_subtitle', 'hdmv_pgs_subtitle', 'dvb_subtitle'].includes((s.codec || '').toLowerCase());
        return `<div style="margin:3px 0"><span class="sm-chip ok">${esc(s.language || 'und')}</span>
          <span class="sm-chip${heavy ? ' warn' : ''}" title="${heavy ? 'Many clients transcode to show this format' : ''}">${esc(s.codec)}</span>
          ${s.forced ? '<span class="sm-chip">forced</span>' : ''}${s.external ? '<span class="sm-chip">external</span>' : '<span class="sm-chip">embedded</span>'}
          <span class="sm-muted">${esc(s.title)}</span></div>`;
      }).join('');
      return;
    }
    if (!d.sidecars.length) { el.innerHTML = '<span class="sm-muted">No subtitle files next to this file.</span>'; return; }
    el.innerHTML = d.sidecars.map((s) => {
      const ext = s.name.split('.').pop().toLowerCase();
      return `<div style="display:flex;gap:8px;align-items:center;margin:3px 0;flex-wrap:wrap">
        <span class="sm-chip ok">${esc(s.language)}</span><span class="sm-chip${ext === 'srt' ? '' : ' warn'}">${esc(ext)}</span>
        <span style="word-break:break-all">${esc(s.name)}</span>
        <button class="sm-btn sm-btn-red" data-del="${esc(s.path)}">Delete</button></div>`;
    }).join('');
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
  $('sm-prev').addEventListener('click', () => { state.page = Math.max(1, state.page - 1); loadList(); });
  $('sm-next').addEventListener('click', () => { state.page += 1; loadList(); });
  $('sm-hprev').addEventListener('click', () => { state.hpage = Math.max(1, state.hpage - 1); loadHistory(); });
  $('sm-hnext').addEventListener('click', () => { state.hpage += 1; loadHistory(); });
  document.addEventListener('click', (e) => {
    const row = e.target.closest('.sm-row');
    if (row && !e.target.closest('button')) { openItem(row.dataset.id); return; }
    const p = e.target.closest('[data-pick]');
    if (p) { pick(p.dataset.pick, p); return; }
    const d = e.target.closest('[data-del]');
    if (d) { del(d.dataset.del); }
  });
  $('sm-d-search').addEventListener('click', search);
  $('sm-d-auto').addEventListener('click', autoPick);
  $('sm-close').addEventListener('click', closePanel);
  $('sm-panel').addEventListener('click', (e) => { if (e.target === $('sm-panel')) closePanel(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && state.item) closePanel(); });

  loadList();
})();
