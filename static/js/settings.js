// Settings.js - Debug logging cleaned up (v2.0 - 2026-01-13)
import { showPopup, POPUP_TYPES } from './notifications.js';

// Declare settingsData globally
let settingsData = {};

// Helper function for splitting strings while respecting parentheses
export function splitRespectingParentheses(str, delimiter = ',') {
    const result = [];
    let currentToken = "";
    let parenthesesCount = 0;

    for (let i = 0; i < str.length; i++) {
        const char = str[i];
        if (char === '(') {
            parenthesesCount++;
        } else if (char === ')') {
            parenthesesCount--;
            if (parenthesesCount < 0) {
                // Gracefully handle mismatched parentheses, though ideally input is well-formed
                parenthesesCount = 0; 
            }
        }

        if (char === delimiter && parenthesesCount === 0) {
            const trimmedToken = currentToken.trim();
            if (trimmedToken !== "") {
                result.push(trimmedToken);
            }
            currentToken = "";
        } else {
            currentToken += char;
        }
    }
    
    const trimmedToken = currentToken.trim();
    if (trimmedToken !== "") {
        result.push(trimmedToken);
    }
    
    return result;
}

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

    const fileManagementSelect = document.getElementById('file management-file_collection_management');
    const plexSettingsInFileManagement = document.getElementById('plex-settings-in-file-management');


    if (fileManagementSelect && plexSettingsInFileManagement) {
        const shouldDisplay = fileManagementSelect.value === 'Plex';
        plexSettingsInFileManagement.style.display = shouldDisplay ? 'block' : 'none';
    }
    // Removed warning - this function may be called from pages that don't have these elements

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

        // Handle Plex symlink settings - only show if media server type is plex
        if (isSymlinked) {
            const mediaServerType = document.getElementById('media-server-type');
            const shouldShowPlexFields = mediaServerType && mediaServerType.value === 'plex';

            plexSymlinkSettings.forEach(element => {
                element.style.display = shouldShowPlexFields ? 'block' : 'none';
            });
        } else {
            // Hide Plex symlink settings when not in Symlinked/Local mode
            plexSymlinkSettings.forEach(element => {
                element.style.display = 'none';
            });
        }
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

    // First handle all regular inputs (excluding Content Sources which are handled separately)
    const inputs = document.querySelectorAll('#settingsForm input, #settingsForm select, #settingsForm textarea');
    
    inputs.forEach(input => {
        const name = input.name;
        if (!name) return; // Skip inputs without names

        // Skip Content Sources inputs - they are handled separately
        if (name.startsWith('Content Sources.')) return;

        // Skip radio buttons that are not checked
        if (input.type === 'radio' && !input.checked) return;

        // Skip hidden inputs and inputs inside hidden containers
        // This prevents hidden duplicate fields from overwriting visible ones
        // EXCEPTION: Include inputs with data-section attribute (legitimate settings in collapsed sections),
        // BUT exclude inputs inside mode-switching panels that are currently display:none — these panels
        // contain duplicate fields that would overwrite the visible counterparts (e.g. plex library fields
        // duplicated across #plex-settings-in-file-management and .symlink-plex-setting).
        if (input.offsetParent === null && input.type !== 'hidden') {
            if (!input.hasAttribute('data-section')) return;
            // Check if a nearest ancestor panel (not a collapsible settings-section-content) is display:none.
            // Mode-switching panels are direct children of the form or have specific ids/classes.
            const hiddenPanel = input.closest('#plex-settings-in-file-management, #jellyfin-settings-in-file-management, .symlink-plex-setting');
            if (hiddenPanel && hiddenPanel.style.display === 'none') return;
        }
        
        let value = input.value;
        
        if (input.type === 'checkbox') {
            value = input.checked;
        } else if (input.type === 'number') {
            value = parseFloat(value) || 0;
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

    // Ensure Staleness Threshold section exists and has a value
    if (!settingsData['Staleness Threshold']) {
        settingsData['Staleness Threshold'] = {};
    }
    const stalenessThresholdInput = document.getElementById('debug-staleness_threshold');
    if (stalenessThresholdInput) {
        const value = parseInt(stalenessThresholdInput.value) || 7;
        settingsData['Staleness Threshold']['staleness_threshold'] = value;
    } else {
        settingsData['Staleness Threshold']['staleness_threshold'] = 7; // Default value
    }

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

    // Create Bazarr Integration section if it doesn't exist
    if (!settingsData['Bazarr Integration']) {
        settingsData['Bazarr Integration'] = {};
    }

    // Handle Bazarr Integration settings
    const bazarrIntegrationInputs = document.querySelectorAll('[id^="bazarr-"][name^="Bazarr Integration."]');
    bazarrIntegrationInputs.forEach(input => {
        const key = input.name.split('.')[1];
        settingsData['Bazarr Integration'][key] = input.type === 'checkbox' ? input.checked : input.value;
    });

    // Create AI Assistant section if it doesn't exist
    if (!settingsData['AI Assistant']) {
        settingsData['AI Assistant'] = {};
    }

    // Handle AI Assistant settings
    const aiAssistantInputs = document.querySelectorAll('[name^="AI Assistant."]');
    aiAssistantInputs.forEach(input => {
        const parts = input.name.split('.');
        // parts[0] = 'AI Assistant', parts[1] = key or sub-section, parts[2] = sub-key (optional)
        const val = input.type === 'checkbox' ? input.checked : input.value;
        if (parts.length === 3) {
            // nested: e.g. AI Assistant.plex_labels.enabled
            if (!settingsData['AI Assistant'][parts[1]] || typeof settingsData['AI Assistant'][parts[1]] !== 'object') {
                settingsData['AI Assistant'][parts[1]] = {};
            }
            settingsData['AI Assistant'][parts[1]][parts[2]] = val;
        } else {
            settingsData['AI Assistant'][parts[1]] = val;
        }
    });


    // Handle Plex Smart Collections settings
    if (!settingsData['Plex Smart Collections']) {
        settingsData['Plex Smart Collections'] = {};
    }
    const plexSmartInputs = document.querySelectorAll('[name^="Plex Smart Collections."]');
    plexSmartInputs.forEach(input => {
        const parts = input.name.split('.');
        const val = input.type === 'checkbox' ? input.checked :
                    (input.type === 'range' || input.type === 'number') ? parseFloat(input.value) || 0 :
                    input.value;
        if (parts.length >= 2) {
            settingsData['Plex Smart Collections'][parts[1]] = val;
        }
    });

    // Handle Plex Movie Box Sets settings
    if (!settingsData['Plex Movie Box Sets']) {
        settingsData['Plex Movie Box Sets'] = {};
    }
    const plexBoxSetsInputs = document.querySelectorAll('[name^="Plex Movie Box Sets."]');
    plexBoxSetsInputs.forEach(input => {
        const parts = input.name.split('.');
        const key = parts[1];
        const val = input.type === 'checkbox' ? input.checked :
                    (input.type === 'range' || input.type === 'number') ? parseFloat(input.value) || 0 :
                    key === 'min_movies' ? parseInt(input.value, 10) || 2 :
                    input.value;
        if (parts.length >= 2) {
            settingsData['Plex Movie Box Sets'][key] = val;
        }
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
    
    // Ensure program_logo is correctly captured from radio buttons
    const selectedLogoRadio = document.querySelector('input[name="UI Settings.program_logo"]:checked');
    if (selectedLogoRadio) {
        settingsData['UI Settings']['program_logo'] = selectedLogoRadio.value;
    } else {
        // Fallback to default if no radio is checked
        settingsData['UI Settings']['program_logo'] = 'Default';
    }

    // Process Notification settings
    const notificationsTab = document.getElementById('notifications');
    if (notificationsTab) {
        const notificationSections = notificationsTab.querySelectorAll('.settings-section');

        if (!settingsData['Notifications']) {
            settingsData['Notifications'] = {};
        }

        notificationSections.forEach(section => {
            const notificationId = section.getAttribute('data-notification-id');
            if (!notificationId) {
                return;
            }

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

            settingsData['Notifications'][notificationId] = notificationData;
        });
    } else {
    }

    // Ensure 'Content Sources' section exists
    if (!settingsData['Content Sources']) {
        settingsData['Content Sources'] = {};
    }

    // Process Content Sources
    const contentSourcesTab = document.getElementById('content-sources');
    if (contentSourcesTab) {
        const contentSourceSections = contentSourcesTab.querySelectorAll('.settings-section');
        
        contentSourceSections.forEach(section => {
            const sourceId = section.getAttribute('data-source-id');
            if (!sourceId) {
                return;
            }

            const sourceData = {};
            sourceData.versions = [];

            // scrob-list-multiselect is a picker UI only (no name attribute) — its
            // selection is mirrored into a real hidden input by initializeScrobListPickers().
            // Excluded here so it isn't collected as a nameless '' field.
            section.querySelectorAll('input, select, textarea').forEach(input => {
                if (input.classList.contains('scrob-list-multiselect')) {
                    return;
                }

                // Skip radio buttons that are not checked - otherwise the last radio in a
                // group would always win regardless of the user's selection.
                if (input.type === 'radio' && !input.checked) return;

                const nameParts = input.name.split('.');
                // nameParts = ['Content Sources', 'Trakt Lists_1', 'plex_labels', 'enabled']
                // We need everything after the source ID (index 2+)
                const fieldPath = nameParts.slice(2); // ['plex_labels', 'enabled']
                const fieldName = nameParts[nameParts.length - 1]; // 'enabled'

                // Handle nested fields like plex_labels.enabled
                if (fieldPath.length > 1) {
                    // This is a nested field (e.g., plex_labels.enabled)
                    let current = sourceData;
                    for (let i = 0; i < fieldPath.length - 1; i++) {
                        if (!current[fieldPath[i]]) {
                            current[fieldPath[i]] = {};
                        }
                        current = current[fieldPath[i]];
                    }

                    // Set the final value
                    const finalKey = fieldPath[fieldPath.length - 1];
                    if (input.type === 'checkbox') {
                        current[finalKey] = input.checked;
                    } else if (finalKey === 'poster_design') {
                        current[finalKey] = parseInt(input.value) || 0;
                    } else if (finalKey === 'libraries') {
                        try { current[finalKey] = JSON.parse(input.value) || []; } catch(e) { current[finalKey] = []; }
                    } else {
                        current[finalKey] = input.value;
                    }
                } else if (input.type === 'checkbox') {
                    // Top-level checkbox field
                    if (fieldName === 'versions') {
                        if (input.checked) {
                            sourceData.versions.push(input.value);
                        }
                    } else {
                        sourceData[fieldName] = input.checked;
                    }
                } else if (input.type === 'select-multiple') {
                    sourceData[fieldName] = Array.from(input.selectedOptions).map(option => option.value);
                } else if (fieldName === 'exclude_genres') {
                    // Handle exclude_genres as a list - split by comma and trim whitespace
                    const value = input.value.trim();
                    if (value) {
                        const genres = value.split(',').map(genre => genre.trim()).filter(genre => genre);
                        sourceData[fieldName] = genres;
                    } else {
                        sourceData[fieldName] = [];
                    }
                } else {
                    sourceData[fieldName] = input.value;
                }
            });

            // If no versions were checked, ensure versions is an empty array
            if (!sourceData.versions || sourceData.versions.length === 0) {
                sourceData.versions = [];
            }

            // Preserve the existing type if it's not in the form
            if (!sourceData.type) {
                const existingSource = window.contentSourceSettings[sourceId.split('_')[0]];
                if (existingSource && existingSource.type) {
                    sourceData.type = existingSource.type;
                }
            }

            // Preserve the 'lists' field for Adaptive List sources (managed via API, not form inputs)
            // The lists are managed through the Discover page API, not through form fields
            if (sourceData.type === 'Adaptive List') {
                // Try to get lists from the existing loaded settings
                const existingSource = window.contentSourceSettings ? window.contentSourceSettings[sourceId] : null;
                if (existingSource && existingSource.lists) {
                    sourceData.lists = existingSource.lists;
                } else {
                    // Initialize with empty array if not found
                    sourceData.lists = [];
                }
            }

            settingsData['Content Sources'][sourceId] = sourceData;
        });
    }

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
    allTabs.forEach((tab, index) => {
    });

    // Process scraper sections
    const scrapersTab = document.getElementById('scrapers');

    if (scrapersTab) {
        const scraperSections = scrapersTab.querySelectorAll('.settings-section');

        if (!settingsData['Scrapers']) {
            settingsData['Scrapers'] = {};
        }

        scraperSections.forEach(section => {
            const header = section.querySelector('.settings-section-header h4');
            if (!header) {
                return;
            }

            const scraperId = header.textContent.trim();

            // Get type from the hidden type input if present (handles custom-named scrapers like Newznab)
            const typeInput = section.querySelector('input[name$=".type"]');
            const scraperType = typeInput ? typeInput.value : scraperId.split('_')[0];

            const scraperData = {
                type: scraperType
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

            settingsData['Scrapers'][scraperId] = scraperData;
        });
    } else {
        console.error('Scrapers tab not found!');
    }

    // Remove any scrapers that are not actual scrapers
    const validScraperTypes = ['Zilean', 'MediaFusion', 'AIOStreams', 'AIOStreams-API', 'Jackett', 'Torrentio', 'Nyaa', 'Prowlarr', 'OldNyaa', 'Newznab'];
    if (settingsData['Scrapers'] && typeof settingsData['Scrapers'] === 'object') {
        Object.keys(settingsData['Scrapers']).forEach(key => {
            if (settingsData['Scrapers'][key] && settingsData['Scrapers'][key].type && !validScraperTypes.includes(settingsData['Scrapers'][key].type)) {
                delete settingsData['Scrapers'][key];
            }
        });
    }
    
    // Update the list of top-level fields to include UI Settings
    const topLevelFields = ['Plex', 'Overseerr', 'RealDebrid', 'Debrid Provider', 'Usenet Provider', 'Torrentio', 'Scraping', 'Queue', 'Trakt', 'Scrob', 'Debug', 'Content Sources', 'Scrapers', 'Notifications', 'TMDB', 'TVDB', 'UI Settings', 'Sync Deletions', 'File Management', 'Subtitle Settings', 'Bazarr Integration', 'Overlay Settings', 'Staleness Threshold', 'Custom Post-Processing', 'System Load Regulation', 'Library Manager', 'MDBList', 'Discover Settings', 'AI Assistant', 'Plex Smart Collections', 'Plex Movie Box Sets'];
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

    // Validate symlink templates
    const movieTemplateInput = document.getElementById('additional-symlink_movie_template');
    const episodeTemplateInput = document.getElementById('additional-symlink_episode_template');

    if (movieTemplateInput && episodeTemplateInput) {
        const movieTemplate = movieTemplateInput.value;
        const episodeTemplate = episodeTemplateInput.value;

        if (movieTemplate.trim() !== '' && !movieTemplate.includes('{imdb_id}')) {
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Validation Error',
                message: 'Symlink Movie Template must include {imdb_id}. Settings not saved.'
            });
            return; // Stop processing and do not save
        }

        if (episodeTemplate.trim() !== '' && !episodeTemplate.includes('{imdb_id}')) {
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Validation Error',
                message: 'Symlink Episode Template must include {imdb_id}. Settings not saved.'
            });
            return; // Stop processing and do not save
        }
        
        // Ensure Debug section exists if not already created by other inputs
        if (!settingsData['Debug']) {
            settingsData['Debug'] = {};
        }
        settingsData['Debug']['symlink_movie_template'] = movieTemplate;
        settingsData['Debug']['symlink_episode_template'] = episodeTemplate;

    } else {
        console.warn('Symlink template input fields not found. Skipping validation.');
        // Decide if you want to proceed or error out if fields are missing
        // For now, we'll proceed but log a warning.
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
                            const source = item.querySelector('.filter-source')?.value || 'both';
                            versionData[filterType].push({pattern: term, source: source});
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

    // Add TMDB certification region
    const tmdbCertificationRegionSelect = document.getElementById('tmdb-certification-region');
    if (tmdbCertificationRegionSelect) {
        settingsData['TMDB']['certification_region'] = tmdbCertificationRegionSelect.value;
    }

    // Add this block to handle the Uncached Handling Method
    const uncachedHandlingSelect = document.getElementById('scraping-uncached_content_handling');
    
    if (uncachedHandlingSelect) {
        
        if (!settingsData['Scraping']) {
            settingsData['Scraping'] = {};
        }
        
        // Handle Hybrid option specially
        if (uncachedHandlingSelect.value === 'Hybrid') {
            settingsData['Scraping']['uncached_content_handling'] = 'None';
            settingsData['Scraping']['hybrid_mode'] = true;
        } else {
            settingsData['Scraping']['uncached_content_handling'] = uncachedHandlingSelect.value;
            settingsData['Scraping']['hybrid_mode'] = false;
        }
        
        // Always set jackett_seeders_only to true
        settingsData['Scraping']['jackett_seeders_only'] = true;
        
        // Always set enable_upgrading_cleanup to true
        settingsData['Scraping']['enable_upgrading_cleanup'] = true;
        
    } else {
    }

    const jackettSeedersOnly = document.getElementById('scraping-jackett_seeders_only');

    const softMaxSize = document.getElementById('scraping-soft_max_size_gb');
    
    if (jackettSeedersOnly) {
        
        if (!settingsData['Scraping']) {
            settingsData['Scraping'] = {};
        }
        
        settingsData['Scraping']['jackett_seeders_only'] = true;
        
    } else {
    }

    if (softMaxSize) {

        settingsData['Scraping']['soft_max_size_gb'] = softMaxSize.checked;

    } else {
    }

    const ultimateSortOrder = document.getElementById('scraping-ultimate_sort_order');
    
    if (ultimateSortOrder) {
        settingsData['Scraping']['ultimate_sort_order'] = ultimateSortOrder.value;

    } else {
    }

    const metadataBatteryUrl = document.getElementById('metadata battery-url');
    
    if (metadataBatteryUrl) {
        // Ensure 'Metadata Battery' object exists in settingsData
        if (!settingsData['Metadata Battery']) {
            settingsData['Metadata Battery'] = {};
        }
        settingsData['Metadata Battery']['url'] = metadataBatteryUrl.value;
    
    } else {
    }

    const sortByUncachedStatus = document.getElementById('debug-sort_by_uncached_status');
    
    if (sortByUncachedStatus) {
        settingsData['Debug']['sort_by_uncached_status'] = sortByUncachedStatus.checked;

    } else {
    }

    const checkingQueuePeriod = document.getElementById('debug-checking_queue_period');
    
    if (checkingQueuePeriod) {
        settingsData['Debug']['checking_queue_period'] = parseInt(checkingQueuePeriod.value) || 3600;

    } else {
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
        default_version: '', // Initialize with a default value
        version_order: [] 
    };

    const defaultVersionInputElement = document.getElementById('default-version');
    if (defaultVersionInputElement) {
        reverseParserSettings.default_version = defaultVersionInputElement.value;
    } else {
        console.warn("Element with ID 'default-version' not found. Reverse Parser default_version will use an empty or previously loaded value if available.");
        // Attempt to preserve existing value if settingsData['Reverse Parser'] was pre-loaded and not cleared
        // However, settingsData is cleared at the beginning of this function.
        // A default or handling on the backend for missing value might be necessary.
    }

    // Get the container of all version inputs
    const versionContainer = document.querySelector('#version-terms-container');
    
    if (versionContainer) {
        const versionInputs = Array.from(versionContainer.children);
    
        versionInputs.forEach((inputElement) => { // index removed as it's not used
            const version = inputElement.getAttribute('data-version');
            const termsInputElement = inputElement.querySelector('.version-terms');
            if (version && termsInputElement) {
                const termsValue = termsInputElement.value;
                const terms = splitRespectingParentheses(termsValue);
                reverseParserSettings.version_terms[version] = terms;
                reverseParserSettings.version_order.push(version); // Add version to order array
            } else {
                console.warn("Skipping a version input in Reverse Parser settings due to missing 'data-version' attribute or '.version-terms' child element.");
            }
        });
    }
    // Note: If version-terms-container not found, Reverse Parser will use pre-loaded data if available

    settingsData['Reverse Parser'] = reverseParserSettings;


    const rescrapeMissingFiles = document.getElementById('debug-rescrape_missing_files');
    
    if (rescrapeMissingFiles) {
        settingsData['Debug']['rescrape_missing_files'] = rescrapeMissingFiles.checked;

    } else {
    }

    // Handle Disable Content Source Caching
    const disableContentSourceCaching = document.getElementById('debug-disable_content_source_caching');

    if (disableContentSourceCaching) {
        settingsData['Debug']['disable_content_source_caching'] = disableContentSourceCaching.checked;

    } else {
    }

    const emphasizeNumberOfItemsOverQuality = document.getElementById('debug-emphasize_number_of_items_over_quality');

    if (emphasizeNumberOfItemsOverQuality) {
        settingsData['Debug']['emphasize_number_of_items_over_quality'] = emphasizeNumberOfItemsOverQuality.checked;
    }

    const enableUpgrading = document.getElementById('scraping-enable_upgrading');
    
    if (enableUpgrading) {
        settingsData['Scraping']['enable_upgrading'] = enableUpgrading.checked;

    } else {
    }

    const enableUpgradingCleanup = document.getElementById('scraping-enable_upgrading_cleanup');
    
    if (enableUpgradingCleanup) {
        settingsData['Scraping']['enable_upgrading_cleanup'] = enableUpgradingCleanup.checked;

    } else {
    }


    const stalenessThreshold = document.getElementById('debug-staleness_threshold');
    
    if (stalenessThreshold) {
        // Ensure 'Staleness Threshold' object exists in settingsData
        if (!settingsData['Staleness Threshold']) {
            settingsData['Staleness Threshold'] = {};
        }
        settingsData['Staleness Threshold']['staleness_threshold'] = parseInt(stalenessThreshold.value) || 7;
    
    } else {
    }
    
    // Set default staleness threshold if not set
    if (!settingsData['Staleness Threshold']) {
        settingsData['Staleness Threshold'] = {};
    }
    
    if (settingsData['Staleness Threshold']['staleness_threshold'] === undefined) {
        settingsData['Staleness Threshold']['staleness_threshold'] = 7;
    }
        

    // Set default sync_deletions if not set
    if (!settingsData['Sync Deletions']) {
        settingsData['Sync Deletions'] = {};
    }
    
    if (settingsData['Sync Deletions']['sync_deletions'] === undefined) {
        settingsData['Sync Deletions']['sync_deletions'] = true;
    }
        

    const enableReverseOrderScraping = document.getElementById('scraping-enable_reverse_order_scraping');
    
    if (enableReverseOrderScraping) {
        settingsData['Scraping']['enable_reverse_order_scraping'] = enableReverseOrderScraping.checked;

    } else {
    }

    const disableAdult = document.getElementById('scraping-disable_adult');

    if (disableAdult) {
        settingsData['Scraping']['disable_adult'] = disableAdult.checked;

    } else {
    }

    const enableSeadexPriority = document.getElementById('scraping-enable_seadex_priority');

    if (enableSeadexPriority) {
        settingsData['Scraping']['enable_seadex_priority'] = enableSeadexPriority.checked;

    } else {
    }

    const syncDeletions = document.getElementById('sync deletions-sync_deletions');
    
    if (syncDeletions) {
        settingsData['Sync Deletions']['sync_deletions'] = syncDeletions.checked;

    } else {
    }

    const traktEarlyReleases = document.getElementById('scraping-trakt_early_releases');
    
    if (traktEarlyReleases) {
        settingsData['Scraping']['trakt_early_releases'] = traktEarlyReleases.checked;

    } else {
    }

    const fileCollectionManagement = document.getElementById('file management-file_collection_management');
    
    if (fileCollectionManagement) {
        settingsData['File Management']['file_collection_management'] = fileCollectionManagement.value;

    } else {
    }

    const zurgAllFolder = document.getElementById('file management-zurg_all_folder');
    
    if (zurgAllFolder) {
        settingsData['File Management']['zurg_all_folder'] = zurgAllFolder.value;

    } else {
    }

    const zurgMoviesFolder = document.getElementById('file management-zurg_movies_folder');
    
    if (zurgMoviesFolder) {
        settingsData['File Management']['zurg_movies_folder'] = zurgMoviesFolder.value;

    } else {
    }

    const zurgShowsFolder = document.getElementById('file management-zurg_shows_folder');
    
    if (zurgShowsFolder) {
        settingsData['File Management']['zurg_shows_folder'] = zurgShowsFolder.value;
    
    } else {
    }

    const disableNotWantedCheck = document.getElementById('debug-disable_not_wanted_check');
    
    if (disableNotWantedCheck) {
        settingsData['Debug']['disable_not_wanted_check'] = disableNotWantedCheck.checked;

    } else {
    }

    const filenameFilterOutList = document.getElementById('queue-filename_filter_out_list');
    
    if (filenameFilterOutList) {
        settingsData['Queue']['filename_filter_out_list'] = filenameFilterOutList.value;

    } else {
    }

    const traktWatchlistRemoval = document.getElementById('scraping-trakt_watchlist_removal');
    
    if (traktWatchlistRemoval) {
        settingsData['Scraping']['trakt_watchlist_removal'] = traktWatchlistRemoval.checked;

    } else {
    }

    const traktWatchlistKeepSeries = document.getElementById('scraping-trakt_watchlist_keep_series');
    
    if (traktWatchlistKeepSeries) {
        settingsData['Scraping']['trakt_watchlist_keep_series'] = traktWatchlistKeepSeries.checked;

    } else {
    }

    const blacklistDuration = document.getElementById('queue-blacklist_duration');
    
    if (blacklistDuration) {
        settingsData['Queue']['blacklist_duration'] = parseInt(blacklistDuration.value) || 30;

    } else {
    }

    const plexWatchlistRemoval = document.getElementById('scraping-plex_watchlist_removal');
    
    if (plexWatchlistRemoval) {
        settingsData['Scraping']['plex_watchlist_removal'] = plexWatchlistRemoval.checked;

    } else {
    }

    const plexWatchlistKeepSeries = document.getElementById('scraping-plex_watchlist_keep_series');
    
    if (plexWatchlistKeepSeries) {
        settingsData['Scraping']['plex_watchlist_keep_series'] = plexWatchlistKeepSeries.checked;

    } else {
    }

    const allowPartialOverseerrRequests = document.getElementById('scraping-allow_partial_overseerr_requests');
    
    if (allowPartialOverseerrRequests) {
        settingsData['Scraping']['allow_partial_overseerr_requests'] = allowPartialOverseerrRequests.checked;

    } else {
    }

    const timezoneOverride = document.getElementById('debug-timezone_override');
    
    if (timezoneOverride) {
        settingsData['Debug']['timezone_override'] = timezoneOverride.value;

    } else {
    }

    // Collect NAS paths as array
    const nasPathInputs = document.querySelectorAll('.nas-path-input');
    if (nasPathInputs.length > 0) {
        if (!settingsData['Debug']) settingsData['Debug'] = {};
        settingsData['Debug']['nas_paths'] = Array.from(nasPathInputs)
            .map(inp => inp.value.trim())
            .filter(v => v);
    }

    const animeRenamingUsingAnidb = document.getElementById('scraping-anime_renaming_using_anidb');
    
    if (animeRenamingUsingAnidb) {
        settingsData['Scraping']['anime_renaming_using_anidb'] = animeRenamingUsingAnidb.checked;

    } else {
    }

    const debridProvider = document.getElementById('debrid provider-provider');

    if (debridProvider) {
        // Ensure Debrid Provider section exists
        if (!settingsData['Debrid Provider']) {
            settingsData['Debrid Provider'] = {};
        }
        settingsData['Debrid Provider']['provider'] = debridProvider.value;

    } else {
    }

    // Usenet Provider settings
    const usenetEnabled = document.getElementById('usenet_provider-enabled');
    const usenetUrl = document.getElementById('usenet_provider-url');
    const usenetToken = document.getElementById('usenet_provider-api_token');
    const usenetFolder = document.getElementById('usenet_provider-download_folder');
    const usenetDataPath = document.getElementById('usenet_provider-data_path');
    const usenetNzbNaming = document.getElementById('usenet_provider-enable_nzb_naming');
    const usenetRetentionDays = document.getElementById('usenet_provider-retention_days');
    const usenetDisableSeasonPacks = document.getElementById('usenet_provider-disable_nzb_season_packs');
    if (usenetEnabled || usenetUrl || usenetToken || usenetFolder || usenetDataPath || usenetNzbNaming || usenetRetentionDays || usenetDisableSeasonPacks) {
        if (!settingsData['Usenet Provider']) settingsData['Usenet Provider'] = {};
        if (usenetEnabled) settingsData['Usenet Provider']['enabled'] = usenetEnabled.checked;
        if (usenetUrl) settingsData['Usenet Provider']['url'] = usenetUrl.value;
        if (usenetToken) settingsData['Usenet Provider']['api_token'] = usenetToken.value;
        if (usenetFolder) settingsData['Usenet Provider']['download_folder'] = usenetFolder.value;
        if (usenetDataPath) settingsData['Usenet Provider']['data_path'] = usenetDataPath.value;
        if (usenetNzbNaming) settingsData['Usenet Provider']['enable_nzb_naming'] = usenetNzbNaming.checked;
        if (usenetRetentionDays) settingsData['Usenet Provider']['retention_days'] = parseInt(usenetRetentionDays.value) || 1500;
        if (usenetDisableSeasonPacks) settingsData['Usenet Provider']['disable_nzb_season_packs'] = usenetDisableSeasonPacks.checked;
    }

    const updatePlexOnFileDiscovery = document.getElementById('plex-update_plex_on_file_discovery');

    if (updatePlexOnFileDiscovery) {
        // Ensure Plex section exists
        if (!settingsData['Plex']) {
            settingsData['Plex'] = {};
        }
        settingsData['Plex']['update_plex_on_file_discovery'] = updatePlexOnFileDiscovery.checked;

    } else {
    }

    const mountedFileLocation = document.getElementById('plex-mounted_file_location');

    if (mountedFileLocation) {
        // Ensure Plex section exists
        if (!settingsData['Plex']) {
            settingsData['Plex'] = {};
        }
        settingsData['Plex']['mounted_file_location'] = mountedFileLocation.value;

    } else {
    }

    const plexDataPath = document.getElementById('additional-plex_data_path');

    if (plexDataPath) {
        if (!settingsData['Overlay Settings']) {
            settingsData['Overlay Settings'] = {};
        }
        settingsData['Overlay Settings']['plex_data_path'] = plexDataPath.value;
    }

    const overlayContentCheckDays = document.getElementById('additional-overlay_content_check_interval_days');

    if (overlayContentCheckDays) {
        if (!settingsData['Overlay Settings']) {
            settingsData['Overlay Settings'] = {};
        }
        settingsData['Overlay Settings']['overlay_content_check_interval_days'] = parseInt(overlayContentCheckDays.value) || 7;
    }

    const doNotAddPlexWatchHistoryItemsToQueue = document.getElementById('scraping-do_not_add_plex_watch_history_items_to_queue');

    if (doNotAddPlexWatchHistoryItemsToQueue) {
        // Ensure Scraping section exists
        if (!settingsData['Scraping']) {
            settingsData['Scraping'] = {};
        }
        settingsData['Scraping']['do_not_add_plex_watch_history_items_to_queue'] = doNotAddPlexWatchHistoryItemsToQueue.checked;

    } else {
    }

    
    // Debug: Check exclude_genres in final data
    if (settingsData['Content Sources']) {
        Object.keys(settingsData['Content Sources']).forEach(sourceId => {
            const source = settingsData['Content Sources'][sourceId];
            if (source.exclude_genres !== undefined) {
            }
        });
    }

    // Set default values for enable_upgrading, disable_adult, and trakt_early_releases
    if (settingsData['Scraping']['enable_upgrading'] === undefined) {
        settingsData['Scraping']['enable_upgrading'] = false;
    }
        
    if (settingsData['Scraping']['disable_adult'] === undefined) {
        settingsData['Scraping']['disable_adult'] = true;
    }
        
    if (settingsData['Scraping']['trakt_early_releases'] === undefined) {
        settingsData['Scraping']['trakt_early_releases'] = false;
    }
        

    // Show loading overlay (hide close button for settings save)
    if (window.Loading) {
        window.Loading.show('Saving settings, please wait...', 'This may take a few seconds while the program restarts.', true);
    }

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

        // Save smart collection states AFTER main save completes (avoids race condition)
        var smartCols = {};
        document.querySelectorAll('.smart-coll-cb').forEach(function(cb) {
            smartCols[cb.dataset.rk] = {enabled: cb.checked, title: cb.dataset.title, section: cb.dataset.section, type: cb.dataset.type};
        });
        if (Object.keys(smartCols).length > 0) {
            fetch('/settings/api/plex-smart-collections', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({collections: smartCols})
            }).catch(function() {});
        }

        if (data.status === 'success') {
            // Determine the success message based on restart requirement
            let successMessage;
            if (data.requires_restart) {
                const reasons = data.restart_reasons ? data.restart_reasons.join(', ') : 'critical settings';
                successMessage = `Settings saved successfully.<br><br><strong>⚠️ Restart Required</strong><br>Changes to ${reasons} require restarting CLI Debrid to take effect.`;
            } else {
                successMessage = 'Settings saved successfully.<br>Program runner restarted if running.';
            }

            // Reload scraping tab to update scraper priority fields after settings save
            if (typeof window.reloadTabContent === 'function') {
                console.log('[Settings Save] Reloading scraping tab to update scraper priorities...');
                window.reloadTabContent('scraping', () => {
                    console.log('[Settings Save] Scraping tab reloaded successfully');
                    // Hide loading overlay after reload completes
                    if (window.Loading) {
                        window.Loading.hide();
                    }
                    showPopup({
                        type: data.requires_restart ? POPUP_TYPES.WARNING : POPUP_TYPES.SUCCESS,
                        title: data.requires_restart ? 'Restart Required' : 'Success',
                        message: successMessage
                    });
                }).catch(err => {
                    console.error('[Settings Save] Error reloading scraping tab:', err);
                    // Hide loading overlay even if reload fails
                    if (window.Loading) {
                        window.Loading.hide();
                    }
                    showPopup({
                        type: data.requires_restart ? POPUP_TYPES.WARNING : POPUP_TYPES.SUCCESS,
                        title: data.requires_restart ? 'Restart Required' : 'Success',
                        message: successMessage
                    });
                });
            } else {
                // Hide loading overlay if reloadTabContent not available
                if (window.Loading) {
                    window.Loading.hide();
                }
                showPopup({
                    type: data.requires_restart ? POPUP_TYPES.WARNING : POPUP_TYPES.SUCCESS,
                    title: data.requires_restart ? 'Restart Required' : 'Success',
                    message: successMessage
                });
            }
        } else {
            // Hide loading overlay on error
            if (window.Loading) {
                window.Loading.hide();
            }
            showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: 'Error saving settings: ' + data.message });
        }
    })
    .catch(error => {
        console.error('Error:', error);
        // Hide loading overlay on error
        if (window.Loading) {
            window.Loading.hide();
        }
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
        'Agregarr': 900,
        'MDBList': 900,
        'Collected': 86400,
        'Trakt Watchlist': 900,
        'Trakt Lists': 900,
        'Trakt Collection': 900,
        'Special Trakt Lists': 900,
        'Scrob Lists': 900,
        'Scrob Collection': 900,
        'Special Scrob Lists': 900,
        'My Plex Watchlist': 900,
        'Other Plex Watchlist': 900,
        'My Plex RSS Watchlist': 900,
        'My Friends Plex RSS Watchlist': 900,
        'Adaptive List': 900
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

    // Silently return if tabs don't exist on this page
    if (!tabButtons.length || !tabContents.length || !tabSelect) {
        return;
    }

    // Tab switching is handled in settings_base.html
}

// Update the DOMContentLoaded event listener
document.addEventListener('DOMContentLoaded', function() {
    // Initialize settings tabs if needed
    if (typeof initializeSettingsTabs === 'function') {
        initializeSettingsTabs();
    }
    
    loadSettingsData().then(() => {
        // Setup radio button handling for program_logo
        const logoRadioButtons = document.querySelectorAll('input[type="radio"][name="UI Settings.program_logo"]');
        logoRadioButtons.forEach(radio => {
            radio.addEventListener('change', function() {
                if (this.checked) {
                    saveSingleSetting('UI Settings', 'program_logo', this.value);
                }
            });
        });
        
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
        }
        // Removed warning - this code may run on pages that don't have this element
        
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
    const stalenessThresholdInput = document.getElementById('debug-staleness_threshold');
    
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

    // Handle staleness threshold sync
    if (stalenessThresholdInput) {
        stalenessThresholdInput.addEventListener('change', function() {
            // Update the settingsData object
            if (!settingsData['Staleness Threshold']) {
                settingsData['Staleness Threshold'] = {};
            }
            settingsData['Staleness Threshold']['staleness_threshold'] = parseInt(stalenessThresholdInput.value) || 7;
        });
    }
}

// Add event listener for the true_debug tab content loaded event
document.addEventListener('trueDebugContentLoaded', syncDebugSettings);
document.addEventListener('DOMContentLoaded', syncDebugSettings);

// Function to save a single setting
function saveSingleSetting(section, key, value) {
    const formData = new FormData();
    formData.append('section', section);
    formData.append('key', key);
    formData.append('value', value);
    
    return fetch('/settings/save_single_setting', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            
            // Show a small notification
            const notification = document.createElement('div');
            notification.className = 'setting-saved-notification';
            notification.textContent = 'Setting saved!';
            notification.style.position = 'fixed';
            notification.style.top = '20px';
            notification.style.right = '20px';
            notification.style.backgroundColor = '#4CAF50';
            notification.style.color = 'white';
            notification.style.padding = '10px 20px';
            notification.style.borderRadius = '4px';
            notification.style.zIndex = '1000';
            notification.style.boxShadow = '0 2px 10px rgba(0, 0, 0, 0.3)';
            notification.style.opacity = '1';
            notification.style.transition = 'opacity 0.5s';
            
            document.body.appendChild(notification);
            
            // Remove notification after a delay
            setTimeout(() => {
                notification.style.opacity = '0';
                setTimeout(() => {
                    notification.remove();
                }, 500);
            }, 2000);
            
            // Special handling for program_logo - reload the page to show the change
            if (section === 'UI Settings' && key === 'program_logo') {
                setTimeout(() => {
                    window.location.reload();
                }, 1000);
            }
            
            return true;
        } else {
            console.error(`Error saving setting: ${data.message || 'Unknown error'}`);
            return false;
        }
    })
    .catch(error => {
        console.error('Error:', error);
        return false;
    });
}

function addNasPath() {
    const container = document.getElementById('nas-paths-container');
    if (!container) return;
    const row = document.createElement('div');
    row.className = 'nas-path-row';
    row.style.cssText = 'display:flex;gap:8px;margin-bottom:6px;';
    row.innerHTML = '<input type="text" class="settings-input nas-path-input" data-section="Debug" data-key="nas_paths" placeholder="e.g. /MySamsungNAS_Movies/">' +
                    '<button type="button" class="btn-danger" onclick="removeNasPath(this)" style="padding:4px 10px;">✕</button>';
    container.appendChild(row);
}
window.addNasPath = addNasPath;

function removeNasPath(btn) {
    btn.closest('.nas-path-row').remove();
}
window.removeNasPath = removeNasPath;
