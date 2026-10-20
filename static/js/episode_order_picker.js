// Episode Order picker for the Discover request dialogs.
//
// Lists the orders a show can be requested in (TVDB aired/DVD/absolute…, TMDB
// episode groups) from /magnet/episode_orders. Hidden when there is nothing to
// pick or the viewer can't change orders (non-admin); disabled when the show
// is already in the library, since that has to go through Show options.
(function () {
    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text) node.textContent = text;
        return node;
    }

    // Adds the picker to `container`. onChange(orderId) runs when the user picks
    // another order. Returns { value() }: the picked order, or null when it is
    // the show's current one (nothing to change).
    function mount(container, tmdbId, onChange) {
        const wrap = el('div', 'episode-order-picker');
        wrap.style.display = 'none';
        const divider = el('div', 'vm-divider');
        const label = el('div', 'section-label', 'Episode Order');
        const select = el('select', 'episode-order-select');
        select.style.cssText = 'width:100%;padding:8px 10px;background:#1a1a1a;color:#fff;border:1px solid #333;border-radius:6px;font-size:12px;margin-top:4px;';
        const hint = el('div', 'episode-order-hint');
        hint.style.cssText = 'font-size:11px;opacity:0.65;margin-top:6px;line-height:1.4;';
        hint.textContent = 'How the show is numbered when searched, matched and named — e.g. DVD order. Seasons above follow it.';
        // Same warning as Show options in the library
        const plexWarning = el('div', 'episode-order-plex-warning');
        plexWarning.style.cssText = 'display:none;font-size:11px;color:#f0a14a;margin-top:6px;line-height:1.4;';
        plexWarning.textContent = "Plex has no matching episode ordering for this order. Once the show is in Plex, " +
            "set its ordering there by hand if its episode numbers don't line up.";
        wrap.append(divider, label, select, hint, plexWarning);
        container.appendChild(wrap);

        let current = 'default';
        const plexSupported = {};
        const updatePlexWarning = () => {
            plexWarning.style.display = plexSupported[select.value] === false ? '' : 'none';
        };

        (async () => {
            try {
                const response = await fetch(`/magnet/episode_orders?tmdb_id=${encodeURIComponent(tmdbId)}`, {
                    headers: { 'Accept': 'application/json' }
                });
                const type = response.headers.get('content-type') || '';
                if (!response.ok || !type.includes('application/json')) return; // e.g. not an admin
                const data = await response.json();
                if (!data.success || !data.orders || data.orders.length < 2) return;
                current = data.current || 'default';
                data.orders.forEach(order => {
                    plexSupported[order.id] = order.plex_supported;
                    const option = el('option', null, order.id === current ? `${order.name} — current` : order.name);
                    option.value = order.id;
                    if (order.id === current) option.selected = true;
                    select.appendChild(option);
                });
                if (data.library_episodes > 0) {
                    select.disabled = true;
                    hint.textContent = `Already in your library (${data.library_episodes} episodes) in this order. Change it from the show page (Show options).`;
                }
                wrap.style.display = '';
                updatePlexWarning();
            } catch (error) {
                console.error('Error loading episode orders:', error);
            }
        })();

        select.addEventListener('change', () => {
            updatePlexWarning();
            if (onChange) onChange(select.value);
        });

        return {
            value() {
                if (select.disabled || !select.value || select.value === current) return null;
                return select.value;
            },
            // The show's saved order ('default' until loaded)
            current() {
                return current;
            }
        };
    }

    window.EpisodeOrderPicker = { mount };
})();
