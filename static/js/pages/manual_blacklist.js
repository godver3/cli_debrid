/* Moved from templates/manual_blacklist.html. */
(function() {
  const BLACKLIST_URL = window.pageData.manual_blacklist.debug_manual_blacklist_url;
  let unsaved = false;
  let toastTimer;

  // ── Toast ──
  function showToast(msg, color) {
    const t = document.getElementById('blToast');
    t.className = 'bl-toast bl-toast-' + (color || 'blue');
    document.getElementById('blToastMsg').textContent = msg;
    void t.offsetWidth;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 2500);
  }

  function markUnsaved() {
    unsaved = true;
    document.getElementById('blUnsavedBadge').style.display = 'inline';
  }

  // ── Search / filter ──
  function getAllRows() {
    return Array.from(document.querySelectorAll('#blTbody tr[data-imdb]'));
  }

  function getAllCards() {
    return Array.from(document.querySelectorAll('#blCardList .bl-card[data-imdb]'));
  }

  function applyFilter() {
    const q = document.getElementById('blSearchInput').value.trim().toLowerCase();
    const t = document.getElementById('blTypeFilter').value;
    const rows = getAllRows();
    const cards = getAllCards();
    let visible = 0;

    function matchItem(el) {
      const title = (el.dataset.title || '').toLowerCase();
      const imdb = (el.dataset.imdb || '').toLowerCase();
      const type = el.dataset.type || '';
      const matchQ = !q || title.includes(q) || imdb.includes(q);
      const matchT = t === 'all' || type === t;
      return matchQ && matchT;
    }

    rows.forEach(row => {
      const show = matchItem(row);
      row.style.display = show ? '' : 'none';
      if (show) visible++;
    });
    cards.forEach(card => {
      card.style.display = matchItem(card) ? '' : 'none';
    });

    const total = rows.length || cards.length;
    document.getElementById('blItemCount').textContent = total + ' item' + (total !== 1 ? 's' : '');
    document.getElementById('blFooterInfo').textContent = visible === total
      ? total + ' total items'
      : visible + ' of ' + total + ' shown';
    document.getElementById('blFilterInfo').textContent = (q || t !== 'all') && visible !== total
      ? visible + ' result' + (visible !== 1 ? 's' : '') + ' found'
      : '';
    const noRes = document.getElementById('blNoResults');
    noRes.style.display = visible === 0 && total > 0 ? 'block' : 'none';
  }

  document.getElementById('blSearchInput').addEventListener('input', applyFilter);
  document.getElementById('blTypeFilter').addEventListener('change', applyFilter);
  document.getElementById('blClearBtn').addEventListener('click', () => {
    document.getElementById('blSearchInput').value = '';
    document.getElementById('blTypeFilter').value = 'all';
    applyFilter();
  });

  // ── Season logic ──
  function updateSeasonSummary(wrap) {
    const allCb = wrap.querySelector('.bl-all-seasons-cb');
    const summaryEl = wrap.querySelector('.bl-season-summary');
    if (!summaryEl) return;
    if (allCb.checked) {
      summaryEl.textContent = '';
    } else {
      const checked = Array.from(wrap.querySelectorAll('.bl-season-cb:checked')).map(cb => 'S' + cb.value);
      summaryEl.textContent = checked.length ? checked.join(', ') : 'None';
    }
  }

  function initSeasonWrap(wrap) {
    const imdbId = wrap.dataset.imdb;
    const selectedSeasons = JSON.parse(wrap.dataset.selected || '[]');
    const allCb = wrap.querySelector('.bl-all-seasons-cb');
    const chevron = wrap.querySelector('.bl-chevron');
    const list = wrap.querySelector('.bl-season-list');
    let loaded = false;

    updateSeasonSummary(wrap);

    allCb.addEventListener('change', function() {
      const cbs = wrap.querySelectorAll('.bl-season-cb');
      cbs.forEach(cb => cb.checked = this.checked);
      updateSeasonSummary(wrap);
      markUnsaved();
      // Auto-expand season list when unchecking All Seasons so user can pick specific seasons
      if (!this.checked && list.style.display === 'none') {
        chevron.click();
      }
    });

    chevron.addEventListener('click', async function() {
      const isHidden = list.style.display === 'none';

      if (isHidden && !loaded) {
        loaded = true;
        list.style.display = 'block';
        chevron.classList.add('open');
        list.innerHTML = '<div class="bl-season-loading">Loading…</div>';

        try {
          const res = await fetch('/debug/api/manual_blacklist/' + imdbId + '/seasons');
          const data = await res.json();
          list.innerHTML = '';

          if (data.success && data.seasons.length) {
            let lastChecked = null;
            data.seasons.forEach(season => {
              const eps = data.season_episodes[season] || 0;
              const isChecked = selectedSeasons.length === 0 || selectedSeasons.includes(season);
              const label = document.createElement('label');
              label.className = 'bl-season-item';
              label.innerHTML = `<input type="checkbox" class="bl-season-cb" value="${season}" ${isChecked ? 'checked' : ''}>
                <span class="bl-season-item-label">Season ${season}${eps ? ` <span>(${eps} episodes)</span>` : ''}</span>`;
              list.appendChild(label);

              const cb = label.querySelector('input');
              cb.addEventListener('change', function() {
                const allChecked = Array.from(wrap.querySelectorAll('.bl-season-cb')).every(c => c.checked);
                allCb.checked = allChecked;
                updateSeasonSummary(wrap);
                markUnsaved();
              });
              cb.addEventListener('click', function(e) {
                if (e.shiftKey && lastChecked) {
                  const boxes = Array.from(wrap.querySelectorAll('.bl-season-cb'));
                  const start = boxes.indexOf(this);
                  const end = boxes.indexOf(lastChecked);
                  const shouldCheck = this.checked;
                  boxes.slice(Math.min(start, end), Math.max(start, end) + 1).forEach(b => b.checked = shouldCheck);
                  const allChecked = boxes.every(c => c.checked);
                  allCb.checked = allChecked;
                  updateSeasonSummary(wrap);
                  markUnsaved();
                }
                lastChecked = this;
              });
            });
            const allChecked = Array.from(wrap.querySelectorAll('.bl-season-cb')).every(c => c.checked);
            allCb.checked = allChecked;
            updateSeasonSummary(wrap);
          } else {
            list.innerHTML = '<div class="bl-season-loading">No season information available</div>';
          }
        } catch(err) {
          list.innerHTML = '<div class="bl-season-loading" style="color:var(--bl-red)">Failed to load seasons</div>';
        }
      } else {
        list.style.display = isHidden ? 'block' : 'none';
        chevron.classList.toggle('open', isHidden);
      }
    });
  }

  // Init season wraps in both table rows and mobile cards
  // De-dupe by imdb id so each item only has one active season state
  const initializedSeasonIds = new Set();
  document.querySelectorAll('.bl-season-wrap').forEach(wrap => {
    const id = wrap.dataset.imdb;
    if (!initializedSeasonIds.has(id)) {
      initializedSeasonIds.add(id);
      initSeasonWrap(wrap);
    } else {
      // For duplicate card wraps, just init without tracking (mobile mirror)
      initSeasonWrap(wrap);
    }
  });

  // ── Delete ──
  function bindDelBtn(btn) {
    btn.addEventListener('click', async function() {
      const imdbId = this.dataset.imdb;
      const row = document.getElementById('blrow-' + imdbId);
      const card = document.getElementById('blcard-' + imdbId);
      if (row) row.classList.add('bl-removing');
      if (card) card.classList.add('bl-removing');

      const fd = new FormData();
      fd.append('action', 'remove');
      fd.append('imdb_id', imdbId);

      try {
        const res = await fetch(BLACKLIST_URL, { method: 'POST', body: fd });
        const data = await res.json().catch(() => ({}));
        if (res.ok && data.success) {
          setTimeout(() => {
            if (row) row.remove();
            if (card) card.remove();
            const remaining = getAllRows();
            if (remaining.length === 0 && !document.getElementById('blEmptyRow')) {
              const tbody = document.getElementById('blTbody');
              const tr = document.createElement('tr');
              tr.id = 'blEmptyRow';
              tr.innerHTML = '<td colspan="6" style="text-align:center;color:var(--bl-text3);padding:2rem">No items in the blacklist</td>';
              tbody.appendChild(tr);
            }
            applyFilter();
            if (window.toastManager) window.toastManager.show('Blacklist', 'Removed from blacklist', 'success');
          }, 250);
        } else {
          if (row) row.classList.remove('bl-removing');
          if (card) card.classList.remove('bl-removing');
          if (window.toastManager) window.toastManager.show('Blacklist', data.error || 'Failed to remove item', 'error');
        }
      } catch(err) {
        if (row) row.classList.remove('bl-removing');
        if (card) card.classList.remove('bl-removing');
        if (window.toastManager) window.toastManager.show('Blacklist', 'Error removing item', 'error');
      }
    });
  }

  document.querySelectorAll('.bl-del-btn').forEach(bindDelBtn);

  // ── Add ──
  async function addItem() {
    const input = document.getElementById('blImdbInput');
    const val = input.value.trim();
    if (!val) { showToast('Enter an IMDb ID first', 'red'); return; }
    if (!val.startsWith('tt')) { showToast('IMDb ID must start with "tt"', 'red'); return; }
    if (document.getElementById('blrow-' + val)) { showToast('Already in blacklist', 'red'); return; }

    const mediaType = document.getElementById('blTypeSelect').value;
    const fd = new FormData();
    fd.append('action', 'add');
    fd.append('imdb_id', val);
    fd.append('media_type', mediaType);

    try {
      const res = await fetch(BLACKLIST_URL, { method: 'POST', body: fd });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        input.value = '';
        if (window.toastManager) window.toastManager.show('Blacklist', 'Added to blacklist — reloading…', 'success');
        setTimeout(() => window.location.reload(), 1200);
      } else {
        if (window.toastManager) window.toastManager.show('Blacklist', data.error || 'Failed to add item', 'error');
      }
    } catch(err) {
      if (window.toastManager) window.toastManager.show('Blacklist', 'Error adding item', 'error');
    }
  }

  document.getElementById('blAddBtn').addEventListener('click', addItem);
  document.getElementById('blImdbInput').addEventListener('keydown', e => { if (e.key === 'Enter') addItem(); });

  // ── Save all ──
  async function saveAll() {
    // De-dupe by imdb id — table and card both have wraps for the same item.
    // Prefer the wrap that already has season checkboxes loaded (desktop table row)
    // over one that hasn't been expanded yet (mobile card).
    const byId = {};
    document.querySelectorAll('.bl-season-wrap').forEach(w => {
      const id = w.dataset.imdb;
      if (!byId[id]) {
        byId[id] = w;
      } else {
        // Prefer whichever has season checkboxes loaded
        const existing = byId[id];
        if (w.querySelectorAll('.bl-season-cb').length > existing.querySelectorAll('.bl-season-cb').length) {
          byId[id] = w;
        }
      }
    });
    const wraps = Object.values(byId);
    let success = true;

    for (const wrap of wraps) {
      const imdbId = wrap.dataset.imdb;
      const allCb = wrap.querySelector('.bl-all-seasons-cb');
      const seasonCbs = wrap.querySelectorAll('.bl-season-cb:checked');

      const fd = new FormData();
      fd.append('action', 'update_seasons');
      fd.append('imdb_id', imdbId);
      if (allCb && allCb.checked) {
        fd.append('all_seasons', 'on');
      } else {
        seasonCbs.forEach(cb => fd.append('seasons', cb.value));
      }

      try {
        const res = await fetch(BLACKLIST_URL, { method: 'POST', body: fd });
        if (!res.ok) { success = false; }
      } catch(err) {
        success = false;
      }
    }

    if (success) {
      unsaved = false;
      document.getElementById('blUnsavedBadge').style.display = 'none';
      showToast('All changes saved', 'blue');
    } else {
      showToast('Failed to save some changes', 'red');
    }
  }

  document.getElementById('blSaveAllBtn').addEventListener('click', saveAll);
  document.getElementById('blSaveAllBtn2').addEventListener('click', saveAll);

  // Initial filter state
  applyFilter();
})();
