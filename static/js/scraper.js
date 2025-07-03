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
            formData.append('original_scraped_torrent_title', torrent.original_title || torrent.title);
            formData.append('current_score', torrent.score_breakdown?.total_score || '0');

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
        if (!button.classList.contains('close-loading')) {
            button.disabled = true;
            button.style.opacity = '0.5';
        }
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
                const content = {
                    mediaId: item.id,
                    title: item.title,
                    year: item.year,
                    mediaType: item.media_type,
                    season: item.season_num,
                    episode: item.episode_num,
                    multi: item.multi,
                    genre_ids: genre_ids
                };
                showScrapeVersionModal(content);
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
    const overlayContent = document.getElementById('overlayContent');

    // data is now the full object: { torrent_results: [...], filtered_out_torrent_results: [...] }
    const passedTorrents = data.torrent_results || [];
    const filteredOutTorrents = data.filtered_out_torrent_results || [];

    const allDisplayItems = passedTorrents.map(t => ({ ...t, __isActuallyFilteredOut: false }))
                                     .concat(filteredOutTorrents.map(t => ({ ...t, __isActuallyFilteredOut: true })));

    const mediaQuery = window.matchMedia('(max-width: 1024px)');
    function handleScreenChange(e) {
        if (e.matches) { // Mobile view
            overlayContent.innerHTML = `<h3>Torrent Results for ${title} (${year})</h3>`;
            const gridContainer = document.createElement('div');
            gridContainer.style.display = 'flex';
            gridContainer.style.flexWrap = 'wrap';
            gridContainer.style.gap = '15px';
            gridContainer.style.justifyContent = 'center';

            allDisplayItems.forEach((torrent, index) => {
                const isFilteredOut = torrent.__isActuallyFilteredOut;
                const torResDiv = document.createElement('div');
                torResDiv.className = 'torresult' + (isFilteredOut ? ' filtered-out-item' : '');
                
                torResDiv.innerHTML = `
                    <button ${isFilteredOut ? 'disabled style="cursor:default;"' : ''}>
                    <div class="torresult-info">
                        <p class="torresult-title">${torrent.title || torrent.original_title || 'N/A'}</p>
                        <p class="torresult-item">${(torrent.size || 0).toFixed(1)} GB | ${isFilteredOut ? 'N/A' : (torrent.score_breakdown?.total_score || 'N/A')}</p>
                        <p class="torresult-item">${torrent.source || 'N/A'}</p>
                        <span class="cache-status ${torrent.cached === 'Yes' ? 'cached' :
                                      torrent.cached === 'No' ? 'not-cached' :
                                      torrent.cached === 'Not Checked' ? 'not-checked' :
                                      torrent.cached === 'N/A' ? 'check-unavailable' : 'unknown'}" data-index="${index}">${torrent.cached || 'N/A'}</span>
                    </div>
                    </button>             
                `;
                if (!isFilteredOut) {
                    torResDiv.onclick = function() {
                        const torrentData = {
                            title: title, year: year, version: version, media_type: mediaType,
                            season: season || null, episode: episode || null, tmdb_id: mediaId,
                            genres: genre_ids, original_title: torrent.original_title // Pass original_title
                        };
                        addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                    };
                }
                gridContainer.appendChild(torResDiv);
            });
            overlayContent.appendChild(gridContainer);

        } else { // Desktop view
            overlayContent.innerHTML = '';
            const header = document.createElement('h3');
            header.textContent = `Torrent Results for ${title} (${year})`;
            overlayContent.appendChild(header);
            
            const table = document.createElement('table');
            table.style.width = '100%';
            table.style.borderCollapse = 'collapse';

            const thead = document.createElement('thead');
            thead.innerHTML = `
                <tr>
                    <th style="color: rgb(191 191 190); width: 38%;">Name</th>
                    <th style="color: rgb(191 191 190); width: 12%; text-align: right;">Size Per File</th>
                    <th style="color: rgb(191 191 190); width: 10%;">Source</th>
                    <th style="color: rgb(191 191 190); width: 10%; text-align: right;">Score</th>
                    <th style="color: rgb(191 191 190); width: 10%; text-align: center;">Cache</th>
                    <th style="color: rgb(191 191 190); width: 10%; text-align: center;">Add</th>
                    <th style="color: rgb(191 191 190); width: 10%; text-align: center;">Assign</th>
                </tr>
            `;
            table.appendChild(thead);

            const tbody = document.createElement('tbody');
            allDisplayItems.forEach((torrent, index) => {
                const isFilteredOut = torrent.__isActuallyFilteredOut;
                const cacheStatus = torrent.cached || 'Unknown';
                const cacheStatusClass = cacheStatus === 'Yes' ? 'cached' :
                                      cacheStatus === 'No' ? 'not-cached' :
                                      cacheStatus === 'Not Checked' ? 'not-checked' :
                                      cacheStatus === 'N/A' ? 'check-unavailable' : 'unknown';
                
                if (torrent.magnet) {
                    torrent.magnet_link = torrent.magnet;
                }

                const currentVersion = document.getElementById('version-select').value;
                const assignUrlParams = new URLSearchParams({
                    prefill_id: mediaId, prefill_type: mediaType, prefill_title: title,
                    prefill_year: year, prefill_magnet: torrent.magnet, prefill_version: currentVersion
                });
                const assignUrl = `/magnet/assign_magnet?${assignUrlParams.toString()}`;

                const row = document.createElement('tr');
                if (isFilteredOut) {
                    row.classList.add('filtered-out-item'); 
                }

                row.innerHTML = `
                    <td style="font-weight: 600; text-transform: uppercase; color: rgb(191 191 190); word-wrap: break-word; white-space: normal; padding: 10px;">
                        <div style="display: block; line-height: 1.4; min-height: fit-content;">
                            ${torrent.title || torrent.original_title || 'N/A'}
                        </div>
                    </td>
                    <td style="color: rgb(191 191 190); text-align: right;">${(torrent.size || 0).toFixed(1)} GB</td>
                    <td style="color: rgb(191 191 190);">${torrent.source || 'N/A'}</td>
                    <td style="color: rgb(191 191 190); text-align: right;">${isFilteredOut ? 'N/A' : (torrent.score_breakdown?.total_score || 'N/A')}</td>
                    <td style="color: rgb(191 191 190); text-align: center;">
                        <span class="cache-status ${cacheStatusClass}" data-index="${index}">${cacheStatus}</span>
                    </td>
                    <td style="color: rgb(191 191 190); text-align: center;">
                        ${!isFilteredOut ? `<button class="action-button add-button" onclick="addToRealDebrid('${torrent.magnet}', ${JSON.stringify({
                            ...torrent, year, version: torrent.version || version, title,
                            media_type: mediaType, season: season || null, episode: episode || null,
                            tmdb_id: torrent.tmdb_id || mediaId, genres: genre_ids, original_title: torrent.original_title // Pass original_title
                        }).replace(/"/g, '&quot;')})">Add</button>` : ''}
                    </td>
                    <td style="color: rgb(191 191 190); text-align: center;">
                         ${!isFilteredOut ? `<button class="action-button assign-button" onclick="window.location.href='${assignUrl}'">Assign</button>` : ''}
                    </td>
                `;
                tbody.appendChild(row);
            });
            table.appendChild(tbody);
            overlayContent.appendChild(table);
        }
    }
    mediaQuery.addListener(handleScreenChange); // Add listener
    handleScreenChange(mediaQuery); // Initial call

    document.body.classList.add('modal-open');
    overlay.style.display = 'flex';
    
    const closeButton = overlay.querySelector('.close-btn');
    if (closeButton) {
        // Ensure only one listener is attached - simple re-assignment might be enough if this is the only place it's set.
        // For now, let's keep the clone for the button as it's less likely to affect layout.
        const newCloseButton = closeButton.cloneNode(true);
        closeButton.parentNode.replaceChild(newCloseButton, closeButton);
        newCloseButton.onclick = function() { closeOverlay(); };
    }

    // --- START TEMPORARY COMMENT OUT FOR TESTING ---
    /*
    // Ensure overlay click listener is also managed to prevent duplicates if this function is called multiple times
    const newOverlay = overlay.cloneNode(false); // shallow clone for overlay
    overlay.parentNode.replaceChild(newOverlay, overlay); // newOverlay is now the active #overlay
    newOverlay.appendChild(overlayContent); // re-append original overlayContent (with table) into newOverlay

    newOverlay.onclick = function(event) {
        if (event.target === newOverlay) { closeOverlay(); }
    };

    // This part was finding .overlay-content inside newOverlay, which is overlayContent itself
    const overlayContentWrapper = newOverlay.querySelector('#overlayContent'); // Use ID for precision
    if (overlayContentWrapper) { // This should always be true if overlayContent is #overlayContent
        // Let's not clone overlayContentWrapper for now to see if it affects layout
        // const newOverlayContentWrapper = overlayContentWrapper.cloneNode(true);
        // overlayContentWrapper.parentNode.replaceChild(newOverlayContentWrapper, overlayContentWrapper);
        // newOverlayContentWrapper.onclick = function(event) { event.stopPropagation(); };
        
        // Simpler stop propagation for the original overlayContent
        overlayContent.onclick = function(event) { event.stopPropagation(); };
    }
    */
    // --- END TEMPORARY COMMENT OUT FOR TESTING ---

    // --- SIMPLER EVENT LISTENERS (if the above is commented out) ---
    // Ensure the original overlay (if not replaced) has its click listener
    // This might attach multiple times if displayTorrentResults is called repeatedly without full refresh
    // So the cloning strategy was better for event listeners, but let's test layout impact first.
    
    // If you didn't replace 'overlay', set its listener:
     overlay.onclick = function(event) {
         if (event.target === overlay) { closeOverlay(); }
     };
    // And the original overlayContent:
     const currentOverlayContent = document.getElementById('overlayContent');
     if (currentOverlayContent) {
        currentOverlayContent.onclick = function(event) { event.stopPropagation(); };
     }
    // --- END SIMPLER EVENT LISTENERS ---


    checkCacheStatusInBackground(null, allDisplayItems);
}

// Function to close the overlay
function closeOverlay() {
    const overlayElement = document.getElementById('overlay'); // Use overlayElement here as well
    if (overlayElement) {
        overlayElement.style.display = 'none';
        document.body.classList.remove('modal-open');
    }
}

// Add event listeners when DOM content is loaded
document.addEventListener('DOMContentLoaded', async function() {
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
    
    // Set up scrape version modal buttons
    const confirmScrapeButton = document.getElementById('confirmScrapeVersion');
    if (confirmScrapeButton) {
        confirmScrapeButton.addEventListener('click', handleScrapeVersionConfirm);
    }
    
    const cancelScrapeButton = document.getElementById('cancelScrapeVersion');
    if (cancelScrapeButton) {
        cancelScrapeButton.addEventListener('click', closeScrapeVersionModal);
    }
    
    // Close modals when clicking outside
    window.addEventListener('click', function(event) {
        const versionModal = document.getElementById('versionModal');
        const mobileActionModal = document.getElementById('mobileActionModal');
        const scrapeVersionModal = document.getElementById('scrapeVersionModal');
        
        // Close version modal if clicking outside modal content
        if (event.target === versionModal) {
            closeVersionModal();
        }
        
        // Close mobile action modal if clicking outside modal content
        if (event.target === mobileActionModal) {
            closeMobileActionModal();
        }

        // Close scrape version modal if clicking outside modal content
        if (event.target === scrapeVersionModal) {
            closeScrapeVersionModal();
        }
    });
    
    // Close modals when pressing Escape key
    window.addEventListener('keydown', function(event) {
        if (event.key === 'Escape') {
            const versionModal = document.getElementById('versionModal');
            const mobileActionModal = document.getElementById('mobileActionModal');
            const overlayElement = document.getElementById('overlay'); // Use a different name
            const scrapeVersionModal = document.getElementById('scrapeVersionModal');
            
            if (versionModal && versionModal.style.display === 'flex') {
                closeVersionModal();
            }
            
            if (mobileActionModal && mobileActionModal.style.display === 'flex') {
                closeMobileActionModal();
            }

            if (overlayElement && overlayElement.style.display === 'flex') { // Check for flex
                closeOverlay();
            }

            if (scrapeVersionModal && scrapeVersionModal.style.display === 'flex') {
                closeScrapeVersionModal();
            }
        }
    });
    
    // Initialize the Loading object
    Loading.init();
    Loading.setOnClose(hideLoadingState);

    // Handle Allow Specials checkbox
    const allowSpecialsCheckbox = document.getElementById('allow-specials');
    if (allowSpecialsCheckbox) {
        // Load initial state from localStorage
        const allowSpecials = localStorage.getItem('allowSpecials') === 'true';
        allowSpecialsCheckbox.checked = allowSpecials;

        // Save state to localStorage on change
        allowSpecialsCheckbox.addEventListener('change', function() {
            localStorage.setItem('allowSpecials', this.checked);
            console.log(`Allow Specials set to: ${this.checked}`);
        });
    }
    
    // Setup scroll functionality for movie container
    const container_mv = document.getElementById('movieContainer'); // Original declaration
    const scrollLeftBtn_mv = document.getElementById('scrollLeft_mv'); // Original declaration
    const scrollRightBtn_mv = document.getElementById('scrollRight_mv'); // Original declaration
    
    // Initialize button states for movies
    if (scrollLeftBtn_mv) {
        scrollLeftBtn_mv.disabled = false; // Don't disable initially
    }
    
    function updateButtonStates_mv() {
        if (!container_mv) return;
        
        if (scrollLeftBtn_mv) {
            const isAtStart = container_mv.scrollLeft <= 0;
            scrollLeftBtn_mv.disabled = isAtStart;
        }
        
        if (scrollRightBtn_mv) {
            const maxScroll = container_mv.scrollWidth - container_mv.clientWidth - 80; // Adjust margin if needed
            const isAtEnd = container_mv.scrollLeft >= maxScroll - 5;
            scrollRightBtn_mv.disabled = isAtEnd;
        }
    }
    
    function scroll_mv(direction) {
        if (!container_mv) return;
        const scrollAmount = container_mv.clientWidth * 0.8;
        const targetScroll = direction === 'left' 
            ? Math.max(container_mv.scrollLeft - scrollAmount, 0)
            : Math.min(container_mv.scrollLeft + scrollAmount, container_mv.scrollWidth - container_mv.clientWidth);
        container_mv.scrollTo({ left: targetScroll, behavior: 'smooth' });
        setTimeout(updateButtonStates_mv, 500);
    }
    
    if (container_mv) {
        container_mv.addEventListener('scroll', updateButtonStates_mv);
    }
    
    // Setup scroll functionality for TV shows container
    const container_tv = document.getElementById('showContainer'); // Original declaration
    const scrollLeftBtn_tv = document.getElementById('scrollLeft_tv'); // Original declaration
    const scrollRightBtn_tv = document.getElementById('scrollRight_tv'); // Original declaration
    
    // Initialize button states for TV shows
    if (scrollLeftBtn_tv) {
        scrollLeftBtn_tv.disabled = false; // Don't disable initially
    }
    
    function updateButtonStates_tv() {
        if (!container_tv) return;
        
        if (scrollLeftBtn_tv) {
            const isAtStart = container_tv.scrollLeft <= 0;
            scrollLeftBtn_tv.disabled = isAtStart;
        }
        
        if (scrollRightBtn_tv) {
            const maxScroll = container_tv.scrollWidth - container_tv.clientWidth - 50; // Adjust margin if needed
            const isAtEnd = container_tv.scrollLeft >= maxScroll - 5;
            scrollRightBtn_tv.disabled = isAtEnd;
        }
    }
    
    function scroll_tv(direction) {
        if (!container_tv) return;
        const scrollAmount = container_tv.clientWidth * 0.8;
        const targetScroll = direction === 'left' 
            ? Math.max(container_tv.scrollLeft - scrollAmount, 0)
            : Math.min(container_tv.scrollLeft + scrollAmount, container_tv.scrollWidth - container_tv.clientWidth);
        container_tv.scrollTo({ left: targetScroll, behavior: 'smooth' });
        setTimeout(updateButtonStates_tv, 500);
    }
    
    if (container_tv) {
        container_tv.addEventListener('scroll', updateButtonStates_tv);
    }
    
    // Setup scroll functionality for anime container
    const container_anime = document.getElementById('animeContainer');
    const scrollLeftBtn_anime = document.getElementById('scrollLeft_anime');
    const scrollRightBtn_anime = document.getElementById('scrollRight_anime');
    
    // Initialize button states for anime
    if (scrollLeftBtn_anime) {
        scrollLeftBtn_anime.disabled = false;
    }
    
    function updateButtonStates_anime() {
        if (!container_anime) return;
        
        if (scrollLeftBtn_anime) {
            const isAtStart = container_anime.scrollLeft <= 0;
            scrollLeftBtn_anime.disabled = isAtStart;
        }
        
        if (scrollRightBtn_anime) {
            const maxScroll = container_anime.scrollWidth - container_anime.clientWidth - 50;
            const isAtEnd = container_anime.scrollLeft >= maxScroll - 5;
            scrollRightBtn_anime.disabled = isAtEnd;
        }
    }
    
    function scroll_anime(direction) {
        if (!container_anime) return;
        const scrollAmount = container_anime.clientWidth * 0.8;
        const targetScroll = direction === 'left' 
            ? Math.max(container_anime.scrollLeft - scrollAmount, 0)
            : Math.min(container_anime.scrollLeft + scrollAmount, container_anime.scrollWidth - container_anime.clientWidth);
        container_anime.scrollTo({ left: targetScroll, behavior: 'smooth' });
        setTimeout(updateButtonStates_anime, 500);
    }
    
    if (container_anime) {
        container_anime.addEventListener('scroll', updateButtonStates_anime);
    }
    
    // Check Trakt Auth and Load Trending Content
    fetch('/trakt/trakt_auth_status', { method: 'GET' })
        .then(response => {
            if (!response.ok) throw new Error(`HTTP error! Status: ${response.status}`);
            return response.json();
        })
        .then(status => {
            if (status.status == 'authorized') {
                get_trendingMovies(); // Call overridden function
                get_trendingShows();  // Call overridden function
                get_trendingAnime();  // Call anime function
            } else {
                displayTraktAuthMessage();
            }
        })
        .catch(error => {
            console.error('Trakt Auth Check Error:', error);
            get_trendingMovies(); // Fallback
            get_trendingShows();  // Fallback
            get_trendingAnime();  // Fallback
        });
    
    // Setup scroll buttons using already declared variables
    if (scrollLeftBtn_mv) scrollLeftBtn_mv.addEventListener('click', () => scroll_mv('left'));
    if (scrollRightBtn_mv) scrollRightBtn_mv.addEventListener('click', () => scroll_mv('right'));
    if (scrollLeftBtn_tv) scrollLeftBtn_tv.addEventListener('click', () => scroll_tv('left'));
    if (scrollRightBtn_tv) scrollRightBtn_tv.addEventListener('click', () => scroll_tv('right'));
    if (scrollLeftBtn_anime) scrollLeftBtn_anime.addEventListener('click', () => scroll_anime('left'));
    if (scrollRightBtn_anime) scrollRightBtn_anime.addEventListener('click', () => scroll_anime('right'));
    
    // Initialize button states
    updateButtonStates_mv();
    updateButtonStates_tv();
    updateButtonStates_anime();
    
    // Add window resize listener
    window.addEventListener('resize', () => {
        updateButtonStates_mv();
        updateButtonStates_tv();
        updateButtonStates_anime();
    });
    
    // Fetch available versions
    fetchVersions();

    // Update button states after images load
    function setupImageLoadHandlers() {
        document.querySelectorAll('#movieContainer img, #showContainer img, #animeContainer img').forEach(img => {
            if (img.complete) {
                updateButtonStates_mv();
                updateButtonStates_tv();
                updateButtonStates_anime();
            } else {
                img.addEventListener('load', () => {
                    updateButtonStates_mv();
                    updateButtonStates_tv();
                    updateButtonStates_anime();
                });
            }
        });
    }
    
    // Setup initial button states and recalculate after images load
    function initializeTrendingScrolling() {
        setTimeout(() => {
            updateButtonStates_mv();
            updateButtonStates_tv();
            updateButtonStates_anime();
            setupImageLoadHandlers();
        }, 500);
    }
    
    // Override global functions - *Do this outside DOMContentLoaded?*
    // No, keep them here where original functions are defined or accessible.
    const originalGetTrendingMovies = window.get_trendingMovies; // Assuming get_trendingMovies is global
    window.get_trendingMovies = function() {
        if (originalGetTrendingMovies) originalGetTrendingMovies();
        setTimeout(initializeTrendingScrolling, 1000); // Initialize scrolling after content loads
    };
    
    const originalGetTrendingShows = window.get_trendingShows; // Assuming get_trendingShows is global
    window.get_trendingShows = function() {
        if (originalGetTrendingShows) originalGetTrendingShows();
        setTimeout(initializeTrendingScrolling, 1000); // Initialize scrolling after content loads
    };
    
    const originalGetTrendingAnime = window.get_trendingAnime; // Assuming get_trendingAnime is global
    window.get_trendingAnime = function() {
        if (originalGetTrendingAnime) originalGetTrendingAnime();
        setTimeout(initializeTrendingScrolling, 1000); // Initialize scrolling after content loads
    };

    // Final initialization when everything is loaded
    window.addEventListener('load', () => {
        setTimeout(() => {
            updateButtonStates_mv();
            updateButtonStates_tv();
            updateButtonStates_anime();
            setupImageLoadHandlers();
        }, 1000);
    });

    // Initialize mobile action modal
    initializeMobileActionModal();
    
    // Close overlay when clicking outside content
    const overlay = document.getElementById('overlay'); // Original declaration
    if (overlay) {
        overlay.addEventListener('click', function(event) {
            if (event.target === overlay) {
                closeOverlay();
            }
        });
    }
    
    const versionSelect = document.getElementById('version-select');
    if (versionSelect) {
        // Load saved version from localStorage
        const savedVersion = localStorage.getItem('selectedVersion');
        if (savedVersion) {
            // Ensure the saved version is still a valid option
            if (Array.from(versionSelect.options).some(option => option.value === savedVersion)) {
                versionSelect.value = savedVersion;
            } else {
                // If the saved version is no longer valid (e.g., options changed), remove it
                localStorage.removeItem('selectedVersion');
            }
        }

        // Save version to localStorage on change
        versionSelect.addEventListener('change', function() {
            localStorage.setItem('selectedVersion', versionSelect.value);
        });
    }

    // Auto-search if search_term is in URL
    const urlParams = new URLSearchParams(window.location.search);
    const searchTermFromUrl = urlParams.get('search_term');
    if (searchTermFromUrl) {
        const searchInput = document.querySelector('#search-form input[name="search_term"]');
        const searchButton = document.getElementById('searchformButton');
        if (searchInput && searchButton) {
            console.log(`Auto-searching for: ${searchTermFromUrl}`);
            searchInput.value = searchTermFromUrl;
            searchButton.click();
        }
    }
}); // End of DOMContentLoaded

// Available versions and selected content
let availableVersions = [];
let selectedContent = null;
let scrapeContent = null;

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
        
        // If there's only one version available, auto-select it
        if (availableVersions.length === 1) {
            div.querySelector('input[type="checkbox"]').checked = true;
        }
    });
    
    // Add modal-open class to body
    document.body.classList.add('modal-open');
    modal.style.display = 'flex';
}

// Close version selection modal
function closeVersionModal() {
    document.getElementById('versionModal').style.display = 'none';
    // Remove modal-open class from body
    document.body.classList.remove('modal-open');
}

// Close scrape version selection modal
function closeScrapeVersionModal() {
    document.getElementById('scrapeVersionModal').style.display = 'none';
    document.body.classList.remove('modal-open');
}

// New function to show scrape version modal
function showScrapeVersionModal(content) {
    scrapeContent = content;
    const modal = document.getElementById('scrapeVersionModal');
    const versionRadios = document.getElementById('scrapeVersionRadios');

    versionRadios.innerHTML = '';

    availableVersions.forEach((version, index) => {
        const div = document.createElement('div');
        div.className = 'version-checkbox'; // Reuse class for styling
        div.innerHTML = `
            <input type="radio" id="scrape-version-${version}" name="scrape-versions" value="${version}" ${index === 0 ? 'checked' : ''}>
            <label for="scrape-version-${version}">${version}</label>
        `;
        versionRadios.appendChild(div);
    });

    // Add a 'No Version' option
    const noVersionDiv = document.createElement('div');
    noVersionDiv.className = 'version-checkbox';
    noVersionDiv.innerHTML = `
        <input type="radio" id="scrape-version-No Version" name="scrape-versions" value="No Version">
        <label for="scrape-version-No Version">No Version</label>
    `;
    versionRadios.appendChild(noVersionDiv);

    document.body.classList.add('modal-open');
    modal.style.display = 'flex';
}

// New handler for scrape version confirmation
async function handleScrapeVersionConfirm() {
    const selectedVersion = document.querySelector('#scrapeVersionRadios input[name="scrape-versions"]:checked')?.value;
    if (selectedVersion === undefined) {
        displayError('Please select a version.');
        return;
    }

    closeScrapeVersionModal();

    const c = scrapeContent;
    await selectMedia(c.mediaId, c.title, c.year, c.mediaType, c.season, c.episode, c.multi, c.genre_ids, selectedVersion);
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
        
        // If there's only one version available, auto-select it
        if (availableVersions.length === 1) {
            div.querySelector('input[type="checkbox"]').checked = true;
        }
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
    
    closeVersionModal();
    await requestContent(selectedContent, selectedVersions);
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
    
    // --- Create DB Status Pip HTML ---
    let dbStatusPipHTML = '';
    if (data.db_status && data.db_status !== 'missing') {
        dbStatusPipHTML = `<div class="db-status-pip db-status-${data.db_status}" title="Status: ${data.db_status.charAt(0).toUpperCase() + data.db_status.slice(1)}"></div>`;
    }
    // --- End DB Status Pip HTML ---
    
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
    
    // Create tester icon HTML - mirrored on the left side
    const testerIconHTML = `
        <div class="tester-icon" title="Test this content">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M9 3h6v4H9zM6 7h12l-3 10H9z"></path>
                <path d="M10 17h4v4h-4z"></path>
            </svg>
        </div>
    `;
    
    movieElement.innerHTML = `
        <div class="media-poster">
            <span id="trending-rating">${(data.rating).toFixed(1)}</span>
            <span id="trending-watchers">👁 ${data.watcher_count}</span>
            <div class="poster-container">
                <img src="${data.poster_path.startsWith('static/') ? '/' + data.poster_path : '/scraper/tmdb_image/w300' + data.poster_path}" 
                    alt="${data.title}" 
                    class="media-poster-img ${data.poster_path.startsWith('static/') ? 'placeholder-poster' : ''}">
                <div class="poster-overlay">
                    <h3>${data.title}</h3>
                    <p>${data.year}</p>
                </div>
                ${requestIconHTML}
                ${testerIconHTML}
                ${dbStatusPipHTML} // <!-- Add DB Status Pip Here -->
            </div>
            <div class="media-title" style="display: ${document.getElementById('tmdb_api_key_set').value === 'True' ? 'none' : 'block'}">
                <h2>${data.title}</h2>
                <p>${data.year}</p>
            </div>
        </div>
    `;
    
    // Add click handlers for the poster
    movieElement.onclick = function() {
        if (isRequester) {
            // Requester behavior: always show version modal for movies
            showVersionModal({
                id: data.tmdb_id,
                title: data.title,
                mediaType: 'movie', // Explicitly 'movie'
                year: data.year
            });
        } else {
            // Non-requester behavior (existing logic)
            // Check if we're on mobile (screen width <= 768px)
            if (window.innerWidth <= 768) {
                // Prepare data for mobile modal
                const item = {
                    id: data.tmdb_id,
                    title: data.title,
                    year: data.year,
                    media_type: 'movie',
                    genre_ids: data.genre_ids,
                    poster_path: data.poster_path,
                    tmdb_api_key_set: document.getElementById('tmdb_api_key_set').value === 'True'
                };
                
                // Show mobile action modal
                showMobileActionModal(item);
            } else {
                // Desktop behavior - open scrape version modal with properly structured content object
                const content = {
                    mediaId: data.tmdb_id,
                    title: data.title,
                    year: data.year,
                    mediaType: 'movie',
                    season: null,
                    episode: null,
                    multi: false,
                    genre_ids: data.genre_ids
                };
                showScrapeVersionModal(content);
            }
        }
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
    
    // Add click handler for the tester icon
    const testerIcon = movieElement.querySelector('.tester-icon');
    if (testerIcon) {
        testerIcon.onclick = function(e) {
            e.preventDefault();
            e.stopPropagation();
            
            // Redirect to the scraper_tester.html page with the content data as URL parameters
            const params = new URLSearchParams({
                title: data.title,
                id: data.tmdb_id,
                year: data.year,
                media_type: 'movie'
            });
            window.location.href = `/scraper/scraper_tester?${params.toString()}`;
            
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
    
    // --- Create DB Status Pip HTML ---
    let dbStatusPipHTML = '';
    if (data.db_status && data.db_status !== 'missing') {
        dbStatusPipHTML = `<div class="db-status-pip db-status-${data.db_status}" title="Status: ${data.db_status.charAt(0).toUpperCase() + data.db_status.slice(1)}"></div>`;
    }
    // --- End DB Status Pip HTML ---
    
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
    
    // Create tester icon HTML - mirrored on the left side
    const testerIconHTML = `
        <div class="tester-icon" title="Test this content">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M9 3h6v4H9zM6 7h12l-3 10H9z"></path>
                <path d="M10 17h4v4h-4z"></path>
            </svg>
        </div>
    `;
    
    showElement.innerHTML = `
        <div class="media-poster">
            <span id="trending-rating">${(data.rating).toFixed(1)}</span>
            <span id="trending-watchers">👁 ${data.watcher_count}</span>
            <div class="poster-container">
                <img src="${data.poster_path.startsWith('static/') ? '/' + data.poster_path : '/scraper/tmdb_image/w300' + data.poster_path}" 
                    alt="${data.title}" 
                    class="media-poster-img ${data.poster_path.startsWith('static/') ? 'placeholder-poster' : ''}">
                <div class="poster-overlay">
                    <h3>${data.title}</h3>
                    <p>${data.year}</p>
                </div>
                ${requestIconHTML}
                ${testerIconHTML}
                ${dbStatusPipHTML} // <!-- Add DB Status Pip Here -->
            </div>
            <div class="media-title" style="display: ${document.getElementById('tmdb_api_key_set').value === 'True' ? 'none' : 'block'}">
                <h2>${data.title}</h2>
                <p>${data.year}</p>
            </div>
        </div>
    `;
    
    // Add click handlers for the poster
    showElement.onclick = function() {
        // Check if we're on mobile (screen width <= 768px)
        if (window.innerWidth <= 768) {
            // Prepare data for mobile modal
            const item = {
                id: data.tmdb_id,
                title: data.title,
                year: data.year,
                media_type: 'tv',
                genre_ids: data.genre_ids,
                vote_average: data.rating, // Use data.rating from trending shows
                backdrop_path: data.backdrop_path,
                show_overview: data.show_overview,
                poster_path: data.poster_path,
                tmdb_api_key_set: document.getElementById('tmdb_api_key_set').value === 'True'
            };
            
            // Show mobile action modal
            showMobileActionModal(item);
        } else {
            // Desktop behavior - direct scrape
            selectSeason(data.tmdb_id, data.title, data.year, 'tv', null, null, true, data.genre_ids, data.rating, data.backdrop_path, data.show_overview, data.tmdb_api_key_set);
        }
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
    
    // Add click handler for the tester icon
    const testerIcon = showElement.querySelector('.tester-icon');
    if (testerIcon) {
        testerIcon.onclick = function(e) {
            e.preventDefault();
            e.stopPropagation();
            
            // Redirect to the scraper_tester.html page with the content data as URL parameters
            const params = new URLSearchParams({
                title: data.title,
                id: data.tmdb_id,
                year: data.year,
                media_type: 'tv'
            });
            window.location.href = `/scraper/scraper_tester?${params.toString()}`;
            
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

function createAnimeElement(data) {
    const animeElement = document.createElement('div');
    animeElement.className = 'media-card';
    
    // Get the isRequester value from the DOM
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';
    
    // --- Create DB Status Pip HTML ---
    let dbStatusPipHTML = '';
    if (data.db_status && data.db_status !== 'missing') {
        dbStatusPipHTML = `<div class="db-status-pip db-status-${data.db_status}" title="Status: ${data.db_status.charAt(0).toUpperCase() + data.db_status.slice(1)}"></div>`;
    }
    // --- End DB Status Pip HTML ---
    
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
    
    // Create tester icon HTML - mirrored on the left side
    const testerIconHTML = `
        <div class="tester-icon" title="Test this content">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M9 3h6v4H9zM6 7h12l-3 10H9z"></path>
                <path d="M10 17h4v4h-4z"></path>
            </svg>
        </div>
    `;
    
    animeElement.innerHTML = `
        <div class="media-poster">
            <span id="trending-rating">${(data.rating).toFixed(1)}</span>
            <span id="trending-watchers">👁 ${data.watcher_count}</span>
            <div class="poster-container">
                <img src="${data.poster_path.startsWith('static/') ? '/' + data.poster_path : '/scraper/tmdb_image/w300' + data.poster_path}" 
                    alt="${data.title}" 
                    class="media-poster-img ${data.poster_path.startsWith('static/') ? 'placeholder-poster' : ''}">
                <div class="poster-overlay">
                    <h3>${data.title}</h3>
                    <p>${data.year}</p>
                </div>
                ${requestIconHTML}
                ${testerIconHTML}
                ${dbStatusPipHTML}
            </div>
            <div class="media-title" style="display: ${document.getElementById('tmdb_api_key_set').value === 'True' ? 'none' : 'block'}">
                <h2>${data.title}</h2>
                <p>${data.year}</p>
            </div>
        </div>
    `;
    
    // Add click handlers for the poster
    animeElement.onclick = function() {
        // Check if we're on mobile (screen width <= 768px)
        if (window.innerWidth <= 768) {
            // Prepare data for mobile modal
            const item = {
                id: data.tmdb_id,
                title: data.title,
                year: data.year,
                media_type: 'tv', // Anime is treated as TV show
                genre_ids: data.genre_ids,
                vote_average: data.rating, // Use data.rating from trending anime
                backdrop_path: data.backdrop_path,
                show_overview: data.show_overview,
                poster_path: data.poster_path,
                tmdb_api_key_set: document.getElementById('tmdb_api_key_set').value === 'True'
            };
            
            // Show mobile action modal
            showMobileActionModal(item);
        } else {
            // Desktop behavior - direct scrape
            selectSeason(data.tmdb_id, data.title, data.year, 'tv', null, null, true, data.genre_ids, data.rating, data.backdrop_path, data.show_overview, data.tmdb_api_key_set);
        }
    };
    
    // Add click handler for the request icon for all users
    const requestIcon = animeElement.querySelector('.request-icon');
    if (requestIcon) {
        requestIcon.onclick = function(e) {
            e.preventDefault();
            e.stopPropagation();
            
            // Show version modal with content info
            showVersionModal({
                id: data.tmdb_id,
                title: data.title,
                mediaType: 'tv', // Anime is treated as TV show
                year: data.year
            });
            
            return false;
        };
    }
    
    // Add click handler for the tester icon
    const testerIcon = animeElement.querySelector('.tester-icon');
    if (testerIcon) {
        testerIcon.onclick = function(e) {
            e.preventDefault();
            e.stopPropagation();
            
            // Redirect to the scraper_tester.html page with the content data as URL parameters
            const params = new URLSearchParams({
                title: data.title,
                id: data.tmdb_id,
                year: data.year,
                media_type: 'tv' // Anime is treated as TV show
            });
            window.location.href = `/scraper/scraper_tester?${params.toString()}`;
            
            return false;
        };
    }
    
    return animeElement;
}

function get_trendingAnime() {
    toggleResultsVisibility('get_trendingMovies');
    const container_anime = document.getElementById('animeContainer');
    
    fetch('/scraper/anime_trending', {
        method: 'GET'
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            const trendingAnime = data.trendingAnime;
            trendingAnime.forEach(item => {
                const animeElement = createAnimeElement(item);
                container_anime.appendChild(animeElement);
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
    
    let searchTerm = document.querySelector('input[name="search_term"]').value.trim(); // Trim whitespace
    let version = document.getElementById('version-select').value;
    
    console.log('Search parameters:', { searchTerm, version });
    
    if (!searchTerm) {
        displayError('Please enter a search term or ID (e.g., tt1234567 or tmdb12345)');
        return;
    }
    
    showLoadingState();
    
    let fetchUrl;
    let fetchBody;
    const imdbIdPattern = /^tt\d+$/i; // Case insensitive for tt
    const tmdbIdPrefixedPattern = /^tmdb\d+$/i; // Case insensitive for tmdb prefix

    if (imdbIdPattern.test(searchTerm)) {
        console.log('Detected IMDb ID:', searchTerm);
        fetchUrl = '/scraper/lookup_by_id';
        fetchBody = `id_type=imdb&media_id=${encodeURIComponent(searchTerm)}`;
    } else if (tmdbIdPrefixedPattern.test(searchTerm)) {
        const tmdbId = searchTerm.substring(4); // Remove "tmdb" prefix
        console.log('Detected TMDb ID (after stripping prefix):', tmdbId);
        fetchUrl = '/scraper/lookup_by_id';
        fetchBody = `id_type=tmdb&media_id=${encodeURIComponent(tmdbId)}`;
    } else {
        console.log('Performing standard search for:', searchTerm);
        fetchUrl = '/scraper/';
        fetchBody = `search_term=${encodeURIComponent(searchTerm)}&version=${encodeURIComponent(version)}`;
    }

    console.log(`Submitting search to ${fetchUrl}`);

    fetch(fetchUrl, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: fetchBody
    })
    .then(response => {
        console.log('Search response status:', response.status);
        if (!response.ok) {
            // Try to parse error JSON, otherwise use status text
            return response.json().then(err => {
                throw new Error(err.error || `HTTP error! Status: ${response.status}`);
            }).catch(() => {
            throw new Error(`HTTP error! Status: ${response.status}`);
            });
        }
        return response.json();
    })
    .then(data => {
        console.log('Search response data:', data);
        hideLoadingState();
        
        if (data.error) {
            displayError(data.error);
        } else if (data.results && data.results.length > 0) {
            // Display search results for all users
            displaySearchResults(data.results, version); // Pass version for consistency
            
            // For requesters, also show a reminder that they can only browse
            if (isRequester) {
                // Insert reminder if needed (optional)
            }
        } else {
             // Handle case where ID lookup returns no results specifically
             if (fetchUrl === '/scraper/lookup_by_id') {
                 displayError('No media found for the provided ID.');
        } else {
            displayError('No results found or invalid response format');
             }
        }
    })
    .catch(error => {
        hideLoadingState();
        console.error('Search Error:', error);
        displayError('An error occurred while searching: ' + error.message);
    });
}

function displaySearchResults(results, version) {
    console.log('Displaying results. First item:', results.length > 0 ? JSON.stringify(results[0]) : 'No results'); // Log the first item as JSON
    
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
    
    // Validate that results is an array
    if (!Array.isArray(results)) {
        console.error('Expected results to be an array but got:', typeof results);
        displayError('Invalid response format, likely Trakt connection issue');
        return;
    }
    
    // Check if we have results
    if (results.length === 0) {
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

    // Tester icon HTML
    const testerIconHTML = `
        <div class="tester-icon" title="Test this content">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M9 3h6v4H9zM6 7h12l-3 10H9z"></path>
                <path d="M10 17h4v4h-4z"></path>
            </svg>
        </div>
    `;

    // Assign Magnet icon HTML
    const assignMagnetIconHTML = `
        <div class="assign-magnet-icon" title="Assign Magnet Link">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                <polyline points="17 8 12 3 7 8"></polyline>
                <line x1="12" y1="3" x2="12" y2="15"></line>
                <path d="M15 8h2a1 1 0 0 1 1 1v2"></path> 
                <path d="M9 8H7a1 1 0 0 0-1 1v2"></path> 
                <path d="M12 18.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z"></path> 
            </svg>
        </div>
    `;

    results.forEach(item => {
        console.log('Processing item for display:', JSON.stringify(item, null, 2));
        const searchResDiv = document.createElement('div');
        searchResDiv.className = 'sresult';
        let posterUrl = '/static/images/placeholder.png'; // Default placeholder
        let isPlaceholder = true;

        // --- Use item.poster_path (lowercase with underscore) ---
        if (item.poster_path && typeof item.poster_path === 'string' && item.poster_path.trim() !== '') {
             const pathToCheck = item.poster_path.trim(); // Use correct key here
             console.log('Checking poster_path:', pathToCheck); // Log correct key

             // --- Logic remains the same, just uses pathToCheck from correct key ---
             if (pathToCheck.startsWith('static/')) {
                 posterUrl = pathToCheck.startsWith('/') ? pathToCheck : `/${pathToCheck}`;
                 isPlaceholder = pathToCheck.includes('placeholder.png');
                 console.log(`Poster type: static, Placeholder: ${isPlaceholder}`);
             } else if (pathToCheck.startsWith('http')) {
                 posterUrl = pathToCheck;
                 isPlaceholder = false;
                  console.log('Poster type: http');
             } else if (pathToCheck.startsWith('/scraper/tmdb_image')) {
                  posterUrl = pathToCheck.startsWith('/') ? pathToCheck : `/${pathToCheck}`;
                  isPlaceholder = false;
                  console.log('Poster type: proxy');
             } else if (pathToCheck.startsWith('/')) { // Assume TMDB path
                 posterUrl = `/scraper/tmdb_image/w300${pathToCheck}`; // Use proxy route
                 isPlaceholder = false;
                  console.log('Poster type: assumed TMDB, using proxy');
             } else {
                 console.warn(`Unknown poster_path format, using placeholder: ${pathToCheck}`);
             }
        } else {
             console.warn('Missing, empty, or invalid poster_path, using placeholder. Value:', item.poster_path); // Log correct key
        }
        console.log('Final poster URL:', posterUrl);
        // --- End Poster Path Logic ---

        // --- Create DB Status Pip HTML ---
        let dbStatusPipHTML = '';
        if (item.db_status && item.db_status !== 'missing') {
            dbStatusPipHTML = `<div class="db-status-pip db-status-${item.db_status}" title="Status: ${item.db_status.charAt(0).toUpperCase() + item.db_status.slice(1)}"></div>`;
        }
        // --- End DB Status Pip HTML ---

        // --- Prioritize item.year for display ---
        const displayYear = item.year || (item.release_date ? String(item.release_date).substring(0, 4) : 'N/A');
        // --- End Year Display Fix ---

        searchResDiv.innerHTML = `
            <div class="media-poster">
                <button>
                    ${item.media_type === 'show' || item.media_type === 'tv' ? '<span class="mediatype-tv">TV</span>' : '<span class="mediatype-mv">MOVIE</span>'}
                    <div class="poster-container">
                        <img src="${posterUrl}"
                            alt="${item.title}"
                            class="${isPlaceholder ? 'placeholder-poster' : ''}">
                        <div class="poster-overlay">
                            <h3>${item.title}</h3>
                            <p>${displayYear}</p>
                        </div>
                        ${requestIconHTML}
                        ${testerIconHTML}
                        ${assignMagnetIconHTML}
                        ${dbStatusPipHTML}
                    </div>
                    <div class="searchresult-info" style="display: ${!tmdb_api_key_set ? 'block' : 'none'}">
                        <h2 class="searchresult-item">${item.title}</h2>
                        <p class="searchresult-year">${displayYear}</p>
                    </div>
                </button>
            </div>
        `;

        // ... (rest of the button handlers remain the same) ...
         // Add click handler for the main content area
        const button = searchResDiv.querySelector('button');
        if (button) {
            button.onclick = function() {
                if (isRequester) { return; }

                if (window.innerWidth <= 768) {
                    item.tmdb_api_key_set = tmdb_api_key_set;
                    item.version = version;
                    showMobileActionModal(item);
                } else {
                    if (item.media_type === 'movie') {
                        const content = {
                            mediaId: item.id,
                            title: item.title,
                            year: item.year,
                            mediaType: 'movie',
                            season: null,
                            episode: null,
                            multi: false,
                            genre_ids: item.genre_ids
                        };
                        showScrapeVersionModal(content);
                    } else {
                         // Make sure to pass the correct poster path key if needed by selectSeason
                        selectSeason(item.id, item.title, item.year, item.media_type, null, null, true, item.genre_ids, item.voteAverage, item.backdrop_path, item.show_overview, tmdb_api_key_set);
                    }
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

        // Add click handler for the tester icon
        const testerIcon = searchResDiv.querySelector('.tester-icon');
        if (testerIcon) {
            testerIcon.onclick = function(e) {
                e.preventDefault();
                e.stopPropagation();

                // Redirect to the scraper_tester.html page with the content data as URL parameters
                const params = new URLSearchParams({
                    title: item.title,
                    id: item.id,
                    year: item.year, // Use item.year which should be correct
                    media_type: item.media_type === 'show' ? 'tv' : item.media_type
                });
                window.location.href = `/scraper/scraper_tester?${params.toString()}`;

                return false;
            };
        }

        // --- Add click handler for the assign magnet icon ---
        const assignMagnetIcon = searchResDiv.querySelector('.assign-magnet-icon');
        if (assignMagnetIcon) {
            // Store data on the icon element itself for easy access
            assignMagnetIcon.dataset.id = item.id;
            assignMagnetIcon.dataset.title = item.title;
            assignMagnetIcon.dataset.year = item.year; // Use item.year
            assignMagnetIcon.dataset.mediaType = item.media_type === 'show' ? 'tv' : item.media_type; // Normalize to 'tv'

            assignMagnetIcon.onclick = function(e) {
                e.preventDefault();
                e.stopPropagation();

                const id = this.dataset.id;
                const title = encodeURIComponent(this.dataset.title);
                const year = this.dataset.year;
                const mediaType = this.dataset.mediaType;
                const currentVersion = document.getElementById('version-select').value; // Get current version

                // Construct the URL for the magnet assigner page
                const assignUrlParams = new URLSearchParams({
                    prefill_id: id,
                    prefill_type: mediaType,
                    prefill_title: title,
                    prefill_year: year,
                    prefill_version: currentVersion
                });
                const assignUrl = `/magnet/assign_magnet?${assignUrlParams.toString()}`;

                // Redirect the user
                window.location.href = assignUrl;

                return false;
            };
        }
        // --- END Assign Magnet Icon Handler ---


        resultsList.appendChild(searchResDiv);
    });
}

async function selectMedia(mediaId, title, year, mediaType, season, episode, multi, genre_ids, version) {
    // Check if user is a requester before making the request
    const isRequesterEl = document.getElementById('is_requester');
    if (isRequesterEl && isRequesterEl.value === 'True') {
        // Display error message for requesters
        return;
    }

    if (!mediaId || mediaId === 'undefined') {
        console.error("selectMedia called with invalid mediaId:", mediaId);
        displayError("An internal error occurred: media ID is missing.");
        hideLoadingState();
        return;
    }

    showLoadingState();
    let formData = new FormData();
    formData.append('media_id', mediaId);
    formData.append('title', title);
    formData.append('year', year);
    formData.append('media_type', mediaType);
    if (season != null) formData.append('season', season);
    if (episode != null) formData.append('episode', episode);
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
        // Pass the whole 'data' object
        displayTorrentResults(data, title, year, version, mediaId, mediaType, season, episode, genre_ids);
        
        // No need to do additional cache checking since displayTorrentResults already does it
    })
    .catch(error => {
        hideLoadingState(); 1
        console.error('Error:', error);
        displayError('An error occurred while processing your request.');
    });
}

// Function to check cache status in the background and update the UI
function checkCacheStatusInBackground(hashes, results) {
    const cacheStatusElements = document.querySelectorAll('.cache-status');
    let processedCount = 0;
    let totalCount = Math.min(5, results.length);
    let processingItems = new Set(); // Track items currently being processed
    const MAX_PARALLEL_REQUESTS = 1; // Process up to 3 items at once

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
        processingItems.delete(index);
        
        // Try to process more items if we have capacity
        processNextItems();
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
        if (processingItems.size === 0) {
            markRemainingAsNA();
            showCompletionNotification();
        }
    }

    // Function to check if we've completed all items
    function checkCompletion() {
        if (processedCount >= totalCount) {
            finalizeCacheCheck();
            return true;
        }
        return false;
    }

    // Function to check cache status of an item by index
    function checkItemCacheStatus(index) {
        if (index >= totalCount || index >= results.length) {
            processingItems.delete(index);
            processNextItems();
            return;
        }

        const result = results[index];
        
        // If the item was filtered out (score is N/A), or its score is inherently null/undefined (displayed as N/A),
        // or if there's no magnet link or torrent URL, mark cache status as N/A and skip checking.
        if (result.__isActuallyFilteredOut || 
            result.score_breakdown?.total_score == null || 
            (!result.magnet_link && !result.torrent_url)) {
            updateCacheStatusUI(index, 'check_unavailable');
            processingItems.delete(index);
            processNextItems();
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

        console.log(`Checking cache status for item at index ${index}`);
        fetch('/scraper/check_cache_status', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(payload)
        })
        .then(response => {
            return response.json();
        })
        .then(data => {
            console.log(`Cache status for index ${index}:`, data);
            updateCacheStatusUI(index, data.status);
            checkCompletion();
        })
        .catch(error => {
            console.error(`Error checking cache status for index ${index}:`, error);
            updateCacheStatusUI(index, 'unknown');
            checkCompletion();
        });
    }

    // Function to process next items in the queue
    function processNextItems() {
        // If we've already processed all items, finalize
        if (processedCount >= totalCount) {
            finalizeCacheCheck();
            return;
        }
        
        // Process new items up to our parallel limit
        for (let i = 0; i < totalCount; i++) {
            // Skip if we're at capacity or this item is already being processed
            if (processingItems.size >= MAX_PARALLEL_REQUESTS || processingItems.has(i)) {
                continue;
            }
            
            // Skip if this item is already processed
            const element = cacheStatusElements[i];
            if (!element.classList.contains('not-checked')) {
                continue;
            }
            
            // Process this item
            processingItems.add(i);
            checkItemCacheStatus(i);
            
            // Exit if we're at capacity
            if (processingItems.size >= MAX_PARALLEL_REQUESTS) {
                break;
            }
        }
        
        // If there's nothing being processed but we haven't finished, check completion
        if (processingItems.size === 0 && processedCount < totalCount) {
            finalizeCacheCheck();
        }
    }

    // Initialize all cache status elements to "Checking..."
    for (let i = 0; i < cacheStatusElements.length; i++) {
        const element = cacheStatusElements[i];
        element.textContent = '...';
        element.classList.add('not-checked');
    }

    // Start processing items
    processNextItems();
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
    formData.append('allow_specials', localStorage.getItem('allowSpecials') === 'true'); // Add allow_specials flag

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
                // Display "Specials" for season 0
                option.textContent = item.season_num === 0 ? 'Specials' : `Season: ${item.season_num}`;
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
                const content = {
                    mediaId: selectedItem.id,
                    title: selectedItem.title,
                    year: selectedItem.year,
                    mediaType: selectedItem.media_type,
                    season: selectedItem.season_num,
                    episode: null,
                    multi: true, // Season packs are multi-file
                    genre_ids: genre_ids
                };
                showScrapeVersionModal(content);
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

    // Display "Specials" for season 0
    const seasonLabel = season_num === 0 ? 'Specials' : `Season ${season_num}`;

    seasonInfo.innerHTML = `
        <div class="season-info-container">
            <img src="/scraper/tmdb_image/w300${poster_path}" alt="${title} ${seasonLabel}" class="season-poster">
            <div class="season-details">
                <span class="show-rating">${(vote_average || 0).toFixed(1)}</span>
                <h2>${title} - ${seasonLabel}</h2>
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
    // Display "Specials" for season 0
    const seasonLabel = season_num === 0 ? 'Specials' : `Season ${season_num}`;

    seasonInfo.innerHTML = `
        <div class="season-info-container text-only">
            <h2>${title} - ${seasonLabel}</h2>
        </div>
    `;
}

function selectEpisode(mediaId, title, year, mediaType, season, episode, multi, genre_ids) {
    // Get requester status for later use
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';

    // Get Allow Specials preference
    const allowSpecials = localStorage.getItem('allowSpecials') === 'true';

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
    formData.append('allow_specials', allowSpecials); // Add allow_specials flag

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

// Add this function to create and handle the mobile action modal
function initializeMobileActionModal() {
    // Create modal element if it doesn't exist
    if (!document.getElementById('mobileActionModal')) {
        const modalHtml = `
            <div id="mobileActionModal" class="mobile-action-modal">
                <div class="mobile-action-content">
                    <div class="mobile-action-title"></div>
                    <div class="mobile-action-year"></div>
                    <div class="mobile-action-buttons">
                        <button class="mobile-action-button mobile-scrape-button">
                            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <circle cx="11" cy="11" r="8"></circle>
                                <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                            </svg>
                            Scrape Content
                        </button>
                        <button class="mobile-action-button mobile-request-button">
                            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <circle cx="12" cy="12" r="10"></circle>
                                <line x1="12" y1="8" x2="12" y2="16"></line>
                                <line x1="8" y1="12" x2="16" y2="12"></line>
                            </svg>
                            Request Content
                        </button>
                        <button class="mobile-action-button mobile-cancel-button">Cancel</button>
                    </div>
                </div>
            </div>
        `;
        
        // Append modal to body
        document.body.insertAdjacentHTML('beforeend', modalHtml);
        
        // Set up event listeners for modal buttons
        const modal = document.getElementById('mobileActionModal');
        const scrapeButton = modal.querySelector('.mobile-scrape-button');
        const requestButton = modal.querySelector('.mobile-request-button');
        const cancelButton = modal.querySelector('.mobile-cancel-button');
        
        cancelButton.addEventListener('click', closeMobileActionModal);
        
        // Close modal when clicking outside content area
        modal.addEventListener('click', function(e) {
            if (e.target === modal) {
                closeMobileActionModal();
            }
        });
    }
    
    // Add window resize listener to handle responsive behavior
    window.addEventListener('resize', function() {
        // Close modal if screen size changes from mobile to desktop
        if (window.innerWidth > 768) {
            closeMobileActionModal();
        }
    });
}

// Function to show mobile action modal
function showMobileActionModal(item) {
    const modal = document.getElementById('mobileActionModal');
    const titleEl = modal.querySelector('.mobile-action-title');
    const yearEl = modal.querySelector('.mobile-action-year');
    const scrapeButton = modal.querySelector('.mobile-scrape-button');
    const requestButton = modal.querySelector('.mobile-request-button');

    // Get requester status
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';
    
    // Set content title and year
    titleEl.textContent = item.title;
    yearEl.textContent = item.year || (item.release_date ? new Date(item.release_date).getFullYear() : 'N/A');
    
    // Change button text based on media type
    if (item.media_type === 'tv' || item.media_type === 'show') {
        // Update the button text and icon for TV shows
        scrapeButton.innerHTML = `
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="2" y="7" width="20" height="15" rx="2" ry="2"></rect>
                <polyline points="17 2 12 7 7 2"></polyline>
            </svg>
            Enter Show
        `;
    } else {
        // Reset to default for movies
        scrapeButton.innerHTML = `
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="11" cy="11" r="8"></circle>
                <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            Scrape Content
        `;
    }

    // Conditionally display the scrape button
    if (isRequester) {
        scrapeButton.style.display = 'none';
    } else {
        scrapeButton.style.display = 'flex'; // Or 'block', 'inline-block' depending on original styling
    }
    
    // Set up button actions
    scrapeButton.onclick = function() {
        closeMobileActionModal();
        if (item.media_type === 'movie') {
            const content = {
                mediaId: item.id,
                title: item.title,
                year: item.year,
                mediaType: item.media_type,
                season: null,
                episode: null,
                multi: false,
                genre_ids: item.genre_ids
            };
            showScrapeVersionModal(content);
        } else {
            selectSeason(item.id, item.title, item.year, item.media_type, null, null, true, item.genre_ids, item.vote_average || item.voteAverage, item.backdrop_path, item.show_overview, item.tmdb_api_key_set);
        }
    };
    
    requestButton.onclick = function() {
        closeMobileActionModal();
        showVersionModal({
            id: item.id,
            title: item.title,
            mediaType: item.media_type === 'show' ? 'tv' : item.media_type,
            year: item.year
        });
    };
    
    // Show modal
    modal.style.display = 'flex';
}

// Function to close mobile action modal
function closeMobileActionModal() {
    const modal = document.getElementById('mobileActionModal');
    modal.style.display = 'none';
    // Remove modal-open class from body
    document.body.classList.remove('modal-open');
}

async function handleAutoScrape(imdbId, season, episode, version) {
    showLoadingState();
    console.log(`Auto-scraping for IMDb ID: ${imdbId}, Season: ${season}, Episode: ${episode}, Version: ${version}`);

    try {
        if (version) {
            const versionSelect = document.getElementById('version-select');
            if (versionSelect) {
                if (availableVersions.length === 0) {
                    await fetchVersions();
                }
                if (Array.from(versionSelect.options).some(opt => opt.value === version)) {
                    versionSelect.value = version;
                } else {
                    console.warn(`Version "${version}" not found in dropdown. Using default.`);
                }
            }
        }

        const lookupResponse = await fetch('/scraper/lookup_by_id', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: `id_type=imdb&media_id=${encodeURIComponent(imdbId)}`
        });

        if (!lookupResponse.ok) {
            const errorData = await lookupResponse.json().catch(() => ({}));
            throw new Error(errorData.error || `Failed to look up IMDb ID: ${lookupResponse.statusText}`);
        }

        const lookupData = await lookupResponse.json();
        if (!lookupData.results || lookupData.results.length === 0) {
            throw new Error('No media found for the provided IMDb ID.');
        }

        const mediaInfo = lookupData.results[0];
        if (mediaInfo.media_type !== 'show' && mediaInfo.media_type !== 'tv') {
            throw new Error('Auto-scraping is only supported for TV shows.');
        }
        
        const { id: mediaId, title, year, media_type: mediaType, genre_ids } = mediaInfo;
        const isMulti = !!(season && !episode);

        await selectMedia(mediaId, title, year, mediaType, season, episode, isMulti, genre_ids, version);

    } catch (error) {
        hideLoadingState();
        console.error('Auto-scrape failed:', error);
        displayError(`Auto-scrape failed: ${error.message}`);
    }
}
