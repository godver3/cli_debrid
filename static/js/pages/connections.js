/* Moved from templates/connections.html. */
(function() {
    const ICON_MAP = [
        ['Plex RSS', 'fa-rss'], ['Plex', 'fa-play-circle'],
        ['Jackett', 'fa-search'], ['Torrentio', 'fa-magnet'], ['Nyaa', 'fa-cat'],
        ['Zilean', 'fa-clock'], ['MediaFusion', 'fa-layer-group'], ['AIOS', 'fa-magnet'],
        ['MDBList', 'fa-list'], ['Trakt', 'fa-tv'], ['Overseerr', 'fa-film'],
    ];

    function getIcon(name) {
        for (const [key, icon] of ICON_MAP) {
            if (name.includes(key)) return icon;
        }
        return 'fa-plug';
    }

    function slugify(name) {
        return name.toLowerCase().replace(/\s+/g, '-').replace(/[()'"]/g, '');
    }

    function timeAgo(ts) {
        const secs = Math.round((Date.now() - ts) / 1000);
        if (secs < 5) return 'just now';
        if (secs < 60) return `${secs}s ago`;
        if (secs < 3600) return `${Math.floor(secs/60)}m ago`;
        return `${Math.floor(secs/3600)}h ${Math.floor((secs%3600)/60)}m ago`;
    }

    // Update a card's footer with live-updating "Last checked X ago"
    function setCardChecked(card) {
        const footer = card.querySelector('.conn-card-footer');
        if (!footer) return;
        const ts = Date.now();
        footer.dataset.ts = ts;
        footer.innerHTML = `<span>Last checked: ${timeAgo(ts)}</span>`;
    }

    // Set card footer to checking state
    function setCardChecking(card) {
        const footer = card.querySelector('.conn-card-footer');
        if (!footer) return;
        footer.innerHTML = `<span class="conn-card-spinner"></span><span>Checking…</span>`;
        footer.dataset.ts = '';
        const ind = card.querySelector('.status-indicator');
        if (ind) ind.className = 'status-indicator checking';
    }

    // Update card status dot and footer timestamp
    function resolveCard(card, connected) {
        const ind = card.querySelector('.status-indicator');
        if (ind) ind.className = 'status-indicator ' + (connected ? 'connected' : 'disconnected');
        setCardChecked(card);
        if (typeof window._tngOnResolve === 'function') window._tngOnResolve();
    }

    // Find or create a card in container — also matches skeleton cards by title text
    function getOrCreateCard(container, service, name, iconClass, details) {
        let card = container.querySelector(`[data-service="${service}"]`);
        if (!card) {
            // Try to find a skeleton card with matching title text
            container.querySelectorAll('.connection-card').forEach(c => {
                const t = c.querySelector('.connection-title');
                if (t && t.textContent.trim() === name) card = c;
            });
        }
        if (!card) {
            card = document.createElement('div');
            card.className = 'connection-card';
            card.innerHTML = `
                <div class="connection-header">
                    <i class="fas ${iconClass}"></i>
                    <span class="connection-title">${name}</span>
                    <div class="status-indicator checking"></div>
                </div>
                <div class="conn-card-footer" data-ts=""><span class="conn-card-spinner"></span><span>Checking…</span></div>`;
            container.appendChild(card);
        if (typeof window._tngOnResolve === 'function') window._tngOnResolve();
        }
        // Update service slug so future lookups work
        card.dataset.service = service;
        card.style.display = '';
        // Set native tooltip so full name shows on hover
        card.title = name;
        updateCardExpiry(card, details);
        return card;
    }

    // Add/update a subscription-expiry line on a card, right before the footer
    function updateCardExpiry(card, details) {
        if (!details || !details.subscription_expiry_date) return;
        let expiry = card.querySelector('.conn-card-expiry');
        if (!expiry) {
            expiry = document.createElement('div');
            expiry.className = 'conn-card-expiry';
            const footer = card.querySelector('.conn-card-footer');
            card.insertBefore(expiry, footer);
        }
        expiry.dataset.expiryDate = details.subscription_expiry_date;
        expiry.dataset.autoRenew = details.auto_renew ? 'true' : 'false';
        const renewText = details.auto_renew ? ' (auto-renews)' : '';
        expiry.innerHTML = `<i class="fa-regular fa-calendar"></i>
            <div class="conn-card-expiry-lines">
                <span>Subscription Expires:${renewText}</span>
                <span class="conn-card-expiry-date">${details.subscription_expiry_date}</span>
            </div>`;
    }

    // Tick all card footers with a timestamp every 30s
    function startFooterTicker() {
        setInterval(() => {
            document.querySelectorAll('.conn-card-footer[data-ts]').forEach(footer => {
                const ts = parseInt(footer.dataset.ts);
                if (!ts) return;
                footer.innerHTML = `<span>Last checked: ${timeAgo(ts)}</span>`;
            });
        }, 30000);
    }

    function updateFailingSummary() {
        const failing = [];
        document.querySelectorAll('.status-indicator.disconnected').forEach(ind => {
            const card = ind.closest('.connection-card');
            if (card) {
                const title = card.querySelector('.connection-title');
                failing.push(title ? title.textContent.trim() : card.dataset.service);
            }
        });
        const section = document.getElementById('failing-connections-section');
        const list = document.getElementById('failing-connections-list');
        if (failing.length > 0) {
            list.innerHTML = failing.map(n =>
                `<div class="failure-details"><h4>${n}</h4><div class="error-message"><strong>Status:</strong> Disconnected</div></div>`
            ).join('');
            section.style.display = '';
        } else {
            section.style.display = 'none';
        }
    }

    // ── System: fetch, update each card as soon as response arrives ───────────
    async function checkSystem() {
        const container = document.getElementById('system-cards');
        // Set all system cards to checking
        container.querySelectorAll('.connection-card').forEach(setCardChecking);

        try {
            const resp = await fetch('/connections/api/check/system');
            const data = await resp.json();

            const map = {
                'cli_battery_status': 'cli_battery',
                'plex_status': 'plex',
                'jellyfin_status': 'jellyfin',
                'mounted_files_status': 'mounted-files',
                'phalanx_db_status': 'phalanx-db',
                'tvdb_status': 'tvdb',
                'tmdb_status': 'tmdb',
                'climount_status': 'climount',
                'scrob_status': 'scrob',
                'trakt_status': 'trakt',
            };
            for (const [key, service] of Object.entries(map)) {
                if (data[key]) {
                    const card = container.querySelector(`[data-service="${service}"]`);
                    if (card) { card.style.display = ''; resolveCard(card, data[key].connected); }
                }
            }
        } catch(e) {
            container.querySelectorAll('.connection-card').forEach(card => {
                const footer = card.querySelector('.conn-card-footer');
                if (footer) footer.innerHTML = '<span>Check failed</span>';
            });
        }
        updateFailingSummary();
    }

    // ── Scrapers: SSE stream — each card resolves as server sends it ──────────
    async function checkScrapers() {
        const container = document.getElementById('scraper-cards');
        const placeholder = document.getElementById('scraper-placeholder');
        // Set existing skeleton cards to checking state, track which get resolved
        const resolvedServices = new Set();
        container.querySelectorAll('.connection-card').forEach(setCardChecking);

        return new Promise((resolve) => {
            const es = new EventSource('/connections/api/stream/scrapers');
            es.addEventListener('scraper', (e) => {
                const scraper = JSON.parse(e.data);
                if (placeholder) placeholder.remove();
                const service = 'scraper-' + slugify(scraper.name);
                resolvedServices.add(service);
                const card = getOrCreateCard(container, service, scraper.name, getIcon(scraper.name), scraper.details);
                resolveCard(card, scraper.connected);
                updateFailingSummary();
            });
            es.addEventListener('done', () => {
                // Remove any skeleton cards that never got a result (disabled/removed scrapers)
                container.querySelectorAll('.connection-card').forEach(c => {
                    if (!resolvedServices.has(c.dataset.service)) c.remove();
                });
                es.close(); resolve();
            });
            es.onerror = () => { es.close(); resolve(); };
            setTimeout(() => { es.close(); resolve(); }, 30000);
        });
    }

    // ── Sources: SSE stream — each card resolves as server sends it ───────────
    async function checkSources() {
        const container = document.getElementById('source-cards');
        const placeholder = document.getElementById('source-placeholder');
        // Set existing skeleton cards to checking state, track which get resolved
        const resolvedServices = new Set();
        container.querySelectorAll('.connection-card').forEach(setCardChecking);

        return new Promise((resolve) => {
            const es = new EventSource('/connections/api/stream/content-sources');
            es.addEventListener('source', (e) => {
                const source = JSON.parse(e.data);
                if (placeholder) placeholder.remove();
                const service = 'source-' + slugify(source.name);
                resolvedServices.add(service);
                const card = getOrCreateCard(container, service, source.name, getIcon(source.name));
                resolveCard(card, source.connected);
                updateFailingSummary();
            });
            es.addEventListener('done', () => {
                // Remove any skeleton cards that never got a result
                container.querySelectorAll('.connection-card').forEach(c => {
                    if (!resolvedServices.has(c.dataset.service)) c.remove();
                });
                es.close(); resolve();
            });
            es.onerror = () => { es.close(); resolve(); };
            setTimeout(() => { es.close(); resolve(); }, 60000);
        });
    }

    async function runAll() {
        const btn = document.getElementById('conn-refresh-all');
        const icon = btn.querySelector('i');
        btn.disabled = true;
        if (icon) icon.style.animation = 'conn-spin 0.7s linear infinite';
        try {
            await checkSystem();
            await checkScrapers();
            await checkSources();
        } finally {
            btn.disabled = false;
            if (icon) icon.style.animation = '';
        }
    }

    document.addEventListener('DOMContentLoaded', function() {
        startFooterTicker();
        document.getElementById('conn-refresh-all').addEventListener('click', runAll);
        runAll();
    });
})();
