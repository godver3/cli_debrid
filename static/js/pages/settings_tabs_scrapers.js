/* Moved from templates/settings_tabs/scrapers.html. */
function initializeScrapersFunctionality() {
    initializeAddScraperButton();
    initializeDeleteButtons();
    initializeScraperEnabledListeners();
    updateAllScraperHeaderColors();
}

function updateAllScraperHeaderColors() {
    document.querySelectorAll('#scrapers .settings-section').forEach(section => {
        const checkbox = section.querySelector('input[type="checkbox"][id$="-enabled"]');
        if (checkbox) {
            updateScraperHeaderColor(section, checkbox.checked);
        }
    });
}

function updateScraperHeaderColor(section, isEnabled) {
    const header = section.querySelector('.settings-section-header');
    if (header) {
        if (isEnabled) {
            header.classList.remove('scraper-disabled');
        } else {
            header.classList.add('scraper-disabled');
        }
    }
}

function initializeScraperEnabledListeners() {
    document.querySelectorAll('#scrapers input[type="checkbox"][id$="-enabled"]').forEach(checkbox => {
        checkbox.addEventListener('change', function() {
            const section = this.closest('.settings-section');
            updateScraperHeaderColor(section, this.checked);
        });
    });
}

function initializeAddScraperButton() {
    const addButton = document.querySelector('#add-scraper-btn');
    if (addButton) {
        addButton.addEventListener('click', function(event) {
            event.preventDefault();
            showAddScraperPopup();
        });
    } else {
        console.error('Add scraper button not found');
    }
}

function showAddScraperPopup() {
    window.showPopup({
        type: POPUP_TYPES.PROMPT,
        title: 'Add New Scraper',
        message: 'Select the type of scraper you want to add:',
        dropdownOptions: [
            { value: 'Zilean', text: 'Zilean' },
            { value: 'MediaFusion', text: 'MediaFusion' },
            { value: 'AIOStreams', text: 'AIOStreams (Stremio Endpoint)' },
            { value: 'AIOStreams-API', text: 'AIOStreams (API)' },
            { value: 'Jackett', text: 'Jackett' },
            { value: 'Torrentio', text: 'Torrentio' },
            { value: 'Nyaa', text: 'Nyaa' },
            { value: 'Prowlarr', text: 'Prowlarr' },
            { value: 'Newznab', text: 'Newznab (Usenet Indexer)' }
        ],
        confirmText: 'Add',
        cancelText: 'Cancel',
        onConfirm: (selectedType) => {
            if (selectedType) {
                addScraper(selectedType);
            } else {
                console.error('No scraper type selected');
            }
        }
    });
}

function _doAddScraper(scraperType, extraData) {
    window.Loading.show('Adding scraper...', 'Please wait while the scraper is being added.', true);
    fetch('/settings/scrapers/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({ type: scraperType }, extraData || {}))
    })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    if (typeof window.reloadTabContent === 'function') {
                        window.reloadTabContent('scrapers', () => {
                            window.initializeExpandCollapse();
                            initializeScrapersFunctionality();
                            // Also reload scraping tab to update scraper priorities
                            window.reloadTabContent('scraping', () => {
                                window.Loading.hide();
                                window.showPopup({
                                    type: POPUP_TYPES.SUCCESS,
                                    message: 'Scraper added and settings updated successfully',
                                });
                            });
                        });
                    } else {
                        window.Loading.hide();
                        console.error('reloadTabContent is not a function');
                        window.showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: 'Scraper added, but failed to reload tab content',
                        });
                    }
                } else {
                    window.Loading.hide();
                    throw new Error(data.error || 'Unknown error');
                }
            })
            .catch(error => {
                window.Loading.hide();
                console.error('Error adding scraper:', error);
                window.showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'Error adding scraper: ' + error.message
                });
            });
}

function addScraper(scraperType) {
    if (scraperType === 'Newznab') {
        window.showPopup({
            type: POPUP_TYPES.PROMPT,
            title: 'Name Your Indexer',
            message: 'Enter a name for this Newznab indexer (e.g. NZBGeek, DrunkenSlug):',
            inputPlaceholder: 'MyIndexer',
            confirmText: 'Add',
            cancelText: 'Cancel',
            onConfirm: (name) => {
                if (!name || !name.trim()) {
                    window.showPopup({ type: POPUP_TYPES.ERROR, message: 'A name is required for Newznab indexers.' });
                    return;
                }
                _doAddScraper('Newznab', { name: name.trim() });
            }
        });
    } else {
        window.showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Add Scraper',
            message: `Are you sure you want to add a scraper of type: ${scraperType}?`,
            confirmText: 'Add',
            cancelText: 'Cancel',
            onConfirm: () => _doAddScraper(scraperType)
        });
    }
}

function deleteScraper(scraperId) {
    window.showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Delete Scraper',
        message: 'Are you sure you want to delete this scraper?',
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: () => {
            // Show loading dialog immediately after confirmation
            window.Loading.show('Deleting scraper...', 'Please wait while the scraper is being deleted.', true);

            fetch('/settings/scrapers/delete', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ scraper_id: scraperId })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    if (typeof window.reloadTabContent === 'function') {
                        window.reloadTabContent('scrapers', () => {
                            window.initializeExpandCollapse();
                            initializeScrapersFunctionality();
                            // Also reload scraping tab to update scraper priorities
                            window.reloadTabContent('scraping', () => {
                                window.Loading.hide();
                                window.showPopup({
                                    type: POPUP_TYPES.SUCCESS,
                                    message: 'Scraper deleted and settings updated successfully',
                                });
                            });
                        });
                    } else {
                        window.Loading.hide();
                        console.error('reloadTabContent is not a function');
                        window.showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: 'Scraper deleted, but failed to reload tab content',
                        });
                    }
                } else {
                    window.Loading.hide();
                    throw new Error(data.error || 'Unknown error');
                }
            })
            .catch(error => {
                window.Loading.hide();
                console.error('Error deleting scraper:', error);
                window.showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'Error deleting scraper: ' + error.message
                });
            });
        }
    });
}

function initializeDeleteButtons() {
    const deleteButtons = document.querySelectorAll('.delete-scraper-btn');
    deleteButtons.forEach(button => {
        button.addEventListener('click', function() {
            const scraperId = this.getAttribute('data-scraper-id');
            deleteScraper(scraperId);
        });
    });
}

function initializeSortable() {
    const scraperList = document.getElementById('scraper-sortable-list');
    if (scraperList && typeof Sortable !== 'undefined') {
        new Sortable(scraperList, {
            animation: 150,
            handle: '.drag-handle',
            ghostClass: 'sortable-ghost',
            chosenClass: 'sortable-chosen',
            dragClass: 'sortable-drag',
            onEnd: function(evt) {
                // Get the new order of scraper IDs
                const scraperOrder = [];
                const sections = document.querySelectorAll('#scraper-sortable-list .settings-section');
                sections.forEach(section => {
                    const scraperId = section.getAttribute('data-scraper-id');
                    if (scraperId) {
                        scraperOrder.push(scraperId);
                    }
                });

                // Send the new order to the backend
                fetch('/settings/scrapers/reorder', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ scraper_order: scraperOrder })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        console.log('Scrapers reordered successfully');
                    } else {
                        console.error('Error reordering scrapers:', data.error);
                        window.showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: 'Error reordering scrapers: ' + (data.error || 'Unknown error')
                        });
                    }
                })
                .catch(error => {
                    console.error('Error reordering scrapers:', error);
                    window.showPopup({
                        type: POPUP_TYPES.ERROR,
                        message: 'Error reordering scrapers: ' + error.message
                    });
                });
            }
        });
    }
}

document.addEventListener('dashboardInitialized', function() {
    initializeScrapersFunctionality();
    initializeSortable();
});

function toggleZileanDbFields(scraperId, show) {
    const fields = document.getElementById(scraperId + '-db-fields');
    if (fields) fields.style.display = show ? 'block' : 'none';
}
