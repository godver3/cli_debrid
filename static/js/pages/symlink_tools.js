/* Moved from templates/symlink_tools.html. */
document.addEventListener('DOMContentLoaded', function() {
    // ── Classic theme JS ────────────────────────────────────────────
    const mediaTypeSelect = document.getElementById('media_type');
    const episodeFields = document.getElementById('episodeFields');
    const seasonNumberInput = document.getElementById('season_number');
    const episodeNumberInput = document.getElementById('episode_number');
    const validateFileBtn = document.getElementById('validateFileBtn');
    const previewPathBtn = document.getElementById('previewPathBtn');
    const sourceFileInput = document.getElementById('source_file');
    const fileValidation = document.getElementById('fileValidation');
    const pathPreview = document.getElementById('pathPreview');
    const browseFilesBtn = document.getElementById('browseFilesBtn');
    const fileBrowserModal = document.getElementById('fileBrowserModal');
    const closeFileBrowser = document.getElementById('closeFileBrowser');
    const goUpBtn = document.getElementById('goUpBtn');
    const goHomeBtn = document.getElementById('goHomeBtn');
    const currentPath = document.getElementById('currentPath');
    const fileList = document.getElementById('fileList');
    const selectFileBtn = document.getElementById('selectFileBtn');
    const cancelFileBtn = document.getElementById('cancelFileBtn');

    let currentDirectory = '/';
    let selectedFile = null;
    const mediaExtensions = ['.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.ts', '.mts', '.m2ts'];

    if (mediaTypeSelect) mediaTypeSelect.addEventListener('change', function() {
        if (this.value === 'episode') {
            episodeFields.style.display = 'block';
            seasonNumberInput.required = true;
            episodeNumberInput.required = true;
        } else {
            episodeFields.style.display = 'none';
            seasonNumberInput.required = false;
            episodeNumberInput.required = false;
        }
    });

    if (validateFileBtn) validateFileBtn.addEventListener('click', function() {
        const filePath = sourceFileInput.value.trim();
        if (!filePath) { showValidation('Please enter a file path', false); return; }
        fetch('/symlink_tools/api/validate-file', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_path: filePath })
        }).then(r => r.json()).then(data => showValidation(data.message, data.valid))
          .catch(e => showValidation('Error: ' + e.message, false));
    });

    if (previewPathBtn) previewPathBtn.addEventListener('click', function() {
        const formData = {
            title: document.getElementById('title').value,
            year: document.getElementById('year').value,
            media_type: document.getElementById('media_type').value,
            version: document.getElementById('version').value,
            content_source: document.getElementById('content_source').value,
            imdb_id: document.getElementById('imdb_id').value,
            tmdb_id: document.getElementById('tmdb_id').value,
            filename: sourceFileInput.value ? sourceFileInput.value.split('/').pop() : '',
            season_number: document.getElementById('season_number').value,
            episode_number: document.getElementById('episode_number').value,
            episode_title: document.getElementById('episode_title').value
        };
        fetch('/symlink_tools/api/preview-path', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(formData)
        }).then(r => r.json()).then(data => {
            if (data.success) {
                pathPreview.innerHTML = `<div class="path">${data.path}</div>`;
                if (data.exists) pathPreview.innerHTML += `<div class="exists">⚠️ This symlink already exists</div>`;
            } else {
                pathPreview.innerHTML = `<div style="color:#dc3545">${data.message}</div>`;
            }
        }).catch(e => { pathPreview.innerHTML = `<div style="color:#dc3545">Error: ${e.message}</div>`; });
    });

    function showValidation(msg, isValid) {
        if (!fileValidation) return;
        fileValidation.textContent = msg;
        fileValidation.className = 'validation-message ' + (isValid ? 'valid' : 'invalid');
    }

    if (sourceFileInput) {
        let vt;
        sourceFileInput.addEventListener('input', function() {
            clearTimeout(vt);
            vt = setTimeout(() => { if (this.value.trim()) validateFileBtn.click(); else { fileValidation.textContent = ''; fileValidation.className = 'validation-message'; } }, 500);
        });
    }

    if (browseFilesBtn) browseFilesBtn.addEventListener('click', () => { fileBrowserModal.style.display = 'block'; loadDir('/'); });
    if (closeFileBrowser) closeFileBrowser.addEventListener('click', () => { fileBrowserModal.style.display = 'none'; });
    if (cancelFileBtn) cancelFileBtn.addEventListener('click', () => { fileBrowserModal.style.display = 'none'; });
    if (goUpBtn) goUpBtn.addEventListener('click', () => { if (currentDirectory !== '/') { const p = currentDirectory.split('/').slice(0,-1).join('/') || '/'; loadDir(p); } });
    if (goHomeBtn) goHomeBtn.addEventListener('click', () => loadDir('/'));
    if (selectFileBtn) selectFileBtn.addEventListener('click', () => { if (selectedFile) { sourceFileInput.value = selectedFile; fileBrowserModal.style.display = 'none'; validateFileBtn.click(); } });
    window.addEventListener('click', e => { if (e.target === fileBrowserModal) fileBrowserModal.style.display = 'none'; });

    function loadDir(path) {
        currentDirectory = path;
        if (currentPath) currentPath.textContent = path;
        if (goUpBtn) goUpBtn.disabled = path === '/';
        if (fileList) fileList.innerHTML = '<div class="loading">Loading...</div>';
        fetch('/symlink_tools/api/browse-files', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path })
        }).then(r => r.json()).then(data => {
            if (data.success) displayClassicFiles(data.items);
            else if (fileList) fileList.innerHTML = `<div class="error-message">${data.message}</div>`;
        }).catch(e => { if (fileList) fileList.innerHTML = `<div class="error-message">Error: ${e.message}</div>`; });
    }

    function displayClassicFiles(items) {
        if (!fileList) return;
        if (!items.length) { fileList.innerHTML = '<div class="loading">Directory is empty</div>'; return; }
        fileList.innerHTML = '';
        items.forEach(item => {
            const el = document.createElement('div');
            el.className = 'file-item'; el.dataset.path = item.path;
            const icon = document.createElement('div'); icon.className = 'file-item-icon'; icon.textContent = item.is_dir ? '📁' : '📄';
            const info = document.createElement('div'); info.className = 'file-item-info';
            const name = document.createElement('div'); name.className = 'file-item-name'; name.textContent = item.name;
            if (!item.is_dir) { const ext = item.name.toLowerCase().substring(item.name.lastIndexOf('.')); if (mediaExtensions.includes(ext)) name.classList.add('media-extensions'); }
            const details = document.createElement('div'); details.className = 'file-item-details';
            details.textContent = item.is_dir ? 'Directory' : formatSize(item.size) + ' • ' + new Date(item.modified).toLocaleDateString();
            info.appendChild(name); info.appendChild(details); el.appendChild(icon); el.appendChild(info);
            el.addEventListener('click', () => {
                if (item.is_dir) { loadDir(item.path); }
                else { document.querySelectorAll('.file-item').forEach(f => f.classList.remove('selected')); el.classList.add('selected'); selectedFile = item.path; if (selectFileBtn) selectFileBtn.disabled = false; }
            });
            fileList.appendChild(el);
        });
    }

    function formatSize(bytes) {
        if (bytes === null || bytes === undefined) return 'Unknown';
        if (bytes === 0) return '0 B';
        const s = ['B','KB','MB','GB','TB'], i = Math.floor(Math.log(bytes) / Math.log(1024));
        return Math.round(bytes / Math.pow(1024, i) * 100) / 100 + ' ' + s[i];
    }

    // ── Tangerine theme JS ──────────────────────────────────────────
    const tngMediaType   = document.getElementById('tngMediaType');
    const tngEpSection   = document.getElementById('tngEpisodeSection');
    const tngSeason      = document.getElementById('tngSeason');
    const tngEpisode     = document.getElementById('tngEpisode');
    const tngSourceFile  = document.getElementById('tngSourceFile');
    const tngValidation  = document.getElementById('tngFileValidation');
    const tngValidateBtn = document.getElementById('tngValidateBtn');
    const tngPreviewBtn  = document.getElementById('tngPreviewBtn');
    const tngPathPreview = document.getElementById('tngPathPreview');
    const tngBrowseBtn   = document.getElementById('tngBrowseBtn');
    const tngModal       = document.getElementById('tngFileBrowser');
    const tngCloseModal  = document.getElementById('tngCloseModal');
    const tngGoUp        = document.getElementById('tngGoUp');
    const tngGoHome      = document.getElementById('tngGoHome');
    const tngCurrentPath = document.getElementById('tngCurrentPath');
    const tngFileList    = document.getElementById('tngFileList');
    const tngSelectFile  = document.getElementById('tngSelectFile');
    const tngCancelFile  = document.getElementById('tngCancelFile');

    let tngDir = '/';
    let tngSelected = null;

    if (tngMediaType) tngMediaType.addEventListener('change', function() {
        const isEp = this.value === 'episode';
        tngEpSection.style.display = isEp ? '' : 'none';
        if (tngSeason) tngSeason.required = isEp;
        if (tngEpisode) tngEpisode.required = isEp;
    });

    function tngShowValidation(msg, ok) {
        if (!tngValidation) return;
        tngValidation.textContent = msg;
        tngValidation.className = 'st-validation ' + (ok ? 'valid' : 'invalid');
    }

    function tngValidateFile(path) {
        if (!path) { tngShowValidation('Please enter a file path', false); return; }
        fetch('/symlink_tools/api/validate-file', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_path: path })
        }).then(r => r.json()).then(d => tngShowValidation(d.message, d.valid))
          .catch(e => tngShowValidation('Error: ' + e.message, false));
    }

    if (tngValidateBtn) tngValidateBtn.addEventListener('click', () => tngValidateFile(tngSourceFile.value.trim()));
    if (tngSourceFile) {
        let vt2;
        tngSourceFile.addEventListener('input', function() {
            clearTimeout(vt2);
            vt2 = setTimeout(() => { if (this.value.trim()) tngValidateFile(this.value.trim()); else tngValidation.className = 'st-validation'; }, 500);
        });
    }

    if (tngPreviewBtn) tngPreviewBtn.addEventListener('click', function() {
        const formData = {
            title: document.getElementById('tngTitle').value,
            year: document.getElementById('tngYear').value,
            media_type: document.getElementById('tngMediaType').value,
            version: document.getElementById('tngVersion').value,
            content_source: document.getElementById('tngContentSource').value,
            imdb_id: document.getElementById('tngImdbId').value,
            tmdb_id: document.getElementById('tngTmdbId').value,
            filename: tngSourceFile.value ? tngSourceFile.value.split('/').pop() : '',
            season_number: document.getElementById('tngSeason').value,
            episode_number: document.getElementById('tngEpisode').value,
            episode_title: document.getElementById('tngEpisodeTitle') ? document.getElementById('tngEpisodeTitle').value : ''
        };
        fetch('/symlink_tools/api/preview-path', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(formData)
        }).then(r => r.json()).then(data => {
            if (data.success) {
                tngPathPreview.innerHTML = '<div class="st-path">' + data.path + '</div>';
                if (data.exists) tngPathPreview.innerHTML += '<div class="st-exists"><i class="fas fa-triangle-exclamation"></i> This symlink already exists</div>';
            } else {
                tngPathPreview.innerHTML = '<div class="st-error">' + data.message + '</div>';
            }
        }).catch(e => { tngPathPreview.innerHTML = '<div class="st-error">Error: ' + e.message + '</div>'; });
    });

    function tngLoadDir(path) {
        tngDir = path;
        if (tngCurrentPath) tngCurrentPath.textContent = path;
        if (tngGoUp) tngGoUp.disabled = path === '/';
        if (tngFileList) tngFileList.innerHTML = '<div class="st-file-msg">Loading…</div>';
        fetch('/symlink_tools/api/browse-files', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path })
        }).then(r => r.json()).then(data => {
            if (data.success) tngDisplayFiles(data.items);
            else if (tngFileList) tngFileList.innerHTML = '<div class="st-file-msg">' + data.message + '</div>';
        }).catch(e => { if (tngFileList) tngFileList.innerHTML = '<div class="st-file-msg">Error: ' + e.message + '</div>'; });
    }

    function tngDisplayFiles(items) {
        if (!tngFileList) return;
        if (!items.length) { tngFileList.innerHTML = '<div class="st-file-msg">Directory is empty</div>'; return; }
        tngFileList.innerHTML = '';
        items.forEach(item => {
            const el = document.createElement('div');
            el.className = 'st-file-item';
            const ext = item.name.toLowerCase().substring(item.name.lastIndexOf('.'));
            const isMedia = !item.is_dir && mediaExtensions.includes(ext);
            el.innerHTML =
                '<div class="st-file-item-icon">' + (item.is_dir ? '<i class="fas fa-folder" style="color:#f59e0b"></i>' : '<i class="fas fa-file' + (isMedia ? '-video" style="color:var(--st-accent)"' : '"') + '></i>') + '</div>' +
                '<div class="st-file-item-name' + (isMedia ? ' media' : '') + '">' + item.name + '</div>' +
                '<div class="st-file-item-details">' + (item.is_dir ? 'Directory' : formatSize(item.size) + ' · ' + new Date(item.modified).toLocaleDateString()) + '</div>';
            el.addEventListener('click', () => {
                if (item.is_dir) { tngLoadDir(item.path); }
                else {
                    document.querySelectorAll('.st-file-item').forEach(f => f.classList.remove('selected'));
                    el.classList.add('selected');
                    tngSelected = item.path;
                    if (tngSelectFile) tngSelectFile.disabled = false;
                }
            });
            tngFileList.appendChild(el);
        });
    }

    if (tngBrowseBtn) tngBrowseBtn.addEventListener('click', () => { tngModal.style.display = 'flex'; tngLoadDir('/'); });
    if (tngCloseModal) tngCloseModal.addEventListener('click', () => { tngModal.style.display = 'none'; });
    if (tngCancelFile) tngCancelFile.addEventListener('click', () => { tngModal.style.display = 'none'; });
    if (tngGoUp) tngGoUp.addEventListener('click', () => { const p = tngDir.split('/').slice(0,-1).join('/') || '/'; tngLoadDir(p); });
    if (tngGoHome) tngGoHome.addEventListener('click', () => tngLoadDir('/'));
    if (tngSelectFile) tngSelectFile.addEventListener('click', () => {
        if (tngSelected && tngSourceFile) {
            tngSourceFile.value = tngSelected;
            tngModal.style.display = 'none';
            tngValidateFile(tngSelected);
        }
    });
    window.addEventListener('click', e => { if (e.target === tngModal) tngModal.style.display = 'none'; });
});
