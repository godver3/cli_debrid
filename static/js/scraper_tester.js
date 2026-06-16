document.addEventListener('DOMContentLoaded', function() {
    const searchSection = document.getElementById('search-section');
    const scrapeSection = document.getElementById('scrape-section');
    const searchInput = document.getElementById('search-input');
    const searchButton = document.getElementById('search-button');
    const searchResults = document.getElementById('search-results');
    const newSearchButton = document.getElementById('new-search-button');

    const selectedItem = document.getElementById('selected-item');
    const versionSelect = document.getElementById('version-select');
    const runScrapeButton = document.getElementById('run-scrape-button');
    const scrapeResults = document.getElementById('scrape-results');
    const versionSettings = document.getElementById('version-settings');
    const originalResults = document.getElementById('original-results');
    const adjustedResults = document.getElementById('adjusted-results');
    const scoreBreakdown = document.getElementById('score-breakdown');
    const saveSettingsButton = document.getElementById('save-settings-button');

    let currentItem = null;
    let currentVersion = null;
    let originalVersionSettings = {};
    let modifiedVersionSettings = {};
    let currentItemGenres = [];

    // Check for URL parameters
    function checkForUrlParameters() {
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.has('id') && urlParams.has('title') && urlParams.has('media_type')) {
            const id = urlParams.get('id');
            const title = urlParams.get('title');
            const year = urlParams.get('year');
            const mediaType = urlParams.get('media_type');
            
            console.log(`Auto-selecting item from URL params: ${title} (${year}), type: ${mediaType}, id: ${id}`);
            
            // Create an item object from the URL parameters
            const item = {
                id: id,
                title: title,
                year: year ? parseInt(year) : null,
                mediaType: mediaType
            };
            
            // Auto-convert TMDB ID to IMDB ID if needed
            fetch(`/scraper/convert_tmdb_to_imdb/${id}`)
                .then(response => response.json())
                .then(data => {
                    item.imdbId = data.imdb_id;
                    
                    // Select the item and show the scrape section
                    selectItem(item);
                    showScrapeSection();
                    
                    // Load the TV details if it's a TV show
                    if (mediaType === 'tv') {
                        fetch(`/scraper/get_tv_details/${id}`)
                            .then(response => response.json())
                            .then(data => {
                                if (data.success) {
                                    populateSeasonEpisodeSelects(data);
                                }
                            })
                            .catch(error => {
                                console.error('Error fetching TV show details:', error);
                            });
                    }
                })
                .catch(error => {
                    console.error('Error converting TMDB ID to IMDB ID:', error);
                    
                    // Still select the item even if IMDB ID conversion fails
                    selectItem(item);
                    showScrapeSection();
                });
        }
    }

    // Call the function to check for URL parameters
    checkForUrlParameters();

    searchButton.addEventListener('click', performSearch);
    searchInput.addEventListener('keypress', function(e) {
        if (e.key === 'Enter') {
            performSearch();
        }
    });
    
    runScrapeButton.addEventListener('click', runScrape);
    newSearchButton.addEventListener('click', startNewSearch);

    function saveVersionSettings() {
        const version = document.getElementById('version-select').value;
        const settings = getModifiedVersionSettings();
    
        fetch('/save_version_settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ version: version, settings: settings })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showPopup({ type: 'success', title: 'Success', message: 'Settings saved successfully', autoClose: 4000 });
            } else {
                showPopup({ type: 'error', title: 'Error', message: 'Error saving settings: ' + data.error, autoClose: 4000 });
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showPopup({ type: 'error', title: 'Error', message: 'An error occurred while saving settings. Please check the console for more details.', autoClose: 4000 });
        });
    }

    function performSearch() {
        Loading.show();
        const searchTerm = searchInput.value;
        fetch('/scraper/scraper_tester', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ search_term: searchTerm })
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            // Always show search results, hide compare section
            scrapeSection.style.display = 'none';
            searchResults.style.display = 'block';
            displaySearchResults(data);
        })
        .catch(error => {
            console.error('Error:', error);
            searchResults.innerHTML = '<p>Error performing search. Please try again.</p>';
            Loading.hide();
        });
    }

    function displaySearchResults(results) {
        const searchResultsElement = document.getElementById('search-results');
        searchResultsElement.innerHTML = '';

        // imdb_id is now returned inline by live_search — no conversion needed
        const validResults = results.filter(result => result.imdbId && result.imdbId !== 'N/A');

        if (validResults.length === 0) {
            searchResultsElement.innerHTML = `<p>No results found with valid IMDB IDs.</p>`;
            Loading.hide();
            return;
        }

        const table = document.createElement('table');
        table.className = 'search-results-table';

        const headerRow = table.insertRow();
        ['Title', 'Year', 'Type', 'IMDB ID'].forEach(headerText => {
            const th = document.createElement('th');
            th.textContent = headerText;
            headerRow.appendChild(th);
        });

        validResults.forEach(result => {
            const row = table.insertRow();
            row.className = 'search-result';

            const title = result.title || 'N/A';
            const year = result.year || 'N/A';
            const mediaType = result.mediaType === 'tv' || result.mediaType === 'show' ? 'TV Show' :
                              result.mediaType === 'movie' ? 'Movie' : 'N/A';
            const imdbId = result.imdbId || 'N/A';

            [title, year, mediaType, imdbId].forEach(cellText => {
                const cell = row.insertCell();
                cell.textContent = cellText;
            });

            row.addEventListener('click', () => {
                selectItem(result);
                showScrapeSection();
            });
        });

        searchResultsElement.appendChild(table);
        Loading.hide();
    }
    
    // Update event listeners
    document.addEventListener('DOMContentLoaded', function() {
        const searchInput = document.getElementById('search-input');
        const searchButton = document.getElementById('search-button');
        const runScrapeButton = document.getElementById('run-scrape-button');

        searchButton.addEventListener('click', performSearch);
        searchInput.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                performSearch();
            }
        });

        runScrapeButton.addEventListener('click', runScrape);


    });

    function selectItem(item) {
        console.log('Selected item:', item);  // Debug log
        currentItem = item;
        
        const selectedItemElement = document.getElementById('selected-item');
        if (selectedItemElement) {
            const title = item.title || item.name;
            const year = item.year || (item.releaseDate ? item.releaseDate.substring(0, 4) : 
                         item.firstAirDate ? item.firstAirDate.substring(0, 4) : 'N/A');
            const isTV = item.mediaType === 'tv' || item.mediaType === 'show';
            const mediaType = isTV ? 'TV Show' : 'Movie';
            const imdbId = item.imdbId || 'N/A';
            
            // Store the year in the currentItem object
            currentItem.year = year !== 'N/A' ? parseInt(year) : null;
            // Ensure we store the correct media type
            currentItem.mediaType = isTV ? 'tv' : 'movie';
            
            console.log(`Selected - Title: ${title}, Year: ${currentItem.year}, Type: ${mediaType}, IMDB ID: ${imdbId}`);  // Debug log
            
            selectedItemElement.innerHTML = `
                <table class="selected-item-table">
                    <tr><th>Title:</th><td>${title}</td></tr>
                    <tr><th>Year:</th><td>${year}</td></tr>
                    <tr><th>Type:</th><td>${mediaType}</td></tr>
                    <tr><th>IMDB ID:</th><td>${imdbId}</td></tr>
                </table>
            `;
        } else {
            console.warn('selected-item element not found in the DOM');
        }
        
        // Update the IMDB ID field
        const imdbIdField = document.getElementById('imdbId');
        if (imdbIdField) {
            imdbIdField.value = item.imdbId || '';
        } else {
            console.warn('imdbId element not found in the DOM');
        }
    
        // Show/hide TV controls
        const tvControls = document.getElementById('tv-controls');
        if (tvControls) {
            const isTV = item.mediaType === 'tv' || item.mediaType === 'show';
            tvControls.style.display = isTV ? 'contents' : 'none';
            if (isTV) {
                // Fetch season and episode data for the TV show
                fetch(`/scraper/get_tv_details/${item.id}`)
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            populateSeasonEpisodeSelects(data);
                        } else {
                            console.error('Failed to fetch TV show details:', data.error);
                        }
                    })
                    .catch(error => {
                        console.error('Error fetching TV show details:', error);
                    });
            }
        }
    
        // Load versions for the selected item
        loadVersions();
        
        // Fetch genre information for the selected item
        fetchItemGenres(item);
    }

    function fetchItemGenres(item) {
        console.log('Fetching genres for item:', item);
        
        // Use the same approach as the main scraper - fetch genre info from TMDB via the get_media_meta endpoint
        fetch('/scraper/get_media_meta', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                tmdb_id: item.id,
                media_type: item.mediaType === 'tv' || item.mediaType === 'show' ? 'tv' : 'movie'
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                console.warn('Could not fetch genre information:', data.error);
                currentItemGenres = [];
            } else {
                // The get_media_meta returns genres as an array of genre names
                currentItemGenres = data.genres || [];
                console.log('Fetched genres for item:', currentItemGenres);
            }
        })
        .catch(error => {
            console.error('Error fetching genres:', error);
            currentItemGenres = [];
        });
    }



    function populateSeasonEpisodeSelects(data) {
        const seasonSelect = document.getElementById('season-select');
        const episodeSelect = document.getElementById('episode-select');
    
        // Clear existing options
        seasonSelect.innerHTML = '';
        episodeSelect.innerHTML = '';
    
        if (data.seasons) {
            // Populate seasons
            data.seasons.forEach(season => {
                const option = document.createElement('option');
                option.value = season.season_number;
                option.textContent = `Season ${season.season_number}`;
                seasonSelect.appendChild(option);
            });
    
            // Add event listener to season select to update episodes
            seasonSelect.addEventListener('change', () => {
                const selectedSeason = data.seasons.find(s => s.season_number === parseInt(seasonSelect.value));
                if (selectedSeason) {
                    updateEpisodeSelect(selectedSeason);
                }
            });
    
            // Set default season and trigger episode population
            if (data.seasons.length > 0) {
                seasonSelect.value = data.seasons[0].season_number;
                updateEpisodeSelect(data.seasons[0]);
            }
        }
    }

    function updateEpisodeSelect(season) {
        const episodeSelect = document.getElementById('episode-select');
        episodeSelect.innerHTML = '';
    
        if (season && season.episode_count) {
            for (let i = 1; i <= season.episode_count; i++) {
                const option = document.createElement('option');
                option.value = i;
                option.textContent = `Episode ${i}`;
                episodeSelect.appendChild(option);
            }
        }
        
        // Set default to episode 1
        episodeSelect.value = '1';
    }

    function loadVersions() {
        console.log("Loading versions...");
        fetch('/settings/get_scraping_versions')
            .then(response => {
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                return response.json();
            })
            .then(data => {
                console.log("Received versions data:", data);
                if (!data.versions || !Array.isArray(data.versions)) {
                    throw new Error("Invalid versions data received");
                }
                versionSelect.innerHTML = '';
                data.versions.forEach(version => {
                    const option = document.createElement('option');
                    option.value = version;
                    option.textContent = version;
                    versionSelect.appendChild(option);
                });
                versionSelect.addEventListener('change', (e) => loadVersionSettings(e.target.value));
                if (versionSelect.options.length > 0) {
                    loadVersionSettings(versionSelect.value);
                } else {
                    console.error("No versions available");
                }
            })
            .catch(error => {
                console.error('Error loading versions:', error);
                versionSelect.innerHTML = `<option>Error: ${error.message}</option>`;
            });
    }

    function loadVersionSettings(version) {
        console.log(`Loading settings for version: ${version}`);
        fetch(`/settings/get_version_settings?version=${version}`)
            .then(response => {
                if (!response.ok) {
                    return response.json().then(errorData => {
                        throw new Error(`Server error: ${errorData.error || response.statusText}`);
                    });
                }
                return response.json();
            })
            .then(data => {
                console.log("Received version settings:", data);
                if (data && data[version]) {
                    displayVersionSettings(version, data[version]);
                } else {
                    console.error("No settings found for version:", version);
                    displayErrorMessage(`No settings found for version: ${version}`);
                }
            })
            .catch(error => {
                console.error('Error loading version settings:', error);
                displayErrorMessage(`Error loading settings: ${error.message}`);
            });
    }
    
    function displayErrorMessage(message) {
        const errorDiv = document.createElement('div');
        errorDiv.className = 'error-message';
        errorDiv.textContent = message;
        
        const settingsContainer = document.querySelector('.version-settings-container');
        settingsContainer.innerHTML = '';
        settingsContainer.appendChild(errorDiv);
    }

    function displayVersionSettings(version, settings) {
        console.log("Displaying version settings:", version, settings);
    
        const originalSettingsContainer = document.getElementById('originalSettings');
        const modifiedSettingsContainer = document.getElementById('modifiedSettings');
        
        originalSettingsContainer.innerHTML = `<h3>Original ${settings.display_name || version} Settings</h3>`;
        modifiedSettingsContainer.innerHTML = `<h3>Modified ${settings.display_name || version} Settings</h3>`;
    
        // Define the order of settings
        const settingsOrder = [
            'max_resolution',
            'resolution_wanted',
            'min_size_gb',
            'max_size_gb',
            'min_bitrate_mbps',
            'max_bitrate_mbps',
            'resolution_weight',
            'size_weight',
            'bitrate_weight',
            'similarity_weight',
            'similarity_threshold',
            'similarity_threshold_anime',
            'enable_hdr',
            'hdr_weight',
            'filter_in',
            'filter_out',
            'preferred_filter_in',
            'preferred_filter_out',
            'enable_scraper_priorities',
            'scraper_priorities'
        ];
    
        // Sort the settings according to the defined order
        const sortedSettings = Object.entries(settings)
            .filter(([key]) => key !== 'display_name')
            .sort(([a], [b]) => {
                const aIndex = settingsOrder.indexOf(a);
                const bIndex = settingsOrder.indexOf(b);
                if (aIndex === -1) return 1;  // Put unknown settings at the end
                if (bIndex === -1) return -1;
                return aIndex - bIndex;
            });
    
        for (const [key, value] of sortedSettings) {
            const formGroup = document.createElement('div');
            formGroup.className = 'settings-form-group';
    
            let labelText = key.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
            
            console.log(`Setting ${key} to:`, value);
    
            // Rename the fields
            if (key === 'max_resolution') {
                labelText = 'Resolution Wanted';
            } else if (key === 'resolution_wanted') {
                labelText = 'Resolution Symbol';
            }
    
            const label = document.createElement('label');
            label.className = 'settings-title';
            label.setAttribute('for', `scraping-${version}-${key}`);
            label.textContent = `${labelText}:`;
    
            // Create input elements for both original and modified settings
            let [originalInput, modifiedInput] = createInputElements(key, value);
    
            // Set up original input
            originalInput.id = `original-scraping-${version}-${key}`;
            originalInput.name = `Original.Scraping.versions.${version}.${key}`;
            originalInput.className = 'settings-input original-input';
            originalInput.disabled = true;
    
            // Set up modified input
            modifiedInput.id = `scraping-${version}-${key}`;
            modifiedInput.name = `Scraping.versions.${version}.${key}`;
            modifiedInput.className = 'settings-input';
    
            const originalGroup = formGroup.cloneNode(true);
            originalGroup.appendChild(label.cloneNode(true));
            originalGroup.appendChild(originalInput);
            originalSettingsContainer.appendChild(originalGroup);
    
            const modifiedGroup = formGroup.cloneNode(true);
            modifiedGroup.appendChild(label.cloneNode(true));
            modifiedGroup.appendChild(modifiedInput);
            modifiedSettingsContainer.appendChild(modifiedGroup);
        }
    
        // Add the save button to hidden modified container (still needed by saveVersionSettings)
        const saveButton = document.createElement('button');
        saveButton.id = 'save-modified-version-button';
        saveButton.textContent = 'Save Modified Version';
        saveButton.onclick = saveVersionSettings;
        modifiedSettingsContainer.appendChild(saveButton);

        // Store the original settings for comparison
        originalSettingsContainer.dataset.settings = JSON.stringify(settings);

        // Build accordion UI
        buildSettingsAccordion(version, settings, sortedSettings);

        // Initialize the save button state
        updateSaveButtonState();
    }

    function buildGroupPreview(group, settings) {
        const get = (k) => settings[k] !== undefined ? settings[k] : null;
        const countVal = (v) => {
            if (!v) return 0;
            if (Array.isArray(v)) return v.length;
            if (typeof v === 'string') return v.trim().split('\n').filter(l => l.trim()).length;
            if (typeof v === 'object') return Object.keys(v).length;
            return 0;
        };

        const pills = [];

        const boolPill = (label, val) => `${label}: ${(val === true || val === 'true') ? 'Enabled' : 'Disabled'}`;

        if (group.label === 'Resolution & Quality') {
            const res = get('max_resolution');
            if (res) pills.push(res);
            const hdr = get('enable_hdr');
            if (hdr !== null) pills.push(boolPill('HDR', hdr));
            const hdrW = get('hdr_weight');
            if (hdrW !== null && hdrW !== 0 && hdrW !== '0') pills.push(`HDR wt: ${hdrW}`);
        } else if (group.label === 'Size & Bitrate') {
            const minS = get('min_size_gb'), maxS = get('max_size_gb');
            pills.push(`${minS ?? 0}–${maxS || '∞'} GB`);
            const minB = get('min_bitrate_mbps'), maxB = get('max_bitrate_mbps');
            pills.push(`${minB ?? 0}–${maxB || '∞'} Mbps`);
        } else if (group.label === 'Scoring & Weights') {
            const rw = get('resolution_weight'), sw = get('size_weight');
            const bw = get('bitrate_weight'), simw = get('similarity_weight');
            const thresh = get('similarity_threshold'), animeThresh = get('similarity_threshold_anime');
            if (rw != null) pills.push(`Res: ${rw}`);
            if (sw != null) pills.push(`Size: ${sw}`);
            if (bw != null) pills.push(`Bit: ${bw}`);
            if (simw != null) pills.push(`Sim: ${simw}`);
            if (thresh != null) pills.push(`Thresh: ${thresh}`);
            if (animeThresh != null) pills.push(`Anime: ${animeThresh}`);
        } else if (group.label === 'Filters') {
            const fi = countVal(get('filter_in'));
            const fo = countVal(get('filter_out'));
            const pfi = countVal(get('preferred_filter_in'));
            const pfo = countVal(get('preferred_filter_out'));
            if (fi) pills.push(`${fi} filter-in`);
            if (fo) pills.push(`${fo} filter-out`);
            if (pfi) pills.push(`${pfi} pref-in`);
            if (pfo) pills.push(`${pfo} pref-out`);
            if (!pills.length) pills.push('No filters');
        } else if (group.label === 'Scrapers') {
            const enabled = get('enable_scraper_priorities');
            pills.push(boolPill('Priorities', enabled));
            // Add a pill for each scraper key (ends with _N)
            Object.entries(settings).forEach(([k, v]) => {
                if (/_\d+$/.test(k)) pills.push(`${k}: ${v}`);
            });
        } else {
            (group.keys || []).forEach(k => {
                const v = get(k);
                if (v === null || v === '' || v === undefined) return;
                const label = k.replace(/_/g,' ');
                if (typeof v === 'boolean' || v === 'true' || v === 'false') {
                    pills.push(boolPill(label, v));
                } else {
                    pills.push(`${label}: ${v}`);
                }
            });
        }

        if (!pills.length) return '';
        return pills.map(p => `<span class="st-preview-pill">${p}</span>`).join('');
    }

    // Known setting keys that belong to named groups
    const KNOWN_SETTING_KEYS = new Set([
        'max_resolution','resolution_wanted','enable_hdr','hdr_weight',
        'min_size_gb','max_size_gb','min_bitrate_mbps','max_bitrate_mbps',
        'resolution_weight','size_weight','bitrate_weight','similarity_weight','similarity_threshold','similarity_threshold_anime',
        'filter_in','filter_out','preferred_filter_in','preferred_filter_out',
        'enable_scraper_priorities','scraper_priorities', // scraper_priorities hidden from accordion
        'display_name',
    ]);

    // Accordion group definitions — Scrapers keys are extended dynamically
    const SETTING_GROUPS = [
        { label: 'Resolution & Quality', keys: ['max_resolution', 'resolution_wanted', 'enable_hdr', 'hdr_weight'] },
        { label: 'Size & Bitrate', keys: ['min_size_gb', 'max_size_gb', 'min_bitrate_mbps', 'max_bitrate_mbps'] },
        { label: 'Scoring & Weights', keys: ['resolution_weight', 'size_weight', 'bitrate_weight', 'similarity_weight', 'similarity_threshold', 'similarity_threshold_anime'] },
        { label: 'Filters', keys: ['filter_in', 'filter_out', 'preferred_filter_in', 'preferred_filter_out'] },
        { label: 'Scrapers', keys: ['enable_scraper_priorities'] },
    ];

    function buildSettingsAccordion(version, settings, sortedSettings) {
        const accordion = document.getElementById('settings-accordion');
        if (!accordion) return;
        accordion.innerHTML = `
            <div class="st-accordion-col-headers">
                <div>Setting</div>
                <div>Original</div>
                <div>Modified</div>
            </div>`;

        // Scraper keys end with _N (e.g. Jackett_1, AIOStreams-API_1, Zilean_1)
        const isScraperKey = (k) => /_\d+$/.test(k);

        const scraperGroup = SETTING_GROUPS.find(g => g.label === 'Scrapers');
        sortedSettings.forEach(([k]) => {
            if (!KNOWN_SETTING_KEYS.has(k) && isScraperKey(k)) {
                if (!scraperGroup.keys.includes(k)) scraperGroup.keys.push(k);
                KNOWN_SETTING_KEYS.add(k);
            }
        });

        const assignedKeys = new Set(SETTING_GROUPS.flatMap(g => g.keys));

        // Keys to never render in the accordion UI
        const HIDDEN_KEYS = new Set(['scraper_priorities', 'display_name']);

        // Add "Other" group for remaining unknown keys
        const otherKeys = sortedSettings.map(([k]) => k).filter(k => !assignedKeys.has(k) && !HIDDEN_KEYS.has(k));
        const groups = otherKeys.length > 0
            ? [...SETTING_GROUPS, { label: 'Other', keys: otherKeys }]
            : SETTING_GROUPS;

        groups.forEach((group, gi) => {
            const keysInGroup = sortedSettings
                .map(([k, v]) => ({ k, v }))
                .filter(({ k }) => group.keys.includes(k) && !HIDDEN_KEYS.has(k));

            if (keysInGroup.length === 0) return;

            // Check if any value differs from original for badge
            let hasChanges = false;

            const section = document.createElement('div');
            section.className = 'st-accordion-section';

            const header = document.createElement('div');
            header.className = 'st-accordion-header';
            header.innerHTML = `<span class="st-accordion-label">${group.label}</span><span class="st-accordion-arrow">▾</span>`;

            // Preview bar — shown when collapsed, hidden when open
            const preview = document.createElement('div');
            preview.className = 'st-accordion-preview';
            preview.style.display = 'flex'; // visible by default (collapsed state)

            header.addEventListener('click', () => {
                const isOpen = section.classList.toggle('open');
                header.querySelector('.st-accordion-arrow').textContent = isOpen ? '▴' : '▾';
                preview.style.display = isOpen ? 'none' : 'block';
            });

            const body = document.createElement('div');
            body.className = 'st-accordion-body';

            keysInGroup.forEach(({ k, v }) => {
                // Get the original and modified inputs from hidden containers
                const origEl = document.getElementById(`original-scraping-${version}-${k}`);
                const modEl = document.getElementById(`scraping-${version}-${k}`);
                if (!origEl || !modEl) return;

                let labelText = k.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
                if (k === 'max_resolution') labelText = 'Resolution Wanted';
                if (k === 'resolution_wanted') labelText = 'Resolution Symbol';

                const row = document.createElement('div');
                row.className = 'st-setting-row';
                row.dataset.key = k;

                const lbl = document.createElement('div');
                lbl.className = 'st-setting-label';
                lbl.textContent = labelText;

                const origWrap = document.createElement('div');
                origWrap.className = 'st-setting-orig';
                const origClone = origEl.cloneNode(true);
                origClone.disabled = true;
                origWrap.appendChild(origClone);

                const modWrap = document.createElement('div');
                modWrap.className = 'st-setting-mod';

                const FILTER_KEYS = ['filter_in', 'filter_out', 'preferred_filter_in', 'preferred_filter_out'];
                let visibleMod;
                if (FILTER_KEYS.includes(k)) {
                    // cloneNode drops onclick handlers — rebuild fresh so Add Item / Remove work
                    let liveEl;
                    if (k === 'filter_in' || k === 'filter_out') {
                        const currentItems = Array.from(modEl.querySelectorAll('.filter-item')).map(item => {
                            const pattern = item.querySelector('.filter-input')?.value || '';
                            const source = item.querySelector('.filter-source')?.value || 'both';
                            return {pattern, source};
                        });
                        liveEl = createFilterList(k, currentItems, false);
                    } else {
                        const currentItems = Array.from(modEl.querySelectorAll('.preferred-filter-item')).map(item => {
                            const term = item.querySelector('.filter-input').value;
                            const weight = parseInt(item.querySelector('.filter-weight').value) || 1;
                            return [term, weight];
                        });
                        liveEl = createPreferredFilterList(k, currentItems, false);
                    }
                    liveEl.id = modEl.id + '-vis';
                    modWrap.appendChild(liveEl);
                    visibleMod = liveEl;
                } else {
                    // Non-filter keys: clone is fine (no event listeners needed beyond input/change)
                    const clone = modEl.cloneNode(true);
                    clone.id = modEl.id + '-vis';
                    modWrap.appendChild(clone);
                    visibleMod = clone;
                }

                if (visibleMod) {
                    const refreshPreview = () => {
                        const liveSettings = {};
                        keysInGroup.forEach(({ k: key }) => {
                            const el = document.getElementById(`scraping-${version}-${key}-vis`)
                                    || document.getElementById(`scraping-${version}-${key}`);
                            if (!el) return;
                            liveSettings[key] = el.type === 'checkbox' ? el.checked : el.value;
                        });
                        const newPills = buildGroupPreview(group, liveSettings);
                        if (newPills) preview.innerHTML = newPills;
                    };
                    visibleMod.addEventListener('input', () => {
                        if (!FILTER_KEYS.includes(k)) {
                            modEl.value = visibleMod.value;
                            modEl.checked = visibleMod.checked;
                        }
                        refreshPreview();
                    });
                    visibleMod.addEventListener('change', () => {
                        if (!FILTER_KEYS.includes(k)) {
                            modEl.value = visibleMod.value;
                            if (visibleMod.type === 'checkbox') modEl.checked = visibleMod.checked;
                        }
                        refreshPreview();
                        updateSaveButtonState();
                    });
                }

                row.appendChild(lbl);
                row.appendChild(origWrap);
                row.appendChild(modWrap);
                body.appendChild(row);
            });

            // Build preview text from settings object directly
            const previewPills = buildGroupPreview(group, settings);
            if (previewPills) {
                preview.innerHTML = previewPills;
            }

            section.appendChild(header);
            section.appendChild(preview);
            section.appendChild(body);
            accordion.appendChild(section);

            // Open first group by default — hide preview when open
            if (gi === 0) {
                section.classList.add('open');
                header.querySelector('.st-accordion-arrow').textContent = '▴';
                preview.style.display = 'none';
            }
        });
    }
    
    function createInputElements(key, value) {
        let originalInput, modifiedInput;
    
        if (typeof value === 'boolean') {
            originalInput = document.createElement('input');
            originalInput.type = 'checkbox';
            originalInput.checked = value;
            modifiedInput = originalInput.cloneNode(true);
        } else if (key === 'max_resolution') {
            originalInput = document.createElement('select');
            modifiedInput = originalInput.cloneNode(true);
            ['2160p', '1080p', '720p', 'SD'].forEach(option => {
                const optionElement = document.createElement('option');
                optionElement.value = option;
                optionElement.textContent = option;
                optionElement.selected = value === option;
                originalInput.appendChild(optionElement);
                modifiedInput.appendChild(optionElement.cloneNode(true));
            });
            originalInput.value = value;
            modifiedInput.value = value;
        } else if (key === 'resolution_wanted') {
            originalInput = document.createElement('select');
            modifiedInput = originalInput.cloneNode(true);
            ['<=', '==', '>='].forEach(option => {
                const optionElement = document.createElement('option');
                optionElement.value = option;
                optionElement.textContent = option;
                optionElement.selected = value === option;
                originalInput.appendChild(optionElement);
                modifiedInput.appendChild(optionElement.cloneNode(true));
            });
            originalInput.value = value;
            modifiedInput.value = value;
        } else if (['filter_in', 'filter_out'].includes(key)) {
            originalInput = createFilterList(key, value, true);
            modifiedInput = createFilterList(key, value, false);
        } else if (['preferred_filter_in', 'preferred_filter_out'].includes(key)) {
            originalInput = createPreferredFilterList(key, value, true);
            modifiedInput = createPreferredFilterList(key, value, false);
        } else if (key === 'scraper_priorities') {
            // Treat scraper_priorities as a JSON object display
            originalInput = document.createElement('textarea');
            originalInput.value = JSON.stringify(value || {}, null, 2);
            originalInput.rows = 5;
            originalInput.disabled = true;

            modifiedInput = document.createElement('textarea');
            modifiedInput.value = JSON.stringify(value || {}, null, 2);
            modifiedInput.rows = 5;
            modifiedInput.className = 'scraper-priorities-textarea';
        } else if (key === 'max_size_gb' || key === 'min_size_gb' || key === 'max_bitrate_mbps' || key === 'min_bitrate_mbps') {
            // Size and bitrate fields
            originalInput = document.createElement('input');
            originalInput.type = 'number';
            originalInput.step = '0.01';  // Allow decimal values
            originalInput.min = '0';      // Don't allow negative values
            originalInput.placeholder = key.startsWith('max_') ? 'No limit' : '0';
            originalInput.value = (value === '' || value === null || value === Infinity) ? '' : value;
            modifiedInput = originalInput.cloneNode(true);
        } else if (key.endsWith('_weight')) {
            // Weight fields
            originalInput = document.createElement('input');
            originalInput.type = 'number';
            originalInput.step = '1';     // Integer values only
            originalInput.min = '0';      // Don't allow negative values
            originalInput.placeholder = '0';
            originalInput.value = value;
            modifiedInput = originalInput.cloneNode(true);
        } else if (key.includes('similarity_threshold')) {
            // Similarity threshold fields
            originalInput = document.createElement('input');
            originalInput.type = 'number';
            originalInput.step = '0.01';  // Allow decimal values
            originalInput.min = '0';      // Min value 0
            originalInput.max = '1';      // Max value 1
            originalInput.placeholder = '0.8';
            originalInput.value = value;
            modifiedInput = originalInput.cloneNode(true);
        } else {
            originalInput = document.createElement('input');
            originalInput.type = typeof value === 'number' ? 'number' : 'text';
            originalInput.value = value;
            modifiedInput = originalInput.cloneNode(true);
        }
    
        return [originalInput, modifiedInput];
    }
    
    function createFilterList(key, items, isOriginal) {
        const listContainer = document.createElement('div');
        listContainer.className = 'filter-list';
    
        items.forEach(item => {
            const itemElement = createFilterItem(item, isOriginal);
            listContainer.appendChild(itemElement);
        });
    
        const addButton = document.createElement('button');
        addButton.textContent = 'Add Item';
        addButton.className = isOriginal ? 'add-filter-item original-input' : 'add-filter-item';
        addButton.disabled = isOriginal;
        if (!isOriginal) {
            addButton.onclick = () => {
                const newItem = createFilterItem('', false);
                listContainer.insertBefore(newItem, addButton);
            };
        }
    
        listContainer.appendChild(addButton);
    
        return listContainer;
    }
    
    function createFilterItem(value, isOriginal) {
        const itemContainer = document.createElement('div');
        itemContainer.className = 'filter-item';

        const pattern = (value && typeof value === 'object') ? (value.pattern || '') : (value || '');
        const source = (value && typeof value === 'object') ? (value.source || 'both') : 'both';

        const input = document.createElement('input');
        input.type = 'text';
        input.value = pattern;
        input.className = isOriginal ? 'filter-input original-input' : 'filter-input';
        input.disabled = isOriginal;

        const select = document.createElement('select');
        select.className = isOriginal ? 'filter-source original-input' : 'filter-source';
        select.disabled = isOriginal;
        ['both', 'nzb', 'debrid'].forEach(opt => {
            const o = document.createElement('option');
            o.value = opt;
            o.textContent = opt.charAt(0).toUpperCase() + opt.slice(1);
            if (opt === source) o.selected = true;
            select.appendChild(o);
        });

        const removeButton = document.createElement('button');
        removeButton.textContent = 'Remove';
        removeButton.className = isOriginal ? 'remove-filter-item original-input' : 'remove-filter-item';
        removeButton.disabled = isOriginal;
        if (!isOriginal) {
            removeButton.onclick = () => itemContainer.remove();
        }

        itemContainer.appendChild(input);
        itemContainer.appendChild(select);
        itemContainer.appendChild(removeButton);

        return itemContainer;
    }
    
    function createPreferredFilterList(key, items, isOriginal) {
        const listContainer = document.createElement('div');
        listContainer.className = 'preferred-filter-list';
    
        items.forEach(item => {
            const itemElement = createPreferredFilterItem(item[0], item[1], isOriginal);
            listContainer.appendChild(itemElement);
        });
    
        const addButton = document.createElement('button');
        addButton.textContent = 'Add Item';
        addButton.className = isOriginal ? 'add-filter-item original-input' : 'add-filter-item';
        addButton.disabled = isOriginal;
        if (!isOriginal) {
            addButton.onclick = () => {
                const newItem = createPreferredFilterItem('', 1, false);
                listContainer.insertBefore(newItem, addButton);
            };
        }
    
        listContainer.appendChild(addButton);
    
        return listContainer;
    }
    
    function createPreferredFilterItem(term, weight, isOriginal) {
        const itemContainer = document.createElement('div');
        itemContainer.className = 'preferred-filter-item';
    
        const termInput = document.createElement('input');
        termInput.type = 'text';
        termInput.value = term;
        termInput.className = isOriginal ? 'filter-input original-input' : 'filter-input';
        termInput.disabled = isOriginal;
    
        const weightInput = document.createElement('input');
        weightInput.type = 'number';
        weightInput.value = weight;
        weightInput.min = '1';
        weightInput.className = isOriginal ? 'filter-weight original-input' : 'filter-weight';
        weightInput.disabled = isOriginal;
    
        const removeButton = document.createElement('button');
        removeButton.textContent = 'Remove';
        removeButton.className = isOriginal ? 'remove-filter-item original-input' : 'remove-filter-item';
        removeButton.disabled = isOriginal;
        if (!isOriginal) {
            removeButton.onclick = () => itemContainer.remove();
        }
    
        itemContainer.appendChild(termInput);
        itemContainer.appendChild(weightInput);
        itemContainer.appendChild(removeButton);
    
        return itemContainer;
    }

    function getModifiedVersionSettings() {
        const settings = {};
        document.querySelectorAll('#modifiedSettings .settings-input').forEach(input => {
            const settingKey = input.id.split('-')[2];
            if (input.type === 'checkbox') {
                settings[settingKey] = input.checked;
            } else if (input.type === 'select-one') {
                settings[settingKey] = input.value;
            } else if (settingKey === 'filter_in' || settingKey === 'filter_out') {
                // Prefer the live accordion copy (-vis) which has working Add/Remove handlers
                const live = document.getElementById(input.id + '-vis') || input;
                settings[settingKey] = Array.from(live.querySelectorAll('.filter-item')).map(item => {
                    const pattern = item.querySelector('.filter-input')?.value?.trim();
                    const source = item.querySelector('.filter-source')?.value || 'both';
                    return pattern ? {pattern, source} : null;
                }).filter(Boolean);
            } else if (settingKey === 'preferred_filter_in' || settingKey === 'preferred_filter_out') {
                const live = document.getElementById(input.id + '-vis') || input;
                settings[settingKey] = Array.from(live.querySelectorAll('.preferred-filter-item')).map(item => {
                    const term = item.querySelector('.filter-input').value;
                    const weight = parseInt(item.querySelector('.filter-weight').value);
                    return term && !isNaN(weight) ? [term, weight] : null;
                }).filter(Boolean);
            } else if (settingKey === 'scraper_priorities') {
                // Parse the JSON from the textarea
                try {
                    settings[settingKey] = JSON.parse(input.value || '{}');
                } catch (e) {
                    console.error('Error parsing scraper_priorities JSON:', e);
                    settings[settingKey] = {};
                }
            } else if (settingKey === 'max_size_gb' || settingKey === 'min_size_gb' || settingKey === 'max_bitrate_mbps' || settingKey === 'min_bitrate_mbps') {
                // Handle size and bitrate fields
                if (input.value === '' || input.value === null) {
                    settings[settingKey] = settingKey.startsWith('max_') ? Infinity : 0;
                } else {
                    const numValue = parseFloat(input.value);
                    settings[settingKey] = isNaN(numValue) ? (settingKey.startsWith('max_') ? Infinity : 0) : numValue;
                }
            } else {
                settings[settingKey] = input.value;
            }
        });
        return settings;
    }

    function runScrape() {
        Loading.show();
        const version = document.getElementById('version-select').value;
        const modifiedSettings = getModifiedVersionSettings();
    
        const isTV = currentItem.mediaType === 'tv' || currentItem.mediaType === 'show';
        
        // Use the fetched genres from TMDB instead of keyword-based detection
        const genres = currentItemGenres || [];
        
        console.log(`Using fetched genres for "${currentItem.title || currentItem.name}":`, genres);

        const scrapeData = {
            imdb_id: document.getElementById('imdbId').value || '',
            tmdb_id: currentItem.id,
            title: currentItem.title || currentItem.name,
            year: currentItem.year,
            movie_or_episode: isTV ? 'episode' : 'movie',
            version: version,
            modifiedSettings: modifiedSettings,
            genres: genres,  // Add genres to the scrape data
            skip_cache_check: true  // Always skip cache check in scraper tester
        };
    
        // Add TV show specific information
        if (isTV) {
            const seasonSelect = document.getElementById('season-select');
            const episodeSelect = document.getElementById('episode-select');
            const multiCheckbox = document.getElementById('multi-checkbox');
            
            if (seasonSelect && episodeSelect) {
                scrapeData.season = parseInt(seasonSelect.value) || 1;
                scrapeData.episode = parseInt(episodeSelect.value) || 1;
                scrapeData.multi = multiCheckbox ? multiCheckbox.checked : false;
            }
        }
    
        console.log('Scrape data:', scrapeData);
    
        fetch('/scraper/run_scrape', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(scrapeData)
        })
        .then(response => response.json())
        .then(data => {
            console.log('Received data:', data);  // Log the entire response
    
            if (!data || ( (!data.originalResults && !data.adjustedResults) && (!data.originalFilteredOutResults && !data.adjustedFilteredOutResults) ) ) {
                console.error('Invalid response structure:', data);
                throw new Error('Invalid response structure');
            }
    
            const originalResults = data.originalResults || [];
            const adjustedResults = data.adjustedResults || [];
            const originalFilteredOutResults = data.originalFilteredOutResults || [];
            const adjustedFilteredOutResults = data.adjustedFilteredOutResults || [];
            
            console.log('Original results:', originalResults);
            console.log('Adjusted results:', adjustedResults);
            console.log('Original filtered out results:', originalFilteredOutResults);
            console.log('Adjusted filtered out results:', adjustedFilteredOutResults);
    
            displayScrapeResults({
                originalResults, 
                adjustedResults, 
                originalFilteredOutResults, 
                adjustedFilteredOutResults
            });
        })
        .catch(error => {
            console.error('Error:', error);
            // Display an error message to the user
            document.getElementById('scrape-results').innerHTML = '<p>An error occurred while fetching results. Please try again.</p>';
        })
        .finally(() => {
            Loading.hide();
        });
    }
   
    function displayScrapeResults(data) {
        const originalResultsDiv = document.getElementById('original-results');
        const adjustedResultsDiv = document.getElementById('adjusted-results');

        // Clear previous results and set main headings with counts
        const originalPassedCount = (data.originalResults || []).length;
        const originalFilteredCount = (data.originalFilteredOutResults || []).length;
        const adjustedPassedCount = (data.adjustedResults || []).length;
        const adjustedFilteredCount = (data.adjustedFilteredOutResults || []).length;
        
        const makeHeader = (label, passedCount, filteredCount, colId) => `
            <div class="results-header-row">
                <h3>${label} <span style="font-size:0.8em;font-weight:normal;color:#aaa">(${passedCount} passed, ${filteredCount} filtered)</span></h3>
                <label class="show-filtered-toggle">
                    <input type="checkbox" id="show-filtered-${colId}">
                    <span>Show filtered</span>
                </label>
            </div>`;

        if (originalResultsDiv) originalResultsDiv.innerHTML = makeHeader('Original Results', originalPassedCount, originalFilteredCount, 'original');
        if (adjustedResultsDiv) adjustedResultsDiv.innerHTML = makeHeader('Adjusted Results', adjustedPassedCount, adjustedFilteredCount, 'adjusted');

        // Build title sets for diff indicators
        const originalTitles = new Set((data.originalResults || []).map(r => r.title || r.original_title || ''));
        const adjustedTitles = new Set((data.adjustedResults || []).map(r => r.title || r.original_title || ''));
        // Attach to data for use in createResultsTable
        data._originalTitles = originalTitles;
        data._adjustedTitles = adjustedTitles;

        console.log("Original Results Div:", originalResultsDiv);
        console.log("Adjusted Results Div:", adjustedResultsDiv);
        
        // Combine passed and filtered out results for the "Original" column
        // Add a marker to distinguish them
        const passedOriginalResults = (data.originalResults || []).map(r => ({ ...r, __isActuallyFilteredOut: false }));
        const filteredOriginalResults = (data.originalFilteredOutResults || []).map(r => ({ ...r, __isActuallyFilteredOut: true }));
        const allOriginalDisplayItems = passedOriginalResults.concat(filteredOriginalResults);

        if (originalResultsDiv) {
            if (allOriginalDisplayItems.length > 0) {
                const tbl = createResultsTable(allOriginalDisplayItems, 'original');
                // Mark rows only in original (not in adjusted)
                tbl.querySelectorAll('tr.result-item').forEach((row, i) => {
                    const item = allOriginalDisplayItems[i];
                    if (!item || item.__isActuallyFilteredOut) return;
                    const title = item.title || item.original_title || '';
                    if (!adjustedTitles.has(title)) row.classList.add('result-only-in-original');
                });
                originalResultsDiv.appendChild(tbl);
            } else {
                originalResultsDiv.innerHTML += '<p>No original results or filtered out items to display.</p>';
            }
        }

        // Combine passed and filtered out results for the "Adjusted" column
        const passedAdjustedResults = (data.adjustedResults || []).map(r => ({ ...r, __isActuallyFilteredOut: false }));
        const filteredAdjustedResults = (data.adjustedFilteredOutResults || []).map(r => ({ ...r, __isActuallyFilteredOut: true }));
        const allAdjustedDisplayItems = passedAdjustedResults.concat(filteredAdjustedResults);

        if (adjustedResultsDiv) {
            if (allAdjustedDisplayItems.length > 0) {
                const tbl = createResultsTable(allAdjustedDisplayItems, 'adjusted');
                // Mark rows only in adjusted (new results from settings change)
                tbl.querySelectorAll('tr.result-item').forEach((row, i) => {
                    const item = allAdjustedDisplayItems[i];
                    if (!item || item.__isActuallyFilteredOut) return;
                    const title = item.title || item.original_title || '';
                    if (!originalTitles.has(title)) row.classList.add('result-only-in-adjusted');
                });
                adjustedResultsDiv.appendChild(tbl);
            } else {
                adjustedResultsDiv.innerHTML += '<p>No adjusted results or filtered out items to display.</p>';
            }
        }
    
        // Hide filtered rows by default, wire up toggles
        ['original', 'adjusted'].forEach(col => {
            const container = document.getElementById(`${col}-results`);
            if (!container) return;
            const checkbox = container.querySelector(`#show-filtered-${col}`);
            const filteredRows = container.querySelectorAll('tr.filtered-out-item');
            // Hide by default
            filteredRows.forEach(r => r.style.display = 'none');
            if (checkbox) {
                checkbox.addEventListener('change', () => {
                    filteredRows.forEach(r => r.style.display = checkbox.checked ? '' : 'none');
                });
            }
        });

        // Ensure scrapeResults element exists before trying to scroll
        const scrapeResultsElement = document.getElementById('scrape-results');
        if (scrapeResultsElement) {
            scrapeResultsElement.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    
        // Add click event listeners to result items (both passed and filtered out)
        document.querySelectorAll('.result-item').forEach(item => {
            const new_item = item.cloneNode(true); // Clone to remove old listeners
            item.parentNode.replaceChild(new_item, item);

            new_item.addEventListener('click', function() {
                const tableElement = this.closest('table');
                if (!tableElement) return;

                const columnDiv = this.closest('.results-column');
                if (!columnDiv) return;
                
                const tableId = columnDiv.id;
                
                // Find the original data object. This relies on the __isActuallyFilteredOut flag
                // and the order of concatenation.
                let resultData;
                const displayedItems = (tableId === 'original-results') ? allOriginalDisplayItems : allAdjustedDisplayItems;
                const originalPassedItems = (tableId === 'original-results') ? (data.originalResults || []) : (data.adjustedResults || []);
                const originalFilteredOutItems = (tableId === 'original-results') ? (data.originalFilteredOutResults || []) : (data.adjustedFilteredOutResults || []);
                
                // Get the index from the tbody (now properly structured)
                const tbody = tableElement.querySelector('tbody');
                const rowIndex = Array.from(tbody.children).indexOf(this); // Index in the displayed table (excluding header)
                const clickedItemData = displayedItems[rowIndex];

                if (clickedItemData) {
                    if (clickedItemData.__isActuallyFilteredOut) {
                        // For filtered out items, show the filter reason
                        displayFilterReason(clickedItemData);
                    } else {
                        // For passed items, show score breakdown
                        // To find the correct item in the original non-filtered array,
                        // we need to count how many non-filtered items appeared before this one.
                        let originalIndex = -1;
                        let nonFilteredCount = 0;
                        for(let i=0; i <= rowIndex; i++) {
                            if (displayedItems[i] && !displayedItems[i].__isActuallyFilteredOut) {
                                if (i === rowIndex) {
                                    originalIndex = nonFilteredCount;
                                }
                                nonFilteredCount++;
                            }
                        }
                        if (originalIndex !== -1 && originalIndex < originalPassedItems.length) {
                             resultData = originalPassedItems[originalIndex];
                        }
                        
                        if (resultData) {
                            displayScoreBreakdown(resultData);
                        }
                    }
                }
            });
        });
    }
    
    function createResultsTable(results, type /* 'original' or 'adjusted' */) {
        const table = document.createElement('table');
        table.className = 'settings-table';
        
        // Create proper table structure with thead and tbody
        const thead = document.createElement('thead');
        const tbody = document.createElement('tbody');
        
        const headerRow = thead.insertRow();
        headerRow.innerHTML = `<th>Release</th><th>Size</th><th>Source</th><th>Score</th>`;

        table.appendChild(thead);
        table.appendChild(tbody);

        results.forEach((result, index) => {
            const isFilteredOut = result.__isActuallyFilteredOut;
            const row = tbody.insertRow();
            row.className = 'result-item' + (isFilteredOut ? ' filtered-out-item' : '');
            row.dataset.type = type + (isFilteredOut ? '-filtered-out-actual' : '-passed-actual');
            row.dataset.index = index;

            // Release: title + quality badges
            const titleCell = row.insertCell();
            const title = result.title || result.original_title || 'N/A';
            titleCell.innerHTML = `<div class="release-title${isFilteredOut ? ' filtered-title' : ''}">${title}</div>`;

            // Size
            const sizeCell = row.insertCell();
            sizeCell.className = 'result-size-cell';
            sizeCell.textContent = result.size != null ? result.size.toFixed(2) + ' GB' : '—';
            if (isFilteredOut) sizeCell.style.color = '#666';

            // Source: badge(s)
            const sourceCell = row.insertCell();
            const sourceParts = (result.source || 'N/A').split(' - ');
            sourceCell.innerHTML = `<div class="st-source-container">${sourceParts.map(p => `<span class="st-source-badge">${p.trim()}</span>`).join('')}</div>`;

            // Score / filter reason
            const scoreCell = row.insertCell();
            if (isFilteredOut) {
                scoreCell.textContent = result.filter_reason || 'Filtered';
                scoreCell.className = 'filter-reason-cell';
                scoreCell.title = result.filter_reason || 'Filtered';
            } else {
                const score = result.score_breakdown?.total_score ?? result.score;
                scoreCell.innerHTML = `<span class="st-score">${score != null ? parseFloat(score).toFixed(2) : 'N/A'}</span>`;
            }
        });
        return table;
    }
    
    function displayFilterReason(result) {
        const scoreBreakdown = document.getElementById('score-breakdown');
        scoreBreakdown.innerHTML = '<h3 class="score-breakdown-title">Filter Reason</h3>';
        scoreBreakdown.className = 'settings-section score-breakdown-container';

        const filterReasonDiv = document.createElement('div');
        filterReasonDiv.className = 'filter-reason-display';

        const titleElement = document.createElement('h4');
        titleElement.textContent = result.title || result.original_title || 'Unknown Title';
        titleElement.className = 'filtered-item-title';
        filterReasonDiv.appendChild(titleElement);

        const reasonElement = document.createElement('div');
        reasonElement.className = 'filter-reason-text';
        reasonElement.innerHTML = `<strong>Reason for filtering:</strong> ${result.filter_reason || 'Unknown reason'}`;
        filterReasonDiv.appendChild(reasonElement);

        // Add additional details if available
        const detailsList = document.createElement('ul');
        detailsList.className = 'filter-details-list';

        if (result.source) {
            const sourceItem = document.createElement('li');
            sourceItem.innerHTML = `<strong>Source:</strong> ${result.source}`;
            detailsList.appendChild(sourceItem);
        }

        if (result.size !== undefined) {
            const sizeItem = document.createElement('li');
            sizeItem.innerHTML = `<strong>Size:</strong> ${result.size.toFixed(2)} GB`;
            detailsList.appendChild(sizeItem);
        }

        if (result.parsed_info) {
            const parsedInfo = result.parsed_info;
            if (parsedInfo.resolution) {
                const resolutionItem = document.createElement('li');
                resolutionItem.innerHTML = `<strong>Resolution:</strong> ${parsedInfo.resolution}`;
                detailsList.appendChild(resolutionItem);
            }
            if (parsedInfo.year) {
                const yearItem = document.createElement('li');
                yearItem.innerHTML = `<strong>Year:</strong> ${parsedInfo.year}`;
                detailsList.appendChild(yearItem);
            }
        }

        if (detailsList.children.length > 0) {
            filterReasonDiv.appendChild(detailsList);
        }

        scoreBreakdown.appendChild(filterReasonDiv);
    }

    function displayScoreBreakdown(result) {
        const scoreBreakdown = document.getElementById('score-breakdown');
        scoreBreakdown.innerHTML = '<h3 class="score-breakdown-title">Score Breakdown</h3>';
        scoreBreakdown.className = 'settings-section score-breakdown-container';

        if (result.score_breakdown) {
            const table = document.createElement('table');
            table.className = 'score-breakdown-table';
            const tbody = document.createElement('tbody');

            const keyOrder = [
                // Scalars first — fill complete rows of 3
                'similarity_score', 'resolution_score', 'hdr_score',
                'size_score', 'bitrate_score', 'country_score',
                'language_score', 'year_match_score', 'season_match_score',
                'episode_match_score', 'multi_pack_score', 'single_episode_score',
                'content_type_score', 'language_code_penalty', 'is_multi_pack',
                'num_items', 'scraper_priority_score', 'version_scraper_priority_score',
                // preferred_filter_score sits just above its breakdown
                'preferred_filter_score',
                // Wide items (objects) at the bottom
                'preferred_filter_in_breakdown', 'preferred_filter_out_breakdown',
                'version',
                // Total always last
                'total_score'
            ];

            const isWideEntry = ([key, value]) =>
                (typeof value === 'object' && value !== null && !Array.isArray(value)) || key === 'total_score';

            // tier: 0=scalar, 1=scalar-adjacent (goes just before wides), 2=wide, 3=total
            const getTier = ([key, value]) => {
                if (key === 'total_score') return 3;
                if (isWideEntry([key, value])) return 2;
                if (key === 'preferred_filter_score') return 1;
                return 0;
            };

            const sortedEntries = Object.entries(result.score_breakdown).sort((a, b) => {
                const at = getTier(a), bt = getTier(b);
                if (at !== bt) return at - bt;
                const ai = keyOrder.indexOf(a[0]), bi = keyOrder.indexOf(b[0]);
                if (ai === -1 && bi === -1) return 0;
                if (ai === -1) return 1; if (bi === -1) return -1;
                return ai - bi;
            });

            // Group into rows of 3, wide items get their own full row
            const rows = [];
            let currentRow = [];
            for (const entry of sortedEntries) {
                const [key, value] = entry;
                const isWide = isWideEntry(entry);
                if (isWide) {
                    if (currentRow.length) { rows.push(currentRow); currentRow = []; }
                    rows.push([entry]); // wide row
                } else {
                    currentRow.push(entry);
                    if (currentRow.length === 3) { rows.push(currentRow); currentRow = []; }
                }
            }
            if (currentRow.length) rows.push(currentRow);

            rows.forEach(rowItems => {
                const tr = document.createElement('tr');
                const isWideRow = rowItems.length === 1 && isWideEntry(rowItems[0]);

                if (isWideRow) {
                    const [key, value] = rowItems[0];
                    const td = document.createElement('td');
                    td.colSpan = 3;
                    td.className = 'score-breakdown-item' + (key === 'total_score' ? ' score-breakdown-item--total' : '');
                    if (typeof value === 'object' && !Array.isArray(value)) {
                        const subParts = Object.entries(value)
                            .map(([sk, sv]) => `<span class="score-sub-item"><strong>${sk}:</strong> ${formatValue(sv)}</span>`)
                            .join('');
                        td.innerHTML = `<strong>${key}:</strong><div class="score-sub-list">${subParts}</div>`;
                    } else {
                        td.innerHTML = `<strong>${key}:</strong> ${formatValue(value)}`;
                    }
                    tr.appendChild(td);
                } else {
                    // Fill up to 3 cells
                    for (let i = 0; i < 3; i++) {
                        const td = document.createElement('td');
                        td.className = 'score-breakdown-item';
                        if (rowItems[i]) {
                            const [key, value] = rowItems[i];
                            if (Array.isArray(value)) {
                                td.innerHTML = `<strong>${key}:</strong> ${value.join(', ')}`;
                            } else {
                                td.innerHTML = `<strong>${key}:</strong> ${formatValue(value)}`;
                            }
                        }
                        tr.appendChild(td);
                    }
                }
                tbody.appendChild(tr);
            });

            table.appendChild(tbody);
            scoreBreakdown.appendChild(table);
        } else {
            scoreBreakdown.innerHTML += '<p>No score breakdown available.</p>';
        }

        // Remove any existing click event listeners from result items
        document.querySelectorAll('.result-item').forEach(item => {
            item.removeEventListener('click', item.scoreBreakdownClickHandler);
        });
    }
    
    function formatValue(value) {
        if (typeof value === 'number') {
            return value.toFixed(2);
        } else if (typeof value === 'boolean') {
            return value ? 'Yes' : 'No';
        } else {
            return value;
        }
    }

    function saveVersionSettings() {
        const version = versionSelect.value;
        const settings = getModifiedVersionSettings();
    
        fetch('/settings/save_version_settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ version: version, settings: settings })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showPopup({ type: 'success', title: 'Success', message: 'Settings saved successfully', autoClose: 4000 });
            } else {
                showPopup({ type: 'error', title: 'Error', message: 'Error saving settings: ' + data.error, autoClose: 4000 });
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showPopup({ type: 'error', title: 'Error', message: 'An error occurred while saving settings. Please check the console for more details.', autoClose: 4000 });
        });
    }

    function updateSaveButtonState() {
        const saveButton = document.getElementById('save-modified-version-button');
        const visibleButton = document.getElementById('save-settings-visible-button');
        const originalSettings = JSON.parse(document.getElementById('originalSettings').dataset.settings);
        const modifiedSettings = getModifiedVersionSettings();

        const hasChanges = JSON.stringify(originalSettings) !== JSON.stringify(modifiedSettings);
        saveButton.disabled = !hasChanges;
        if (visibleButton) visibleButton.style.display = hasChanges ? 'inline-block' : 'none';
    }

    function revertSettings() {
        return fetch('/save_version_settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                version: currentVersion,
                settings: originalVersionSettings
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                return;
            } else {
                throw new Error(data.error || 'Failed to revert settings');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showPopup({ type: 'error', title: 'Error', message: `Failed to revert settings: ${error.message}`, autoClose: 4000 });
        });
    }

    function showScrapeSection() {
        searchResults.style.display = 'none';
        scrapeSection.style.display = 'block';
    }

    function startNewSearch() {
        scrapeSection.style.display = 'none';
        searchResults.style.display = 'block';
    }

    Loading.init()

    // Call this function whenever a setting is changed
document.getElementById('modifiedSettings').addEventListener('input', updateSaveButtonState);
document.getElementById('modifiedSettings').addEventListener('change', updateSaveButtonState);
});
