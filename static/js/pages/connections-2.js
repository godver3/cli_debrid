/* Moved from templates/connections.html. */
(function() {
  // Set hook immediately — runs before DOMContentLoaded so classic resolveCard can call it
  window._tngOnResolve = function() {
    if (typeof syncFromClassic === 'function') syncFromClassic();
  };

  const SECTION_LABELS = {
    system:  'System Connections',
    scraper: 'Scraper Connections',
    source:  'Content Source Connections',
  };

  const TYPE_MAP = {
    system:  { cli_battery:'Internal', plex:'Media Server', jellyfin:'Media Server', 'mounted-files':'File System', 'phalanx-db':'Database' },
  };

  function getType(group, service, name) {
    if (group === 'system') return TYPE_MAP.system[service] || 'Internal';
    if (group === 'scraper') {
      if (/jackett/i.test(name)) return 'Scraper';
      if (/zilean/i.test(name)) return 'Scraper';
      if (/torrentio/i.test(name)) return 'Torrent';
      if (/nyaa/i.test(name)) return 'Torrent';
      if (/aio/i.test(name)) return 'Stream API';
      if (/mediafusion/i.test(name)) return 'Scraper';
      return 'Scraper';
    }
    if (/trakt/i.test(name)) return 'Trakt';
    if (/mdblist/i.test(name)) return 'MDBList';
    if (/plex/i.test(name)) return 'Plex';
    if (/overseerr/i.test(name)) return 'Overseerr';
    if (/adaptive/i.test(name)) return 'Adaptive';
    return 'Source';
  }

  // card registry: service -> { name, group, status, ts, el }
  const registry = {};

  let tngFilter = 'all';
  let tngQ = '';

  function allCards() { return Object.values(registry); }

  const SCRAPER_SVG = `<svg viewBox="-0.56 0 171.265 171.265" style="width:16px;height:16px;fill:currentColor" xmlns="http://www.w3.org/2000/svg"><g transform="translate(-421.662 -997.216)"><path d="M567.7,1015.657a4,4,0,1,0,5.656,0A4,4,0,0,0,567.7,1015.657Z"/><path d="M591.8,1017.423a19.372,19.372,0,0,0-5.711-13.787l-.707-.709a19.5,19.5,0,0,0-27.578,0l-40.61,40.61c-25.274-2.8-61.3,14.466-92.591,44.575l-2.94,2.828,77.541,77.54,2.827-2.939c30.688-31.9,48.212-69.5,44.308-94.58l39.75-39.75A19.373,19.373,0,0,0,591.8,1017.423Zm-52.871,60.949c.113,21.706-14.987,51.8-39.863,78.658l-65.951-65.951c25.689-23.775,54.811-38.8,76.333-39.794l-7.859,7.859,28.285,28.283Zm41.5-52.817-50.558,50.56L512.9,1059.144l50.559-50.559a11.5,11.5,0,0,1,16.262,0l.708.709a11.5,11.5,0,0,1,0,16.263Z"/></g></svg>`;

  const SOURCE_SVG = `<svg viewBox="0 0 256 256" style="width:16px;height:16px;fill:currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M25.801,82.108l0,-1.152c0,-6.666 4.033,-13.058 11.21,-17.772c7.178,-4.713 16.913,-7.361 27.064,-7.361c36.543,0 91.307,0 127.85,0c10.151,0 19.886,2.648 27.064,7.361c7.177,4.714 11.21,11.106 11.21,17.772l0,1.152c-1.449,-0.11 -2.914,-0.166 -4.387,-0.166c-51.829,0 -143.795,0 -195.624,0c-1.473,0 -2.938,0.056 -4.387,0.166Z" style="fill:currentColor;fill-opacity:0.29"/><path d="M51.25,57.276l0,-1.107c0,-6.666 4.032,-13.058 11.21,-17.772c7.178,-4.713 16.913,-7.361 27.064,-7.361c23.489,0 53.463,0 76.952,0c10.151,0 19.886,2.648 27.064,7.361c7.178,4.714 11.21,11.106 11.21,17.772l0,1.107c-4.079,-0.953 -8.413,-1.453 -12.825,-1.453c-36.543,0 -91.307,0 -127.85,0c-4.412,0 -8.746,0.5 -12.825,1.453Z" style="fill:currentColor;fill-opacity:0.71"/><path d="M45.532,52.632c1.827,-11.217 12.562,-20.01 22.832,-23.878c6.643,-2.503 13.801,-3.674 20.898,-3.717c25.825,-0.053 51.651,-0.053 77.476,0c18.406,0.112 38.802,7.932 43.295,25.475c0.171,0.668 0.311,1.344 0.421,2.025c11.89,3.635 22.02,11.021 25.027,22.762c0.441,1.719 0.676,3.489 0.713,5.263c10.836,6.161 18.705,17.784 18.83,31.098c0.085,27.842 0.085,55.685 0,83.528c-0.175,18.632 -16.365,35.595 -35.776,35.775c-60.832,0.193 -121.664,0.193 -182.496,0c-18.554,-0.172 -35.593,-16.135 -35.776,-35.776c-0.086,-27.842 -0.086,-55.685 0,-83.527c0.119,-12.759 7.71,-24.807 18.83,-31.144c0.018,-1.002 0.05,-1.253 0.116,-1.892c1.225,-11.791 12.359,-21.078 22.993,-25.083c0.864,-0.326 1.736,-0.629 2.617,-0.909Zm174.903,35.298c-61.602,-0.138 -123.204,-0.138 -184.806,0c-11.896,0.774 -22.535,11.14 -22.653,23.804c-0.086,27.843 -0.258,55.687 0.001,83.528c0.19,12.251 10.729,23.499 23.7,23.7c60.879,0.564 121.767,0.564 182.646,0c12.325,-0.191 23.499,-10.765 23.7,-23.7c0.262,-27.891 0.262,-55.786 0,-83.677c-0.185,-11.942 -10.167,-22.853 -22.588,-23.655Zm-28.166,-26.106c-42.728,-0.151 -85.455,-0.001 -128.183,-0.001c-3.869,0.014 -7.723,0.435 -11.493,1.301c0,0 -0.209,0.037 -0.536,0.068c-4.828,1.165 -9.409,3.134 -13.343,6.133c-2.277,1.735 -4.313,3.873 -5.617,6.436l-0.09,0.181l2.048,0c0.562,-0.033 1.128,-0.053 1.697,-0.058c60.832,-0.189 121.664,-0.189 182.496,0c0.558,0.005 1.113,0.025 1.668,0.058l2.077,0c-3.316,-6.787 -10.682,-10.888 -19.107,-12.807c-0.387,-0.025 -0.782,-0.081 -1.182,-0.17c-1.533,-0.328 -1.678,-0.34 -2.429,-0.464c-2.635,-0.435 -5.301,-0.648 -7.969,-0.676l-0.037,-0.001Zm4.385,-11.823c0.172,0.011 0.272,0.017 0.272,0.017c-5.547,-9.188 -18.305,-12.908 -30.237,-12.981c-25.864,-0.053 -51.728,-0.157 -77.592,0.001c-11.66,0.119 -24.363,3.769 -29.986,12.918c0,0 1.857,-0.08 3.608,-0.117c0.171,-0.003 0.342,-0.007 0.512,-0.009l0.017,0l0.17,-0.003l0.062,-0.001l0.006,0l0.133,-0.001l0.116,-0.001l0.078,0l0.292,-0.001c17.877,-0.062 35.754,-0.072 53.631,-0.063c24.726,-0.008 49.452,0.013 74.177,0.063l0.274,0.001c0.13,0 0.26,0.002 0.391,0.003l0.258,0.003c0.112,0.001 0.227,0.004 0.345,0.007c1.152,0.025 2.312,0.079 3.473,0.164Z" style="fill:currentColor;fill-rule:nonzero"/><path d="M99.68,103.306c0.896,0.113 1.753,0.387 2.559,0.829l74.651,44.677c1.253,0.817 1.505,1.19 1.96,1.896c1.571,2.435 1.05,5.996 -1.213,7.872c-0.314,0.261 -0.406,0.307 -0.747,0.529l-74.651,44.678c-1.294,0.709 -1.734,0.756 -2.559,0.828c-2.987,0.261 -5.992,-2.034 -6.454,-5.076c-0.061,-0.398 -0.053,-0.5 -0.068,-0.901l0,-89.354c0.052,-1.389 0.187,-1.617 0.421,-2.208c0.937,-2.368 3.413,-3.903 6.101,-3.77Zm5.478,84.749l56.967,-34.094l-56.967,-34.094c0,22.729 0,45.459 0,68.188Z" style="fill:currentColor;fill-rule:nonzero"/></svg>`;

  function buildTngCard(entry) {
    const { name, group, status, ts, expiryDate, autoRenew } = entry;
    const checked = ts ? timeAgoTng(ts) : 'Checking…';
    const type = getType(group, entry.service, name);
    const icon = getTngIcon(name);
    const dotCls = status || 'checking';
    const statusLabel = status === 'online' ? 'Online' : status === 'offline' ? 'Offline' : 'Checking';
    const iconHtml = group === 'source'
      ? `<div class="conn-icon">${SOURCE_SVG}</div>`
      : group === 'scraper'
      ? `<div class="conn-icon">${SCRAPER_SVG}</div>`
      : `<div class="conn-icon"><i class="${icon}"></i></div>`;
    return `<div class="tng-card ${dotCls}" title="${name}">
      <div class="conn-top">
        ${iconHtml}
        <div class="conn-info">
          <div class="conn-name">${name}</div>
          <div class="conn-type">${type}</div>
        </div>
        <div class="conn-status">
          <span class="status-dot ${dotCls}"></span>
          <span class="status-text ${dotCls}">${statusLabel}</span>
        </div>
      </div>
      <div class="conn-divider"></div>
      <div class="conn-meta">
        ${expiryDate ? `<div class="conn-checked conn-expiry"><i class="fa-regular fa-calendar"></i><div class="conn-expiry-lines"><span>Subscription Expires:${autoRenew ? ' (auto-renews)' : ''}</span><span class="conn-expiry-date">${expiryDate}</span></div></div>` : ''}
        <div class="conn-checked"><i class="fa-regular fa-clock"></i> Last checked: ${checked}</div>
      </div>
    </div>`;
  }

  function getTngIcon(name) {
    const n = name.toLowerCase();
    if (n.includes('battery')) return 'fa-solid fa-battery-full';
    if (n.includes('plex'))    return 'fa-solid fa-play';
    if (n.includes('jellyfin') || n.includes('emby')) return 'fa-solid fa-play';
    if (n.includes('mount'))   return 'fa-solid fa-hard-drive';
    if (n.includes('phalanx') || n.includes('db')) return 'fa-solid fa-database';
    if (n.includes('jackett')) return 'fa-solid fa-magnifying-glass';
    if (n.includes('zilean'))  return 'fa-solid fa-clock';
    if (n.includes('aio'))     return 'fa-solid fa-rss';
    if (n.includes('trakt'))   return 'fa-solid fa-tv';
    if (n.includes('mdblist')) return 'fa-solid fa-list';
    if (n.includes('torrentio') || n.includes('nyaa')) return 'fa-solid fa-magnet';
    if (n.includes('overseerr')) return 'fa-solid fa-film';
    return 'fa-solid fa-plug';
  }

  function timeAgoTng(ts) {
    const secs = Math.round((Date.now() - ts) / 1000);
    if (secs < 5) return 'just now';
    if (secs < 60) return `${secs}s ago`;
    if (secs < 3600) return `${Math.floor(secs/60)}m ago`;
    return `${Math.floor(secs/3600)}h ${Math.floor((secs%3600)/60)}m ago`;
  }

  function tngRender() {
    const filtered = allCards().filter(e => {
      if (tngFilter === 'online' && e.status !== 'online') return false;
      if (tngFilter === 'offline' && e.status !== 'offline') return false;
      if (tngQ && !e.name.toLowerCase().includes(tngQ)) return false;
      return true;
    });

    const total = allCards().length;
    document.getElementById('tng-result-info').textContent =
      filtered.length < total ? `${filtered.length} of ${total} shown` : `${total} connections`;

    const area = document.getElementById('tng-sections-area');
    if (!filtered.length) {
      area.innerHTML = '<div class="tng-empty"><i class="fa-solid fa-plug" style="font-size:28px;opacity:.2;display:block;margin:0 auto 10px"></i>No connections match</div>';
      return;
    }

    if (tngFilter !== 'all' || tngQ) {
      area.innerHTML = `<div class="conn-grid">${filtered.map(buildTngCard).join('')}</div>`;
      return;
    }

    const groups = { system: [], scraper: [], source: [] };
    allCards().forEach(e => { if (groups[e.group]) groups[e.group].push(e); });

    let html = '';
    for (const [group, entries] of Object.entries(groups)) {
      if (!entries.length) continue;
      const online = entries.filter(e => e.status === 'online').length;
      const hasIssue = entries.some(e => e.status === 'offline');
      const cls = hasIssue ? 'section-warn' : 'section-ok';
      html += `<div class="tng-section ${cls}">
        <div class="section-hdr">
          <div class="section-title">${SECTION_LABELS[group]}<span class="section-count">${online}/${entries.length}</span></div>
        </div>
        <div class="conn-grid">${entries.map(buildTngCard).join('')}</div>
      </div>`;
    }
    area.innerHTML = html;

    tngRenderIssues();
  }

  function tngRenderIssues() {
    const offline = allCards().filter(e => e.status === 'offline');
    const area = document.getElementById('tng-issues-area');
    if (!offline.length) { area.innerHTML = ''; return; }
    area.innerHTML = `<div class="issues-section">
      <div class="issues-hdr">
        <div class="issues-hdr-title">
          <i class="fa-solid fa-triangle-exclamation"></i> Connection Issues
          <span style="background:#7f1d1d;color:#fca5a5;border-radius:10px;padding:1px 7px;font-size:10px;font-family:monospace">${offline.length}</span>
        </div>
      </div>
      <div>${offline.map(e => `<div class="issue-row">
        <div class="issue-bar"></div>
        <div class="issue-info">
          <div class="issue-name">${e.name}</div>
          <div class="issue-status"><i class="fa-solid fa-circle-xmark" style="color:var(--red);font-size:10px"></i> Status: Disconnected</div>
        </div>
      </div>`).join('')}</div>
    </div>`;
  }

  function tngRenderStats() {
    if (!document.getElementById('tng-stats-row')) return;
    const all = allCards();
    const online = all.filter(e => e.status === 'online').length;
    const offline = all.filter(e => e.status === 'offline').length;
    document.getElementById('tng-stats-row').innerHTML = `
      <div class="stat"><div class="stat-icon all"><i class="fa-solid fa-plug"></i></div><div><div class="stat-val">${all.length}</div><div class="stat-lbl">Total</div></div></div>
      <div class="stat"><div class="stat-icon ok"><i class="fa-solid fa-circle-check"></i></div><div><div class="stat-val" style="color:var(--green)">${online}</div><div class="stat-lbl">Online</div></div></div>
      <div class="stat"><div class="stat-icon fail"><i class="fa-solid fa-circle-xmark"></i></div><div><div class="stat-val" style="color:${offline>0?'var(--red)':'var(--text3)'}">${offline}</div><div class="stat-lbl">Offline</div></div></div>`;
  }

  // Mirror classic cards into registry
  function syncFromClassic() {
    // system
    document.querySelectorAll('#system-cards .connection-card').forEach(card => {
      const service = card.dataset.service;
      if (!service || card.style.display === 'none') return;
      const name = card.querySelector('.connection-title')?.textContent.trim() || service;
      const ind = card.querySelector('.status-indicator');
      const connected = ind?.classList.contains('connected');
      const disconnected = ind?.classList.contains('disconnected');
      const status = connected ? 'online' : disconnected ? 'offline' : 'checking';
      const footer = card.querySelector('.conn-card-footer');
      const ts = footer?.dataset.ts ? parseInt(footer.dataset.ts) : null;
      registry[service] = { service, name, group: 'system', status, ts };
    });
    // scraper
    document.querySelectorAll('#scraper-cards .connection-card').forEach(card => {
      const service = card.dataset.service;
      if (!service) return;
      const name = card.querySelector('.connection-title')?.textContent.trim() || service;
      const ind = card.querySelector('.status-indicator');
      const connected = ind?.classList.contains('connected');
      const disconnected = ind?.classList.contains('disconnected');
      const status = connected ? 'online' : disconnected ? 'offline' : 'checking';
      const footer = card.querySelector('.conn-card-footer');
      const ts = footer?.dataset.ts ? parseInt(footer.dataset.ts) : null;
      const expiryEl = card.querySelector('.conn-card-expiry');
      const expiryDate = expiryEl?.dataset.expiryDate || null;
      const autoRenew = expiryEl?.dataset.autoRenew === 'true';
      registry[service] = { service, name, group: 'scraper', status, ts, expiryDate, autoRenew };
    });
    // source
    document.querySelectorAll('#source-cards .connection-card').forEach(card => {
      const service = card.dataset.service;
      if (!service) return;
      const name = card.querySelector('.connection-title')?.textContent.trim() || service;
      const ind = card.querySelector('.status-indicator');
      const connected = ind?.classList.contains('connected');
      const disconnected = ind?.classList.contains('disconnected');
      const status = connected ? 'online' : disconnected ? 'offline' : 'checking';
      const footer = card.querySelector('.conn-card-footer');
      const ts = footer?.dataset.ts ? parseInt(footer.dataset.ts) : null;
      registry[service] = { service, name, group: 'source', status, ts };
    });
    tngRenderStats();
    tngRender();
  }

  window.tngSetFilter = function(btn, val) {
    tngFilter = val;
    document.querySelectorAll('#tng-connections .ftab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    tngRender();
  };

  window.tngSearch = function(val) {
    tngQ = val.trim().toLowerCase();
    tngRender();
  };

  document.addEventListener('DOMContentLoaded', function() {
    if (!document.getElementById('tng-connections')) return;

    window._tngOnResolve = syncFromClassic;

    document.getElementById('tng-refresh-all')?.addEventListener('click', () => {
      document.getElementById('conn-refresh-all')?.click();
    });

    syncFromClassic();

    // Poll rapidly while connections are being checked, then slow down
    let polls = 0;
    const fastPoll = setInterval(() => {
      syncFromClassic();
      polls++;
      if (polls >= 120) clearInterval(fastPoll);
    }, 500);

    setInterval(syncFromClassic, 30000);
  });
})();
