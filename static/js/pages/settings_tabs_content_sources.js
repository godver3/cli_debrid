/* Moved from templates/settings_tabs/content_sources.html. */
let contentSourceSettingsLoaded = false;
let scrapingVersionsLoaded = false;
// Initialize with value from Jinja template
window.currentFileManagementModeForSymlinkVisibility = window.pageData.settings_tabs_content_sources.File_Management_file_collection_manageme;

// ── Virtual Folder Creator ───────────────────────────────────────────────
let _csVfSourceId = null;
function csVfOpen(sourceId) {
    _csVfSourceId = sourceId;
    const tags = window._csTags || [];
    const modal = document.getElementById('cs-vf-modal');
    if (!modal) return;
    // Populate tag checkboxes
    const cbs = document.getElementById('cs-vf-tags');
    cbs.innerHTML = tags.length ? tags.map(t =>
        `<label style="display:flex;align-items:center;gap:8px;padding:6px 0;cursor:pointer;">
            <input type="checkbox" class="cs-vf-tag-cb" value="${t}"> <span>${t}</span>
        </label>`
    ).join('') : '<p style="opacity:.6;font-size:13px;">No global tags defined yet.</p>';
    document.getElementById('cs-vf-name').value = '';
    document.getElementById('cs-vf-combined').checked = true;
    csVfToggleMode();
    modal.style.display = 'flex';
}
function csVfClose() { const m = document.getElementById('cs-vf-modal'); if(m) m.style.display='none'; }
function csVfToggleMode() {
    const combined = document.getElementById('cs-vf-combined').checked;
    document.getElementById('cs-vf-name-row').style.display = combined ? '' : 'none';
}
async function csVfSubmit() {
    const combined = document.getElementById('cs-vf-combined').checked;
    const folderName = document.getElementById('cs-vf-name').value.trim();
    const selectedTags = Array.from(document.querySelectorAll('.cs-vf-tag-cb:checked')).map(cb => cb.value);
    if (!selectedTags.length) { alert('Select at least one tag.'); return; }
    if (combined && !folderName) { alert('Enter a folder name.'); return; }
    const btn = document.getElementById('cs-vf-submit');
    btn.disabled = true; btn.textContent = 'Creating...';
    try {
        const r = await fetch('/settings/api/create_virtual_folder', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ folder_name: folderName, tags: selectedTags, mode: combined ? 'combined' : 'per_tag' })
        });
        const d = await r.json();
        if (d.success) {
            csVfClose();
            if (window.showPopup) window.showPopup({ type: 'success', title: 'Virtual Folder Created', message: d.message });
            else alert(d.message);
        } else {
            if (window.showPopup) window.showPopup({ type: 'error', title: 'Error', message: d.error });
            else alert('Error: ' + d.error);
        }
    } catch(e) {
        alert('Request failed: ' + e);
    }
    btn.disabled = false; btn.textContent = 'Create';
}
// ── End Virtual Folder Creator ───────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function() {
    loadSettings();
    // Tags pill UI init
    fetch('/settings/api/config').then(r=>r.json()).then(cfg=>{
        window._csTags=(cfg['Tags']||{})['tags_list']||[];
    }).catch(()=>{ window._csTags=[]; });
    document.addEventListener('click',function(e){
        if(!e.target.closest('.cs-tags-pill-input')) document.querySelectorAll('.cs-tags-dropdown').forEach(d=>d.style.display='none');
    });
});

function csTgGet(sid){const h=document.getElementById('cs-tags-hidden-'+sid);return h&&h.value.trim()?h.value.split(',').map(t=>t.trim()).filter(Boolean):[];}
function csTgSet(sid,tags){const h=document.getElementById('cs-tags-hidden-'+sid);if(h){h.value=tags.join(',');h.dispatchEvent(new Event('change',{bubbles:true}));}}
function csTgRender(sid){const c=document.getElementById('cs-tags-pill-input-'+sid);if(!c)return;c.querySelectorAll('.cs-tag-pill').forEach(p=>p.remove());const tags=csTgGet(sid),w=c.querySelector('.cs-tags-input-wrapper');tags.forEach(tag=>{const p=document.createElement('span');p.className='cs-tag-pill';p.innerHTML=tag+'<button type="button" onclick="csTgRemove(\''+sid+'\',\''+tag+'\')">&times;</button>';c.insertBefore(p,w);});}
function csTgAdd(sid,tag){tag=tag.trim();if(!tag)return;const tags=csTgGet(sid);if(tags.includes(tag))return;tags.push(tag);if(!(window._csTags||[]).includes(tag)){(window._csTags=window._csTags||[]).push(tag);fetch('/settings/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({Tags:{tags_list:window._csTags}})}).catch(()=>{});}csTgSet(sid,tags);csTgRender(sid);const i=document.getElementById('cs-tags-search-'+sid);if(i){i.value='';csTgFilter(sid);}}
function csTgRemove(sid,tag){csTgSet(sid,csTgGet(sid).filter(t=>t!==tag));csTgRender(sid);}
function csTgFilter(sid){const i=document.getElementById('cs-tags-search-'+sid),d=document.getElementById('cs-tags-dropdown-'+sid);if(!i||!d)return;const q=i.value.trim().toLowerCase(),cur=csTgGet(sid),all=window._csTags||[],matches=all.filter(t=>t.toLowerCase().includes(q)&&!cur.includes(t));d.innerHTML='';if(q&&!all.map(t=>t.toLowerCase()).includes(q)&&!cur.includes(i.value.trim())){const c=document.createElement('div');c.className='cs-tags-dd-item cs-tags-dd-create';c.textContent='+ Create "'+i.value.trim()+'"';c.onclick=()=>{csTgAdd(sid,i.value.trim());d.style.display='none';};d.appendChild(c);}matches.forEach(tag=>{const item=document.createElement('div');item.className='cs-tags-dd-item';item.textContent=tag;item.onclick=()=>{csTgAdd(sid,tag);d.style.display='none';};d.appendChild(item);});d.style.display=d.children.length?'block':'none';}
function csTgKeydown(e,sid){if(e.key==='Enter'){e.preventDefault();const i=document.getElementById('cs-tags-search-'+sid);if(i&&i.value.trim())csTgAdd(sid,i.value.trim());}else if(e.key==='Escape'){const d=document.getElementById('cs-tags-dropdown-'+sid);if(d)d.style.display='none';}}

function loadSettings() {
    Promise.all([
        fetch('/settings/get_content_source_settings').then(response => response.json()),
        fetch('/settings/get_scraping_versions').then(response => response.json())
    ]).then(([contentSourceSettings, scrapingVersions]) => {
        window.contentSourceSettings = contentSourceSettings;
        window.scrapingVersions = scrapingVersions.versions;
        contentSourceSettingsLoaded = true;
        scrapingVersionsLoaded = true;
        initializeContentSourcesFunctionality();
    }).catch(error => {
        console.error('Error fetching settings:', error);
    });
}

function initializeContentSourcesFunctionality() {
    initializeAddSourceButton();
    initializeSourceButtons();
    initializeAssignUserButtons();
    initializePlexLabelsFields();
    initializeAdaptiveListButtons();
    initializeScrobListPickers();
    loadOverseerrUsers();
    updatePlexOnlySections();
    initializeMdblistModeToggles();

    const settingsContainer = document.querySelector('.settings-container');
    if (settingsContainer) {
        settingsContainer.removeEventListener('click', handleImportLikedClick);
        settingsContainer.addEventListener('click', handleImportLikedClick);
    }

    // Set initial visibility for symlink subfolder settings
    document.querySelectorAll('.settings-section[data-source-id]').forEach(section => {
        const sourceId = section.getAttribute('data-source-id');
        if (sourceId) {
            updateSymlinkSubfolderVisibility(sourceId, window.currentFileManagementModeForSymlinkVisibility);
        }
    });

    // Initialize version checkbox monitoring for warning indicators
    initializeVersionWarningUpdates();

    // Initialize version warnings for all sources on page load
    document.querySelectorAll('.settings-section[data-source-id]').forEach(section => {
        const sourceId = section.getAttribute('data-source-id');
        if (sourceId) {
            updateVersionWarning(sourceId);
        }
    });
    // Note: Header colors are set by Jinja template based on config.enabled
}

// Show only the fields belonging to the MDBList list source method that is selected.
// Fields for the other methods stay in the DOM (just hidden) so their values survive
// switching back and forth before saving.
function updateMdblistModeVisibility(sourceId, mode) {
    document.querySelectorAll(`.mdblist-mode-field[data-source-id="${sourceId}"]`).forEach(field => {
        field.style.display = (field.getAttribute('data-mdblist-mode') === mode) ? 'block' : 'none';
    });
}

function initializeMdblistModeToggles() {
    document.querySelectorAll('input[data-mdblist-mode-radio]').forEach(radio => {
        radio.addEventListener('change', function() {
            if (this.checked) {
                updateMdblistModeVisibility(this.getAttribute('data-mdblist-mode-radio'), this.value);
            }
        });
    });

    document.querySelectorAll('.mdblist-mode-group[data-source-id]').forEach(group => {
        const sourceId = group.getAttribute('data-source-id');
        const checked = group.querySelector('input[data-mdblist-mode-radio]:checked');
        updateMdblistModeVisibility(sourceId, checked ? checked.value : 'json_url');
    });
}

function updateSymlinkSubfolderVisibility(sourceId, fileManagementValue) {
    const containers = document.querySelectorAll(`.symlink-subfolder-container[data-source-id="${sourceId}"]`);
    containers.forEach(container => {
        if (container) {
            const show = (fileManagementValue === 'Symlinked/Local');
            container.style.display = show ? 'block' : 'none';
            // Also hide/show the parent settings-form-group to prevent empty gap
            const parent = container.parentElement;
            if (parent && parent.classList.contains('settings-form-group')) {
                parent.style.display = show ? '' : 'none';
            }
        }
    });
}

// Show/hide Plex-only sections (Plex Labels, Plex Collection)
// Visible when: file_collection_management == 'Plex'
//            OR file_collection_management == 'Symlinked/Local' AND media_server_type == 'plex'
window.currentMediaServerType = window.pageData.settings_tabs_content_sources.File_Management_media_server_type;

function updatePlexOnlySections() {
    const fm = window.currentFileManagementModeForSymlinkVisibility || '';
    const ms = window.currentMediaServerType || '';
    const show = !(fm === 'Symlinked/Local' && ms === 'jellyfin');
    document.querySelectorAll('.plex-only-section').forEach(el => {
        el.style.display = show ? '' : 'none';
        // Also hide/show parent settings-form-group to prevent empty gap
        const parent = el.parentElement;
        if (parent && parent.classList.contains('settings-form-group')) {
            parent.style.display = show ? '' : 'none';
        }
    });
    // Tags visible only in Plex mode
    const isPlex = (fm === 'Plex');
    document.querySelectorAll('.cs-tags-plex-only').forEach(el => {
        el.style.display = isPlex ? '' : 'none';
        // Also hide/show parent settings-form-group to prevent empty gap
        const parent = el.parentElement;
        if (parent && parent.classList.contains('settings-form-group')) {
            parent.style.display = isPlex ? '' : 'none';
        }
    });
}

document.addEventListener('DOMContentLoaded', updatePlexOnlySections);

// Listen for changes from other tabs (specifically required.html for file management type)
document.addEventListener('settings-changed', function(event) {
    if (event.detail.section === 'File Management' && event.detail.key === 'file_collection_management') {
        window.currentFileManagementModeForSymlinkVisibility = event.detail.value;
        document.querySelectorAll('.settings-section[data-source-id]').forEach(section => {
            const sourceId = section.getAttribute('data-source-id');
            if (sourceId) {
                updateSymlinkSubfolderVisibility(sourceId, window.currentFileManagementModeForSymlinkVisibility);
            }
        });
        updatePlexOnlySections();
    }
    if (event.detail.section === 'File Management' && event.detail.key === 'media_server_type') {
        window.currentMediaServerType = event.detail.value;
        updatePlexOnlySections();
    }
});

function initializeAddSourceButton() {
    const addButton = document.getElementById('add-source-btn');
    if (addButton) {
        addButton.addEventListener('click', function(event) {
            event.preventDefault();
            if (contentSourceSettingsLoaded && scrapingVersionsLoaded) {
                showAddSourcePopup();
            } else {
                console.error('Settings not loaded yet');
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'Settings are still loading. Please try again in a moment.'
                });
            }
        });
    } else {
        console.error('Add source button not found');
    }
}

function showAddSourcePopup() {
    fetch('/settings/content-sources/types')
        .then(response => response.json())
        .then(data => {
            const sourceTypes = data.source_types;
            const dropdownOptions = sourceTypes.map(type => ({
                value: type,
                text: type === 'MDBList' ? 'MDBList (JSON list URL or API)' :
                      type === 'Overseerr' ? 'Seerr' : type
            }));

            window.showPopup({
                type: POPUP_TYPES.PROMPT,
                title: 'Add New Content Source',
                message: 'Select a content source type:',
                dropdownOptions: dropdownOptions,
                confirmText: 'Add Source',
                cancelText: 'Cancel',
                onConfirm: (selectedType) => {
                    if (selectedType === 'Friends Trakt Watchlist') {
                        showTraktFriendsDropdown();
                    } else if (selectedType === 'Trakt Lists') {
                        addContentSource({
                            type: 'Trakt Lists',
                            url: '',
                            versions: {},
                            media_type: 'All'
                        });
                    } else {
                        addContentSource({
                            type: selectedType,
                            display_name: selectedType,
                            versions: {}
                        });
                    }
                }
            });
        })
        .catch(error => {
            console.error('Error fetching content source types:', error);
            window.showPopup({ type: POPUP_TYPES.ERROR, message: 'Error fetching types.' });
        });
}

function showTraktFriendsDropdown() {
    fetch('/settings/content-sources/trakt-friends')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.friends.length > 0) {
                const dropdownOptions = data.friends.map(friend => ({
                    value: friend.auth_id,
                    text: friend.display_name
                }));

                window.showPopup({
                    type: POPUP_TYPES.PROMPT,
                    title: 'Select Trakt Friend',
                    message: 'Choose a friend\'s Trakt account:',
                    dropdownOptions: dropdownOptions,
                    confirmText: 'Continue',
                    cancelText: 'Cancel',
                    onConfirm: (selectedAuthId) => {
                        const selectedFriend = data.friends.find(friend => friend.auth_id === selectedAuthId);
                        if (selectedFriend) {
                            showTraktFriendOptions(selectedFriend);
                        }
                    }
                });
            } else {
                window.showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'No Trakt friends found. Please add friends on the Trakt Friends page first.'
                });
            }
        })
        .catch(error => {
            console.error('Error fetching Trakt friends:', error);
            window.showPopup({
                type: POPUP_TYPES.ERROR,
                message: 'Error fetching Trakt friends. Please try again.'
            });
        });
}

function showTraktFriendOptions(friend) {
    fetch('/settings/get_scraping_versions')
        .then(response => response.json())
        .then(data => {
            if (data.versions && Array.isArray(data.versions)) {
                const formHtml = `
                    <form id="friendTraktForm">
                        <div class="form-group">
                            <label for="display_name">Display Name</label>
                            <input type="text" id="display_name" name="display_name" value="${friend.display_name}" placeholder="Display name for this source">
                        </div>
                        <div class="form-group">
                            <label for="media_type">Media Type</label>
                            <select id="media_type" name="media_type">
                                <option value="All">All Media Types</option>
                                <option value="Movies">Movies Only</option>
                                <option value="Shows">TV Shows Only</option>
                            </select>
                        </div>
                        <div class="form-group">
                            <label for="versions">Versions</label>
                            <div class="versions-checkboxes">
                                ${data.versions.map(version => `
                                    <div class="checkbox-item">
                                        <input type="checkbox" id="version_${version}" name="version_${version}" value="${version}">
                                        <label for="version_${version}">${version}</label>
                                    </div>
                                `).join('')}
                            </div>
                        </div>
                    </form>
                `;

                window.showPopup({
                    type: POPUP_TYPES.PROMPT,
                    title: 'Configure Friend\'s Trakt Watchlist',
                    message: `Configure ${friend.friend_name}'s Trakt watchlist:`,
                    formHtml: formHtml,
                    confirmText: 'Add Source',
                    cancelText: 'Cancel',
                    onConfirm: (formData) => {
                        let versions = [];
                        for (const version of data.versions) {
                            if (formData[`version_${version}`]) {
                                versions.push(version);
                            }
                        }

                        addContentSource({
                            type: 'Friends Trakt Watchlist',
                            display_name: formData.display_name,
                            auth_id: friend.auth_id,
                            username: friend.username,
                            media_type: formData.media_type,
                            versions: versions
                        });
                    }
                });
            } else {
                console.error('Failed to fetch versions or invalid response format');
                showFallbackTraktFriendOptions(friend);
            }
        })
        .catch(error => {
            console.error('Error fetching versions:', error);
            showFallbackTraktFriendOptions(friend);
        });
}

function showFallbackTraktFriendOptions(friend) {
    const formHtml = `
        <form id="friendTraktForm">
            <div class="form-group">
                <label for="display_name">Display Name</label>
                <input type="text" id="display_name" name="display_name" value="${friend.display_name}" placeholder="Display name for this source">
            </div>
            <div class="form-group">
                <label for="media_type">Media Type</label>
                <select id="media_type" name="media_type">
                    <option value="All">All Media Types</option>
                    <option value="Movies">Movies Only</option>
                    <option value="Shows">TV Shows Only</option>
                </select>
            </div>
            <div class="form-group">
                <label for="versions">Versions (comma separated)</label>
                <input type="text" id="versions" name="versions" placeholder="e.g., 1080p,4K">
            </div>
        </form>
    `;

    window.showPopup({
        type: POPUP_TYPES.PROMPT,
        title: 'Configure Friend\'s Trakt Watchlist',
        message: `Configure ${friend.friend_name}'s Trakt watchlist:`,
        formHtml: formHtml,
        confirmText: 'Add Source',
        cancelText: 'Cancel',
        onConfirm: (formData) => {
            let versions = [];
            if (formData.versions) {
                versions = formData.versions.split(',').map(v => v.trim()).filter(v => v);
            }

            addContentSource({
                type: 'Friends Trakt Watchlist',
                display_name: formData.display_name,
                auth_id: friend.auth_id,
                username: friend.username,
                media_type: formData.media_type,
                versions: versions
            });
        }
    });
}

function addContentSource(sourceData) {
    // Show loading dialog immediately
    window.Loading.show('Adding content source...', 'Please wait while the content source is being added.', true);

    fetch('/settings/content_sources/add', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(sourceData)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            window.reloadTabContent('content-sources', () => {
                window.Loading.hide();
                window.initializeExpandCollapse();
                initializeContentSourcesFunctionality();
                window.showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    message: 'Content source added successfully',
                });
            });
        } else {
            window.Loading.hide();
            throw new Error(data.error || 'Unknown error');
        }
    })
    .catch(error => {
        window.Loading.hide();
        console.error('Error adding content source:', error);
        window.showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Error adding content source: ' + error.message
        });
    });
}

function initializeSourceButtons() {
    const deleteButtons = document.querySelectorAll('.delete-source-btn');
    deleteButtons.forEach(button => {
        button.addEventListener('click', function() {
            const sourceId = this.getAttribute('data-source-id');
            deleteContentSource(sourceId);
        });
    });
}

function deleteContentSource(sourceId) {
    showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Delete Content Source',
        message: 'Are you sure you want to delete this content source?',
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: () => {
            // Show loading dialog immediately after confirmation
            window.Loading.show('Deleting content source...', 'Please wait while the content source is being deleted.', true);

            fetch('/settings/content_sources/delete', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ source_id: sourceId })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    window.reloadTabContent('content-sources', () => {
                        window.Loading.hide();
                        window.initializeExpandCollapse();
                        initializeContentSourcesFunctionality();
                        showPopup({
                            type: POPUP_TYPES.SUCCESS,
                            message: 'Content source deleted successfully',
                        });
                    });
                } else {
                    window.Loading.hide();
                    throw new Error(data.error || 'Unknown error');
                }
            })
            .catch(error => {
                window.Loading.hide();
                console.error('Error deleting content source:', error);
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'Error deleting content source: ' + error.message
                });
            });
        }
    });
}

function handleImportLikedClick(event) {
    if (!event.target.matches('.import-liked-to-source-btn')) {
        return;
    }

    const button = event.target;
    const sourceId = button.getAttribute('data-source-id');
    const urlInput = document.getElementById(`content-sources-${sourceId}-trakt_lists`);

    if (!sourceId || !urlInput) {
        console.error('Could not find source ID or URL/trakt_lists input for import button.');
        window.showPopup({ type: POPUP_TYPES.ERROR, message: 'Internal error: Could not find associated elements.' });
        return;
    }

    button.disabled = true;
    button.textContent = 'Importing...';

    fetch('/settings/trakt/import_liked_lists', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success && data.lists) {
                const likedListUrls = data.lists.map(list => list.url);

                if (likedListUrls.length === 0) {
                     window.showPopup({ type: POPUP_TYPES.INFO, title: 'Import Liked Lists', message: 'No new liked lists found to import.' });
                     return;
                }

                const currentUrlsRaw = urlInput.value.split(',').map(url => url.trim()).filter(url => url);
                const currentUrlsSet = new Set(currentUrlsRaw);

                const newUrlsToAdd = likedListUrls.filter(url => !currentUrlsSet.has(url));

                if (newUrlsToAdd.length === 0) {
                     window.showPopup({ type: POPUP_TYPES.INFO, title: 'Import Liked Lists', message: 'All liked lists are already present in this source.' });
                     return;
                }

                const combinedUrls = currentUrlsRaw.concat(newUrlsToAdd);
                const newUrlString = combinedUrls.join(', ');

                urlInput.value = newUrlString;

                urlInput.dispatchEvent(new Event('change', { bubbles: true })); 

                window.showPopup({ 
                    type: POPUP_TYPES.SUCCESS, 
                    title: 'Import Complete',
                    message: `Added ${newUrlsToAdd.length} new liked list URLs to this source. Please save your settings.`
                });

            } else {
                throw new Error(data.error || 'Failed to fetch liked lists from server.');
            }
        })
        .catch(error => {
            console.error('Error importing liked lists to source:', error);
            window.showPopup({ type: POPUP_TYPES.ERROR, message: 'Error importing liked lists: ' + error.message });
        })
        .finally(() => {
             button.disabled = false;
             button.textContent = 'Import Liked Lists to this Source';
        });
}

function initializeAssignUserButtons() {
    const assignButtons = document.querySelectorAll('.get-user-watchlist-btn');
    assignButtons.forEach(button => {
        button.removeEventListener('click', handleAssignUserClick);
        button.addEventListener('click', handleAssignUserClick);
    });
}

function handleAssignUserClick(event) {
    const sourceId = event.target.getAttribute('data-source-id');
    showUserSelectionPopup(sourceId);
}

function showUserSelectionPopup(sourceId) {
    fetch('/user_token/collect_tokens/get_usernames')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.usernames && data.usernames.length > 0) {
                const dropdownOptions = data.usernames.map(username => ({
                    value: username,
                    text: username
                }));

                window.showPopup({
                    type: POPUP_TYPES.PROMPT,
                    title: 'Select Plex User to Assign',
                    message: 'Choose the user whose credentials should be used for this source:',
                    dropdownOptions: dropdownOptions,
                    confirmText: 'Assign User',
                    cancelText: 'Cancel',
                    onConfirm: (selectedUsername) => {
                        if (selectedUsername) {
                            assignUserToSource(sourceId, selectedUsername);
                        }
                    }
                });
            } else if (data.success && data.usernames.length === 0) {
                 window.showPopup({
                     type: POPUP_TYPES.INFO,
                     title: 'No Users Found',
                     message: 'No Plex user tokens have been collected yet. Please use the "Collect User Plex Tokens" page first.'
                 });
            } else {
                throw new Error(data.error || 'Failed to fetch usernames.');
            }
        })
        .catch(error => {
            console.error('Error fetching stored usernames:', error);
            window.showPopup({
                type: POPUP_TYPES.ERROR,
                message: 'Error fetching user list: ' + error.message
            });
        });
}

function assignUserToSource(sourceId, username) {
    console.log(`Assigning user ${username} to source ${sourceId}`);
     const assignButton = document.querySelector(`.get-user-watchlist-btn[data-source-id="${sourceId}"]`);
     if (assignButton) {
         assignButton.textContent = 'Assigning...';
         assignButton.disabled = true;
     }

    fetch('/user_token/collect_tokens/assign_to_source', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ source_id: sourceId, username: username })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            window.showPopup({
                type: POPUP_TYPES.SUCCESS,
                title: 'User Assigned',
                message: `Successfully assigned ${data.username} and their token to this source. Save settings to persist.`
            });

            const usernameInput = document.getElementById(`content-sources-${sourceId}-username`);
            const tokenInput = document.getElementById(`content-sources-${sourceId}-token`);

            if (usernameInput) {
                usernameInput.value = data.username;
                usernameInput.dispatchEvent(new Event('change', { bubbles: true }));
            } else {
                console.warn(`Could not find username input for source ${sourceId}`);
            }
            if (tokenInput) {
                tokenInput.value = data.token;
                tokenInput.dispatchEvent(new Event('change', { bubbles: true }));
            } else {
                console.warn(`Could not find token input for source ${sourceId}`);
            }

        } else {
            throw new Error(data.error || 'Unknown error during assignment.');
        }
    })
    .catch(error => {
        console.error('Error assigning user to source:', error);
        window.showPopup({
            type: POPUP_TYPES.ERROR,
            title: 'Assignment Failed',
            message: 'Error assigning user: ' + error.message
        });
    })
    .finally(() => {
         if (assignButton) {
             assignButton.textContent = 'Assign User/Token to Source';
             assignButton.disabled = false;
         }
    });
}

function initializeVersionWarningUpdates() {
    // Listen for changes to version checkboxes
    document.addEventListener('change', function(event) {
        if (event.target.type === 'checkbox' && 
            event.target.name && 
            event.target.name.includes('Content Sources') &&
            event.target.name.includes('versions')) {

            // Extract source ID from the checkbox name
            // Format: "Content Sources.SourceID.versions"
            const nameparts = event.target.name.split('.');
            if (nameparts.length >= 2) {
                const sourceId = nameparts[1];
                updateVersionWarning(sourceId);
            }
        }
    });
}

function updateVersionWarning(sourceId) {
    // Since enabled checkbox is removed, we'll always show the warning if no versions are enabled
    // This provides a helpful reminder to users

    // Find all version checkboxes for this source
    const versionCheckboxes = document.querySelectorAll(`input[type="checkbox"][name*="Content Sources.${sourceId}.versions"]`);

    // Check if any version is enabled
    let hasEnabledVersions = false;
    versionCheckboxes.forEach(checkbox => {
        if (checkbox.checked) {
            hasEnabledVersions = true;
        }
    });

    // Find the warning indicator for this source
    const sourceSection = document.querySelector(`.settings-section[data-source-id="${sourceId}"]`);
    if (sourceSection) {
        const warningElement = sourceSection.querySelector('.no-versions-warning');

        // Show warning if no versions are enabled (regardless of source enabled state)
        if (!hasEnabledVersions) {
            // Show warning
            if (warningElement) {
                warningElement.style.display = 'inline-flex';
            } else {
                // Create warning element if it doesn't exist
                const headerH4 = sourceSection.querySelector('.settings-section-header h4');
                if (headerH4) {
                    const warningSpan = document.createElement('span');
                    warningSpan.className = 'no-versions-warning';
                    warningSpan.innerHTML = '<span class="no-versions-warning-icon">⚠️</span>No versions enabled';
                    headerH4.appendChild(warningSpan);
                }
            }
        } else {
            // Hide warning if versions are enabled
            if (warningElement) {
                warningElement.style.display = 'none';
            }
        }
    }
}

function initializePlexLabelsFields() {
    // Initialize all Plex Labels fields on page load
    document.querySelectorAll('[id^="content-sources-"][id$="-plex_labels-enabled"]').forEach(checkbox => {
        // Extract source ID from checkbox ID
        const match = checkbox.id.match(/content-sources-(.+)-plex_labels-enabled/);
        if (match) {
            const sourceId = match[1];
            togglePlexLabelsFields(sourceId);
        }
    });
}

function togglePlexLabelsFields(sourceId) {
    const checkbox = document.getElementById(`content-sources-${sourceId}-plex_labels-enabled`);
    if (!checkbox) return;

    const isEnabled = checkbox.checked;
    const dependentContainer = document.getElementById(`plex-labels-dependent-${sourceId}`);
    const labelModeSelect = document.getElementById(`content-sources-${sourceId}-plex_labels-label_mode`);
    const fixedLabelInput = document.getElementById(`content-sources-${sourceId}-plex_labels-fixed_label`);

    // Enable/disable the dependent fields
    if (labelModeSelect) labelModeSelect.disabled = !isEnabled;
    if (fixedLabelInput) fixedLabelInput.disabled = !isEnabled;

    // Add/remove visual styling for disabled state
    if (dependentContainer) {
        if (isEnabled) {
            dependentContainer.classList.remove('disabled-fields');
        } else {
            dependentContainer.classList.add('disabled-fields');
        }
    }
}

function updatePlexLabelsVisibility(sourceId) {
    const dropdown = document.getElementById(`content-sources-${sourceId}-plex_labels-label_mode`);
    if (!dropdown) return;

    const selectedMode = dropdown.value;
    const fixedContainer = document.getElementById(`plex-labels-fixed-${sourceId}`);

    if (fixedContainer) {
        fixedContainer.style.display = (selectedMode === 'fixed') ? 'block' : 'none';
    }
}

function initializeAdaptiveListButtons() {
    // No longer needed - each adaptive list is its own content source
    // and can be deleted using the standard "Delete Source" button
}

// ── Scrob list multi-select picker ────────────────────────────────────
// Populates the <select multiple> for each Scrob Lists source with the
// user's real Scrob lists (fetched once, shared across all Scrob Lists
// sources on the page), pre-selects previously-saved IDs, and keeps the
// real hidden input (the one actually collected/saved) in sync on change.
let _scrobListsCache = null;
let _scrobListsFetchPromise = null;

function fetchScrobLists() {
    if (_scrobListsFetchPromise) return _scrobListsFetchPromise;
    _scrobListsFetchPromise = fetch('/settings/content-sources/scrob-lists')
        .then(response => response.json())
        .then(data => {
            _scrobListsCache = data.success ? data.lists : [];
            if (!data.success) {
                console.warn('Could not load Scrob lists:', data.error);
            }
            return _scrobListsCache;
        })
        .catch(error => {
            console.error('Error fetching Scrob lists:', error);
            _scrobListsCache = [];
            return _scrobListsCache;
        });
    return _scrobListsFetchPromise;
}

function initializeScrobListPickers() {
    const pickers = document.querySelectorAll('.scrob-list-multiselect');
    if (!pickers.length) return;

    fetchScrobLists().then(lists => {
        pickers.forEach(select => {
            const sourceId = select.getAttribute('data-source-id');
            const hidden = document.getElementById(`content-sources-${sourceId}-scrob_list_ids`);
            const initialIds = new Set(
                (select.getAttribute('data-initial-value') || '')
                    .split(',').map(s => s.trim()).filter(Boolean)
            );

            select.innerHTML = '';
            if (!lists.length) {
                const opt = document.createElement('option');
                opt.value = '';
                opt.disabled = true;
                opt.textContent = 'No Scrob lists found (check URL/API Key in Additional Settings → Scrob)';
                select.appendChild(opt);
                return;
            }

            lists.forEach(list => {
                const opt = document.createElement('option');
                opt.value = String(list.id);
                opt.textContent = `${list.name} (${list.item_count} items)`;
                opt.selected = initialIds.has(String(list.id));
                select.appendChild(opt);
            });

            select.addEventListener('change', () => {
                if (hidden) {
                    hidden.value = Array.from(select.selectedOptions).map(o => o.value).join(',');
                    hidden.dispatchEvent(new Event('change', { bubbles: true }));
                }
            });
        });
    });
}

// ── Overseerr requester checkboxes ────────────────────────────────────
function syncSelectFromCheckboxes(sourceId) {
    const select = document.getElementById('overseerr-requesters-' + sourceId);
    if (!select) return;
    const checks = document.querySelectorAll('.overseerr-req-cb[data-source-id="' + sourceId + '"]');
    Array.from(select.options).forEach(function(opt) { opt.selected = false; });
    checks.forEach(function(cb) {
        if (cb.checked) {
            let opt = select.querySelector('option[value="' + CSS.escape(cb.value) + '"]');
            if (!opt) {
                opt = document.createElement('option');
                opt.value = cb.value;
                select.appendChild(opt);
            }
            opt.selected = true;
        }
    });
}

function buildRequesterCheckboxes(sourceId, users, savedValues) {
    const container = document.getElementById('overseerr-requesters-checks-' + sourceId);
    const select = document.getElementById('overseerr-requesters-' + sourceId);
    if (!container || !select) return;
    container.innerHTML = '';

    const allUsers = [{ value: '__all__', label: 'All users (normal behavior)' }]
        .concat(users.map(function(u) {
            const name = u.displayName || u.email || String(u.id);
            return { value: name, label: name };
        }));

    select.innerHTML = '';
    allUsers.forEach(function(u) {
        const opt = document.createElement('option');
        opt.value = u.value;
        select.appendChild(opt);
    });

    allUsers.forEach(function(u) {
        const isChecked = savedValues.includes(u.value);
        const label = document.createElement('label');
        label.style.cssText = 'display:flex;align-items:center;gap:8px;cursor:pointer;';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = u.value;
        cb.checked = isChecked;
        cb.className = 'overseerr-req-cb';
        cb.dataset.sourceId = sourceId;
        cb.addEventListener('change', function() {
            if (u.value === '__all__' && cb.checked) {
                container.querySelectorAll('.overseerr-req-cb').forEach(function(other) {
                    if (other.value !== '__all__') other.checked = false;
                });
            }
            if (u.value !== '__all__' && cb.checked) {
                const allCb = container.querySelector('.overseerr-req-cb[value="__all__"]');
                if (allCb) allCb.checked = false;
            }
            syncSelectFromCheckboxes(sourceId);
        });
        const opt = select.querySelector('option[value="' + u.value + '"]');
        if (opt) opt.selected = isChecked;

        const span = document.createElement('span');
        span.textContent = u.label;
        label.appendChild(cb);
        label.appendChild(span);
        container.appendChild(label);
    });
}

function loadOverseerrUsers() {
    const selects = document.querySelectorAll('.overseerr-requesters-select');
    selects.forEach(function(select) {
        const sourceId = select.dataset.sourceId;
        const statusEl = document.getElementById('overseerr-requesters-status-' + sourceId);
        const savedValues = Array.from(select.selectedOptions).map(function(o) { return o.value; });
        const effectiveSaved = savedValues.length > 0 ? savedValues : ['__all__'];

        fetch('/settings/content-sources/overseerr-users?source_id=' + encodeURIComponent(sourceId))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                const users = (data.success && data.users) ? data.users : [];
                buildRequesterCheckboxes(sourceId, users, effectiveSaved);
                if (statusEl) {
                    statusEl.textContent = data.success && users.length > 0
                        ? users.length + ' user(s) loaded.'
                        : (data.error ? 'Could not load users: ' + data.error : 'No users found — check URL and API key.');
                }
            })
            .catch(function(err) {
                buildRequesterCheckboxes(sourceId, [], effectiveSaved);
                if (statusEl) statusEl.textContent = 'Failed to reach Seerr: ' + err;
            });
    });
}

// Fire after the page settings are ready
document.addEventListener('DOMContentLoaded', function() {
    loadOverseerrUsers();
    loadCollectionPosterPickers();
    initCollectionIconPickers();
    initPlexLibraryPickers();
});
// Also call after a short delay in case the tab content loads after DOMContentLoaded
setTimeout(function() {
    if (document.querySelectorAll('.plex-collection-poster-picker').length > 0) {
        loadCollectionPosterPickers();
    }
}, 800);

// ── Plex Collection Poster Picker ─────────────────────────────────────────
let _posterDesigns = null;

async function loadCollectionPosterPickers() {
    if (!_posterDesigns) {
        try {
            const resp = await fetch('/settings/content-sources/collection-poster/previews');
            const data = await resp.json();
            _posterDesigns = data.success ? data.designs : [];
        } catch(e) {
            console.error('Failed to load poster designs:', e);
            _posterDesigns = [];
        }
    }
    document.querySelectorAll('.plex-collection-poster-picker').forEach(function(picker) {
        _renderPosterPicker(picker);
    });
}

function _renderPosterPicker(picker) {
    const sourceId = picker.dataset.sourceId;
    const hidden = document.getElementById(`content-sources-${sourceId}-plex_collection-poster_design`);
    const currentVal = hidden ? parseInt(hidden.value || '0') : 0;
    picker.innerHTML = '';
    (_posterDesigns || []).forEach(function(design) {
        const wrap = document.createElement('div');
        wrap.style.cssText = 'flex:0 0 auto; text-align:center; cursor:pointer;';
        const isSelected = design.id === currentVal;
        const img = document.createElement('img');
        img.src = `/settings/content-sources/collection-poster/preview/${design.id}`;
        img.alt = design.name;
        img.style.cssText = `
            width:240px; height:auto; border-radius:10px;
            border:2px solid ${isSelected ? 'var(--accent-color,#e87429)' : 'rgba(255,255,255,0.1)'};
            display:block; margin-bottom:4px; transition:border-color 0.2s;
        `;
        img.onerror = function() {
            // Preview not ready yet — retry after 2 seconds (generation may still be in progress)
            const _img = this;
            _img.style.background='#1a1a2e';
            setTimeout(function() {
                const src = _img.src;
                _img.src = '';
                _img.src = src + '?t=' + Date.now();
            }, 2000);
        };
        const label = document.createElement('div');
        label.textContent = design.name;
        label.style.cssText = `font-size:0.7em; color:${isSelected ? 'var(--accent-color,#e87429)' : '#aaa'}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:244px;`;
        wrap.appendChild(img);
        wrap.appendChild(label);
        wrap.addEventListener('click', function() {
            if (hidden) hidden.value = design.id;
            // Update borders
            picker.querySelectorAll('img').forEach(i => {
                i.style.borderColor = 'rgba(255,255,255,0.1)';
                i.nextElementSibling.style.color = '#aaa';
            });
            img.style.borderColor = 'var(--accent-color,#e87429)';
            label.style.color = 'var(--accent-color,#e87429)';
            // Trigger change for save detection
            if (hidden) hidden.dispatchEvent(new Event('change', {bubbles: true}));
        });
        picker.appendChild(wrap);
    });
}

// ── Plex Collection Icon Picker ───────────────────────────────────────────
let _iconPickerTargetSourceId = null;

function initCollectionIconPickers() {
    // Inject mini logo picker modal once
    if (!document.getElementById('plex-coll-logo-modal')) {
        const modal = document.createElement('div');
        modal.id = 'plex-coll-logo-modal';
        modal.style.cssText = 'display:none; position:fixed; inset:0; z-index:9999; background:rgba(0,0,0,0.7); align-items:center; justify-content:center;';
        modal.innerHTML = `
            <div style="background:#1a1a2a; border:1px solid rgba(255,255,255,0.1); border-radius:12px; width:min(900px,95vw); max-height:85vh; display:flex; flex-direction:column; overflow:hidden;">
                <div style="display:flex; align-items:center; justify-content:space-between; padding:16px 20px; border-bottom:1px solid rgba(255,255,255,0.08);">
                    <span style="font-weight:600; font-size:1em; color:#fff;">Choose Icon</span>
                    <div style="display:flex; gap:10px; align-items:center;">
                        <input type="text" id="plex-coll-logo-search" placeholder="Search logos..." style="background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.12); border-radius:6px; padding:6px 12px; color:#ddd; font-size:0.85em; width:200px;">
                        <button onclick="document.getElementById('plex-coll-logo-modal').style.display='none'" style="background:none; border:none; color:#888; font-size:1.4em; cursor:pointer; line-height:1;">✕</button>
                    </div>
                </div>
                <div style="display:flex; gap:0; flex:1; overflow:hidden;">
                    <div id="plex-coll-logo-cats" style="width:140px; flex-shrink:0; border-right:1px solid rgba(255,255,255,0.08); overflow-y:auto; padding:8px 0;"></div>
                    <div id="plex-coll-logo-grid" style="flex:1; overflow-y:auto; padding:16px; display:grid; grid-template-columns:repeat(auto-fill,minmax(80px,1fr)); gap:10px; align-content:start;"></div>
                </div>
                <div style="padding:12px 20px; border-top:1px solid rgba(255,255,255,0.08); display:flex; justify-content:space-between; align-items:center;">
                    <button onclick="_clearCollIcon()" style="background:none; border:1px solid rgba(255,0,0,0.3); border-radius:6px; padding:6px 14px; color:#f66; cursor:pointer; font-size:0.85em;">Clear (use default)</button>
                    <button onclick="document.getElementById('plex-coll-logo-modal').style.display='none'" style="background:rgba(255,255,255,0.08); border:1px solid rgba(255,255,255,0.15); border-radius:6px; padding:6px 14px; color:#ddd; cursor:pointer; font-size:0.85em;">Cancel</button>
                </div>
            </div>`;
        document.body.appendChild(modal);
        modal.addEventListener('click', function(e) { if (e.target === modal) modal.style.display='none'; });
        document.getElementById('plex-coll-logo-search').addEventListener('input', function() { _filterCollLogos(this.value); });
    }

    document.querySelectorAll('.plex-collection-icon-picker-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            _iconPickerTargetSourceId = btn.dataset.sourceId;
            _openCollIconPicker();
        });
    });
}

let _collLogosCache = null;
let _collActiveCat = 'rating';

async function _openCollIconPicker() {
    const modal = document.getElementById('plex-coll-logo-modal');
    modal.style.display = 'flex';
    if (!_collLogosCache) {
        document.getElementById('plex-coll-logo-grid').innerHTML = '<div style="color:#888; padding:20px;">Loading...</div>';
        try {
            const resp = await fetch('/api/overlays/logos');
            _collLogosCache = (await resp.json()).categories || [];
        } catch(e) { _collLogosCache = []; }
    }
    _renderCollCats();
    _renderCollLogos(_collActiveCat);
}

function _renderCollCats() {
    const el = document.getElementById('plex-coll-logo-cats');
    el.innerHTML = '';
    (_collLogosCache || []).forEach(function(cat) {
        const btn = document.createElement('button');
        btn.textContent = cat.name;
        btn.style.cssText = `display:block; width:100%; text-align:left; padding:8px 16px; background:${cat.name===_collActiveCat?'rgba(255,255,255,0.08)':'none'}; border:none; color:${cat.name===_collActiveCat?'#fff':'#aaa'}; cursor:pointer; font-size:0.82em; transition:background 0.15s;`;
        btn.onclick = function() { _collActiveCat = cat.name; _renderCollCats(); _renderCollLogos(cat.name); };
        el.appendChild(btn);
    });
}

function _renderCollLogos(catName, filter) {
    const grid = document.getElementById('plex-coll-logo-grid');
    grid.innerHTML = '';
    const cat = (_collLogosCache || []).find(c => c.name === catName);
    if (!cat) return;
    const logos = cat.groups ? cat.groups.flatMap(g => g.logos || []) : (cat.logos || []);
    logos.filter(l => !filter || l.name.toLowerCase().includes(filter.toLowerCase())).forEach(function(logo) {
        const wrap = document.createElement('div');
        wrap.style.cssText = 'text-align:center; cursor:pointer; padding:8px; border-radius:8px; background:rgba(255,255,255,0.03); transition:background 0.15s;';
        wrap.onmouseover = function() { this.style.background='rgba(255,255,255,0.08)'; };
        wrap.onmouseout = function() { this.style.background='rgba(255,255,255,0.03)'; };
        const img = document.createElement('img');
        img.src = logo.url || `/api/overlays/logos/serve/${logo.path}`;
        img.style.cssText = 'width:48px; height:48px; object-fit:contain; display:block; margin:0 auto 4px;';
        img.onerror = function() { this.style.display='none'; };
        const label = document.createElement('div');
        label.textContent = logo.name;
        label.style.cssText = 'font-size:0.65em; color:#888; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;';
        wrap.appendChild(img); wrap.appendChild(label);
        wrap.onclick = function() { _selectCollIcon(logo.path || logo.url); };
        grid.appendChild(wrap);
    });
    if (!grid.children.length) grid.innerHTML = '<div style="color:#666; padding:20px; font-size:0.85em;">No logos found</div>';
}

function _filterCollLogos(q) {
    _renderCollLogos(_collActiveCat, q);
}

function _selectCollIcon(path) {
    if (!_iconPickerTargetSourceId) return;
    const hidden = document.getElementById(`content-sources-${_iconPickerTargetSourceId}-plex_collection-poster_icon`);
    if (hidden) { hidden.value = path; hidden.dispatchEvent(new Event('change', {bubbles:true})); }
    // Update button preview
    const btn = document.querySelector(`.plex-collection-icon-picker-btn[data-source-id="${_iconPickerTargetSourceId}"]`);
    if (btn) {
        const preview = btn.querySelector('.plex-coll-icon-preview');
        const label = btn.querySelector('span:last-child');
        if (preview) preview.innerHTML = `<img src="/api/overlays/logos/serve/${path}" style="max-width:28px;max-height:28px;object-fit:contain;" alt=""><svg style="margin-left:4px;opacity:0.5" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>`;
        if (label) label.textContent = path.split('/').pop();
    }
    document.getElementById('plex-coll-logo-modal').style.display = 'none';
}

function _clearCollIcon() {
    if (!_iconPickerTargetSourceId) return;
    const hidden = document.getElementById(`content-sources-${_iconPickerTargetSourceId}-plex_collection-poster_icon`);
    if (hidden) { hidden.value = ''; hidden.dispatchEvent(new Event('change', {bubbles:true})); }
    const btn = document.querySelector(`.plex-collection-icon-picker-btn[data-source-id="${_iconPickerTargetSourceId}"]`);
    if (btn) {
        const preview = btn.querySelector('.plex-coll-icon-preview');
        const label = btn.querySelector('span:last-child');
        if (preview) preview.innerHTML = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.4"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>`;
        if (label) label.textContent = 'Choose icon from library';
    }
    document.getElementById('plex-coll-logo-modal').style.display = 'none';
}

// ── Plex Library Picker ──────────────────────────────────────────────
function initPlexLibraryPickers() {
    document.querySelectorAll('.plex-lib-load-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var sourceId = btn.dataset.sourceId;
            var listEl   = document.getElementById('plex-lib-list-' + sourceId);
            var hiddenEl = document.getElementById('plex-lib-hidden-' + sourceId);
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin" style="margin-right:6px;"></i>Loading…';
            fetch('/settings/api/plex-libraries')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fas fa-rotate" style="margin-right:6px;"></i>Reload Libraries';
                    if (!data.success) {
                        listEl.innerHTML = '<span style="color:#e57373;font-size:0.85em;"><i class="fas fa-circle-exclamation" style="margin-right:4px;"></i>Error: ' + (data.error || 'Could not connect to Plex') + '</span>';
                        return;
                    }
                    var saved = [];
                    try { saved = JSON.parse(hiddenEl.value) || []; } catch(e) {}
                    saved = saved.map(String);

                    listEl.innerHTML = '';
                    data.libraries.forEach(function(lib) {
                        var iconClass = lib.type === 'movie' ? 'fa-film' : 'fa-tv';
                        var checked = saved.includes(String(lib.key));
                        var label = document.createElement('label');
                        label.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer;font-size:0.9em;color:var(--text-secondary,#ccc);';
                        var cb = document.createElement('input');
                        cb.type = 'checkbox';
                        cb.checked = checked;
                        cb.style.cursor = 'pointer';
                        cb.addEventListener('change', function() {
                            var allChecked = Array.from(listEl.querySelectorAll('input[type=checkbox]'))
                                .filter(function(c) { return c.checked; })
                                .map(function(c) { return c.dataset.key; });
                            hiddenEl.value = JSON.stringify(allChecked);
                            hiddenEl.dispatchEvent(new Event('change', {bubbles: true}));
                        });
                        cb.dataset.key = String(lib.key);
                        var icon = document.createElement('i');
                        icon.className = 'fas ' + iconClass;
                        icon.style.cssText = 'width:14px;text-align:center;opacity:0.7;';
                        label.appendChild(cb);
                        label.appendChild(icon);
                        label.appendChild(document.createTextNode(' ' + lib.title + ' (' + lib.key + ')'));
                        listEl.appendChild(label);
                    });
                    if (data.libraries.length === 0) {
                        listEl.innerHTML = '<span style="color:#888;font-size:0.85em;">No movie or show libraries found in Plex.</span>';
                    }
                })
                .catch(function(e) {
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fas fa-photo-film" style="margin-right:6px;"></i>Load Libraries';
                    listEl.innerHTML = '<span style="color:#e57373;font-size:0.85em;"><i class="fas fa-circle-exclamation" style="margin-right:4px;"></i>Failed to reach server.</span>';
                });
        });
    });
}
