function addToRealDebrid(magnetLink, torrent) {
    // Check if user is a requester before making the request
    const isRequesterEl = document.getElementById('is_requester');
    if (isRequesterEl && isRequesterEl.value === 'True') {
        // Silently return without showing an error for requesters
        return;
    }

    showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Confirm Action',
        message: 'Are you sure you want to add this torrent to your Debrid Provider?',
        confirmText: 'Add',
        cancelText: 'Cancel',
        onConfirm: () => {
            showLoadingState();

            const formData = new FormData();
            formData.append('magnet_link', magnetLink);
            formData.append('title', torrent.title);
            formData.append('year', torrent.year);
            formData.append('media_type', torrent.media_type);
            formData.append('season', torrent.season || '');
            formData.append('episode', torrent.episode || '');
            formData.append('version', torrent.version || '');
            formData.append('tmdb_id', torrent.tmdb_id || '');
            formData.append('genres', torrent.genres || '');

            fetch('/scraper/add_to_debrid', {
                method: 'POST',
                body: formData
            })
            .then(response => {
                if (response.status === 403) {
                    hideLoadingState();
                    return { abort: true };  // Signal to not continue processing, but don't show error
                }
                
                if (!response.ok) {
                    return response.json().then(errorData => {
                        throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
                    });
                }
                return response.json();
            })
            .then(data => {
                // Skip further processing if aborted
                if (data && data.abort) return;
                
                hideLoadingState();

                if (data.error) {
                    throw new Error(data.error);
                } else {
                    // Check if the item is uncached
                    if (data.cache_status && data.cache_status.is_cached === false) {
                        // Show prompt for uncached item
                        showPopup({
                            type: POPUP_TYPES.CONFIRM,
                            title: 'Uncached Item',
                            message: data.message + ' (Uncached item will be kept)',
                            confirmText: 'Keep',
                            cancelText: 'Remove',
                            onConfirm: () => {
                                // User chose to keep the uncached item
                                showPopup({
                                    type: POPUP_TYPES.SUCCESS,
                                    title: 'Success',
                                    message: data.message + ' (Uncached item will be kept)',
                                    autoClose: 15000  // 15 seconds
                                });
                            },
                            onCancel: () => {
                                // User chose to remove the uncached item
                                removeUncachedItem(data.cache_status.torrent_id, data.cache_status.torrent_hash);
                            }
                        });
                    } else {
                        // Regular success message for cached items
                        showPopup({
                            type: POPUP_TYPES.SUCCESS,
                            title: 'Success',
                            message: data.message,
                            autoClose: 15000  // 15 seconds
                        });
                    }
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error',
                    message: `Error adding to Real-Debrid: ${error.message}`,
                });
            })
        },
    });
}

// Function to remove an uncached item
function removeUncachedItem(torrentId, torrentHash) {
    showLoadingState();
    
    const formData = new FormData();
    formData.append('torrent_id', torrentId || '');
    formData.append('torrent_hash', torrentHash || '');
    
    fetch('/scraper/remove_uncached_item', {
        method: 'POST',
        body: formData
    })
    .then(response => {
        if (!response.ok) {
            return response.json().then(errorData => {
                throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
            });
        }
        return response.json();
    })
    .then(data => {
        hideLoadingState();
        
        if (data.error) {
            throw new Error(data.error);
        } else {
            showPopup({
                type: POPUP_TYPES.SUCCESS,
                title: 'Success',
                message: 'Uncached item has been removed',
                autoClose: 5000
            });
        }
    })
    .catch(error => {
        hideLoadingState();
        console.error('Error:', error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            title: 'Error',
            message: `Error removing uncached item: ${error.message}`,
        });
    });
}

function displayError(message) {
    showPopup({
        type: POPUP_TYPES.ERROR,
        title: 'Error',
        message: message
    });
}

function displaySuccess(message) {
    showPopup({
        type: POPUP_TYPES.SUCCESS,
        title: 'Success',
        message: message
    });
}

function showLoadingState() {
    Loading.show();
    
    // Disable all buttons
    const buttons = document.getElementsByTagName('button');
    for (let button of buttons) {
        button.disabled = true;
        button.style.opacity = '0.5';
    }
    
    const selecter = document.getElementsByTagName('select');
    for (let select of selecter) {
        select.disabled = true;
        select.style.opacity = '0.5';
    }

    const episodeDiv = document.getElementsByClassName('episode');
    for (let episode of episodeDiv) {
        episode.style.pointerEvents = 'none';
        episode.style.opacity = '0.5';
    }
}

// Function to hide loading state and re-enable buttons
function hideLoadingState() {
    Loading.hide();

    // Re-enable all buttons
    const buttons = document.getElementsByTagName('button');
    for (let button of buttons) {
        button.disabled = false;
        button.style.opacity = '1';
    }
    
    const selecter = document.getElementsByTagName('select');
    for (let select of selecter) {
        select.disabled = false;
        select.style.opacity = '1';
    }

    const episodeDiv = document.getElementsByClassName('episode');
    for (let episode of episodeDiv) {
        episode.style.pointerEvents = 'auto';
        episode.style.opacity = '1';
    }
}

function displayEpisodeResults(episodeResults, title, year, version, mediaId, mediaType, season, episode, genre_ids) {
    if (!episodeResults) {
        displayError('No episode results found');
        return;
    }
    
    // Get requester status
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';
    
    toggleResultsVisibility('displayEpisodeResults');
    const episodeResultsDiv = document.getElementById('episodeResults');
    episodeResultsDiv.innerHTML = '';
    
    // Create a container for the grid layout
    const gridContainer = document.createElement('div');
    gridContainer.style.display = 'flex';
    gridContainer.style.flexWrap = 'wrap';
    gridContainer.style.gap = '20px';
    gridContainer.style.justifyContent = 'center';

    episodeResults.forEach(item => {
        const episodeDiv = document.createElement('div');
        episodeDiv.className = 'episode';
        var options = {year: 'numeric', month: 'long', day: 'numeric' };
        var date = item.air_date ? new Date(item.air_date) : null;
        episodeDiv.innerHTML = `        
            <button ${isRequester ? 'disabled' : ''}><span class="episode-rating">${(item.vote_average || 0).toFixed(1)}</span>
            <img src="${item.still_path ? `/scraper/tmdb_image/w300${item.still_path}` : '/static/image/placeholder-horizontal.png'}" 
                alt="${item.episode_title || ''}" 
                class="${item.still_path ? '' : 'placeholder-episode'}">
            <div class="episode-info">
                <h2 class="episode-title">${item.episode_num}. ${item.episode_title || ''}</h2>
                <p class="episode-sub">${date ? date.toLocaleDateString("en-US", options) : 'Air date unknown'}</p>
            </div></button>
        `;
        
        // Only add click handler for non-requester users
        if (!isRequester) {
            episodeDiv.onclick = function() {
                selectMedia(item.id, item.title, item.year, item.media_type, item.season_num, item.episode_num, item.multi, genre_ids);
            };
        } else {
            // Apply visual styling to show it's not clickable for requesters
            episodeDiv.style.cursor = 'default';
            episodeDiv.style.opacity = '0.8';
        }
        
        gridContainer.appendChild(episodeDiv);
    });

    episodeResultsDiv.appendChild(gridContainer);
}

function toggleResultsVisibility(section) {
    const trendingContainer = document.getElementById('trendingContainer');
    const searchResult = document.getElementById('searchResult');
    const searchResults = document.getElementById('searchResults');
    const seasonResults = document.getElementById('seasonResults');
    const dropdown = document.getElementById('seasonDropdown');
    const seasonPackButton = document.getElementById('seasonPackButton');
    const episodeResultsDiv = document.getElementById('episodeResults');
    
    // Check if user is a requester
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';
    
    if (section === 'displayEpisodeResults') {
        trendingContainer.style.display = 'none';
        searchResult.style.display = 'none';
        searchResults.style.display = 'none';
        seasonResults.style.display = 'block';
        dropdown.style.display = 'block';
        // Only show season pack button for non-requester users
        seasonPackButton.style.display = isRequester ? 'none' : 'block';
        episodeResultsDiv.style.display = 'block';
    }
    if (section === 'displaySearchResults') {
        trendingContainer.style.display = 'none';
        searchResult.style.display = 'none';
        searchResults.style.display = 'block';
        seasonResults.style.display = 'none';
        episodeResultsDiv.style.display = 'none';
    }
    if (section === 'get_trendingMovies') {
        trendingContainer.style.display = 'block';
        searchResult.style.display = 'none';
        searchResults.style.display = 'none';
        seasonResults.style.display = 'none';
        episodeResultsDiv.style.display = 'none';
    }
}

function displayTorrentResults(data, title, year, version, mediaId, mediaType, season, episode, genre_ids) {
    hideLoadingState();
    const overlay = document.getElementById('overlay');

    const mediaQuery = window.matchMedia('(max-width: 1024px)');
    function handleScreenChange(e) {
        if (e.matches) {
            const overlayContentRes = document.getElementById('overlayContent');
            overlayContentRes.innerHTML = `<h3>Torrent Results for ${title} (${year})</h3>`;
            const gridContainer = document.createElement('div');
            gridContainer.style.display = 'flex';
            gridContainer.style.flexWrap = 'wrap';
            gridContainer.style.gap = '15px';
            gridContainer.style.justifyContent = 'center';

            data.forEach((torrent, index) => {
                const torResDiv = document.createElement('div');
                torResDiv.className = 'torresult';
                var options = {year: 'numeric', month: 'long', day: 'numeric' };
                var date = torrent.air_date ? new Date(torrent.air_date) : null;
                
                // Prepare the torrent data with both magnet_link and torrent_url for cache checking
                if (torrent.magnet) {
                    torrent.magnet_link = torrent.magnet;
                }
                
                torResDiv.innerHTML = `
                    <button>
                    <div class="torresult-info">
                        <p class="torresult-title">${torrent.title}</p>
                        <p class="torresult-item">${(torrent.size).toFixed(1)} GB | ${torrent.score_breakdown.total_score}</p>
                        <p class="torresult-item">${torrent.source}</p>
                        <span class="cache-status ${torrent.cached === 'Yes' ? 'cached' : 
                                      torrent.cached === 'No' ? 'not-cached' : 
                                      torrent.cached === 'Not Checked' ? 'not-checked' :
                                      torrent.cached === 'N/A' ? 'check-unavailable' : 'unknown'}" data-index="${index}">${torrent.cached}</span>
                    </div>
                    </button>             
                `;
                torResDiv.onclick = function() {
                    // Add metadata to torrent object
                    const torrentData = {
                        title: title,
                        year: year,
                        version: version,
                        media_type: mediaType,
                        season: season || null,
                        episode: episode || null,
                        tmdb_id: mediaId,
                        genres: genre_ids
                    };
                    addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                };
                gridContainer.appendChild(torResDiv);
            });

            overlayContentRes.appendChild(gridContainer);
        } else {
            const overlayContent = document.getElementById('overlayContent');
            overlayContent.innerHTML = `<h3>Torrent Results for ${title} (${year})</h3>`;
            // Create table element
            const table = document.createElement('table');
            table.style.width = '100%';
            table.style.borderCollapse = 'collapse';

            // Create table header
            const thead = document.createElement('thead');
            thead.innerHTML = `
                <tr>
                    <th style="color: rgb(191 191 190); width: 80%;">Name</th>
                    <th style="color: rgb(191 191 190); width: 10%;">Size</th>
                    <th style="color: rgb(191 191 190); width: 15%;">Source</th>
                    <th style="color: rgb(191 191 190); width: 10%;">Score</th>
                    <th style="color: rgb(191 191 190); width: 15%; text-align: center;">Cache</th>
                    <th style="color: rgb(191 191 190); width: 15%; text-align: center;">Action</th>
                </tr>
            `;
            table.appendChild(thead);

            // Create table body
            const tbody = document.createElement('tbody');
            data.forEach((torrent, index) => {
                const cacheStatus = torrent.cached || 'Unknown';
                const cacheStatusClass = cacheStatus === 'Yes' ? 'cached' : 
                                      cacheStatus === 'No' ? 'not-cached' : 
                                      cacheStatus === 'Not Checked' ? 'not-checked' :
                                      cacheStatus === 'N/A' ? 'check-unavailable' : 'unknown';
                
                // Prepare the torrent data with both magnet_link and torrent_url for cache checking
                if (torrent.magnet) {
                    torrent.magnet_link = torrent.magnet;
                }

                const row = document.createElement('tr');
                row.innerHTML = `
                    <td style="font-weight: 600; text-transform: uppercase; color: rgb(191 191 190); max-width: 80%; word-wrap: break-word; white-space: normal; padding: 10px;">
                        <div style="display: block; line-height: 1.4; min-height: fit-content;">
                            ${torrent.title}
                        </div>
                    </td>
                    <td style="color: rgb(191 191 190);">${(torrent.size).toFixed(1)} GB</td>
                    <td style="color: rgb(191 191 190);">${torrent.source}</td>
                    <td style="color: rgb(191 191 190);">${torrent.score_breakdown.total_score}</td>
                    <td style="color: rgb(191 191 190); text-align: center;">
                        <span class="cache-status ${cacheStatusClass}" data-index="${index}">${cacheStatus}</span>
                    </td>
                    <td style="color: rgb(191 191 190); text-align: center;">
                        <button onclick="addToRealDebrid('${torrent.magnet}', ${JSON.stringify({
                            ...torrent,
                            year,
                            version: torrent.version || version,
                            title,
                            media_type: mediaType,
                            season: season || null,
                            episode: episode || null,
                            tmdb_id: torrent.tmdb_id || mediaId,
                            genres: genre_ids
                        }).replace(/"/g, '&quot;')})">Add</button>
                    </td>
                `;
                tbody.appendChild(row);
            });
            table.appendChild(tbody);

            overlayContent.appendChild(table);
        }
    }
    mediaQuery.addListener(handleScreenChange);
    handleScreenChange(mediaQuery);

    overlay.style.display = 'block';
    
    // Prepare data for cache check - now using the full results object
    // Check cache status in the background for first 5 items
    checkCacheStatusInBackground(null, data);
}

// Add event listeners when DOM content is loaded
document.addEventListener('DOMContentLoaded', function() {
    // Set up search form behavior 
    const searchForm = document.getElementById('search-form');
    if (searchForm) {
        searchForm.addEventListener('submit', function(event) {
            searchMedia(event);
        });
    
        // Bind the button click as well
        const searchButton = document.getElementById('searchformButton');
        if (searchButton) {
            searchButton.addEventListener('click', function(event) {
                searchMedia(event);
            });
        }
    }
    
    // Set up version modal buttons
    const confirmVersionsButton = document.getElementById('confirmVersions');
    if (confirmVersionsButton) {
        confirmVersionsButton.addEventListener('click', handleVersionConfirm);
    }
    
    const cancelVersionsButton = document.getElementById('cancelVersions');
    if (cancelVersionsButton) {
        cancelVersionsButton.addEventListener('click', closeVersionModal);
    }
    
    // Close the overlay when the close button is clicked
    const closeButton = document.querySelector('.close-btn');
    if (closeButton) {
        closeButton.onclick = function() {
            document.getElementById('overlay').style.display = 'none';
        };
    }
    
    // Initialize the Loading object
    Loading.init();
    
    // Setup scroll functionality for movie container
    const container_mv = document.getElementById('movieContainer');
    const scrollLeftBtn_mv = document.getElementById('scrollLeft_mv');
    const scrollRightBtn_mv = document.getElementById('scrollRight_mv');
    
    // Initialize button states
    if (scrollLeftBtn_mv) {
        scrollLeftBtn_mv.disabled = container_mv.scrollLeft === 0;
    }
    
    function updateButtonStates_mv() {
        if (container_mv) {
            scrollLeftBtn_mv.disabled = container_mv.scrollLeft === 0;
            scrollRightBtn_mv.disabled = container_mv.scrollLeft >= container_mv.scrollWidth - container_mv.offsetWidth;
        }
    }
    
    function scroll_mv(direction) {
        if (container_mv) {
            const scrollAmount = container_mv.offsetWidth;
            const newPosition = direction === 'left'
                ? Math.max(container_mv.scrollLeft - scrollAmount, 0)
                : Math.min(container_mv.scrollLeft + scrollAmount, container_mv.scrollWidth - container_mv.offsetWidth);
            
            container_mv.scrollTo({ left: newPosition, behavior: 'smooth' });
        }
    }
    
    if (container_mv) {
        container_mv.addEventListener('scroll', updateButtonStates_mv);
    }
    
    // Setup scroll functionality for TV shows container
    const container_tv = document.getElementById('showContainer');
    const scrollLeftBtn_tv = document.getElementById('scrollLeft_tv');
    const scrollRightBtn_tv = document.getElementById('scrollRight_tv');
    
    // Initialize button states
    if (scrollLeftBtn_tv) {
        scrollLeftBtn_tv.disabled = container_tv.scrollLeft === 0;
    }
    
    function updateButtonStates_tv() {
        if (container_tv) {
            scrollLeftBtn_tv.disabled = container_tv.scrollLeft === 0;
            scrollRightBtn_tv.disabled = container_tv.scrollLeft >= container_tv.scrollWidth - container_tv.offsetWidth;
        }
    }
    
    function scroll_tv(direction) {
        if (container_tv) {
            const scrollAmount = container_tv.offsetWidth;
            const newPosition = direction === 'left'
                ? Math.max(container_tv.scrollLeft - scrollAmount, 0)
                : Math.min(container_tv.scrollLeft + scrollAmount, container_tv.scrollWidth - container_tv.offsetWidth);
            
            container_tv.scrollTo({ left: newPosition, behavior: 'smooth' });
        }
    }
    
    if (container_tv) {
        container_tv.addEventListener('scroll', updateButtonStates_tv);
    }
    
    // Check if Trakt is authorized before attempting to load trending content
    fetch('/trakt/trakt_auth_status', { method: 'GET' })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! Status: ${response.status}`);
            }
            return response.json();
        })
        .then(status => {
            if (status.status == 'authorized') {
                get_trendingMovies();
                get_trendingShows();
            } else {
                displayTraktAuthMessage();
            }
        })
        .catch(error => {
            console.error('Trakt Auth Check Error:', error);
            // Fallback to show trending content even if auth check fails
            get_trendingMovies();
            get_trendingShows();
        });
    
    // Setup scroll buttons for trending sections
    document.getElementById('scrollLeft_mv').addEventListener('click', function() {
        scroll_mv('left');
    });
    document.getElementById('scrollRight_mv').addEventListener('click', function() {
        scroll_mv('right');
    });
    document.getElementById('scrollLeft_tv').addEventListener('click', function() {
        scroll_tv('left');
    });
    document.getElementById('scrollRight_tv').addEventListener('click', function() {
        scroll_tv('right');
    });
    
    // Initialize the button states
    updateButtonStates_mv();
    updateButtonStates_tv();
    
    // Add window resize listener to update button states
    window.addEventListener('resize', function() {
        updateButtonStates_mv();
        updateButtonStates_tv();
    });
    
    // Fetch available versions
    fetchVersions();
});

// Available versions and selected content
let availableVersions = [];
let selectedContent = null;

// Fetch available versions
async function fetchVersions() {
    try {
        const response = await fetch('/content/versions');
        const data = await response.json();
        if (data.versions) {
            availableVersions = data.versions;
        }
    } catch (error) {
        console.error('Error fetching versions:', error);
        displayError('Error fetching versions');
    }
}

// Show version selection modal
function showVersionModal(content) {
    selectedContent = content;
    const modal = document.getElementById('versionModal');
    const versionCheckboxes = document.getElementById('versionCheckboxes');
    
    // Clear existing checkboxes
    versionCheckboxes.innerHTML = '';
    
    // If this is a TV show, add options for whole show or seasons
    if (content.mediaType === 'tv') {
        // Add a heading for show selection
        const showSelectionHeader = document.createElement('div');
        showSelectionHeader.className = 'version-section-header';
        showSelectionHeader.innerHTML = '<h4>Select Request Type:</h4>';
        versionCheckboxes.appendChild(showSelectionHeader);
        
        // Add radio buttons for selection type
        const selectionTypeContainer = document.createElement('div');
        selectionTypeContainer.className = 'selection-type-container';
        selectionTypeContainer.innerHTML = `
            <div class="selection-type-option">
                <input type="radio" id="whole-show" name="selection-type" value="whole-show" checked>
                <label for="whole-show">Whole Show</label>
            </div>
            <div class="selection-type-option">
                <input type="radio" id="specific-seasons" name="selection-type" value="specific-seasons">
                <label for="specific-seasons">Specific Seasons</label>
            </div>
        `;
        versionCheckboxes.appendChild(selectionTypeContainer);
        
        // Container for season selection (initially hidden)
        const seasonSelectionContainer = document.createElement('div');
        seasonSelectionContainer.className = 'season-selection-container';
        seasonSelectionContainer.id = 'season-selection-container';
        seasonSelectionContainer.style.display = 'none';
        seasonSelectionContainer.innerHTML = '<p>Loading seasons...</p>';
        versionCheckboxes.appendChild(seasonSelectionContainer);
        
        // Add handlers for radio buttons
        const wholeShowRadio = selectionTypeContainer.querySelector('#whole-show');
        const specificSeasonsRadio = selectionTypeContainer.querySelector('#specific-seasons');
        
        wholeShowRadio.addEventListener('change', function() {
            if (this.checked) {
                document.getElementById('season-selection-container').style.display = 'none';
            }
        });
        
        specificSeasonsRadio.addEventListener('change', function() {
            if (this.checked) {
                document.getElementById('season-selection-container').style.display = 'block';
                // Fetch seasons if not already loaded
                if (document.getElementById('season-selection-container').innerHTML === '<p>Loading seasons...</p>') {
                    fetchShowSeasons(content.id);
                }
            }
        });
        
        // Add a separator
        const separator = document.createElement('hr');
        versionCheckboxes.appendChild(separator);
    }
    
    // Add a heading for version selection
    const versionHeader = document.createElement('div');
    versionHeader.className = 'version-section-header';
    versionHeader.innerHTML = '<h4>Select Versions:</h4>';
    versionCheckboxes.appendChild(versionHeader);
    
    // Create checkboxes for each version
    availableVersions.forEach(version => {
        const div = document.createElement('div');
        div.className = 'version-checkbox';
        div.innerHTML = `
            <input type="checkbox" id="${version}" name="versions" value="${version}">
            <label for="${version}">${version}</label>
        `;
        versionCheckboxes.appendChild(div);
    });
    
    modal.style.display = 'flex';
}

// Show version selection modal for a specific season
function showVersionModalForSeason(content) {
    selectedContent = content;
    const modal = document.getElementById('versionModal');
    const versionCheckboxes = document.getElementById('versionCheckboxes');
    
    // Clear existing checkboxes
    versionCheckboxes.innerHTML = '';
    
    // Add a heading for the season being requested
    const seasonHeader = document.createElement('div');
    seasonHeader.className = 'version-section-header';
    seasonHeader.innerHTML = `<h4>Requesting: ${content.title} - Season ${content.seasons[0]}</h4>`;
    versionCheckboxes.appendChild(seasonHeader);
    
    // Add a separator
    const separator = document.createElement('hr');
    versionCheckboxes.appendChild(separator);
    
    // Add a heading for version selection
    const versionHeader = document.createElement('div');
    versionHeader.className = 'version-section-header';
    versionHeader.innerHTML = '<h4>Select Versions:</h4>';
    versionCheckboxes.appendChild(versionHeader);
    
    // Create checkboxes for each version
    availableVersions.forEach(version => {
        const div = document.createElement('div');
        div.className = 'version-checkbox';
        div.innerHTML = `
            <input type="checkbox" id="${version}" name="versions" value="${version}">
            <label for="${version}">${version}</label>
        `;
        versionCheckboxes.appendChild(div);
    });
    
    modal.style.display = 'flex';
}

// Function to fetch show seasons from the server
async function fetchShowSeasons(tmdbId) {
    try {
        console.log(`Fetching seasons for TMDB ID: ${tmdbId}`);
        const response = await fetch(`/content/show_seasons?tmdb_id=${tmdbId}`, {
            method: 'GET'
        });
        
        // Log the HTTP status
        console.log(`Show seasons fetch response status: ${response.status}`);
        
        const data = await response.json();
        console.log('Show seasons API response:', data);
        
        if (data.success && data.seasons && data.seasons.length > 0) {
            // Update the season selection container
            const seasonContainer = document.getElementById('season-selection-container');
            seasonContainer.innerHTML = '<div class="seasons-list"></div>';
            const seasonsList = seasonContainer.querySelector('.seasons-list');
            
            // Sort seasons in numerical order
            const seasons = data.seasons.sort((a, b) => a - b);
            console.log(`Found ${seasons.length} seasons:`, seasons);
            
            // Create checkbox for each season
            seasons.forEach(season => {
                const seasonDiv = document.createElement('div');
                seasonDiv.className = 'season-checkbox';
                seasonDiv.innerHTML = `
                    <input type="checkbox" id="season-${season}" name="seasons" value="${season}">
                    <label for="season-${season}">Season ${season}</label>
                `;
                seasonsList.appendChild(seasonDiv);
            });
        } else {
            console.warn('No seasons found or invalid response format:', data);
            let errorMessage = 'Could not load seasons. Please try again or request the whole show.';
            if (data.error) {
                console.error('API error message:', data.error);
                errorMessage = `Error: ${data.error}`;
            }
            document.getElementById('season-selection-container').innerHTML = `<p>${errorMessage}</p>`;
        }
    } catch (error) {
        console.error('Error fetching show seasons:', error);
        document.getElementById('season-selection-container').innerHTML = 
            '<p>Error loading seasons. Please try again later.</p>';
    }
}

// Close version selection modal
function closeVersionModal() {
    document.getElementById('versionModal').style.display = 'none';
}

// Handle version confirmation
async function handleVersionConfirm() {
    const versionCheckboxes = document.querySelectorAll('#versionCheckboxes input[name="versions"]:checked');
    const selectedVersions = Array.from(versionCheckboxes).map(cb => cb.value);
    
    if (selectedVersions.length === 0) {
        displayError('Please select at least one version');
        return;
    }
    
    // Check if this is a TV show
    if (selectedContent.mediaType === 'tv') {
        // Check if the whole-show radio button exists (it won't exist when using showVersionModalForSeason)
        const wholeShowRadio = document.querySelector('#whole-show');
        
        // If the radio buttons exist, process the selection
        if (wholeShowRadio) {
            const wholeShowSelected = wholeShowRadio.checked;
            
            if (!wholeShowSelected) {
                // Get selected seasons
                const seasonCheckboxes = document.querySelectorAll('#versionCheckboxes input[name="seasons"]:checked');
                const selectedSeasons = Array.from(seasonCheckboxes).map(cb => parseInt(cb.value));
                
                if (selectedSeasons.length === 0) {
                    displayError('Please select at least one season or choose "Whole Show"');
                    return;
                }
                
                // Add seasons to selectedContent
                selectedContent.seasons = selectedSeasons;
            }
        }
        // If radio buttons don't exist, the seasons are already pre-selected in selectedContent
        // from the showVersionModalForSeason function, so we don't need to do anything
    }
    
    await requestContent(selectedContent, selectedVersions);
    closeVersionModal();
}

// Request content
async function requestContent(content, selectedVersions) {
    showLoadingState();
    try {
        const requestData = {
            id: content.id,
            mediaType: content.mediaType,
            title: content.title,
            versions: selectedVersions
        };
        
        // Add seasons if specified for TV shows
        if (content.mediaType === 'tv' && content.seasons) {
            requestData.seasons = content.seasons;
        }
        
        const response = await fetch('/content/request', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(requestData)
        });

        const result = await response.json();
        if (result.success) {
            displaySuccess(`Successfully requested ${content.title}`);
        } else {
            displayError(result.error || 'Failed to request content');
        }
    } catch (error) {
        console.error('Error requesting content:', error);
        displayError('Error requesting content');
    } finally {
        hideLoadingState();
    }
}

function displayTraktAuthMessage() {
    const trendingContainer = document.getElementById('trendingContainer');
    trendingContainer.innerHTML = '<p>Please authenticate with Trakt to see trending movies and shows.</p>';
}

function createMovieElement(data) {
    const movieElement = document.createElement('div');
    movieElement.className = 'media-card';
    
    // Get the isRequester value from the DOM
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';
    
    // Always include the request icon HTML regardless of user type
    const requestIconHTML = `
        <div class="request-icon" title="Request this content">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="8" x2="12" y2="16"></line>
                <line x1="8" y1="12" x2="16" y2="12"></line>
            </svg>
        </div>
    `;
    
    movieElement.innerHTML = `
        <div class="media-poster">
            <span id="trending-rating">${(data.rating).toFixed(1)}</span>
            <span id="trending-watchers">👁 ${data.watcher_count}</span>
            <img src="${data.poster_path.startsWith('static/') ? '/' + data.poster_path : '/scraper/tmdb_image/w300' + data.poster_path}" 
                alt="${data.title}" 
                class="media-poster-img ${data.poster_path.startsWith('static/') ? 'placeholder-poster' : ''}">
            <div class="media-title" style="display: ${document.getElementById('tmdb_api_key_set').value === 'True' ? 'none' : 'block'}">
                <h2>${data.title}</h2>
                <p>${data.year}</p>
            </div>
            ${requestIconHTML}
        </div>
    `;
    
    // Add click handlers for the poster
    movieElement.onclick = function() {
        selectMedia(data.tmdb_id, data.title, data.year, 'movie', null, null, false, data.genre_ids);
    };
    
    // Add click handler for the request icon for all users
    const requestIcon = movieElement.querySelector('.request-icon');
    if (requestIcon) {
        requestIcon.onclick = function(e) {
            e.preventDefault();
            e.stopPropagation();
            
            // Show version modal with content info
            showVersionModal({
                id: data.tmdb_id,
                title: data.title,
                mediaType: 'movie',
                year: data.year
            });
            
            return false;
        };
    }
    
    return movieElement;
}

function createShowElement(data) {
    const showElement = document.createElement('div');
    showElement.className = 'media-card';
    
    // Get the isRequester value from the DOM
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';
    
    // Always include the request icon HTML regardless of user type
    const requestIconHTML = `
        <div class="request-icon" title="Request this content">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="8" x2="12" y2="16"></line>
                <line x1="8" y1="12" x2="16" y2="12"></line>
            </svg>
        </div>
    `;
    
    showElement.innerHTML = `
        <div class="media-poster">
            <span id="trending-rating">${(data.rating).toFixed(1)}</span>
            <span id="trending-watchers">👁 ${data.watcher_count}</span>
            <img src="${data.poster_path.startsWith('static/') ? '/' + data.poster_path : '/scraper/tmdb_image/w300' + data.poster_path}" 
                alt="${data.title}" 
                class="media-poster-img ${data.poster_path.startsWith('static/') ? 'placeholder-poster' : ''}">
            <div class="media-title" style="display: ${document.getElementById('tmdb_api_key_set').value === 'True' ? 'none' : 'block'}">
                <h2>${data.title}</h2>
                <p>${data.year}</p>
            </div>
            ${requestIconHTML}
        </div>
    `;
    
    // Add click handlers for the poster
    showElement.onclick = function() {
        selectSeason(data.tmdb_id, data.title, data.year, 'tv', null, null, true, data.genre_ids, data.vote_average, data.backdrop_path, data.show_overview, data.tmdb_api_key_set);
    };
    
    // Add click handler for the request icon for all users
    const requestIcon = showElement.querySelector('.request-icon');
    if (requestIcon) {
        requestIcon.onclick = function(e) {
            e.preventDefault();
            e.stopPropagation();
            
            // Show version modal with content info
            showVersionModal({
                id: data.tmdb_id,
                title: data.title,
                mediaType: 'tv',
                year: data.year
            });
            
            return false;
        };
    }
    
    return showElement;
}

function get_trendingMovies() {
    toggleResultsVisibility('get_trendingMovies');
    const container_mv = document.getElementById('movieContainer');
    
    fetch('/scraper/movies_trending', {
        method: 'GET'
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            const trendingMovies = data.trendingMovies;
            trendingMovies.forEach(item => {
                const movieElement = createMovieElement(item);
                container_mv.appendChild(movieElement);
            });
        }
    })
    .catch(error => {
        console.error('Error:', error);
        displayError('An error occurred.');
    });
}

function get_trendingShows() {
    toggleResultsVisibility('get_trendingMovies');
    const container_tv = document.getElementById('showContainer');
    
    fetch('/scraper/shows_trending', {
        method: 'GET'
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            const trendingShows = data.trendingShows;
            trendingShows.forEach(item => {
                const showElement = createShowElement(item);
                container_tv.appendChild(showElement);
            });
        }
    })
    .catch(error => {
        console.error('Error:', error);
        displayError('An error occurred.');
    });
}

function searchMedia(event) {
    console.log('searchMedia called', event);
    
    // Prevent the default form submission which would reload the page
    if (event) {
        event.preventDefault();
        console.log('Event default prevented');
    }
    
    // Get the isRequester value
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';
    
    let searchTerm = document.querySelector('input[name="search_term"]').value;
    let version = document.getElementById('version-select').value;
    
    console.log('Search parameters:', { searchTerm, version });
    
    if (!searchTerm) {
        displayError('Please enter a search term');
        return;
    }
    
    showLoadingState();
    
    console.log('Submitting search to /scraper/');
    
    fetch('/scraper/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: `search_term=${encodeURIComponent(searchTerm)}&version=${encodeURIComponent(version)}`
    })
    .then(response => {
        console.log('Search response status:', response.status);
        if (!response.ok) {
            throw new Error(`HTTP error! Status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        console.log('Search response data:', data);
        hideLoadingState();
        
        if (data.error) {
            displayError(data.error);
        } else if (data.results) {
            // Display search results for all users
            displaySearchResults(data.results, version);
            
            // For requesters, also show a reminder that they can only browse
            if (isRequester) {

                // Insert at the top of search results
                const searchResultDiv = document.getElementById('searchResult');

            }
        } else {
            displayError('No results found or invalid response format');
        }
    })
    .catch(error => {
        hideLoadingState();
        console.error('Search Error:', error);
        displayError('An error occurred while searching: ' + error.message);
    });
}

function displaySearchResults(results, version) {
    console.log('Displaying results:', results);
    
    // First hide trending container and show search results
    toggleResultsVisibility('displaySearchResults');
    
    // Get the search results container
    const searchResultsDiv = document.getElementById('searchResults');
    const resultsList = document.getElementById('resultsList');
    
    if (!searchResultsDiv || !resultsList) {
        console.error('Search result elements not found!');
        return;
    }
    
    // Clear previous results
    resultsList.innerHTML = '';
    
    // Show the search results container
    searchResultsDiv.style.display = 'block';
    
    // Check if we have results
    if (!results || results.length === 0) {
        console.log('No results found');
        resultsList.innerHTML = '<p>No results found. Try a different search term.</p>';
        return;
    }
    
    // Get TMDB API key status
    const tmdb_api_key_set = document.getElementById('tmdb_api_key_set').value === 'True';
    // Check if user is a requester
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';

    // Request icon HTML
    const requestIconHTML = `
        <div class="request-icon" title="Request this content">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="8" x2="12" y2="16"></line>
                <line x1="8" y1="12" x2="16" y2="12"></line>
            </svg>
        </div>
    `;

    results.forEach(item => {
        console.log('Creating element for item:', item);  // Debug log
        const searchResDiv = document.createElement('div');
        searchResDiv.className = 'sresult';
        let posterUrl;
        // Remove leading slash if present for checking
        const normalizedPath = item.poster_path.replace(/^\//, '');
        console.log('Raw poster_path:', item.poster_path);
        console.log('Normalized path:', normalizedPath);
        console.log('Starts with static?', normalizedPath.startsWith('static/'));
        console.log('Starts with http?', normalizedPath.startsWith('http'));
        if (normalizedPath.startsWith('static/')) {
            posterUrl = `/${normalizedPath}`;  // Local static image
        } else if (normalizedPath.startsWith('http')) {
            posterUrl = item.poster_path;  // Full URL
        } else {
            posterUrl = `/scraper/tmdb_image/w300${item.poster_path}`; // Use our proxy route
        }
        console.log('Final poster URL:', posterUrl);
        
        // Create the container with a relative position for the request icon
        searchResDiv.innerHTML = `
            <div class="media-poster">
                <button>
                    ${item.media_type === 'show' || item.media_type === 'tv' ? '<span class="mediatype-tv">TV</span>' : '<span class="mediatype-mv">MOVIE</span>'}
                    <img src="${posterUrl}" 
                        alt="${item.title}" 
                        class="${normalizedPath.startsWith('static/') ? 'placeholder-poster' : ''}">
                    <div class="searchresult-info" style="display: ${document.getElementById('tmdb_api_key_set').value === 'True' ? 'none' : 'block'}">
                        <h2 class="searchresult-item">${item.title}</h2>
                        <p class="searchresult-year">${item.year || 'N/A'}</p>
                    </div>
                </button>
                ${requestIconHTML}
            </div>
        `;
        
        console.log('Created HTML:', searchResDiv.innerHTML);  // Debug log
        
        // Add click handler for the main content area
        const button = searchResDiv.querySelector('button');
        if (button) {
            button.onclick = function() {
                // Display a message for requesters instead of attempting to scrape
                if (isRequester) {
                    return;
                }
                
                if (item.media_type === 'movie') {
                    selectMedia(item.id, item.title, item.year, item.media_type, null, null, false, version);
                } else {
                    selectSeason(item.id, item.title, item.year, item.media_type, null, null, true, item.genre_ids, item.vote_average, item.backdrop_path, item.show_overview, tmdb_api_key_set);
                }
            };
        }
        
        // Add click handler for the request icon
        const requestIcon = searchResDiv.querySelector('.request-icon');
        if (requestIcon) {
            requestIcon.onclick = function(e) {
                e.preventDefault();
                e.stopPropagation();
                
                // Show version modal with content info
                showVersionModal({
                    id: item.id,
                    title: item.title,
                    mediaType: item.media_type === 'show' ? 'tv' : item.media_type,
                    year: item.year
                });
                
                return false;
            };
        }
        
        resultsList.appendChild(searchResDiv);
    });
}

async function selectMedia(mediaId, title, year, mediaType, season, episode, multi, genre_ids) {
    // Check if user is a requester before making the request
    const isRequesterEl = document.getElementById('is_requester');
    if (isRequesterEl && isRequesterEl.value === 'True') {
        // Display error message for requesters
        return;
    }

    showLoadingState();
    const version = document.getElementById('version-select').value;
    let formData = new FormData();
    formData.append('media_id', mediaId);
    formData.append('title', title);
    formData.append('year', year);
    formData.append('media_type', mediaType);
    if (season !== null) formData.append('season', season);
    if (episode !== null) formData.append('episode', episode);
    formData.append('multi', multi);
    formData.append('version', version);
    formData.append('skip_cache_check', 'true'); // Always use background checking
    if (genre_ids) formData.append('genre_ids', genre_ids); // Add genre_ids to form data
    
    fetch('/scraper/select_media', {
        method: 'POST',
        body: formData
    })
    .then(response => {
        // Check if response status is 403 (Forbidden) - which means the user is a requester trying to scrape
        if (response.status === 403) {
            hideLoadingState();
            displayError("Access forbidden. You don't have permission to perform this action.");
            return { abort: true };  // Signal to not continue processing
        }
        return response.json();
    })
    .then(data => {
        // Skip further processing if aborted
        if (data.abort) return;
        
        hideLoadingState();
        if (data.error) {
            displayError(data.error);
            return;
        }
        displayTorrentResults(data.torrent_results, title, year, version, mediaId, mediaType, season, episode, genre_ids);
        
        // No need to do additional cache checking since displayTorrentResults already does it
    })
    .catch(error => {
        hideLoadingState();
        console.error('Error:', error);
        displayError('An error occurred while processing your request.');
    });
}

// Function to check cache status in the background and update the UI
function checkCacheStatusInBackground(hashes, results) {
    const cacheStatusElements = document.querySelectorAll('.cache-status');
    let processedCount = 0;
    let totalCount = Math.min(5, results.length);
    let processingQueue = [];
    let isProcessing = false;

    // Update to handle both magnet links and torrent files
    function updateCacheStatusUI(index, status) {
        if (index >= cacheStatusElements.length) return;
        
        const element = cacheStatusElements[index];
        element.classList.remove('not-checked', 'cached', 'not-cached', 'check-unavailable', 'unknown');
        
        if (status === 'cached') {
            element.classList.add('cached');
            element.textContent = '✓';
        } else if (status === 'not_cached') {
            element.classList.add('not-cached');
            element.textContent = '✗';
        } else if (status === 'check_unavailable') {
            element.classList.add('check-unavailable');
            element.textContent = 'N/A';
        } else {
            element.classList.add('unknown');
            element.textContent = '?';
        }
        
        processedCount++;
    }

    function markRemainingAsNA() {
        for (let i = processedCount; i < cacheStatusElements.length; i++) {
            const element = cacheStatusElements[i];
            element.classList.remove('not-checked');
            element.classList.add('check-unavailable');
            element.textContent = 'N/A';
        }
    }

    function showCompletionNotification() {
        if (processedCount > 0) {
            // Only show if at least one result was processed
            const message = `Cache check completed for ${processedCount} ${processedCount === 1 ? 'result' : 'results'}`;
            const notification = document.createElement('div');
            notification.className = 'notification';
            notification.textContent = message;
            document.body.appendChild(notification);
            
            setTimeout(() => {
                notification.classList.add('show');
                setTimeout(() => {
                    notification.classList.remove('show');
                    setTimeout(() => {
                        document.body.removeChild(notification);
                    }, 500);
                }, 3000);
            }, 100);
        }
    }

    function finalizeCacheCheck() {
        markRemainingAsNA();
        showCompletionNotification();
    }

    // Function to check cache status of an item by index
    function checkItemCacheStatus(index) {
        if (index >= totalCount || index >= results.length) {
            isProcessing = false;
            if (processingQueue.length > 0) {
                const nextIndex = processingQueue.shift();
                isProcessing = true;
                checkItemCacheStatus(nextIndex);
            } else {
                finalizeCacheCheck();
            }
            return;
        }

        const result = results[index];
        
        // Skip if no magnet link or torrent URL
        if (!result.magnet_link && !result.torrent_url) {
            updateCacheStatusUI(index, 'check_unavailable');
            isProcessing = false;
            if (processingQueue.length > 0) {
                const nextIndex = processingQueue.shift();
                isProcessing = true;
                checkItemCacheStatus(nextIndex);
            } else if (processedCount >= totalCount) {
                finalizeCacheCheck();
            }
            return;
        }

        // Prepare the data to send
        const payload = {
            index: index
        };

        // Add either magnet link or torrent URL
        if (result.magnet_link) {
            payload.magnet_link = result.magnet_link;
        } else if (result.torrent_url) {
            payload.torrent_url = result.torrent_url;
        }

        console.log(`Checking cache status for item at index ${index}`, payload);
        fetch('/scraper/check_cache_status', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(payload)
        })
        .then(response => {
            console.log(`Received response for index ${index} with status ${response.status}`);
            return response.json();
        })
        .then(data => {
            console.log(`Cache status for index ${index}:`, data);
            updateCacheStatusUI(index, data.status);
            
            isProcessing = false;
            if (processingQueue.length > 0) {
                const nextIndex = processingQueue.shift();
                isProcessing = true;
                checkItemCacheStatus(nextIndex);
            } else if (processedCount >= totalCount) {
                finalizeCacheCheck();
            }
        })
        .catch(error => {
            console.error('Error checking cache status:', error);
            updateCacheStatusUI(index, 'unknown');
            
            isProcessing = false;
            if (processingQueue.length > 0) {
                const nextIndex = processingQueue.shift();
                isProcessing = true;
                checkItemCacheStatus(nextIndex);
            } else if (processedCount >= totalCount) {
                finalizeCacheCheck();
            }
        });
    }

    // Initialize all cache status elements to "Checking..."
    for (let i = 0; i < cacheStatusElements.length; i++) {
        const element = cacheStatusElements[i];
        element.textContent = '...';
        element.classList.add('not-checked');
    }

    // Check the first 5 results (or fewer if there are less than 5 results)
    for (let i = 0; i < totalCount; i++) {
        if (i === 0) {
            isProcessing = true;
            checkItemCacheStatus(i);
        } else {
            processingQueue.push(i);
        }
    }
}

function selectSeason(mediaId, title, year, mediaType, season, episode, multi, genre_ids, vote_average, backdrop_path, show_overview, tmdb_api_key_set) {
    showLoadingState();
    const resultsDiv = document.getElementById('seasonResults');
    const dropdown = document.getElementById('seasonDropdown');
    const seasonPackButton = document.getElementById('seasonPackButton');
    const requestSeasonButton = document.getElementById('requestSeasonButton');
    const version = document.getElementById('version-select').value;
    
    // Get requester status for later use
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';
    
    // Show/hide buttons based on requester status
    if (isRequester) {
        // For requesters: hide season pack button, show request season button
        if (seasonPackButton) seasonPackButton.style.display = 'none';
        if (requestSeasonButton) requestSeasonButton.style.display = 'inline-block';
    } else {
        // For non-requesters: show season pack button, hide request season button
        if (seasonPackButton) seasonPackButton.style.display = 'inline-block';
        if (requestSeasonButton) requestSeasonButton.style.display = 'inline-block';
    }
    
    let formData = new FormData();
    formData.append('media_id', mediaId);
    formData.append('title', title);
    formData.append('year', year);
    formData.append('media_type', mediaType);
    if (season !== null) formData.append('season', season);
    if (episode !== null) formData.append('episode', episode);
    formData.append('multi', multi);
    formData.append('version', version);

    fetch('/scraper/select_season', {
        method: 'POST',
        body: formData
    })
    .then(response => {
        // Check if response status is 403 (Forbidden) - which means the user is a requester trying to scrape
        if (response.status === 403) {
            hideLoadingState();
            displayError("Access forbidden. You don't have permission to perform this action.");
            return { abort: true };  // Signal to not continue processing
        }
        return response.json();
    })
    .then(data => {
        // Skip further processing if aborted
        if (data && data.abort) return;
        
        hideLoadingState();
        if (data.error) {
            displayError(data.error);
        } else {
            const seasonResults = data.episode_results || data.results;

            if (!seasonResults || seasonResults.length === 0) {
                displayError('No season results found');
                return;
            }

            dropdown.innerHTML = '';
            seasonResults.forEach(item => {
                const option = document.createElement('option');
                option.value = JSON.stringify(item);
                option.textContent = `Season: ${item.season_num}`;
                dropdown.appendChild(option);
            });

            dropdown.addEventListener('change', function() {
                const selectedItem = JSON.parse(this.value);
                if (tmdb_api_key_set) {
                    // Use the backdrop_path from the selected item or from the parent scope backdrop_path parameter
                    // Same for show_overview
                    const itemBackdropPath = selectedItem.backdrop_path || backdrop_path || null;
                    const itemShowOverview = selectedItem.show_overview || show_overview || 'No overview available';
                    
                    displaySeasonInfo(
                        selectedItem.title, 
                        selectedItem.season_num, 
                        selectedItem.air_date, 
                        selectedItem.season_overview, 
                        selectedItem.poster_path, 
                        genre_ids, 
                        vote_average, 
                        itemBackdropPath, 
                        itemShowOverview
                    );
                } else {
                    displaySeasonInfoTextOnly(selectedItem.title, selectedItem.season_num);
                }
                selectEpisode(selectedItem.id, selectedItem.title, selectedItem.year, selectedItem.media_type, selectedItem.season_num, null, selectedItem.multi, genre_ids);
            });

            seasonPackButton.onclick = function() {
                // Check if user is a requester before proceeding
                if (isRequester) {
                    return;
                }
                
                const selectedItem = JSON.parse(dropdown.value);
                selectMedia(selectedItem.id, selectedItem.title, selectedItem.year, selectedItem.media_type, selectedItem.season_num, null, selectedItem.multi, genre_ids);
            };
            
            // Add event handler for the request season button
            requestSeasonButton.onclick = function() {
                const selectedItem = JSON.parse(dropdown.value);
                
                // Create content object for the version modal
                const content = {
                    id: selectedItem.id,
                    title: selectedItem.title,
                    year: selectedItem.year,
                    mediaType: 'tv',
                    // Pre-select the current season
                    seasons: [selectedItem.season_num]
                };
                
                // Show the version modal with the current season pre-selected
                showVersionModalForSeason(content);
            };

            // Show results
            resultsDiv.style.display = 'block';

            // Trigger initial selection
            if (dropdown.options.length > 0) {
                dropdown.selectedIndex = 0;
                dropdown.dispatchEvent(new Event('change'));
            }
        }
    })
    .catch(error => {
        hideLoadingState();
        console.error('Error:', error);
        displayError('An error occurred while processing your request.');
    });
}

function displaySeasonInfo(title, season_num, air_date, season_overview, poster_path, genre_ids, vote_average, backdrop_path, show_overview) {
    console.log('Received genre_ids:', genre_ids);
    const seasonInfo = document.getElementById('season-info');

    // Format genre_ids into a string of genre names
    let genreString = '';
    if (Array.isArray(genre_ids)) {
        genreString = genre_ids
            .filter(genre => genre) // Filter out null or undefined genres
            .map(genre => {
                if (typeof genre === 'string') {
                    return genre;
                } else if (typeof genre === 'object' && genre.name) {
                    return genre.name.split(' ').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
                }
                return '';
            })
            .filter(genre => genre) // Filter out any empty strings
            .slice(0, 3) // Truncate to 3 genres
            .join(', ');
    } else if (typeof genre_ids === 'string') {
        genreString = genre_ids;
    }

    // If genreString is empty after processing, set a default message
    if (!genreString) {
        genreString = 'Genres not available';
    }

    // Create the background image style with a fallback if backdrop_path is undefined
    let backgroundImageStyle = '';
    if (backdrop_path) {
        backgroundImageStyle = `background-image: url('${backdrop_path.startsWith('http') ? backdrop_path : `/scraper/tmdb_image/w1920_and_h800_multi_faces${backdrop_path}`}');`;
    } else {
        // Set a fallback background color or gradient
        backgroundImageStyle = 'background: linear-gradient(to bottom, #333333, #121212);';
    }

    seasonInfo.innerHTML = `
        <div class="season-info-container">
            <img src="/scraper/tmdb_image/w300${poster_path}" alt="${title} Season ${season_num}" class="season-poster">
            <div class="season-details">
                <span class="show-rating">${(vote_average).toFixed(1)}</span>
                <h2>${title} - Season ${season_num}</h2>
                <p>${genreString}</p>
                <div class="season-overview">
                    <p>${season_overview ? season_overview : show_overview}</p>
                </div>
            </div>
        </div>
        <div class="season-bg-image" style="${backgroundImageStyle}"></div>
    `;
}

function displaySeasonInfoTextOnly(title, season_num) {
    const seasonInfo = document.getElementById('season-info');

    seasonInfo.innerHTML = `
        <div class="season-info-container text-only">
            <h2>${title} - Season ${season_num}</h2>
        </div>
    `;
}

function selectEpisode(mediaId, title, year, mediaType, season, episode, multi, genre_ids) {
    // Get requester status for later use
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';

    showLoadingState();
    const version = document.getElementById('version-select').value;
    let formData = new FormData();
    formData.append('media_id', mediaId);
    formData.append('title', title);
    formData.append('year', year);
    formData.append('media_type', mediaType);
    formData.append('season', season);
    if (episode !== null) formData.append('episode', episode);
    formData.append('multi', multi);
    formData.append('version', version);

    fetch('/scraper/select_episode', {
        method: 'POST',
        body: formData
    })
    .then(response => {
        // Check if response status is 403 (Forbidden) - which means the user is a requester trying to scrape
        if (response.status === 403) {
            hideLoadingState();
            displayError("Access forbidden. You don't have permission to perform this action.");
            return { abort: true };  // Signal to not continue processing
        }
        return response.json();
    })
    .then(data => {
        // Skip further processing if aborted
        if (data && data.abort) return;
        
        hideLoadingState();
        if (data.error) {
            displayError(data.error);
        } else if (!data.episode_results) {
            displayError('No episode results found');
        } else {
            // Allow requesters to view episodes, but they won't be able to select them
            displayEpisodeResults(data.episode_results, title, year, version, mediaId, mediaType, season, episode, genre_ids);
        }
    })
    .catch(error => {
        hideLoadingState();
        console.error('Error:', error);
        displayError('An error occurred while fetching episodes.');
    });
}