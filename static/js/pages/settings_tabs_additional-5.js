/* Moved from templates/settings_tabs/additional.html. */
(function() {
    // Init slider display values from saved settings
    (function() {
        var ov = document.getElementById('smart-overlay-op');
        var go = document.getElementById('smart-glow-op');
        var gr = document.getElementById('smart-glow-r');
        if (ov) document.getElementById('smart-overlay-op-val').textContent = ov.value + '%';
        if (go) document.getElementById('smart-glow-op-val').textContent = go.value + '%';
        if (gr) document.getElementById('smart-glow-r-val').textContent = gr.value;
    })();

    var ai = document.getElementById('smart-poster-accent');
    var al = document.getElementById('smart-poster-accent-label');
    var ac = document.getElementById('smart-poster-accent-clear');
    if (ai) {
        if (ai.value && ai.value !== '#000000') {
            ai.style.opacity = '1';
            if (al) al.textContent = ai.value;
            if (ac) ac.style.display = 'inline-block';
        }
        ai.addEventListener('input', function() {
            this.style.opacity = '1';
            if (al) al.textContent = this.value;
            if (ac) ac.style.display = 'inline-block';
        });
    }
    if (ac) {
        ac.addEventListener('click', function() {
            if (ai) { ai.value = '#000000'; ai.style.opacity = '0.4'; }
            if (al) al.textContent = 'Design default';
            this.style.display = 'none';
        });
    }

    var lb = document.getElementById('load-smart-collections-btn');
    if (lb) {
        lb.addEventListener('click', async function() {
            this.textContent = 'Loading...'; this.disabled = true;
            try {
                var r = await fetch('/settings/api/plex-smart-collections');
                var d = await r.json();
                if (d.success) {
                    renderSmartCollections(d.collections);
                    document.getElementById('smart-collections-list').style.display = 'block';
                } else { showPopup({ type: 'error', title: 'Error', message: 'Error: ' + (d.error || 'Failed'), autoClose: 5000 }); }
            } catch(e) { showPopup({ type: 'error', title: 'Error', message: 'Error: ' + e, autoClose: 5000 }); }
            finally { this.textContent = 'Reload Collections'; this.disabled = false; }
        });
    }

    var rb = document.getElementById('run-smart-collections-btn');
    if (rb) {
        rb.addEventListener('click', async function() {
            this.textContent = 'Running...'; this.disabled = true;
            try {
                var r = await fetch('/settings/api/plex-smart-collections/run', {method:'POST'});
                var d = await r.json();
                showPopup({ type: d.success ? 'success' : 'error', title: d.success ? 'Success' : 'Error', message: d.success ? 'Task started.' : 'Error: ' + d.error, autoClose: d.success ? 4000 : 5000 });
            } finally { this.textContent = 'Run Now'; this.disabled = false; }
        });
    }

    document.addEventListener('DOMContentLoaded', function() {
        var ms = document.getElementById('save-settings-button');
        if (ms) {
            ms.addEventListener('click', function() {
                var cols = {};
                document.querySelectorAll('.smart-coll-cb').forEach(function(cb) {
                    cols[cb.dataset.rk] = {
                        enabled: cb.checked,
                        title: cb.dataset.title,
                        section: cb.dataset.section,
                        type: cb.dataset.type
                    };
                });
                if (Object.keys(cols).length > 0) {
                    fetch('/settings/api/plex-smart-collections', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({collections: cols})
                    }).catch(function() {});
                }
            }, true);
        }
    });

    function renderSmartCollections(cols) {
        var c = document.getElementById('smart-collections-container');
        if (!c) return;
        var secs = {};
        cols.forEach(function(x) {
            if (!secs[x.section]) secs[x.section] = [];
            secs[x.section].push(x);
        });
        var h = '';
        Object.keys(secs).forEach(function(s) {
            h += '<div style="width:100%;margin-top:8px;color:#888;font-size:0.75em;text-transform:uppercase;">' + s + '</div>';
            secs[s].forEach(function(x) {
                h += '<label style="display:flex;align-items:center;gap:6px;padding:4px 10px;background:rgba(255,255,255,0.05);border-radius:6px;cursor:pointer;font-size:0.9em;">';
                h += '<input type="checkbox" class="smart-coll-cb"';
                h += ' data-rk="' + x.ratingKey + '"';
                h += ' data-title="' + x.title + '"';
                h += ' data-section="' + x.section + '"';
                h += ' data-type="' + x.type + '"';
                if (x.enabled) h += ' checked';
                h += '>' + x.title + '</label>';
            });
        });
        c.innerHTML = h;
    }
})();
