/* Moved from templates/overlays.html. */
// ═══════════════════════════════════════════════════════════════════════════
// Phase 2 — Poster Review Grid
// ═══════════════════════════════════════════════════════════════════════════

// Move posterReviewModal to <body> so position:fixed works correctly
// (fixed positioning breaks when any ancestor has transform/filter/will-change)
(function() {
    const m = document.getElementById('posterReviewModal');
    if (m && m.parentElement !== document.body) document.body.appendChild(m);
})();

let _reviewItems    = [];        // full list from API
let _reviewFiltered = [];        // after search filter
let _reviewSelected = new Set(); // selected ms_item_ids
const _REVIEW_PAGE  = 300;       // max cards rendered at once

// Expose as both names so overlay_settings_modal.js delegates here when on this page
window._openPosterReviewGridLocal = window.openPosterReviewGrid = async function() {
    const modal = document.getElementById('posterReviewModal');
    if (!modal) return;
    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';
    _reviewSelected.clear();
    _updateReviewSelCount();
    document.getElementById('posterReviewSearch').value = '';

    const grid = document.getElementById('posterReviewGrid');
    grid.innerHTML = '<div class="poster-review-loading">Loading library…</div>';

    try {
        const r = await fetch('/api/overlays/reset/preview');
        const d = await r.json();
        if (!d.success) throw new Error(d.error || 'Failed to load');
        _reviewItems = d.items || [];
        _reviewFiltered = _reviewItems.slice();
        document.getElementById('posterReviewTotalCount').textContent =
            `${_reviewItems.length} item${_reviewItems.length !== 1 ? 's' : ''} in library`;
        _renderReviewGrid();
    } catch (e) {
        grid.innerHTML = `<div class="poster-review-loading" style="color:#dc3545;">Error: ${e.message}</div>`;
    }
};

window.closePosterReviewGrid = function() {
    const modal = document.getElementById('posterReviewModal');
    if (modal) modal.style.display = 'none';
    document.body.style.overflow = '';
};

// Debounced search — avoids re-rendering on every keystroke
const _filterReviewGridDebounced = _debounce(function() {
    const q = document.getElementById('posterReviewSearch').value.toLowerCase().trim();
    _reviewFiltered = q
        ? _reviewItems.filter(i => (i.title || '').toLowerCase().includes(q))
        : _reviewItems.slice();
    _renderReviewGrid();
}, 250);
window.filterReviewGrid = _filterReviewGridDebounced;

window.selectAllReview = function() {
    // Add all filtered items to selection without re-rendering the whole grid
    _reviewFiltered.forEach(i => _reviewSelected.add(i.ms_item_id || ''));
    // Just toggle CSS on already-rendered cards
    document.querySelectorAll('#posterReviewGrid .poster-review-card').forEach(card => {
        if (_reviewSelected.has(card.dataset.key)) {
            card.classList.add('poster-review-card--selected');
        }
    });
    _updateReviewSelCount();
};

window.deselectAllReview = function() {
    _reviewSelected.clear();
    document.querySelectorAll('#posterReviewGrid .poster-review-card').forEach(card => {
        card.classList.remove('poster-review-card--selected');
    });
    _updateReviewSelCount();
};

window.selectNonAppliedReview = function() {
    _reviewFiltered.forEach(i => {
        if (i.overlay_status !== 'applied') _reviewSelected.add(i.ms_item_id || '');
    });
    document.querySelectorAll('#posterReviewGrid .poster-review-card').forEach(card => {
        if (_reviewSelected.has(card.dataset.key)) {
            card.classList.add('poster-review-card--selected');
        }
    });
    _updateReviewSelCount();
};

window.submitReviewReset = function() {
    if (_reviewSelected.size === 0) return;
    const keys = Array.from(_reviewSelected);
    const msg = `Reset ${keys.length} selected poster(s) to clean TMDB originals?\n\nThis cannot be undone. Overlays will NOT be re-applied automatically.`;
    showPopup({
        type: 'confirm',
        title: 'Reset Posters',
        message: msg,
        confirmText: 'Reset',
        cancelText: 'Cancel',
        onConfirm: function() {
            closePosterReviewGrid();
            // Delegate to overlay_settings_modal.js which owns the progress UI
            if (typeof window._startSelectiveResetJob === 'function') {
                // Ensure settings modal is open so progress is visible
                if (typeof window.openOverlaySettingsModal === 'function') {
                    window.openOverlaySettingsModal().then
                        ? window.openOverlaySettingsModal().then(() => window._startSelectiveResetJob(keys))
                        : (window.openOverlaySettingsModal(), window._startSelectiveResetJob(keys));
                } else {
                    window._startSelectiveResetJob(keys);
                }
            }
        }
    });
};

function _toggleReviewItem(key) {
    if (_reviewSelected.has(key)) {
        _reviewSelected.delete(key);
    } else {
        _reviewSelected.add(key);
    }
    // Target the card directly via dataset — no full-DOM scan needed
    const card = document.querySelector(`#posterReviewGrid .poster-review-card[data-key="${CSS.escape(key)}"]`);
    if (card) {
        card.classList.toggle('poster-review-card--selected', _reviewSelected.has(key));
    }
    _updateReviewSelCount();
}

function _updateReviewSelCount() {
    const el = document.getElementById('posterReviewSelCount');
    if (el) el.textContent = `${_reviewSelected.size} selected`;
    const btn = document.getElementById('posterReviewResetBtn');
    if (btn) btn.disabled = _reviewSelected.size === 0;
}

function _statusBadge(status) {
    const map = {
        applied:     { label: 'Applied',     color: '#28a745' },
        pending:     { label: 'Pending',      color: '#fd7e14' },
        failed:      { label: 'Failed',       color: '#dc3545' },
        analyzing:   { label: 'Analyzing',    color: '#17a2b8' },
        not_started: { label: 'Not started',  color: '#6c757d' },
    };
    const s = map[status] || { label: status || 'Unknown', color: '#6c757d' };
    return `<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:${s.color}22;color:${s.color};border:1px solid ${s.color}44;">${s.label}</span>`;
}

function _renderReviewGrid() {
    const grid = document.getElementById('posterReviewGrid');
    if (!grid) return;

    if (_reviewFiltered.length === 0) {
        grid.innerHTML = '<div class="poster-review-loading">No items match.</div>';
        return;
    }

    const visible = _reviewFiltered.slice(0, _REVIEW_PAGE);
    const overflow = _reviewFiltered.length > _REVIEW_PAGE;

    const html = visible.map(item => {
        const key     = item.ms_item_id || '';
        const sel     = _reviewSelected.has(key);
        const poster  = item.tmdb_poster_url || '';
        const title   = _escHtml(item.title || '?');
        const year    = item.year ? ` (${item.year})` : '';
        const badge   = _statusBadge(item.overlay_status);
        return `
        <div class="poster-review-card${sel ? ' poster-review-card--selected' : ''}"
             data-key="${_escHtml(key)}"
             onclick="_toggleReviewItem('${_escHtml(key)}')">
            <div class="poster-review-img-wrap">
                ${poster
                    ? `<img src="${_escHtml(poster)}" alt="${title}" loading="lazy" class="poster-review-img">`
                    : `<div class="poster-review-img poster-review-img--placeholder">No image</div>`
                }
            </div>
            <div class="poster-review-info">
                <div class="poster-review-item-title" title="${title}${year}">${title}${year}</div>
                ${badge}
            </div>
        </div>`;
    }).join('');

    const overflowMsg = overflow
        ? `<div class="poster-review-loading" style="grid-column:1/-1;font-size:12px;color:#888;padding:8px 0;">
               Showing ${_REVIEW_PAGE} of ${_reviewFiltered.length} — use the search to narrow results
           </div>`
        : '';

    grid.innerHTML = html + overflowMsg;
    _updateReviewSelCount();
}

// Auto-open review grid if navigated here via hash from settings modal
if (window.location.hash === '#open-review-grid') {
    history.replaceState(null, '', window.location.pathname);
    window.addEventListener('load', () => window.openPosterReviewGrid(), { once: true });
}
