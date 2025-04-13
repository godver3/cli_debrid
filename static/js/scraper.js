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
            formData.append('original_scraped_torrent_title', torrent.original_title || ''); // Add the original torrent title

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
    const overlayContent = document.getElementById('overlayContent');

    const mediaQuery = window.matchMedia('(max-width: 1024px)');
    function handleScreenChange(e) {
        if (e.matches) {
            overlayContent.innerHTML = `<h3>Torrent Results for ${title} (${year})</h3>`;
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

            overlayContent.appendChild(gridContainer);
        } else {
            // Clear content first
            overlayContent.innerHTML = '';
            
            // Add the header
            const header = document.createElement('h3');
            header.textContent = `Torrent Results for ${title} (${year})`;
            overlayContent.appendChild(header);
            
            // Create table element
            const table = document.createElement('table');
            table.style.width = '100%';
            table.style.borderCollapse = 'collapse';

            // Create table header with corrected widths
            const thead = document.createElement('thead');
            thead.innerHTML = `
                <tr>
                    <th style="color: rgb(191 191 190); width: 40%;">Name</th>
                    <th style="color: rgb(191 191 190); width: 10%; text-align: right;">Size</th>
                    <th style="color: rgb(191 191 190); width: 10%;">Source</th>
                    <th style="color: rgb(191 191 190); width: 10%%; text-align: right;">Score</th>
                    <th style="color: rgb(191 191 190); width: 10%; text-align: center;">Cache</th>
                    <th style="color: rgb(191 191 190); width: 10%; text-align: center;">Add</th>
                    <th style="color: rgb(191 191 190); width: 10%; text-align: center;">Assign</th>
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

                // --- Prepare data for Assign Magnet button ---
                const currentVersion = document.getElementById('version-select').value; // <-- Get current version
                const assignUrlParams = new URLSearchParams({
                    prefill_id: mediaId,
                    prefill_type: mediaType,
                    prefill_title: title,
                    prefill_year: year,
                    prefill_magnet: torrent.magnet,
                    prefill_version: currentVersion // <-- Add version
                });
                const assignUrl = `/magnet/assign_magnet?${assignUrlParams.toString()}`;
                // --- End Prepare data ---

                const row = document.createElement('tr');
                // --- MODIFIED row.innerHTML with adjusted column styles ---
                row.innerHTML = `
                    <td style="font-weight: 600; text-transform: uppercase; color: rgb(191 191 190); word-wrap: break-word; white-space: normal; padding: 10px;">
                        <div style="display: block; line-height: 1.4; min-height: fit-content;">
                            ${torrent.title}
                        </div>
                    </td>
                    <td style="color: rgb(191 191 190); text-align: right;">${(torrent.size).toFixed(1)} GB</td>
                    <td style="color: rgb(191 191 190);">${torrent.source}</td>
                    <td style="color: rgb(191 191 190); text-align: right;">${torrent.score_breakdown.total_score}</td>
                    <td style="color: rgb(191 191 190); text-align: center;">
                        <span class="cache-status ${cacheStatusClass}" data-index="${index}">${cacheStatus}</span>
                    </td>
                    <td style="color: rgb(191 191 190); text-align: center;">
                        <button class="action-button add-button" onclick="addToRealDebrid('${torrent.magnet}', ${JSON.stringify({
                            ...torrent, // Ensure original_title is included here if present
                            year,
                            version: torrent.version || version,
                            title, // Media title
                            media_type: mediaType,
                            season: season || null,
                            episode: episode || null,
                            tmdb_id: torrent.tmdb_id || mediaId,
                            genres: genre_ids
                        }).replace(/"/g, '&quot;')})">Add</button>
                    </td>
                    <td style="color: rgb(191 191 190); text-align: center;">
                         <button class="action-button assign-button" onclick="window.location.href='${assignUrl}'">Assign</button>
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

    // Add modal-open class to body
    document.body.classList.add('modal-open');
    overlay.style.display = 'flex';
    
    // Add click handler for close button if not already added
    const closeButton = overlay.querySelector('.close-btn');
    if (closeButton) {
        closeButton.onclick = function() {
            closeOverlay();
        };
    }

    // Add click handler for overlay background if not already added
    overlay.onclick = function(event) {
        if (event.target === overlay) {
            closeOverlay();
        }
    };

    // Stop propagation on overlay content to prevent closing when clicking inside
    const overlayContentWrapper = overlay.querySelector('.overlay-content');
    if (overlayContentWrapper) {
        overlayContentWrapper.onclick = function(event) {
            event.stopPropagation();
        };
    }
    
    // Prepare data for cache check
    checkCacheStatusInBackground(null, data);
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
    
    // Close modals when clicking outside
    window.addEventListener('click', function(event) {
        const versionModal = document.getElementById('versionModal');
        const mobileActionModal = document.getElementById('mobileActionModal');
        
        // Close version modal if clicking outside modal content
        if (event.target === versionModal) {
            closeVersionModal();
        }
        
        // Close mobile action modal if clicking outside modal content
        if (event.target === mobileActionModal) {
            closeMobileActionModal();
        }
    });
    
    // Close modals when pressing Escape key
    window.addEventListener('keydown', function(event) {
        if (event.key === 'Escape') {
            const versionModal = document.getElementById('versionModal');
            const mobileActionModal = document.getElementById('mobileActionModal');
            const overlayElement = document.getElementById('overlay'); // Use a different name
            
            if (versionModal && versionModal.style.display === 'flex') {
                closeVersionModal();
            }
            
            if (mobileActionModal && mobileActionModal.style.display === 'flex') {
                closeMobileActionModal();
            }

            if (overlayElement && overlayElement.style.display === 'flex') { // Check for flex
                closeOverlay();
            }
        }
    });
    
    // Initialize the Loading object
    Loading.init();

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
            } else {
                displayTraktAuthMessage();
            }
        })
        .catch(error => {
            console.error('Trakt Auth Check Error:', error);
            get_trendingMovies(); // Fallback
            get_trendingShows();  // Fallback
        });
    
    // Setup scroll buttons using already declared variables
    if (scrollLeftBtn_mv) scrollLeftBtn_mv.addEventListener('click', () => scroll_mv('left'));
    if (scrollRightBtn_mv) scrollRightBtn_mv.addEventListener('click', () => scroll_mv('right'));
    if (scrollLeftBtn_tv) scrollLeftBtn_tv.addEventListener('click', () => scroll_tv('left'));
    if (scrollRightBtn_tv) scrollRightBtn_tv.addEventListener('click', () => scroll_tv('right'));
    
    // Initialize button states
    updateButtonStates_mv();
    updateButtonStates_tv();
    
    // Add window resize listener
    window.addEventListener('resize', () => {
        updateButtonStates_mv();
        updateButtonStates_tv();
    });
    
    // Fetch available versions
    fetchVersions();

    // Update button states after images load
    function setupImageLoadHandlers() {
        document.querySelectorAll('#movieContainer img, #showContainer img').forEach(img => {
            if (img.complete) {
                updateButtonStates_mv();
                updateButtonStates_tv();
            } else {
                img.addEventListener('load', () => {
                    updateButtonStates_mv();
                    updateButtonStates_tv();
                });
            }
        });
    }
    
    // Setup initial button states and recalculate after images load
    function initializeTrendingScrolling() {
        setTimeout(() => {
            updateButtonStates_mv();
            updateButtonStates_tv();
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
    
    // No need to reassign to global scope if already modifying window object
    // get_trendingMovies = window.get_trendingMovies;
    // get_trendingShows = window.get_trendingShows;

    // Final initialization when everything is loaded
    window.addEventListener('load', () => {
        setTimeout(() => {
            updateButtonStates_mv();
            updateButtonStates_tv();
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
    
}); // End of DOMContentLoaded

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
            // Desktop behavior - direct scrape
            selectMedia(data.tmdb_id, data.title, data.year, 'movie', null, null, false, data.genre_ids);
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
                vote_average: data.vote_average,
                backdrop_path: data.backdrop_path,
                show_overview: data.show_overview,
                poster_path: data.poster_path,
                tmdb_api_key_set: document.getElementById('tmdb_api_key_set').value === 'True'
            };
            
            // Show mobile action modal
            showMobileActionModal(item);
        } else {
            // Desktop behavior - direct scrape
            selectSeason(data.tmdb_id, data.title, data.year, 'tv', null, null, true, data.genre_ids, data.vote_average, data.backdrop_path, data.show_overview, data.tmdb_api_key_set);
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

    // --- NEW: Assign Magnet icon HTML ---
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
    // --- END NEW ---

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
        
        // --- Create DB Status Pip HTML for search results ---
        let dbStatusPipHTML = '';
        if (item.db_status && item.db_status !== 'missing') {
            dbStatusPipHTML = `<div class="db-status-pip db-status-${item.db_status}" title="Status: ${item.db_status.charAt(0).toUpperCase() + item.db_status.slice(1)}"></div>`;
        }
        // --- End DB Status Pip HTML ---
        
        // Create the container with a relative position for the request icon
        searchResDiv.innerHTML = `
            <div class="media-poster">
                <button>
                    ${item.media_type === 'show' || item.media_type === 'tv' ? '<span class="mediatype-tv">TV</span>' : '<span class="mediatype-mv">MOVIE</span>'}
                    <div class="poster-container">
                        <img src="${posterUrl}" 
                            alt="${item.title}" 
                            class="${normalizedPath.startsWith('static/') ? 'placeholder-poster' : ''}">
                        <div class="poster-overlay">
                            <h3>${item.title}</h3>
                            <p>${item.release_date ? new Date(item.release_date).getFullYear() : item.year || 'N/A'}</p>
                        </div>
                        ${requestIconHTML}
                        ${testerIconHTML}
                        ${assignMagnetIconHTML} {/* <-- Added new icon */}
                        ${dbStatusPipHTML} // <!-- Add DB Status Pip Here -->
                    </div>
                    <div class="searchresult-info" style="display: ${document.getElementById('tmdb_api_key_set').value === 'True' ? 'none' : 'block'}">
                        <h2 class="searchresult-item">${item.title}</h2>
                        <p class="searchresult-year">${item.year || 'N/A'}</p>
                    </div>
                </button>
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
                
                // Check if we're on mobile (screen width <= 768px)
                if (window.innerWidth <= 768) {
                    // Save tmdb_api_key_set in the item for later use in the modal
                    item.tmdb_api_key_set = tmdb_api_key_set;
                    item.version = version;
                    
                    // Show mobile action modal
                    showMobileActionModal(item);
                } else {
                    // Desktop behavior - direct scrape
                    if (item.media_type === 'movie') {
                        selectMedia(item.id, item.title, item.year, item.media_type, null, null, false, version);
                    } else {
                        selectSeason(item.id, item.title, item.year, item.media_type, null, null, true, item.genre_ids, item.vote_average, item.backdrop_path, item.show_overview, tmdb_api_key_set);
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
                    year: item.year || (item.release_date ? new Date(item.release_date).getFullYear() : ''),
                    media_type: item.media_type === 'show' ? 'tv' : item.media_type
                });
                window.location.href = `/scraper/scraper_tester?${params.toString()}`;
                
                return false;
            };
        }
        
        // --- NEW: Add click handler for the assign magnet icon ---
        const assignMagnetIcon = searchResDiv.querySelector('.assign-magnet-icon');
        if (assignMagnetIcon) {
            // Store data on the icon element itself for easy access
            assignMagnetIcon.dataset.id = item.id;
            assignMagnetIcon.dataset.title = item.title;
            assignMagnetIcon.dataset.year = item.year || (item.release_date ? new Date(item.release_date).getFullYear() : '');
            assignMagnetIcon.dataset.mediaType = item.media_type === 'show' ? 'tv' : item.media_type; // Normalize to 'tv'

            assignMagnetIcon.onclick = function(e) {
                e.preventDefault();
                e.stopPropagation();

                const id = this.dataset.id;
                const title = encodeURIComponent(this.dataset.title);
                const year = this.dataset.year;
                const mediaType = this.dataset.mediaType;
                const currentVersion = document.getElementById('version-select').value; // <-- Get current version

                // Construct the URL for the magnet assigner page
                const assignUrlParams = new URLSearchParams({ // <-- Create params object
                    prefill_id: id,
                    prefill_type: mediaType,
                    prefill_title: title,
                    prefill_year: year,
                    prefill_version: currentVersion // <-- Add version
                });
                const assignUrl = `/magnet/assign_magnet?${assignUrlParams.toString()}`;

                // Redirect the user
                window.location.href = assignUrl;

                return false;
            };
        }
        // --- END NEW ---

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
        
        // Skip if no magnet link or torrent URL
        if (!result.magnet_link && !result.torrent_url) {
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

    // Display "Specials" for season 0
    const seasonLabel = season_num === 0 ? 'Specials' : `Season ${season_num}`;

    seasonInfo.innerHTML = `
        <div class="season-info-container">
            <img src="/scraper/tmdb_image/w300${poster_path}" alt="${title} ${seasonLabel}" class="season-poster">
            <div class="season-details">
                <span class="show-rating">${(vote_average).toFixed(1)}</span>
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
    
    // Set up button actions
    scrapeButton.onclick = function() {
        closeMobileActionModal();
        if (item.media_type === 'movie') {
            selectMedia(item.id, item.title, item.year, item.media_type, null, null, false, item.version);
        } else {
            selectSeason(item.id, item.title, item.year, item.media_type, null, null, true, item.genre_ids, item.vote_average, item.backdrop_path, item.show_overview, item.tmdb_api_key_set);
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
