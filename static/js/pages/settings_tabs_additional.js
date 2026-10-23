/* Moved from templates/settings_tabs/additional.html. */
(function() {
    // ── Accent color ──────────────────────────────────────────────────────────
    var accentInput = document.getElementById('smart-poster-accent');
    var accentLabel = document.getElementById('smart-poster-accent-label');
    var accentClear = document.getElementById('smart-poster-accent-clear');
    if (accentInput) {
        // Init display from current input value
        var initAccent = accentInput.value;
        if (initAccent && initAccent !== '#000000') {
            accentInput.style.opacity = '1';
            if (accentLabel) accentLabel.textContent = initAccent;
            if (accentClear) accentClear.style.display = 'inline-block';
        }
        accentInput.addEventListener('input', function() {
            this.style.opacity = '1';
            if (accentLabel) accentLabel.textContent = this.value;
            if (accentClear) accentClear.style.display = 'inline-block';
        });
    }
    if (accentClear) {
        accentClear.addEventListener('click', function() {
            if (accentInput) { accentInput.value = '#000000'; accentInput.style.opacity = '0.4'; }
            if (accentLabel) accentLabel.textContent = 'Design default';
            this.style.display = 'none';
        });
    }

    // ── Auto-load collections on page init so checkboxes are always in DOM ──
    fetch('/settings/api/plex-smart-collections')
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.success && d.collections && d.collections.length > 0) {
                renderSmartCollections(d.collections);
                document.getElementById('smart-collections-list').style.display = 'block';
            }
        }).catch(function() {});

    // ── Load collections button ───────────────────────────────────────────────
    var loadBtn = document.getElementById('load-smart-collections-btn');
    if (loadBtn) {
        loadBtn.addEventListener('click', async function() {
            this.textContent = 'Loading...';
            this.disabled = true;
            try {
                var resp = await fetch('/settings/api/plex-smart-collections');
                var data = await resp.json();
                if (data.success) {
                    renderCollections(data.collections);
                    document.getElementById('smart-collections-list').style.display = 'block';
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Error: ' + (data.error || 'Failed to load'), autoClose: 5000 });
                }
            } catch(e) {
                showPopup({ type: 'error', title: 'Error', message: 'Error: ' + e, autoClose: 5000 });
            } finally {
                this.textContent = 'Reload Collections';
                this.disabled = false;
            }
        });
    }

    // ── Run Now button ────────────────────────────────────────────────────────
    var runBtn = document.getElementById('run-smart-collections-btn');
    if (runBtn) {
        runBtn.addEventListener('click', async function() {
            this.textContent = 'Running...';
            this.disabled = true;
            try {
                var resp = await fetch('/settings/api/plex-smart-collections/run', {method:'POST'});
                var data = await resp.json();
                showPopup({ type: data.success ? 'success' : 'error', title: data.success ? 'Success' : 'Error', message: data.success ? 'Task started.' : 'Error: ' + data.error, autoClose: data.success ? 4000 : 5000 });
            } finally {
                this.textContent = 'Run Now';
                this.disabled = false;
            }
        });
    }



    // ── Render collection list ────────────────────────────────────────────────
    function renderCollections(collections) {
        var container = document.getElementById('smart-collections-container');
        if (!container) return;
        var sections = {};
        collections.forEach(function(c) {
            if (!sections[c.section]) sections[c.section] = [];
            sections[c.section].push(c);
        });
        var html = '';
        Object.keys(sections).forEach(function(section) {
            html += '<div style="width:100%;margin-top:8px;margin-bottom:2px;color:#888;font-size:0.75em;text-transform:uppercase;">' + section + '</div>';
            sections[section].forEach(function(c) {
                html += '<label style="display:flex;align-items:center;gap:6px;padding:4px 10px;background:rgba(255,255,255,0.05);border-radius:6px;cursor:pointer;font-size:0.9em;">' +
                    '<input type="checkbox" class="smart-coll-cb" data-rk="' + c.ratingKey + '" data-title="' + c.title + '" data-section="' + c.section + '" data-type="' + c.type + '"' + (c.enabled ? ' checked' : '') + '>' +
                    c.title + '</label>';
            });
        });
        container.innerHTML = html;
    }
})();
