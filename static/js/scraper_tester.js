document.addEventListener('DOMContentLoaded', function() {
    const searchInput = document.getElementById('search-input');
    const searchButton = document.getElementById('search-button');
    const searchResults = document.getElementById('search-results');
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
    saveSettingsButton.addEventListener('click', saveModifiedSettings);

    function performSearch() {
        const searchTerm = searchInput.value;
        fetch('/scraper_tester', {
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
        searchResults.innerHTML = '';
        results.forEach(result => {
            const resultItem = document.createElement('div');
            resultItem.classList.add('result-item');
            const title = result.title || result.name; // Use name as fallback for TV shows
            const year = result.releaseDate ? result.releaseDate.substring(0, 4) : 
                         result.firstAirDate ? result.firstAirDate.substring(0, 4) : 'N/A';
            resultItem.textContent = `${title} (${year}) - ${result.mediaType}`;
            resultItem.addEventListener('click', () => {
                selectItem(result);
                document.getElementById('scrape-details').scrollIntoView({ behavior: 'smooth' });
            });
            searchResults.appendChild(resultItem);
        });
    }

    function selectItem(item) {
        currentItem = item;
        const title = item.title || item.name; // Use name as fallback for TV shows
        const year = item.releaseDate ? item.releaseDate.substring(0, 4) : 
                     item.firstAirDate ? item.firstAirDate.substring(0, 4) : 'N/A';
        selectedItem.innerHTML = `
            <h3>${title} (${year})</h3>
            <p>Type: ${item.mediaType}</p>
            <p>IMDB ID: ${item.externalIds?.imdbId || 'N/A'}</p>
        `;
        loadVersions();
    }

    function loadVersions() {
        fetch('/get_version_settings')
            .then(response => response.json())
            .then(data => {
                versionSelect.innerHTML = '';
                Object.keys(data).forEach(version => {
                    const option = document.createElement('option');
                    option.value = version;
                    option.textContent = version;
                    versionSelect.appendChild(option);
                });
                versionSelect.addEventListener('change', (e) => loadVersionSettings(e.target.value));
                loadVersionSettings(versionSelect.value);
            })
            .catch(error => {
                console.error('Error loading versions:', error);
                versionSelect.innerHTML = '<option>Error loading versions</option>';
            });
    }

    function loadVersionSettings(version) {
        currentVersion = version;
        fetch('/get_version_settings')
            .then(response => response.json())
            .then(data => {
                originalVersionSettings = data[version];
                modifiedVersionSettings = JSON.parse(JSON.stringify(originalVersionSettings));
                displayVersionSettings();
            })
            .catch(error => {
                console.error('Error loading version settings:', error);
                versionSettings.innerHTML = '<p>Error loading version settings</p>';
            });
    }

    function displayVersionSettings() {
        versionSettings.innerHTML = '';
        Object.entries(modifiedVersionSettings).forEach(([key, value]) => {
            const settingItem = document.createElement('div');
            settingItem.className = 'settings-form-group';
            
            const label = document.createElement('label');
            label.textContent = key;
            label.className = 'settings-title';
            
            const input = document.createElement('input');
            input.className = 'settings-input';
            input.name = key;
            
            if (typeof value === 'boolean') {
                input.type = 'checkbox';
                input.checked = value;
            } else {
                input.type = 'text';
                input.value = value;
            }
            
            input.addEventListener('change', (e) => {
                modifiedVersionSettings[key] = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
                saveSettingsButton.style.display = 'block';
            });
            
            settingItem.appendChild(label);
            settingItem.appendChild(input);
            versionSettings.appendChild(settingItem);
        });
    }

    function runScrape() {
        if (!currentItem) return;

        const scrapeData = {
            imdb_id: currentItem.externalIds?.imdbId || '',
            tmdb_id: currentItem.id,
            title: currentItem.title || currentItem.name, // Use name as fallback for TV shows
            year: currentItem.releaseDate ? currentItem.releaseDate.substring(0, 4) : 
                  currentItem.firstAirDate ? currentItem.firstAirDate.substring(0, 4) : '',
            movie_or_episode: currentItem.mediaType === 'movie' ? 'movie' : 'episode',
            version: currentVersion,
        };

        if (currentItem.mediaType === 'tv') {
            scrapeData.season = currentItem.seasonNumber;
            scrapeData.episode = currentItem.episodeNumber;
            scrapeData.multi = currentItem.seasonNumber && !currentItem.episodeNumber;
        }

        console.log("Sending scrape data:", scrapeData);  // Keep this line for debugging

        saveModifiedSettings()
            .then(() => {
                return fetch('/run_scrape', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(scrapeData)
                });
            })
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    throw new Error(data.error);
                }
                displayResults(data);
                return revertSettings();
            })
            .then(() => {
                document.getElementById('save-settings-button').style.display = 'none';
            })
            .catch(error => {
                console.error('Error:', error);
                scrapeResults.innerHTML = `<p>Error: ${error.message || 'No results found'}</p>`;
            });
    }

    function displayResults(data) {
        originalResults.innerHTML = '<h3>Original Results</h3>';
        adjustedResults.innerHTML = '<h3>Adjusted Results</h3>';
        
        if (data.originalResults && data.originalResults.length > 0) {
            originalResults.appendChild(createResultsTable(data.originalResults));
        } else {
            originalResults.innerHTML += '<p>No original results found</p>';
        }

        if (data.modifiedResults && data.modifiedResults.length > 0) {
            adjustedResults.appendChild(createResultsTable(data.modifiedResults));
        } else {
            adjustedResults.innerHTML += '<p>No adjusted results found</p>';
        }

        scrapeResults.scrollIntoView({ behavior: 'smooth', block: 'start' });
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
            row.innerHTML = `
                <td>${result.title || 'N/A'}</td>
                <td>${result.score_breakdown ? result.score_breakdown.total_score.toFixed(2) : 'N/A'}</td>
            `;
            row.addEventListener('click', () => showScoreBreakdown(result));
        });
        return table;
    }

    function showScoreBreakdown(result) {
        const breakdownContent = scoreBreakdown.querySelector('.settings-section-content');
        breakdownContent.innerHTML = '';
        if (result.score_breakdown) {
            Object.entries(result.score_breakdown).forEach(([key, value]) => {
                const breakdownItem = document.createElement('div');
                breakdownItem.className = 'settings-form-group';
                breakdownItem.innerHTML = `
                    <span class="settings-title">${key}:</span>
                    <span class="settings-value">${typeof value === 'number' ? value.toFixed(2) : value}</span>
                `;
                breakdownContent.appendChild(breakdownItem);
            });
        } else {
            breakdownContent.innerHTML = '<p>No score breakdown available</p>';
        }
        scoreBreakdown.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function saveModifiedSettings() {
        return fetch('/save_version_settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                version: currentVersion,
                settings: modifiedVersionSettings
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                alert('Settings saved successfully');
                originalVersionSettings = JSON.parse(JSON.stringify(modifiedVersionSettings));
                saveSettingsButton.style.display = 'none';
            } else {
                throw new Error(data.error || 'Failed to save settings');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert(`Failed to save settings: ${error.message}`);
        });
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
});