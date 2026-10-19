/**
 * Show Options Modal
 *
 * Per-show settings on the library show page: series type (automatic, anime,
 * or not anime). Backed by /library/show_options/<imdb_id>.
 *
 * Used by library_show.js via window.openShowOptionsModal({imdbId, title}).
 */

(function () {
    'use strict';

    let modal, statusEl, errorEl, seriesTypeEl, seriesTypeHintEl;
    let context = null;
    let busy = false;

    function initializeElements() {
        modal = document.getElementById('showOptionsModal');
        if (!modal) {
            console.error('[Show Options] Modal element not found in DOM');
            return false;
        }
        // Same stacking-context reason as fix_match_modal.js: live under <body>.
        if (modal.parentElement !== document.body) {
            document.body.appendChild(modal);
        }
        statusEl = document.getElementById('showOptionsStatus');
        errorEl = document.getElementById('showOptionsError');
        seriesTypeEl = document.getElementById('showOptionsSeriesType');
        seriesTypeHintEl = document.getElementById('showOptionsSeriesTypeHint');

        modal.querySelector('.show-options-overlay').addEventListener('click', close);
        modal.querySelector('.show-options-close').addEventListener('click', close);
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && modal.style.display !== 'none') close();
        });
        seriesTypeEl.querySelectorAll('.show-options-choice').forEach(function (btn) {
            btn.addEventListener('click', function () { setSeriesType(btn.dataset.value); });
        });
        return true;
    }

    function setMessage(el, text) {
        el.textContent = text || '';
        el.style.display = text ? '' : 'none';
    }

    function renderSeriesType(options) {
        seriesTypeEl.querySelectorAll('.show-options-choice').forEach(function (btn) {
            btn.classList.toggle('selected', btn.dataset.value === options.series_type);
            btn.disabled = busy;
        });
        let detected = 'unknown';
        if (options.auto_is_anime === true) detected = 'anime';
        if (options.auto_is_anime === false) detected = 'not anime';
        seriesTypeHintEl.textContent = 'Automatic detection says: ' + detected +
            '. Currently treated as ' + (options.is_anime ? 'anime' : 'not anime') + '.';
    }

    async function load(statusAfter) {
        setMessage(errorEl, '');
        setMessage(statusEl, 'Loading…');
        try {
            const resp = await fetch('/library/show_options/' + encodeURIComponent(context.imdbId));
            const data = await resp.json();
            if (!data.success) throw new Error(data.error || 'Could not load options');
            renderSeriesType(data);
            setMessage(statusEl, statusAfter || '');
        } catch (e) {
            setMessage(statusEl, '');
            setMessage(errorEl, e.message);
        }
    }

    async function setSeriesType(value) {
        if (busy) return;
        busy = true;
        setMessage(errorEl, '');
        setMessage(statusEl, 'Saving…');
        try {
            const resp = await fetch('/library/show_options/' + encodeURIComponent(context.imdbId) + '/series_type', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({series_type: value})
            });
            const data = await resp.json();
            if (!data.success) throw new Error(data.error || 'Could not save');
            busy = false;
            await load('Saved. ' + data.episodes_updated + ' episode entries updated.');
        } catch (e) {
            busy = false;
            setMessage(statusEl, '');
            setMessage(errorEl, e.message);
        }
    }

    function close() {
        if (modal) modal.style.display = 'none';
        context = null;
    }

    window.openShowOptionsModal = function (ctx) {
        if (!modal && !initializeElements()) return;
        if (!ctx || !ctx.imdbId) {
            console.error('[Show Options] An IMDb ID is required');
            return;
        }
        context = ctx;
        document.getElementById('showOptionsTitle').textContent = 'Show options' + (ctx.title ? ' — ' + ctx.title : '');
        modal.style.display = 'flex';
        load();
    };
})();
