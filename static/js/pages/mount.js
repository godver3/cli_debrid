// Page script for templates/mount.html.
// Server-rendered values come from window.TPL_MOUNT, set inline in the template.
(function () {
  const $ = s => document.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmtBytes = b => { if (!b) return '0 B'; const u = ['B','KB','MB','GB','TB']; let i = 0; while (b >= 1024 && i < 4) { b /= 1024; i++; } return b.toFixed(i ? 1 : 0) + ' ' + u[i]; };
  const form = $('#mntForm');
  let loaded = false, dirty = false;
  // Server rows as last saved (from /mount/api/status); the On/Off toggle
  // saves from these so unsaved edits on other cards aren't committed with it.
  let savedServers = [];
  form.addEventListener('input', () => { dirty = true; });

  const field = (label, html, cls) => '<label class="sf' + (cls ? ' ' + cls : '') + '"><span>' + label + '</span>' + html + '</label>';
  const check = (label, k, on, title) => '<label class="sc"' + (title ? ' title="' + esc(title) + '"' : '') + '><input data-k="' + k + '" type="checkbox"' + (on ? ' checked' : '') + '> ' + label + '</label>';
  function serverState(card) {
    const on = card.querySelector('[data-k=enabled]').checked;
    card.classList.toggle('off', !on);
    const b = card.querySelector('.tg');
    b.textContent = on ? 'On' : 'Off';
    b.title = on ? 'In use. Click to turn this server off (saved right away, the mount restarts; its settings are kept).' : 'Turned off: the mount does not use it. Click to turn it back on (saved right away, the mount restarts).';
  }
  function serverRow(s) {
    s = s || {port: 563, ssl: true, connections: 10};
    const card = document.createElement('div');
    card.className = 'srvcard';
    card.innerHTML =
      '<div class="sh">' +
        '<button type="button" class="tg"></button><input data-k="enabled" type="checkbox" hidden' + (s.enabled === false ? '' : ' checked') + '>' +
        '<span class="sn">' + esc(s.host || 'New server') + '</span>' + (s.backup ? '<span class="sb">backup</span>' : '') +
        '<span class="sp"></span>' +
        '<button type="button" class="btn sec mv" data-d="-1" title="Move up">▲</button>' +
        '<button type="button" class="btn sec mv" data-d="1" title="Move down">▼</button>' +
        '<button type="button" class="btn sec rm" title="Remove this server">✕</button>' +
      '</div><div class="sg">' +
        field('Host', '<input data-k="host" value="' + esc(s.host) + '" placeholder="news.example.com">', 'w2') +
        field('Port', '<input data-k="port" type="number" value="' + esc(s.port ?? 563) + '">') +
        field('Connections', '<input data-k="connections" type="number" value="' + esc(s.connections ?? 10) + '">') +
        field('Username', '<input data-k="username" value="' + esc(s.username) + '">') +
        field('Password', '<input data-k="password" type="password" autocomplete="new-password" placeholder="' + (s.password_set ? 'unchanged' : '') + '">') +
        field('Backbone', '<input data-k="backbone" value="' + esc(s.backbone || '') + '" placeholder="e.g. omicron" title="Servers with the same backbone share one article store: when one says an article is missing, the others are not asked.">') +
        field('Plan ends', '<input data-k="expires" type="date" value="' + esc(s.expires || '') + '">') +
        '<div class="sf checks">' + check('SSL', 'ssl', s.ssl !== false) + check('Backup', 'backup', !!s.backup, 'Block account: asked only after every main server') +
          check('Auto-renews', 'auto_renew', !!s.auto_renew) + '</div>' +
      '</div>';
    // Which saved row this card is (host + username as loaded); none for a new card.
    const savedKey = s.host ? s.host + '\n' + (s.username || '') : '';
    serverState(card);
    card.querySelector('[data-k=host]').addEventListener('input', e => { card.querySelector('.sn').textContent = e.target.value || 'New server'; });
    card.querySelector('.tg').onclick = async () => {
      const box = card.querySelector('[data-k=enabled]');
      box.checked = !box.checked;
      serverState(card);
      // Only this server's flag is saved, on the rows as last saved: other
      // cards' unsaved edits stay for the Save button.
      const i = savedServers.findIndex(x => savedKey && x.host + '\n' + (x.username || '') === savedKey);
      if (i < 0) { dirty = true; return; }  // not saved yet: goes with the form
      const rows = savedServers.map((x, k) => k === i ? {...x, enabled: box.checked} : x);
      const msg = $('#srvMsg');
      msg.textContent = 'Saving…';
      try {
        const r = await fetch(TPL_MOUNT.url_for_4, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({servers: rows})});
        const j = await r.json();
        if (!j.success) throw new Error(j.error);
        savedServers = rows;
        msg.innerHTML = '<span class="ok">' + esc(card.querySelector('[data-k=host]').value) + ' turned ' + (box.checked ? 'on' : 'off') + '.</span> ' + (j.applying ? 'The mount restarts to apply it.' : '');
      } catch (e) {
        box.checked = !box.checked; serverState(card);
        msg.innerHTML = '<span class="no">Not saved: ' + esc(e.message || e) + '</span>';
      }
    };
    card.querySelector('.rm').onclick = () => { card.remove(); dirty = true; };
    card.querySelectorAll('.mv').forEach(b => b.onclick = () => {
      const sib = b.dataset.d === '-1' ? card.previousElementSibling : card.nextElementSibling;
      if (!sib) return;
      if (b.dataset.d === '-1') card.parentNode.insertBefore(card, sib); else card.parentNode.insertBefore(sib, card);
      dirty = true;
    });
    $('#servers').appendChild(card);
  }
  function collectServers() {
    return [...document.querySelectorAll('#servers .srvcard')].map(card => {
      const s = {};
      card.querySelectorAll('[data-k]').forEach(i => { s[i.dataset.k] = i.type === 'checkbox' ? i.checked : (i.type === 'number' ? parseInt(i.value || '0', 10) : i.value); });
      return s;
    });
  }
  $('#addServer').onclick = () => { serverRow(); dirty = true; };

  const VF_TYPES = ['regex', 'not_regex', 'include', 'exclude', 'starts_with', 'ends_with', 'not_starts_with', 'not_ends_with',
    'exact_match', 'not_exact_match', 'files_regex', 'not_files_regex', 'size_gt', 'size_lt', 'last_added',
    'file_count_gt', 'file_count_lt', 'category', 'not_category', 'tags', 'not_tags', 'protocol'];
  function vfFilterRow(box, f) {
    f = f || {type: 'regex', value: ''};
    const row = document.createElement('div');
    row.className = 'vff';
    row.style.cssText = 'display:flex;gap:6px;margin-top:6px';
    row.innerHTML = '<select class="vt" style="width:auto">' + VF_TYPES.map(t => '<option' + (t === f.type ? ' selected' : '') + '>' + t + '</option>').join('') + '</select>' +
      '<input class="vv" value="' + esc(f.value) + '" placeholder="value" style="flex:1">' +
      '<button type="button" class="btn sec" style="padding:4px 10px">✕</button>';
    row.querySelector('button').onclick = () => { row.remove(); dirty = true; };
    box.appendChild(row);
  }
  function vfRow(v) {
    v = v || {name: '', filters: [{type: 'regex', value: ''}]};
    const card = document.createElement('div');
    card.className = 'vf';
    card.style.cssText = 'border:1px solid #333;border-radius:6px;padding:8px 10px;margin-top:8px';
    card.innerHTML = '<div style="display:flex;gap:6px;align-items:center"><input class="vn" value="' + esc(v.name) + '" placeholder="folder name, e.g. movies" style="flex:1">' +
      '<button type="button" class="btn sec addf" style="padding:4px 10px">Add filter</button>' +
      '<button type="button" class="btn sec rmv" style="padding:4px 10px">Remove folder</button></div><div class="vfl"></div>';
    const box = card.querySelector('.vfl');
    const pathLine = document.createElement('div');
    pathLine.className = 'vfpath';
    pathLine.style.cssText = 'font-size:12px;color:#8f8f8f;margin-top:4px;word-break:break-all';
    card.insertBefore(pathLine, box);
    card.querySelector('.vn').addEventListener('input', vfPaths);
    (v.filters || []).forEach(f => vfFilterRow(box, f));
    card.querySelector('.addf').onclick = () => { vfFilterRow(box); dirty = true; };
    card.querySelector('.rmv').onclick = () => { card.remove(); dirty = true; };
    $('#vfolders').appendChild(card);
  }
  // Where each virtual folder appears, for pointing a Plex library at it.
  // Mirrors the engine: the name is trimmed, / and \ become _, and the mount's
  // own top folders can't be used.
  const VF_RESERVED = ['__all__', '__bad__', 'torrents', 'nzbs', '', '_', '.', '..'];
  function vfPaths() {
    const mp = ((form.querySelector('[name="mount_path"]') || {}).value || '/mnt/clid').trim().replace(/[\/\\]+$/, '');
    document.querySelectorAll('#vfolders .vf').forEach(card => {
      const name = card.querySelector('.vn').value.trim().replace(/[\/\\]/g, '_');
      const line = card.querySelector('.vfpath');
      if (!line) return;
      line.innerHTML = VF_RESERVED.includes(name.toLowerCase())
        ? '<span class="no">' + (name ? 'Reserved name, this folder is skipped.' : 'Give the folder a name.') + '</span>'
        : 'Plex library path: <code>' + esc(mp + '/' + name) + '</code>';
    });
  }
  form.addEventListener('input', e => { if (e.target && e.target.name === 'mount_path') vfPaths(); });
  $('#addVf').onclick = () => { vfRow(); vfPaths(); dirty = true; };
  const DEFAULT_VFOLDERS = TPL_MOUNT.default_vfolders;
  $('#addVfDefaults').onclick = () => {
    const have = new Set([...document.querySelectorAll('#vfolders .vn')].map(i => i.value.trim()));
    const add = DEFAULT_VFOLDERS.filter(v => !have.has(v.name));
    add.forEach(v => vfRow(JSON.parse(JSON.stringify(v))));
    vfPaths();
    if (add.length) dirty = true;
    $('#msg').textContent = add.length ? 'Added ' + add.map(v => v.name).join(' and ') + '. Save to apply.' : 'Movies and TV Shows are already there.';
  };

  function fill(settings) {
    form.querySelectorAll('[name]').forEach(el => {
      const v = settings[el.name];
      if (el.dataset.type === 'boolean') el.checked = !!v;
      else if (el.type === 'password') { el.value = ''; el.placeholder = settings[el.name + '_set'] ? 'unchanged' : 'not set'; }
      else el.value = v ?? '';
    });
    const pe = settings.plex_effective || {}, je = settings.jellyfin_effective || {}, se = settings.silo_effective || {};
    if ($('#plexEffective')) $('#plexEffective').textContent = pe.url
      ? 'Plex in use: ' + pe.url + ' (from ' + pe.url_source + '), token ' + (pe.token_set ? 'from ' + pe.token_source : 'not set')
      : 'No Plex URL set.';
    if ($('#jellyfinEffective')) $('#jellyfinEffective').textContent = (je.url
      ? 'Jellyfin/Emby in use: ' + je.url + ' (from ' + je.url_source + '), API key ' + (je.token_set ? 'from ' + je.token_source : 'not set')
      : 'No Jellyfin/Emby URL set.') + (!pe.url && !je.url && !se.url
      ? ' Stream protection, show precache and Now playing are off until a media server is entered here or in Settings.' : '');
    if ($('#siloEffective')) $('#siloEffective').textContent = se.url
      ? 'Silo in use: ' + se.url + ' (from Settings → File Management), API key ' + (se.key_set ? 'set' : 'not set')
      : 'Silo is not the media server (Settings → File Management → Media Server Type).';
    $('#servers').innerHTML = '';
    (settings.servers || []).forEach(serverRow);
    $('#vfolders').innerHTML = '';
    (settings.virtual_folders || []).forEach(v => vfRow(v));
    vfPaths();
  }

  function serverTest(kind) {
    const msg = $('#' + kind + 'TestMsg');
    msg.textContent = 'Testing…'; msg.style.color = '';
    const urlField = form.querySelector('[name=' + kind + '_url]'), tokenField = form.querySelector('[name=' + kind + '_token]');
    fetch('/mount/api/' + kind + '_test', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: urlField ? urlField.value : '', token: tokenField ? tokenField.value : ''})})
      .then(r => r.json()).then(d => {
        msg.style.color = d.ok ? '#4caf50' : '#e57373';
        msg.textContent = d.ok ? 'OK: ' + (d.server || d.url) + ', ' + d.sessions + ' playing now' : d.error;
      }).catch(err => { msg.style.color = '#e57373'; msg.textContent = String(err); });
  }
  if ($('#plexTest')) $('#plexTest').onclick = () => serverTest('plex');
  if ($('#jellyfinTest')) $('#jellyfinTest').onclick = () => serverTest('jellyfin');
  if ($('#siloTest')) $('#siloTest').onclick = () => serverTest('silo');

  // Speed tests: results kept across table refreshes, keyed kind:name.
  const speed = {};
  function speedCell(kind, name) {
    const r = speed[kind + ':' + name];
    let text = '';
    if (r && r.running) text = 'testing…';
    else if (r && r.ok) text = '<span class="ok">' + (+r.mb_per_second || 0).toFixed(r.mb_per_second >= 10 ? 0 : 1) + ' MB/s</span> · ' + Math.round(r.latency_ms) + ' ms · ' + esc(r.tested_at || '');
    else if (r) text = '<span class="no" title="' + esc(r.error || '') + '">failed</span> ' + esc(r.tested_at || '');
    return '<td style="white-space:nowrap"><button type="button" class="btn sec speedtest" data-kind="' + esc(kind) + '" data-name="' + esc(name) +
           '"' + (r && r.running ? ' disabled' : '') + ' style="padding:2px 8px;font-size:12px">Test</button> <span style="font-size:12px">' + text + '</span></td>';
  }
  document.addEventListener('click', ev => {
    const b = ev.target.closest('button.speedtest');
    if (!b) return;
    const key = b.dataset.kind + ':' + b.dataset.name;
    speed[key] = {running: true};
    b.disabled = true; b.nextElementSibling.textContent = 'testing…';
    fetch('/mount/api/speedtest', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: b.dataset.kind, provider: b.dataset.name})})
      .then(r => r.json()).then(d => { speed[key] = d; })
      .catch(err => { speed[key] = {ok: false, error: String(err)}; })
      .finally(() => { if (typeof refresh === 'function') refresh(); });
  });

  function tile(k, v, cls, sub) { return '<div class="tile"><div class="k">' + esc(k) + '</div><div class="v ' + (cls || '') + '">' + v + '</div>' + (sub ? '<div class="s">' + sub + '</div>' : '') + '</div>'; }

  function render(st) {
    const probs = st.selected ? (st.problems || []) : [];
    $('#problems').style.display = probs.length ? '' : 'none';
    $('#problems').innerHTML = probs.length ? '<strong>The engine can\'t start:</strong><br>' + probs.map(esc).join('<br>') : '';
    const e = st.engine, s = st.stats;
    const stateCls = st.state === 'RUNNING' ? 'ok' : (st.state === 'STARTING' ? 'mid' : 'no');
    // Biggest state first ("543 ready"), the rest ("2 failed") as the detail line.
    const jobList = e && e.jobs ? Object.entries(e.jobs).sort((a, b) => b[1] - a[1]).map(([k, v]) => v + ' ' + k) : null;
    const jobs = jobList ? (jobList[0] || 'none') : '—';
    const jobsSub = jobList && jobList.length > 1 ? esc(jobList.slice(1).join(' · ')) : '';
    const dc = s && s.disk_cache;
    const debrid = (s && s.debrid) || [];
    const debridBytes = debrid.reduce((n, d) => n + (d.bytes || 0), 0);
    $('#tiles').innerHTML =
      tile('Engine', esc(st.selected ? st.state : 'not selected'), st.selected ? stateCls : 'mid') +
      tile('Mount', e ? esc(e.mount_path) + (e.ready ? ' <span class="ok">●</span>' : ' <span class="no">●</span>') : '—') +
      tile('Jobs', esc(jobs), '', jobsSub) +
      tile('Downloaded', s ? fmtBytes((s.bytes_downloaded || 0) + debridBytes) : '—', '',
        s ? (debrid.length ? 'usenet ' + fmtBytes(s.bytes_downloaded || 0) + ' · debrid ' + fmtBytes(debridBytes) : (s.articles || 0) + ' articles') : '') +
      tile('Missing articles', s ? String(s.articles_missing || 0) : '—', s && s.articles_missing ? 'mid' : '') +
      tile('Disk cache', dc ? fmtBytes(dc.bytes) : '—', '', dc ? (dc.hits || 0) + ' hits · ' + (dc.misses || 0) + ' misses' : '');
    const la = st.last_sync && st.last_sync.result ? (st.last_sync.running ? 'applying…' : st.last_sync.result) : '';
    $('#lastapply').style.display = la ? '' : 'none';
    $('#lastapply').textContent = la ? 'Last settings apply: ' + la : '';
    const o = s && s.overview;
    const dur = sec => { sec = sec || 0; const d = Math.floor(sec / 86400), h = Math.floor(sec / 3600) % 24, m = Math.floor(sec / 60) % 60; return (d ? d + 'd ' : '') + (d || h ? h + 'h ' : '') + m + 'm'; };
    $('#tiles2').innerHTML = o ? (
      tile('Content', fmtBytes(o.content.bytes || 0), '',
        (o.content.jobs || 0) + ' folders · ' + (o.content.usenet_jobs || 0) + ' usenet · ' + (o.content.debrid_jobs || 0) + ' debrid · ' + (o.content.files || 0) + ' files') +
      tile('Streams', (o.streams.open || 0) + ' open', '', o.streams.rate_bytes ? fmtBytes(o.streams.rate_bytes) + '/s' : '') +
      tile('Health', (o.health.broken || 0) + ' broken', o.health.broken ? 'mid' : '',
        (o.health.healthy || 0) + ' healthy · ' + (o.health.unchecked || 0) + ' unchecked') +
      tile('Stream protection', !o.plex_protection.enabled ? 'off' : (o.plex_protection.active ? o.plex_protection.files.length + ' protected' : 'on'),
        o.plex_protection.enabled && !o.plex_protection.poll_ok && o.plex_protection.error ? 'mid' : '',
        !o.plex_protection.enabled ? '' : (o.plex_protection.active ? 'stream(s) playing while importing' :
          (!o.plex_protection.poll_ok ? esc(o.plex_protection.error || 'waiting for the media server') :
            ((o.plex_protection.files || []).length ? 'playing, no imports to protect from' : 'nothing playing')))) +
      tile('Memory cache', fmtBytes((s.mem_cache || {}).bytes || 0), '', ((s.mem_cache || {}).segments || 0) + ' segments') +
      tile('Uptime', dur(o.uptime_seconds), '', fmtBytes(o.runtime.heap_bytes || 0) + ' heap · ' + (o.runtime.goroutines || 0) + ' goroutines')
    ) : '';
    const pools = (s && s.pools) || (e && e.providers) || [];
    const openedFor = iso => { const t = Date.parse(iso); if (!t || t < 0) return '—'; const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
      return sec < 60 ? sec + 's' : (sec < 3600 ? Math.floor(sec / 60) + 'm' : Math.floor(sec / 3600) + 'h ' + Math.floor(sec / 60) % 60 + 'm'); };
    // Only while protection is active (playing AND an import is running).
    const protectedFiles = (o && o.plex_protection && o.plex_protection.active && o.plex_protection.files) || [];
    const streams = (st.streams || []).slice().sort((a, b) => (b.rate_bytes || 0) - (a.rate_bytes || 0) || (b.bytes_read || 0) - (a.bytes_read || 0));
    $('#streams tbody').innerHTML = streams.length ? streams.map(x => {
      const pct = x.size ? Math.min(100, Math.round(100 * (x.position || 0) / x.size)) : 0;
      const prot = protectedFiles.some(f => f && f.job_id === x.job_id && f.file === x.file);
      const idle = !(x.bytes_read > 0);
      return '<tr' + (idle ? ' style="color:#888"' : '') + '><td title="' + esc(x.folder) + '">' + esc(x.file) +
             '</td><td>' + esc(x.protocol) +
             '</td><td>' + openedFor(x.opened_at) + ' ago</td><td>' + fmtBytes(x.bytes_read || 0) + (idle ? ' (idle)' : '') +
             '</td><td>' + pct + '%</td><td>' + (x.rate_bytes ? fmtBytes(x.rate_bytes) + '/s' : '—') + '</td><td>' + (prot ? '<span class="ok">protected</span>' : '') + '</td></tr>';
    }).join('') : '<tr><td colspan="7" style="color:#888">Nothing open.</td></tr>';
    const debridKeys = st.debrid_keys || [];
    const DEBRID_NAMES = {rd: 'Real-Debrid', tb: 'TorBox', ad: 'AllDebrid', pm: 'Premiumize', dl: 'Debrid-Link', hw: 'HighWay'};
    $('#debridPools tbody').innerHTML = debrid.length ? debrid.map(d =>
      '<tr><td>' + esc(DEBRID_NAMES[d.provider] || d.provider) + '</td><td>' + (d.open || 0) + '</td><td>' + (d.requests || 0) +
      '</td><td>' + fmtBytes(d.bytes || 0) + '</td>' +
      '<td' + (d.errors ? ' class="no" title="' + esc(d.last_error || '') + '"' : '') + '>' + (d.errors || 0) + '</td>' +
      '<td' + (d.link_errors ? ' class="mid" title="' + esc(d.last_error || '') + '"' : '') + '>' + (d.link_errors || 0) + '</td>' +
      '<td>' + (d.latency_ms ? Math.round(d.latency_ms) + ' ms' : '—') + '</td>' + speedCell('debrid', d.provider) + '</tr>'
    ).join('') : (debridKeys.length ? debridKeys.map(k => '<tr><td>' + esc(DEBRID_NAMES[k] || k) + '</td><td colspan="6" style="color:#888">No reads since the engine started.</td>' + speedCell('debrid', k) + '</tr>').join('')
      : '<tr><td colspan="8" style="color:#888">No debrid reads since the engine started.</td></tr>');
    renderUsage(st, s, pools, debrid, DEBRID_NAMES);
    $('#pools tbody').innerHTML = pools.length ? pools.map(p => {
      const busy = Math.max(0, (p.open || 0) - (p.idle || 0));
      const pct = p.max ? Math.round(100 * busy / p.max) : 0;
      // Paused by the engine's breaker (refused login / unreachable), or
      // running under a lowered connection limit after "too many connections".
      const retry = p.retry_in_s ? ' (retry in ' + Math.ceil(p.retry_in_s) + 's)' : '';
      const paused = p.state === 'auth_failed' ? '<div class="no" style="font-size:12px">login refused' + retry + '</div>'
        : (p.state === 'down' ? '<div class="no" style="font-size:12px">unreachable' + retry + '</div>' : '');
      const capped = (p.cap_now && p.cap_now < p.max)
        ? '<div class="mid" style="font-size:12px" title="' + (p.server_limit ? 'Server says its limit is ' + p.server_limit : 'Server refused more connections') + '">limit ' + p.cap_now + '</div>' : '';
      return '<tr><td>' + esc(p.name) + '</td><td>' + (p.backup ? 'backup' : 'main') + paused + '</td><td>' + busy + ' / ' + p.max + capped +
             '</td><td>' + (p.open || 0) + '</td><td>' + (p.waiting || 0) + '</td><td><div class="bar"><i style="width:' + pct + '%"></i></div></td>' +
             '<td>' + (p.articles || 0) + '</td><td>' + fmtBytes(p.bytes || 0) + '</td>' +
             '<td' + (p.missing ? ' class="mid"' : '') + '>' + (p.missing || 0) + '</td>' +
             '<td' + (p.errors ? ' class="no" title="' + esc(p.last_error || '') + '"' : '') + '>' + (p.errors || 0) + '</td>' +
             '<td>' + (p.latency_ms ? Math.round(p.latency_ms) + ' ms' : '—') + '</td>' + speedCell('usenet', p.name) + '</tr>';
    }).join('') : '<tr><td colspan="12" style="color:#888">The engine isn\'t running.</td></tr>';
    if (st.settings && Array.isArray(st.settings.servers)) savedServers = st.settings.servers;
    if (!loaded || !dirty) { fill(st.settings); loaded = true; dirty = false; }
  }

  // Plan end cell: date, days left (red inside a week or past, amber inside
  // a month) and how it renews.
  function planCell(dateText, renew) {
    if (!dateText) return '<td style="color:#888">—</td>';
    const d = /^\d{4}-\d{2}-\d{2}$/.test(dateText) ? new Date(dateText + 'T00:00:00') : new Date(dateText);
    if (isNaN(d)) return '<td>' + esc(dateText) + '</td>';
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const day = new Date(d); day.setHours(0, 0, 0, 0);
    const left = Math.round((day - today) / 86400000);
    const cls = left <= 7 ? 'no' : (left <= 30 ? 'mid' : '');
    const when = left < 0 ? 'expired ' + (-left) + 'd ago' : (left === 0 ? 'ends today' : left + 'd left');
    return '<td>' + esc(d.toLocaleDateString()) + ' <span class="' + cls + '">(' + when + ')</span>' + (renew ? '<div style="color:#888;font-size:12px">' + esc(renew) + '</div>' : '') + '</td>';
  }

  function renderUsage(st, s, pools, debrid, names) {
    const usage = (s && s.usage) || {};
    const rows = [], seen = new Set();
    const add = (key, label, type, plan, renew) => {
      if (seen.has(key)) return;
      seen.add(key);
      rows.push({key, label, type, plan, renew, u: usage[key] || {}});
    };
    const servers = (st.settings && st.settings.servers) || [];
    servers.forEach(x => add('usenet/' + (x.name || x.host), x.host + (x.enabled === false ? ' (off)' : ''), x.backup ? 'usenet (backup)' : 'usenet', x.expires,
      x.expires ? (x.auto_renew ? 'auto-renews' : 'manual renewal') : ''));
    pools.forEach(p => add('usenet/' + p.name, p.name, 'usenet', '', ''));
    const plans = st.debrid_plans || {};
    const dkeys = new Set([...debrid.map(d => d.provider), ...Object.keys(plans),
      ...Object.keys(usage).filter(k => k.startsWith('debrid/')).map(k => k.slice(7))]);
    [...dkeys].sort().forEach(k => {
      const pl = plans[k] || {};
      add('debrid/' + k, names[k] || k, 'debrid', pl.expiration || '', pl.expiration ? 'from ' + (names[k] || k) : (pl.premium === false ? 'not premium' : ''));
    });
    Object.keys(usage).filter(k => k.startsWith('usenet/')).forEach(k => add(k, k.slice(7) + ' (removed)', 'usenet', '', ''));
    $('#usage tbody').innerHTML = rows.length ? rows.map(r =>
      '<tr><td>' + esc(r.label) + '</td><td>' + esc(r.type) + '</td><td>' + fmtBytes(r.u.today_bytes || 0) + '</td>' +
      '<td>' + fmtBytes(r.u.last_30d_bytes || 0) + '</td>' +
      '<td title="' + (r.u.days_tracked ? 'over ' + r.u.days_tracked + ' day(s) tracked' : '') + '">' + fmtBytes(r.u.avg_daily_bytes || 0) +
      (r.u.days_tracked && r.u.days_tracked < 30 ? ' <span style="color:#888;font-size:12px">(' + r.u.days_tracked + 'd)</span>' : '') + '</td>' +
      planCell(r.plan, r.renew) + '</tr>'
    ).join('') : '<tr><td colspan="6" style="color:#888">No servers or debrid services yet.</td></tr>';
  }

  async function refresh() {
    try { const r = await fetch(TPL_MOUNT.url_for_5); render(await r.json()); }
    catch (e) { $('#msg').textContent = 'Could not load status: ' + e; }
  }

  function collect() {
    const out = {};
    form.querySelectorAll('[name]').forEach(el => {
      if (el.dataset.type === 'boolean') out[el.name] = el.checked;
      else if (el.dataset.type === 'integer') out[el.name] = el.value === '' ? 0 : parseInt(el.value, 10);
      else out[el.name] = el.value;
    });
    out.virtual_folders = [...document.querySelectorAll('#vfolders .vf')].map(c => ({
      name: c.querySelector('.vn').value.trim(),
      filters: [...c.querySelectorAll('.vff')].map(r => ({type: r.querySelector('.vt').value, value: r.querySelector('.vv').value}))
    })).filter(v => v.name || v.filters.some(f => f.value));
    out.servers = collectServers();
    return out;
  }

  $('#saveBtn').onclick = async () => {
    const btn = $('#saveBtn'); btn.disabled = true; $('#msg').textContent = 'Saving…';
    try {
      const r = await fetch(TPL_MOUNT.url_for_4, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(collect())});
      const j = await r.json();
      if (!j.success) { $('#msg').innerHTML = '<span class="no">' + esc(j.error) + '</span>'; return; }
      dirty = false;
      $('#msg').innerHTML = j.changed.length ? '<span class="ok">Saved.</span> ' + (j.applying ? 'Applying to the engine…' : '') : 'Nothing changed.';
      setTimeout(refresh, 1500);
    } catch (e) { $('#msg').textContent = 'Save failed: ' + e; }
    finally { btn.disabled = false; }
  };
  async function cacheAction(action, btn) {
    if (action === 'purge' && !confirm('Purge the cache? Everything cached is downloaded again when next read. Playback keeps working.')) return;
    btn.disabled = true; $('#cacheMsg').textContent = action === 'purge' ? 'Purging…' : 'Running cleanup…';
    try {
      const r = await fetch('/mount/api/cache/' + action, {method: 'POST'});
      const d = await r.json();
      if (!d.success) { $('#cacheMsg').innerHTML = '<span class="no">' + esc(d.error || 'failed') + '</span>'; return; }
      const x = d.result || {};
      $('#cacheMsg').innerHTML = '<span class="ok">' + (action === 'purge'
        ? 'Purged ' + fmtBytes((x.disk_bytes_freed || 0) + (x.memory_bytes_freed || 0)) + ' (' + (x.disk_segments_removed || 0) + ' disk segments).'
        : 'Cleanup freed ' + fmtBytes(x.freed_bytes || 0) + ' (' + (x.expired_segments || 0) + ' expired, ' + (x.stray_files_removed || 0) + ' stray files).') + '</span>';
      refresh();
    } catch (e) { $('#cacheMsg').textContent = 'Failed: ' + e; }
    finally { btn.disabled = false; }
  }
  $('#cleanupBtn').onclick = e => cacheAction('cleanup', e.currentTarget);
  $('#purgeBtn').onclick = e => cacheAction('purge', e.currentTarget);

  $('#restartBtn').onclick = async () => {
    if (!confirm('Restart the mount engine? Anything playing from the mount will stop for a few seconds.')) return;
    $('#msg').textContent = 'Restarting…';
    const r = await fetch(TPL_MOUNT.url_for_3, {method: 'POST'});
    const j = await r.json();
    $('#msg').innerHTML = j.success ? '<span class="ok">Restarted.</span>' : '<span class="no">' + esc(j.error || j.output) + '</span>';
    setTimeout(refresh, 1500);
  };

  // ---- tabs ---------------------------------------------------------------
  let tab = 'overview';
  function showTab(name) {
    tab = name;
    document.querySelectorAll('#mntTabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
    document.querySelectorAll('.pane').forEach(p => { p.hidden = p.dataset.pane !== name; });
    try { history.replaceState(null, '', '#' + name); } catch (e) {}
    if (name === 'queue') loadJobs();
    if (name === 'browse') renderBrowse();
    if (name === 'migrate') loadMigrate();
  }
  document.querySelectorAll('#mntTabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));

  // ---- queue --------------------------------------------------------------
  let jobs = [], page = 0;
  // Long engine errors (one line per file of a failed NZB) are cut to one
  // line with a toggle; which ones are open survives the 4 s refresh.
  const ERR_SHORT = 180, openErrs = new Set();
  function errHtml(key, text, prefix) {
    text = (prefix || '') + text;
    if (text.length <= ERR_SHORT) return '<div class="err">' + esc(text) + '</div>';
    const open = openErrs.has(key);
    let shown = text;
    if (!open) {
      shown = text.slice(0, ERR_SHORT);
      const sp = shown.lastIndexOf(' ');
      if (sp > ERR_SHORT - 40) shown = shown.slice(0, sp);  // end on a whole word when one is near
      shown += '…';
    }
    return '<div class="err' + (open ? ' full' : '') + '">' + esc(shown) +
      ' <span class="errToggle" data-err="' + esc(key) + '">' + (open ? 'Show less' : 'Show full error') + '</span></div>';
  }
  function toggleErr(e, rerender) {
    const t = e.target.closest('[data-err]');
    if (!t) return false;
    const k = t.dataset.err;
    openErrs.has(k) ? openErrs.delete(k) : openErrs.add(k);
    rerender();
    return true;
  }
  const PER = 50;
  const ago = t => { const d = (Date.now() - new Date(t)) / 1000; if (!t || isNaN(d)) return ''; if (d < 60) return 'just now'; if (d < 3600) return Math.floor(d / 60) + 'm ago'; if (d < 86400) return Math.floor(d / 3600) + 'h ago'; return Math.floor(d / 86400) + 'd ago'; };
  const stateLabel = j => j.status === 'queued' && j.attempts ? 'retrying (' + j.attempts + ')' : j.status;
  // Category column: usenet or debrid (engine jobs carry protocol "debrid"; "" = usenet).
  const DEBRID_NAMES = {rd: 'Real-Debrid', tb: 'TorBox', ad: 'AllDebrid', pm: 'Premiumize', dl: 'Debrid-Link', hw: 'HighWay'};
  const jobKind = j => j.protocol === 'debrid' ? 'Debrid' : 'Usenet';
  const kindLabel = j => j.protocol === 'debrid' && j.provider
    ? 'Debrid (' + (DEBRID_NAMES[j.provider] || j.provider.toUpperCase()) + ')' : jobKind(j);
  async function loadJobs() {
    try {
      const r = await fetch(TPL_MOUNT.url_for_2);
      const d = await r.json();
      if (!d.success) { $('#qInfo').textContent = d.error || 'Could not load jobs'; jobs = []; renderJobs(); return; }
      jobs = d.jobs || [];
      const cats = [...new Set(jobs.map(jobKind))].sort();
      const sel = $('#qCategory'), keep = sel.value;
      sel.innerHTML = '<option value="">All categories</option>' + cats.map(c => '<option>' + esc(c) + '</option>').join('');
      sel.value = cats.includes(keep) ? keep : '';
      renderJobs();
      if (tab === 'browse') renderBrowse();
    } catch (e) { $('#qInfo').textContent = 'Could not load jobs: ' + e; }
  }
  function filtered() {
    const q = $('#qSearch').value.trim().toLowerCase(), st = $('#qStatus').value, cat = $('#qCategory').value, so = $('#qSort').value;
    const out = jobs.filter(j => (!q || (j.name || '').toLowerCase().includes(q)) && (!st || j.status === st) && (!cat || jobKind(j) === cat));
    const cmp = {added_desc: (a, b) => new Date(b.created_at) - new Date(a.created_at), added_asc: (a, b) => new Date(a.created_at) - new Date(b.created_at),
                 name_asc: (a, b) => (a.name || '').localeCompare(b.name || ''), name_desc: (a, b) => (b.name || '').localeCompare(a.name || ''),
                 size_desc: (a, b) => (b.size || 0) - (a.size || 0), size_asc: (a, b) => (a.size || 0) - (b.size || 0)}[so];
    return out.sort(cmp);
  }
  function renderJobs() {
    const list = filtered(), pages = Math.max(1, Math.ceil(list.length / PER));
    page = Math.min(page, pages - 1);
    const rows = list.slice(page * PER, page * PER + PER);
    $('#qTable tbody').innerHTML = rows.length ? rows.map(j => {
      const h = j.health ? j.health.status : '';
      return '<tr data-id="' + esc(j.id) + '"><td><input type="checkbox" class="qSel"></td>' +
        '<td class="nm"><span class="link" data-open="' + esc(j.id) + '">' + esc(j.name) + '</span>' + (j.error ? errHtml('q:' + j.id, j.error) : '') + '</td>' +
        '<td><span class="pill ' + esc(j.status) + '">' + esc(stateLabel(j)) + '</span></td>' +
        '<td>' + (j.size ? fmtBytes(j.size) : '—') + '</td><td' + (j.category ? ' title="' + esc('Engine category: ' + j.category) + '"' : '') + '>' + esc(kindLabel(j)) + '</td>' +
        '<td title="' + esc(j.created_at) + '">' + ago(j.created_at) + '</td>' +
        '<td>' + (h ? '<span class="pill ' + esc(h) + '">' + esc(h) + '</span>' : '—') + '</td>' +
        '<td style="white-space:nowrap">' + (j.status === 'failed' ? '<button class="rowbtn" data-act="retry">Retry</button>' : '') +
        (j.status === 'ready' ? '<button class="rowbtn" data-act="check">Check health</button>' : '') +
        '<button class="rowbtn" data-act="delete">Delete</button></td></tr>';
    }).join('') : '<tr><td colspan="8" style="color:#888">No jobs' + (jobs.length ? ' match these filters' : ' yet') + '.</td></tr>';
    $('#qInfo').textContent = list.length ? (page * PER + 1) + '–' + Math.min(list.length, page * PER + PER) + ' of ' + list.length : '';
    $('#qPages').innerHTML = pages > 1 ? [...Array(pages).keys()].map(i => '<button class="rowbtn" data-page="' + i + '"' + (i === page ? ' style="border-color:#e8601c"' : '') + '>' + (i + 1) + '</button>').join('') : '';
    $('#qAll').checked = false;
    updateSel();
  }
  function updateSel() { $('#qDeleteSel').hidden = !document.querySelector('.qSel:checked'); }
  async function del(ids) {
    if (!ids.length || !confirm('Delete ' + ids.length + ' job(s) from the mount? Their files disappear (cli_debrid sees them as removed). This cannot be undone.')) return;
    const r = await fetch(TPL_MOUNT.url_for, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids})});
    const d = await r.json();
    if (!d.success) alert('Could not delete ' + d.failed.length + ' job(s).');
    loadJobs();
  }
  ['#qSearch', '#qStatus', '#qCategory', '#qSort'].forEach(id => $(id).addEventListener('input', () => { page = 0; renderJobs(); }));
  $('#qRefresh').onclick = loadJobs;
  $('#qAll').onchange = e => { document.querySelectorAll('.qSel').forEach(c => { c.checked = e.target.checked; }); updateSel(); };
  $('#qDeleteSel').onclick = () => del([...document.querySelectorAll('.qSel:checked')].map(c => c.closest('tr').dataset.id));
  $('#qTable').addEventListener('change', e => { if (e.target.classList.contains('qSel')) updateSel(); });
  $('#qTable').addEventListener('click', async e => {
    if (toggleErr(e, renderJobs)) return;
    const pg = e.target.closest('[data-page]');
    if (pg) { page = +pg.dataset.page; renderJobs(); return; }
    const open = e.target.closest('[data-open]');
    if (open) { browseTop = '__all__'; browseJob = open.dataset.open; showTab('browse'); return; }
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const id = b.closest('tr').dataset.id;
    if (b.dataset.act === 'delete') return del([id]);
    b.disabled = true;
    const r = await fetch('/mount/api/jobs/' + encodeURIComponent(id) + '/' + b.dataset.act, {method: 'POST'});
    const d = await r.json();
    if (!d.success) alert(d.error || 'Failed');
    else if (b.dataset.act === 'check') b.textContent = 'Checking…';
    setTimeout(loadJobs, 1500);
  });
  $('#qPages').addEventListener('click', e => { const pg = e.target.closest('[data-page]'); if (pg) { page = +pg.dataset.page; renderJobs(); } });

  // ---- browse -------------------------------------------------------------
  // Same folders as the mount itself (and cli_mount): __all__, __bad__,
  // torrents, nzbs and one per debrid provider.
  let browseTop = null, browseJob = null;
  const TOP_NOTES = {'__all__': 'everything', '__bad__': 'jobs with a broken file', 'torrents': 'debrid',
                     'nzbs': 'usenet'};
  function crumbs(parts) {
    $('#bCrumbs').innerHTML = parts.map((p, i) => i === parts.length - 1 ? '<strong>' + esc(p[0]) + '</strong>'
      : '<a data-up="' + p[1] + '">' + esc(p[0]) + '</a>').join(' / ');
  }
  async function renderBrowse() {
    const q = $('#bSearch').value.trim().toLowerCase();
    const tb = $('#bTable tbody');
    if (!browseTop) {
      crumbs([['mount', 'root']]);
      const r = await fetch('/mount/api/tree');
      const d = await r.json().catch(() => ({}));
      if (tab !== 'browse' || browseTop) return;
      if (!d.success) { tb.innerHTML = '<tr><td colspan="4" style="color:#888">' + esc(d.error || 'The mount engine is not running.') + '</td></tr>'; return; }
      tb.innerHTML = d.tops.filter(t => !q || t.name.toLowerCase().includes(q)).map(t =>
        '<tr><td class="nm">📁 <span class="link" data-top="' + esc(t.name) + '">' + esc(t.name) + '</span></td><td>' +
        t.count + ' folder' + (t.count === 1 ? '' : 's') + '</td><td style="color:#999;font-size:12px">' +
        esc(TOP_NOTES[t.name] || 'debrid provider') + '</td><td></td></tr>').join('');
      return;
    }
    if (!browseJob) {
      crumbs([['mount', 'root'], [browseTop, 'top']]);
      const top = browseTop;
      const r = await fetch('/mount/api/tree/' + encodeURIComponent(top));
      const d = await r.json().catch(() => ({}));
      if (tab !== 'browse' || browseTop !== top || browseJob) return;
      if (!d.success) { browseTop = null; return renderBrowse(); }
      const list = d.folders.filter(f => !q || f.name.toLowerCase().includes(q)).sort((a, b) => a.name.localeCompare(b.name));
      tb.innerHTML = list.length ? list.map(f => '<tr><td class="nm">📁 <span class="link" data-open="' + esc(f.job_id) + '">' + esc(f.name) +
        '</span></td><td>' + fmtBytes(f.size) + '</td><td style="color:#999;font-size:12px">' + f.files + ' file' + (f.files === 1 ? '' : 's') +
        '</td><td></td></tr>').join('') : '<tr><td colspan="4" style="color:#888">Empty folder.</td></tr>';
      return;
    }
    const r = await fetch('/mount/api/jobs/' + encodeURIComponent(browseJob));
    const d = await r.json();
    if (!d.success) { browseJob = null; return renderBrowse(); }
    const j = d.job;
    crumbs([['mount', 'root'], [browseTop, 'top'], [j.name, 'job']]);
    const broken = new Set(((j.health || {}).broken_files || []).map(b => b.file_name));
    const hidden = new Set(j.hidden || []);
    const ents = (j.entries || []).filter(e => !q || e.name.toLowerCase().includes(q));
    tb.innerHTML = (ents.length ? ents.map(e => '<tr><td class="nm">🎞 ' + esc(e.name) + (hidden.has(e.name) ? ' <span class="pill">hidden by repair</span>' : '') +
        (broken.has(e.name) ? ' <span class="pill broken">broken</span>' : '') + '</td><td>' + fmtBytes(e.size) + '</td><td style="color:#999;font-size:12px">' +
        (e.from_archive ? 'inside ' + esc(e.from_archive) : 'plain file') + '</td><td><button class="rowbtn" data-copy="' + esc(e.path) + '">Copy path</button></td></tr>').join('')
      : '<tr><td colspan="4" style="color:#888">No files.</td></tr>') +
      ((j.failures || []).length ? '<tr><td colspan="4">' + errHtml('b:' + j.id, j.failures.map(f => f.name + ' (' + f.error + ')').join('; '), 'Not streamable: ') + '</td></tr>' : '');
  }
  $('#bSearch').addEventListener('input', renderBrowse);
  $('#bCrumbs').addEventListener('click', e => {
    const up = e.target.closest('[data-up]');
    if (!up) return;
    if (up.dataset.up === 'root') browseTop = null;
    browseJob = null;
    $('#bSearch').value = '';
    renderBrowse();
  });
  $('#bTable').addEventListener('click', e => {
    if (toggleErr(e, renderBrowse)) return;
    const top = e.target.closest('[data-top]');
    if (top) { browseTop = top.dataset.top; browseJob = null; $('#bSearch').value = ''; renderBrowse(); return; }
    const open = e.target.closest('[data-open]');
    if (open) { browseJob = open.dataset.open; $('#bSearch').value = ''; renderBrowse(); return; }
    const c = e.target.closest('[data-copy]');
    if (c) { navigator.clipboard && navigator.clipboard.writeText(c.dataset.copy); c.textContent = 'Copied'; setTimeout(() => { c.textContent = 'Copy path'; }, 1200); }
  });

  // ---- migrate -----------------------------------------------------------
  let mg = null, mgJob = null, mgTimer = null;
  async function loadMigrate() {
    const r = await fetch('/mount/api/migrate/status');
    mg = await r.json();
    const ok = (b, t, f) => '<div>' + (b ? '<span class="ok">✔</span> ' + t : '<span class="no">✘</span> ' + f) + '</div>';
    $('#mgStatus .body').innerHTML =
      ok(mg.provider === 'climount' || mg.provider === 'builtin', 'Usenet backend: ' + esc(mg.provider), 'Usenet backend is ' + esc(mg.provider) + ' (migration is from cli_mount)') +
      ok(mg.metas > 0, (mg.listed != null
          ? mg.listed + ' job(s) listed by cli_mount; ' + mg.metas + ' job maps in ' + esc(mg.meta_dir) +
            (mg.metas > mg.listed ? ' (maps of jobs cli_mount has since removed are left out)' : '')
          : mg.metas + " cli_mount job maps found in " + esc(mg.meta_dir) + " (cli_mount didn't answer: Import imports only the jobs it lists)"),
        "cli_mount's metadata isn't visible at " + esc(mg.meta_dir) + ': bind cli_mount\'s appdata into this container and set Settings → Usenet Mount → cli_mount Data Path') +
      ok(!!mg.climount_root, "cli_mount's mount: " + esc(mg.climount_root) + (mg.climount_root_mounted ? ' (mounted)' : ' (not mounted)'), "cli_mount's mount path isn't known (File Management → original files path)") +
      ok(mg.engine_state === 'RUNNING', 'Built-in mount running at ' + esc(mg.builtin_root), 'Built-in mount is ' + esc(mg.engine_state) + (mg.problems.length ? ': ' + esc(mg.problems.join('; ')) : '')) +
      (mg.debrid_needed ? ok(!!(mg.debrid.synced_at && mg.debrid.complete),
        'Debrid account(s) mirrored on the built-in mount' + (mg.debrid.held_removals ? ' (' + mg.debrid.held_removals + ' removal(s) on hold, see the log)' : ''),
        mg.debrid.synced_at ? 'Debrid: ' + (mg.debrid.pending || 0) + ' torrent(s) still being added' + (mg.debrid.errors ? ' (' + mg.debrid.errors + ' error(s) in the last pass)' : '')
                            : 'Debrid: not listed yet (starts once the built-in mount runs alongside)') : '') +
      (mg.program_running ? '<div style="color:#8f8f8f">The program is running: stop it before switching back.</div>' : '');
    const sp = mg.space, gb = n => (n / 1073741824).toFixed(1) + ' GB';
    $('#mgSpace').innerHTML = !sp ? '' : (sp.fits
      ? '<span class="ok">✔</span> Importing all ' + sp.jobs + ' jobs needs about ' + gb(sp.needed) + '; ' + gb(sp.room) + ' can be used on the engine\'s disk.'
      : '<span class="mid">⚠</span> Importing all ' + sp.jobs + ' jobs needs about ' + gb(sp.needed) + ', but only ' + gb(sp.room) +
        ' can be used on the engine\'s disk (' + gb(sp.free) + ' free, ' + gb(sp.margin) + ' kept free' +
        (sp.cache_reserved ? ', ' + gb(sp.cache_reserved) + ' for the disk cache' : '') + '). About ' + sp.fits_jobs + ' fit: Import stops before the disk fills.');
    $('#mgAlong').textContent = mg.alongside ? 'Stop running alongside' : 'Start alongside';
    $('#mgAlong').disabled = mg.provider === 'builtin';
    $('#mgBack').disabled = mg.provider !== 'builtin';
    $('#mgExport').disabled = mg.provider === 'builtin' || mg.engine_state !== 'RUNNING';
  }
  $('#mgAlong').onclick = async () => {
    $('#mgAlongMsg').textContent = 'Applying…';
    await fetch('/mount/api/migrate/alongside', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({on: !(mg && mg.alongside)})});
    setTimeout(() => { loadMigrate(); $('#mgAlongMsg').textContent = ''; }, 3000);
  };
  async function pollImport() {
    const r = await fetch('/mount/api/migrate/job/' + mgJob);
    const j = await r.json();
    const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
    $('#mgProgress').innerHTML = '<div class="bar" style="height:8px"><i style="width:' + pct + '%"></i></div><div style="font-size:13px;margin-top:6px">' +
      esc(j.status) + ' — ' + (j.done || 0) + ' / ' + (j.total || 0) + ' · imported ' + (j.imported || 0) + ' · already there ' + (j.already || 0) +
      ' · failed ' + (j.failed || 0) + (j.error ? ' · <span class="no">' + esc(j.error) + '</span>' : '') + '</div>';
    $('#mgLog').hidden = !(j.log || []).length;
    $('#mgLog').textContent = (j.log || []).join('\n');
    if (j.complete) { clearInterval(mgTimer); mgTimer = null; $('#mgCancel').hidden = true; $('#mgImport').disabled = false; }
  }
  $('#mgImport').onclick = async () => {
    $('#mgImport').disabled = true;
    const limit = Math.max(0, parseInt($('#mgLimit').value, 10) || 0);
    const r = await fetch('/mount/api/migrate/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({limit})});
    const d = await r.json();
    if (!d.success) { $('#mgProgress').innerHTML = '<span class="no">' + esc(d.error || 'Import failed to start') + '</span>'; $('#mgImport').disabled = false; return; }
    mgJob = d.job; $('#mgCancel').hidden = false;
    pollImport(); mgTimer = setInterval(pollImport, 1500);
  };
  $('#mgCancel').onclick = () => mgJob && fetch('/mount/api/migrate/job/' + mgJob + '/cancel', {method: 'POST'});
  $('#mgCompare').onclick = async () => {
    const b = $('#mgCompare'); b.disabled = true; b.textContent = 'Comparing…';
    const r = await fetch('/mount/api/migrate/compare', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({sample: 10})});
    const d = await r.json();
    b.disabled = false; b.textContent = 'Compare 10 files';
    $('#mgCmp tbody').innerHTML = d.success ? '<tr><td colspan="3"><strong>' + d.identical + ' of ' + d.checked + ' identical</strong></td></tr>' +
      d.rows.map(x => '<tr><td>' + (x.ok ? '<span class="ok">✔</span>' : '<span class="no">✘</span>') + '</td><td class="nm">' + esc(x.folder) + '<div style="color:#888;font-size:12px">' + esc(x.file) + '</div></td><td>' + esc(x.detail) + (x.seconds != null ? ' (' + x.seconds + ' s)' : '') + '</td></tr>').join('')
      : '<tr><td class="no">' + esc(d.error) + '</td></tr>';
  };
  $('#mgSwitch').onclick = async () => {
    if (!confirm("Switch to the built-in mount? Make sure the cli_mount container is stopped first. You can switch back here at any time.")) return;
    const r = await fetch('/mount/api/migrate/switch', {method: 'POST'});
    const d = await r.json();
    $('#mgSwitchMsg').innerHTML = d.success ? '<span class="ok">Switched: the built-in mount now serves ' + esc(d.mount_path) + '.</span>' : '<span class="no">' + esc(d.error) + '</span>';
    setTimeout(loadMigrate, 3000);
  };
  $('#mgBack').onclick = async () => {
    if (!confirm('Switch back to cli_mount? The built-in mount moves back to its own path; then start the cli_mount container and press Copy back.')) return;
    const r = await fetch('/mount/api/migrate/rollback', {method: 'POST'});
    const d = await r.json();
    $('#mgBackMsg').innerHTML = d.success ? '<span class="ok">' + esc(d.message) + '</span>' : '<span class="no">' + esc(d.error) + '</span>';
    setTimeout(loadMigrate, 3000);
  };
  let exJob = null, exTimer = null;
  async function pollExport() {
    const r = await fetch('/mount/api/migrate/job/' + exJob);
    const j = await r.json();
    const pct = j.total ? Math.round(100 * j.done / j.total) : (j.complete ? 100 : 0);
    $('#mgExportProgress').innerHTML = '<div class="bar" style="height:8px"><i style="width:' + pct + '%"></i></div><div style="font-size:13px;margin-top:6px">' +
      esc(j.status) + ' — ' + (j.done || 0) + ' / ' + (j.total || 0) + ' · copied back ' + (j.imported || 0) + ' · already in cli_mount ' + (j.already || 0) +
      ' · failed ' + (j.failed || 0) + (j.error ? ' · <span class="no">' + esc(j.error) + '</span>' : '') + '</div>';
    $('#mgExportLog').hidden = !(j.log || []).length;
    $('#mgExportLog').textContent = (j.log || []).join('\n');
    if (j.complete) { clearInterval(exTimer); exTimer = null; $('#mgExportCancel').hidden = true; $('#mgExport').disabled = false; }
  }
  $('#mgExport').onclick = async () => {
    $('#mgExport').disabled = true;
    const r = await fetch('/mount/api/migrate/export', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    const d = await r.json();
    if (!d.success) { $('#mgBackMsg').innerHTML = '<span class="no">' + esc(d.error) + '</span>'; $('#mgExport').disabled = false; return; }
    exJob = d.job; $('#mgExportCancel').hidden = false;
    pollExport(); exTimer = setInterval(pollExport, 1500);
  };
  $('#mgExportCancel').onclick = () => exJob && fetch('/mount/api/migrate/job/' + exJob + '/cancel', {method: 'POST'});

  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 4000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  setInterval(() => { if (tab === 'queue' && !document.hidden) loadJobs(); }, 10000);
  const start = (location.hash || '').slice(1);
  if (['queue', 'browse', 'settings', 'migrate'].includes(start)) showTab(start);
})();

// Settings tab: each card folds like the main Settings page (closed at
// first), with Collapse All / Expand All. The open set is remembered per
// browser.
(function () {
  const pane = document.querySelector('.pane[data-pane="settings"]');
  if (!pane) return;
  const KEY = 'mountSettingsOpen';
  const cards = Array.from(pane.querySelectorAll('.card.fold'));
  const title = c => (c.querySelector('h3') || {}).textContent || '';
  let saved = [];
  try { saved = JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (e) { saved = []; }
  const save = () => {
    try { localStorage.setItem(KEY, JSON.stringify(cards.filter(c => c.classList.contains('open')).map(title))); } catch (e) { /* private mode */ }
  };
  cards.forEach(c => {
    if (saved.indexOf(title(c)) !== -1) c.classList.add('open');
    c.querySelector('h3').addEventListener('click', () => { c.classList.toggle('open'); save(); });
  });
  const all = open => () => { cards.forEach(c => c.classList.toggle('open', open)); save(); };
  const ex = document.getElementById('expandAll'), co = document.getElementById('collapseAll');
  if (ex) ex.addEventListener('click', all(true));
  if (co) co.addEventListener('click', all(false));
})();
