/* Moved from templates/overlays.html. */
// Tab switching
document.querySelectorAll('.overlay-tab').forEach(tab => {
    tab.addEventListener('click', () => {
        // Remove active class from all tabs and contents
        document.querySelectorAll('.overlay-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));

        // Add active class to clicked tab and corresponding content
        tab.classList.add('active');
        const tabName = tab.dataset.tab;
        document.getElementById(`${tabName}-tab`).classList.add('active');
        sessionStorage.setItem('overlays_active_tab', tabName);

        // Load content for tab if needed
        if (tabName === 'movies' && !window.moviesLoaded) {
            loadMovies();
        } else if (tabName === 'shows' && !window.showsLoaded) {
            loadShows();
        } else if (tabName === 'templates' && !window.templatesLoaded) {
            loadTemplates();
        } else if (tabName === 'badges' && !window.badgeStatsLoaded) {
            loadBadgeStats();
        } else if (tabName === 'activity') {
            loadActivity();
        }
    });
});

// Library Sync
let _syncPollTimer = null;

async function syncLibrary() {
    const btn = document.getElementById('sync-library-btn');
    const status = document.getElementById('sync-library-status');

    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Syncing…';
    status.textContent = 'Starting sync…';

    try {
        const resp = await fetch('/api/overlays/sync_library', {method: 'POST'});
        const data = await resp.json();
        if (!data.success) {
            status.textContent = `Error: ${data.error}`;
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-sync-alt"></i> Sync Library';
            return;
        }
        // Poll for completion
        _syncPollTimer = setInterval(_pollSyncStatus, 2000);
    } catch (e) {
        status.textContent = `Request failed: ${e}`;
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-sync-alt"></i> Sync Library';
    }
}

async function _pollSyncStatus() {
    try {
        const resp = await fetch('/api/overlays/sync_library/status');
        const data = await resp.json();
        const status = document.getElementById('sync-library-status');
        const btn = document.getElementById('sync-library-btn');

        status.textContent = data.progress || '…';

        if (!data.running && data.done) {
            clearInterval(_syncPollTimer);
            _syncPollTimer = null;
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-sync-alt"></i> Sync Library';
            // Refresh grids so new plex_rating_key counts are reflected
            if (window.moviesLoaded) { _selectedMovieIds.clear(); window.moviesLoaded = false; loadMovies(); }
            if (window.showsLoaded)  { _selectedShowIds.clear();  window.showsLoaded = false;  loadShows(); }
            loadStats();
        }
    } catch (e) {
        console.warn('Sync poll error:', e);
        // Stop polling on error — show message and re-enable button so user can retry
        clearInterval(_syncPollTimer);
        _syncPollTimer = null;
        const status = document.getElementById('sync-library-status');
        const btn = document.getElementById('sync-library-btn');
        if (status) status.textContent = 'Status check failed — sync may still be running. Click to retry.';
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync-alt"></i> Sync Library'; }
    }
}

// Load statistics
async function _safeJsonFetch(url) {
    try {
        const resp = await fetch(url);
        if (!resp.ok) return null;
        const ct = resp.headers.get('content-type') || '';
        if (!ct.includes('application/json')) return null;
        return await resp.json();
    } catch (e) {
        return null;
    }
}

async function loadStats() {
    const [data, cd] = await Promise.all([
        _safeJsonFetch('/api/overlays/status'),
        _safeJsonFetch('/api/overlays/cleanup/stats')
    ]);

    if (data?.success) {
        document.getElementById('stat-total').textContent = data.layouts.total;
        document.getElementById('stat-layouts-movies').textContent = data.layouts.layouts_movies ?? '-';
        document.getElementById('stat-layouts-shows').textContent = data.layouts.layouts_shows ?? '-';
        document.getElementById('stat-layouts-seasons').textContent = data.layouts.layouts_seasons ?? '-';
        document.getElementById('stat-applied').textContent = data.overlays.applied;
        document.getElementById('stat-applied-movies').textContent = data.overlays.applied_movies ?? '-';
        document.getElementById('stat-applied-shows').textContent = data.overlays.applied_shows ?? '-';
        document.getElementById('stat-applied-seasons').textContent = data.overlays.applied_seasons ?? '-';
        document.getElementById('stat-unprocessed').textContent = data.overlays.unprocessed ?? 0;
        document.getElementById('stat-unprocessed-movies').textContent = data.overlays.unprocessed_movies ?? '-';
        document.getElementById('stat-unprocessed-shows').textContent = data.overlays.unprocessed_shows ?? '-';
        document.getElementById('stat-unprocessed-seasons').textContent = data.overlays.unprocessed_seasons ?? '-';
        document.getElementById('stat-failed').textContent = data.overlays.failed;
        document.getElementById('stat-failed-movies').textContent = data.overlays.failed_movies ?? '-';
        document.getElementById('stat-failed-shows').textContent = data.overlays.failed_shows ?? '-';
        document.getElementById('stat-failed-seasons').textContent = data.overlays.failed_seasons ?? '-';
    }

    if (cd?.success) {
        const mb = cd.backup_size_mb || 0;
        document.getElementById('stat-backup-size').textContent =
            mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : mb.toFixed(1) + ' MB';
        document.getElementById('stat-backup-count').textContent = cd.backup_count ?? '-';
        document.getElementById('stat-backup-orphaned').textContent = cd.backup_orphaned ?? '-';

        const totalPosters = cd.cleaned_total_posters || 0;
        const totalMb = cd.cleaned_total_mb || 0;
        document.getElementById('stat-cleaned').textContent = totalPosters;
        const reclaimedStr = totalMb >= 1024
            ? (totalMb / 1024).toFixed(1) + ' GB reclaimed'
            : totalMb.toFixed(1) + ' MB reclaimed';
        document.getElementById('stat-cleaned-breakdown').textContent =
            totalPosters > 0 ? reclaimedStr : 'No cleanups yet';
    }
}

async function resetCleanupStats(e) {
    e.stopPropagation();
    try {
        const r = await fetch('/api/overlays/stats/reset-cleanup', {method: 'POST'});
        const d = await r.json();
        if (d.success) {
            document.getElementById('stat-cleaned').textContent = '0';
            document.getElementById('stat-cleaned-breakdown').textContent = 'No cleanups yet';
        }
    } catch (_) {}
}

async function resetFailedStats(e) {
    e.stopPropagation();
    try {
        const r = await fetch('/api/overlays/stats/reset-failed', {method: 'POST'});
        const d = await r.json();
        if (d.success) {
            document.getElementById('stat-failed').textContent = '0';
            document.getElementById('stat-failed-movies').textContent = '0';
            document.getElementById('stat-failed-shows').textContent = '0';
            document.getElementById('stat-failed-seasons').textContent = '0';
        }
    } catch (_) {}
}

// Load movies
function _posterSrc(item) {
    // Applied items with a Plex key use the proxy route (disk-cached server-side).
    // Non-applied items fall back to TMDB poster path or placeholder.
    if (item.overlay_status === 'applied' && item.ms_item_id) {
        return `/api/overlays/poster_applied/${encodeURIComponent(item.ms_item_id)}`;
    }
    return item.poster_path ? `/scraper/tmdb_image/w185${item.poster_path}` : '/static/images/placeholder.png';
}

function _posterAttrs(item, mediaType) {
    // Applied poster: lazy-load via IntersectionObserver to avoid firing hundreds
    // of simultaneous requests when the grid first renders.
    if (item.overlay_status === 'applied' && item.ms_item_id) {
        const plexUrl  = `/api/overlays/poster_applied/${encodeURIComponent(item.ms_item_id)}`;
        const fallback = item.poster_path ? `/scraper/tmdb_image/w185${item.poster_path}` : '/static/images/placeholder.png';
        return `src="/static/images/placeholder.png" data-lazy-plex="${plexUrl}" data-lazy-fallback="${fallback}"`;
    }
    // Non-applied: TMDB path already available, or lazy-fetch via existing mechanism
    const src = item.poster_path ? `/scraper/tmdb_image/w185${item.poster_path}` : '/static/images/placeholder.png';
    const lazy = !item.poster_path && item.imdb_id
        ? ` data-lazy-imdb="${item.imdb_id}" data-lazy-type="${mediaType}"` : '';
    return `src="${src}"${lazy}`;
}

// Persists across grid reloads for the lifetime of the page session
const _posterFailCache = new Set();

// In-memory item stores — used for JS-side filtering without DOM traversal
let _moviesData = [];
let _showsData  = [];

// How many cards to render at once (prevents massive DOM from freezing the page)
const _GRID_PAGE = 300;

function lazyLoadPosters(grid) {
    // Only load posters for visible (non-hidden) cards in the current render
    const imgs = Array.from(grid.querySelectorAll('img[data-lazy-imdb]:not([data-lazy-started])'))
        .filter(img => !_posterFailCache.has(img.dataset.lazyImdb));
    if (!imgs.length) return;
    imgs.forEach(img => img.setAttribute('data-lazy-started', '1'));
    // Process in batches of 5 with 300ms delay — larger delay reduces fetch pile-up
    const BATCH = 5, DELAY = 300;
    let i = 0;
    let _lazyAborted = false;
    // Attach abort handle to grid so re-renders can cancel in-flight batches
    if (grid._lazyAbort) grid._lazyAbort();
    grid._lazyAbort = () => { _lazyAborted = true; };

    function nextBatch() {
        if (_lazyAborted) return;
        const batch = imgs.slice(i, i + BATCH);
        if (!batch.length) return;
        i += BATCH;
        batch.forEach(img => {
            if (_lazyAborted) return;
            const imdbId = img.dataset.lazyImdb;
            fetch(`/library/fetch_poster_imdb/${imdbId}/${img.dataset.lazyType}`)
                .then(r => {
                    if (!r.ok) { _posterFailCache.add(imdbId); return null; }
                    return r.json();
                })
                .then(d => {
                    if (!d) return;
                    if (d.success && d.poster_path) {
                        img.src = `/scraper/tmdb_image/w185${d.poster_path}`;
                    } else {
                        _posterFailCache.add(imdbId);
                    }
                })
                .catch(() => { _posterFailCache.add(imdbId); });
        });
        if (i < imgs.length) setTimeout(nextBatch, DELAY);
    }
    nextBatch();
}

// IntersectionObserver for applied-poster lazy loading (data-lazy-plex).
// Fires only when a card scrolls into the viewport, preventing a flood of
// simultaneous Plex requests when the grid first renders.
let _plexPosterObserver = null;
function _initPlexPosterObserver() {
    if (_plexPosterObserver) return;
    _plexPosterObserver = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (!entry.isIntersecting) return;
            const img = entry.target;
            const url      = img.dataset.lazyPlex;
            const fallback = img.dataset.lazyFallback || '/static/images/placeholder.png';
            if (!url) return;
            _plexPosterObserver.unobserve(img);
            delete img.dataset.lazyPlex;
            img.onerror = () => { img.onerror = null; img.src = fallback; };
            img.src = url;
        });
    }, { rootMargin: '200px' });
}

function _observePlexPosters(grid) {
    _initPlexPosterObserver();
    grid.querySelectorAll('img[data-lazy-plex]').forEach(img => {
        _plexPosterObserver.observe(img);
    });
}

function _renderMovieCard(item) {
    const failReason = item.overlay_status === 'failed' && item.overlay_reason
        ? `<div class="overlay-fail-reason">${item.overlay_reason}</div>` : '';
    const pickerBtn = item.ms_item_id
        ? `<button class="card-poster-btn" title="Choose poster" onclick="event.stopPropagation(); openPosterPicker(${item.id})">&#9998;</button>` : '';
    return `<div class="media-card" data-item-id="${item.id}" data-status="${item.overlay_status}" onclick="toggleSelect(this)">
        <img class="media-poster" ${_posterAttrs(item, 'movie')} alt="${item.title}">
        <div class="overlay-status status-${item.overlay_status}">${item.overlay_status.replace('_', ' ')}</div>
        ${failReason}
        <button class="card-regen-btn" title="Regenerate overlay using current library poster" onclick="event.stopPropagation(); regenerateSingleOverlay(${item.id}, this)">↻</button>
        ${pickerBtn}
        ${item.version_count > 1 ? `<div class="card-version-badge" title="${item.version_count} versions in library">${item.version_count} versions</div>` : ''}
        <div class="media-info">
            <div class="media-title">${item.title}</div>
            <div class="media-year">${item.year || 'Unknown'}</div>
        </div>
    </div>`;
}

function _seasonsBadge(item) {
    const nums = item.seasons_unprocessed;
    if (!nums || !nums.length) return '';
    const safeKey = (item.ms_item_id || '').replace(/'/g, "\\'");
    const safeTitle = (item.title || '').replace(/'/g, "\\'");
    const labels = nums.map(n => n === 0 ? 'S0' : `S${n}`);
    const text = labels.length <= 4 ? labels.join(' · ') : labels.slice(0, 3).join(' · ') + ` +${labels.length - 3}`;
    const tooltip = `Unprocessed seasons: ${labels.join(', ')} — click to manage`;
    return `<div class="card-seasons-unprocessed" title="${tooltip}" onclick="event.stopPropagation(); openSeasonsModal('${safeKey}', '${safeTitle}')">${text} unprocessed</div>`;
}

function _renderShowCard(item) {
    const failReason = item.overlay_status === 'failed' && item.overlay_reason
        ? `<div class="overlay-fail-reason">${item.overlay_reason}</div>` : '';
    const pickerBtn = item.ms_item_id
        ? `<button class="card-poster-btn" title="Choose poster" onclick="event.stopPropagation(); openPosterPicker(${item.id})">&#9998;</button>` : '';
    return `<div class="media-card" data-item-id="${item.id}" data-status="${item.overlay_status}" onclick="toggleSelect(this)">
        <img class="media-poster" ${_posterAttrs(item, 'show')} alt="${item.title}">
        <div class="overlay-status status-${item.overlay_status}">${item.overlay_status.replace('_', ' ')}</div>
        ${failReason}
        <button class="card-regen-btn" title="Regenerate overlay using current library poster" onclick="event.stopPropagation(); regenerateSingleOverlay(${item.id}, this)">↻</button>
        ${pickerBtn}
        ${item.ms_item_id ? `<button class="card-seasons-btn" title="Manage season poster overlays" onclick="event.stopPropagation(); openSeasonsModal('${item.ms_item_id}', '${(item.title||'').replace(/'/g,"\\'")}')">Seasons</button>` : ''}
        <div class="media-info">
            <div class="media-title">${item.title}</div>
            <div class="media-year">${item.year || 'Unknown'}${item.collected_episodes != null ? ` · ${item.collected_episodes}/${item.total_episodes} eps` : ''}</div>
            ${_seasonsBadge(item)}
        </div>
    </div>`;
}

function _renderGrid(gridId, items, renderFn, searchQuery, statusFilter) {
    const grid = document.getElementById(gridId);
    if (!grid) return;

    // Abort any in-flight lazy loading before re-rendering
    if (grid._lazyAbort) { grid._lazyAbort(); grid._lazyAbort = null; }

    const q = (searchQuery || '').trim().toLowerCase();
    const filtered = items.filter(item => {
        const titleMatch = !q || (item.title || '').toLowerCase().includes(q);
        const statusMatch = !statusFilter || item.overlay_status === statusFilter
            || (gridId === 'shows-grid' && (item.seasons_unprocessed || []).length > 0
                && (statusFilter === 'not_started' || statusFilter === 'failed'));
        return titleMatch && statusMatch;
    });

    const shown = filtered.slice(0, _GRID_PAGE);
    const total = filtered.length;

    if (shown.length === 0) {
        grid.innerHTML = `<p class="loading">${items.length === 0 ? 'No items found' : 'No matches for current filter'}</p>`;
        return;
    }

    let html = shown.map(renderFn).join('');
    if (total > _GRID_PAGE) {
        html += `<p class="loading" style="grid-column:1/-1;text-align:center;padding:12px;opacity:.6;">
            Showing ${_GRID_PAGE} of ${total} — refine your search to see more</p>`;
    }
    grid.innerHTML = html;

    // Restore selection state for re-rendered cards
    const selStore = gridId === 'movies-grid' ? _selectedMovieIds : _selectedShowIds;
    if (selStore.size > 0) {
        grid.querySelectorAll('.media-card').forEach(card => {
            if (selStore.has(parseInt(card.dataset.itemId))) card.classList.add('selected');
        });
    }

    lazyLoadPosters(grid);
    _observePlexPosters(grid);
}

async function loadMovies() {
    try {
        const response = await fetch('/api/overlays/media/movies');
        const data = await response.json();
        if (data.success) {
            _moviesData = data.items || [];
            window.moviesLoaded = true;
            const q = document.getElementById('movies-search')?.value || '';
            _renderGrid('movies-grid', _moviesData, _renderMovieCard, q, _activeStatusFilter);
        } else {
            document.getElementById('movies-grid').innerHTML = '<p class="loading">No movies found</p>';
        }
    } catch (error) {
        console.error('Failed to load movies:', error);
        document.getElementById('movies-grid').innerHTML = '<p class="loading">Error loading movies</p>';
    }
}

// Load TV shows
async function loadShows() {
    try {
        const response = await fetch('/api/overlays/media/shows');
        const data = await response.json();
        if (data.success) {
            _showsData = data.items || [];
            window.showsLoaded = true;
            const q = document.getElementById('shows-search')?.value || '';
            _renderGrid('shows-grid', _showsData, _renderShowCard, q, _activeStatusFilter);
        } else {
            document.getElementById('shows-grid').innerHTML = '<p class="loading">No TV shows found</p>';
        }
    } catch (error) {
        console.error('Failed to load shows:', error);
        document.getElementById('shows-grid').innerHTML = '<p class="loading">Error loading shows</p>';
    }
}

// Load templates
async function loadTemplates() {
    try {
        const response = await fetch('/api/overlays/layouts');
        const data = await response.json();

        // Populate layout selectors
        const movieSelector = document.getElementById('movies-layout-selector');
        const showSelector = document.getElementById('shows-layout-selector');

        if (data.success) {
            const options = data.layouts.map(t =>
                `<option value="${t.id}">${t.name} (${t.media_type})</option>`
            ).join('');

            movieSelector.innerHTML = '<option value="">Select Layout...</option>' + options;
            showSelector.innerHTML = '<option value="">Select Layout...</option>' + options;

            // Display templates list
            const list = document.getElementById('templates-list');
            list.innerHTML = data.layouts.map(t => `
                <div class="layout-card${t.is_default ? ' active-layout' : ''}">
                    <div class="layout-card-hdr">
                        <span class="layout-name">${t.name}</span>
                        ${t.is_system ? '<span class="layout-badge badge-default">Default</span>' : ''}
                        ${t.is_default ? '<span class="layout-badge badge-active">Active</span>' : '<span class="layout-badge badge-inactive">Inactive</span>'}
                    </div>
                    <div class="layout-meta">Type: ${t.media_type} &nbsp;|&nbsp; Active: ${t.is_default ? '<span class="yes">Yes</span>' : '<span class="no">No</span>'}</div>
                    <div class="layout-actions">
                        <button class="btn btn-ghost btn-sm" onclick="editTemplate(${t.id})"><i class="fa-solid fa-pen"></i> Edit</button>
                        <button class="btn btn-ghost btn-sm" onclick="toggleActive(${t.id}, ${t.is_default})">${t.is_default ? '<i class="fa-solid fa-toggle-off"></i> Deactivate' : '<i class="fa-solid fa-toggle-on"></i> Set Active'}</button>
                        <button class="btn btn-ghost btn-sm" onclick="duplicateTemplate(${t.id})"><i class="fa-solid fa-copy"></i> Duplicate</button>
                        <button class="btn btn-ghost btn-sm" onclick="exportTemplate(${t.id})"><i class="fa-solid fa-file-export"></i> Export</button>
                        <button class="btn btn-danger btn-sm" onclick="deleteTemplate(${t.id})"><i class="fa-solid fa-trash"></i> Delete</button>
                    </div>
                </div>
            `).join('');

            window.templatesLoaded = true;
        }
    } catch (error) {
        console.error('Failed to load templates:', error);
    }
}

// Toggle item selection
// Persistent selection sets — survive grid re-renders caused by filtering
const _selectedMovieIds = new Set();
const _selectedShowIds  = new Set();

function toggleSelect(card) {
    const id = parseInt(card.dataset.itemId);
    const inMovies = !!document.getElementById('movies-grid')?.contains(card);
    const store = inMovies ? _selectedMovieIds : _selectedShowIds;
    if (store.has(id)) { store.delete(id); card.classList.remove('selected'); }
    else               { store.add(id);    card.classList.add('selected'); }
}

// Generate overlays for selected items
// ── Progress overlay helpers ──────────────────────────────────────────────
let _progressPollTimer = null;

function _showProgress(title, msg) {
    document.getElementById('task-progress-title').textContent = title;
    document.getElementById('task-progress-msg').textContent = msg || '';
    const counter = document.getElementById('task-progress-counter');
    if (counter) counter.textContent = '';
    // Reset bar to animated pulse
    const bar = document.getElementById('task-progress-bar');
    if (bar) { bar.style.width = ''; bar.style.animation = ''; }
    const el = document.getElementById('task-progress-overlay');
    el.style.display = 'flex';
}
function _updateProgressCounter(done, total, extra) {
    const counter = document.getElementById('task-progress-counter');
    if (!counter) return;
    if (total > 0) {
        const pct = Math.round(done / total * 100);
        counter.textContent = `${done} / ${total}  (${pct}%)${extra ? '  · ' + extra : ''}`;
        const bar = document.getElementById('task-progress-bar');
        if (bar) {
            bar.style.animation = 'none';
            bar.style.width = pct + '%';
        }
    } else {
        counter.textContent = extra || '';
    }
}
function _hideProgress() {
    document.getElementById('task-progress-overlay').style.display = 'none';
    if (_progressPollTimer) { clearInterval(_progressPollTimer); _progressPollTimer = null; }
}

async function _pollUntilDone(statusUrl, onUpdate, onDone, intervalMs) {
    if (_progressPollTimer) clearInterval(_progressPollTimer);
    _progressPollTimer = setInterval(async () => {
        try {
            const resp = await fetch(statusUrl);
            const s = await resp.json();
            if (!s.success) return;
            // Keep the progress title and subtitle in sync when the backend changes phase
            // (e.g. switches from "Regenerating TV shows…" to "Applying season overlays…")
            if (s.label) {
                const titleEl = document.getElementById('task-progress-title');
                if (titleEl && titleEl.textContent !== s.label) {
                    titleEl.textContent = s.label;
                    // Clear the stale subtitle so it doesn't say "Starting shows…" during seasons
                    const msgEl = document.getElementById('task-progress-msg');
                    if (msgEl) msgEl.textContent = '';
                }
            }
            onUpdate(s);
            if (!s.running && s.finished_at) {
                clearInterval(_progressPollTimer);
                _progressPollTimer = null;
                onDone(s);
            }
        } catch (_e) { /* ignore transient fetch errors */ }
    }, intervalMs || 1500);
}
function _refreshGrids(mediaType) {
    if (!mediaType || mediaType === 'movie') { _selectedMovieIds.clear(); window.moviesLoaded = false; loadMovies(); }
    if (!mediaType || mediaType === 'tv')    { _selectedShowIds.clear();  window.showsLoaded = false;  loadShows();  }
    loadStats();
}

async function generateSelected(mediaType) {
    const store = mediaType === 'movie' ? _selectedMovieIds : _selectedShowIds;
    if (store.size === 0) { showPopup({ type: 'warning', title: 'Required', message: 'Please select at least one item', autoClose: 4000 }); return; }
    const itemIds = Array.from(store);
    const templateSelector = document.getElementById(`${mediaType === 'movie' ? 'movies' : 'shows'}-layout-selector`);
    const layoutId = templateSelector.value ? parseInt(templateSelector.value) : null;

    _showProgress('Generating overlays…', `Starting — ${itemIds.length} item${itemIds.length !== 1 ? 's' : ''}…`);
    try {
        const response = await fetch('/api/overlays/generate/batch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({item_ids: itemIds, layout_id: layoutId})
        });
        const data = await response.json();
        if (!data.success) {
            _hideProgress();
            showPopup({ type: 'error', title: 'Error', message: 'Failed to start: ' + (data.error || 'Unknown error'), autoClose: 5000 });
            return;
        }
        _pollUntilDone(
            '/api/overlays/generate/status',
            s => {
                _updateProgressCounter(s.done, s.total,
                    [s.failed > 0 ? `${s.failed} failed` : '', (s.current || '').substring(0, 40)].filter(Boolean).join(' · '));
            },
            s => {
                _hideProgress();
                const msg = `Generated ${s.applied} overlay${s.applied !== 1 ? 's' : ''}${s.failed > 0 ? `, ${s.failed} failed` : ''}`;
                showPopup({ type: 'success', title: 'Success', message: msg, autoClose: 4000 });
                _refreshGrids(mediaType);
            }
        );
    } catch (error) {
        _hideProgress();
        console.error('Generation failed:', error);
        showPopup({ type: 'error', title: 'Error', message: 'Failed to generate overlays', autoClose: 5000 });
    }
}

// Select all cards in a grid
function selectAll(checkbox, gridId) {
    const store = gridId === 'movies-grid' ? _selectedMovieIds : _selectedShowIds;
    const data  = gridId === 'movies-grid' ? _moviesData : _showsData;
    if (checkbox.checked) {
        // Select all items in the current filter (not just visible rendered ones)
        const q = (document.getElementById(gridId === 'movies-grid' ? 'movies-search' : 'shows-search')?.value || '').trim().toLowerCase();
        data.filter(i => !q || (i.title||'').toLowerCase().includes(q))
            .forEach(i => store.add(i.id));
    } else {
        store.clear();
    }
    // Update DOM for currently rendered cards
    const grid = document.getElementById(gridId);
    grid.querySelectorAll('.media-card').forEach(card => {
        card.classList.toggle('selected', store.has(parseInt(card.dataset.itemId)));
    });
}

async function removeSelectedOverlays(mediaType) {
    const store = mediaType === 'movie' ? _selectedMovieIds : _selectedShowIds;
    if (store.size === 0) {
        showPopup({ type: 'warning', title: 'Required', message: 'Please select items to remove overlays from', autoClose: 4000 });
        return;
    }
    showPopup({
        type: 'confirm',
        title: 'Remove Overlays',
        message: `Remove overlays for ${store.size} item${store.size !== 1 ? 's' : ''}? Original posters will be restored.`,
        confirmText: 'Remove',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const itemIds = Array.from(store);
            _showProgress('Removing overlays…', `Restoring ${itemIds.length} original poster${itemIds.length !== 1 ? 's' : ''}…`);
            try {
                const response = await fetch('/api/overlays/cleanup/remove-batch', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({item_ids: itemIds})
                });
                const data = await response.json();
                _hideProgress();
                showPopup({ type: 'success', title: 'Success', message: `Removed ${data.restored} overlays${data.failed > 0 ? `, ${data.failed} failed` : ''}`, autoClose: 4000 });
                _refreshGrids(mediaType);
            } catch (error) {
                _hideProgress();
                console.error('Removal failed:', error);
                showPopup({ type: 'error', title: 'Error', message: 'Failed to remove overlays', autoClose: 5000 });
            }
        }
    });
}

async function removeAllOverlays() {
    showPopup({
        type: 'confirm',
        title: 'Remove All Overlays',
        message: 'Remove ALL overlays for both movies and TV shows? This will restore original posters.',
        confirmText: 'Continue',
        cancelText: 'Cancel',
        onConfirm: function() {
            showPopup({
                type: 'confirm',
                title: 'Are You Sure?',
                message: 'Are you SURE? This cannot be undone.',
                confirmText: 'Remove All',
                cancelText: 'Cancel',
                onConfirm: async function() {
                    _showProgress('Removing all overlays…', 'Starting — fetching applied items…');
                    try {
                        const response = await fetch('/api/overlays/cleanup/remove-all', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({})
                        });
                        const data = await response.json();
                        if (!data.success) {
                            _hideProgress();
                            showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + (data.error || 'Unknown error'), autoClose: 5000 });
                            return;
                        }
                        if (!data.started) {
                            _hideProgress();
                            showPopup({ type: 'info', title: 'Notice', message: 'No applied overlays found', autoClose: 4000 });
                            return;
                        }
                        _pollUntilDone(
                            '/api/overlays/cleanup/remove-all/status',
                            s => {
                                _updateProgressCounter(s.done, s.total,
                                    [s.failed > 0 ? `${s.failed} failed` : '', (s.current || '').substring(0, 40)].filter(Boolean).join(' · '));
                            },
                            s => {
                                _hideProgress();
                                showPopup({ type: 'success', title: 'Success', message: `Removed ${s.restored} overlay${s.restored !== 1 ? 's' : ''}${s.failed > 0 ? `, ${s.failed} failed` : ''}`, autoClose: 4000 });
                                _refreshGrids(null);
                            }
                        );
                    } catch (error) {
                        _hideProgress();
                        console.error('Remove all failed:', error);
                        showPopup({ type: 'error', title: 'Error', message: 'Failed to remove overlays', autoClose: 5000 });
                    }
                }
            });
        }
    });
}

async function cleanupOrphanedBackups(event) {
    event.stopPropagation();
    const btn = document.getElementById('cleanup-backup-btn');
    const originalHTML = btn.innerHTML;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
    btn.disabled = true;
    try {
        const resp = await fetch('/api/overlays/cleanup/orphaned-backups', {method: 'POST'});
        const data = await resp.json();
        if (data.success) {
            const removed = data.orphaned_removed || 0;
            showPopup({ type: removed > 0 ? 'success' : 'info', title: removed > 0 ? 'Success' : 'Notice', message: removed > 0 ? `Removed ${removed} orphaned backup(s)` : 'No orphaned backups found', autoClose: 4000 });
            loadStats();
        } else {
            showPopup({ type: 'error', title: 'Error', message: 'Cleanup failed: ' + (data.message || 'Unknown error'), autoClose: 5000 });
        }
    } catch (e) {
        showPopup({ type: 'error', title: 'Error', message: 'Cleanup failed', autoClose: 5000 });
    } finally {
        btn.innerHTML = originalHTML;
        btn.disabled = false;
    }
}

async function deleteAllBackups(event) {
    event.stopPropagation();
    showPopup({
        type: 'confirm',
        title: 'Delete All Backups',
        message: 'Delete ALL backup posters and reset every overlay to pending?\n\nThis cannot be undone. The next sync will re-download fresh posters from your media server and re-apply all overlays from scratch.',
        confirmText: 'Delete All',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const btn = document.getElementById('delete-all-backups-btn');
            const originalHTML = btn.innerHTML;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
            btn.disabled = true;
            try {
                const resp = await fetch('/api/overlays/cleanup/delete-all-backups', {method: 'POST'});
                const data = await resp.json();
                if (data.success) {
                    showPopup({ type: 'success', title: 'Success', message: `Deleted ${data.deleted} backup(s). All overlays reset to pending — next sync will start fresh.`, autoClose: 4000 });
                    loadStats();
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + (data.message || 'Unknown error'), autoClose: 5000 });
                }
            } catch (e) {
                showPopup({ type: 'error', title: 'Error', message: 'Failed to delete backups', autoClose: 5000 });
            } finally {
                btn.innerHTML = originalHTML;
                btn.disabled = false;
            }
        }
    });
}

// ── Poster Picker ─────────────────────────────────────────────────────────
let _ppItemId = null;
let _ppPosters = [];
let _ppIndex = 0;

async function openPosterPicker(itemId) {
    _ppItemId = itemId;
    _ppPosters = [];
    _ppIndex = 0;
    const overlay = document.getElementById('poster-picker-overlay');
    overlay.style.display = 'flex';
    _ppSetLoading(true);
    document.getElementById('pp-apply').disabled = true;
    document.getElementById('pp-prev').disabled = true;
    document.getElementById('pp-next').disabled = true;
    document.getElementById('pp-info').textContent = 'Loading posters…';
    document.getElementById('pp-img').src = '';
    try {
        const resp = await fetch(`/api/overlays/poster_list/${itemId}`);
        const data = await resp.json();
        if (!data.success || !data.posters.length) {
            document.getElementById('pp-info').textContent = data.error || 'No posters available';
            _ppSetLoading(false);
            return;
        }
        _ppPosters = data.posters;
        // Start at the currently selected poster (or 0)
        _ppIndex = _ppPosters.findIndex(p => p.selected);
        if (_ppIndex < 0) _ppIndex = 0;
        _ppShowPoster(_ppIndex);
    } catch (e) {
        document.getElementById('pp-info').textContent = 'Failed to load posters';
        _ppSetLoading(false);
    }
}

function closePosterPicker() {
    document.getElementById('poster-picker-overlay').style.display = 'none';
    _ppItemId = null;
    _ppPosters = [];
}

function _ppSetLoading(on) {
    document.getElementById('pp-spinner').style.display = on ? 'flex' : 'none';
}

function _ppShowPoster(idx) {
    const p = _ppPosters[idx];
    if (!p) return;
    _ppSetLoading(true);
    document.getElementById('pp-apply').disabled = true;
    const img = document.getElementById('pp-img');
    img.onload = () => { _ppSetLoading(false); document.getElementById('pp-apply').disabled = false; };
    img.onerror = () => { _ppSetLoading(false); document.getElementById('pp-info').textContent = 'Failed to load image'; };
    img.src = p.thumb_url + '?t=' + Date.now();
    const provider = p.provider ? ` · ${p.provider}` : '';
    const sel = p.selected ? ' · currently selected' : '';
    document.getElementById('pp-info').textContent = `${idx + 1} of ${_ppPosters.length}${provider}${sel}`;
    document.getElementById('pp-prev').disabled = idx === 0;
    document.getElementById('pp-next').disabled = idx === _ppPosters.length - 1;
}

function navigatePoster(dir) {
    const next = _ppIndex + dir;
    if (next < 0 || next >= _ppPosters.length) return;
    _ppIndex = next;
    _ppShowPoster(_ppIndex);
}

async function applyChosenPoster() {
    if (!_ppItemId || !_ppPosters[_ppIndex]) return;
    const btn = document.getElementById('pp-apply');
    const ratingKey = _ppPosters[_ppIndex].rating_key;
    btn.disabled = true;
    btn.textContent = 'Applying…';
    _ppSetLoading(true);
    try {
        const resp = await fetch(`/api/overlays/apply_poster/${_ppItemId}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({rating_key: ratingKey})
        });
        const data = await resp.json();
        if (data.success) {
            closePosterPicker();
            _refreshGrids();
        } else {
            showPopup({ type: 'error', title: 'Error', message: 'Failed to apply poster: ' + (data.message || data.error || 'Unknown error'), autoClose: 5000 });
            btn.disabled = false;
            btn.textContent = 'Apply with Overlay';
            _ppSetLoading(false);
        }
    } catch (e) {
        showPopup({ type: 'error', title: 'Error', message: 'Failed to apply poster', autoClose: 5000 });
        btn.disabled = false;
        btn.textContent = 'Apply with Overlay';
        _ppSetLoading(false);
    }
}
// ── End Poster Picker ──────────────────────────────────────────────────────

async function regenerateSingleOverlay(itemId, btn) {
    showPopup({
        type: 'confirm',
        title: 'Regenerate Overlay',
        message: 'Regenerate overlay using the currently-selected library poster?',
        confirmText: 'Regenerate',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const originalHTML = btn.innerHTML;
            btn.innerHTML = '…';
            btn.disabled = true;
            try {
                const response = await fetch(`/api/overlays/generate/regenerate/${itemId}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({})
                });
                const data = await response.json();
                if (data.success) {
                    btn.innerHTML = '✓';
                    setTimeout(() => { btn.innerHTML = originalHTML; btn.disabled = false; }, 2000);
                    window.moviesLoaded = false; loadMovies();
                    window.showsLoaded = false; loadShows();
                    loadStats();
                } else {
                    btn.innerHTML = originalHTML;
                    btn.disabled = false;
                    showPopup({ type: 'error', title: 'Error', message: 'Regenerate failed: ' + (data.message || data.error || 'Unknown error'), autoClose: 5000 });
                }
            } catch (error) {
                btn.innerHTML = originalHTML;
                btn.disabled = false;
                showPopup({ type: 'error', title: 'Error', message: 'Failed to regenerate overlay', autoClose: 5000 });
            }
        }
    });
}

// Layout management functions
function createTemplate() {
    // Navigate to the layout builder page
    window.location.href = '/overlays/builder';
}

async function loadDefaultLayouts() {
    try {
        const response = await fetch('/api/overlays/layouts/load_defaults', {method: 'POST'});
        const data = await response.json();
        if (data.success) {
            if (data.loaded > 0) {
                showPopup({ type: 'success', title: 'Success', message: `Loaded ${data.loaded} default layout${data.loaded !== 1 ? 's' : ''}.`, autoClose: 4000 });
                window.templatesLoaded = false;
                loadTemplates();
            } else {
                showPopup({ type: 'info', title: 'Notice', message: 'All default layouts are already present — nothing to import.', autoClose: 4000 });
            }
        } else {
            showPopup({ type: 'error', title: 'Error', message: 'Failed to load defaults: ' + (data.error || 'Unknown error'), autoClose: 5000 });
        }
    } catch (error) {
        showPopup({ type: 'error', title: 'Error', message: 'Failed to load default layouts: ' + error.message, autoClose: 5000 });
    }
}

function importTemplate() {
    // Create a file input element dynamically
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'application/json';

    input.onchange = async (e) => {
        const file = e.target.files[0];
        if (!file) return;

        try {
            const text = await file.text();
            const templateData = JSON.parse(text);

            // Send to API
            const response = await fetch('/api/overlays/layouts/import', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(templateData)
            });

            const data = await response.json();
            if (data.success) {
                showPopup({ type: 'success', title: 'Success', message: 'Layout imported successfully!', autoClose: 4000 });
                loadTemplates(); // Reload templates list
            } else {
                showPopup({ type: 'error', title: 'Error', message: 'Failed to import layout: ' + (data.error || 'Unknown error'), autoClose: 5000 });
            }
        } catch (error) {
            console.error('Import failed:', error);
            showPopup({ type: 'error', title: 'Error', message: 'Failed to import layout. Please check the file format.', autoClose: 5000 });
        }
    };

    input.click();
}

async function editTemplate(templateId) {
    // Navigate to layout editor with the layout ID
    window.location.href = `/overlays/builder?layout_id=${templateId}`;
}

async function toggleActive(layoutId, currentlyActive) {
    try {
        const response = await fetch(`/api/overlays/layouts/${layoutId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({is_default: !currentlyActive})
        });
        const data = await response.json();
        if (data.success) {
            loadTemplates();
        } else {
            showPopup({ type: 'error', title: 'Error', message: 'Failed to update: ' + (data.error || 'Unknown error'), autoClose: 5000 });
        }
    } catch (e) {
        showPopup({ type: 'error', title: 'Error', message: 'Failed to update layout', autoClose: 5000 });
    }
}

async function duplicateTemplate(templateId) {
    try {
        const response = await fetch(`/api/overlays/layouts/${templateId}/duplicate`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
        });
        const data = await response.json();
        if (data.success) {
            loadTemplates();
        } else {
            showPopup({ type: 'error', title: 'Error', message: 'Failed to duplicate: ' + (data.error || 'Unknown error'), autoClose: 5000 });
        }
    } catch (e) {
        showPopup({ type: 'error', title: 'Error', message: 'Failed to duplicate layout', autoClose: 5000 });
    }
}

async function exportTemplate(templateId) {
    try {
        const response = await fetch(`/api/overlays/layouts/${templateId}/export`);
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            showPopup({ type: 'error', title: 'Error', message: 'Failed to export layout: ' + (data.error || response.statusText), autoClose: 5000 });
            return;
        }
        const blob = await response.blob();
        const disposition = response.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename="?([^"]+)"?/);
        const filename = match ? match[1] : `layout_${templateId}.json`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        a.click();
        URL.revokeObjectURL(url);
    } catch (error) {
        console.error('Export failed:', error);
        showPopup({ type: 'error', title: 'Error', message: 'Failed to export layout', autoClose: 5000 });
    }
}

async function deleteTemplate(templateId) {
    showPopup({
        type: 'confirm',
        title: 'Delete Layout',
        message: 'Are you sure you want to delete this layout?',
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            try {
                const response = await fetch(`/api/overlays/layouts/${templateId}`, {
                    method: 'DELETE'
                });

                const data = await response.json();
                if (data.success) {
                    showPopup({ type: 'success', title: 'Success', message: 'Layout deleted successfully', autoClose: 4000 });
                    loadTemplates(); // Reload templates list
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Failed to delete layout: ' + (data.error || 'Unknown error'), autoClose: 5000 });
                }
            } catch (error) {
                console.error('Delete failed:', error);
                showPopup({ type: 'error', title: 'Error', message: 'Failed to delete layout', autoClose: 5000 });
            }
        }
    });
}

async function generateAll(mediaType) {
    const templateSelector = document.getElementById(`${mediaType === 'movie' ? 'movies' : 'shows'}-layout-selector`);
    const layoutId = templateSelector.value ? parseInt(templateSelector.value) : null;

    if (!layoutId) { showPopup({ type: 'warning', title: 'Required', message: 'Please select a layout first', autoClose: 4000 }); return; }
    showPopup({
        type: 'confirm',
        title: 'Generate Overlays',
        message: `Generate overlays for all ${mediaType === 'movie' ? 'movies' : 'TV shows'}?`,
        confirmText: 'Generate',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const label = mediaType === 'movie' ? 'movies' : 'TV shows';
            _showProgress(`Generating ${label}…`, 'Starting job…');
            try {
                const response = await fetch('/api/overlays/generate/all', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({media_type: mediaType, layout_id: layoutId})
                });
                const data = await response.json();
                if (!data.success) {
                    _hideProgress();
                    showPopup({ type: 'error', title: 'Error', message: 'Failed to start: ' + (data.error || 'Unknown error'), autoClose: 5000 });
                    return;
                }
                document.getElementById('task-progress-title').textContent = data.label || `Generating ${label}…`;
                _pollUntilDone(
                    '/api/overlays/generate/status',
                    s => {
                        _updateProgressCounter(s.done, s.total,
                            [s.failed > 0 ? `${s.failed} failed` : '', (s.current || '').substring(0, 40)].filter(Boolean).join(' · '));
                    },
                    s => {
                        _hideProgress();
                        showPopup({ type: 'success', title: 'Success', message: `Generated ${s.applied} overlay${s.applied !== 1 ? 's' : ''}${s.failed > 0 ? `, ${s.failed} failed` : ''}`, autoClose: 4000 });
                        _refreshGrids(mediaType);
                    }
                );
            } catch (error) {
                _hideProgress();
                console.error('Generate all failed:', error);
                showPopup({ type: 'error', title: 'Error', message: 'Failed to start generation', autoClose: 5000 });
            }
        }
    });
}

function toggleRegenerateMenu(event) {
    if (event) event.stopPropagation();
    const menu = document.getElementById('regenerate-scope-menu');
    const isOpen = menu.style.display !== 'none';
    menu.style.display = isOpen ? 'none' : 'block';
    if (!isOpen) {
        const close = function(e) {
            if (!document.getElementById('regenerate-all-wrapper').contains(e.target)) {
                menu.style.display = 'none';
                document.removeEventListener('click', close);
            }
        };
        setTimeout(() => document.addEventListener('click', close), 0);
    }
}

async function regenerateLibrary(scope) {
    // scope: 'all' | 'movie' | 'tv'  (defaults to 'all' for backwards compatibility)
    if (!scope) scope = 'all';
    const scopeLabels = { all: 'movies, shows and seasons', movie: 'movies', tv: 'shows and seasons' };
    const label = scopeLabels[scope] || scopeLabels.all;

    showPopup({
        type: 'confirm',
        title: 'Regenerate Library',
        message: `Regenerate ALL overlays for ${label}? This will overwrite all existing overlays.`,
        confirmText: 'Regenerate',
        cancelText: 'Cancel',
        onConfirm: async function() {
            // Run movies first, then TV sequentially (can't run in parallel since they share the same job state)
            const btn = document.getElementById('regenerate-all-btn');
            btn.disabled = true;

            try {
                if (scope === 'all' || scope === 'movie') {
                    _showProgress('Regenerating movies…', 'Starting movies…');
                    const movieResp = await fetch('/api/overlays/generate/regenerate-all', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({media_type: 'movie', force: true})
                    });
                    const movieData = await movieResp.json();
                    if (!movieData.success) {
                        _hideProgress();
                        showPopup({ type: 'error', title: 'Error', message: 'Failed to start regeneration: ' + (movieData.error || 'Unknown error'), autoClose: 5000 });
                        btn.disabled = false;
                        return;
                    }
                    document.getElementById('task-progress-title').textContent = movieData.label || 'Regenerating movies…';
                    await new Promise(resolve => {
                        _pollUntilDone('/api/overlays/generate/status',
                            s => _updateProgressCounter(s.done, s.total, [s.failed > 0 ? `${s.failed} failed` : '', (s.current || '').substring(0, 40)].filter(Boolean).join(' · ')),
                            resolve
                        );
                    });
                    if (scope === 'movie') {
                        _hideProgress();
                        showPopup({ type: 'success', title: 'Success', message: 'Movie overlay regeneration complete', autoClose: 4000 });
                        _refreshGrids(null);
                        btn.disabled = false;
                        return;
                    }
                }

                // Phase: TV shows
                _showProgress('Regenerating TV shows…', 'Starting shows…');
                const tvResp = await fetch('/api/overlays/generate/regenerate-all', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({media_type: 'tv', force: true})
                });
                const tvData = await tvResp.json();
                if (!tvData.success) {
                    _hideProgress();
                    const prefix = scope === 'all' ? 'Movie regeneration done. ' : '';
                    showPopup({ type: 'error', title: 'Error', message: prefix + 'Failed to start TV: ' + (tvData.error || 'Unknown error'), autoClose: 5000 });
                    _refreshGrids(null);
                    btn.disabled = false;
                    return;
                }
                document.getElementById('task-progress-title').textContent = tvData.label || 'Regenerating TV shows…';
                await new Promise(resolve => {
                    _pollUntilDone('/api/overlays/generate/status',
                        s => _updateProgressCounter(s.done, s.total, [s.failed > 0 ? `${s.failed} failed` : '', (s.current || '').substring(0, 40)].filter(Boolean).join(' · ')),
                        resolve
                    );
                });

                _hideProgress();
                const doneMsg = scope === 'all' ? 'Regeneration complete for entire library' : 'Show overlay regeneration complete';
                showPopup({ type: 'success', title: 'Success', message: doneMsg, autoClose: 4000 });
                _refreshGrids(null);
            } catch (error) {
                _hideProgress();
                console.error('Regenerate library failed:', error);
                showPopup({ type: 'error', title: 'Error', message: 'Regeneration encountered an error — check logs', autoClose: 5000 });
            } finally {
                btn.disabled = false;
                chevron.disabled = false;
            }
        }
    });
}

async function regenerateAll(mediaType) {
    const templateSelector = document.getElementById(`${mediaType === 'movie' ? 'movies' : 'shows'}-layout-selector`);
    const layoutId = templateSelector.value ? parseInt(templateSelector.value) : null;

    showPopup({
        type: 'confirm',
        title: 'Regenerate All Overlays',
        message: `Regenerate ALL overlays for ${mediaType === 'movie' ? 'movies' : 'TV shows'}? This will overwrite existing overlays.`,
        confirmText: 'Regenerate',
        cancelText: 'Cancel',
        onConfirm: async function() {
            _showProgress(`Regenerating ${mediaType === 'movie' ? 'movies' : 'TV shows'}…`, 'Starting job…');
            try {
                const response = await fetch('/api/overlays/generate/regenerate-all', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({media_type: mediaType, layout_id: layoutId, force: true})
                });
                const data = await response.json();
                if (!data.success) {
                    _hideProgress();
                    showPopup({ type: 'error', title: 'Error', message: 'Failed to start: ' + (data.error || 'Unknown error'), autoClose: 5000 });
                    return;
                }
                document.getElementById('task-progress-title').textContent = data.label || `Regenerating ${mediaType === 'movie' ? 'movies' : 'TV shows'}…`;
                _pollUntilDone(
                    '/api/overlays/generate/status',
                    s => _updateProgressCounter(s.done, s.total, [s.failed > 0 ? `${s.failed} failed` : '', (s.current || '').substring(0, 40)].filter(Boolean).join(' · ')),
                    s => {
                        _hideProgress();
                        showPopup({ type: 'success', title: 'Success', message: `Regenerated ${s.applied} overlay${s.applied !== 1 ? 's' : ''}${s.failed > 0 ? `, ${s.failed} failed` : ''}`, autoClose: 4000 });
                        _refreshGrids(mediaType);
                    }
                );
            } catch (error) {
                _hideProgress();
                console.error('Regenerate all failed:', error);
                showPopup({ type: 'error', title: 'Error', message: 'Failed to start regeneration', autoClose: 5000 });
            }
        }
    });
}

// Load badge stats for the Badges tab preview
async function loadBadgeStats() {
    try {
        const response = await fetch('/api/overlays/badges/stats');
        const data = await response.json();
        if (data.success) {
            document.getElementById('badge-stat-types').textContent = data.stats.total_types;
            document.getElementById('badge-stat-variations').textContent = data.stats.total_variations;
            document.getElementById('badge-stat-assets').textContent = data.stats.filled_variations;
            window.badgeStatsLoaded = true;
        }
    } catch (error) {
        console.error('Failed to load badge stats:', error);
    }
}

// ── Activity Tab ─────────────────────────────────────────────────────────────

const _ACTIVITY_PAGE_SIZE = 50;
let _activityOffset = 0;
let _activityTotal  = 0;

const _ACTION_LABELS = {
    overlay_sync:       'Overlay Sync',
    cleanup:            'Cleanup',
    generate:           'Generate',
    regenerate:         'Regenerate',
    generate_all:       'Generate All',
    regenerate_all:     'Regenerate All',
    batch_generate:     'Batch Generate',
    remove:             'Remove',
    remove_all:         'Remove All',
    layout_create:      'Layout Created',
    layout_update:      'Layout Updated',
    layout_delete:      'Layout Deleted',
    layout_duplicate:   'Layout Duplicated',
    sync_library:       'Sync Library',
    season_generate:    'Season Generate',
    season_regenerate:  'Season Regenerate',
    season_remove:      'Season Remove',
    poster_reset:       'Poster Reset',
    full_sync:          'Full Sync',
};

const _RESULT_COLORS = {
    success: '#28a745',
    failed:  '#dc3545',
    partial: '#fd7e14',
};

function _formatActivityStats(stats) {
    if (!stats || typeof stats !== 'object') return '';
    const parts = [];
    const order = ['applied','generated','regenerated','removed','restored','reset',
                   'seasons_applied','skipped','seasons_skipped',
                   'failed','seasons_failed','errors','total_deleted','total_mb',
                   'movies','shows','seasons'];
    const labels = {
        applied: 'applied', generated: 'generated', regenerated: 'regenerated',
        removed: 'removed', restored: 'restored', reset: 'reset', skipped: 'skipped',
        seasons_applied: 'seasons applied', seasons_skipped: 'seasons skipped',
        failed: 'failed', seasons_failed: 'seasons failed', errors: 'errors',
        total_deleted: 'cleaned', total_mb: 'MB freed',
        movies: 'movies', shows: 'shows', seasons: 'seasons',
    };
    for (const k of order) {
        if (stats[k] !== undefined && stats[k] !== null && stats[k] !== 0) {
            parts.push(`${stats[k]} ${labels[k] || k}`);
        }
    }
    // Any extra keys not in the ordered list (skip internal-only keys)
    const _skip = new Set(['total','media_item_id','layout_id','name','media_type','type',
                           'status','show_key','season_plex_key','duplicated_from',
                           'movies_updated','episodes_updated','failures']);
    for (const k of Object.keys(stats)) {
        if (!order.includes(k) && !_skip.has(k) && stats[k] !== undefined && stats[k] !== null && stats[k] !== 0) {
            parts.push(`${stats[k]} ${k}`);
        }
    }
    return parts.join(' · ');
}

function _formatActivityFailures(stats) {
    if (!stats || !Array.isArray(stats.failures) || stats.failures.length === 0) return '';
    const items = stats.failures.map(f => `<span style="display:block;margin-top:1px;">${_escHtml(String(f))}</span>`).join('');
    return `<span style="display:block;margin-top:3px;font-size:11px;color:#dc3545;">`
         + `<strong>Failures:</strong>${items}</span>`;
}

function _escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _formatActivityDate(ts) {
    if (!ts) return '';
    const d = new Date(ts.endsWith('Z') ? ts : ts + 'Z');
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} `
         + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function _formatDuration(seconds) {
    if (seconds == null) return '—';
    seconds = Math.round(seconds);
    if (seconds < 1) return '< 1s';
    if (seconds < 60) return `${seconds}s`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
}

async function loadActivity(reset) {
    if (reset) _activityOffset = 0;
    const container = document.getElementById('activity-log-container');
    const pagination = document.getElementById('activity-pagination');
    const typeFilter = document.getElementById('activity-type-filter').value;

    container.innerHTML = '<div class="loading">Loading activity…</div>';

    try {
        const params = new URLSearchParams({
            limit:  _ACTIVITY_PAGE_SIZE,
            offset: _activityOffset,
        });
        if (typeFilter) params.set('type', typeFilter);

        const resp = await fetch('/api/overlays/activity?' + params);
        const data = await resp.json();
        if (!data.success) throw new Error(data.error || 'Request failed');

        _activityTotal = data.total;
        const rows = data.activities;

        if (!rows.length) {
            container.innerHTML = '<p style="color:var(--text-secondary,#666);padding:20px 0;">No activity recorded yet.</p>';
            pagination.style.display = 'none';
            return;
        }

        let html = `<table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead>
                <tr style="border-bottom:2px solid var(--border-color,#ddd);text-align:left;">
                    <th style="padding:8px 12px;white-space:nowrap;color:var(--text-secondary,#888);font-weight:600;width:155px;">Date / Time</th>
                    <th style="padding:8px 12px;color:var(--text-secondary,#888);font-weight:600;width:140px;">Action</th>
                    <th style="padding:8px 12px;color:var(--text-secondary,#888);font-weight:600;width:80px;">By</th>
                    <th style="padding:8px 12px;color:var(--text-secondary,#888);font-weight:600;width:70px;">Result</th>
                    <th style="padding:8px 12px;color:var(--text-secondary,#888);font-weight:600;width:90px;">Duration</th>
                    <th style="padding:8px 12px;color:var(--text-secondary,#888);font-weight:600;">Details</th>
                </tr>
            </thead>
            <tbody>`;

        rows.forEach((row, idx) => {
            const bg = idx % 2 === 0 ? 'transparent' : 'rgba(128,128,128,0.04)';
            const label  = _ACTION_LABELS[row.action_type] || row.action_type;
            const rColor = _RESULT_COLORS[row.result] || '#888';
            const statsStr    = _formatActivityStats(row.stats);
            const failuresStr = _formatActivityFailures(row.stats);
            const detail = row.title || statsStr || '—';
            const sub    = row.title && statsStr ? `<br><span style="font-size:11px;color:var(--text-secondary,#888);">${statsStr}</span>` : '';
            const failSub = failuresStr ? failuresStr : '';
            const triggeredLabel = row.triggered_by === 'scheduled' ? '⏱ auto' : '✋ manual';
            const durationStr = _formatDuration(row.duration_seconds);
            html += `<tr style="border-bottom:1px solid var(--border-color,#eee);background:${bg};">
                <td style="padding:8px 12px;white-space:nowrap;font-family:monospace;font-size:12px;color:var(--text-secondary,#888);">${_formatActivityDate(row.created_at)}</td>
                <td style="padding:8px 12px;font-weight:500;">${label}</td>
                <td style="padding:8px 12px;font-size:12px;color:var(--text-secondary,#888);">${triggeredLabel}</td>
                <td style="padding:8px 12px;"><span style="color:${rColor};font-weight:600;font-size:12px;">${row.result}</span></td>
                <td style="padding:8px 12px;white-space:nowrap;font-size:12px;color:var(--text-secondary,#888);">${durationStr}</td>
                <td style="padding:8px 12px;">${detail}${sub}${failSub}</td>
            </tr>`;
        });

        html += '</tbody></table>';
        container.innerHTML = html;

        // Pagination
        const totalPages = Math.ceil(_activityTotal / _ACTIVITY_PAGE_SIZE);
        const currentPage = Math.floor(_activityOffset / _ACTIVITY_PAGE_SIZE) + 1;
        if (totalPages > 1) {
            pagination.style.display = 'flex';
            pagination.style.justifyContent = 'center';
            pagination.style.alignItems = 'center';
            document.getElementById('activity-page-info').textContent =
                `Page ${currentPage} of ${totalPages} (${_activityTotal} events)`;
            document.getElementById('activity-prev-btn').disabled = _activityOffset === 0;
            document.getElementById('activity-next-btn').disabled = _activityOffset + _ACTIVITY_PAGE_SIZE >= _activityTotal;
        } else {
            pagination.style.display = 'none';
        }
    } catch (err) {
        container.innerHTML = `<p style="color:#dc3545;">Failed to load activity: ${err.message}</p>`;
        console.error('Activity load error:', err);
    }
}

function activityPage(dir) {
    _activityOffset = Math.max(0, _activityOffset + dir * _ACTIVITY_PAGE_SIZE);
    loadActivity(false);
}

// Re-load when the filter changes
document.getElementById('activity-type-filter')?.addEventListener('change', () => loadActivity(true));

// ─────────────────────────────────────────────────────────────────────────────

// Check if a sync is already running when page loads
async function checkInitialSyncStatus() {
    try {
        const resp = await fetch('/api/overlays/sync_library/status');
        const data = await resp.json();
        if (data.running) {
            const btn = document.getElementById('sync-library-btn');
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Syncing…';
            document.getElementById('sync-library-status').textContent = data.progress || '…';
            _syncPollTimer = setInterval(_pollSyncStatus, 2000);
        } else if (data.done && data.progress) {
            document.getElementById('sync-library-status').textContent = data.progress;
        }
    } catch (e) { /* ignore */ }
}

// Active status filter state
let _activeStatusFilter = null;

// Debounce helper
function _debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function filterGrid(gridId, query) {
    // Re-render from in-memory data — no DOM traversal over thousands of cards
    if (gridId === 'movies-grid') {
        _renderGrid('movies-grid', _moviesData, _renderMovieCard, query, _activeStatusFilter);
    } else if (gridId === 'shows-grid') {
        _renderGrid('shows-grid', _showsData, _renderShowCard, query, _activeStatusFilter);
    }
}

function filterByStatus(status) {
    // Toggle off if already active
    if (_activeStatusFilter === status) {
        _activeStatusFilter = null;
        document.querySelectorAll('.stat-card-clickable').forEach(c => c.classList.remove('stat-filter-active'));
        const mq = document.getElementById('movies-search')?.value || '';
        const sq = document.getElementById('shows-search')?.value || '';
        if (window.moviesLoaded) _renderGrid('movies-grid', _moviesData, _renderMovieCard, mq, null);
        if (window.showsLoaded)  _renderGrid('shows-grid',  _showsData,  _renderShowCard,  sq, null);
        return;
    }

    _activeStatusFilter = status;

    // Highlight the active stat card
    document.querySelectorAll('.stat-card-clickable').forEach(c => c.classList.remove('stat-filter-active'));
    const _statusElId = status === 'not_started' ? 'stat-unprocessed' : 'stat-failed';
    document.getElementById(_statusElId)?.closest('.stat-card-clickable')?.classList.add('stat-filter-active');

    // Re-render both grids with new status filter (uses in-memory data, fast)
    const mq = document.getElementById('movies-search')?.value || '';
    const sq = document.getElementById('shows-search')?.value || '';
    if (window.moviesLoaded) _renderGrid('movies-grid', _moviesData, _renderMovieCard, mq, status);
    if (window.showsLoaded)  _renderGrid('shows-grid',  _showsData,  _renderShowCard,  sq, status);

    // Decide which tab to show based on match counts (in-memory, no DOM query)
    const movieMatches = _moviesData.filter(i => i.overlay_status === status).length;
    const showMatches  = _showsData.filter(i =>
        i.overlay_status === status ||
        ((status === 'not_started' || status === 'failed') && (i.seasons_unprocessed || []).length > 0)
    ).length;
    const targetTab = (showMatches > 0 && movieMatches === 0) ? 'shows' : 'movies';

    document.querySelectorAll('.overlay-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector(`.overlay-tab[data-tab="${targetTab}"]`)?.classList.add('active');
    document.getElementById(`${targetTab}-tab`)?.classList.add('active');

    // Load data if not yet loaded
    if (targetTab === 'movies' && !window.moviesLoaded) loadMovies();
    if (targetTab === 'shows'  && !window.showsLoaded)  loadShows();
}

// ─── Season overlay modal ────────────────────────────────────────────────────

let _seasonsShowKey   = null;
let _seasonsShowTitle = null;
let _seasonsData      = [];

function _seasonStatusClass(status) {
    const map = {
        applied: 'status-applied',
        pending: 'status-pending',
        analyzing: 'status-analyzing',
        failed: 'status-failed',
        removed: 'status-not-started',
        not_started: 'status-not-started',
    };
    return map[status] || 'status-not-started';
}

async function openSeasonsModal(showRatingKey, showTitle) {
    _seasonsShowKey   = showRatingKey;
    _seasonsShowTitle = showTitle;
    _seasonsData      = [];

    document.getElementById('seasons-modal-title').textContent =
        `Season Posters — ${showTitle}`;
    document.getElementById('seasons-modal-body').innerHTML =
        '<div class="loading">Loading seasons…</div>';
    document.getElementById('seasons-modal-backdrop').classList.add('open');

    // Populate layout selector with season/tv layouts
    const sel = document.getElementById('seasons-layout-selector');
    sel.innerHTML = '<option value="">Auto (use best available layout)</option>';
    try {
        const lr = await fetch('/api/overlays/layouts');
        const ld = await lr.json();
        if (ld.success) {
            ld.layouts
                .filter(l => l.media_type === 'season' || l.media_type === 'tv')
                .forEach(l => {
                    const opt = document.createElement('option');
                    opt.value = l.id;
                    opt.textContent = `${l.name} (${l.media_type})`;
                    sel.appendChild(opt);
                });
        }
    } catch (_e) {}

    await _loadSeasonsIntoModal();
}

async function _loadSeasonsIntoModal() {
    const body = document.getElementById('seasons-modal-body');
    try {
        const resp = await fetch(`/api/overlays/seasons/${_seasonsShowKey}`);
        const data = await resp.json();

        if (!data.success) {
            body.innerHTML = `<p style="color:#dc3545">Error: ${data.error || 'Unknown error'}</p>`;
            return;
        }
        if (!data.seasons || data.seasons.length === 0) {
            body.innerHTML = '<p class="loading">No seasons found for this show.</p>';
            return;
        }

        _seasonsData = data.seasons;
        _renderSeasonsModal();

    } catch (e) {
        body.innerHTML = `<p style="color:#dc3545">Failed to load seasons: ${e}</p>`;
    }
}

function _renderSeasonsModal() {
    const body = document.getElementById('seasons-modal-body');
    body.innerHTML = _seasonsData.map(s => {
        const statusClass = _seasonStatusClass(s.overlay_status);
        const thumbUrl = `/api/overlays/seasons/${s.season_ms_item_id}/thumb`;
        const displayStatus = (s.overlay_status || 'not started').replace('_', ' ');
        const isApplied  = s.overlay_status === 'applied';
        const isRemoved  = s.overlay_status === 'removed' || s.overlay_status === 'not_started';

        return `
        <div class="season-row" id="season-row-${s.season_ms_item_id}">
            <img class="season-thumb"
                 src="${thumbUrl}"
                 onerror="this.src='/static/images/placeholder.png'"
                 alt="${s.title}">
            <div class="season-info">
                <div class="season-title">${s.title}</div>
                <span class="season-status-badge overlay-status ${statusClass}">${displayStatus}</span>
            </div>
            <div class="season-actions">
                ${isApplied ? `
                <button class="btn btn-warning"
                    onclick="regenSingleSeason('${s.season_ms_item_id}', ${s.season_number}, this)"
                    title="Regenerate with current library poster">↻</button>
                <button class="btn btn-danger"
                    onclick="removeSingleSeason('${s.season_ms_item_id}', ${s.season_number}, this)"
                    title="Remove overlay">✕</button>
                ` : `
                <button class="btn btn-success"
                    onclick="generateSingleSeason('${s.season_ms_item_id}', ${s.season_number}, this)"
                    title="Apply overlay">Apply</button>
                `}
            </div>
        </div>`;
    }).join('');
}

function closeSeasonsModal() {
    document.getElementById('seasons-modal-backdrop').classList.remove('open');
    _seasonsShowKey = null;
}

async function generateSingleSeason(seasonKey, seasonNumber, btn) {
    const layoutId = _getSeasonsLayoutId();
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = '…';
    try {
        const resp = await fetch(
            `/api/overlays/seasons/${_seasonsShowKey}/${seasonKey}/regenerate`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({season_number: seasonNumber, layout_id: layoutId})
        });
        const data = await resp.json();
        if (data.success) {
            await _loadSeasonsIntoModal();
        } else {
            btn.textContent = orig; btn.disabled = false;
            showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + (data.message || data.error || 'Unknown error'), autoClose: 5000 });
        }
    } catch (e) {
        btn.textContent = orig; btn.disabled = false;
        showPopup({ type: 'error', title: 'Error', message: 'Request failed: ' + e, autoClose: 5000 });
    }
}

async function regenSingleSeason(seasonKey, seasonNumber, btn) {
    showPopup({
        type: 'confirm',
        title: 'Regenerate Season',
        message: 'Regenerate season overlay using current library poster?',
        confirmText: 'Regenerate',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const layoutId = _getSeasonsLayoutId();
            const orig = btn.textContent;
            btn.disabled = true; btn.textContent = '…';
            try {
                const resp = await fetch(
                    `/api/overlays/seasons/${_seasonsShowKey}/${seasonKey}/regenerate`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({season_number: seasonNumber, layout_id: layoutId})
                });
                const data = await resp.json();
                if (data.success) {
                    await _loadSeasonsIntoModal();
                } else {
                    btn.textContent = orig; btn.disabled = false;
                    showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + (data.message || data.error || 'Unknown error'), autoClose: 5000 });
                }
            } catch (e) {
                btn.textContent = orig; btn.disabled = false;
                showPopup({ type: 'error', title: 'Error', message: 'Request failed: ' + e, autoClose: 5000 });
            }
        }
    });
}

async function removeSingleSeason(seasonKey, seasonNumber, btn) {
    showPopup({
        type: 'confirm',
        title: 'Remove Season Overlay',
        message: 'Remove overlay and restore original season poster?',
        confirmText: 'Remove',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const orig = btn.textContent;
            btn.disabled = true; btn.textContent = '…';
            try {
                const resp = await fetch(
                    `/api/overlays/seasons/${_seasonsShowKey}/${seasonKey}/remove`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({season_number: seasonNumber})
                });
                const data = await resp.json();
                if (data.success) {
                    await _loadSeasonsIntoModal();
                } else {
                    btn.textContent = orig; btn.disabled = false;
                    showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + (data.message || data.error || 'Unknown error'), autoClose: 5000 });
                }
            } catch (e) {
                btn.textContent = orig; btn.disabled = false;
                showPopup({ type: 'error', title: 'Error', message: 'Request failed: ' + e, autoClose: 5000 });
            }
        }
    });
}

async function generateAllSeasons() {
    if (!_seasonsShowKey) return;
    showPopup({
        type: 'confirm',
        title: 'Generate Season Overlays',
        message: `Generate season poster overlays for all seasons of "${_seasonsShowTitle}"?`,
        confirmText: 'Generate',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const layoutId = _getSeasonsLayoutId();
            const btn = document.querySelector('#seasons-modal-controls .btn-success');
            const orig = btn.textContent; btn.disabled = true; btn.textContent = '…';
            try {
                const resp = await fetch(`/api/overlays/seasons/${_seasonsShowKey}/generate`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({layout_id: layoutId, force: false})
                });
                const data = await resp.json();
                if (data.success) {
                    showPopup({ type: 'success', title: 'Success', message: `Applied: ${data.applied}, Skipped: ${data.skipped}, Failed: ${data.failed}`, autoClose: 4000 });
                    await _loadSeasonsIntoModal();
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + (data.error || 'Unknown error'), autoClose: 5000 });
                }
            } catch (e) {
                showPopup({ type: 'error', title: 'Error', message: 'Request failed: ' + e, autoClose: 5000 });
            } finally {
                btn.textContent = orig; btn.disabled = false;
            }
        }
    });
}

async function regenerateAllSeasons() {
    if (!_seasonsShowKey) return;
    showPopup({
        type: 'confirm',
        title: 'Regenerate All Season Overlays',
        message: `Regenerate ALL season poster overlays for "${_seasonsShowTitle}"? This will overwrite existing season overlays.`,
        confirmText: 'Regenerate',
        cancelText: 'Cancel',
        onConfirm: async function() {
            const layoutId = _getSeasonsLayoutId();
            const btn = document.querySelector('#seasons-modal-controls .btn-warning');
            const orig = btn.textContent; btn.disabled = true; btn.textContent = '…';
            try {
                const resp = await fetch(`/api/overlays/seasons/${_seasonsShowKey}/generate`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({layout_id: layoutId, force: true})
                });
                const data = await resp.json();
                if (data.success) {
                    showPopup({ type: 'success', title: 'Success', message: `Applied: ${data.applied}, Skipped: ${data.skipped}, Failed: ${data.failed}`, autoClose: 4000 });
                    await _loadSeasonsIntoModal();
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + (data.error || 'Unknown error'), autoClose: 5000 });
                }
            } catch (e) {
                showPopup({ type: 'error', title: 'Error', message: 'Request failed: ' + e, autoClose: 5000 });
            } finally {
                btn.textContent = orig; btn.disabled = false;
            }
        }
    });
}

function _getSeasonsLayoutId() {
    const val = document.getElementById('seasons-layout-selector').value;
    return val ? parseInt(val) : null;
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    // Restore last-visited tab
    const _savedTab = sessionStorage.getItem('overlays_active_tab');
    if (_savedTab && document.querySelector(`.overlay-tab[data-tab="${_savedTab}"]`)) {
        document.querySelectorAll('.overlay-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        document.querySelector(`.overlay-tab[data-tab="${_savedTab}"]`).classList.add('active');
        document.getElementById(`${_savedTab}-tab`)?.classList.add('active');
    }

    loadStats();
    loadTemplates(); // Always populate layout dropdowns on page load
    // Load the content for the active tab
    if (!_savedTab || _savedTab === 'movies') loadMovies();
    else if (_savedTab === 'shows')     loadShows();
    else if (_savedTab === 'badges')    loadBadgeStats();
    else if (_savedTab === 'activity')  loadActivity();
    checkInitialSyncStatus();

    // Close seasons modal when clicking the backdrop
    document.getElementById('seasons-modal-backdrop').addEventListener('click', function(e) {
        if (e.target === this) closeSeasonsModal();
    });

    // Wire up search boxes — debounced so typing fast doesn't re-render on every keystroke
    const _debouncedMovieFilter = _debounce(q => filterGrid('movies-grid', q), 250);
    const _debouncedShowFilter  = _debounce(q => filterGrid('shows-grid',  q), 250);
    document.getElementById('movies-search').addEventListener('input', e => _debouncedMovieFilter(e.target.value));
    document.getElementById('shows-search').addEventListener('input',  e => _debouncedShowFilter(e.target.value));

    // Refresh stats every 30 seconds — paused while tab is hidden
    let _statsTimer = setInterval(loadStats, 30000);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            clearInterval(_statsTimer);
            _statsTimer = null;
        } else {
            if (!_statsTimer) {
                loadStats();  // immediate refresh when tab becomes visible again
                _statsTimer = setInterval(loadStats, 30000);
            }
        }
    });
});
