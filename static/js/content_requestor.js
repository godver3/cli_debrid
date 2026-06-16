let selectedContent = null;
let availableVersions = [];

// Initialize everything when the DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    // Initialize Loading object
    window.Loading.init();
    
    // Initialize event listeners
    initializeEventListeners();
    
    // Fetch versions
    fetchVersions();
});

function initializeEventListeners() {
    // Add form submit event listener
    const searchForm = document.getElementById('search-form');
    if (searchForm) {
        searchForm.addEventListener('submit', searchMedia);
    }

    // Event listeners for modal buttons
    const confirmVersionsBtn = document.getElementById('confirmVersions');
    if (confirmVersionsBtn) {
        confirmVersionsBtn.addEventListener('click', () => {
            const selectedVersions = Array.from(document.querySelectorAll('input[name="versions"]:checked'))
                .map(checkbox => checkbox.value);
            
            if (selectedVersions.length === 0) {
                window.showPopup({
                    type: window.POPUP_TYPES.WARNING,
                    title: 'Warning',
                    message: 'Please select at least one version'
                });
                return;
            }
            
            requestContent(selectedContent, selectedVersions);
            document.getElementById('versionModal').style.display = 'none';
        });
    }

    const cancelVersionsBtn = document.getElementById('cancelVersions');
    if (cancelVersionsBtn) {
        cancelVersionsBtn.addEventListener('click', () => {
            document.getElementById('versionModal').style.display = 'none';
        });
    }

    // Close modal when clicking outside
    window.addEventListener('click', (event) => {
        const modal = document.getElementById('versionModal');
        if (event.target === modal) {
            modal.style.display = 'none';
        }
    });
}

async function fetchVersions() {
    try {
        const response = await fetch('/content/versions');
        const data = await response.json();
        if (data.versions) {
            availableVersions = data.versions;
        }
    } catch (error) {
        console.error('Error fetching versions:', error);
        window.showPopup({
            type: window.POPUP_TYPES.ERROR,
            title: 'Error',
            message: 'Error fetching versions'
        });
    }
}

async function searchMedia(event) {
    event.preventDefault();
    const searchTerm = document.querySelector('input[name="search_term"]').value;
    
    if (!searchTerm) return;

    window.Loading.show();
    
    try {
        const response = await fetch('/content/search', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ search_term: searchTerm })
        });

        const results = await response.json();
        displayResults(results);
    } catch (error) {
        console.error('Error searching:', error);
        window.showPopup({
            type: window.POPUP_TYPES.ERROR,
            title: 'Error',
            message: 'Error searching for content'
        });
    } finally {
        window.Loading.hide();
    }
}

function displayResults(results) {
    const resultsContainer = document.getElementById('searchResults');
    resultsContainer.innerHTML = '';

    results.forEach(result => {
        const card = document.createElement('div');
        card.className = 'media-card';
        
        const posterUrl = result.posterPath || '/static/images/no-poster.jpg';
        const title = result.title || 'Untitled';
        
        card.innerHTML = `
            <div class="media-poster">
                <img src="${posterUrl}" alt="${title}" loading="lazy">
                <span class="media-type-badge">
                    ${result.mediaType === 'movie' ? 'MOVIE' : 'TV'}
                </span>
                <div class="media-overlay">
                    <div class="media-year">${result.year || 'N/A'}</div>
                    <h3 class="media-title">${title}</h3>
                </div>
            </div>
        `;
        
        // Add click event to show version modal
        card.addEventListener('click', () => showVersionModal(result));
        
        resultsContainer.appendChild(card);
    });
}

function showVersionModal(content) {
    selectedContent = content;
    const modal = document.getElementById('versionModal');
    const versionCheckboxes = document.getElementById('versionCheckboxes');
    
    // Clear existing checkboxes
    versionCheckboxes.innerHTML = '';
    
    // Create checkboxes for each version
    availableVersions.forEach(version => {
        const div = document.createElement('div');
        div.className = 'version-checkbox';
        div.innerHTML = `
            <input type="checkbox" id="${version}" name="versions" value="${version}">
            <label for="${version}">${version}</label>
        `;
        versionCheckboxes.appendChild(div);
    });
    
    // Folder dropdown — symlink mode only
    const folderContainer = document.createElement('div');
    folderContainer.id = 'request-folder-container';
    versionCheckboxes.appendChild(folderContainer);
    (async () => {
        try {
            const fRes = await fetch('/scraper/get_symlink_folders');
            const fData = await fRes.json();
            if (!fData.enabled || !fData.folders || !fData.folders.length) return;
            const fs = fData.folder_settings || {};
            const genreList = (content.genre_ids || content.genres || []).map(g => String(g).trim().toLowerCase());
            const isAnime = genreList.some(g => g.includes('anime') || g.includes('animation') || g === '16');
            const isDoc = genreList.some(g => g.includes('documentary') || g === '99');
            const mediaType = content.mediaType === 'movie' ? 'movie' : 'tv';
            let autoFolder = mediaType === 'movie'
                ? ((isAnime && fs.enable_separate_anime_folders) ? fs.anime_movies_folder_name : (isDoc && fs.enable_separate_documentary_folders) ? fs.documentary_movies_folder_name : fs.movies_folder_name)
                : ((isAnime && fs.enable_separate_anime_folders) ? fs.anime_tv_shows_folder_name : (isDoc && fs.enable_separate_documentary_folders) ? fs.documentary_tv_shows_folder_name : fs.tv_shows_folder_name);
            const filtered = fData.folders.filter(f => {
                if (f.is_custom) return true;
                const n = f.name.toLowerCase();
                return mediaType === 'movie' ? (n.includes('movie') || n === (fs.movies_folder_name||'').toLowerCase()) : (n.includes('show') || n.includes('tv') || n === (fs.tv_shows_folder_name||'').toLowerCase());
            });
            if (!filtered.length) return;
            const div = document.createElement('div'); div.className = 'vm-divider'; folderContainer.appendChild(div);
            const lbl = document.createElement('div'); lbl.className = 'section-label'; lbl.textContent = 'Folder'; folderContainer.appendChild(lbl);
            const sel = document.createElement('select'); sel.id = 'request-folder-select';
            sel.style.cssText = 'width:100%;padding:8px 10px;background:#1a1a1a;color:#fff;border:1px solid #333;border-radius:6px;font-size:12px;margin-top:4px;';
            filtered.forEach(f => { const o = document.createElement('option'); o.value = f.name; o.dataset.isCustom = f.is_custom ? 'true' : 'false'; o.textContent = f.is_custom ? `${f.name} (${mediaType === 'movie' ? fs.movies_folder_name : fs.tv_shows_folder_name})` : f.name; if (f.name === autoFolder) o.selected = true; sel.appendChild(o); });
            folderContainer.appendChild(sel);
        } catch(e) {}
    })();

    // Tags multi-select — Plex mode only
    const tagsContainer = document.createElement('div');
    tagsContainer.id = 'request-tags-container';
    versionCheckboxes.appendChild(tagsContainer);
    (async () => {
        try {
            const cfgR = await fetch('/settings/api/config');
            const cfgD = await cfgR.json();
            const globalTags = (cfgD['Tags'] || {})['tags_list'] || [];
            const fileMode = (cfgD['File Management'] || {})['file_collection_management'] || '';
            if (fileMode !== 'Plex' || !globalTags.length) return;
            const div2 = document.createElement('div'); div2.className = 'vm-divider'; tagsContainer.appendChild(div2);
            const lbl2 = document.createElement('div'); lbl2.className = 'section-label'; lbl2.textContent = 'Tags'; tagsContainer.appendChild(lbl2);
            const pillWrap2 = document.createElement('div');
            pillWrap2.id = 'request-tags-pills';
            pillWrap2.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;';
            globalTags.forEach(tag => {
                const pill = document.createElement('div');
                pill.className = 'option-row';
                pill.dataset.value = tag;
                pill.dataset.type = 'tag';
                pill.style.cssText = 'padding:5px 14px;border-radius:14px;cursor:pointer;font-size:12px;flex:none;';
                pill.innerHTML = `<span class="option-label">${tag}</span>`;
                pill.addEventListener('click', () => pill.classList.toggle('checked'));
                pillWrap2.appendChild(pill);
            });
            tagsContainer.appendChild(pillWrap2);
        } catch(e) {}
    })();

    modal.style.display = 'block';
}

async function requestContent(content, selectedVersions) {
    const folderSelect = document.getElementById('request-folder-select');
    const selectedFolder = folderSelect ? folderSelect.value : null;
    const selectedFolderIsCustom = folderSelect ? (folderSelect.options[folderSelect.selectedIndex]?.dataset?.isCustom === 'true') : false;
    window.Loading.show();
    try {
        const body = { ...content, versions: selectedVersions };
        if (selectedFolder) { body.selected_folder = selectedFolder; body.selected_folder_is_custom = selectedFolderIsCustom; }
        const response = await fetch('/content/request', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            ...((() => {
                const _tp3 = document.querySelectorAll('#request-tags-pills .option-row.checked[data-type="tag"]');
                const _st3 = Array.from(_tp3).map(p=>p.dataset.value).join(',');
                if (_st3) body.selected_tags = _st3;
                return {};
            })()),
            body: JSON.stringify(body)
        });

        const result = await response.json();
        if (result.success) {
            window.showPopup({
                type: window.POPUP_TYPES.SUCCESS,
                title: 'Success',
                message: `Successfully requested ${content.title}`,
                autoClose: 3000
            });
        } else {
            window.showPopup({
                type: window.POPUP_TYPES.ERROR,
                title: 'Error',
                message: result.error || 'Error requesting content'
            });
        }
    } catch (error) {
        console.error('Error requesting content:', error);
        window.showPopup({
            type: window.POPUP_TYPES.ERROR,
            title: 'Error',
            message: 'Error requesting content'
        });
    } finally {
        window.Loading.hide();
    }
} 