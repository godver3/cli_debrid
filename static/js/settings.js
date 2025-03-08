import { showPopup, POPUP_TYPES } from './notifications.js';

// Declare settingsData globally
let settingsData = {};

// Function to load settings data
function loadSettingsData() {
    return fetch('/settings/api/program_settings')
        .then(response => response.json())
        .then(data => {
            settingsData = data;
            // After loading settings data, handle descriptions
            handleDescriptions();
            return data;
        })
        .catch(error => {
            console.error('Error loading settings:', error);
        });
}

// Function to handle descriptions
function handleDescriptions() {
    document.querySelectorAll('.settings-description').forEach(descElement => {
        const description = descElement.textContent;
        if (Array.isArray(description)) {
            // If description is an array, create multiple paragraphs
            const descriptionHtml = description.map(line => 
                `<p class="settings-description">${line}</p>`
            ).join('');
            descElement.outerHTML = descriptionHtml;
        }
    });
}

// Function to toggle Plex section visibility
function togglePlexSection() {
    console.log('togglePlexSection called');
    
    const fileManagementSelect = document.getElementById('file management-file_collection_management');
    const plexSettingsInFileManagement = document.getElementById('plex-settings-in-file-management');
    
    console.log('File Management Select element:', fileManagementSelect);
    console.log('Plex Settings element:', plexSettingsInFileManagement);
    
    if (fileManagementSelect && plexSettingsInFileManagement) {
        const shouldDisplay = fileManagementSelect.value === 'Plex';
        console.log('Should display Plex settings:', shouldDisplay);
        plexSettingsInFileManagement.style.display = shouldDisplay ? 'block' : 'none';
        console.log('Set display style to:', plexSettingsInFileManagement.style.display);
    } else {
        console.warn('Missing required elements - fileManagementSelect or plexSettingsInFileManagement not found');
    }

    // Toggle path input fields and Plex symlink settings
    const originalFilesPath = document.getElementById('file management-original_files_path');
    const symlinkedFilesPath = document.getElementById('file management-symlinked_files_path');
    const plexSymlinkSettings = document.querySelectorAll('.symlink-plex-setting');

    if (fileManagementSelect) {
        const isSymlinked = fileManagementSelect.value === 'Symlinked/Local';
        
        // Handle path fields
        const pathElements = [
            originalFilesPath?.closest('.settings-form-group'),
            symlinkedFilesPath?.closest('.settings-form-group')
        ].filter(Boolean);

        pathElements.forEach(element => {
            element.style.display = isSymlinked ? 'block' : 'none';
        });

        // Handle Plex symlink settings
        plexSymlinkSettings.forEach(element => {
            element.style.display = isSymlinked ? 'block' : 'none';
        });
    }
}

// Function to validate content sources
function validateContentSources(contentSources) {
    for (const [sourceId, source] of Object.entries(contentSources)) {
        if (source.enabled && (!source.versions || source.versions.length === 0)) {
            return {
                valid: false,
                message: `Content source "${sourceId}" is enabled but has no versions enabled. Please enable at least one version or disable the content source.`
            };
        }
    }
    return { valid: true };
}

// Export the updateSettings function
export function updateSettings() {
    settingsData = {}; // Reset settingsData

    // First handle all regular inputs
    const inputs = document.querySelectorAll('#settingsForm input, #settingsForm select, #settingsForm textarea');
    
    inputs.forEach(input => {
        const name = input.name;
        if (!name) return; // Skip inputs without names
        
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

    // Create Custom Post-Processing section if it doesn't exist
    if (!settingsData['Custom Post-Processing']) {
        settingsData['Custom Post-Processing'] = {};
    }

    // Handle custom post-processing settings
    const customPostProcessingInputs = document.querySelectorAll('[id^="additional-"][name^="Custom Post-Processing."]');
    customPostProcessingInputs.forEach(input => {
        const key = input.name.split('.')[1];
        settingsData['Custom Post-Processing'][key] = input.type === 'checkbox' ? input.checked : input.value;
    });

    console.log('Final Custom Post-Processing settings:', settingsData['Custom Post-Processing']);

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

            // Initialize notify_on object
            notificationData.notify_on = {};

            // Process all inputs including checkboxes for notification categories
            section.querySelectorAll('input, select').forEach(input => {
                const nameParts = input.name.split('.');
                if (nameParts.length === 4 && nameParts[2] === 'notify_on') {
                    // This is a notification category checkbox
                    const category = nameParts[3];
                    notificationData.notify_on[category] = input.checked;
                } else {
                    // This is a regular field
                    const fieldName = nameParts[nameParts.length - 1];
                    if (input.type === 'checkbox') {
                        notificationData[fieldName] = input.checked;
                    } else {
                        notificationData[fieldName] = input.value;
                    }
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

    // Validate content sources before saving
    const contentSourceValidation = validateContentSources(settingsData['Content Sources']);
    if (!contentSourceValidation.valid) {
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: contentSourceValidation.message
        });
        return;
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
    const validScraperTypes = ['Zilean', 'MediaFusion', 'Jackett', 'Torrentio', 'Nyaa', 'OldNyaa'];
    if (settingsData['Scrapers'] && typeof settingsData['Scrapers'] === 'object') {
        Object.keys(settingsData['Scrapers']).forEach(key => {
            if (settingsData['Scrapers'][key] && !validScraperTypes.includes(settingsData['Scrapers'][key].type)) {
                delete settingsData['Scrapers'][key];
            }
        });
    }

    // Update the list of top-level fields to include UI Settings
    const topLevelFields = ['Plex', 'Overseerr', 'RealDebrid', 'Debrid Provider','Torrentio', 'Scraping', 'Queue', 'Trakt', 'Debug', 'Content Sources', 'Scrapers', 'Notifications', 'TMDB', 'UI Settings', 'Sync Deletions', 'File Management', 'Subtitle Settings', 'Custom Post-Processing'];
    Object.keys(settingsData).forEach(key => {
        if (!topLevelFields.includes(key)) {
            delete settingsData[key];
        }
    });

    // Handle subtitle providers multi-select
    const subtitleProvidersSelect = document.getElementById('additional-subtitle_providers');
    if (subtitleProvidersSelect) {
        if (!settingsData['Subtitle Settings']) {
            settingsData['Subtitle Settings'] = {};
        }
        settingsData['Subtitle Settings']['subtitle_providers'] = Array.from(subtitleProvidersSelect.selectedOptions).map(option => option.value);
    }

    // Set default values for Subtitle Settings if not set
    if (!settingsData['Subtitle Settings']) {
        settingsData['Subtitle Settings'] = {};
    }
    
    if (settingsData['Subtitle Settings']['enable_subtitles'] === undefined) {
        settingsData['Subtitle Settings']['enable_subtitles'] = false;
    }
    
    if (!settingsData['Subtitle Settings']['subtitle_languages']) {
        settingsData['Subtitle Settings']['subtitle_languages'] = 'eng,zho';
    }
    
    if (!settingsData['Subtitle Settings']['subtitle_providers'] || !settingsData['Subtitle Settings']['subtitle_providers'].length) {
        settingsData['Subtitle Settings']['subtitle_providers'] = ['opensubtitles', 'opensubtitlescom', 'podnapisi', 'tvsubtitles'];
    }
    
    if (!settingsData['Subtitle Settings']['user_agent']) {
        settingsData['Subtitle Settings']['user_agent'] = 'SubDownloader/1.0 (your-email@example.com)';
    }

    // Log the Debug settings before sending
    if (settingsData.Debug && settingsData.Debug.content_source_check_period) {
        console.log('Final Debug content_source_check_period values:', settingsData.Debug.content_source_check_period);
        Object.entries(settingsData.Debug.content_source_check_period).forEach(([key, value]) => {
            console.log(`${key}: value=${value}, type=${typeof value}`);
        });
    }

    const versions = {};
    document.querySelectorAll('.settings-section[data-version-id]').forEach(section => {
        const versionId = section.getAttribute('data-version-id');
        const versionData = {};

        // Handle regular inputs
        section.querySelectorAll('input, select').forEach(input => {
            if (input.name && input.name.split('.').pop() !== 'display_name') {
                const key = input.name.split('.').pop();
                if (input.type === 'checkbox') {
                    versionData[key] = input.checked;
                } else if (input.type === 'number') {
                    // Handle special fields that can be infinity
                    if (key === 'max_size_gb' || key === 'max_bitrate_mbps') {
                        versionData[key] = input.value === '' ? Infinity : parseFloat(input.value) || 0;
                    } else {
                        versionData[key] = parseFloat(input.value) || 0;
                    }
                } else {
                    versionData[key] = input.value;
                }
            }
        });

        // Handle filter lists
        ['filter_in', 'filter_out', 'preferred_filter_in', 'preferred_filter_out'].forEach(filterType => {
            const filterList = section.querySelector(`.filter-list[data-version="${versionId}"][data-filter-type="${filterType}"]`);
            if (filterList) {
                versionData[filterType] = [];
                filterList.querySelectorAll('.filter-item').forEach(item => {
                    const term = item.querySelector('.filter-term')?.value?.trim();
                    if (term) {  // Only add non-empty terms
                        if (filterType.startsWith('preferred_')) {
                            const weight = parseInt(item.querySelector('.filter-weight')?.value) || 1;
                            versionData[filterType].push([term, weight]);
                        } else {
                            versionData[filterType].push(term);
                        }
                    }
                });
            }
        });

        // Add similarity_threshold_anime with default 0.35 if it doesn't exist
        if (!('similarity_threshold_anime' in versionData)) {
            versionData['similarity_threshold_anime'] = 0.35;
        }

        // Add similarity_threshold with default 0.8 if it doesn't exist
        if (!('similarity_threshold' in versionData)) {
            versionData['similarity_threshold'] = 0.8;
        }

        // Add max_size_gb with default Infinity if it doesn't exist
        if (!('max_size_gb' in versionData)) {
            versionData['max_size_gb'] = Infinity;
        }

        // Add display_name separately to ensure it's always included
        const displayNameInput = section.querySelector('input[name$=".display_name"]');
        if (displayNameInput) {
            versionData['display_name'] = displayNameInput.value;
        }

        versions[versionId] = versionData;
    });

    settingsData['Scraping'] = { 
        ...settingsData['Scraping'],
        versions: versions 
    };
    
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
        
        // Handle Hybrid option specially
        if (uncachedHandlingSelect.value === 'Hybrid') {
            console.log("Setting uncached_content_handling to 'None' and hybrid_mode to true");
            settingsData['Scraping']['uncached_content_handling'] = 'None';
            settingsData['Scraping']['hybrid_mode'] = true;
        } else {
            console.log("Setting uncached_content_handling value");
            settingsData['Scraping']['uncached_content_handling'] = uncachedHandlingSelect.value;
            settingsData['Scraping']['hybrid_mode'] = false;
        }
        
        // Always set jackett_seeders_only to true
        console.log("Setting jackett_seeders_only value to true");
        settingsData['Scraping']['jackett_seeders_only'] = true;
        
        // Always set enable_upgrading_cleanup to true
        console.log("Setting enable_upgrading_cleanup value to true");
        settingsData['Scraping']['enable_upgrading_cleanup'] = true;
        
        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Uncached Handling Method select element not found!");
    }

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
        
        console.log("Setting jackett_seeders_only value to true");
        settingsData['Scraping']['jackett_seeders_only'] = true;
        
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
            contentSourceCheckPeriods[sourceName] = parseFloat(value) || 0.1;
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

    // Handle Disable Content Source Caching
    const disableContentSourceCaching = document.getElementById('debug-disable_content_source_caching'); 
    console.log("Disable Content Source Caching element:", disableContentSourceCaching);
    
    if (disableContentSourceCaching) {
        settingsData['Debug']['disable_content_source_caching'] = disableContentSourceCaching.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Disable Content Source Caching checkbox element not found!");
    }

    const enableUpgrading = document.getElementById('scraping-enable_upgrading'); 
    console.log("Enable Upgrading element:", enableUpgrading);
    
    if (enableUpgrading) {
        settingsData['Scraping']['enable_upgrading'] = enableUpgrading.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Enable Upgrading checkbox element not found!");
    }

    const enableUpgradingCleanup = document.getElementById('scraping-enable_upgrading_cleanup');
    console.log("Enable Upgrading Cleanup element:", enableUpgradingCleanup);
    
    if (enableUpgradingCleanup) {
        settingsData['Scraping']['enable_upgrading_cleanup'] = enableUpgradingCleanup.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Enable Upgrading Cleanup checkbox element not found!");
    }


    const stalenessThreshold = document.getElementById('staleness threshold-staleness_threshold');
    console.log("Staleness Threshold element:", stalenessThreshold);
    
    if (stalenessThreshold) {
        // Ensure 'Staleness Threshold' object exists in settingsData
        if (!settingsData['Staleness Threshold']) {
            settingsData['Staleness Threshold'] = {};
        }
        settingsData['Staleness Threshold']['staleness_threshold'] = parseInt(stalenessThreshold.value) || 7;
    
        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Staleness Threshold input element not found!");
    }
    
    // Set default staleness threshold if not set
    if (!settingsData['Staleness Threshold']) {
        settingsData['Staleness Threshold'] = {};
    }
    
    if (settingsData['Staleness Threshold']['staleness_threshold'] === undefined) {
        console.log("Setting default staleness_threshold to 7 days");
        settingsData['Staleness Threshold']['staleness_threshold'] = 7;
    }
        
    console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));

    // Set default sync_deletions if not set
    if (!settingsData['Sync Deletions']) {
        settingsData['Sync Deletions'] = {};
    }
    
    if (settingsData['Sync Deletions']['sync_deletions'] === undefined) {
        console.log("Setting default sync_deletions to true");
        settingsData['Sync Deletions']['sync_deletions'] = true;
    }
        
    console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));

    const enableReverseOrderScraping = document.getElementById('scraping-enable_reverse_order_scraping');
    console.log("Enable Reverse Order Scraping element:", enableReverseOrderScraping);
    
    if (enableReverseOrderScraping) {
        settingsData['Scraping']['enable_reverse_order_scraping'] = enableReverseOrderScraping.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Enable Reverse Order Scraping checkbox element not found!");
    }

    const disableAdult = document.getElementById('scraping-disable_adult');
    console.log("Disable Adult Content element:", disableAdult);
    
    if (disableAdult) {
        settingsData['Scraping']['disable_adult'] = disableAdult.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Disable Adult Content checkbox element not found!");
    }

    const syncDeletions = document.getElementById('sync deletions-sync_deletions');
    console.log("Sync Deletions element:", syncDeletions);
    
    if (syncDeletions) {
        settingsData['Sync Deletions']['sync_deletions'] = syncDeletions.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Sync Deletions checkbox element not found!");
    }

    const traktEarlyReleases = document.getElementById('scraping-trakt_early_releases');
    console.log("Trakt Early Releases element:", traktEarlyReleases);
    
    if (traktEarlyReleases) {
        settingsData['Scraping']['trakt_early_releases'] = traktEarlyReleases.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Trakt Early Releases checkbox element not found!");
    }

    const fileCollectionManagement = document.getElementById('file management-file_collection_management');
    console.log("File Collection Management element:", fileCollectionManagement);
    
    if (fileCollectionManagement) {
        settingsData['File Management']['file_collection_management'] = fileCollectionManagement.value;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("File Collection Management select element not found!");
    }

    const zurgAllFolder = document.getElementById('file management-zurg_all_folder');
    console.log("Zurg All Folder element:", zurgAllFolder);
    
    if (zurgAllFolder) {
        settingsData['File Management']['zurg_all_folder'] = zurgAllFolder.value;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Zurg All Folder input element not found!");
    }

    const zurgMoviesFolder = document.getElementById('file management-zurg_movies_folder');
    console.log("Zurg Movies Folder element:", zurgMoviesFolder);
    
    if (zurgMoviesFolder) {
        settingsData['File Management']['zurg_movies_folder'] = zurgMoviesFolder.value;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Zurg Movies Folder input element not found!");
    }

    const zurgShowsFolder = document.getElementById('file management-zurg_shows_folder');
    console.log("Zurg Shows Folder element:", zurgShowsFolder);
    
    if (zurgShowsFolder) {
        settingsData['File Management']['zurg_shows_folder'] = zurgShowsFolder.value;
    
        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Zurg Shows Folder input element not found!");
    }

    const disableNotWantedCheck = document.getElementById('debug-disable_not_wanted_check');
    console.log("Disable Not Wanted Check element:", disableNotWantedCheck);
    
    if (disableNotWantedCheck) {
        settingsData['Debug']['disable_not_wanted_check'] = disableNotWantedCheck.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Disable Not Wanted Check checkbox element not found!");
    }

    const filenameFilterOutList = document.getElementById('queue-filename_filter_out_list');
    console.log("Filename Filter Out List element:", filenameFilterOutList);
    
    if (filenameFilterOutList) {
        settingsData['Queue']['filename_filter_out_list'] = filenameFilterOutList.value;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Filename Filter Out List input element not found!");
    }

    const traktWatchlistRemoval = document.getElementById('scraping-trakt_watchlist_removal');
    console.log("Trakt Watchlist Removal element:", traktWatchlistRemoval);
    
    if (traktWatchlistRemoval) {
        settingsData['Scraping']['trakt_watchlist_removal'] = traktWatchlistRemoval.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Trakt Watchlist Removal checkbox element not found!");
    }

    const traktWatchlistKeepSeries = document.getElementById('scraping-trakt_watchlist_keep_series');
    console.log("Trakt Watchlist Keep Series element:", traktWatchlistKeepSeries);
    
    if (traktWatchlistKeepSeries) {
        settingsData['Scraping']['trakt_watchlist_keep_series'] = traktWatchlistKeepSeries.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Trakt Watchlist Keep Series checkbox element not found!");
    }

    const blacklistDuration = document.getElementById('queue-blacklist_duration');
    console.log("Blacklist Duration element:", blacklistDuration);
    
    if (blacklistDuration) {
        settingsData['Queue']['blacklist_duration'] = parseInt(blacklistDuration.value) || 30;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Blacklist Duration input element not found!");
    }

    const plexWatchlistRemoval = document.getElementById('scraping-plex_watchlist_removal');
    console.log("Plex Watchlist Removal element:", plexWatchlistRemoval);
    
    if (plexWatchlistRemoval) {
        settingsData['Scraping']['plex_watchlist_removal'] = plexWatchlistRemoval.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Plex Watchlist Removal checkbox element not found!");
    }

    const plexWatchlistKeepSeries = document.getElementById('scraping-plex_watchlist_keep_series');
    console.log("Plex Watchlist Keep Series element:", plexWatchlistKeepSeries);
    
    if (plexWatchlistKeepSeries) {
        settingsData['Scraping']['plex_watchlist_keep_series'] = plexWatchlistKeepSeries.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Plex Watchlist Keep Series checkbox element not found!");
    }

    const allowPartialOverseerrRequests = document.getElementById('scraping-allow_partial_overseerr_requests');
    console.log("Allow Partial Overseerr Requests element:", allowPartialOverseerrRequests);
    
    if (allowPartialOverseerrRequests) {
        settingsData['Scraping']['allow_partial_overseerr_requests'] = allowPartialOverseerrRequests.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Allow Partial Overseerr Requests checkbox element not found!");
    }

    const timezoneOverride = document.getElementById('debug-timezone_override');
    console.log("Timezone Override element:", timezoneOverride);
    
    if (timezoneOverride) {
        settingsData['Debug']['timezone_override'] = timezoneOverride.value;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Timezone Override input element not found!");
    }

    const animeRenamingUsingAnidb = document.getElementById('scraping-anime_renaming_using_anidb');
    console.log("Anime Renaming Using AniDB element:", animeRenamingUsingAnidb);
    
    if (animeRenamingUsingAnidb) {
        settingsData['Scraping']['anime_renaming_using_anidb'] = animeRenamingUsingAnidb.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Anime Renaming Using AniDB checkbox element not found!");
    }

    const debridProvider = document.getElementById('debrid provider-provider');
    console.log("Debrid Provider element:", debridProvider);
    
    if (debridProvider) {
        settingsData['Debrid Provider']['provider'] = debridProvider.value;
    
        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Debrid Provider select element not found!");
    }

    const updatePlexOnFileDiscovery = document.getElementById('plex-update_plex_on_file_discovery');
    console.log("Update Plex on File Discovery element:", updatePlexOnFileDiscovery);
    
    if (updatePlexOnFileDiscovery) {
        settingsData['Plex']['update_plex_on_file_discovery'] = updatePlexOnFileDiscovery.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Update Plex on File Discovery checkbox element not found!");
    }

    const mountedFileLocation = document.getElementById('plex-mounted_file_location');
    console.log("Plex File Location element:", mountedFileLocation);
    
    if (mountedFileLocation) {
        settingsData['Plex']['mounted_file_location'] = mountedFileLocation.value;
    
        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Plex File Location input element not found!");
    }

    const doNotAddPlexWatchHistoryItemsToQueue = document.getElementById('scraping-do_not_add_plex_watch_history_items_to_queue');
    console.log("Do Not Add Plex Watch History Items To Queue element:", doNotAddPlexWatchHistoryItemsToQueue);
    
    if (doNotAddPlexWatchHistoryItemsToQueue) {
        settingsData['Scraping']['do_not_add_plex_watch_history_items_to_queue'] = doNotAddPlexWatchHistoryItemsToQueue.checked;

        console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));
    } else {
        console.warn("Do Not Add Plex Watch History Items To Queue checkbox element not found!");
    }

    console.log("Final settings data to be sent:", JSON.stringify(settingsData, null, 2));

    // Set default values for enable_upgrading, disable_adult, and trakt_early_releases
    if (settingsData['Scraping']['enable_upgrading'] === undefined) {
        console.log("Setting default enable_upgrading to false");
        settingsData['Scraping']['enable_upgrading'] = false;
    }
        
    if (settingsData['Scraping']['disable_adult'] === undefined) {
        console.log("Setting default disable_adult to true");
        settingsData['Scraping']['disable_adult'] = true;
    }
        
    if (settingsData['Scraping']['trakt_early_releases'] === undefined) {
        console.log("Setting default trakt_early_releases to false");
        settingsData['Scraping']['trakt_early_releases'] = false;
    }
        
    console.log("Updated settingsData:", JSON.stringify(settingsData, null, 2));

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

    const defaultIntervals = {
        'Overseerr': 900,
        'MDBList': 900,
        'Collected': 86400,
        'Trakt Watchlist': 900,
        'Trakt Lists': 900,
        'Trakt Collection': 900,
        'My Plex Watchlist': 900,
        'Other Plex Watchlist': 900,
        'My Plex RSS Watchlist': 900,
        'My Friends Plex RSS Watchlist': 900
    };

    const enabledContentSources = Object.keys(settingsData['Content Sources'] || {}).filter(source => settingsData['Content Sources'][source].enabled);
    
    contentSourcesDiv.innerHTML = '';
    enabledContentSources.forEach(source => {
        const sourceType = source.split('_')[0];
        const div = document.createElement('div');
        div.className = 'content-source-check-period';
        const defaultInterval = defaultIntervals[sourceType] ? Math.floor(defaultIntervals[sourceType] / 60) : '';  // Convert seconds to minutes
        div.innerHTML = `
            <label for="debug-content-source-${source}">${source}:</label>
            <input type="number" id="debug-content-source-${source}" name="Debug.content_source_check_period.${source}" 
                   value="${(settingsData['Debug'] && settingsData['Debug']['content_source_check_period'] && settingsData['Debug']['content_source_check_period'][source]) || ''}" 
                   step="0.1" min="0.1" class="settings-input" placeholder="${defaultInterval}">
        `;
        contentSourcesDiv.appendChild(div);
    });
}

// Function to initialize settings tabs
function initializeSettingsTabs() {
    const tabButtons = document.querySelectorAll('.settings-tab-button');
    const tabContents = document.querySelectorAll('.settings-tab-content');
    const tabSelect = document.querySelector('.settings-tab-select');
    
    if (!tabButtons.length || !tabContents.length || !tabSelect) {
        console.warn('Settings tabs elements not found');
        return;
    }
    
    // Tab switching is handled in settings_base.html
    console.log('Settings tabs initialized');
}

// Update the DOMContentLoaded event listener
document.addEventListener('DOMContentLoaded', function() {
    // Initialize settings tabs if needed
    if (typeof initializeSettingsTabs === 'function') {
        initializeSettingsTabs();
    }
    
    loadSettingsData().then(() => {
        // Initial toggle of Plex section
        togglePlexSection();
        
        // Add event listener for collection management type changes
        const collectionManagementSelect = document.getElementById('file-management-collection_management_type');
        if (collectionManagementSelect) {
            collectionManagementSelect.addEventListener('change', function() {
                togglePlexSection();
            });
        }
        
        // Add event listener for the "Save Settings" button
        const saveSettingsButton = document.getElementById('save-settings-button');
        if (saveSettingsButton) {
            saveSettingsButton.addEventListener('click', function() {
                saveSettings();
            });
        }
        
        // Add event listener for the "Reset Settings" button
        const resetSettingsButton = document.getElementById('reset-settings-button');
        if (resetSettingsButton) {
            resetSettingsButton.addEventListener('click', function() {
                resetSettings();
            });
        }
        
        // Add event listeners for content source check periods
        const contentSourceCheckPeriods = document.getElementById('content-source-check-periods');
        if (contentSourceCheckPeriods) {
            const checkPeriodInputs = contentSourceCheckPeriods.querySelectorAll('input[type="number"]');
            checkPeriodInputs.forEach(function(input) {
                input.addEventListener('change', function() {
                    updateContentSourceCheckPeriod(this);
                });
            });
        } else {
            console.warn("Element with id 'content-source-check-periods' not found. Make sure it exists in your HTML.");
        }
        
        // Hide hybrid mode and jackett seeders only checkboxes
        hideHybridModeCheckboxes();
        
        // Also call it when the scraping tab is shown
        document.addEventListener('scrapingContentLoaded', hideHybridModeCheckboxes);
    });
});

// Define the hideHybridModeCheckboxes function outside the DOMContentLoaded event
function hideHybridModeCheckboxes() {
    // Hide hybrid_mode checkbox completely
    const hybridModeCheckbox = document.getElementById('scraping-hybrid_mode');
    if (hybridModeCheckbox) {
        const hybridModeFormGroup = hybridModeCheckbox.closest('.settings-form-group');
        if (hybridModeFormGroup) {
            hybridModeFormGroup.classList.add('hybrid-mode-group');
        }
    }
    
    // Hide jackett_seeders_only checkbox completely
    const jackettSeedersOnlyCheckbox = document.getElementById('scraping-jackett_seeders_only');
    if (jackettSeedersOnlyCheckbox) {
        const jackettSeedersOnlyFormGroup = jackettSeedersOnlyCheckbox.closest('.settings-form-group');
        if (jackettSeedersOnlyFormGroup) {
            jackettSeedersOnlyFormGroup.classList.add('jackett-seeders-only-group');
        }
    }
    
    // Also hide any version-specific hybrid_mode and jackett_seeders_only checkboxes
    document.querySelectorAll('input[data-hybrid-mode="true"], input[data-jackett-seeders-only="true"]').forEach(function(checkbox) {
        const formGroup = checkbox.closest('.settings-form-group');
        if (formGroup) {
            if (checkbox.hasAttribute('data-hybrid-mode')) {
                formGroup.classList.add('hybrid-mode-group');
            }
            if (checkbox.hasAttribute('data-jackett-seeders-only')) {
                formGroup.classList.add('jackett-seeders-only-group');
            }
        }
    });
}

// Add event listener for the scraping tab content loaded event
document.addEventListener('scrapingContentLoaded', hideHybridModeCheckboxes);

// Function to handle debug settings synchronization
function syncDebugSettings() {
    // Get the debug settings from the true_debug tab
    const ultimateSortOrderSelect = document.getElementById('debug-ultimate_sort_order');
    const softMaxSizeGbCheckbox = document.getElementById('debug-soft_max_size_gb');
    
    if (ultimateSortOrderSelect && softMaxSizeGbCheckbox) {
        // Add change event listeners to sync with the original settings
        ultimateSortOrderSelect.addEventListener('change', function() {
            // Find the original setting in the scraping tab (if it exists)
            const originalSelect = document.getElementById('scraping-ultimate_sort_order');
            if (originalSelect) {
                originalSelect.value = ultimateSortOrderSelect.value;
            }
        });
        
        softMaxSizeGbCheckbox.addEventListener('change', function() {
            // Find the original setting in the scraping tab (if it exists)
            const originalCheckbox = document.getElementById('scraping-soft_max_size_gb');
            if (originalCheckbox) {
                originalCheckbox.checked = softMaxSizeGbCheckbox.checked;
            }
        });
    }
}

// Add event listener for the true_debug tab content loaded event
document.addEventListener('trueDebugContentLoaded', syncDebugSettings);
document.addEventListener('DOMContentLoaded', syncDebugSettings);
