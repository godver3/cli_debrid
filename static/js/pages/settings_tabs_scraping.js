/* Moved from templates/settings_tabs/scraping.html. */
// Define handler functions outside
const addVersionHandler = function(event) {
    event.preventDefault();
    showAddVersionPopup();
};

const defaultVersionHandler = function(event) {
    event.preventDefault();
    showAddDefaultVersionConfirmation();
};

const separateVersionsHandler = function(event) {
    event.preventDefault();
    showSeparateVersionsConfirmation();
};

const importVersionsHandler = function(event) {
    event.preventDefault();
    showImportConfirmation();
};

function initializeScrapingFunctionality() {
    initializeAddVersionButton();
    initializeVersionButtons();
    initializeFilterButtons();
    initializeImportButton();
    initializeDefaultVersionButton();
    initializeSeparateVersionsButton();
}

function initializeAddVersionButton() {
    const addButton = document.querySelector('#add-version-btn');
    if (addButton) {
        addButton.removeEventListener('click', addVersionHandler);
        addButton.addEventListener('click', addVersionHandler);
    } else {
        console.error('Add version button not found');
    }
}

function initializeImportButton() {
    const importButton = document.querySelector('#import-default-versions-btn');
    if (importButton) {
        importButton.removeEventListener('click', importVersionsHandler);
        importButton.addEventListener('click', importVersionsHandler);
    }
    // Button intentionally hidden in settings page, only exists in onboarding
}

function initializeDefaultVersionButton() {
    const addDefaultButton = document.querySelector('#add-default-version-btn');
    if (addDefaultButton) {
        addDefaultButton.removeEventListener('click', defaultVersionHandler);
        addDefaultButton.addEventListener('click', defaultVersionHandler);
    }
}

function initializeSeparateVersionsButton() {
    const addSeparateButton = document.querySelector('#add-separate-versions-btn');
    if (addSeparateButton) {
        addSeparateButton.removeEventListener('click', separateVersionsHandler);
        addSeparateButton.addEventListener('click', separateVersionsHandler);
    }
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

// Wire up Import button and hidden file input
(function() {
    const importBtn = document.getElementById('import-versions-btn');
    const fileInput = document.getElementById('import-versions-file');
    if (importBtn) {
        importBtn.addEventListener('click', showImportVersionsDialog);
    }
    if (fileInput) {
        fileInput.addEventListener('change', function() {
            const file = this.files[0];
            if (!file) return;
            this.value = '';
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
    }
})();

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
    const buttons = {
        '.export-version-btn': exportVersion,
        '.rename-version-btn': renameVersion,
        '.duplicate-version-btn': duplicateVersion,
        '.delete-version-btn': deleteVersion
    };

    for (const [selector, handler] of Object.entries(buttons)) {
        document.querySelectorAll(selector).forEach(btn => {
            // Store the handler function as a property on the button element
            const wrappedHandler = function() {
                const versionId = this.getAttribute('data-version-id');
                handler(versionId);
            };

            // Remove old handler if it exists
            if (btn._versionHandler) {
                btn.removeEventListener('click', btn._versionHandler);
            }

            // Store and add new handler
            btn._versionHandler = wrappedHandler;
            btn.addEventListener('click', wrappedHandler);
        });
    }
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
    const list = document.querySelector(`.filter-list[data-version="${version}"][data-filter-type="${filterType}"]`);
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
