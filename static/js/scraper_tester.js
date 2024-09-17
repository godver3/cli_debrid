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

    searchButton.addEventListener('click', performSearch);
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
                alert('Settings saved successfully');
            } else {
                alert('Error saving settings: ' + data.error);
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert('An error occurred while saving settings. Please check the console for more details.');
        });
    }

    function performSearch() {
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
        .then(data => displaySearchResults(data))
        .catch(error => {
            console.error('Error:', error);
            searchResults.innerHTML = '<p>Error performing search. Please try again.</p>';
        });
    }

    function displaySearchResults(results) {
        console.log('Search results:', results);  // Debug log
        const searchResultsElement = document.getElementById('search-results');
        searchResultsElement.innerHTML = '';
        
        const table = document.createElement('table');
        table.className = 'search-results-table';
        
        // Create table header
        const headerRow = table.insertRow();
        ['Title', 'Year', 'Type', 'IMDB ID'].forEach(headerText => {
            const th = document.createElement('th');
            th.textContent = headerText;
            headerRow.appendChild(th);
        });
        
        // Process results and convert TMDB IDs to IMDB IDs if necessary
        const processedResults = results.map(result => {
            if (!result.imdbId || result.imdbId === 'N/A') {
                return fetch(`/scraper/convert_tmdb_to_imdb/${result.id}`)
                    .then(response => response.json())
                    .then(data => {
                        result.imdbId = data.imdb_id;
                        return result;
                    })
                    .catch(error => {
                        console.error('Error converting TMDB ID to IMDB ID:', error);
                        return result;
                    });
            }
            return Promise.resolve(result);
        });
    
        Promise.all(processedResults).then(validResults => {
            validResults = validResults.filter(result => result.imdbId && result.imdbId !== 'N/A');
            
            console.log(`Valid results: ${validResults.length} out of ${results.length}`);
            
            if (validResults.length === 0) {
                searchResultsElement.innerHTML = `
                    <p>No results found with valid IMDB IDs.</p>
                    <p>Total results received: ${results.length}</p>
                    <p>Check the console for more details on filtered results.</p>
                `;
                return;
            }
            
            validResults.forEach(result => {
                console.log('Processing result:', result);  // Debug log
                const row = table.insertRow();
                row.className = 'search-result';
                
                const title = result.title || 'N/A';
                const year = result.year || 'N/A';
                const mediaType = result.mediaType === 'tv' ? 'TV Show' : 'Movie';
                const imdbId = result.imdbId || 'N/A';
                
                console.log(`Title: ${title}, Year: ${year}, Type: ${mediaType}, IMDB ID: ${imdbId}`);  // Debug log
                
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
            
            console.log(`Displayed ${validResults.length} results with valid IMDB IDs`);  // Debug log
        });
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
            const mediaType = item.mediaType === 'tv' ? 'TV Show' : 'Movie';
            const imdbId = item.imdbId || 'N/A';
            
            // Store the year in the currentItem object
            currentItem.year = year !== 'N/A' ? parseInt(year) : null;
            
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
            tvControls.style.display = item.mediaType === 'tv' ? 'block' : 'none';
            if (item.mediaType === 'tv') {
                populateSeasonEpisodeSelects(item);
            }
        }
    
        // Load versions for the selected item
        loadVersions();
    }

    function populateSeasonEpisodeSelects(item) {
        const seasonSelect = document.getElementById('season-select');
        const episodeSelect = document.getElementById('episode-select');
    
        // Clear existing options
        seasonSelect.innerHTML = '';
        episodeSelect.innerHTML = '';
    
        if (item.seasonEpisodeCounts) {
            // Populate seasons
            Object.keys(item.seasonEpisodeCounts).forEach(season => {
                const option = document.createElement('option');
                option.value = season;
                option.textContent = `Season ${season}`;
                seasonSelect.appendChild(option);
            });
    
            // Add event listener to season select to update episodes
            seasonSelect.addEventListener('change', () => updateEpisodeSelect(item, seasonSelect.value));
    
            // Initially populate episodes for the first season
            updateEpisodeSelect(item, Object.keys(item.seasonEpisodeCounts)[0]);
        }
    }

    function updateEpisodeSelect(item, selectedSeason) {
        const episodeSelect = document.getElementById('episode-select');
        episodeSelect.innerHTML = '';
    
        if (item.seasonEpisodeCounts && item.seasonEpisodeCounts[selectedSeason]) {
            const episodeCount = item.seasonEpisodeCounts[selectedSeason];
            for (let i = 1; i <= episodeCount; i++) {
                const option = document.createElement('option');
                option.value = i;
                option.textContent = `Episode ${i}`;
                episodeSelect.appendChild(option);
            }
        }
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
    
        for (const [key, value] of Object.entries(settings)) {
            if (key !== 'display_name') {
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
        }
    
        // Add the save button
        const saveButton = document.createElement('button');
        saveButton.id = 'save-modified-version-button';
        saveButton.textContent = 'Save Modified Version';
        saveButton.onclick = saveVersionSettings;
        modifiedSettingsContainer.appendChild(saveButton);
    
        // Store the original settings for comparison
        originalSettingsContainer.dataset.settings = JSON.stringify(settings);
    
        // Initialize the save button state
        updateSaveButtonState();
    }
    
    function createInputElements(key, value) {
        let originalInput, modifiedInput;
    
        if (key === 'enable_hdr') {
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
            // Ensure the correct option is selected for both inputs
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
            // Ensure the correct option is selected for both inputs
            originalInput.value = value;
            modifiedInput.value = value;
        } else if (['filter_in', 'filter_out'].includes(key)) {
            originalInput = createFilterList(key, value, true);
            modifiedInput = createFilterList(key, value, false);
        } else if (['preferred_filter_in', 'preferred_filter_out'].includes(key)) {
            originalInput = createPreferredFilterList(key, value, true);
            modifiedInput = createPreferredFilterList(key, value, false);
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
    
        const input = document.createElement('input');
        input.type = 'text';
        input.value = value;
        input.className = isOriginal ? 'filter-input original-input' : 'filter-input';
        input.disabled = isOriginal;
    
        const removeButton = document.createElement('button');
        removeButton.textContent = 'Remove';
        removeButton.className = isOriginal ? 'remove-filter-item original-input' : 'remove-filter-item';
        removeButton.disabled = isOriginal;
        if (!isOriginal) {
            removeButton.onclick = () => itemContainer.remove();
        }
    
        itemContainer.appendChild(input);
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

    function runScrape() {
        const version = document.getElementById('version-select').value;
        const modifiedSettings = getModifiedVersionSettings();
    
        const scrapeData = {
            imdb_id: document.getElementById('imdbId').value || '',
            tmdb_id: currentItem.id,
            title: currentItem.title || currentItem.name,
            year: currentItem.year,
            movie_or_episode: currentItem.mediaType === 'tv' ? 'episode' : 'movie',
            version: version,
            modifiedSettings: modifiedSettings
        };
    
        // Add TV show specific information
        if (currentItem.mediaType === 'tv') {
            scrapeData.season = document.getElementById('season-select').value;
            scrapeData.episode = document.getElementById('episode-select').value;
            scrapeData.multi = document.getElementById('multi-checkbox').checked;
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
    
            if (!data || (!data.originalResults && !data.adjustedResults)) {
                console.error('Invalid response structure:', data);
                throw new Error('Invalid response structure');
            }
    
            const originalResults = data.originalResults || [];
            const adjustedResults = data.adjustedResults || [];
            
            console.log('Original results:', originalResults);
            console.log('Adjusted results:', adjustedResults);
    
            displayScrapeResults({originalResults, adjustedResults});
        })
        .catch(error => {
            console.error('Error:', error);
            // Display an error message to the user
            document.getElementById('scrape-results').innerHTML = '<p>An error occurred while fetching results. Please try again.</p>';
        });
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
                settings[settingKey] = Array.from(input.querySelectorAll('.filter-input')).map(item => item.value).filter(Boolean);
            } else if (settingKey === 'preferred_filter_in' || settingKey === 'preferred_filter_out') {
                settings[settingKey] = Array.from(input.querySelectorAll('.preferred-filter-item')).map(item => {
                    const term = item.querySelector('.filter-input').value;
                    const weight = parseInt(item.querySelector('.filter-weight').value);
                    return term && !isNaN(weight) ? [term, weight] : null;
                }).filter(Boolean);
            } else {
                settings[settingKey] = input.value;
            }
        });
        return settings;
    }

    function displayScrapeResults(data) {
        originalResults.innerHTML = '<h3>Original Results</h3>';
        adjustedResults.innerHTML = '<h3>Adjusted Results</h3>';
        
        if (data.originalResults && data.originalResults.length > 0) {
            originalResults.appendChild(createResultsTable(data.originalResults));
        } else {
            originalResults.innerHTML += '<p>No original results found</p>';
        }
    
        if (data.adjustedResults && data.adjustedResults.length > 0) {
            adjustedResults.appendChild(createResultsTable(data.adjustedResults));
        } else {
            adjustedResults.innerHTML += '<p>No adjusted results found</p>';
        }
    
        scrapeResults.scrollIntoView({ behavior: 'smooth', block: 'start' });
    
        // Add click event listeners to result items
        document.querySelectorAll('.result-item').forEach(item => {
            item.addEventListener('click', function() {
                const resultIndex = Array.from(this.parentNode.children).indexOf(this);
                const result = this.parentNode.id === 'original-results' 
                    ? data.originalResults[resultIndex] 
                    : data.adjustedResults[resultIndex];
                displayScoreBreakdown(result);
            });
        });
    }
    
    function createResultsTable(results) {
        const table = document.createElement('table');
        table.className = 'settings-table';
        table.innerHTML = `
            <tr>
                <th>Title</th>
                <th>Score</th>
            </tr>
        `;
        results.forEach((result, index) => {
            const row = table.insertRow();
            row.className = 'result-item';
            row.innerHTML = `
                <td>${result.title || 'N/A'}</td>
                <td>${result.score_breakdown ? result.score_breakdown.total_score.toFixed(2) : 'N/A'}</td>
            `;
        });
        return table;
    }
    
    function displayScoreBreakdown(result) {
        const scoreBreakdown = document.getElementById('score-breakdown');
        scoreBreakdown.innerHTML = '<h3 class="score-breakdown-title">Score Breakdown</h3>';
        scoreBreakdown.className = 'settings-section score-breakdown-container';

        if (result.score_breakdown) {
            const breakdownList = document.createElement('ul');
            breakdownList.className = 'score-breakdown-list';
    
            for (const [key, value] of Object.entries(result.score_breakdown)) {
                const breakdownItem = document.createElement('li');
                breakdownItem.className = 'score-breakdown-item';
    
                if (typeof value === 'object' && value !== null) {
                    if (Array.isArray(value)) {
                        breakdownItem.innerHTML = `<strong>${key}:</strong> ${value.join(', ')}`;
                    } else {
                        breakdownItem.innerHTML = `<strong>${key}:</strong>`;
                        const subList = document.createElement('ul');
                        for (const [subKey, subValue] of Object.entries(value)) {
                            const subItem = document.createElement('li');
                            subItem.className = 'score-breakdown-subitem';
                            subItem.innerHTML = `<strong>${subKey}:</strong> ${formatValue(subValue)}`;
                            subList.appendChild(subItem);
                        }
                        breakdownItem.appendChild(subList);
                    }
                } else {
                    breakdownItem.innerHTML = `<strong>${key}:</strong> ${formatValue(value)}`;
                }
    
                breakdownList.appendChild(breakdownItem);
            }
    
            scoreBreakdown.appendChild(breakdownList);
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
                alert('Settings saved successfully');
            } else {
                alert('Error saving settings: ' + data.error);
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert('An error occurred while saving settings. Please check the console for more details.');
        });
    }

    function updateSaveButtonState() {
        const saveButton = document.getElementById('save-modified-version-button');
        const originalSettings = JSON.parse(document.getElementById('originalSettings').dataset.settings);
        const modifiedSettings = getModifiedVersionSettings();
        
        const hasChanges = JSON.stringify(originalSettings) !== JSON.stringify(modifiedSettings);
        saveButton.disabled = !hasChanges;
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
            alert(`Failed to revert settings: ${error.message}`);
        });
    }

    function showScrapeSection() {
        searchSection.style.display = 'none';
        scrapeSection.style.display = 'block';
    }

    function startNewSearch() {
        searchInput.value = '';
        searchResults.innerHTML = '';
        scrapeSection.style.display = 'none';
        searchSection.style.display = 'block';
    }

    // Call this function whenever a setting is changed
document.getElementById('modifiedSettings').addEventListener('input', updateSaveButtonState);
document.getElementById('modifiedSettings').addEventListener('change', updateSaveButtonState);
});

