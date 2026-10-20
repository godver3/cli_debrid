/**
 * Show Options Modal
 *
 * Per-show settings on the library show page:
 *  - series type (automatic, anime, or not anime)
 *  - episode order (TVDB season types / TMDB episode groups), previewed first:
 *    the server reports where every library entry would move before Apply.
 * Backed by /library/show_options/<imdb_id>.
 *
 * Used by library_show.js via window.openShowOptionsModal({imdbId, title, onOrderApplied}).
 */

(function () {
    'use strict';

    let modal, statusEl, errorEl, seriesTypeEl, seriesTypeHintEl;
    let orderSelect, previewBtn, previewEl, applyBtn, plexLabel, plexCheckbox;
    let missingLabel, missingCheckbox, missingText;
    let context = null;
    let busy = false;
    let currentOrder = 'default';
    let previewedOrder = null;

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
        orderSelect = document.getElementById('showOptionsOrderSelect');
        previewBtn = document.getElementById('showOptionsPreviewBtn');
        previewEl = document.getElementById('showOptionsPreview');
        applyBtn = document.getElementById('showOptionsApplyBtn');
        plexLabel = document.getElementById('showOptionsPlexLabel');
        plexCheckbox = document.getElementById('showOptionsSyncPlex');
        missingLabel = document.getElementById('showOptionsMissingLabel');
        missingCheckbox = document.getElementById('showOptionsAddMissing');
        missingText = document.getElementById('showOptionsMissingText');

        modal.querySelector('.show-options-overlay').addEventListener('click', close);
        modal.querySelector('.show-options-close').addEventListener('click', close);
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && modal.style.display !== 'none') close();
        });
        seriesTypeEl.querySelectorAll('.show-options-choice').forEach(function (btn) {
            btn.addEventListener('click', function () { setSeriesType(btn.dataset.value); });
        });
        orderSelect.addEventListener('change', resetPreview);
        previewBtn.addEventListener('click', previewOrder);
        applyBtn.addEventListener('click', applyOrder);
        return true;
    }

    function setMessage(el, text) {
        el.textContent = text || '';
        el.style.display = text ? '' : 'none';
    }

    function el(tag, text, className) {
        const node = document.createElement(tag);
        if (text !== undefined && text !== null) node.textContent = text;
        if (className) node.className = className;
        return node;
    }

    function se(pair) {
        return 'S' + String(pair[0]).padStart(2, '0') + 'E' + String(pair[1]).padStart(2, '0');
    }

    function url(path) {
        return '/library/show_options/' + encodeURIComponent(context.imdbId) + (path || '');
    }

    async function postJson(path, body) {
        const resp = await fetch(url(path), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        return resp.json();
    }

    // --- Series type ---------------------------------------------------------

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
            const resp = await fetch(url());
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
            const data = await postJson('/series_type', {series_type: value});
            if (!data.success) throw new Error(data.error || 'Could not save');
            busy = false;
            await load('Saved. ' + data.episodes_updated + ' episode entries updated.'
                       + (data.symlink_note ? ' ' + data.symlink_note + '.' : ''));
        } catch (e) {
            busy = false;
            setMessage(statusEl, '');
            setMessage(errorEl, e.message);
        }
    }

    // --- Episode order -------------------------------------------------------

    async function loadOrders() {
        orderSelect.disabled = true;
        previewBtn.disabled = true;
        orderSelect.replaceChildren(el('option', 'Loading…'));
        try {
            const resp = await fetch(url('/episode_orders'));
            const data = await resp.json();
            if (!data.success) throw new Error(data.error || 'Could not list episode orders');
            currentOrder = data.current || 'default';
            orderSelect.replaceChildren();
            data.orders.forEach(function (order) {
                const option = el('option', order.name + (order.id === currentOrder ? ' — current' : ''));
                option.value = order.id;
                option.selected = order.id === currentOrder;
                orderSelect.appendChild(option);
            });
            orderSelect.disabled = data.orders.length < 2;
            previewBtn.disabled = orderSelect.disabled;
            if (data.orders.length < 2) {
                orderSelect.replaceChildren(el('option', 'No other episode orders available for this show'));
            }
        } catch (e) {
            orderSelect.replaceChildren(el('option', 'Episode orders unavailable'));
            setMessage(errorEl, e.message);
        }
        resetPreview();
    }

    function resetPreview() {
        previewedOrder = null;
        previewEl.style.display = 'none';
        previewEl.replaceChildren();
        applyBtn.style.display = 'none';
        applyBtn.disabled = true;
        plexLabel.style.display = 'none';
        missingLabel.style.display = 'none';
        missingCheckbox.checked = false;
        previewBtn.disabled = orderSelect.disabled || orderSelect.value === currentOrder;
    }

    function list(title, items, render, className) {
        if (!items || !items.length) return null;
        const wrap = el('div', null, className);
        wrap.appendChild(el('strong', title));
        const ul = el('ul');
        items.slice(0, 50).forEach(function (item) { ul.appendChild(el('li', render(item))); });
        if (items.length > 50) ul.appendChild(el('li', '…and ' + (items.length - 50) + ' more'));
        wrap.appendChild(ul);
        return wrap;
    }

    function renderPreview(data) {
        previewEl.replaceChildren();
        const summary = el('p');
        summary.textContent = data.moves.length + ' entries move, ' + data.unchanged + ' keep their numbers, ' +
            data.removes.length + ' wanted entries are dropped (not in the new order), ' +
            data.new_episodes + ' episodes exist only in the new order.';
        previewEl.appendChild(summary);

        if (data.moves.length) {
            const wrap = el('div', null, 'show-options-table-wrap');
            const table = el('table', null, 'show-options-table');
            data.moves.slice(0, 300).forEach(function (m) {
                const tr = el('tr');
                tr.appendChild(el('td', se(m.old) + ' → ' + se(m.new)));
                tr.appendChild(el('td', (m.new_title || m.title || '') + (m.has_file ? ' (has file)' : '')));
                tr.appendChild(el('td', m.version || ''));
                table.appendChild(tr);
            });
            wrap.appendChild(table);
            previewEl.appendChild(wrap);
        }
        [
            list('Can\'t be placed in the new order (left as they are):', data.unmappable,
                 function (u) { return se(u.old) + ' ' + (u.title || '') + ' [' + (u.version || '') + ', ' + u.state + ']'; },
                 'show-options-warning'),
            list('Would collide (nothing will be changed until this is resolved):', data.conflicts,
                 function (c) { return se([c.season, c.episode]) + ' [' + (c.version || '') + ']: ' + c.row_ids.length + ' entries'; },
                 'show-options-warning'),
            list(data.missing_episodes + ' episode' + (data.missing_episodes === 1 ? '' : 's') +
                 ' of the new order won\'t be in your library:', data.missing,
                 function (m) { return se([m.season, m.episode]) + ' ' + (m.title || '') + (m.aired ? ' (' + m.aired + ')' : ''); },
                 'show-options-warning'),
        ].forEach(function (node) { if (node) previewEl.appendChild(node); });
        if (data.missing_episodes > (data.missing || []).length) {
            previewEl.lastChild.querySelector('ul').appendChild(el('li', '…and ' + (data.missing_episodes - data.missing.length) + ' more'));
        }
        if (data.plex_note) previewEl.appendChild(el('p', data.plex_note, 'show-options-warning'));
        previewEl.style.display = '';

        plexLabel.style.display = data.plex_ordering ? '' : 'none';
        missingLabel.style.display = data.missing_episodes ? '' : 'none';
        missingText.textContent = 'Add the ' + data.missing_episodes + ' missing episode' +
            (data.missing_episodes === 1 ? '' : 's') + ' to Wanted';
        applyBtn.style.display = '';
        applyBtn.disabled = !!(data.conflicts && data.conflicts.length);
    }

    async function previewOrder() {
        const order = orderSelect.value;
        setMessage(errorEl, '');
        setMessage(statusEl, 'Comparing episode orders…');
        previewBtn.disabled = true;
        try {
            const data = await postJson('/episode_order/preview', {order: order});
            if (!data.success) throw new Error(data.error || 'Preview failed');
            previewedOrder = order;
            renderPreview(data);
            setMessage(statusEl, '');
        } catch (e) {
            setMessage(statusEl, '');
            setMessage(errorEl, e.message);
        } finally {
            previewBtn.disabled = false;
        }
    }

    async function applyOrder() {
        if (!previewedOrder || previewedOrder !== orderSelect.value) return;
        applyBtn.disabled = true;
        previewBtn.disabled = true;
        setMessage(errorEl, '');
        setMessage(statusEl, 'Applying… moving entries and rebuilding the show\'s metadata');
        try {
            const data = await postJson('/episode_order/apply', {order: previewedOrder, sync_plex: plexCheckbox.checked,
                                                              add_missing: missingCheckbox.checked});
            if (!data.success) throw new Error(data.error || 'Applying the order failed');
            const notes = [data.moves.length + ' entries moved, ' + data.removes.length + ' dropped.'];
            if (data.symlink_note) notes.push(data.symlink_note + '.');
            if (data.missing_requested) {
                notes.push(data.missing_added === null || data.missing_added === undefined
                    ? 'Adding the missing episodes failed; use Refresh Metadata on the show page to add them.'
                    : data.missing_added + ' missing episode' + (data.missing_added === 1 ? '' : 's') + ' added to Wanted.');
            }
            if (data.plex_note) notes.push(data.plex_note + '.');
            await loadOrders();
            setMessage(statusEl, 'Episode order changed. ' + notes.join(' '));
            if (context && typeof context.onOrderApplied === 'function') context.onOrderApplied(data);
        } catch (e) {
            setMessage(statusEl, '');
            setMessage(errorEl, e.message);
            applyBtn.disabled = false;
            previewBtn.disabled = false;
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
        loadOrders();
    };
})();
