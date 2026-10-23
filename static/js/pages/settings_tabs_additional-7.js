/* Moved from templates/settings_tabs/additional.html. */
(function() {
    // Enable/disable dependent controls
    var enabledCb = document.getElementById('boxsets-enabled');
    var depControls = document.getElementById('boxsets-dependent-controls');
    if (enabledCb && depControls) {
        enabledCb.addEventListener('change', function() {
            depControls.style.display = this.checked ? '' : 'none';
        });
    }

    // Show/hide version dropdown when grab_missing toggled
    var grabMissingCb = document.getElementById('boxsets-grab-missing');
    var versionGroup = document.getElementById('boxsets-version-group');
    if (grabMissingCb && versionGroup) {
        grabMissingCb.addEventListener('change', function() {
            versionGroup.style.display = this.checked ? '' : 'none';
        });
    }

    // Populate version dropdown from app versions endpoint
    var versionSelect = document.getElementById('boxsets-grab-version');
    if (versionSelect) {
        var savedVersion = window.pageData.settings_tabs_additional.Plex_Movie_Box;
        fetch('/content/versions')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var versions = data.versions || data || [];
                if (!Array.isArray(versions)) versions = Object.keys(versions);
                versionSelect.innerHTML = '';
                versions.forEach(function(v) {
                    var opt = document.createElement('option');
                    opt.value = v;
                    opt.textContent = v;
                    if (v === savedVersion) opt.selected = true;
                    versionSelect.appendChild(opt);
                });
                if (!versions.includes(savedVersion)) {
                    var opt = document.createElement('option');
                    opt.value = savedVersion;
                    opt.textContent = savedVersion;
                    opt.selected = true;
                    versionSelect.insertBefore(opt, versionSelect.firstChild);
                }
            })
            .catch(function() {
                // Leave Default option if fetch fails
            });
    }

    // Fetch and display status
    function loadBoxSetsStatus() {
        var statusEl = document.getElementById('boxsets-status');
        if (!statusEl) return;
        fetch('/settings/api/plex-boxsets/status')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.success) {
                    var parts = [];
                    if (d.last_run) {
                        var dt = new Date(d.last_run + 'Z');
                        parts.push('Last run: ' + dt.toLocaleString());
                    }
                    if (d.collections_managed > 0) {
                        parts.push(d.collections_managed + ' collection' + (d.collections_managed !== 1 ? 's' : '') + ' managed');
                    }
                    statusEl.textContent = parts.join(' · ') || 'Not yet run';
                }
            })
            .catch(function() {});
    }
    loadBoxSetsStatus();

    // Run Now button
    var runBtn = document.getElementById('boxsets-run-btn');
    if (runBtn) {
        runBtn.addEventListener('click', function() {
            var statusEl = document.getElementById('boxsets-status');
            runBtn.disabled = true;
            runBtn.textContent = 'Starting...';
            fetch('/settings/api/plex-boxsets/run', {method: 'POST'})
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    runBtn.disabled = false;
                    runBtn.textContent = 'Run Now';
                    if (statusEl) statusEl.textContent = d.success ? 'Task started — check logs for progress' : ('Error: ' + d.error);
                })
                .catch(function(e) {
                    runBtn.disabled = false;
                    runBtn.textContent = 'Run Now';
                    if (statusEl) statusEl.textContent = 'Request failed';
                });
        });
    }

    // Smart Collections — Manage Posters modal
    (function() {
        var scBtn    = document.getElementById('smartcoll-manage-btn');
        var scModal  = document.getElementById('smartcoll-manage-modal');
        var scClose  = document.getElementById('smartcoll-manage-close');
        var scList   = document.getElementById('smartcoll-manage-list');
        var scSearch = document.getElementById('smartcoll-manage-search');
        var _scColls = [];

        function scRenderRows(filter) {
            var q = (filter || '').toLowerCase().trim();
            var filtered = q ? _scColls.filter(function(c) { return c.name.toLowerCase().includes(q); }) : _scColls;
            scList.innerHTML = '';
            if (filtered.length === 0) {
                scList.innerHTML = '<span style="color:#aaa;font-size:0.85em;">' + (q ? 'No collections match.' : 'No enabled collections found. Enable collections and run the task first.') + '</span>';
                return;
            }
            filtered.forEach(function(c) {
                var row = document.createElement('div');
                row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 10px;background:rgba(255,255,255,0.04);border-radius:6px;';
                var name = document.createElement('span');
                name.textContent = c.name;
                name.style.cssText = 'font-size:0.88em;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
                var btn = document.createElement('button');
                btn.textContent = 'Force reapply';
                btn.className = 'btn btn-secondary';
                btn.style.cssText = 'font-size:0.78em;padding:4px 10px;white-space:nowrap;flex-shrink:0;';
                btn.addEventListener('click', function() {
                    btn.disabled = true;
                    btn.textContent = 'Applying...';
                    fetch('/settings/api/plex-smart-collections/reset-collection', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({collection_id: c.id})
                    })
                    .then(function(r) { return r.json(); })
                    .then(function(res) {
                        if (res.success) {
                            btn.textContent = 'Applied ✓';
                            btn.style.color = '#4caf50';
                        } else {
                            btn.disabled = false;
                            btn.textContent = 'Force reapply';
                            btn.style.color = '';
                            showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + (res.message || 'Unknown error'), autoClose: 5000 });
                        }
                    })
                    .catch(function() {
                        btn.disabled = false;
                        btn.textContent = 'Force reapply';
                        btn.style.color = '';
                        showPopup({ type: 'error', title: 'Error', message: 'Request failed — check logs', autoClose: 5000 });
                    });
                });
                row.appendChild(name);
                row.appendChild(btn);
                scList.appendChild(row);
            });
        }

        function scOpenModal() {
            scModal.style.display = 'flex';
            if (scSearch) scSearch.value = '';
            scList.innerHTML = '<span style="color:#aaa;font-size:0.85em;">Loading...</span>';
            fetch('/settings/api/plex-smart-collections/status')
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    _scColls = (d.success && d.collections) ? d.collections : [];
                    scRenderRows('');
                    if (scSearch) scSearch.focus();
                })
                .catch(function() {
                    scList.innerHTML = '<span style="color:#f44336;font-size:0.85em;">Failed to load collections.</span>';
                });
        }

        if (scSearch) scSearch.addEventListener('input', function() { scRenderRows(this.value); });
        if (scBtn)    scBtn.addEventListener('click', scOpenModal);
        if (scClose)  scClose.addEventListener('click', function() { scModal.style.display = 'none'; });
        if (scModal)  scModal.addEventListener('click', function(e) { if (e.target === scModal) scModal.style.display = 'none'; });
    })();

    // Manage Posters modal
    var manageBtn = document.getElementById('boxsets-manage-btn');
    var manageModal = document.getElementById('boxsets-manage-modal');
    var manageClose = document.getElementById('boxsets-manage-close');
    var manageList = document.getElementById('boxsets-manage-list');
    var manageSearch = document.getElementById('boxsets-manage-search');
    var _allCollections = [];

    function renderCollectionRows(filter) {
        var q = (filter || '').toLowerCase().trim();
        var filtered = q ? _allCollections.filter(function(c) { return c.name.toLowerCase().includes(q); }) : _allCollections;
        manageList.innerHTML = '';
        if (filtered.length === 0) {
            manageList.innerHTML = '<span style="color:#aaa;font-size:0.85em;">' + (q ? 'No collections match.' : 'No collections found. Run the task first.') + '</span>';
            return;
        }
        filtered.forEach(function(c) {
            var row = document.createElement('div');
            row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 10px;background:rgba(255,255,255,0.04);border-radius:6px;';
            var name = document.createElement('span');
            name.textContent = c.name;
            name.style.cssText = 'font-size:0.88em;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
            var btn = document.createElement('button');
            btn.textContent = 'Force reapply';
            btn.className = 'btn btn-secondary';
            btn.style.cssText = 'font-size:0.78em;padding:4px 10px;white-space:nowrap;flex-shrink:0;';
            btn.addEventListener('click', function() {
                btn.disabled = true;
                btn.textContent = 'Applying...';
                fetch('/settings/api/plex-boxsets/reset-collection', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({collection_id: c.id})
                })
                .then(function(r) { return r.json(); })
                .then(function(res) {
                    if (res.success) {
                        btn.textContent = 'Applied ✓';
                        btn.style.color = '#4caf50';
                    } else {
                        btn.disabled = false;
                        btn.textContent = 'Force reapply';
                        btn.style.color = '';
                        btn.title = res.message || 'Unknown error';
                        showPopup({ type: 'error', title: 'Error', message: 'Failed: ' + (res.message || 'Unknown error'), autoClose: 5000 });
                    }
                })
                .catch(function() {
                    btn.disabled = false;
                    btn.textContent = 'Force reapply';
                    btn.style.color = '';
                    showPopup({ type: 'error', title: 'Error', message: 'Request failed — check logs', autoClose: 5000 });
                });
            });
            row.appendChild(name);
            row.appendChild(btn);
            manageList.appendChild(row);
        });
    }

    function openManageModal() {
        manageModal.style.display = 'flex';
        if (manageSearch) manageSearch.value = '';
        manageList.innerHTML = '<span style="color:#aaa;font-size:0.85em;">Loading...</span>';
        fetch('/settings/api/plex-boxsets/status')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                _allCollections = (d.success && d.collections) ? d.collections : [];
                renderCollectionRows('');
                if (manageSearch) manageSearch.focus();
            })
            .catch(function() {
                manageList.innerHTML = '<span style="color:#f44336;font-size:0.85em;">Failed to load collections.</span>';
            });
    }

    if (manageSearch) {
        manageSearch.addEventListener('input', function() { renderCollectionRows(this.value); });
    }
    if (manageBtn) manageBtn.addEventListener('click', openManageModal);
    if (manageClose) manageClose.addEventListener('click', function() { manageModal.style.display = 'none'; });
    if (manageModal) manageModal.addEventListener('click', function(e) { if (e.target === manageModal) manageModal.style.display = 'none'; });
})();
