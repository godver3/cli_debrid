document.addEventListener('DOMContentLoaded', function() {
    const saveSettingsButton = document.getElementById('saveSettingsButton');
    if (saveSettingsButton) {
        saveSettingsButton.addEventListener('click', handleSettingsFormSubmit);
    }

    window.contentSourceSettings = window.contentSourceSettings || {};
    window.scraperSettings = window.scraperSettings || {};
    window.scrapingVersions = window.scrapingVersions || {};
    
    initializeAllFunctionalities();
    
    const lastActiveTab = localStorage.getItem('currentTab') || 'required';
    openTab(lastActiveTab);

    // Only initialize program status check on the settings page
    if (document.querySelector('.settings-container')) {
        checkProgramStatus();
        setInterval(checkProgramStatus, 5000); // Check every 5 seconds
    }

    debugDOMStructure();
});

function debugDOMStructure() {
    console.log('Debugging DOM structure:');
    const settingsForm = document.getElementById('settingsForm');
    const scrapersTab = document.getElementById('scrapers');
    
    if (settingsForm) {
        console.log('Settings form found');
        const tabContents = settingsForm.querySelectorAll('.settings-tab-content');
        console.log(`Found ${tabContents.length} tab contents inside settingsForm`);
        tabContents.forEach((tab, index) => {
            console.log(`Tab ${index + 1}: id="${tab.id}", class="${tab.className}"`);
        });
    } else {
        console.log('Settings form not found');
    }

    if (scrapersTab) {
        console.log('Scrapers tab found');
        const scraperSections = scrapersTab.querySelectorAll('.settings-section');
        console.log(`Found ${scraperSections.length} scraper sections`);
    } else {
        console.log('Scrapers tab not found');
    }
}

function initializeAllFunctionalities() {
    initializeTabSwitching();
    initializeExpandCollapse();
    initializeContentSourcesFunctionality();
    initializeScrapersFunctionality();
    initializeScrapingFunctionality();
    initializeTraktAuthorization();
}

function checkProgramStatus() {
    fetch('/api/program_status')
        .then(response => response.json())
        .then(data => {
            const isRunning = data.running;
            const buttons = document.querySelectorAll('#saveSettingsButton, .add-scraper-link, .add-version-link, .add-source-link, .delete-scraper-btn, .delete-version-btn, .duplicate-version-btn, .delete-source-btn');
            buttons.forEach(button => {
                button.disabled = isRunning;
                button.style.opacity = isRunning ? '0.5' : '1';
                button.style.cursor = isRunning ? 'not-allowed' : 'pointer';
            });

            const runningMessage = document.getElementById('programRunningMessage');
            if (isRunning) {
                if (!runningMessage) {
                    const message = document.createElement('div');
                    message.id = 'programRunningMessage';
                    message.textContent = 'Program is running. Settings management is disabled.';
                    message.style.color = 'red';
                    message.style.marginBottom = '10px';
                    document.querySelector('.settings-container').prepend(message);
                }
            } else if (runningMessage) {
                runningMessage.remove();
            }


        });
}


function initializeTabSwitching() {
    const tabButtons = document.querySelectorAll('.settings-tab-button');
    tabButtons.forEach(button => {
        button.addEventListener('click', function() {
            const tabName = this.getAttribute('data-tab');
            openTab(tabName);
        });
    });
}

function openTab(tabName) {
    const tabContents = document.querySelectorAll('.settings-tab-content');
    const tabButtons = document.querySelectorAll('.settings-tab-button');
    
    tabContents.forEach(content => content.style.display = 'none');
    tabButtons.forEach(button => button.classList.remove('active'));
    
    const activeTab = document.getElementById(tabName);
    activeTab.style.display = 'block';
    document.querySelector(`[data-tab="${tabName}"]`).classList.add('active');

    localStorage.setItem('currentTab', tabName);
}

function reinitializeExpandCollapse() {
    const allSections = document.querySelectorAll('.settings-section');
    allSections.forEach(section => {
        initializeExpandCollapseForSection(section);
    });
    console.log(`Reinitialized expand/collapse for ${allSections.length} sections`);
}

function initializeExpandCollapseForSection(section) {
    const header = section.querySelector('.settings-section-header');
    const content = section.querySelector('.settings-section-content');
    const toggleIcon = header.querySelector('.settings-toggle-icon');

    if (header && content && toggleIcon) {
        header.removeEventListener('click', toggleSection);
        header.addEventListener('click', toggleSection);
    }
}

function initializeExpandCollapse() {
    const allTabContents = document.querySelectorAll('.settings-tab-content');
    
    allTabContents.forEach(tabContent => {
        const expandAllButton = tabContent.querySelector('.settings-expand-all');
        const collapseAllButton = tabContent.querySelector('.settings-collapse-all');
        const sections = tabContent.querySelectorAll('.settings-section');

        sections.forEach(section => initializeExpandCollapseForSection(section));

        if (expandAllButton) {
            expandAllButton.removeEventListener('click', expandAllHandler);
            expandAllButton.addEventListener('click', expandAllHandler);
        }

        if (collapseAllButton) {
            collapseAllButton.removeEventListener('click', collapseAllHandler);
            collapseAllButton.addEventListener('click', collapseAllHandler);
        }
    });
}

function reinitializeAllExpandCollapse() {
    const allSections = document.querySelectorAll('.settings-section');
    allSections.forEach(section => {
        initializeExpandCollapseForSection(section);
    });
    console.log(`Reinitialized expand/collapse for ${allSections.length} sections`);
}

function toggleSection(event) {
    if (!event.target.classList.contains('delete-source-btn')) {
        event.stopPropagation();
        const content = this.nextElementSibling;
        const toggleIcon = this.querySelector('.settings-toggle-icon');
        if (content.style.display === 'none' || content.style.display === '') {
            content.style.display = 'block';
            toggleIcon.textContent = '-';
        } else {
            content.style.display = 'none';
            toggleIcon.textContent = '+';
        }
    }
}

function expandAllHandler(event) {
    const tabContent = event.target.closest('.settings-tab-content');
    expandAll(tabContent);
}

function collapseAllHandler(event) {
    const tabContent = event.target.closest('.settings-tab-content');
    collapseAll(tabContent);
}

function expandAll(tabContent) {
    const sections = tabContent.querySelectorAll('.settings-section-content');
    const toggleIcons = tabContent.querySelectorAll('.settings-toggle-icon');
    sections.forEach(section => section.style.display = 'block');
    toggleIcons.forEach(icon => icon.textContent = '-');
}

function collapseAll(tabContent) {
    const sections = tabContent.querySelectorAll('.settings-section-content');
    const toggleIcons = tabContent.querySelectorAll('.settings-toggle-icon');
    sections.forEach(section => section.style.display = 'none');
    toggleIcons.forEach(icon => icon.textContent = '+');
}

function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

const debouncedUpdateSettings = debounce(updateSettings, 300);

function handleSettingsFormSubmit(event) {
    event.preventDefault();
    try {
        updateSettings()
            .then(() => {
                console.log("Settings updated successfully");
            })
            .catch(error => {
                console.error("Error updating settings:", error);
            });
    } catch (error) {
        console.error("Error in handleSettingsFormSubmit:", error);
    }
}

function updateSettings() {
    const settingsData = {};
    const inputs = document.querySelectorAll('#settingsForm input, #settingsForm select, #settingsForm textarea');
    
    inputs.forEach(input => {
        const name = input.name;
        let value = input.value;
        
        if (input.type === 'checkbox') {
            value = input.checked;
        } else if (value.toLowerCase() === 'true') {
            value = true;
        } else if (value.toLowerCase() === 'false') {
            value = false;
        }

        const nameParts = name.split('.');
        let current = settingsData;
        
        for (let i = 0; i < nameParts.length - 1; i++) {
            if (!current[nameParts[i]]) {
                current[nameParts[i]] = {};
            }
            current = current[nameParts[i]];
        }
        
        current[nameParts[nameParts.length - 1]] = value;
    });
  
    // Ensure 'Content Sources' section exists
    if (!settingsData['Content Sources']) {
        settingsData['Content Sources'] = {};
    }

    // Process Content Sources
    const contentSourcesTab = document.getElementById('content-sources');
    if (contentSourcesTab) {
        console.log("Processing Content Sources tab");
        const contentSourceSections = contentSourcesTab.querySelectorAll('.settings-section');
        console.log(`Found ${contentSourceSections.length} content source sections`);
        
        contentSourceSections.forEach(section => {
            const sourceId = section.getAttribute('data-source-id');
            if (!sourceId) {
                console.log("Skipping section: No source ID found");
                return;
            }
            console.log(`Processing source: ${sourceId}`);

            const sourceData = {};
            sourceData.versions = [];

            section.querySelectorAll('input, select').forEach(input => {
                const nameParts = input.name.split('.');
                const fieldName = nameParts[nameParts.length - 1];
                console.log(`Processing field: ${fieldName}, Type: ${input.type}, Value: ${input.value}, Checked: ${input.checked}`);
                
                if (input.type === 'checkbox') {
                    if (fieldName === 'versions') {
                        if (input.checked) {
                            sourceData.versions.push(input.value);
                        }
                    } else {
                        sourceData[fieldName] = input.checked;
                    }
                } else if (input.type === 'select-multiple') {
                    sourceData[fieldName] = Array.from(input.selectedOptions).map(option => option.value);
                } else {
                    sourceData[fieldName] = input.value;
                }
            });

            console.log(`Source data before final processing:`, JSON.stringify(sourceData));

            // If no versions were checked, ensure versions is an empty array
            if (!sourceData.versions || sourceData.versions.length === 0) {
                sourceData.versions = [];
            }

            // Preserve the existing type if it's not in the form
            if (!sourceData.type) {
                const existingSource = window.contentSourceSettings[sourceId.split('_')[0]];
                if (existingSource && existingSource.type) {
                    sourceData.type = existingSource.type;
                    console.log(`Added type from existing settings: ${sourceData.type}`);
                }
            }

            console.log(`Final source data for ${sourceId}:`, JSON.stringify(sourceData));
            settingsData['Content Sources'][sourceId] = sourceData;
        });
    } else {
        console.log("Content Sources tab not found");
    }

    console.log("Final Content Sources data:", JSON.stringify(settingsData['Content Sources'], null, 2));

    // Remove any 'Unknown' content sources
    if (settingsData['Content Sources'] && typeof settingsData['Content Sources'] === 'object') {
        Object.keys(settingsData['Content Sources']).forEach(key => {
            if (key.startsWith('Unknown_')) {
                delete settingsData['Content Sources'][key];
            }
        });
    }

    // Debug: Log all tabs
    const allTabs = document.querySelectorAll('.tab-content > div');
    console.log(`Found ${allTabs.length} tabs:`);
    allTabs.forEach((tab, index) => {
        console.log(`Tab ${index + 1} id: ${tab.id}`);
    });

    // Process scraper sections
    const scrapersTab = document.getElementById('scrapers');
    console.log(`Scrapers tab found: ${scrapersTab !== null}`);

    if (scrapersTab) {
        const scraperSections = scrapersTab.querySelectorAll('.settings-section');
        console.log(`Found ${scraperSections.length} scraper sections`);

        if (!settingsData['Scrapers']) {
            settingsData['Scrapers'] = {};
        }

        scraperSections.forEach(section => {
            const header = section.querySelector('.settings-section-header h4');
            if (!header) {
                console.log('Skipping section: No header found');
                return;
            }

            const scraperId = header.textContent.trim();
            console.log(`Processing scraper: ${scraperId}`);
            
            const scraperData = {
                type: scraperId.split('_')[0] // Extract type from the scraper ID
            };

            // Collect all input fields for this scraper
            section.querySelectorAll('input, select, textarea').forEach(input => {
                const fieldName = input.name.split('.').pop();
                if (input.type === 'checkbox') {
                    scraperData[fieldName] = input.checked;
                } else {
                    scraperData[fieldName] = input.value;
                }
            });

            console.log(`Collected data for scraper ${scraperId}:`, scraperData);
            settingsData['Scrapers'][scraperId] = scraperData;
        });
    } else {
        console.error('Scrapers tab not found!');
    }

    // Remove any scrapers that are not actual scrapers
    const validScraperTypes = ['Zilean', 'Comet', 'Jackett', 'Torrentio'];
    if (settingsData['Scrapers'] && typeof settingsData['Scrapers'] === 'object') {
        Object.keys(settingsData['Scrapers']).forEach(key => {
            if (settingsData['Scrapers'][key] && !validScraperTypes.includes(settingsData['Scrapers'][key].type)) {
                delete settingsData['Scrapers'][key];
            }
        });
    }

    // Remove any top-level fields that should be nested
    const topLevelFields = ['Plex', 'Overseerr', 'RealDebrid', 'Torrentio', 'Scraping', 'Queue', 'Trakt', 'Debug', 'Content Sources', 'Scrapers'];
    Object.keys(settingsData).forEach(key => {
        if (!topLevelFields.includes(key)) {
            delete settingsData[key];
        }
    });

    const versions = {};
    document.querySelectorAll('.settings-section[data-version-id]').forEach(section => {
        const versionId = section.getAttribute('data-version-id');
        const versionData = {};

        section.querySelectorAll('input, select').forEach(input => {
            if (input.name && input.name.split('.').pop() !== 'display_name') {
                const key = input.name.split('.').pop();
                if (input.type === 'checkbox') {
                    versionData[key] = input.checked;
                } else if (input.type === 'number') {
                    versionData[key] = parseFloat(input.value) || 0;
                } else {
                    versionData[key] = input.value;
                }
            }
        });

        // Handle filter lists
        ['filter_in', 'filter_out', 'preferred_filter_in', 'preferred_filter_out'].forEach(filterType => {
            const filterList = section.querySelector(`.filter-list[data-version="${versionId}"][data-filter-type="${filterType}"]`);
            versionData[filterType] = [];
            filterList.querySelectorAll('.filter-item').forEach(item => {
                const term = item.querySelector('.filter-term').value.trim();
                if (term) {  // Only add non-empty terms
                    if (filterType.startsWith('preferred_')) {
                        const weight = parseInt(item.querySelector('.filter-weight').value) || 1;
                        versionData[filterType].push([term, weight]);
                    } else {
                        versionData[filterType].push(term);
                    }
                }
            });
        });

        // Add display_name separately to ensure it's always included
        const displayNameInput = section.querySelector('input[name$=".display_name"]');
        if (displayNameInput) {
            versionData['display_name'] = displayNameInput.value;
        }

        versions[versionId] = versionData;
    });

    settingsData['Scraping'] = { versions: versions };
    
    // Ensure TMDB section exists
    if (!settingsData['TMDB']) {
        settingsData['TMDB'] = {};
    }
    
    // Add TMDB API key
    const tmdbApiKeyInput = document.getElementById('tmdb-api-key');
    if (tmdbApiKeyInput) {
        settingsData['TMDB']['api_key'] = tmdbApiKeyInput.value;
    }

    // Add this block to handle the Uncached Handling Method
    const uncachedHandlingSelect = document.getElementById('scraping-uncached-handling');
    console.log("Uncached Handling Select element:", uncachedHandlingSelect);
    
    if (uncachedHandlingSelect) {
        console.log("Uncached Handling Method found. Value:", uncachedHandlingSelect.value);
        console.log("Current settingsData:", JSON.stringify(settingsData, null, 2));
        
        if (!settingsData['Scraping']) {
            console.log("Initializing Scraping section in settingsData");
            settingsData['Scraping'] = {};
        }
        
        console.log("Setting uncached_content_handling value");
        settingsData['Scraping']['uncached_content_handling'] = uncachedHandlingSelect.value;
        
        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Uncached Handling Method select element not found!");
    }

    console.log("Final settings data to be sent:", JSON.stringify(settingsData, null, 2));

    return fetch('/api/settings', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(settingsData)
    })
    .then(response => response.json())
    // In the updateSettings function
    .then(data => {
        console.log("Server response:", data);
        if (data.status === 'success') {
            showNotification('Settings saved successfully', 'success');
            // Update tabs sequentially
            return updateContentSourcesTab()
                .then(() => updateScrapersTab())
                .then(() => updateScrapingTab())
                .then(() => {
                    reinitializeExpandCollapse();
                    console.log("Expand/collapse functionality reinitialized");
                });
        } else {
            throw new Error('Error saving settings');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error saving settings', 'error');
        throw error;
    });
}

function showConfirmationPopup(message, onConfirm) {
    const popup = document.createElement('div');
    popup.className = 'error-popup';
    popup.innerHTML = `
        <div class="error-popup-content">
            <h3>Confirmation</h3>
            <p>${message}</p>
            <button id="confirmYes">Yes</button>
            <button id="confirmNo">No</button>
        </div>
    `;
    document.body.appendChild(popup);

    document.getElementById('confirmYes').addEventListener('click', () => {
        onConfirm();
        popup.remove();
    });

    document.getElementById('confirmNo').addEventListener('click', () => {
        popup.remove();
    });
}

function showNotification(message, type) {
    const popup = document.createElement('div');
    popup.className = 'error-popup';
    popup.innerHTML = `
        <div class="error-popup-content">
            <h3>${type === 'success' ? 'Success' : 'Error'}</h3>
            <p>${message}</p>
            <button onclick="this.closest('.error-popup').remove()">Close</button>
        </div>
    `;
    document.body.appendChild(popup);

    // Automatically remove the popup after 5 seconds
    setTimeout(() => {
        popup.remove();
    }, 5000);
}

function initializeContentSourcesFunctionality() {
    console.log('Initializing Content Sources Functionality');

    const addSourceBtn = document.getElementById('add-source-btn');
    const addSourcePopup = document.getElementById('add-source-popup');
    const cancelAddSourceBtn = document.getElementById('cancel-add-source');
    const addSourceForm = document.getElementById('add-source-form');
    const sourceTypeSelect = document.getElementById('source-type');

    if (addSourceBtn && addSourcePopup) {
        addSourceBtn.addEventListener('click', function(e) {
            e.preventDefault();
            addSourcePopup.style.display = 'block';
            updateDynamicFields('source');
        });
    }

    if (cancelAddSourceBtn && addSourcePopup) {
        cancelAddSourceBtn.addEventListener('click', function() {
            addSourcePopup.style.display = 'none';
        });
    }

    if (addSourceForm) {
        addSourceForm.addEventListener('submit', handleAddSourceSubmit);
    }

    if (sourceTypeSelect) {
        sourceTypeSelect.addEventListener('change', () => updateDynamicFields('source'));
    }

    document.querySelectorAll('.delete-source-btn').forEach(button => {
        button.addEventListener('click', function() {
            const sourceId = this.getAttribute('data-source-id');
            deleteContentSource(sourceId);
        });
    });

    reinitializeAllExpandCollapse();
    initializeExpandCollapse();
}

function initializeScrapersFunctionality() {
    const addScraperBtn = document.getElementById('add-scraper-btn');
    const addScraperPopup = document.getElementById('add-scraper-popup');
    const cancelAddScraperBtn = document.getElementById('cancel-add-scraper');
    const addScraperForm = document.getElementById('add-scraper-form');
    const scraperTypeSelect = document.getElementById('scraper-type');

    if (addScraperBtn && addScraperPopup) {
        addScraperBtn.addEventListener('click', function(e) {
            e.preventDefault();
            addScraperPopup.style.display = 'block';
            updateDynamicFields('scraper');
        });
    }

    if (cancelAddScraperBtn && addScraperPopup) {
        cancelAddScraperBtn.addEventListener('click', function() {
            addScraperPopup.style.display = 'none';
        });
    }

    if (addScraperForm) {
        addScraperForm.addEventListener('submit', handleAddScraperSubmit);
    }

    if (scraperTypeSelect) {
        scraperTypeSelect.addEventListener('change', () => updateDynamicFields('scraper'));
    }

    document.querySelectorAll('.delete-scraper-btn').forEach(button => {
        button.addEventListener('click', function() {
            const scraperId = this.getAttribute('data-scraper-id');
            deleteScraper(scraperId);
        });
    });
}

function initializeScrapingFunctionality() {
    const addVersionBtn = document.getElementById('add-version-btn');
    const addVersionPopup = document.getElementById('add-version-popup');
    const cancelAddVersionBtn = document.getElementById('cancel-add-version');
    const addVersionForm = document.getElementById('add-version-form');

    if (addVersionBtn && addVersionPopup) {
        addVersionBtn.addEventListener('click', function(e) {
            e.preventDefault();
            addVersionPopup.style.display = 'block';
        });
    }

    if (cancelAddVersionBtn && addVersionPopup) {
        cancelAddVersionBtn.addEventListener('click', function() {
            addVersionPopup.style.display = 'none';
        });
    }

    if (addVersionForm) {
        addVersionForm.addEventListener('submit', handleAddVersionSubmit);
    }

    document.querySelectorAll('.duplicate-version-btn').forEach(button => {
        button.addEventListener('click', function() {
            const versionId = this.getAttribute('data-version-id');
            duplicateVersion(versionId);
        });
    });

    document.querySelectorAll('input[name$=".display_name"]').forEach(input => {
        input.addEventListener('change', function() {
            updateVersionDisplayName(this);
        });
    });
    
    document.querySelectorAll('.delete-version-btn').forEach(button => {
        button.addEventListener('click', function() {
            const versionId = this.getAttribute('data-version-id');
            deleteVersion(versionId);
        });
    });

    document.querySelectorAll('.add-filter').forEach(button => {
        button.addEventListener('click', function(e) {
            e.preventDefault();
            const version = this.getAttribute('data-version');
            const filterType = this.getAttribute('data-filter-type');
            addFilterItem(version, filterType);
        });
    });

    document.querySelectorAll('.filter-list').forEach(list => {
        list.addEventListener('click', function(e) {
            if (e.target.classList.contains('remove-filter')) {
                e.target.closest('.filter-item').remove();
                // Remove updateSettings() call from here
            }
        });
    });
}

function addFilterItem(version, filterType) {
    const list = document.querySelector(`.filter-list[data-version="${version}"][data-filter-type="${filterType}"]`);
    const newItem = document.createElement('div');
    newItem.className = 'filter-item';

    if (filterType.startsWith('preferred_')) {
        newItem.innerHTML = `
            <input type="text" class="filter-term" placeholder="Term">
            <input type="number" class="filter-weight" min="1" value="1" placeholder="Weight">
            <button type="button" class="remove-filter">Remove</button>
        `;
    } else {
        newItem.innerHTML = `
            <input type="text" class="filter-term" placeholder="Term">
            <button type="button" class="remove-filter">Remove</button>
        `;
    }

    list.appendChild(newItem);
}

function duplicateVersion(versionId) {
    fetch('/versions/duplicate', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ version_id: versionId })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            updateScrapingTab().then(() => {
                showNotification('Version duplicated successfully', 'success');
            });
        } else {
            showNotification('Error duplicating version: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error duplicating version', 'error');
    });
}

function updateVersionDisplayName(input) {
    const versionId = input.name.split('.')[2];
    const newDisplayName = input.value;
    const header = input.closest('.settings-section').querySelector('h4');
    header.textContent = newDisplayName;

    // Update the server with the new display name
    updateSettings();
}

function updateDynamicFields(type) {
    const typeSelect = document.getElementById(`${type}-type`);
    const dynamicFields = document.getElementById('dynamic-fields');
    if (!typeSelect || !dynamicFields) return;

    const selectedType = typeSelect.value;
    dynamicFields.innerHTML = '';

    const settings = type === 'source' ? window.contentSourceSettings : window.scraperSettings;

    if (!settings || !settings[selectedType]) {
        console.error(`Settings for ${selectedType} are not defined`);
        return;
    }

    Object.entries(settings[selectedType]).forEach(([setting, config]) => {
        // Exclude 'type' field and any field that starts with an underscore
        if (setting !== 'type' && !setting.startsWith('_')) {
            const field = createFormField(setting, '', config.type);
            dynamicFields.appendChild(field);
        }
    });

    // Add version checkboxes for content sources
    if (type === 'source' && window.scrapingVersions) {
        const versionsField = createVersionsField();
        dynamicFields.appendChild(versionsField);
    }
}

function handleAddSourceSubmit(e) {
    e.preventDefault();
    const form = e.target;
    const formData = new FormData(form);
    const sourceData = {
        type: form.elements['source-type'].value // Add type separately
    };
    
    formData.forEach((value, key) => {
        if (key === 'versions') {
            if (!sourceData.versions) {
                sourceData.versions = [];
            }
            if (form.elements[key].checked) {
                sourceData.versions.push(value);
            }
        } else if (form.elements[key].type === 'checkbox') {
            sourceData[key] = form.elements[key].checked;
        } else if (key !== 'source-type') { // Exclude the 'source-type' field
            sourceData[key] = value === 'true' ? true : (value === 'false' ? false : value);
        }
    });

    fetch('/content_sources/add', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(sourceData)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            document.getElementById('add-source-popup').style.display = 'none';
            form.reset();
            return updateContentSourcesTab();
        } else {
            throw new Error(data.error || 'Unknown error');
        }
    })
    .then(() => {
        showNotification('Content source added successfully', 'success');
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error adding content source: ' + error.message, 'error');
        return updateContentSourcesTab();
    });
}

function handleAddScraperSubmit(e) {
    e.preventDefault();
    const form = e.target;
    const formData = new FormData(form);
    const jsonData = {};
    
    formData.forEach((value, key) => {
        if (value === 'true') value = true;
        if (value === 'false') value = false;
        if (form.elements[key].type === 'checkbox') value = form.elements[key].checked;
        jsonData[key] = value;
    });

    // Ensure the scraper type is included
    if (!jsonData.type) {
        console.error('No scraper type provided');
        showNotification('Error: No scraper type provided', 'error');
        return;
    }

    fetch('/scrapers/add', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(jsonData)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            document.getElementById('add-scraper-popup').style.display = 'none';
            form.reset();
            updateScrapersTab().then(() => {
                const newSection = document.querySelector(`.settings-section[data-scraper-id="${data.scraper_id}"]`);
                if (newSection) {
                    initializeExpandCollapseForSection(newSection);
                }
            });
            showNotification('Scraper added successfully', 'success');
        } else {
            showNotification('Error adding scraper: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error adding scraper', 'error');
    });
}

function handleAddVersionSubmit(e) {
    e.preventDefault();
    const form = e.target;
    const formData = new FormData(form);
    const jsonData = Object.fromEntries(formData.entries());

    fetch('/versions/add', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(jsonData)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            document.getElementById('add-version-popup').style.display = 'none';
            form.reset();
            updateScrapingTab().then(() => {
                const newSection = document.querySelector(`.settings-section[data-version-id="${data.version_id}"]`);
                if (newSection) {
                    initializeExpandCollapseForSection(newSection);
                }
            });
            showNotification('Version added successfully', 'success');
        } else {
            showNotification('Error adding version: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error adding version', 'error');
    });
}

function deleteContentSource(sourceId) {
    showConfirmationPopup(`Are you sure you want to delete the source "${sourceId}"?`, () => {
        fetch('/content_sources/delete', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ source_id: sourceId })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                updateContentSourcesTab().then(() => {
                    showNotification('Content source deleted successfully', 'success');
                });
            } else {
                showNotification('Error deleting content source: ' + (data.error || 'Unknown error'), 'error');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showNotification('Error deleting content source', 'error');
        });
    });
}

function deleteScraper(scraperId) {
    showConfirmationPopup(`Are you sure you want to delete the scraper "${scraperId}"?`, () => {
        fetch('/scrapers/delete', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ scraper_id: scraperId })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                updateScrapersTab().then(() => {
                    showNotification('Scraper deleted successfully', 'success');
                });
            } else {
                showNotification('Error deleting scraper: ' + (data.error || 'Unknown error'), 'error');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showNotification('Error deleting scraper', 'error');
        });
    });
}

function deleteVersion(versionId) {
    showConfirmationPopup(`Are you sure you want to delete the version "${versionId}"?`, () => {
        fetch('/versions/delete', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ version_id: versionId })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                updateScrapingTab().then(() => {
                    showNotification('Version deleted successfully', 'success');
                });
            } else {
                showNotification('Error deleting version: ' + (data.error || 'Unknown error'), 'error');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showNotification('Error deleting version', 'error');
        });
    });
}

function updateContentSourcesTab() {
    return fetch('/content_sources/content')
        .then(response => {
            if (!response.ok) {
                throw new Error('Network response was not ok');
            }
            return response.text();
        })
        .then(html => {
            const contentSourcesTab = document.getElementById('content-sources');
            if (contentSourcesTab) {
                contentSourcesTab.innerHTML = html;
                initializeContentSourcesFunctionality();
                displayContentSourceNames();
                initializeExpandCollapse();  // Re-initialize expand/collapse functionality
            }
        })
        .catch(error => {
            console.error('Error updating Content Sources tab:', error);
            showNotification('Error updating Content Sources tab', 'error');
        });
}

function displayContentSourceNames() {
    const sourceHeaders = document.querySelectorAll('#content-sources .settings-section-header h4');
    sourceHeaders.forEach(header => {
        const sourceId = header.textContent.trim();
        const displayNameInput = document.querySelector(`input[name="Content Sources.${sourceId}.display_name"]`);
        if (displayNameInput && displayNameInput.value) {
            header.textContent = displayNameInput.value;
        }
    });
}

function updateScrapersTab() {
    return fetch('/scrapers/content')
        .then(response => response.text())
        .then(html => {
            const scrapersTab = document.getElementById('scrapers');
            if (scrapersTab) {
                scrapersTab.innerHTML = html;
                initializeScrapersFunctionality();
                initializeExpandCollapse();
            }
        })
        .catch(error => {
            console.error('Error updating Scrapers tab:', error);
            showNotification('Error updating Scrapers tab', 'error');
        });
}

function updateScrapingTab() {
    return fetch('/scraping/content')
        .then(response => response.text())
        .then(html => {
            const scrapingTab = document.getElementById('scraping');
            if (scrapingTab) {
                scrapingTab.innerHTML = html;
                initializeScrapingFunctionality();
                initializeExpandCollapse();
            }
        })
        .catch(error => {
            console.error('Error updating Scraping tab:', error);
            showNotification('Error updating Scraping tab', 'error');
        });
}

function createVersionsField() {
    const div = document.createElement('div');
    div.className = 'form-group';
    
    const label = document.createElement('label');
    label.textContent = 'Versions:';
    div.appendChild(label);

    const versionsDiv = document.createElement('div');
    versionsDiv.className = 'version-checkboxes';

    Object.keys(window.scrapingVersions).forEach(version => {
        const checkboxLabel = document.createElement('label');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.name = 'versions';
        checkbox.value = version;
        checkboxLabel.appendChild(checkbox);
        checkboxLabel.appendChild(document.createTextNode(` ${version}`));
        versionsDiv.appendChild(checkboxLabel);
    });

    div.appendChild(versionsDiv);
    return div;
}

function createFormField(setting, value, type) {
    const div = document.createElement('div');
    div.className = 'form-group';
    
    const label = document.createElement('label');
    label.htmlFor = setting;
    label.textContent = setting.charAt(0).toUpperCase() + setting.slice(1) + ':';
    
    let input;
    if (type === 'boolean') {
        input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = value;
    } else {
        input = document.createElement('input');
        input.type = 'text';
        input.value = value;
    }
    
    input.id = setting;
    input.name = setting;
    input.className = 'settings-input';
    
    div.appendChild(label);
    div.appendChild(input);
    return div;
}

function initializeTraktAuthorization() {
    const traktAuthBtn = document.getElementById('trakt-auth-btn');
    const traktAuthStatus = document.getElementById('trakt-auth-status');
    const traktAuthCode = document.getElementById('trakt-auth-code');
    const traktCode = document.getElementById('trakt-code');
    const traktActivateLink = document.getElementById('trakt-activate-link');

    if (traktAuthBtn) {
        traktAuthBtn.addEventListener('click', function() {
            traktAuthBtn.disabled = true;
            traktAuthStatus.textContent = 'Initializing Trakt authorization...';
            traktAuthCode.style.display = 'none';

            fetch('/trakt_auth', { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    console.log('Trakt auth response:', data);
                    if (data.user_code) {
                        traktCode.textContent = data.user_code;
                        traktActivateLink.href = data.verification_url;
                        traktAuthCode.style.display = 'block';
                        traktAuthStatus.textContent = 'Please enter the code on the Trakt website to complete authorization.';
                        pollTraktAuthStatus(data.device_code);
                    } else {
                        traktAuthStatus.textContent = 'Error: ' + (data.error || 'Unable to get authorization code');
                    }
                })
                .catch(error => {
                    console.error('Trakt auth error:', error);
                    traktAuthStatus.textContent = 'Error: Unable to start authorization process';
                })
                .finally(() => {
                    traktAuthBtn.disabled = false;
                });
        });
    }

    // Check initial Trakt authorization status
    checkTraktAuthStatus();
}

function pollTraktAuthStatus(device_code) {
    const traktAuthStatus = document.getElementById('trakt-auth-status');
    const traktAuthCode = document.getElementById('trakt-auth-code');
    const pollInterval = setInterval(() => {
        fetch('/trakt_auth_status', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ device_code: device_code }),
        })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'authorized') {
                    clearInterval(pollInterval);
                    traktAuthStatus.textContent = 'Trakt authorization successful!';
                    traktAuthStatus.classList.add('authorized');
                    traktAuthCode.style.display = 'none';
                    setTimeout(() => {
                        traktAuthStatus.textContent = 'Trakt is currently authorized.';
                    }, 5000);
                } else if (data.status === 'error') {
                    clearInterval(pollInterval);
                    traktAuthStatus.textContent = 'Error: ' + (data.message || 'Unknown error occurred');
                }
            })
            .catch(error => {
                console.error('Error:', error);
                traktAuthStatus.textContent = 'Error checking authorization status. Please try again.';
            });
    }, 5000); // Check every 5 seconds
}

function checkTraktAuthStatus() {
    const traktAuthStatus = document.getElementById('trakt-auth-status');
    fetch('/trakt_auth_status', { method: 'GET' })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'authorized') {
                traktAuthStatus.textContent = 'Trakt is currently authorized.';
                traktAuthStatus.classList.add('authorized');
            } else {
                traktAuthStatus.textContent = 'Trakt is not authorized.';
                traktAuthStatus.classList.remove('authorized');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            traktAuthStatus.textContent = 'Unable to check Trakt authorization status.';
        });
}