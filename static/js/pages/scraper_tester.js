/* Moved from templates/scraper_tester.html. */
(function() {
    const originalFetch = window.fetch;
    window.fetch = async function(input, init) {
        try {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            const method = (init && init.method) ? init.method.toUpperCase() : 'GET';
            if (url.endsWith('/scraper/scraper_tester') && method === 'POST') {
                // Use battery DirectAPI search — same source the scraper uses internally
                const body = init && init.body ? JSON.parse(init.body) : {};
                const query = encodeURIComponent(body.search_term || body.query || '');
                const resp = await originalFetch(`/scraper/api/search?query=${query}`);
                const data = await resp.json();
                const transformed = (data.results || []).map(r => {
                    r.mediaType = r.media_type === 'show' ? 'tv' : (r.media_type || 'movie');
                    r.imdbId = r.imdb_id || null;
                    return r;
                });
                return new Response(JSON.stringify(transformed), { status: 200, headers: { 'Content-Type': 'application/json' }});
            }
        } catch (e) {
            console.error('Scraper Tester fetch shim error:', e);
        }
        return originalFetch.apply(this, arguments);
    };
})();
