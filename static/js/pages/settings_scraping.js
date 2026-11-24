// Page script for templates/settings_tabs/scraping.html.
function initializeScrapingFunctionality() {
    initializeVersionButtons();
    initializeFilterButtons();
    initializeImportButton();
    initializeVersionMenus();
    document.querySelectorAll('.version-card').forEach(updateVersionSummary);
}

// "Add Version" menu entries.
const ADD_VERSION_ACTIONS = {
    blank: () => showAddVersionPopup(),
    default: () => showAddDefaultVersionConfirmation(),
    pair: () => showSeparateVersionsConfirmation(),
    import: () => showImportVersionsDialog(),
};

function initializeImportButton() {
    // Only exists in onboarding, which has its own copy of this flow.
    const importButton = document.querySelector('#import-default-versions-btn');
    if (importButton && !importButton._importBound) {
        importButton._importBound = true;
        importButton.addEventListener('click', event => {
            event.preventDefault();
            showImportConfirmation();
        });
    }
}

// ---- Menus (Add Version, and each version's ⋯ actions) ---------------------
// One set of document listeners, added once: the tab's HTML is replaced after
// add/rename/delete, and listeners on the old elements would go with it.

function closeVersionMenus(except) {
    document.querySelectorAll('.vs-menu').forEach(menu => {
        if (menu === except) return;
        const items = menu.querySelector('.vs-menu-items');
        const toggle = menu.querySelector('.vs-menu-toggle');
        if (items) items.hidden = true;
        if (toggle) toggle.setAttribute('aria-expanded', 'false');
    });
}

// Menus are position: fixed (cards clip overflow), so place them under their
// button, right-aligned and kept on screen.
function placeVersionMenu(toggle, items) {
    const rect = toggle.getBoundingClientRect();
    const width = items.offsetWidth;
    const height = items.offsetHeight;
    const left = Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8));
    const below = rect.bottom + 4;
    const top = below + height > window.innerHeight - 8 ? Math.max(8, rect.top - height - 4) : below;
    items.style.left = `${left}px`;
    items.style.top = `${top}px`;
}

function initializeVersionMenus() {
    if (document._versionMenusBound) return;
    document._versionMenusBound = true;

    document.addEventListener('click', event => {
        const toggle = event.target.closest('.vs-menu-toggle');
        if (toggle) {
            event.preventDefault();
            const menu = toggle.closest('.vs-menu');
            const items = menu.querySelector('.vs-menu-items');
            const opening = items.hidden;
            closeVersionMenus(menu);
            items.hidden = !opening;
            toggle.setAttribute('aria-expanded', String(opening));
            if (opening) {
                placeVersionMenu(toggle, items);
                items.querySelector('[role="menuitem"]')?.focus({preventScroll: true});
            }
            return;
        }
        const addItem = event.target.closest('[data-add-action]');
        if (addItem) {
            event.preventDefault();
            closeVersionMenus();
            ADD_VERSION_ACTIONS[addItem.dataset.addAction]?.();
            return;
        }
        if (!event.target.closest('.vs-menu-items')) closeVersionMenus();
    });

    // A fixed menu would stay put while the page moves under it.
    window.addEventListener('scroll', () => closeVersionMenus(), true);
    window.addEventListener('resize', () => closeVersionMenus());

    document.addEventListener('keydown', event => {
        if (event.key !== 'Escape') return;
        const open = document.querySelector('.vs-menu-items:not([hidden])');
        if (!open) return;
        const toggle = open.closest('.vs-menu').querySelector('.vs-menu-toggle');
        closeVersionMenus();
        toggle?.focus();
    });

    // Keep each card's summary line and filter counts current while editing.
    const refresh = event => {
        const card = event.target.closest?.('.version-card');
        if (card) setTimeout(() => updateVersionSummary(card), 0);
    };
    document.addEventListener('input', refresh);
    document.addEventListener('change', refresh);
    document.addEventListener('click', refresh);
}

// ---- Summary line on each version card -------------------------------------

const RESOLUTION_SIGNS = {'<=': '≤', '==': '=', '>=': '≥'};

function versionInput(card, key) {
    return card.querySelector(`[name$=".${key}"]`);
}

function formatGb(value) {
    const n = parseFloat(value);
    return Number.isFinite(n) ? `${+n.toFixed(2)} GB` : null;
}

function versionSummaryParts(card) {
    const parts = [];
    const resolution = versionInput(card, 'max_resolution')?.value;
    if (resolution) {
        const wanted = versionInput(card, 'resolution_wanted')?.value;
        parts.push(`${RESOLUTION_SIGNS[wanted] || ''} ${resolution}`.trim());
    }
    if (versionInput(card, 'enable_hdr')?.checked) parts.push('HDR');

    const minSize = parseFloat(versionInput(card, 'min_size_gb')?.value);
    const maxSize = formatGb(versionInput(card, 'max_size_gb')?.value);
    const hasMin = Number.isFinite(minSize) && minSize > 0.01;
    if (hasMin && maxSize) parts.push(`${+minSize.toFixed(2)}–${maxSize}`);
    else if (hasMin) parts.push(`≥ ${formatGb(minSize)}`);
    else if (maxSize) parts.push(`≤ ${maxSize}`);

    const anime = versionInput(card, 'anime_filter_mode')?.value;
    if (anime === 'Anime Only') parts.push('anime only');
    else if (anime === 'Non-Anime Only') parts.push('no anime');

    const fallback = versionInput(card, 'fallback_version')?.value;
    if (fallback && fallback !== 'None') parts.push(`fallback: ${fallback}`);
    return parts;
}

function updateVersionSummary(card) {
    const summary = card.querySelector('.vs-summary');
    if (summary) {
        summary.replaceChildren(...versionSummaryParts(card).map(text => {
            const chip = document.createElement('span');
            chip.className = 'vs-chip';
            chip.textContent = text;
            return chip;
        }));
    }
    card.querySelectorAll('.vs-count[data-count-for]').forEach(counter => {
        let count = 0;
        counter.dataset.countFor.split(' ').forEach(type => {
            card.querySelectorAll(`.filter-list[data-filter-type="${type}"] .filter-term`).forEach(input => {
                if (input.value.trim()) count += 1;
            });
        });
        counter.textContent = count ? String(count) : '';
    });
}

function showImportConfirmation() {
    window.showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Add Alternate Default Versions',
        message: 'This will add alternate versions that include comprehensive 2160p, REMUX and WEB versions for the connoisseur.',
        confirmText: 'Add',
        cancelText: 'Cancel',
        onConfirm: () => {
            importDefaultVersions();
        }
    });
}

function showAddDefaultVersionConfirmation() {
    showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Add Default Version',
        message: 'This will add a new version with recommended default settings.',
        confirmText: 'Add',
        cancelText: 'Cancel',
        onConfirm: () => {
            addDefaultVersion();
        }
    });
}

function showSeparateVersionsConfirmation() {
    showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Add Separate Versions',
        message: 'This will add two new versions: one for 1080p content and another for 4K HDR content.',
        confirmText: 'Add Versions',
        cancelText: 'Cancel',
        onConfirm: () => {
            addSeparateVersions();
        }
    });
}

function importDefaultVersions() {
    fetch('/settings/versions/import_defaults', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            window.reloadTabContent('scraping', () => {
                window.initializeExpandCollapse();
                initializeScrapingFunctionality();
                window.showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    message: 'Default versions imported successfully',
                });
            });
        } else {
            throw new Error(data.error || 'Unknown error');
        }
    })
    .catch(error => {
        console.error('Error importing default versions:', error);
        window.showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Error importing default versions: ' + error.message
        });
    });
}

function exportVersion(versionId) {
    const url = '/settings/versions/export?version=' + encodeURIComponent(versionId);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'version_' + versionId.replace(/\s+/g, '_') + '.json';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
}

function showImportVersionsDialog() {
    window.showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Import Versions',
        message: 'Select a JSON file exported from cli_debrid. If a version name already exists you can choose to: <b>Rename</b> (import as a copy), <b>Overwrite</b>, or <b>Skip</b>.',
        confirmText: 'Choose File',
        cancelText: 'Cancel',
        onConfirm: () => {
            document.getElementById('import-versions-file').click();
        }
    });
}

function doImportVersions(file, conflictAction) {
    window.Loading.show('Importing versions...', 'Please wait.', true);
    const fd = new FormData();
    fd.append('file', file);
    fd.append('conflict_action', conflictAction);
    fetch('/settings/versions/import', { method: 'POST', body: fd })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                window.reloadTabContent('scraping', () => {
                    window.Loading.hide();
                    window.initializeExpandCollapse();
                    initializeScrapingFunctionality();
                    window.showPopup({
                        type: POPUP_TYPES.SUCCESS,
                        message: data.message || 'Import complete.'
                    });
                });
            } else {
                window.Loading.hide();
                window.showPopup({ type: POPUP_TYPES.ERROR, message: 'Import failed: ' + (data.error || 'Unknown error') });
            }
        })
        .catch(err => {
            window.Loading.hide();
            window.showPopup({ type: POPUP_TYPES.ERROR, message: 'Import error: ' + err.message });
        });
}

// Hidden file input for "Import from file". Listened for on the document: the
// tab's HTML (input included) is replaced after add/rename/delete.
document.addEventListener('change', function(event) {
    const fileInput = event.target;
    if (fileInput.id !== 'import-versions-file') return;
    const file = fileInput.files[0];
    if (!file) return;
    fileInput.value = '';
    window.showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Conflict Resolution',
        message: 'If a version with the same name already exists, what should happen?\n\n• <b>Rename</b> — import with "(imported)" suffix\n• <b>Overwrite</b> — replace the existing version\n\nClick Cancel to skip duplicates.',
        confirmText: 'Rename',
        cancelText: 'Overwrite',
        onConfirm: () => doImportVersions(file, 'rename'),
        onCancel: () => doImportVersions(file, 'overwrite')
    });
});

function showAddVersionPopup() {
    window.showPopup({
        type: POPUP_TYPES.PROMPT,
        title: 'Add New Version',
        message: 'Enter a name for the new version:',
        inputPlaceholder: 'Version name',
        confirmText: 'Add',
        cancelText: 'Cancel',
        onConfirm: (versionName) => {
            if (versionName) {
                addVersion(versionName);
            } else {
                console.error('No version name entered');
            }
        }
    });
}

function addVersion(versionName) {
    // Show loading dialog immediately after confirmation
    window.Loading.show('Adding version...', 'Please wait while the version is being added.', true);

    fetch('/settings/versions/add', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ name: versionName })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            window.reloadTabContent('scraping', () => {
                window.Loading.hide();
                window.initializeExpandCollapse();
                initializeScrapingFunctionality();
                window.showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    message: 'Version added successfully',
                });
            });
        } else {
            window.Loading.hide();
            throw new Error(data.error || 'Unknown error');
        }
    })
    .catch(error => {
        window.Loading.hide();
        console.error('Error adding version:', error);
        window.showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Error adding version: ' + error.message
        });
    });
}

function initializeVersionButtons() {
    const handlers = {
        export: exportVersion,
        rename: renameVersion,
        duplicate: duplicateVersion,
        delete: deleteVersion,
    };
    document.querySelectorAll('[data-version-action]').forEach(btn => {
        if (btn._versionHandler) btn.removeEventListener('click', btn._versionHandler);
        btn._versionHandler = function() {
            closeVersionMenus();
            handlers[this.dataset.versionAction]?.(this.dataset.versionId);
        };
        btn.addEventListener('click', btn._versionHandler);
    });
}

function renameVersion(versionId) {
    window.showPopup({
        type: POPUP_TYPES.PROMPT,
        title: 'Rename Version',
        message: 'Enter a new name for the version:',
        inputPlaceholder: 'New version name',
        confirmText: 'Rename',
        cancelText: 'Cancel',
        onConfirm: (newName) => {
            if (newName) {
                window.Loading.show('Renaming version...', 'Please wait while the version is being renamed.', true);

                fetch('/settings/versions/rename', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ old_name: versionId, new_name: newName })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        window.reloadTabContent('scraping', () => {
                            window.Loading.hide();
                            window.initializeExpandCollapse();
                            window.showPopup({
                                type: POPUP_TYPES.SUCCESS,
                                message: 'Version renamed successfully. Associated database items have also been updated.',
                            });
                        });
                    } else {
                        window.Loading.hide();
                        throw new Error(data.error || 'Unknown error');
                    }
                })
                .catch(error => {
                    window.Loading.hide();
                    console.error('Error renaming version:', error);
                    window.showPopup({
                        type: POPUP_TYPES.ERROR,
                        message: 'Error renaming version: ' + error.message
                    });
                });
            }
        }
    });
}

function duplicateVersion(versionId) {
    window.showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Duplicate Version',
        message: 'Are you sure you want to duplicate this version?',
        confirmText: 'Duplicate',
        cancelText: 'Cancel',
        onConfirm: () => {
            window.Loading.show('Duplicating version...', 'Please wait while the version is being duplicated.', true);

            fetch('/settings/versions/duplicate', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ version_id: versionId })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    window.reloadTabContent('scraping', () => {
                        window.Loading.hide();
                        window.initializeExpandCollapse();
                        initializeScrapingFunctionality();
                        window.showPopup({
                            type: POPUP_TYPES.SUCCESS,
                            message: 'Version duplicated successfully',
                        });
                    });
                } else {
                    window.Loading.hide();
                    throw new Error(data.error || 'Unknown error');
                }
            })
            .catch(error => {
                window.Loading.hide();
                console.error('Error duplicating version:', error);
                window.showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'Error duplicating version: ' + error.message
                });
            });
        }
    });
}

function deleteVersion(versionId) {
    // First, check usage
    fetch(`/settings/versions/check_usage/${versionId}`)
    .then(response => response.json())
    .then(checkData => {
        if (!checkData.success) {
            throw new Error(checkData.error || 'Failed to check version usage.');
        }

        let popupConfig;
        let onConfirmAction;

        if (!checkData.in_use) {
            // Not in use, simple confirm
            popupConfig = {
                type: POPUP_TYPES.CONFIRM,
                title: 'Delete Version',
                message: 'Are you sure you want to delete this version? It is not currently used by any database items.',
                confirmText: 'Delete',
                cancelText: 'Cancel'
            };
            onConfirmAction = () => {
                performDelete(versionId);
            };
        } else if (checkData.is_last) {
            // In use and is the last version
            popupConfig = {
                type: POPUP_TYPES.CONFIRM,
                title: 'Delete Last Version?',
                message: 'This is the last version and it is currently in use. Deleting it will make associated database items versionless. Are you sure?',
                confirmText: 'Delete Anyway',
                cancelText: 'Cancel',
                warning: true
            };
            onConfirmAction = () => {
                performDelete(versionId); // No target version needed
            };
        } else {
            // In use, but other versions exist
            const options = checkData.alternatives.map(alt => ({ text: alt, value: alt }));
            popupConfig = {
                type: POPUP_TYPES.PROMPT,
                title: 'Reassign and Delete Version',
                message: 'This version is currently in use. Choose a version to reassign the associated database items to before deleting:',
                dropdownOptions: options,
                confirmText: 'Reassign and Delete',
                cancelText: 'Cancel'
            };
            onConfirmAction = (selectedVersion) => {
                if (selectedVersion) {
                    performDelete(versionId, selectedVersion);
                } else {
                    window.showPopup({ type: POPUP_TYPES.ERROR, message: 'You must select a version to reassign items to.' });
                }
            };
        }

        console.log('Popup Config:', popupConfig);
        window.showPopup({ ...popupConfig, onConfirm: onConfirmAction });

    })
    .catch(error => {
        console.error('Error checking version usage:', error);
        window.showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Error checking version usage: ' + error.message
        });
    });
}

function performDelete(versionId, targetVersionId = null) {
    // Show loading dialog immediately after confirmation
    window.Loading.show('Deleting version...', 'Please wait while the version is being deleted.', true);

    fetch('/settings/versions/delete', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ version_id: versionId, target_version_id: targetVersionId })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            window.reloadTabContent('scraping', () => {
                window.Loading.hide();
                window.initializeExpandCollapse();
                initializeScrapingFunctionality();
                window.showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    message: data.message || 'Version deleted successfully.',
                });
            });
        } else {
            window.Loading.hide();
            throw new Error(data.error || 'Unknown error');
        }
    })
    .catch(error => {
        window.Loading.hide();
        console.error('Error deleting version:', error);
        window.showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Error deleting version: ' + error.message
        });
    });
}

function initializeFilterButtons() {
    document.querySelectorAll('.add-filter-btn').forEach(btn => {
        // Create a wrapped handler function that can be stored and removed
        const filterButtonHandler = function() {
            const versionId = this.getAttribute('data-version-id');
            const filterType = this.getAttribute('data-filter-type');
            addFilterItem(versionId, filterType);
        };
        
        // Remove old handler if it exists
        if (btn._filterHandler) {
            btn.removeEventListener('click', btn._filterHandler);
        }
        
        // Store and add new handler
        btn._filterHandler = filterButtonHandler;
        btn.addEventListener('click', filterButtonHandler);
    });

    // Initialize remove filter buttons
    document.querySelectorAll('.filter-list').forEach(list => {
        // Create a wrapped handler function
        const removeFilterHandler = function(event) {
            if (event.target.classList.contains('remove-filter')) {
                event.target.closest('.filter-item').remove();
            }
        };
        
        // Remove old handler if it exists
        if (list._removeFilterHandler) {
            list.removeEventListener('click', list._removeFilterHandler);
        }
        
        // Store and add new handler
        list._removeFilterHandler = removeFilterHandler;
        list.addEventListener('click', removeFilterHandler);
    });
}

function addFilterItem(version, filterType) {
    const list = document.querySelector(`.filter-list[data-version="${CSS.escape(version)}"][data-filter-type="${filterType}"]`);
    const newItem = document.createElement('div');
    newItem.className = 'filter-item';

    // Generate unique ID using timestamp and random number
    const uniqueId = `${filterType}-${version}-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

    if (filterType.startsWith('preferred_')) {
        newItem.innerHTML = `
            <input type="text" id="${uniqueId}-term" class="filter-term" placeholder="Term">
            <input type="number" id="${uniqueId}-weight" class="filter-weight" min="1" value="1" placeholder="Weight">
            <button type="button" class="remove-filter">Remove</button>
        `;
    } else {
        newItem.innerHTML = `
            <input type="text" id="${uniqueId}" class="filter-term" placeholder="Term">
            <select class="filter-source">
                <option value="both" selected>Both</option>
                <option value="nzb">NZB</option>
                <option value="debrid">Debrid</option>
            </select>
            <button type="button" class="remove-filter">Remove</button>
        `;
    }

    list.appendChild(newItem);
}

function addDefaultVersion() {
    // Show loading dialog immediately
    window.Loading.show('Adding default version...', 'Please wait while the default version is being added.', true);

    fetch('/settings/versions/add_default', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            window.reloadTabContent('scraping', () => {
                window.Loading.hide();
                window.initializeExpandCollapse();
                initializeScrapingFunctionality();
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    message: 'Default version added successfully',
                });
            });
        } else {
            window.Loading.hide();
            throw new Error(data.error || 'Unknown error');
        }
    })
    .catch(error => {
        window.Loading.hide();
        console.error('Error adding default version:', error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Error adding default version: ' + error.message
        });
    });
}

function addSeparateVersions() {
    // Show loading dialog immediately
    window.Loading.show('Adding separate versions...', 'Please wait while separate versions are being added.', true);

    fetch('/settings/versions/add_separate_versions', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            window.reloadTabContent('scraping', () => {
                window.Loading.hide();
                window.initializeExpandCollapse();
                initializeScrapingFunctionality();
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    message: 'Separate versions added successfully',
                });
            });
        } else {
            window.Loading.hide();
            throw new Error(data.error || 'Unknown error');
        }
    })
    .catch(error => {
        window.Loading.hide();
        console.error('Error adding separate versions:', error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Error adding separate versions: ' + error.message
        });
    });
}

// Remove any existing initialization listeners
document.removeEventListener('dashboardInitialized', initializeScrapingFunctionality);
document.removeEventListener('scrapingContentLoaded', initializeScrapingFunctionality);

// Add new listeners
document.addEventListener('dashboardInitialized', initializeScrapingFunctionality);
document.addEventListener('scrapingContentLoaded', initializeScrapingFunctionality);
