/**
 * Fix Match Modal
 *
 * Corrects a library entry's IMDb, TMDB and TVDB IDs together. Only the IMDb ID
 * has to be chosen — the server resolves the other two from it — so picking a
 * search result (or pasting an ID) and hitting Apply fixes all three at once,
 * across media_items, the battery, and Plex's own match.
 *
 * Used by library_show.js and library_movie.js via window.openFixMatchModal().
 */

(function () {
    'use strict';

    let modal, overlay, closeBtn, cancelBtn, applyBtn;
    let searchInput, searchYearInput, searchBtn, resultsEl, searchStatusEl;
    let manualInput, manualBtn;
    let previewEl, previewTitleEl, previewImdbEl, previewTmdbEl, previewTvdbEl;
    let impactEl, rematchCheckbox, errorEl;
    let currentTitleEl, currentImdbEl, currentTmdbEl, currentTvdbEl;

    // The entry being fixed, as handed over by the page.
    let context = null;
    // The verified candidate from /fix_match/preview — Apply is enabled only once set.
    let candidate = null;

    function initializeElements() {
        modal = document.getElementById('fixMatchModal');
        if (!modal) {
            console.error('[Fix Match] Modal element not found in DOM');
            return false;
        }

        // The markup ships inside the page's content block, which renders in
        // <main> — and main is `position: relative; z-index: 1`, a stacking
        // context. Our z-index is scoped inside it, so the modal cannot rise
        // above the nav, toasts or the full-viewport #loading overlay, all of
        // which sit at 9999 in the root context. Every other modal here is a
        // direct child of <body>; move ours there so it behaves the same.
        if (modal.parentElement !== document.body) {
            document.body.appendChild(modal);
        }

        overlay = modal.querySelector('.fix-match-modal-overlay');
        closeBtn = modal.querySelector('.fix-match-modal-close');
        cancelBtn = modal.querySelector('.fix-match-btn-cancel');
        applyBtn = modal.querySelector('.fix-match-btn-apply');

        searchInput = document.getElementById('fixMatchSearchInput');
        searchYearInput = document.getElementById('fixMatchSearchYear');
        searchBtn = document.getElementById('fixMatchSearchBtn');
        resultsEl = document.getElementById('fixMatchResults');
        searchStatusEl = document.getElementById('fixMatchSearchStatus');

        manualInput = document.getElementById('fixMatchManualId');
        manualBtn = document.getElementById('fixMatchManualBtn');

        previewEl = document.getElementById('fixMatchPreview');
        previewTitleEl = document.getElementById('fixMatchPreviewTitle');
        previewImdbEl = document.getElementById('fixMatchPreviewImdb');
        previewTmdbEl = document.getElementById('fixMatchPreviewTmdb');
        previewTvdbEl = document.getElementById('fixMatchPreviewTvdb');
        impactEl = document.getElementById('fixMatchImpact');
        rematchCheckbox = document.getElementById('fixMatchRematchServer');
        errorEl = document.getElementById('fixMatchError');

        currentTitleEl = document.getElementById('fixMatchCurrentTitle');
        currentImdbEl = document.getElementById('fixMatchCurrentImdb');
        currentTmdbEl = document.getElementById('fixMatchCurrentTmdb');
        currentTvdbEl = document.getElementById('fixMatchCurrentTvdb');

        attachHandlers();
        return true;
    }

    function attachHandlers() {
        [overlay, closeBtn, cancelBtn].forEach(function (el) {
            if (el) el.addEventListener('click', closeModal);
        });
        if (searchBtn) searchBtn.addEventListener('click', runSearch);
        if (manualBtn) manualBtn.addEventListener('click', runManualLookup);
        if (applyBtn) applyBtn.addEventListener('click', applyFix);

        [searchInput, searchYearInput].forEach(function (el) {
            if (!el) return;
            el.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    runSearch();
                }
            });
        });
        if (manualInput) {
            manualInput.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    runManualLookup();
                }
            });
        }

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && modal && modal.style.display === 'flex') closeModal();
        });
    }

    function setError(message) {
        if (!errorEl) return;
        errorEl.textContent = message || '';
        errorEl.style.display = message ? 'block' : 'none';
    }

    function setStatus(message) {
        if (!searchStatusEl) return;
        searchStatusEl.textContent = message || '';
        searchStatusEl.style.display = message ? 'block' : 'none';
    }

    function clearCandidate() {
        candidate = null;
        if (previewEl) previewEl.style.display = 'none';
        if (applyBtn) applyBtn.disabled = true;
    }

    function displayId(value) {
        return value ? String(value) : '—';
    }

    /**
     * Show a resolved ID, highlighted when it differs from what the entry has now.
     */
    function renderResolvedId(el, newValue, currentValue) {
        if (!el) return;
        el.textContent = displayId(newValue);
        const changed = String(newValue || '') !== String(currentValue || '');
        el.classList.toggle('fix-match-id-changed', changed && Boolean(newValue));
    }

    /**
     * Open the modal for one library entry.
     *
     * @param {Object} entry
     * @param {'show'|'movie'} entry.mediaType
     * @param {string} entry.title
     * @param {number|string} [entry.year]
     * @param {string} [entry.imdbId]
     * @param {string|number} [entry.tmdbId]
     * @param {string|number} [entry.tvdbId]
     * @param {Function} [entry.onApplied]  Called with the apply response on success.
     */
    window.openFixMatchModal = function (entry) {
        if (!modal && !initializeElements()) return;

        context = Object.assign({ mediaType: 'movie' }, entry || {});

        const yearSuffix = context.year ? ' (' + context.year + ')' : '';
        currentTitleEl.textContent = (context.title || 'Unknown') + yearSuffix;
        currentImdbEl.textContent = displayId(context.imdbId);
        currentTmdbEl.textContent = displayId(context.tmdbId);
        currentTvdbEl.textContent = displayId(context.tvdbId);

        searchInput.value = context.title || '';
        searchYearInput.value = context.year || '';
        manualInput.value = '';
        resultsEl.innerHTML = '';
        resultsEl.style.display = 'none';
        rematchCheckbox.checked = true;
        applyBtn.textContent = 'Apply Fix';
        setStatus('');
        setError('');
        clearCandidate();

        modal.style.display = 'flex';

        // Only lock the page once the overlay is genuinely covering the
        // viewport. Without its styles the modal lays out in the page flow
        // instead, and locking the scroll then strands the reader on a page
        // they can neither see the modal on nor scroll.
        if (window.getComputedStyle(modal).position === 'fixed') {
            document.body.style.overflow = 'hidden';
        } else {
            console.error('[Fix Match] modal styles missing (the <style> block ' +
                'in fix_match_modal.html) — leaving the page scrollable');
        }

        // preventScroll: focusing the field must never move the page.
        searchInput.focus({ preventScroll: true });
        searchInput.select();
    };

    function closeModal() {
        if (!modal) return;
        modal.style.display = 'none';
        document.body.style.overflow = '';
    }

    async function runSearch() {
        const query = searchInput.value.trim();
        if (!query) {
            setError('Enter a title to search for');
            return;
        }

        setError('');
        setStatus('Searching…');
        resultsEl.innerHTML = '';
        resultsEl.style.display = 'none';
        searchBtn.disabled = true;

        try {
            const response = await fetch('/library/fix_match/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    query: query,
                    year: searchYearInput.value.trim() || null,
                    media_type: context.mediaType,
                }),
            });
            const data = await response.json();

            if (!data.success) {
                setStatus('');
                setError(data.error || 'Search failed');
                return;
            }
            if (!data.results.length) {
                setStatus('No results — try a different title, or enter the ID directly.');
                return;
            }

            setStatus('');
            renderResults(data.results);
        } catch (error) {
            setStatus('');
            setError('Search failed: ' + error.message);
        } finally {
            searchBtn.disabled = false;
        }
    }

    function renderResults(results) {
        resultsEl.innerHTML = '';

        results.forEach(function (result) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'fix-match-result';

            const label = document.createElement('span');
            label.textContent = (result.title || 'Unknown') +
                (result.year ? ' (' + result.year + ')' : '') +
                (result.type ? ' · ' + result.type : '');

            const ids = document.createElement('span');
            ids.className = 'fix-match-result-ids';
            ids.textContent = [result.imdb_id, result.tmdb_id ? 'tmdb ' + result.tmdb_id : null]
                .filter(Boolean).join(' · ') || 'no IDs';

            button.appendChild(label);
            button.appendChild(ids);
            button.addEventListener('click', function () {
                resultsEl.querySelectorAll('.fix-match-result').forEach(function (el) {
                    el.classList.remove('selected');
                });
                button.classList.add('selected');
                lookupCandidate({ imdb_id: result.imdb_id, tmdb_id: result.tmdb_id });
            });

            resultsEl.appendChild(button);
        });

        resultsEl.style.display = 'block';
    }

    function runManualLookup() {
        const raw = manualInput.value.trim();
        if (!raw) {
            setError('Enter an IMDb ID (tt1234567) or a TMDB ID');
            return;
        }

        resultsEl.querySelectorAll('.fix-match-result').forEach(function (el) {
            el.classList.remove('selected');
        });

        if (/^tt\d{7,10}$/i.test(raw)) {
            lookupCandidate({ imdb_id: raw.toLowerCase() });
        } else if (/^\d+$/.test(raw)) {
            lookupCandidate({ tmdb_id: raw });
        } else {
            setError('"' + raw + '" is neither an IMDb ID (tt1234567) nor a numeric TMDB ID');
        }
    }

    /**
     * Verify a candidate server-side and show what applying it would do.
     */
    async function lookupCandidate(ids) {
        setError('');
        setStatus('Resolving IDs…');
        clearCandidate();
        manualBtn.disabled = true;

        try {
            const response = await fetch('/library/fix_match/preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    imdb_id: ids.imdb_id || null,
                    tmdb_id: ids.tmdb_id || null,
                    media_type: context.mediaType,
                    current_imdb_id: context.imdbId || null,
                    current_tmdb_id: context.tmdbId || null,
                }),
            });
            const data = await response.json();

            setStatus('');
            if (!data.success) {
                setError(data.error || 'Could not resolve that ID');
                return;
            }

            candidate = data;
            previewTitleEl.textContent = (data.title || 'Unknown') +
                (data.year ? ' (' + data.year + ')' : '');
            renderResolvedId(previewImdbEl, data.imdb_id, context.imdbId);
            renderResolvedId(previewTmdbEl, data.tmdb_id, context.tmdbId);
            renderResolvedId(previewTvdbEl, data.tvdb_id, context.tvdbId);
            impactEl.textContent = buildImpactText(data);

            previewEl.style.display = 'block';
            applyBtn.disabled = false;
        } catch (error) {
            setStatus('');
            setError('Lookup failed: ' + error.message);
        } finally {
            manualBtn.disabled = false;
        }
    }

    function buildImpactText(data) {
        const rows = data.affected_rows || 0;
        if (!rows) {
            return 'No matching database rows found for this entry — the battery metadata will ' +
                'still be corrected.';
        }

        const parts = ['Will rewrite ' + rows + ' database row' + (rows === 1 ? '' : 's')];
        const titles = (data.affected_titles || [])
            .map(function (t) { return t.title + (t.year ? ' (' + t.year + ')' : ''); });
        if (titles.length) parts.push('currently titled ' + titles.join(', '));
        return parts.join(', ') + '.';
    }

    async function applyFix() {
        if (!candidate) return;

        const originalText = applyBtn.textContent;
        applyBtn.disabled = true;
        applyBtn.textContent = 'Applying…';
        setError('');

        try {
            const response = await fetch('/library/fix_match/apply', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    media_type: context.mediaType,
                    new_imdb_id: candidate.imdb_id,
                    current_imdb_id: context.imdbId || null,
                    current_tmdb_id: context.tmdbId || null,
                    rematch_media_server: rematchCheckbox.checked,
                }),
            });
            const data = await response.json();

            if (!data.success) {
                setError(data.error || 'Failed to apply the fix');
                applyBtn.disabled = false;
                applyBtn.textContent = originalText;
                return;
            }

            closeModal();

            // The page is about to navigate onto the corrected ID, which would
            // wipe a toast shown now — stash it and raise it after the reload.
            stashResultMessage(buildResultMessage(data));

            if (typeof context.onApplied === 'function') {
                context.onApplied(data);
            } else {
                flushStashedResult();
            }
        } catch (error) {
            setError('Failed to apply the fix: ' + error.message);
            applyBtn.disabled = false;
            applyBtn.textContent = originalText;
        }
    }

    function buildResultMessage(data) {
        const summary = [
            'Matched to ' + data.title + (data.year ? ' (' + data.year + ')' : ''),
            'IMDb ' + displayId(data.imdb_id),
            'TMDB ' + displayId(data.tmdb_id),
            'TVDB ' + displayId(data.tvdb_id),
            data.rows_updated + ' row' + (data.rows_updated === 1 ? '' : 's') + ' updated',
        ].join(' · ') + '.';

        const extras = [];

        // Metadata is re-pulled as part of the fix; say what that produced,
        // and say so loudly when it did not, since the entry is then still
        // showing the old match's episode titles and artwork.
        const refresh = data.metadata_refresh || {};
        if (refresh.success) {
            const bits = [];
            if (refresh.updated_episodes) bits.push(refresh.updated_episodes + ' episode titles');
            if (refresh.new_episodes_added) {
                bits.push(refresh.new_episodes_added + ' newly-found episodes queued');
            }
            extras.push(bits.length ? 'Metadata refreshed: ' + bits.join(', ') + '.'
                                    : 'Metadata refreshed.');
        } else {
            extras.push('Metadata refresh failed (' + (refresh.error || 'unknown error') +
                ') — episode titles and artwork may still be from the old match; ' +
                'try the Refresh Metadata button.');
        }

        if (data.rematch_note) extras.push(data.rematch_note + '.');
        return [summary].concat(extras).join(' ');
    }

    function stashResultMessage(message) {
        try {
            sessionStorage.setItem('fixMatchResult', message);
        } catch (e) {
            // Private mode / storage disabled — the toast is a nicety, not the work.
        }
    }

    /**
     * Show (and clear) a message stashed before a navigation.
     */
    function flushStashedResult() {
        let message = null;
        try {
            message = sessionStorage.getItem('fixMatchResult');
            if (message) sessionStorage.removeItem('fixMatchResult');
        } catch (e) {
            return;
        }
        if (!message) return;

        if (window.showPopup) {
            window.showPopup({
                type: window.POPUP_TYPES.SUCCESS,
                title: 'Match fixed',
                message: message,
                autoClose: 10000,
            });
        } else {
            // notifications.js is a module and may not have run yet.
            setTimeout(function () {
                if (window.showPopup) {
                    window.showPopup({
                        type: window.POPUP_TYPES.SUCCESS,
                        title: 'Match fixed',
                        message: message,
                        autoClose: 10000,
                    });
                }
            }, 500);
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (document.getElementById('fixMatchModal')) initializeElements();
        flushStashedResult();
    });
})();
