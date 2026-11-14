(function() {
  /*
   * Card layout for the Tools, Maintenance and Diagnostics pages.
   *
   * Each page renders its .debug_item cards (templates/tool_cards/*.html) in a hidden
   * holder; this moves (not clones) them into #tdbMainGrid as styled cards, so the
   * listeners in tool_cards_actions.js stay attached. data-cat on a card picks its tab.
   */


  /*
   * Button map: text (lowercased, trimmed) → { cls, icon }
   * Matches the redesign file exactly.
   */
  const BTN_MAP = {
    'delete':                      { cls:'btn-outline-red',     icon:'fa-solid fa-trash' },
    'delete battery db files':     { cls:'btn-outline-red',     icon:'fa-solid fa-database' },
    'reset show cache':            { cls:'btn-outline-orange',  icon:'fa-solid fa-rotate' },
    'delete database':             { cls:'btn-outline-red',     icon:'fa-solid fa-bomb' },
    'run task':                    { cls:'btn-orange',          icon:'fa-solid fa-play' },
    'test lookup':                 { cls:'btn-outline-blue',    icon:'fa-solid fa-magnifying-glass' },
    'scan for old databases':      { cls:'btn-outline-orange',  icon:'fa-solid fa-magnifying-glass' },
    'load available backups':      { cls:'btn-outline-orange',  icon:'fa-solid fa-folder-open' },
    'run cleanup':                 { cls:'btn-orange',          icon:'fa-solid fa-wrench' },
    'backup now':                  { cls:'btn-orange',          icon:'fa-solid fa-floppy-disk' },
    'load available backups':      { cls:'btn-outline-orange',  icon:'fa-solid fa-folder-open' },
    'restore selected':            { cls:'btn-outline-orange',  icon:'fa-solid fa-rotate-left' },
    'scan for old backups':        { cls:'btn-outline-orange',  icon:'fa-solid fa-magnifying-glass' },
    'dry run (preview)':           { cls:'btn-outline-orange',  icon:'fa-solid fa-eye' },
    'delete selected':             { cls:'btn-outline-red',     icon:'fa-solid fa-trash' },
    'fix episode numbers':         { cls:'btn-orange',          icon:'fa-solid fa-list-ol' },
    'get plex collected':          { cls:'btn-orange',          icon:'' },
    'run':                         { cls:'btn-orange',          icon:'' },
    'get wanted':                  { cls:'btn-orange',          icon:'' },
    'propagate version':           { cls:'btn-orange',          icon:'' },
    'apply action':                { cls:'btn-orange',          icon:'' },
    'delete selected cache files': { cls:'btn-outline-red',     icon:'fa-solid fa-trash-can' },
    'refresh release dates':       { cls:'btn-orange',          icon:'fa-solid fa-calendar-days' },
    'run emby/jellyfin scan':      { cls:'btn-orange',          icon:'' },
    'move to upgrading':           { cls:'btn-outline-orange',  icon:'' },
    'manage manual blacklist':     { cls:'btn-outline-red',     icon:'' },
    'add to blacklist':            { cls:'btn-outline-red',     icon:'' },
    'search':                      { cls:'btn-orange',          icon:'fa-solid fa-magnifying-glass' },
    'preview':                     { cls:'btn-outline-orange',  icon:'fa-solid fa-eye' },
    'apply labels':                { cls:'btn-orange',          icon:'' },
    'remove label':                { cls:'btn-outline-red',     icon:'' },
    'find orphaned labels':        { cls:'btn-outline-orange',  icon:'fa-solid fa-magnifying-glass' },
    'cleanup':                     { cls:'btn-outline-red',     icon:'' },
    'sync all labels':             { cls:'btn-outline-orange',  icon:'fa-solid fa-rotate' },
    'backfill resolution':         { cls:'btn-outline-orange',  icon:'' },
    'refresh queue':               { cls:'btn-outline-orange',  icon:'fa-solid fa-rotate' },
    'verification scan':           { cls:'btn-orange',          icon:'' },
    'convert to symlinks':         { cls:'btn-outline-red',     icon:'fa-solid fa-link' },
    'recover symlink library':     { cls:'btn-orange',          icon:'' },
    'recover riven library':       { cls:'btn-orange',          icon:'' },
    'fix zurg symlinks':           { cls:'btn-outline-orange',  icon:'' },
    'update base paths':           { cls:'btn-outline-orange',  icon:'' },
    'resync symlinks':             { cls:'btn-outline-orange',  icon:'fa-solid fa-rotate' },
    'refresh rate limits':         { cls:'btn-outline-orange',  icon:'fa-solid fa-rotate' },
    'refresh tokens':              { cls:'btn-orange',          icon:'fa-solid fa-rotate' },
    'refresh status':              { cls:'btn-outline-orange',  icon:'fa-solid fa-rotate' },
    'view torrent history':        { cls:'btn-outline-blue',    icon:'fa-solid fa-clock-rotate-left' },
    'download logs':               { cls:'btn-outline-blue',    icon:'fa-solid fa-download' },
    'send test notification':      { cls:'btn-outline-orange',  icon:'fa-solid fa-bell' },
    'select all':                  { cls:'btn-outline-orange btn-sm', icon:'' },
    'unselect all':                { cls:'btn-outline-orange btn-sm', icon:'' },
  };

  /* Apply redesign classes to a live element in-place (no cloning). */
  function restyleItem(item) {
    item.querySelectorAll('.form-group').forEach(n => {
      n.className = 'field';
      const lbl = n.querySelector(':scope > label');
      if (lbl && !lbl.querySelector('input[type=checkbox]')) lbl.className = 'field-label';
    });
    item.querySelectorAll('input[type=text],input[type=number],input[type=password],input[type=email]').forEach(n => {
      n.className = 'finput';
    });
    item.querySelectorAll('select').forEach(n => {
      n.className = n.multiple ? 'fselect fselect-multi' : 'fselect';
    });
    item.querySelectorAll('label').forEach(n => {
      if (n.querySelector('input[type=checkbox]')) n.className = 'check-row';
    });
    item.querySelectorAll('button, a.btn').forEach(n => {
      if (n.classList.contains('episode-modal-close') || n.classList.contains('plex-labels-help-btn')) return;

      const txt = n.textContent.trim().toLowerCase();
      /* A bare "Refresh" next to a field reloads its options: small and secondary. */
      if (/^\W*refresh$/.test(txt)) { n.className = 'btn btn-outline-orange btn-sm'; return; }
      const map = BTN_MAP[txt];

      if (map) {
        n.className = 'btn ' + map.cls;
        if (map.icon && !n.querySelector('i')) {
          const ic = document.createElement('i');
          ic.className = map.icon;
          n.prepend(ic);
        }
      } else {
        const c = n.className || '';
        if (/btn-danger/.test(c))                     n.className = 'btn btn-outline-red';
        else if (/btn-warning/.test(c))               n.className = 'btn btn-outline-orange';
        else if (/btn-info|btn-primary/.test(c))      n.className = 'btn btn-outline-blue';
        else if (/btn-secondary|btn-outline/.test(c)) n.className = 'btn btn-outline-orange';
        else                                           n.className = 'btn btn-orange';
      }
    });

    /* ── Bulk Queue Actions: queue-items-container inner layout ── */
    const qContainer = item.querySelector('#queue_items_container.queue-items-container');
    if (qContainer) {
      /* Restyle the h4 header */
      const qh4 = qContainer.querySelector('h4');
      if (qh4) { qh4.style.cssText = 'font-size:12px;font-weight:500;color:var(--text2);margin-bottom:8px'; }
      /* Select-all buttons get flex:1 */
      const selCont = qContainer.querySelector('.select-all-container');
      if (selCont) {
        selCont.style.cssText = 'display:flex;gap:6px;margin-bottom:8px';
        selCont.querySelectorAll('button').forEach(b => {
          b.className = 'btn btn-outline-orange btn-sm';
          b.style.flex = '1';
        });
      }
    }

    /* ── Manage Cache Files: redesign layout ── */
    const cacheForm = item.querySelector('#manage-cache-files-form');
    const cacheFilesContainer = item.querySelector('#cache_files_container.cache-files-container');
    if (cacheFilesContainer && cacheForm) {
      /* Pull h4 and select-all OUT of the container, place them above it on the form */
      const cfh4 = cacheFilesContainer.querySelector('h4');
      const cfSelCont = cacheFilesContainer.querySelector('.select-all-container');

      if (cfh4) {
        cfh4.className = 'tng-cache-header';
        cacheForm.insertBefore(cfh4, cacheFilesContainer);
      }
      if (cfSelCont) {
        cfSelCont.className = 'tng-cache-selall';
        cfSelCont.querySelectorAll('button').forEach(b => {
          b.className = 'btn btn-outline-orange';
        });
        cacheForm.insertBefore(cfSelCont, cacheFilesContainer);
      }

      /* Turn container into just the check-list box */
      cacheFilesContainer.className = 'check-list';
      cacheFilesContainer.removeAttribute('style');

      const cfList = cacheFilesContainer.querySelector('#cache_files_list');
      if (cfList) {
        /* Add "Adaptive Lists" label before the first item */
        const firstItem = cfList.querySelector('.cache-file-item');
        if (firstItem) {
          const firstLabel = document.createElement('div');
          firstLabel.className = 'check-section-label';
          firstLabel.textContent = 'Content Sources';
          cfList.insertBefore(firstLabel, firstItem);
        }

        /* hr + next <p> → check-section-label */
        cfList.querySelectorAll('hr').forEach(hr => {
          const next = hr.nextElementSibling;
          const div = document.createElement('div');
          div.className = 'check-section-label';
          if (next && next.tagName === 'P') {
            div.textContent = next.textContent.replace(':', '').trim();
            cfList.insertBefore(div, hr);
            hr.remove();
            next.remove();
          } else {
            div.textContent = 'Other Cache Files';
            cfList.insertBefore(div, hr);
            hr.remove();
          }
        });

        /* cache-file-item → check-row label */
        cfList.querySelectorAll('.cache-file-item').forEach(n => {
          const inp = n.querySelector('input[type=checkbox]');
          const lbl = n.querySelector('label');
          if (inp && lbl) {
            const row = document.createElement('label');
            row.className = 'check-row';
            row.setAttribute('for', inp.id || '');
            if (n.style.opacity) row.style.opacity = n.style.opacity;
            row.appendChild(inp);
            row.appendChild(document.createTextNode(' ' + lbl.textContent.trim()));
            n.replaceWith(row);
          }
        });

        /* Flatten: move cfList children directly into cacheFilesContainer */
        while (cfList.firstChild) cacheFilesContainer.appendChild(cfList.firstChild);
        cfList.remove();
      }
    }

    /* ── Plex Labels Management: CSS-only 2-col grid, all sections kept intact ── */
    const labelsOuterGrid = item.querySelector('[style*="grid-template-columns"][style*="auto-fit"]');
    if (labelsOuterGrid) {
      /* Replace outer grid style with our class — no DOM surgery */
      labelsOuterGrid.className = 'tng-labels-grid';
      labelsOuterGrid.removeAttribute('style');

      const botRow = document.createElement('div');
      botRow.className = 'tng-labels-bot-row';

      /* snapshot before any DOM moves */
      const labelSections = Array.from(labelsOuterGrid.querySelectorAll('.label-items'));
      labelSections.forEach(s => {
        /* restyle buttons in-place — no DOM removal, all onclick handlers preserved */
        s.querySelectorAll('button').forEach(btn => {
          const t = btn.textContent.trim().toLowerCase();
          if (/search/.test(t))             btn.className = 'btn btn-orange';
          else if (/preview/.test(t))       btn.className = 'btn btn-outline-orange';
          else if (/apply labels/.test(t))  btn.className = 'btn btn-orange';
          else if (/remove label/.test(t))  btn.className = 'btn btn-outline-red';
          else if (/find orphaned/.test(t)) btn.className = 'btn btn-outline-orange';
          else if (/cleanup/.test(t))       btn.className = 'btn btn-outline-red';
          else if (/sync all/.test(t))      btn.className = 'btn btn-outline-orange';
          else if (/backfill/.test(t))      btn.className = 'btn btn-outline-orange';
          else                              btn.className = 'btn btn-orange';
          btn.removeAttribute('style');
        });
        /* hide empty result/status divs */
        s.querySelectorAll('div[id$="-preview"],div[id$="-results"],div[id$="-status"]').forEach(d => {
          if (!d.textContent.trim()) d.style.display = 'none';
        });
        /* move bottom 3 sections into botRow */
        const h = (s.querySelector('h4') || {}).textContent || '';
        if (/orphaned|sync|backfill/i.test(h)) {
          s.classList.add('tng-label-bot');
          botRow.appendChild(s);
        }
      });

      if (botRow.children.length > 0) labelsOuterGrid.appendChild(botRow);
    }

    /* ── Fallback select-all-container (for any not caught above) ── */
    item.querySelectorAll('.select-all-container').forEach(n => {
      if (!n.style.cssText) {
        n.style.cssText = 'display:flex;gap:6px';
        n.querySelectorAll('button').forEach(b => { b.className = 'btn btn-outline-orange btn-sm'; });
      }
    });

    /* Strip remaining inline styles — CSS gap handles spacing */
    item.querySelectorAll('[style]').forEach(n => {
      const s = n.getAttribute('style') || '';
      const isHidden = /display\s*:\s*none/.test(s);
      const isProtected =
        n.classList.contains('results-area') || n.classList.contains('progress-area') ||
        n.classList.contains('queue-items-container') || n.classList.contains('cache-files-container') ||
        n.classList.contains('verification-queue-container') || n.classList.contains('select-all-container') ||
        n.classList.contains('tng-cache-selall') ||
        n.id === 'target_queue_container' || n.id === 'rate-limit-content' ||
        n.id === 'bulk-apply-preview' || n.id === 'bulk-remove-preview' ||
        n.id === 'orphaned-labels-results' || n.id === 'sync-labels-status' ||
        n.id === 'backfill-resolution-status' || n.id === 'label-search-results' ||
        n.id === 'queue_items_container' || n.id === 'restoreResults' || n.id === 'cleanupResults' ||
        n.id === 'cleanup-watchlist-result' || n.id === 'reset-battery-show-cache-result' ||
        n.id === 'remove-duplicates-results' || n.id === 'fix-imdb-results' || n.id === 'test-lookup-results' ||
        n.id === 'fix-zurg-results' || n.id === 'modify-symlink-results' || n.id === 'resync-symlinks-results' ||
        n.id === 'rclone-scan-progress' || n.id === 'convert-to-symlinks-progress' ||
        n.id === 'upgrades-progress' || n.id === 'episode-fix-progress' ||
        (n.style.display === 'grid') ||
        (n.tagName === 'BUTTON' && n.style.fontSize === '11px') ||
        n.classList.contains('label-items');
      if (isHidden) { n.style.cssText = 'display:none'; return; }
      if (!isProtected) n.removeAttribute('style');
    });
  }

  /*
   * One look for every card: icon + title + one-line description in the header,
   * fields in the body, actions in one row at the bottom. Keyed by the card's
   * lowercased <h3>. desc is used only when the card has no description of its own.
   */
  const TOOL_META = {
    /* Tools */
    'get collected from library (plex)': { icon:'fa-solid fa-film', desc:'Read what Plex already has and mark it Collected. Backfill only updates file sizes of Collected items.' },
    'version propagator':                { icon:'fa-solid fa-code-branch', desc:'Add a second version to everything that has the first: every item with the original version gets the propagated one too.' },
    'bulk subtitle scan':                { icon:'fa-solid fa-closed-captioning' },
    'direct emby/jellyfin scan':         { icon:'fa-solid fa-server' },
    'move item id to upgrading':         { icon:'fa-solid fa-arrow-up-right-dots', desc:'Send one Collected item back to Upgrading so it looks for a better release.' },
    'fix missing imdb id':               { icon:'fa-solid fa-fingerprint', desc:'Set the IMDb ID on one item, or on every row with the same TMDB ID.' },
    'test plex item lookup':             { icon:'fa-solid fa-magnifying-glass', desc:'Check whether Plex can find a database item, and how it matched.' },
    'symlink verification queue':        { icon:'fa-solid fa-list-check', desc:'Symlinks waiting for your media server to confirm it picked them up.', wide:true },
    'convert library to symlinks':       { icon:'fa-solid fa-link', desc:'One-time conversion of an existing Plex library to symlinks. Not reversible.', danger:true },
    'rclone mount to symlinks':          { icon:'fa-solid fa-folder-tree' },
    'duplicate symlink targets':         { icon:'fa-solid fa-clone', desc:'Find TV episodes whose symlinks point at the same file, usually a mismatched season pack.' , note:true },
    'resync symlinks with new settings': { icon:'fa-solid fa-rotate', desc:'Rebuild the symlinks of Collected items after changing symlink settings or moving files.' , note:true },
    'modify symlink base paths':         { icon:'fa-solid fa-pen-to-square', desc:'Change the base paths stored for symlinks and original files.' },
    'symlink library recovery':          { icon:'fa-solid fa-life-ring', desc:'Rebuild lost database entries from your existing symlink folders.' , note:true },
    'plex labels management':            { icon:'fa-solid fa-tags', wide:true, desc:'View, apply and remove Plex labels, and sync them from your content sources.' },
    'migrate between usenet providers':  { icon:'fa-solid fa-right-left', wide:true },
    'other tool pages':                  { icon:'fa-solid fa-toolbox', wide:true },
    /* Maintenance */
    'restore database from backup':          { icon:'fa-solid fa-clock-rotate-left' },
    'clean up old database files':           { icon:'fa-solid fa-broom' },
    'backup cli_mount databases':            { icon:'fa-solid fa-floppy-disk' },
    'restore cli_mount database from backup':{ icon:'fa-solid fa-clock-rotate-left' },
    'clean up old cli_mount backup files':   { icon:'fa-solid fa-broom' },
    'remove duplicate database items':       { icon:'fa-solid fa-clone', desc:'Keep one database row per file when several rows point at the same file.' , note:true },
    'manage duplicate versions':             { icon:'fa-solid fa-layer-group', desc:'Clean up items collected more than once, by quality, state or version.' },
    'fix episode numbers':                   { icon:'fa-solid fa-list-ol', desc:'Re-read season and episode numbers from file names and fix the wrong ones.' , note:true },
    'manage cache files':                    { icon:'fa-solid fa-box-archive', desc:'Delete cached content-source lists so they are fetched fresh on the next run.' },
    'cli_mount db cleanup':                  { icon:'fa-solid fa-database', desc:'Remove one debrid provider\'s entries from cli_mount\'s databases.' , note:true },
    'backfill nzb file names':               { icon:'fa-solid fa-file-signature', desc:'Rename collected NZB items to the structured NZB naming format.' , note:true },
    'cleanup my plex watchlist and other plex watchlists': { icon:'fa-solid fa-eraser', desc:'Remove every Collected item from your Plex watchlists now.' , note:true },
    'reset battery show status cache':       { icon:'fa-solid fa-battery-half', desc:'Re-fetch every show\'s status (Ended, Canceled...) on the next run.' , note:true },
    'not wanted list':                       { icon:'fa-solid fa-ban' },
    'full cli_mount sync':                   { icon:'fa-solid fa-arrows-rotate' },
    'bulk delete from database':             { icon:'fa-solid fa-trash', desc:'Delete several items from the database by ID.', danger:true },
    'delete battery database files':         { icon:'fa-solid fa-battery-empty', danger:true },
    'delete database':                       { icon:'fa-solid fa-bomb', danger:true },
    /* Diagnostics */
    'current rate limit state':     { icon:'fa-solid fa-gauge-high', desc:'Requests used against each site\'s rate limit.' },
    'plex token status':            { icon:'fa-solid fa-key', desc:'Whether each Plex token cli_debrid holds is still valid.' },
    'personal trakt token status':  { icon:'fa-solid fa-key', desc:'Whether your Trakt sign-in is still valid.' },
    'torrent tracking':             { icon:'fa-solid fa-clock-rotate-left' },
    'api summary':                  { icon:'fa-solid fa-chart-column' },
    'real time api':                { icon:'fa-solid fa-wave-square' },
  };

  /* Buttons placed straight in a form (or the card) become one action row. */
  function groupActions(container) {
    const btns = Array.from(container.children).filter(n => n.matches('button, a.btn'));
    if (!btns.length) return;
    const row = document.createElement('div');
    row.className = 'card-actions';
    container.insertBefore(row, btns[0]);
    btns.forEach(b => row.appendChild(b));
    /* A lone, non-destructive action is the card's primary action. */
    if (btns.length === 1 && /btn-outline-(orange|blue)/.test(btns[0].className)) {
      btns[0].className = btns[0].className.replace(/btn-outline-(orange|blue)/, 'btn-orange');
    }
  }

  function buildCards() {
    const mainGrid = document.getElementById('tdbMainGrid');
    if (!mainGrid) return;

    /* Snapshot the list before we start moving nodes */
    const items = Array.from(document.querySelectorAll('.debug_item'));

    items.forEach(item => {
      const h3 = item.querySelector('h3');
      if (!h3) return;
      const title = h3.textContent.trim();
      const key = title.toLowerCase();
      const meta = TOOL_META[key] || {};
      const cat = item.dataset.cat || 'all';

      const descEl = item.querySelector('p.description');
      let descHtml = meta.desc ? escapeHtml(meta.desc) : '';
      let noteHtml = '';
      if (descEl) {
        /* The card's own description, when a short one replaces it in the header,
           stays in the body only if it says more (note:true). */
        if (!descHtml) descHtml = descEl.innerHTML;
        else if (meta.note) noteHtml = descEl.innerHTML;  /* it adds a warning or a requirement */
        descEl.remove();
      }

      h3.style.display = 'none';

      /* For Plex Labels: extract help button to place in the header */
      const helpBtn = item.querySelector('.plex-labels-help-btn');

      restyleItem(item);
      item.querySelectorAll('form').forEach(groupActions);
      groupActions(item);
      item.querySelectorAll('.verification-actions').forEach(n => n.classList.add('card-actions'));

      const card = document.createElement('div');
      card.className = 'card';
      if (meta.wide) card.classList.add('card-wide');
      if (meta.danger || cat === 'danger') card.classList.add('card-danger');
      card.dataset.cat = cat;
      card.dataset.title = key;

      const head = document.createElement('div');
      head.className = 'card-head';
      head.innerHTML =
        `<div class="card-icon"><i class="${meta.icon || 'fa-solid fa-wrench'}"></i></div>` +
        `<div class="card-head-text"><div class="card-title"></div>` +
        (descHtml ? `<div class="card-desc">${descHtml}</div>` : '') + `</div>`;
      head.querySelector('.card-title').textContent = title;
      if (helpBtn) head.appendChild(helpBtn);
      card.appendChild(head);

      const body = document.createElement('div');
      body.className = 'card-body';
      if (noteHtml) body.insertAdjacentHTML('beforeend', `<div class="card-note">${noteHtml}</div>`);
      body.appendChild(item); /* MOVE — preserves event listeners */
      card.appendChild(body);
      mainGrid.appendChild(card);
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  let currentCat = null;
  const TAB_KEY = 'toolCardsTab:' + location.pathname;
  let currentQ = '';

  function applyFilter() {
    document.querySelectorAll('#tdbMainGrid .card').forEach(card => {
      const catMatch = !currentCat || currentCat === 'all' || card.dataset.cat === currentCat;
      const qMatch = !currentQ || card.dataset.title.includes(currentQ) || card.textContent.toLowerCase().includes(currentQ);
      card.style.display = (catMatch && qMatch) ? '' : 'none';
    });
  }

  function tdbSetTab(btn) {
    document.querySelectorAll('#tngDebug .tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    currentCat = btn.dataset.cat || 'all';
    try { localStorage.setItem(TAB_KEY, currentCat); } catch (e) {}
    if (history.replaceState) history.replaceState(null, '', '#' + currentCat);
    applyFilter();
  }

  /* ── Post-render transformers for JS-injected monitoring content ── */

  /*
   * Rate Limit: The classic JS populates #rate-limit-content with .domain-rate-limit items.
   * Each item has an h5 (domain name) and <p> tags like "5-minute: X / Y requests".
   * Transform to: domain header (.rl-domain) + bar rows (.rl-bar-row) inside .rl-item wrappers.
   * Also ensure the "Rate Limits by Domain:" header exists above the box.
   */
  function transformRateLimitContent(rlContentEl) {
    /* Add the domain header label above the box if missing */
    const rlInfo = rlContentEl.parentElement;
    if (rlInfo && !rlInfo.querySelector('.rl-domain-header')) {
      const hdr = document.createElement('div');
      hdr.className = 'rl-domain-header';
      hdr.style.cssText = 'font-size:12px;font-weight:600;color:var(--text2);margin-bottom:4px';
      hdr.textContent = 'Rate Limits by Domain:';
      rlInfo.insertBefore(hdr, rlContentEl);
    }

    rlContentEl.querySelectorAll('.domain-rate-limit').forEach(domainItem => {
      /* Add rl-item class for border-bottom styling */
      domainItem.classList.add('rl-item');

      /* h5 → .rl-domain */
      const h5 = domainItem.querySelector('h5');
      if (h5 && !h5.classList.contains('rl-domain')) {
        h5.className = 'rl-domain';
      }

      /* p tags with "5-minute: X / Y" or "hourly: X / Y" → bar rows */
      domainItem.querySelectorAll('p').forEach(p => {
        if (p.classList.contains('rl-bar-row')) return; /* already transformed */
        const m = p.textContent.match(/([\w\-]+):\s*(\d+)\s*\/\s*(\d+)/);
        if (!m) return;
        const label = m[1] === '5-minute' ? '5-min' : 'Hourly';
        const count = parseInt(m[2]), limit = parseInt(m[3]);
        const pct = limit > 0 ? Math.min(100, Math.round(count / limit * 100)) : 0;
        const cls = pct < 30 ? 'low' : pct < 70 ? 'mid' : 'high';
        const row = document.createElement('div');
        row.className = 'rl-bar-row';
        row.innerHTML = `<span style="font-size:10px;color:var(--text3);width:36px;flex-shrink:0;font-family:var(--sans)">${label}</span>` +
          `<div class="rl-bar-bg"><div class="rl-bar-fill ${cls}" style="width:${pct}%"></div></div>` +
          `<span class="rl-text">${count} / ${limit}</span>`;
        p.replaceWith(row);
      });
    });
  }

  /*
   * Plex Token Status: classic JS injects .token-status blocks into .token-status-container.
   * Each has .token-user (username), .token-details (validity, expiry etc).
   * Transform .token-status → .token-card with .token-card-hdr and .token-card-body.
   * Transform .token-validity.valid/invalid → status-badge badge-green/badge-red.
   */
  function transformPlexTokenContent(container) {
    container.querySelectorAll('.token-status').forEach(ts => {
      if (ts.classList.contains('token-card')) return; /* already done */

      const userEl = ts.querySelector('.token-user');
      const detailsEl = ts.querySelector('.token-details');
      if (!userEl || !detailsEl) return;

      ts.classList.add('token-card');
      userEl.className = 'token-card-hdr';
      detailsEl.className = 'token-card-body';

      /* .token-validity → status-badge */
      const validity = detailsEl.querySelector('.token-validity');
      if (validity) {
        const isValid = validity.classList.contains('valid');
        validity.className = 'status-badge ' + (isValid ? 'badge-green' : 'badge-red');
        validity.style.cssText = '';
      }
    });
  }

  /*
   * Trakt Token Status: classic JS injects into #trakt-token-status .token-status-container.
   * Has .token-validity, .token-access, .token-refresh divs.
   * Transform: validity → status-badge, access/refresh → token-code.
   */
  function transformTraktTokenContent(container) {
    const validity = container.querySelector('.token-validity');
    if (validity && !validity.classList.contains('status-badge')) {
      const isValid = validity.classList.contains('valid');
      validity.className = 'status-badge ' + (isValid ? 'badge-green' : 'badge-orange');
      validity.style.cssText = '';
    }
    container.querySelectorAll('.token-access,.token-refresh').forEach(el => {
      if (!el.classList.contains('token-code')) el.className += ' token-code';
      el.style.cssText = '';
    });
    /* Wrap all sibling divs with text in a flex column */
    const details = container.querySelector('.token-details');
    if (details && !details.classList.contains('tng-styled')) {
      details.classList.add('tng-styled');
      details.style.cssText = 'display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--text2)';
      details.querySelectorAll('[style]').forEach(n => {
        if (!n.classList.contains('token-code') && !n.classList.contains('status-badge') && n.tagName !== 'SPAN') n.removeAttribute('style');
      });
    }
  }

  function attachMonitoringObservers() {
    const rlContent = document.getElementById('rate-limit-content');
    if (rlContent) {
      new MutationObserver(() => transformRateLimitContent(rlContent))
        .observe(rlContent, { childList: true, subtree: true });
      /* Run immediately in case content already loaded */
      if (rlContent.children.length > 0) transformRateLimitContent(rlContent);
    }

    /* Plex token: observe the container inside #plex-token-status */
    const plexContainer = document.querySelector('#plex-token-status .token-status-container');
    if (plexContainer) {
      new MutationObserver(() => transformPlexTokenContent(plexContainer))
        .observe(plexContainer, { childList: true, subtree: true });
      if (plexContainer.children.length > 0) transformPlexTokenContent(plexContainer);
    }

    const traktContainer = document.querySelector('#trakt-token-status .token-status-container');
    if (traktContainer) {
      new MutationObserver(() => transformTraktTokenContent(traktContainer))
        .observe(traktContainer, { childList: true, subtree: true });
      if (traktContainer.children.length > 0) transformTraktTokenContent(traktContainer);
    }
  }

  document.addEventListener('DOMContentLoaded', function() {
    if (!document.body.classList.contains('tangerine-theme')) return;

    buildCards();
    attachMonitoringObservers();

    /* Open the tab from the URL hash, else the last one used, else the first. */
    let saved = (location.hash || '').slice(1);
    if (!saved) { try { saved = localStorage.getItem(TAB_KEY) || ''; } catch (e) {} }
    const btn = (saved && document.querySelector(`#tngDebug .tab[data-cat="${saved}"]`))
      || document.querySelector('#tngDebug .tab');
    if (btn) tdbSetTab(btn);

    document.querySelectorAll('#tngDebug .tab').forEach(btn => {
      btn.addEventListener('click', () => tdbSetTab(btn));
    });
    document.getElementById('tdbSearch')?.addEventListener('input', function(e) {
      currentQ = e.target.value.trim().toLowerCase();
      applyFilter();
    });
  });
})();
