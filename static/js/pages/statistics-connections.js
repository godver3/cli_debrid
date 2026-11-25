// Home page Connections tile (usenet / combined mode): reads the cached
// connection health from /connections/api/health and refreshes it every minute.
// The checks themselves run in the background on the server.
(function () {
    const tile = document.getElementById('kpi-connections');
    const value = document.getElementById('conn_health');
    const number = document.getElementById('conn_health_number');
    if (!tile || !value || !number) return;

    function ago(iso) {
        if (!iso) return '';
        const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
        if (mins < 1) return 'checked just now';
        if (mins < 60) return `checked ${mins} min ago`;
        const hrs = Math.round(mins / 60);
        return `checked ${hrs} h ago`;
    }

    function show(data) {
        number.classList.remove('normal', 'warning', 'critical');
        if (!data.checked) {
            value.textContent = 'Checking…';
            tile.title = 'Checking connections…';
            return;
        }
        if (!data.total) {
            value.textContent = 'None set up';
            tile.title = 'No connections to check yet.';
            return;
        }
        if (data.down.length) {
            value.textContent = `${data.down.length} down`;
            number.classList.add('critical');
            const short = (e) => (e.length > 80 ? e.slice(0, 79) + '…' : e);
            const lines = data.down.map(d => d.error ? `${d.name}: ${short(String(d.error))}` : d.name);
            tile.title = `Not connected:\n${lines.join('\n')}\n\n${data.ok} of ${data.total} OK · ${ago(data.checked_at)}. Click for details.`;
        } else {
            value.textContent = 'All OK';
            number.classList.add('normal');
            tile.title = `All ${data.total} connections OK · ${ago(data.checked_at)}. Click for details.`;
        }
    }

    async function refresh() {
        try {
            const resp = await fetch('/connections/api/health');
            if (!resp.ok) return;
            const data = await resp.json();
            show(data);
            // The first check runs right after startup; look again soon instead of in a minute.
            if (!data.checked) setTimeout(refresh, 5000);
        } catch (e) { /* keep the last value */ }
    }

    refresh();
    setInterval(refresh, 60000);
})();
