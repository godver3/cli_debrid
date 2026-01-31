// SEARCH OPTIMIZATION VERSION: 2026-01-11-v2
if (window.DEBUG) console.log('🔧 Scraper.js loaded - Search Optimizations ACTIVE (v2026-01-11-v2)');

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

            // Get selected folder from dropdown if it exists (for symlink mode)
            const folderSelect = document.getElementById('torrent-folder-select');
            if (folderSelect && folderSelect.value) {
                const selectedOption = folderSelect.options[folderSelect.selectedIndex];
                const isCustom = selectedOption.getAttribute('data-is-custom') === 'true';

                formData.append('selected_folder', folderSelect.value);
                formData.append('selected_folder_is_custom', isCustom);
                if (window.DEBUG) console.log('📁 Sending selected folder to backend:', {
                    folder: folderSelect.value,
                    isCustom: isCustom
                });
            }

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
                if (window.DEBUG) console.error('Error:', error);
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
        if (window.DEBUG) console.error('Error:', error);
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

// PHASE 1.1: Client-side episode cache (in-memory with 60-minute TTL)
const episodeCache = new Map();
const EPISODE_CACHE_TTL = 60 * 60 * 1000; // 60 minutes in milliseconds

// Client-side torrent results cache (in-memory with 30-minute TTL)
const torrentResultsCache = new Map();
const TORRENT_CACHE_TTL = 30 * 60 * 1000; // 30 minutes in milliseconds

function getCachedTorrentResults(cacheKey) {
    const cached = torrentResultsCache.get(cacheKey);
    if (!cached) return null;

    const now = Date.now();
    if (now - cached.timestamp > TORRENT_CACHE_TTL) {
        torrentResultsCache.delete(cacheKey);
        return null;
    }

    return cached.data;
}

function setCachedTorrentResults(cacheKey, data) {
    torrentResultsCache.set(cacheKey, {
        data: data,
        timestamp: Date.now()
    });
}

function getCachedEpisodes(cacheKey) {
    const cached = episodeCache.get(cacheKey);
    if (!cached) return null;

    const now = Date.now();
    if (now - cached.timestamp > EPISODE_CACHE_TTL) {
        episodeCache.delete(cacheKey);
        return null;
    }

    return cached.data;
}

function setCachedEpisodes(cacheKey, data) {
    episodeCache.set(cacheKey, {
        data: data,
        timestamp: Date.now()
    });
}

// Client-side trending cache (in-memory with 15-minute TTL)
const trendingCache = new Map();
const TRENDING_CACHE_TTL = 15 * 60 * 1000; // 15 minutes in milliseconds

function getCachedTrending(cacheKey) {
    const cached = trendingCache.get(cacheKey);
    if (!cached) return null;

    const now = Date.now();
    if (now - cached.timestamp > TRENDING_CACHE_TTL) {
        trendingCache.delete(cacheKey);
        return null;
    }

    return cached.data;
}

function setCachedTrending(cacheKey, data) {
    trendingCache.set(cacheKey, {
        data: data,
        timestamp: Date.now()
    });
}

// OPTIMIZATION: Prefetch all trending data in parallel for instant display
function prefetchTrendingData() {

    // Fetch all three trending categories in parallel
    const fetchPromises = [
        fetch('/scraper/movies_trending', { method: 'GET' })
            .then(response => response.json())
            .then(data => {
                if (data.trendingMovies) {
                    setCachedTrending('trending_movies', data.trendingMovies);
                    if (window.DEBUG) console.log('✅ Prefetched trending movies:', data.trendingMovies.length, 'items');
                }
            })
            .catch(error => { if (window.DEBUG) console.warn('Failed to prefetch movies:', error) }),

        fetch('/scraper/shows_trending', { method: 'GET' })
            .then(response => response.json())
            .then(data => {
                if (data.trendingShows) {
                    setCachedTrending('trending_shows', data.trendingShows);
                    if (window.DEBUG) console.log('✅ Prefetched trending shows:', data.trendingShows.length, 'items');
                }
            })
            .catch(error => { if (window.DEBUG) console.warn('Failed to prefetch shows:', error) }),

        fetch('/scraper/anime_trending', { method: 'GET' })
            .then(response => response.json())
            .then(data => {
                if (data.trendingAnime) {
                    setCachedTrending('trending_anime', data.trendingAnime);
                    if (window.DEBUG) console.log('✅ Prefetched trending anime:', data.trendingAnime.length, 'items');
                }
            })
            .catch(error => { if (window.DEBUG) console.warn('Failed to prefetch anime:', error) })
    ];

    // Wait for all to complete
    Promise.all(fetchPromises).then(() => {
        if (window.DEBUG) console.log('🎉 All trending data prefetched and cached!');

        // PHASE 1 FIX #3: Prefetch common searches after trending data loads
        setTimeout(prefetchCommonSearches, 2000); // 2 second delay
    });
}

// PHASE 1 FIX #3: Prefetch common/popular search terms for instant results
async function prefetchCommonSearches() {
    if (window.DEBUG) console.log('🔍 Prefetching common searches...');

    const commonSearches = [
        'Marvel',
        'Star Wars',
        'Game of Thrones',
        'Stranger Things',
        'The Last of Us',
        'The Walking Dead',
        'Breaking Bad',
        'Wednesday'
    ];

    let prefetchedCount = 0;

    for (const term of commonSearches) {
        try {
            const response = await fetch('/scraper/live_search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ search_term: term, year: null })
            });

            if (response.ok) {
                prefetchedCount++;
                if (window.DEBUG) console.log(`✅ Prefetched: "${term}"`);
            }
        } catch (err) {
            if (window.DEBUG) console.debug(`Prefetch skipped: "${term}"`);
        }

        // Small delay between requests to avoid hammering the server
        await new Promise(resolve => setTimeout(resolve, 300));
    }

    if (window.DEBUG) console.log(`🎉 Prefetched ${prefetchedCount}/${commonSearches.length} common searches!`);
}

// Infinite scroll state for episodes
let episodeScrollState = {
    allEpisodes: [],
    renderedCount: 0,
    batchSize: 12,
    isLoading: false,
    container: null,
    metadata: null
};

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

    // Update URL state with season selection
    updateEpisodeURLState(mediaId, title, season);

    // Create a container for the grid layout
    const gridContainer = document.createElement('div');
    gridContainer.style.display = 'flex';
    gridContainer.style.flexWrap = 'wrap';
    gridContainer.style.gap = '20px';
    gridContainer.style.justifyContent = 'center';
    gridContainer.id = 'episodeGridContainer';

    // Initialize infinite scroll state
    episodeScrollState.allEpisodes = episodeResults;
    episodeScrollState.renderedCount = 0;
    episodeScrollState.isLoading = false;
    episodeScrollState.container = gridContainer;
    episodeScrollState.metadata = {
        title, year, version, mediaId, mediaType, season, episode, genre_ids, isRequester
    };

    episodeResultsDiv.appendChild(gridContainer);

    // Render initial batch
    renderEpisodeBatch();

    // Set up scroll listener for infinite scroll
    setupEpisodeScrollListener();
}

// Render a batch of episodes using infinite scroll
function renderEpisodeBatch() {
    if (episodeScrollState.isLoading) return;
    if (episodeScrollState.renderedCount >= episodeScrollState.allEpisodes.length) return;

    episodeScrollState.isLoading = true;

    const start = episodeScrollState.renderedCount;
    const end = Math.min(start + episodeScrollState.batchSize, episodeScrollState.allEpisodes.length);
    const batch = episodeScrollState.allEpisodes.slice(start, end);
    const metadata = episodeScrollState.metadata;
    const gridContainer = episodeScrollState.container;

    // Use DocumentFragment for batch DOM updates
    const fragment = document.createDocumentFragment();

    batch.forEach((item, batchIndex) => {
        const episodeDiv = document.createElement('div');
        episodeDiv.className = 'episode';
        var options = {year: 'numeric', month: 'long', day: 'numeric' };
        var date = item.air_date ? new Date(item.air_date) : null;

        // Calculate global index for eager loading (first 12 episodes total)
        const globalIndex = start + batchIndex;
        const loadingAttr = globalIndex < 12 ? 'eager' : 'lazy';

        // PHASE: Keep all posters - no filtering, show placeholders for missing images
        episodeDiv.innerHTML = `
            <button ${metadata.isRequester ? 'disabled' : ''}><span class="episode-rating">${(item.vote_average || 0).toFixed(1)}</span>
            <img src="${item.still_path ? `/scraper/tmdb_image/w300${item.still_path}` : '/static/image/placeholder-horizontal.png'}"
                alt="${item.episode_title || ''}"
                loading="${loadingAttr}"
                class="${item.still_path ? '' : 'placeholder-episode'}">
            <div class="episode-info">
                <h2 class="episode-title">${item.episode_num}. ${item.episode_title || ''}</h2>
                <p class="episode-sub">${date ? date.toLocaleDateString("en-US", options) : 'Air date unknown'}</p>
            </div></button>
        `;

        // Only add click handler for non-requester users
        if (!metadata.isRequester) {
            episodeDiv.onclick = function() {
                const content = {
                    mediaId: item.id,
                    title: item.title,
                    year: item.year,
                    mediaType: item.media_type,
                    season: item.season_num,
                    episode: item.episode_num,
                    multi: item.multi,
                    genre_ids: metadata.genre_ids
                };
                showScrapeVersionModal(content);
            };
        } else {
            // Apply visual styling to show it's not clickable for requesters
            episodeDiv.style.cursor = 'default';
            episodeDiv.style.opacity = '0.8';
        }

        fragment.appendChild(episodeDiv);
    });

    // Append all episodes at once using DocumentFragment
    gridContainer.appendChild(fragment);

    episodeScrollState.renderedCount = end;
    episodeScrollState.isLoading = false;

    if (window.DEBUG) console.log(`Rendered episodes ${start + 1}-${end} of ${episodeScrollState.allEpisodes.length}`);
}

// Handle infinite scroll - load more episodes when nearing bottom
let episodeScrollListener = null;
function setupEpisodeScrollListener() {
    // Remove previous listener if exists
    if (episodeScrollListener) {
        window.removeEventListener('scroll', episodeScrollListener);
    }

    // Simple scroll handler - load more when 500px from bottom
    episodeScrollListener = function() {
        const scrollPosition = window.innerHeight + window.scrollY;
        const pageHeight = document.documentElement.scrollHeight;
        const triggerDistance = 500;

        if (scrollPosition >= pageHeight - triggerDistance) {
            renderEpisodeBatch();
        }
    };

    window.addEventListener('scroll', episodeScrollListener);
}

// PHASE 2.3: Update URL state for episode view
function updateEpisodeURLState(mediaId, title, season) {
    const url = new URL(window.location);
    url.searchParams.set('media_id', mediaId);
    url.searchParams.set('title', title);
    url.searchParams.set('season', season);
    url.searchParams.set('view', 'episodes');

    window.history.pushState({}, '', url);
    if (window.DEBUG) console.log('Episode URL state updated:', url.search);
}

// Back button functionality
function showBackButton() {
    const backButtonContainer = document.getElementById('back-button-container');
    if (backButtonContainer) {
        backButtonContainer.style.display = 'block';
    }
}

function hideBackButton() {
    const backButtonContainer = document.getElementById('back-button-container');
    if (backButtonContainer) {
        backButtonContainer.style.display = 'none';
    }
}

function goBackToTrending() {
    // Hide back button
    hideBackButton();
    
    // Show trending container and hide other sections
    toggleResultsVisibility('get_trendingMovies');
    
    // Clear search form
    const searchForm = document.querySelector('#search-form input[name="search_term"]');
    if (searchForm) {
        searchForm.value = '';
    }
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
        // Show back button for episode results
        showBackButton();
    }
    if (section === 'displaySearchResults') {
        trendingContainer.style.display = 'none';
        searchResult.style.display = 'none';
        searchResults.style.display = 'block';
        seasonResults.style.display = 'none';
        episodeResultsDiv.style.display = 'none';
        // Show back button for search results
        showBackButton();
    }
    if (section === 'get_trendingMovies') {
        trendingContainer.style.display = 'block';
        searchResult.style.display = 'none';
        searchResults.style.display = 'none';
        seasonResults.style.display = 'none';
        episodeResultsDiv.style.display = 'none';
        // Hide back button when showing trending
        hideBackButton();
    }
}

// Helper function to format bitrate information
function formatBitrate(bitrate) {
    if (!bitrate || bitrate === 0) {
        return 'Bitrate: N/A';
    }
    const bitrateKbps = parseFloat(bitrate);
    const bitrateMbps = (bitrateKbps / 1000).toFixed(2);
    return `Bitrate: ~${bitrateMbps} Mbps`;
}

// Helper function to format bitrate for inline display (mobile)
function formatBitrateInline(bitrate) {
    if (!bitrate || bitrate === 0) {
        return 'N/A mbps';
    }
    const bitrateKbps = parseFloat(bitrate);
    const bitrateMbps = (bitrateKbps / 1000).toFixed(1);
    return `~${bitrateMbps} mbps`;
}

async function displayTorrentResults(data, title, year, version, mediaId, mediaType, season, episode, genre_ids, searchDuration = 0) {
    hideLoadingState();
    const overlay = document.getElementById('overlay');
    const overlayContent = document.getElementById('overlayContent');

    // data is now the full object: { torrent_results: [...], filtered_out_torrent_results: [...] }
    const passedTorrents = data.torrent_results || [];
    const filteredOutTorrents = data.filtered_out_torrent_results || [];

    const allDisplayItems = passedTorrents.map(t => ({ ...t, __isActuallyFilteredOut: false }))
                                     .concat(filteredOutTorrents.map(t => ({ ...t, __isActuallyFilteredOut: true })));

    const mediaQuery = window.matchMedia('(max-width: 1024px)');
    async function handleScreenChange(e) {
        if (e.matches) { // Mobile view
            overlayContent.innerHTML = `
                <h3>
                    Torrent Results for ${title} (${year})
                </h3>`;
            const gridContainer = document.createElement('div');
            gridContainer.style.display = 'flex';
            gridContainer.style.flexWrap = 'wrap';
            gridContainer.style.gap = '15px';
            gridContainer.style.justifyContent = 'center';

            allDisplayItems.forEach((torrent, index) => {
                const isFilteredOut = torrent.__isActuallyFilteredOut;
                const torResDiv = document.createElement('div');
                torResDiv.className = 'torresult' + (isFilteredOut ? ' filtered-out-item' : '');
                
                // Format bitrate for inline display in mobile
                const bitrateInline = formatBitrateInline(torrent.bitrate);
                
                torResDiv.innerHTML = `
                    <button ${isFilteredOut ? 'style="cursor:pointer; opacity:0.7;"' : ''}>
                    <div class="torresult-info">
                        <p class="torresult-title">${torrent.title || torrent.original_title || 'N/A'}</p>
                        <p class="torresult-item">${(torrent.size || 0).toFixed(1)} GB | ${bitrateInline} | ${isFilteredOut ? (torrent.filter_reason || 'Filtered') : (torrent.score_breakdown?.total_score || 'N/A')}</p>
                        <p class="torresult-item">${torrent.source || 'N/A'}</p>
                        <span class="cache-status ${torrent.cached === 'Yes' ? 'cached' :
                                      torrent.cached === 'No' ? 'not-cached' :
                                      torrent.cached === 'Not Checked' ? 'not-checked' :
                                      torrent.cached === 'N/A' ? 'check-unavailable' : 'unknown'}" data-index="${index}">${torrent.cached || 'N/A'}</span>
                    </div>
                    </button>
                    ${torrent.cached === 'Yes' ? '<div class="mobile-cache-check">✓</div>' : ''}
                    <div class="assign-magnet-icon" title="Assign Magnet Link">
                        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path>
                            <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path>
                        </svg>
                    </div>            
                `;

                // Assign Magnet click handler for mobile cards
                const assignIcon = torResDiv.querySelector('.assign-magnet-icon');
                if (assignIcon) {
                    assignIcon.onclick = function(e) {
                        e.preventDefault();
                        e.stopPropagation();
                        const assignUrlParams = new URLSearchParams({
                            prefill_id: mediaId,
                            prefill_type: mediaType,
                            prefill_title: title,
                            prefill_year: year,
                            prefill_version: version,
                        });
                        if (torrent.magnet) {
                            assignUrlParams.set('prefill_magnet', torrent.magnet);
                        }
                        // Set selection type based on whether this is an episode or season pack
                        if (season) {
                            assignUrlParams.set('prefill_seasons', season);
                            if (episode) {
                                // Individual episode
                                assignUrlParams.set('prefill_selection', 'episode');
                                assignUrlParams.set('prefill_episode', episode);
                            } else {
                                // Season pack
                                assignUrlParams.set('prefill_selection', 'seasons');
                            }
                        }
                        const assignUrl = `/magnet/assign_magnet?${assignUrlParams.toString()}`;
                        window.location.href = assignUrl;
                        return false;
                    };
                }
                
                // Add click handler for all items (both filtered and non-filtered)
                torResDiv.onclick = function() {
                    const torrentData = {
                        title: title, year: year, version: version, media_type: mediaType,
                        season: season || null, episode: episode || null, tmdb_id: mediaId,
                        genres: genre_ids, original_title: torrent.original_title // Pass original_title
                    };
                    
                    if (isFilteredOut) {
                        // Show confirmation dialog for filtered items
                        const confirmationMessage = `This item was filtered for the following reason:\n\n'${torrent.filter_reason || 'No specific reason provided'}'.\n\nDo you want to add it anyway?`;
                        showPopup({
                            type: POPUP_TYPES.CONFIRM,
                            title: 'Add Filtered Item?',
                            message: confirmationMessage,
                            confirmText: 'Add Anyway',
                            onConfirm: () => {
                                addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                            }
                        });
                    } else {
                        // Standard behavior for non-filtered items
                        addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                    }
                };
                
                gridContainer.appendChild(torResDiv);
            });
            overlayContent.appendChild(gridContainer);

        } else { // Desktop view
            // Check current theme using the same storage key as theme_switcher.js
            const currentTheme = localStorage.getItem('selectedTheme') || 'classic';
            if (window.DEBUG) console.log('🎨 Torrent modal theme:', currentTheme);
            
            if (currentTheme === 'tangerine') {
                // TANGERINE THEME - Modern Redesign
            overlayContent.innerHTML = '';
            
            // Create modal header
            const modalHeader = document.createElement('div');
            modalHeader.className = 'torrent-modal-header';
            
            // Get unique scrapers count
            const scrapers = new Set(allDisplayItems.map(t => t.source?.split(' - ')[0]).filter(Boolean));
            const scraperCount = scrapers.size;
            
            modalHeader.innerHTML = `
                <div class="torrent-modal-title-section">
                    <h3>Torrent Results for ${title} (${year})</h3>
                    <div class="torrent-stats">
                        <span>${allDisplayItems.length} results</span>
                        <span>Search: ${searchDuration}ms</span>
                        <span>${scraperCount} scraper${scraperCount !== 1 ? 's' : ''}</span>
                    </div>
                </div>
            `;
            overlayContent.appendChild(modalHeader);
            
            // Create filter section
            const filterSection = document.createElement('div');
            filterSection.className = 'torrent-filter-section';

            // Get versions from either availableVersions array or the page's version select dropdown
            let versionsToUse = [];

            // Always try to get from page dropdown first as it's server-rendered and always available
            const pageVersionSelect = document.getElementById('version-select');
            if (pageVersionSelect) {
                versionsToUse = Array.from(pageVersionSelect.options).map(opt => opt.value);
                if (window.DEBUG) console.log('✅ Using versions from page dropdown:', versionsToUse);
            } else if (availableVersions.length > 0) {
                // Fallback to availableVersions if page dropdown not found
                versionsToUse = availableVersions;
                if (window.DEBUG) console.log('✅ Using versions from API:', versionsToUse);
            } else {
                // Last resort: just use the current version
                versionsToUse = [version];
                if (window.DEBUG) console.log('⚠️ No versions found, using current version only:', version);
            }

            // Debug log
            if (window.DEBUG) console.log('📦 Final versions for dropdown:', versionsToUse, 'Current version:', version);

            // Strip asterisks from version for comparison (e.g., "4K Remux*" -> "4K Remux")
            const cleanVersion = version.replace(/\*/g, '');

            // Generate version options HTML
            const versionOptionsHTML = versionsToUse.map(v =>
                `<option value="${v}" ${v === cleanVersion ? 'selected' : ''}>${v}</option>`
            ).join('');

            // Fetch symlink folders and build folder dropdown HTML
            let folderDropdownHTML = '';
            try {
                const foldersResponse = await fetch('/scraper/get_symlink_folders');
                const foldersData = await foldersResponse.json();

                if (foldersData.enabled && foldersData.folders && foldersData.folders.length > 0) {
                    const folderSettings = foldersData.folder_settings;

                    // Determine which folder to auto-select based on genres
                    let autoSelectedFolder = null;

                    // Convert genre_ids to lowercase array for checking
                    let genreList = [];
                    if (Array.isArray(genre_ids)) {
                        genreList = genre_ids.map(g => String(g).trim().toLowerCase());
                    } else if (typeof genre_ids === 'string') {
                        genreList = genre_ids.split(',').map(g => g.trim().toLowerCase());
                    } else if (typeof genre_ids === 'number') {
                        genreList = [String(genre_ids)];
                    }

                    if (window.DEBUG) console.log('📁 Folder auto-select - Raw genre_ids:', genre_ids);
                    if (window.DEBUG) console.log('📁 Folder auto-select - Parsed genreList:', genreList);
                    if (window.DEBUG) console.log('📁 Folder auto-select - genre_ids type:', typeof genre_ids);

                    // For anime: TMDB uses "animation" genre ID (16) or name, not "anime"
                    // Check for both "anime" and "animation"
                    const isAnime = genreList.some(g => {
                        const matches = g.includes('anime') || g.includes('animation') || g === '16';
                        if (matches && window.DEBUG) console.log(`📁 Anime match found: "${g}"`);
                        return matches;
                    });

                    // For documentary: TMDB uses "documentary" genre ID (99) or name
                    const isDocumentary = genreList.some(g => {
                        const matches = g.includes('documentary') || g === '99';
                        if (matches && window.DEBUG) console.log(`📁 Documentary match found: "${g}"`);
                        return matches;
                    });

                    if (window.DEBUG) console.log('📁 Folder auto-select - Detection results:', {
                        isAnime,
                        isDocumentary,
                        mediaType,
                        animeEnabled: folderSettings.enable_separate_anime_folders,
                        documentaryEnabled: folderSettings.enable_separate_documentary_folders
                    });

                    // Determine expected folder name based on media type and genres
                    if (mediaType === 'movie') {
                        if (isAnime && folderSettings.enable_separate_anime_folders) {
                            autoSelectedFolder = folderSettings.anime_movies_folder_name;
                        } else if (isDocumentary && folderSettings.enable_separate_documentary_folders) {
                            autoSelectedFolder = folderSettings.documentary_movies_folder_name;
                        } else {
                            autoSelectedFolder = folderSettings.movies_folder_name;
                        }
                    } else { // TV show
                        if (isAnime && folderSettings.enable_separate_anime_folders) {
                            autoSelectedFolder = folderSettings.anime_tv_shows_folder_name;
                        } else if (isDocumentary && folderSettings.enable_separate_documentary_folders) {
                            autoSelectedFolder = folderSettings.documentary_tv_shows_folder_name;
                        } else {
                            autoSelectedFolder = folderSettings.tv_shows_folder_name;
                        }
                    }

                    if (window.DEBUG) console.log('📁 Auto-selected folder:', autoSelectedFolder);

                    // Filter folders based on media type
                    const filteredFolders = foldersData.folders.filter(folder => {
                        if (folder.is_custom) {
                            // Custom folders appear for both movies and TV shows
                            return true;
                        }

                        // Standard folders - filter based on media type
                        const folderNameLower = folder.name.toLowerCase();
                        if (mediaType === 'movie') {
                            return folderNameLower.includes('movie') ||
                                   (folderNameLower === folderSettings.movies_folder_name.toLowerCase());
                        } else { // TV show
                            return folderNameLower.includes('show') || folderNameLower.includes('tv') ||
                                   (folderNameLower === folderSettings.tv_shows_folder_name.toLowerCase());
                        }
                    });

                    if (window.DEBUG) console.log('📁 Available folders:', foldersData.folders);
                    if (window.DEBUG) console.log('📁 Filtered folders:', filteredFolders);

                    if (filteredFolders.length > 0) {
                        const folderOptionsHTML = filteredFolders.map(folder => {
                            const isSelected = folder.name === autoSelectedFolder;
                            if (window.DEBUG) console.log(`📁 Checking folder "${folder.name}" === "${autoSelectedFolder}"? ${isSelected}`);
                            const displayName = folder.is_custom ?
                                `${folder.name} (${mediaType === 'movie' ? folderSettings.movies_folder_name : folderSettings.tv_shows_folder_name})` :
                                folder.name;
                            return `<option value="${folder.name}" data-is-custom="${folder.is_custom}" ${isSelected ? 'selected' : ''}>${displayName}</option>`;
                        }).join('');

                        folderDropdownHTML = `
                            <div class="torrent-folder-dropdown-wrapper">
                                <label for="torrent-folder-select">Folder:</label>
                                <select id="torrent-folder-select" class="torrent-folder-select">
                                    ${folderOptionsHTML}
                                </select>
                            </div>
                        `;
                    }
                }
            } catch (error) {
                if (window.DEBUG) console.error('Error fetching symlink folders:', error);
                // Continue without folder dropdown if there's an error
            }

            filterSection.innerHTML = `
                <div class="torrent-filter-input-wrapper">
                    ${createSearchIcon()}
                    <input type="text" class="torrent-filter-input" id="torrent-filter-input" placeholder="Filter results...">
                </div>
                <div class="torrent-version-dropdown-wrapper">
                    <label for="torrent-version-select">Version:</label>
                    <select id="torrent-version-select" class="torrent-version-select">
                        ${versionOptionsHTML}
                    </select>
                </div>
                ${folderDropdownHTML}
                <div class="torrent-filter-toggles">
                    <label class="torrent-filter-checkbox">
                        <input type="checkbox" id="show-filtered-checkbox">
                        <span>Show filtered</span>
                    </label>
                    <label class="torrent-filter-checkbox">
                        <input type="checkbox" id="show-filename-checkbox">
                        <span>Filename</span>
                    </label>
                </div>
            `;
            overlayContent.appendChild(filterSection);

            // Add version change handler
            const versionSelect = document.getElementById('torrent-version-select');
            if (versionSelect) {
                versionSelect.addEventListener('change', async function(e) {
                    const newVersion = e.target.value;
                    if (window.DEBUG) console.log(`Version changed from ${version} to ${newVersion}`);

                    // Close current overlay
                    closeOverlay();

                    // Trigger new search with new version
                    // Note: multi value defaults to true for TV shows
                    const multi = mediaType === 'tv' ? true : false;
                    await selectMedia(mediaId, title, year, mediaType, season, episode, multi, genre_ids, newVersion);
                });
            }
            
            // Create table
            const table = document.createElement('table');

            const thead = document.createElement('thead');
            thead.innerHTML = `
                <tr>
                    <th class="sortable" style="width: 40%;">Release</th>
                    <th class="sortable text-right" style="width: 10%;">Size</th>
                    <th style="width: 12%;">Scraper</th>
                    <th class="sortable text-right" style="width: 10%;">Score</th>
                    <th class="text-center" style="width: 8%;">Cache</th>
                    <th class="text-center" style="width: 10%;">Add</th>
                    <th class="text-center" style="width: 10%;">Assign</th>
                </tr>
            `;
            table.appendChild(thead);

            const tbody = document.createElement('tbody');
            allDisplayItems.forEach((torrent, index) => {
                const isFilteredOut = torrent.__isActuallyFilteredOut;
                const cacheStatus = torrent.cached || 'Unknown';
                
                if (torrent.magnet) {
                    torrent.magnet_link = torrent.magnet;
                }

                const assignUrlParams = new URLSearchParams({
                    prefill_id: mediaId, prefill_type: mediaType, prefill_title: title,
                    prefill_year: year, prefill_magnet: torrent.magnet, prefill_version: version
                });
                
                if (season) {
                    assignUrlParams.set('prefill_seasons', season);
                    if (episode) {
                        assignUrlParams.set('prefill_selection', 'episode');
                        assignUrlParams.set('prefill_episode', episode);
                    } else {
                        assignUrlParams.set('prefill_selection', 'seasons');
                    }
                }
                const assignUrl = `/magnet/assign_magnet?${assignUrlParams.toString()}`;

                // Extract quality tags from title
                const qualityTags = extractQualityTags(torrent.title || torrent.original_title || '');
                const qualityBadgesHtml = qualityTags.map(tag => createQualityBadge(tag)).join('');
                
                // Use clean title from header, store filename for toggle
                const ShowInfo = `${season ? `<span class="season-info">S${season.toString().padStart(2, '0')}` : ''}${(torrent.parsed_info.seasons).length > 1 ? ` - ${(torrent.parsed_info.seasons).length}</span>` : `</span>`} ${episode ? `<span class="ds-episode-info"> E${episode.toString().padStart(2, '0')}</span>`: ''}`;
                const cleanTitle = `${title} (${year})${ShowInfo ? ` ${ShowInfo}` : ''}`;
                const filename = torrent.title || torrent.original_title || 'N/A';
                
                // Get score and color class
                const score = torrent.score_breakdown?.total_score || 0;
                const scoreClass = getScoreColorClass(score);
                const scoreDisplay = isFilteredOut ? (torrent.filter_reason || 'Filtered') : (score || 'N/A');
                
                // Create cache icon
                const cacheIconHtml = createCacheIcon(cacheStatus);
                
                const row = document.createElement('tr');
                if (isFilteredOut) {
                    row.classList.add('filtered-row');
                }

                row.innerHTML = `
                    <td>
                        <div class="release-title-wrapper">
                            <div class="release-title" data-clean-title="${cleanTitle.replace(/"/g, '&quot;')}" data-filename="${filename.replace(/"/g, '&quot;')}">${cleanTitle}</div>
                            <div class="release-tags">${qualityBadgesHtml}</div>
                        </div>
                    </td>
                    <td class="text-right">${(torrent.size || 0).toFixed(1)} GB</td>
                    <td>
                        ${(torrent.source || 'N/A').split(' - ').map(p => `<span class="source-badge">${p.trim()}</span>`).join('')}
                    </td>
                    <td class="text-right">
                        <span class="score-value ${scoreClass}" ${isFilteredOut ? `title="${torrent.filter_reason || 'Filtered'}"` : ''}>${scoreDisplay}</span>
                    </td>
                    <td class="text-center cache-cell" data-torrent-index="${index}">
                        <span class="cache-icon-wrapper">${cacheIconHtml}</span>
                    </td>
                    <td class="text-center">
                        <button class="action-button add-button">
                            ${createDownloadIcon()}
                            ADD
                        </button>
                    </td>
                    <td class="text-center">
                        <button class="action-button assign-button">
                            ${createExternalLinkIcon()}
                            ASSIGN
                        </button>
                    </td>
                `;
                tbody.appendChild(row);

                // Add event listeners to the buttons
                const addButton = row.querySelector('.add-button');
                const assignButton = row.querySelector('.assign-button');

                if (isFilteredOut) {
                    addButton.onclick = function() {
                        const confirmationMessage = `This item was filtered for the following reason:\n\n'${torrent.filter_reason || 'No specific reason provided'}'.\n\nDo you want to add it anyway?`;
                        showPopup({
                            type: POPUP_TYPES.CONFIRM,
                            title: 'Add Filtered Item?',
                            message: confirmationMessage,
                            confirmText: 'Add Anyway',
                            onConfirm: () => {
                                addToRealDebrid(torrent.magnet, {
                                    ...torrent, year, version: torrent.version || version, title,
                                    media_type: mediaType, season: season || null, episode: episode || null,
                                    tmdb_id: torrent.tmdb_id || mediaId, genres: genre_ids, original_title: torrent.original_title
                                });
                            }
                        });
                    };
                } else {
                    addButton.onclick = function() {
                        addToRealDebrid(torrent.magnet, {
                            ...torrent, year, version: torrent.version || version, title,
                            media_type: mediaType, season: season || null, episode: episode || null,
                            tmdb_id: torrent.tmdb_id || mediaId, genres: genre_ids, original_title: torrent.original_title
                        });
                    };
                }
                
                assignButton.onclick = function() {
                    window.location.href = assignUrl;
                };
            });
            table.appendChild(tbody);
            overlayContent.appendChild(table);
            
            // Add filter functionality
            const filterInput = overlayContent.querySelector('#torrent-filter-input');
            const showFilteredCheckbox = overlayContent.querySelector('#show-filtered-checkbox');
            const showFilenameCheckbox = overlayContent.querySelector('#show-filename-checkbox');
            
            // Filename toggle functionality
            if (showFilenameCheckbox) {
                // Load saved state from localStorage
                const savedFilenameState = localStorage.getItem('torrentShowFilename') === 'true';
                showFilenameCheckbox.checked = savedFilenameState;
                
                // Apply saved state on initial load
                if (savedFilenameState) {
                    const rows = overlayContent.querySelectorAll('tbody tr');
                    rows.forEach(row => {
                        const titleDiv = row.querySelector('.release-title');
                        if (titleDiv) {
                            const filename = titleDiv.getAttribute('data-filename');
                            titleDiv.textContent = filename;
                        }
                    });
                }
                
                showFilenameCheckbox.addEventListener('change', function() {
                    // Save state to localStorage
                    localStorage.setItem('torrentShowFilename', this.checked);
                    
                    const rows = overlayContent.querySelectorAll('tbody tr');
                    rows.forEach(row => {
                        const titleDiv = row.querySelector('.release-title');
                        if (titleDiv) {
                            const cleanTitle = titleDiv.getAttribute('data-clean-title');
                            const filename = titleDiv.getAttribute('data-filename');
                            if (this.checked) {
                                titleDiv.textContent = filename;
                            } else {
                                titleDiv.innerHTML = cleanTitle;
                            }
                        }
                    });
                });
            }
            
            function applyFilters() {
                const filterText = filterInput.value.toLowerCase();
                const showFiltered = showFilteredCheckbox.checked;
                
                const rows = tbody.querySelectorAll('tr');
                rows.forEach(row => {
                    const isFiltered = row.classList.contains('filtered-row');
                    const text = row.textContent.toLowerCase();
                    const matchesSearch = !filterText || text.includes(filterText);
                    const shouldShow = matchesSearch && (showFiltered || !isFiltered);
                    
                    row.style.display = shouldShow ? '' : 'none';
                });
            }
            
            filterInput.addEventListener('input', applyFilters);
            showFilteredCheckbox.addEventListener('change', applyFilters);
            
            // Hide filtered items by default
            applyFilters();
            
            // Add close button handlers
            overlayContent.querySelectorAll('.close-modal-btn').forEach(btn => {
                btn.onclick = () => closeOverlay();
            });
            
            } else {
                // CLASSIC THEME - Original Desktop Table
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

                    const assignUrlParams = new URLSearchParams({
                        prefill_id: mediaId, prefill_type: mediaType, prefill_title: title,
                        prefill_year: year, prefill_magnet: torrent.magnet, prefill_version: version
                    });
                    if (season) {
                        assignUrlParams.set('prefill_seasons', season);
                        if (episode) {
                            assignUrlParams.set('prefill_selection', 'episode');
                            assignUrlParams.set('prefill_episode', episode);
                        } else {
                            assignUrlParams.set('prefill_selection', 'seasons');
                        }
                    }
                    const assignUrl = `/magnet/assign_magnet?${assignUrlParams.toString()}`;

                    const bitrateTooltip = formatBitrate(torrent.bitrate);

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
                        <td style="color: rgb(191 191 190); text-align: right;" data-bitrate="${bitrateTooltip}">${(torrent.size || 0).toFixed(1)} GB</td>
                        <td id="scraper-source" style="color: rgb(191 191 190);">
                            <div class="source-container">
                                ${(torrent.source || 'N/A').split(' - ').map(part => `<span class="source-badge">${part.trim()}</span>`).join('')}
                            </div>
                        </td>
                        <td style="color: rgb(191 191 190); text-align: right;" ${isFilteredOut ? `data-tooltip="${torrent.filter_reason || 'Filtered'}"` : ''}>${isFilteredOut ? (torrent.filter_reason || 'Filtered') : (torrent.score_breakdown?.total_score || 'N/A')}</td>
                        <td style="color: rgb(191 191 190); text-align: center;">
                            <span class="cache-status ${cacheStatusClass}" data-index="${index}">${cacheStatus}</span>
                        </td>
                        <td style="color: rgb(191 191 190); text-align: center;">
                            <button class="action-button add-button">Add</button>
                        </td>
                        <td style="color: rgb(191 191 190); text-align: center;">
                             <button class="action-button assign-button" onclick="window.location.href='${assignUrl}'">Assign</button>
                        </td>
                    `;
                    tbody.appendChild(row);

                    const addButton = row.querySelector('.add-button');
                    const assignButton = row.querySelector('.assign-button');

                    if (isFilteredOut) {
                        addButton.onclick = function() {
                            const confirmationMessage = `This item was filtered for the following reason:\n\n'${torrent.filter_reason || 'No specific reason provided'}'.\n\nDo you want to add it anyway?`;
                            showPopup({
                                type: POPUP_TYPES.CONFIRM,
                                title: 'Add Filtered Item?',
                                message: confirmationMessage,
                                confirmText: 'Add Anyway',
                                onConfirm: () => {
                                    addToRealDebrid(torrent.magnet, {
                                        ...torrent, year, version: torrent.version || version, title,
                                        media_type: mediaType, season: season || null, episode: episode || null,
                                        tmdb_id: torrent.tmdb_id || mediaId, genres: genre_ids, original_title: torrent.original_title
                                    });
                                }
                            });
                        };
                    } else {
                        addButton.onclick = function() {
                            addToRealDebrid(torrent.magnet, {
                                ...torrent, year, version: torrent.version || version, title,
                                media_type: mediaType, season: season || null, episode: episode || null,
                                tmdb_id: torrent.tmdb_id || mediaId, genres: genre_ids, original_title: torrent.original_title
                            });
                        };

                        assignButton.onclick = function() {
                            window.location.href = assignUrl;
                        };
                    }
                });
                table.appendChild(tbody);
                overlayContent.appendChild(table);
            }
        }
    }
    mediaQuery.addListener(handleScreenChange); // Add listener
    handleScreenChange(mediaQuery); // Initial call

    document.body.classList.add('modal-open');
    overlay.style.display = 'flex';
    
    // Hide back button when overlay is shown
    hideBackButton();
    
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


    // Check cache status in background
    checkCacheStatusInBackground(null, allDisplayItems);
    
    // Setup tooltips for filter reasons
    setupFilterReasonTooltips();
}

// Function to setup tooltip positioning for filter reasons
function setupFilterReasonTooltips() {
    // Desktop tooltips
    const filteredCells = document.querySelectorAll('.filtered-out-item td:nth-child(4)');
    
    filteredCells.forEach(cell => {
        cell.addEventListener('mouseenter', function(e) {
            const tooltipText = this.getAttribute('data-tooltip');
            if (tooltipText) {
                const rect = this.getBoundingClientRect();
                
                // Create tooltip element
                const tooltipEl = document.createElement('div');
                tooltipEl.className = 'filter-reason-tooltip';
                tooltipEl.textContent = tooltipText;
                tooltipEl.style.cssText = `
                    position: fixed;
                    background-color: #2a2a2a;
                    color: #f4f4f4;
                    padding: 8px 12px;
                    border-radius: 6px;
                    font-size: 0.85em;
                    font-style: normal;
                    white-space: normal;
                    word-wrap: break-word;
                    max-width: 300px;
                    z-index: 99999;
                    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
                    border: 1px solid rgba(255, 255, 255, 0.1);
                    pointer-events: none;
                    left: ${rect.left + rect.width / 2}px;
                    top: ${rect.top - 10}px;
                    transform: translateX(-50%);
                `;
                
                document.body.appendChild(tooltipEl);
                this._tooltip = tooltipEl;
            }
        });
        
        cell.addEventListener('mouseleave', function(e) {
            if (this._tooltip) {
                this._tooltip.remove();
                this._tooltip = null;
            }
        });
    });
    
    // Mobile tooltips
    const mobileFilteredItems = document.querySelectorAll('.torresult.filtered-out-item .torresult-item:first-of-type');
    
    mobileFilteredItems.forEach(item => {
        item.addEventListener('mouseenter', function(e) {
            const tooltipText = this.getAttribute('data-tooltip');
            if (tooltipText) {
                const rect = this.getBoundingClientRect();
                
                // Create tooltip element
                const tooltipEl = document.createElement('div');
                tooltipEl.className = 'filter-reason-tooltip';
                tooltipEl.textContent = tooltipText;
                tooltipEl.style.cssText = `
                    position: fixed;
                    background-color: #2a2a2a;
                    color: #f4f4f4;
                    padding: 8px 12px;
                    border-radius: 6px;
                    font-size: 0.85em;
                    font-style: normal;
                    white-space: normal;
                    word-wrap: break-word;
                    max-width: 300px;
                    z-index: 99999;
                    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
                    border: 1px solid rgba(255, 255, 255, 0.1);
                    pointer-events: none;
                    left: ${rect.left + rect.width / 2}px;
                    top: ${rect.top - 10}px;
                    transform: translateX(-50%);
                `;
                
                document.body.appendChild(tooltipEl);
                this._tooltip = tooltipEl;
            }
        });
        
        item.addEventListener('mouseleave', function(e) {
            if (this._tooltip) {
                this._tooltip.remove();
                this._tooltip = null;
            }
        });
    });
}

// Function to close the overlay
function closeOverlay() {
    const overlayElement = document.getElementById('overlay'); // Use overlayElement here as well
    if (overlayElement) {
        overlayElement.style.display = 'none';
        document.body.classList.remove('modal-open');
        
        // Show back button when overlay is closed (since overlay shows torrent results)
        // Check if we're not on the trending page
        const trendingContainer = document.getElementById('trendingContainer');
        if (trendingContainer && trendingContainer.style.display === 'none') {
            showBackButton();
        }
    }
}

// Add event listeners when DOM content is loaded
document.addEventListener('DOMContentLoaded', async function() {
    // Only run scraper page initialization if we're on the scraper page
    // Check if scraper-specific elements exist
    const scraperContainer = document.getElementById('scraper-container');
    if (!scraperContainer) {
        // We're not on the scraper page, skip initialization
        return;
    }

    // NOTE: Search form behavior is now handled by Phase 2.3 live search below
    // The old searchMedia() function is replaced by performLiveSearch() for faster results

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
    
    // Set up back button
    const backButton = document.getElementById('back-button');
    if (backButton) {
        backButton.addEventListener('click', goBackToTrending);
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
            if (window.DEBUG) console.log(`Allow Specials set to: ${this.checked}`);
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
                get_allTrending(); // Single combined call
            } else {
                displayTraktAuthMessage();
            }
        })
        .catch(error => {
            if (window.DEBUG) console.error('Trakt Auth Check Error:', error);
            get_allTrending(); // Fallback uses combined call
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
            if (window.DEBUG) console.log(`Auto-searching for: ${searchTermFromUrl}`);
            searchInput.value = searchTermFromUrl;
            searchButton.click();
        }
    }

    // PHASE 2.3: Search on Enter key press (like Mydia)
    const searchInput = document.querySelector('#search-form input[name="search_term"]');
    if (searchInput) {
        searchInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault(); // Prevent form submission
                const searchTerm = e.target.value.trim();

                if (searchTerm.length === 0) {
                    // Clear results if search is empty
                    const resultsContainer = document.getElementById('search-results');
                    if (resultsContainer) {
                        resultsContainer.innerHTML = '';
                    }
                    // Clear URL when search is cleared
                    const url = new URL(window.location);
                    url.searchParams.delete('q');
                    url.searchParams.delete('v');
                    window.history.pushState({}, '', url);
                    return;
                }

                // Perform search when Enter is pressed
                if (searchTerm.length >= 3) {
                    performLiveSearch(searchTerm);
                }
            }
        });

        // Also handle search button click and form submit
        const searchForm = document.getElementById('search-form');
        if (searchForm) {
            searchForm.addEventListener('submit', function(e) {
                e.preventDefault();
                e.stopPropagation(); // Prevent any other submit handlers
                const searchTerm = searchInput.value.trim();
                if (searchTerm.length >= 3) {
                    performLiveSearch(searchTerm);
                }
            });
        }

        // Explicitly handle search button click
        const searchButton = document.getElementById('searchformButton');
        if (searchButton) {
            searchButton.addEventListener('click', function(e) {
                e.preventDefault();
                e.stopPropagation(); // Prevent form submission
                const searchTerm = searchInput.value.trim();
                if (searchTerm.length >= 3) {
                    performLiveSearch(searchTerm);
                }
            });
        }
    }

    // PHASE 2.3: Restore search from URL on page load
    // Note: urlParams already declared above for old search_term parameter
    const searchQuery = urlParams.get('q');
    const versionParam = urlParams.get('v');

    if (searchQuery && searchInput) {
        if (window.DEBUG) console.log('Restoring search from URL:', searchQuery);
        searchInput.value = searchQuery;

        // Restore version if present
        const versionSelect = document.getElementById('version-select');
        if (versionParam && versionSelect) {
            versionSelect.value = versionParam;
        }

        // Perform the search (don't update URL since we're loading from URL)
        performLiveSearch(searchQuery, false);
    }

    // Check for pending scraper load from discover page
    const pendingLoad = sessionStorage.getItem('pendingScraperLoad');
    if (pendingLoad) {
        try {
            const mediaData = JSON.parse(pendingLoad);
            if (window.DEBUG) console.log('Auto-loading media from discover:', mediaData);

            // Clear the sessionStorage immediately to prevent re-triggering
            sessionStorage.removeItem('pendingScraperLoad');

            // Hide search form when coming from discover
            const searchForm = document.getElementById('search-form');
            if (searchForm) {
                searchForm.style.display = 'none';
            }

            // Clean URL to prevent back button issues
            if (window.location.search.includes('from_discover=1')) {
                // Replace current history entry with clean /scraper URL
                window.history.replaceState({}, '', '/scraper');
            }

            // Store pre-fetched episodes if available (but don't display yet - need to set up UI first)
            let preFetchedEpisodes = null;
            let preFetchedSeason = null;
            if (mediaData.preFetchedEpisodes) {
                if (window.DEBUG) console.log('⚡ Pre-fetched episodes available - will display instantly after UI setup');
                if (window.DEBUG) console.log('MediaData genres:', mediaData.genre_ids);
                preFetchedEpisodes = mediaData.preFetchedEpisodes;
                preFetchedSeason = mediaData.preFetchedSeason || 1;

                // Cache the pre-fetched episodes
                const versionSelect = document.getElementById('version-select');
                const version = versionSelect ? versionSelect.value : 'Any';
                const cacheKey = `episodes:${mediaData.media_id}:${preFetchedSeason}:${version}:${mediaData.allow_specials}`;
                setCachedEpisodes(cacheKey, preFetchedEpisodes);
            }

            // Show loading state only if not using pre-fetched episodes
            if (!preFetchedEpisodes) {
                showLoadingState();
            }

            // Get version from select element
            const versionSelect = document.getElementById('version-select');
            const version = versionSelect ? versionSelect.value : 'Any';

            // Prepare form data for season selection
            const formData = new FormData();
            formData.append('media_id', mediaData.media_id);
            formData.append('title', mediaData.title);
            formData.append('year', mediaData.year);
            formData.append('media_type', mediaData.media_type);
            formData.append('multi', mediaData.multi);
            formData.append('version', version);
            formData.append('allow_specials', mediaData.allow_specials);
            if (mediaData.rating) formData.append('rating', mediaData.rating);
            if (mediaData.vote_average) formData.append('vote_average', mediaData.vote_average);
            // Convert genre_ids array to comma-separated string (not JSON)
            if (mediaData.genre_ids && Array.isArray(mediaData.genre_ids)) {
                formData.append('genre_ids', mediaData.genre_ids.join(','));
            }

            // POST to select_season endpoint to get all available seasons
            fetch('/scraper/select_season', {
                method: 'POST',
                body: formData
            })
            .then(response => response.json())
            .then(data => {
                hideLoadingState();

                if (data.error) {
                    if (window.DEBUG) console.error('Error loading season data:', data.error);
                    displayError(data.error);
                } else {
                    if (window.DEBUG) console.log('Season data loaded successfully');

                    // Get season results
                    const seasonResults = data.episode_results || data.results;
                    if (!seasonResults || seasonResults.length === 0) {
                        displayError('No season results found');
                        return;
                    }

                    // Show season results section
                    toggleResultsVisibility('displayEpisodeResults');

                    // Populate the season dropdown
                    const dropdown = document.getElementById('seasonDropdown');
                    const seasonPackButton = document.getElementById('seasonPackButton');
                    const requestSeasonButton = document.getElementById('requestSeasonButton');

                    if (!dropdown) {
                        if (window.DEBUG) console.error('Season dropdown not found');
                        return;
                    }

                    dropdown.innerHTML = '';
                    seasonResults.forEach(item => {
                        const option = document.createElement('option');
                        option.value = JSON.stringify(item);
                        option.textContent = item.season_num === 0 ? 'Specials' : `Season: ${item.season_num}`;
                        dropdown.appendChild(option);
                    });

                    // Store these in a scope accessible to the dropdown change handler
                    const genre_ids = mediaData.genre_ids || [];
                    const vote_average = mediaData.vote_average || 0;
                    const tmdb_api_key_set = document.getElementById('tmdb_api_key_set')?.value === 'True';

                    // Store show-level metadata for display
                    const showBackdropPath = mediaData.backdrop_path || null;
                    const showOverview = mediaData.overview || 'No overview available';

                    // Debug logging
                    if (window.DEBUG) console.log('=== Season Dropdown Setup ===');
                    if (window.DEBUG) console.log('genre_ids from mediaData:', genre_ids);
                    if (window.DEBUG) console.log('vote_average from mediaData:', vote_average);
                    if (window.DEBUG) console.log('backdrop_path from mediaData:', showBackdropPath);
                    if (window.DEBUG) console.log('overview from mediaData:', showOverview);

                    // Check if user is a requester
                    const isRequesterEl = document.getElementById('is_requester');
                    const isRequester = isRequesterEl && isRequesterEl.value === 'True';

                    // Add change event listener to dropdown
                    dropdown.addEventListener('change', function() {
                        const selectedItem = JSON.parse(this.value);
                        const optionText = this.options[this.selectedIndex].textContent;
                        let displayedSeasonNum = selectedItem.season_num;

                        if (optionText.startsWith('Season: ')) {
                            const extractedSeason = parseInt(optionText.replace('Season: ', ''));
                            if (!isNaN(extractedSeason)) {
                                displayedSeasonNum = extractedSeason;
                            }
                        }

                        if (tmdb_api_key_set) {
                            // Use show-level backdrop/overview from mediaData, season-specific poster
                            const itemBackdropPath = showBackdropPath || selectedItem.backdrop_path || null;
                            const itemShowOverview = showOverview || selectedItem.show_overview || 'No overview available';

                            displaySeasonInfo(
                                selectedItem.title,
                                displayedSeasonNum,
                                selectedItem.air_date,
                                selectedItem.season_overview,
                                selectedItem.poster_path,
                                genre_ids,
                                vote_average,
                                itemBackdropPath,
                                itemShowOverview
                            );
                        } else {
                            displaySeasonInfoTextOnly(selectedItem.title, displayedSeasonNum);
                        }

                        // Check if we have pre-fetched episodes for this season
                        if (preFetchedEpisodes && displayedSeasonNum === preFetchedSeason) {
                            if (window.DEBUG) console.log('⚡ Using pre-fetched episodes for season', preFetchedSeason);
                            // Display pre-fetched episodes instantly without API call
                            displayEpisodeResults(
                                preFetchedEpisodes,
                                selectedItem.title,
                                selectedItem.year,
                                version,
                                selectedItem.id,
                                selectedItem.media_type,
                                displayedSeasonNum,
                                null,
                                genre_ids
                            );
                        } else {
                            // Normal flow - fetch episodes from API
                            selectEpisode(selectedItem.id, selectedItem.title, selectedItem.year, selectedItem.media_type, displayedSeasonNum, null, selectedItem.multi, genre_ids);
                        }
                    });

                    // Setup season pack button
                    if (seasonPackButton) {
                        seasonPackButton.onclick = function() {
                            if (isRequester) return;

                            const selectedItem = JSON.parse(dropdown.value);
                            const content = {
                                mediaId: selectedItem.id,
                                title: selectedItem.title,
                                year: selectedItem.year,
                                mediaType: selectedItem.media_type,
                                season: selectedItem.season_num,
                                episode: null,
                                multi: true,
                                genre_ids: genre_ids
                            };
                            showScrapeVersionModal(content);
                        };
                    }

                    // Setup request season button
                    if (requestSeasonButton) {
                        requestSeasonButton.onclick = function() {
                            const selectedItem = JSON.parse(dropdown.value);
                            const content = {
                                id: selectedItem.id,
                                mediaType: selectedItem.media_type,
                                title: selectedItem.title,
                                seasons: [selectedItem.season_num]
                            };
                            requestContent(content, ['Any']);
                        };
                    }

                    // Auto-select first season to trigger episode loading
                    if (dropdown.options.length > 0) {
                        dropdown.dispatchEvent(new Event('change'));
                    }
                }
            })
            .catch(error => {
                hideLoadingState();
                if (window.DEBUG) console.error('Error auto-loading media:', error);
                displayError('Failed to load media data');
            });
        } catch (error) {
            if (window.DEBUG) console.error('Error parsing pending scraper load:', error);
            sessionStorage.removeItem('pendingScraperLoad');
        }
    }

    // OPTIMIZATION: Prefetch trending data in background for instant display
    prefetchTrendingData();
}); // End of DOMContentLoaded

// PHASE 2.3: Handle browser back/forward navigation
window.addEventListener('popstate', function(event) {
    if (window.DEBUG) console.log('Popstate event:', event.state);

    const searchInput = document.querySelector('#search-form input[name="search_term"]');
    if (!searchInput) return;

    // Get search term from URL
    const popstateUrlParams = new URLSearchParams(window.location.search);
    const searchQuery = popstateUrlParams.get('q');
    const versionParam = popstateUrlParams.get('v');

    if (searchQuery) {
        // Restore search input value
        searchInput.value = searchQuery;

        // Restore version if present
        const versionSelect = document.getElementById('version-select');
        if (versionParam && versionSelect) {
            versionSelect.value = versionParam;
        }

        // Perform search (don't update URL again)
        performLiveSearch(searchQuery, false);
    } else {
        // Clear search if no query in URL
        searchInput.value = '';
        const resultsContainer = document.getElementById('search-results');
        if (resultsContainer) {
            resultsContainer.innerHTML = '';
        }
    }
});

// PHASE 1.2 & 2.3: Live Search Function with URL state management
function performLiveSearch(searchTerm, updateURL = true) {
    if (window.DEBUG) console.log('Performing live search for:', searchTerm);

    // Get current version selection
    const versionSelect = document.getElementById('version-select');
    const version = versionSelect ? versionSelect.value : '';

    // INSTANT FEEDBACK: Show loading with existing loading system
    Loading.show(`Searching for "${searchTerm}"...`, '', true, false);

    // PHASE 2.3: Update URL with search query (but don't trigger popstate)
    if (updateURL && searchTerm) {
        const url = new URL(window.location);
        url.searchParams.set('q', searchTerm);
        window.history.pushState({ search: searchTerm }, '', url);
    }

    fetch('/scraper/live_search', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            search_term: searchTerm,
            year: null
        })
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        // Hide loading overlay
        Loading.hide();

        if (data.error) {
            if (window.DEBUG) console.error('Live search error:', data.error);
            return;
        }

        // Display results using existing display function with version parameter
        if (data.results) {
            displaySearchResults(data.results);
        }
    })
    .catch(error => {
        // Hide loading overlay on error
        Loading.hide();
        if (window.DEBUG) console.error('Live search failed:', error);
    });
}

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
        if (window.DEBUG) console.error('Error fetching versions:', error);
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
        if (window.DEBUG) console.log(`Fetching seasons for TMDB ID: ${tmdbId}`);
        const response = await fetch(`/content/show_seasons?tmdb_id=${tmdbId}`, {
            method: 'GET'
        });
        
        // Log the HTTP status
        if (window.DEBUG) console.log(`Show seasons fetch response status: ${response.status}`);
        
        const data = await response.json();
        if (window.DEBUG) console.log('Show seasons API response:', data);
        
        if (data.success && data.seasons && data.seasons.length > 0) {
            // Update the season selection container
            const seasonContainer = document.getElementById('season-selection-container');
            seasonContainer.innerHTML = '<div class="seasons-list"></div>';
            const seasonsList = seasonContainer.querySelector('.seasons-list');
            
            // Sort seasons in numerical order
            const seasons = data.seasons.sort((a, b) => a - b);
            if (window.DEBUG) console.log(`Found ${seasons.length} seasons:`, seasons);
            
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
            if (window.DEBUG) console.warn('No seasons found or invalid response format:', data);
            let errorMessage = 'Could not load seasons. Please try again or request the whole show.';
            if (data.error) {
                if (window.DEBUG) console.error('API error message:', data.error);
                errorMessage = `Error: ${data.error}`;
            }
            document.getElementById('season-selection-container').innerHTML = `<p>${errorMessage}</p>`;
        }
    } catch (error) {
        if (window.DEBUG) console.error('Error fetching show seasons:', error);
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
        if (window.DEBUG) console.error('Error requesting content:', error);
        displayError('Error requesting content');
    } finally {
        hideLoadingState();
    }
}

function displayTraktAuthMessage() {
    const trendingContainer = document.getElementById('trendingContainer');
    trendingContainer.innerHTML = '<p>Please authenticate with Trakt to see trending movies and shows.</p>';
}

function createMovieElement(data, index = 999) {
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
                    loading="${index < 8 ? 'eager' : 'lazy'}"
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

function createShowElement(data, index = 999) {
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
                    loading="${index < 8 ? 'eager' : 'lazy'}"
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
            selectSeason(data.tmdb_id, data.title, data.year, 'tv', null, null, true, data.genre_ids, data.vote_average || data.rating, data.backdrop_path, data.show_overview, data.tmdb_api_key_set, data.rating);
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

// Combined function to fetch all trending data in one request
function get_allTrending() {
    toggleResultsVisibility('get_trendingMovies');
    const container_mv = document.getElementById('movieContainer');
    const container_tv = document.getElementById('showContainer');
    const container_anime = document.getElementById('animeContainer');

    // PRIORITY 1: Check for SSR data (instant rendering)
    if (window.SCRAPER_SSR_ENABLED) {
        const ssrDataElement = document.getElementById('ssr-trending-data');
        if (ssrDataElement) {
            try {
                const data = JSON.parse(ssrDataElement.textContent);
                if (window.DEBUG) console.log('✅ SSR: Using embedded trending data (0ms delay)');

                // Process movies
                if (data.trendingMovies && data.trendingMovies.length > 0) {
                    setCachedTrending('trending_movies', data.trendingMovies);
                    const movieFrag = document.createDocumentFragment();
                    data.trendingMovies.forEach((item, index) => {
                        movieFrag.appendChild(createMovieElement(item, index));
                    });
                    container_mv.appendChild(movieFrag);
                }

                // Process shows
                if (data.trendingShows && data.trendingShows.length > 0) {
                    setCachedTrending('trending_shows', data.trendingShows);
                    const showFrag = document.createDocumentFragment();
                    data.trendingShows.forEach((item, index) => {
                        showFrag.appendChild(createShowElement(item, index));
                    });
                    container_tv.appendChild(showFrag);
                }

                // Process anime
                if (data.trendingAnime && data.trendingAnime.length > 0) {
                    setCachedTrending('trending_anime', data.trendingAnime);
                    const animeFrag = document.createDocumentFragment();
                    data.trendingAnime.forEach((item, index) => {
                        animeFrag.appendChild(createAnimeElement(item, index));
                    });
                    container_anime.appendChild(animeFrag);
                }

                return; // SSR successful, exit early
            } catch (error) {
                if (window.DEBUG) console.error('⚠️ SSR: Failed to parse embedded data, falling back to fetch:', error);
                // Fall through to client-side cache or fetch
            }
        }
    }

    // PRIORITY 2: Check if all data is cached client-side
    const moviesCached = getCachedTrending('trending_movies');
    const showsCached = getCachedTrending('trending_shows');
    const animeCached = getCachedTrending('trending_anime');

    if (moviesCached && showsCached && animeCached) {
        // All data is cached, render immediately
        if (window.DEBUG) console.log('✅ Client cache: Using cached trending data');
        const movieFrag = document.createDocumentFragment();
        moviesCached.forEach((item, index) => {
            movieFrag.appendChild(createMovieElement(item, index));
        });
        container_mv.appendChild(movieFrag);

        const showFrag = document.createDocumentFragment();
        showsCached.forEach((item, index) => {
            showFrag.appendChild(createShowElement(item, index));
        });
        container_tv.appendChild(showFrag);

        const animeFrag = document.createDocumentFragment();
        animeCached.forEach((item, index) => {
            animeFrag.appendChild(createAnimeElement(item, index));
        });
        container_anime.appendChild(animeFrag);

        return;
    }

    // PRIORITY 3: Fetch combined data from server
    if (window.DEBUG) console.log('⏳ Fetching trending data via AJAX...');
    fetch('/scraper/all_trending', { method: 'GET' })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                displayError(data.error);
                return;
            }

            // Process movies
            if (data.trendingMovies && data.trendingMovies.length > 0) {
                setCachedTrending('trending_movies', data.trendingMovies);
                const movieFrag = document.createDocumentFragment();
                data.trendingMovies.forEach((item, index) => {
                    movieFrag.appendChild(createMovieElement(item, index));
                });
                container_mv.appendChild(movieFrag);
            }

            // Process shows
            if (data.trendingShows && data.trendingShows.length > 0) {
                setCachedTrending('trending_shows', data.trendingShows);
                const showFrag = document.createDocumentFragment();
                data.trendingShows.forEach((item, index) => {
                    showFrag.appendChild(createShowElement(item, index));
                });
                container_tv.appendChild(showFrag);
            }

            // Process anime
            if (data.trendingAnime && data.trendingAnime.length > 0) {
                setCachedTrending('trending_anime', data.trendingAnime);
                const animeFrag = document.createDocumentFragment();
                data.trendingAnime.forEach((item, index) => {
                    animeFrag.appendChild(createAnimeElement(item, index));
                });
                container_anime.appendChild(animeFrag);
            }
        })
        .catch(error => {
            if (window.DEBUG) console.error('Error fetching all trending:', error);
            displayError('An error occurred while fetching trending content.');
        });
}

function get_trendingMovies() {
    toggleResultsVisibility('get_trendingMovies');
    const container_mv = document.getElementById('movieContainer');

    // Check client-side cache first
    const cacheKey = 'trending_movies';
    const cachedData = getCachedTrending(cacheKey);

    if (cachedData) {
        // Display cached results immediately using DocumentFragment
        const fragment = document.createDocumentFragment();
        cachedData.forEach((item, index) => {
            const movieElement = createMovieElement(item, index);
            fragment.appendChild(movieElement);
        });
        container_mv.appendChild(fragment);
        return;
    }

    fetch('/scraper/movies_trending', {
        method: 'GET'
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            const trendingMovies = data.trendingMovies;

            // Cache the results
            setCachedTrending(cacheKey, trendingMovies);

            // Use DocumentFragment for batch DOM updates
            const fragment = document.createDocumentFragment();
            trendingMovies.forEach((item, index) => {
                const movieElement = createMovieElement(item, index);
                fragment.appendChild(movieElement);
            });
            container_mv.appendChild(fragment);
        }
    })
    .catch(error => {
        if (window.DEBUG) console.error('Error:', error);
        displayError('An error occurred.');
    });
}

function get_trendingShows() {
    toggleResultsVisibility('get_trendingMovies');
    const container_tv = document.getElementById('showContainer');

    // Check client-side cache first
    const cacheKey = 'trending_shows';
    const cachedData = getCachedTrending(cacheKey);

    if (cachedData) {
        // Display cached results immediately using DocumentFragment
        const fragment = document.createDocumentFragment();
        cachedData.forEach((item, index) => {
            const showElement = createShowElement(item, index);
            fragment.appendChild(showElement);
        });
        container_tv.appendChild(fragment);
        return;
    }

    fetch('/scraper/shows_trending', {
        method: 'GET'
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            const trendingShows = data.trendingShows;

            // Cache the results
            setCachedTrending(cacheKey, trendingShows);

            // Use DocumentFragment for batch DOM updates
            const fragment = document.createDocumentFragment();
            trendingShows.forEach((item, index) => {
                const showElement = createShowElement(item, index);
                fragment.appendChild(showElement);
            });
            container_tv.appendChild(fragment);
        }
    })
    .catch(error => {
        if (window.DEBUG) console.error('Error:', error);
        displayError('An error occurred.');
    });
}

function createAnimeElement(data, index = 999) {
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
                    loading="${index < 8 ? 'eager' : 'lazy'}"
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
            selectSeason(data.tmdb_id, data.title, data.year, 'tv', null, null, true, data.genre_ids, data.vote_average || data.rating, data.backdrop_path, data.show_overview, data.tmdb_api_key_set, data.rating);
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

    // Check client-side cache first
    const cacheKey = 'trending_anime';
    const cachedData = getCachedTrending(cacheKey);

    if (cachedData) {
        // Display cached results immediately using DocumentFragment
        const fragment = document.createDocumentFragment();
        cachedData.forEach((item, index) => {
            const animeElement = createAnimeElement(item, index);
            fragment.appendChild(animeElement);
        });
        container_anime.appendChild(fragment);
        return;
    }

    fetch('/scraper/anime_trending', {
        method: 'GET'
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            const trendingAnime = data.trendingAnime;

            // Cache the results
            setCachedTrending(cacheKey, trendingAnime);

            // Use DocumentFragment for batch DOM updates
            const fragment = document.createDocumentFragment();
            trendingAnime.forEach((item, index) => {
                const animeElement = createAnimeElement(item, index);
                fragment.appendChild(animeElement);
            });
            container_anime.appendChild(fragment);
        }
    })
    .catch(error => {
        if (window.DEBUG) console.error('Error:', error);
        displayError('An error occurred.');
    });
}

function searchMedia(event) {
    if (window.DEBUG) console.log('searchMedia called', event);
    
    // Prevent the default form submission which would reload the page
    if (event) {
        event.preventDefault();
        if (window.DEBUG) console.log('Event default prevented');
    }
    
    // Get the isRequester value
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';
    
    let searchTerm = document.querySelector('input[name="search_term"]').value.trim(); // Trim whitespace
    let version = document.getElementById('version-select').value;
    
    if (window.DEBUG) console.log('Search parameters:', { searchTerm, version });
    
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
        if (window.DEBUG) console.log('Detected IMDb ID:', searchTerm);
        fetchUrl = '/scraper/lookup_by_id';
        fetchBody = `id_type=imdb&media_id=${encodeURIComponent(searchTerm)}`;
    } else if (tmdbIdPrefixedPattern.test(searchTerm)) {
        const tmdbId = searchTerm.substring(4); // Remove "tmdb" prefix
        if (window.DEBUG) console.log('Detected TMDb ID (after stripping prefix):', tmdbId);
        fetchUrl = '/scraper/lookup_by_id';
        fetchBody = `id_type=tmdb&media_id=${encodeURIComponent(tmdbId)}`;
    } else {
        if (window.DEBUG) console.log('Performing standard search for:', searchTerm);
        fetchUrl = '/scraper/';
        fetchBody = `search_term=${encodeURIComponent(searchTerm)}&version=${encodeURIComponent(version)}`;
    }

    if (window.DEBUG) console.log(`Submitting search to ${fetchUrl}`);

    fetch(fetchUrl, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: fetchBody
    })
    .then(response => {
        if (window.DEBUG) console.log('Search response status:', response.status);
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
        if (window.DEBUG) console.log('Search response data:', data);
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
        if (window.DEBUG) console.error('Search Error:', error);
        displayError('An error occurred while searching: ' + error.message);
    });
}

// Infinite scroll state for search results
window.searchScrollState = {
    allResults: [],
    renderedCount: 0,
    batchSize: 20,
    isLoading: false,
    version: null
};

function displaySearchResults(results, version) {
    if (window.DEBUG) console.log('Displaying results. First item:', results.length > 0 ? JSON.stringify(results[0]) : 'No results'); // Log the first item as JSON

    // First hide trending container and show search results
    toggleResultsVisibility('displaySearchResults');

    // Get the search results container
    const searchResultsDiv = document.getElementById('searchResults');
    const resultsList = document.getElementById('resultsList');

    if (!searchResultsDiv || !resultsList) {
        if (window.DEBUG) console.error('Search result elements not found!');
        return;
    }

    // Clear previous results
    resultsList.innerHTML = '';

    // Show the search results container
    searchResultsDiv.style.display = 'block';

    // Validate that results is an array
    if (!Array.isArray(results)) {
        if (window.DEBUG) console.error('Expected results to be an array but got:', typeof results);
        displayError('Invalid response format, likely Trakt connection issue');
        return;
    }

    // Check if we have results
    if (results.length === 0) {
        if (window.DEBUG) console.log('No results found');
        resultsList.innerHTML = '<p>No results found. Try a different search term.</p>';
        return;
    }

    // Initialize infinite scroll
    searchScrollState.allResults = results;
    searchScrollState.renderedCount = 0;
    searchScrollState.version = version;
    searchScrollState.isLoading = false;

    // Render initial batch
    renderResultsBatch();

    // Set up scroll listener for infinite scroll
    setupSearchScrollListener();
}

// OLD RENDERING CODE BELOW - DEPRECATED BY VIRTUAL SCROLLING (Phase 3.2)
// This is kept as a fallback but should not be called anymore
function displaySearchResults_OLD(results, version) {
    // Deprecated - kept for reference only
    const resultsList = document.getElementById('resultsList');
    if (!resultsList) return;

    const tmdb_api_key_set = document.getElementById('tmdb_api_key_set')?.value === 'True';
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';

    const requestIconHTML = `
        <div class="request-icon" title="Request this content">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="8" x2="12" y2="16"></line>
                <line x1="8" y1="12" x2="16" y2="12"></line>
            </svg>
        </div>
    `;

    const testerIconHTML = `
        <div class="tester-icon" title="Test this content">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M9 3h6v4H9zM6 7h12l-3 10H9z"></path>
                <path d="M10 17h4v4h-4z"></path>
            </svg>
        </div>
    `;

    const assignMagnetIconHTML = `
    <div class="assign-magnet-icon assign-magnet-mobile" title="Assign Magnet Link">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path>
            <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path>
        </svg>
    </div>
    `;

    results.forEach(item => {
        if (window.DEBUG) console.log('Processing item for display:', JSON.stringify(item, null, 2));
        const searchResDiv = document.createElement('div');
        searchResDiv.className = 'sresult';
        let posterUrl = '/static/images/placeholder.png'; // Default placeholder
        let isPlaceholder = true;

        // --- Use item.poster_path (lowercase with underscore) ---
        if (item.poster_path && typeof item.poster_path === 'string' && item.poster_path.trim() !== '') {
             const pathToCheck = item.poster_path.trim(); // Use correct key here
             if (window.DEBUG) console.log('Checking poster_path:', pathToCheck); // Log correct key

             // --- Logic remains the same, just uses pathToCheck from correct key ---
             if (pathToCheck.startsWith('static/')) {
                 posterUrl = pathToCheck.startsWith('/') ? pathToCheck : `/${pathToCheck}`;
                 isPlaceholder = pathToCheck.includes('placeholder.png');
                 if (window.DEBUG) console.log(`Poster type: static, Placeholder: ${isPlaceholder}`);
             } else if (pathToCheck.startsWith('http')) {
                 posterUrl = pathToCheck;
                 isPlaceholder = false;
                  if (window.DEBUG) console.log('Poster type: http');
             } else if (pathToCheck.startsWith('/scraper/tmdb_image')) {
                  posterUrl = pathToCheck.startsWith('/') ? pathToCheck : `/${pathToCheck}`;
                  isPlaceholder = false;
                  if (window.DEBUG) console.log('Poster type: proxy');
             } else if (pathToCheck.startsWith('/')) { // Assume TMDB path
                 posterUrl = `/scraper/tmdb_image/w300${pathToCheck}`; // Use proxy route
                 isPlaceholder = false;
                  if (window.DEBUG) console.log('Poster type: assumed TMDB, using proxy');
             } else {
                 if (window.DEBUG) console.warn(`Unknown poster_path format, using placeholder: ${pathToCheck}`);
             }
        } else {
             if (window.DEBUG) console.warn('Missing, empty, or invalid poster_path, using placeholder. Value:', item.poster_path); // Log correct key
        }
        if (window.DEBUG) console.log('Final poster URL:', posterUrl);
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
                        ${dbStatusPipHTML}
                    </div>
                    <div class="searchresult-info" style="display: ${!tmdb_api_key_set ? 'block' : 'none'}">
                        <h2 class="searchresult-item">${item.title}</h2>
                        <p class="searchresult-year">${displayYear}</p>
                    </div>
                ${assignMagnetIconHTML}
            </div>
        `;

        // ... (rest of the button handlers remain the same) ...
         // Add click handler for the main content area
         // Add click handlers for the poster
        searchResDiv.onclick = function() {
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
                    selectSeason(item.id, item.title, item.year, item.media_type, null, null, true, item.genre_ids, item.vote_average || item.voteAverage, item.backdrop_path, item.show_overview, tmdb_api_key_set, item.rating);
                }
            }
        };
 

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

// Render a batch of search results using infinite scroll
function renderResultsBatch() {
    if (searchScrollState.isLoading) return;
    if (searchScrollState.renderedCount >= searchScrollState.allResults.length) return;

    searchScrollState.isLoading = true;

    const resultsList = document.getElementById('resultsList');
    if (!resultsList) {
        searchScrollState.isLoading = false;
        return;
    }

    const start = searchScrollState.renderedCount;
    const end = Math.min(start + searchScrollState.batchSize, searchScrollState.allResults.length);
    const batch = searchScrollState.allResults.slice(start, end);

    if (window.DEBUG) console.log(`Rendering batch: ${start}-${end} of ${searchScrollState.allResults.length}`);

    // Get settings
    const tmdb_api_key_set = document.getElementById('tmdb_api_key_set')?.value === 'True';
    const isRequesterEl = document.getElementById('is_requester');
    const isRequester = isRequesterEl && isRequesterEl.value === 'True';
    const version = searchScrollState.version;

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
    <div class="assign-magnet-icon assign-magnet-mobile" title="Assign Magnet Link">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path>
            <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path>
        </svg>
    </div>
    `;

    // Use DocumentFragment for batch DOM updates
    const fragment = document.createDocumentFragment();

    // Render each item in the batch
    batch.forEach((item, batchIndex) => {
        // Calculate global index for eager loading (first 12 results total)
        const globalIndex = start + batchIndex;
        const searchResDiv = createResultElement(item, tmdb_api_key_set, isRequester, version, requestIconHTML, testerIconHTML, assignMagnetIconHTML, globalIndex);
        fragment.appendChild(searchResDiv);
    });

    // Append all results at once using DocumentFragment
    resultsList.appendChild(fragment);

    searchScrollState.renderedCount = end;
    searchScrollState.isLoading = false;

    if (window.DEBUG) console.log(`Rendered ${end} of ${searchScrollState.allResults.length} results`);
}

// Handle infinite scroll - load more results when nearing bottom
let searchScrollListener = null;
function setupSearchScrollListener() {
    // Remove previous listener if exists
    if (searchScrollListener) {
        window.removeEventListener('scroll', searchScrollListener);
    }

    // Simple scroll handler - load more when 500px from bottom
    searchScrollListener = function() {
        const scrollPosition = window.innerHeight + window.scrollY;
        const pageHeight = document.documentElement.scrollHeight;
        const triggerDistance = 500;

        if (scrollPosition >= pageHeight - triggerDistance) {
            renderResultsBatch();
        }
    };

    window.addEventListener('scroll', searchScrollListener);
}

// Extract the result element creation into a separate function for reusability
function createResultElement(item, tmdb_api_key_set, isRequester, version, requestIconHTML, testerIconHTML, assignMagnetIconHTML, index = 999) {
    if (window.DEBUG) console.log('Processing item for display:', JSON.stringify(item, null, 2));
    const searchResDiv = document.createElement('div');
    searchResDiv.className = 'sresult';
    let posterUrl = '/static/images/placeholder.png'; // Default placeholder
    let isPlaceholder = true;

    // --- Use item.poster_path (lowercase with underscore) ---
    if (item.poster_path && typeof item.poster_path === 'string' && item.poster_path.trim() !== '') {
         const pathToCheck = item.poster_path.trim(); // Use correct key here
         if (window.DEBUG) console.log('Checking poster_path:', pathToCheck); // Log correct key

         // --- Logic remains the same, just uses pathToCheck from correct key ---
         if (pathToCheck.startsWith('static/')) {
             posterUrl = pathToCheck.startsWith('/') ? pathToCheck : `/${pathToCheck}`;
             isPlaceholder = pathToCheck.includes('placeholder.png');
             if (window.DEBUG) console.log(`Poster type: static, Placeholder: ${isPlaceholder}`);
         } else if (pathToCheck.startsWith('http')) {
             posterUrl = pathToCheck;
             isPlaceholder = false;
              if (window.DEBUG) console.log('Poster type: http');
         } else if (pathToCheck.startsWith('/scraper/tmdb_image')) {
              posterUrl = pathToCheck.startsWith('/') ? pathToCheck : `/${pathToCheck}`;
              isPlaceholder = false;
              if (window.DEBUG) console.log('Poster type: proxy');
         } else if (pathToCheck.startsWith('/')) { // Assume TMDB path
             posterUrl = `/scraper/tmdb_image/w300${pathToCheck}`; // Use proxy route
             isPlaceholder = false;
              if (window.DEBUG) console.log('Poster type: assumed TMDB, using proxy');
         } else {
             if (window.DEBUG) console.warn(`Unknown poster_path format, using placeholder: ${pathToCheck}`);
         }
    } else {
         if (window.DEBUG) console.warn('Missing, empty, or invalid poster_path, using placeholder. Value:', item.poster_path); // Log correct key
    }
    if (window.DEBUG) console.log('Final poster URL:', posterUrl);
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
                ${item.media_type === 'show' || item.media_type === 'tv' ? '<span class="mediatype-tv">TV</span>' : '<span class="mediatype-mv">MOVIE</span>'}
                <div class="poster-container">
                    <img src="${posterUrl}"
                        alt="${item.title}"
                        loading="${index < 12 ? 'eager' : 'lazy'}"
                        class="${isPlaceholder ? 'placeholder-poster' : ''}">
                    <div class="poster-overlay">
                        <h3>${item.title}</h3>
                        <p>${displayYear}</p>
                    </div>
                    ${requestIconHTML}
                    ${testerIconHTML}
                    ${dbStatusPipHTML}
                </div>
                <div class="searchresult-info" style="display: ${!tmdb_api_key_set ? 'block' : 'none'}">
                    <h2 class="searchresult-item">${item.title}</h2>
                    <p class="searchresult-year">${displayYear}</p>
                </div>
            ${assignMagnetIconHTML}
        </div>
    `;

     // Add click handler for the main content area
     // Add click handlers for the poster
    searchResDiv.onclick = function() {
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
                    // Pass all metadata to selectSeason (use snake_case field names from API)
                selectSeason(item.id, item.title, item.year, item.media_type, null, null, true, item.genre_ids, item.vote_average || item.rating, item.backdrop_path, item.show_overview, tmdb_api_key_set, item.rating);
            }
        }
    };


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

            const params = new URLSearchParams({
                title: item.title,
                year: item.year,
                media_type: item.media_type === 'show' ? 'tv' : item.media_type,
                id: item.id
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
            const currentVersion = document.getElementById('version-select')?.value || ''; // Get current version

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

    return searchResDiv;
}

async function selectMedia(mediaId, title, year, mediaType, season, episode, multi, genre_ids, version) {
    // Check if user is a requester before making the request
    const isRequesterEl = document.getElementById('is_requester');
    if (isRequesterEl && isRequesterEl.value === 'True') {
        // Display error message for requesters
        return;
    }

    if (!mediaId || mediaId === 'undefined') {
        if (window.DEBUG) console.error("selectMedia called with invalid mediaId:", mediaId);
        displayError("An internal error occurred: media ID is missing.");
        hideLoadingState();
        return;
    }

    // Create cache key from search parameters
    const cacheKey = `${mediaId}_${mediaType}_${version}_${season || 'null'}_${episode || 'null'}`;
    
    // Check cache first
    const cachedData = getCachedTorrentResults(cacheKey);
    if (cachedData) {
        if (window.DEBUG) console.log('⚡ Using cached torrent results - instant display!');
        hideLoadingState();
        displayTorrentResults(cachedData.data, title, year, version, mediaId, mediaType, season, episode, genre_ids, cachedData.searchDuration);
        return;
    }

    showLoadingState();
    const searchStartTime = performance.now(); // Track search start time
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
    // Convert genre_ids to comma-separated string if it's an array
    if (genre_ids) {
        const genreString = Array.isArray(genre_ids) ? genre_ids.join(',') : genre_ids;
        formData.append('genre_ids', genreString);
    }
    
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
        
        const searchDuration = Math.round(performance.now() - searchStartTime); // Calculate duration
        hideLoadingState();
        if (data.error) {
            displayError(data.error);
            return;
        }
        
        // Cache the results
        setCachedTorrentResults(cacheKey, { data: data, searchDuration: searchDuration });
        
        // Pass the whole 'data' object and search duration
        displayTorrentResults(data, title, year, version, mediaId, mediaType, season, episode, genre_ids, searchDuration);
        
        // No need to do additional cache checking since displayTorrentResults already does it
    })
    .catch(error => {
        hideLoadingState();
        if (window.DEBUG) console.error('Error:', error);
        displayError('An error occurred while processing your request.');
    });
}

// Function to check cache status in the background and update the UI
function checkCacheStatusInBackground(hashes, results) {
    let processedCount = 0;
    let totalCount = Math.min(5, results.length);
    let processingItems = new Set(); // Track items currently being processed
    const MAX_PARALLEL_REQUESTS = 1; // Process up to 3 items at once

    // Update to handle both magnet links and torrent files
    function updateCacheStatusUI(index, status) {
        // Try new desktop structure first (Tangerine theme)
        const cacheCell = document.querySelector(`.cache-cell[data-torrent-index="${index}"]`);
        if (cacheCell) {
            const wrapper = cacheCell.querySelector('.cache-icon-wrapper');
            if (wrapper) {
                // Update with new cache icon (Tangerine theme)
                if (status === 'cached') {
                    wrapper.innerHTML = createCacheIcon('Yes');
                } else if (status === 'not_cached') {
                    wrapper.innerHTML = createCacheIcon('No');
                } else if (status === 'check_unavailable') {
                    wrapper.innerHTML = createCacheIcon('N/A');
                } else {
                    wrapper.innerHTML = createCacheIcon('Unknown');
                }
            }
        } else {
            // Fallback to old structure (mobile view or classic theme)
            const cacheStatusElements = document.querySelectorAll('.cache-status');
            if (index < cacheStatusElements.length) {
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
            }
        }
        
        processedCount++;
        processingItems.delete(index);
        
        // Try to process more items if we have capacity
        processNextItems();
    }

    function markRemainingAsNA() {
        // Mark any remaining unchecked cache cells as N/A
        for (let i = processedCount; i < totalCount; i++) {
            // Try new desktop structure first
            const cacheCell = document.querySelector(`.cache-cell[data-torrent-index="${i}"]`);
            if (cacheCell) {
                const wrapper = cacheCell.querySelector('.cache-icon-wrapper');
                if (wrapper) {
                    wrapper.innerHTML = createCacheIcon('N/A');
                }
            } else {
                // Fallback to old structure
                const cacheStatusElements = document.querySelectorAll('.cache-status');
                if (i < cacheStatusElements.length) {
                    const element = cacheStatusElements[i];
                    element.classList.remove('not-checked');
                    element.classList.add('check-unavailable');
                    element.textContent = 'N/A';
                }
            }
        }
    }

    function showCompletionNotification() {
        return;
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

        if (window.DEBUG) console.log(`Checking cache status for item at index ${index}`);
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
            if (window.DEBUG) console.log(`Cache status for index ${index}:`, data);
            updateCacheStatusUI(index, data.status);
            checkCompletion();
        })
        .catch(error => {
            if (window.DEBUG) console.error(`Error checking cache status for index ${index}:`, error);
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
            if (i < processedCount) {
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

    // Start processing items
    processNextItems();
}

function selectSeason(mediaId, title, year, mediaType, season, episode, multi, genre_ids, vote_average, backdrop_path, show_overview, tmdb_api_key_set, rating) {
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
    if (rating) formData.append('rating', rating); // Pass rating to backend
    if (vote_average) formData.append('vote_average', vote_average); // Pass vote_average to backend
    if (genre_ids) formData.append('genre_ids', JSON.stringify(genre_ids)); // Pass genres to backend

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
                
                // Extract the displayed season number from the option text for Lego Masters US
                const optionText = this.options[this.selectedIndex].textContent;
                let displayedSeasonNum = selectedItem.season_num;
                
                if (window.DEBUG) console.log('Season selection debug:', {
                    optionText: optionText,
                    originalSeasonNum: selectedItem.season_num,
                    selectedItem: selectedItem
                });
                
                // For Lego Masters US, extract season number from display text
                if (optionText.startsWith('Season: ')) {
                    const extractedSeason = parseInt(optionText.replace('Season: ', ''));
                    if (!isNaN(extractedSeason)) {
                        displayedSeasonNum = extractedSeason;
                        if (window.DEBUG) console.log('Extracted season number from display text:', extractedSeason);
                    }
                }
                
                if (window.DEBUG) console.log('Final season number to be passed to selectEpisode:', displayedSeasonNum);
                
                if (tmdb_api_key_set) {
                    // Use the backdrop_path from the selected item or from the parent scope backdrop_path parameter
                    // Same for show_overview
                    const itemBackdropPath = selectedItem.backdrop_path || backdrop_path || null;
                    const itemShowOverview = selectedItem.show_overview || show_overview || 'No overview available';
                    
                    displaySeasonInfo(
                        selectedItem.title, 
                        displayedSeasonNum, // Use displayed season number
                        selectedItem.air_date, 
                        selectedItem.season_overview, 
                        selectedItem.poster_path, 
                        genre_ids, 
                        vote_average, 
                        itemBackdropPath, 
                        itemShowOverview
                    );
                } else {
                    displaySeasonInfoTextOnly(selectedItem.title, displayedSeasonNum); // Use displayed season number
                }
                selectEpisode(selectedItem.id, selectedItem.title, selectedItem.year, selectedItem.media_type, displayedSeasonNum, null, selectedItem.multi, genre_ids); // Use displayed season number
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

            // Show back button for season results
            showBackButton();

            // Trigger initial selection
            if (dropdown.options.length > 0) {
                dropdown.selectedIndex = 0;
                dropdown.dispatchEvent(new Event('change'));
            }
        }
    })
    .catch(error => {
        hideLoadingState();
        if (window.DEBUG) console.error('Error:', error);
        displayError('An error occurred while processing your request.');
    });
}

function displaySeasonInfo(title, season_num, air_date, season_overview, poster_path, genre_ids, vote_average, backdrop_path, show_overview) {
    if (window.DEBUG) console.log('Received genre_ids:', genre_ids);
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
    const version = document.getElementById('version-select').value;

    if (window.DEBUG) console.log('selectEpisode called with:', {
        mediaId: mediaId,
        title: title,
        year: year,
        mediaType: mediaType,
        season: season,
        episode: episode,
        multi: multi,
        genre_ids: genre_ids
    });

    // PHASE 1.1: Check cache first
    const cacheKey = `episodes:${mediaId}:${season}:${version}:${allowSpecials}`;
    const cachedEpisodes = getCachedEpisodes(cacheKey);

    if (cachedEpisodes) {
        if (window.DEBUG) console.log('Using cached episodes for season', season);
        displayEpisodeResults(cachedEpisodes, title, year, version, mediaId, mediaType, season, episode, genre_ids);
        return;
    }

    if (window.DEBUG) console.log('Episode cache MISS for:', cacheKey, '- fetching fresh data');
    showLoadingState();

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
            // PHASE 1.1: Cache the results
            setCachedEpisodes(cacheKey, data.episode_results);

            // Allow requesters to view episodes, but they won't be able to select them
            displayEpisodeResults(data.episode_results, title, year, version, mediaId, mediaType, season, episode, genre_ids);
        }
    })
    .catch(error => {
        hideLoadingState();
        if (window.DEBUG) console.error('Error:', error);
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
            selectSeason(item.id, item.title, item.year, item.media_type, null, null, true, item.genre_ids, item.vote_average || item.voteAverage, item.backdrop_path, item.show_overview, item.tmdb_api_key_set, item.rating);
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
    if (window.DEBUG) console.log(`Auto-scraping for IMDb ID: ${imdbId}, Season: ${season}, Episode: ${episode}, Version: ${version}`);

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
                    if (window.DEBUG) console.warn(`Version "${version}" not found in dropdown. Using default.`);
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
        if (window.DEBUG) console.error('Auto-scrape failed:', error);
        displayError(`Auto-scrape failed: ${error.message}`);
    }
}
