// Declare settingsData globally
let settingsData = {};

// Function to load settings data
function loadSettingsData() {
    return fetch('/settings/api/program_settings')
        .then(response => response.json())
        .then(data => {
            settingsData = data;
            return data;
        })
        .catch(error => {
            console.error('Error loading settings:', error);
        });
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

    // Ensure UI Settings section exists
    if (!settingsData['UI Settings']) {
        settingsData['UI Settings'] = {};
    }

    // Save user system enabled setting
    const userSystemEnabledCheckbox = document.querySelector('input[name="UI Settings.enable_user_system"]');
    if (userSystemEnabledCheckbox) {
        settingsData['UI Settings']['enable_user_system'] = userSystemEnabledCheckbox.checked;
    }

    // Process Notification settings
    const notificationsTab = document.getElementById('notifications');
    if (notificationsTab) {
        console.log("Processing Notifications tab");
        const notificationSections = notificationsTab.querySelectorAll('.settings-section');
        console.log(`Found ${notificationSections.length} notification sections`);

        if (!settingsData['Notifications']) {
            settingsData['Notifications'] = {};
        }

        notificationSections.forEach(section => {
            const notificationId = section.getAttribute('data-notification-id');
            if (!notificationId) {
                console.log("Skipping section: No notification ID found");
                return;
            }
            console.log(`Processing notification: ${notificationId}`);

            const notificationData = {};

            // Get the title from the header
            const headerElement = section.querySelector('.settings-section-header h4');
            if (headerElement) {
                notificationData.title = headerElement.textContent.split('_')[0].trim();
            }

            section.querySelectorAll('input, select').forEach(input => {
                const fieldName = input.name.split('.').pop();
                if (input.type === 'checkbox') {
                    notificationData[fieldName] = input.checked;
                } else {
                    notificationData[fieldName] = input.value;
                }
            });

            console.log(`Notification data for ${notificationId}:`, JSON.stringify(notificationData));
            settingsData['Notifications'][notificationId] = notificationData;
        });
    } else {
        console.log("Notifications tab not found");
    }

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
    const validScraperTypes = ['Zilean', 'Comet', 'Jackett', 'Torrentio', 'Nyaa'];
    if (settingsData['Scrapers'] && typeof settingsData['Scrapers'] === 'object') {
        Object.keys(settingsData['Scrapers']).forEach(key => {
            if (settingsData['Scrapers'][key] && !validScraperTypes.includes(settingsData['Scrapers'][key].type)) {
                delete settingsData['Scrapers'][key];
            }
        });
    }

    // Update the list of top-level fields to include UI Settings
    const topLevelFields = ['Plex', 'Overseerr', 'RealDebrid', 'Torrentio', 'Scraping', 'Queue', 'Trakt', 'Debug', 'Content Sources', 'Scrapers', 'Notifications', 'TMDB', 'UI Settings'];
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
                    if (key === 'max_size_gb') {
                        versionData[key] = input.value === '' ? Infinity : parseFloat(input.value) || 0;
                    } else {
                        versionData[key] = parseFloat(input.value) || 0;
                    }
                } else {
                    versionData[key] = input.value;
                }
            }
        });

        // Add max_size_gb with default Infinity if it doesn't exist
        if (!('max_size_gb' in versionData)) {
            versionData['max_size_gb'] = Infinity;
        }

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
    const uncachedHandlingSelect = document.getElementById('scraping-uncached_content_handling');
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

    // Handle the Jackett Seeders Only checkbox
    const jackettSeedersOnly = document.getElementById('scraping-jackett_seeders_only');
    console.log("Jackett Seeders Only element:", jackettSeedersOnly);

    const softMaxSize = document.getElementById('scraping-soft_max_size_gb');
    console.log("Soft Max Size element:", softMaxSize);
    
    if (jackettSeedersOnly) {
        console.log("Jackett Seeders Only found. Checked:", jackettSeedersOnly.checked);
        
        if (!settingsData['Scraping']) {
            console.log("Initializing Scraping section in settingsData");
            settingsData['Scraping'] = {};
        }
        
        console.log("Setting jackett_seeders_only value");
        settingsData['Scraping']['jackett_seeders_only'] = jackettSeedersOnly.checked;
        
        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Jackett Seeders Only checkbox element not found!");
    }

    if (softMaxSize) {
        console.log("Soft Max Size found. Checked:", softMaxSize.checked);

        settingsData['Scraping']['soft_max_size_gb'] = softMaxSize.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Soft Max Size checkbox element not found!");
    }

    const ultimateSortOrder = document.getElementById('scraping-ultimate_sort_order');
    console.log("Ultimate Sort Order element:", ultimateSortOrder);
    
    if (ultimateSortOrder) {
        settingsData['Scraping']['ultimate_sort_order'] = ultimateSortOrder.value;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Ultimate Sort Order select element not found!");
    }

    const metadataBatteryUrl = document.getElementById('metadata battery-url');
    console.log("Metadata Battery URL element:", metadataBatteryUrl);
    
    if (metadataBatteryUrl) {
        // Ensure 'Metadata Battery' object exists in settingsData
        if (!settingsData['Metadata Battery']) {
            settingsData['Metadata Battery'] = {};
        }
        settingsData['Metadata Battery']['url'] = metadataBatteryUrl.value;
    
        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Metadata Battery URL input element not found!");
    }

    const sortByUncachedStatus = document.getElementById('debug-sort_by_uncached_status');
    console.log("Sort By Uncached Status element:", sortByUncachedStatus);
    
    if (sortByUncachedStatus) {
        settingsData['Debug']['sort_by_uncached_status'] = sortByUncachedStatus.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Sort By Uncached Status checkbox element not found!");
    }

    const checkingQueuePeriod = document.getElementById('debug-checking_queue_period');
    console.log("Checking Queue Period element:", checkingQueuePeriod);
    
    if (checkingQueuePeriod) {
        settingsData['Debug']['checking_queue_period'] = parseInt(checkingQueuePeriod.value) || 3600;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Checking Queue Period input element not found!");
    }

    // Handle Content Source check periods
    const contentSourceCheckPeriods = {};
    document.querySelectorAll('#content-source-check-periods input').forEach(input => {
        const sourceName = input.id.replace('debug-content-source-', '');
        const value = input.value.trim();
        if (value !== '') {
            contentSourceCheckPeriods[sourceName] = parseInt(value) || 1;
        }
    });
    settingsData['Debug'] = settingsData['Debug'] || {};
    settingsData['Debug']['content_source_check_period'] = contentSourceCheckPeriods;

    // Handle Reverse Parser settings
    const reverseParserSettings = {
        version_terms: {},
        default_version: document.getElementById('default-version').value,
        version_order: [] // New array to store the order
    };

    // Get the container of all version inputs
    const versionContainer = document.querySelector('#version-terms-container');
    
    // Get all version inputs in their current order
    const versionInputs = Array.from(versionContainer.children);
    
    versionInputs.forEach((input, index) => {
        const version = input.getAttribute('data-version');
        const terms = input.querySelector('.version-terms').value.split(',').map(term => term.trim()).filter(term => term);
        reverseParserSettings.version_terms[version] = terms;
        reverseParserSettings.version_order.push(version); // Add version to order array
    });

    settingsData['Reverse Parser'] = reverseParserSettings;


    const rescrapeMissingFiles = document.getElementById('debug-rescrape_missing_files');
    console.log("Rescrape Missing Files element:", rescrapeMissingFiles);
    
    if (rescrapeMissingFiles) {
        settingsData['Debug']['rescrape_missing_files'] = rescrapeMissingFiles.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Rescrape Missing Files checkbox element not found!");
    }

    const enableUpgrading = document.getElementById('debug-enable_upgrading'); 
    console.log("Enable Upgrading element:", enableUpgrading);
    
    if (enableUpgrading) {
        settingsData['Debug']['enable_upgrading'] = enableUpgrading.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Enable Upgrading checkbox element not found!");
    }

    const enableUpgradingCleanup = document.getElementById('debug-enable_upgrading_cleanup');
    console.log("Enable Upgrading Cleanup element:", enableUpgradingCleanup);
    
    if (enableUpgradingCleanup) {
        settingsData['Debug']['enable_upgrading_cleanup'] = enableUpgradingCleanup.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Enable Upgrading Cleanup checkbox element not found!");
    }

    console.log("Final settings data to be sent:", JSON.stringify(settingsData, null, 2));

    return fetch('/settings/api/settings', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(settingsData)
    })
    .then(response => response.json())
    .then(data => {
        console.log("Server response:", data);
        if (data.status === 'success') {
            showPopup({ type: POPUP_TYPES.SUCCESS, title: 'Success', message: 'Settings saved successfully' });
        } else {
            showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: 'Error saving settings: ' + data.message });
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showPopup({type: POPUP_TYPES.ERROR, title: 'Error', message: 'Error saving settings:' + data.message });
        throw error;
    });
}

function updateContentSourceCheckPeriods() {
    const contentSourcesDiv = document.getElementById('content-source-check-periods');
    if (!contentSourcesDiv) {
        console.warn("Element with id 'content-source-check-periods' not found. Skipping update.");
        return;
    }

    const enabledContentSources = Object.keys(settingsData['Content Sources'] || {}).filter(source => settingsData['Content Sources'][source].enabled);
    
    contentSourcesDiv.innerHTML = '';
    enabledContentSources.forEach(source => {
        const div = document.createElement('div');
        div.className = 'content-source-check-period';
        div.innerHTML = `
            <label for="debug-content-source-${source}">${source}:</label>
            <input type="number" id="debug-content-source-${source}" name="Debug.content_source_check_period.${source}" value="${(settingsData['Debug'] && settingsData['Debug']['content_source_check_period'] && settingsData['Debug']['content_source_check_period'][source]) || ''}" min="1" class="settings-input" placeholder="Default">
        `;
        contentSourcesDiv.appendChild(div);
    });
}

// Update the DOMContentLoaded event listener
document.addEventListener('DOMContentLoaded', () => {
    loadSettingsData().then(() => {
        // Ensure the content-source-check-periods element exists before calling the function
        if (document.getElementById('content-source-check-periods')) {
            updateContentSourceCheckPeriods();
        } else {
            console.warn("Element with id 'content-source-check-periods' not found. Make sure it exists in your HTML.");
        }
        // Add any other initialization functions here
    });
});