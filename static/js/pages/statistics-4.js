/* Moved from templates/statistics.html. */
// Initialize displays when page loads
document.addEventListener('DOMContentLoaded', function() {
    // Check if on mobile
    if (window.innerWidth <= 768) {
        // Add compact-view class to statistics-wrapper
        document.querySelector('.statistics-wrapper').classList.add('compact-view');
    }

    // Show search box (tangerine is the only theme now)
    const searchBox = document.getElementById('stats-search-box');
    if (searchBox) {
        searchBox.style.display = 'block';
    }

    // Handle search form submission
    // Helper function to detect IMDb/TMDB IDs (inline version for statistics page)
    function buildSearchURL(searchTerm) {
        const term = searchTerm.trim();

        // Check for IMDb ID (tt followed by digits)
        const imdbMatch = term.match(/^(tt\d+)$/i);
        if (imdbMatch) {
            return `/discover?imdb_id=${encodeURIComponent(imdbMatch[1])}`;
        }

        // Check for TMDB ID (tmdb: prefix or just digits)
        const tmdbWithPrefix = term.match(/^tmdb:?(\d+)$/i);
        if (tmdbWithPrefix) {
            return `/discover?tmdb_id=${encodeURIComponent(tmdbWithPrefix[1])}`;
        }

        // Check if it's just digits (treat as TMDB ID if >= 1)
        const digitsOnly = term.match(/^(\d+)$/);
        if (digitsOnly && parseInt(digitsOnly[1]) > 0) {
            return `/discover?tmdb_id=${encodeURIComponent(digitsOnly[1])}`;
        }

        // Default: regular search query
        return `/discover?q=${encodeURIComponent(term)}`;
    }

    const searchForm = document.getElementById('stats-search-form');
    if (searchForm) {
        searchForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const searchInput = document.getElementById('stats-search-input');
            const searchTerm = searchInput.value.trim();
            if (searchTerm) {
                // Create and show loading overlay to prevent white flash
                const overlay = document.createElement('div');
                overlay.style.cssText = `
                    position: fixed;
                    top: 0;
                    left: 0;
                    width: 100%;
                    height: 100%;
                    background: #0a0a0a;
                    z-index: 99999;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                `;

                const spinner = document.createElement('div');
                spinner.style.cssText = `
                    border: 4px solid rgba(255, 255, 255, 0.1);
                    border-top: 4px solid #ff6b35;
                    border-radius: 50%;
                    width: 40px;
                    height: 40px;
                    animation: spin 1s linear infinite;
                `;

                overlay.appendChild(spinner);
                document.body.appendChild(overlay);

                // Add keyframes for spinner if not already present
                if (!document.querySelector('#spinner-keyframes')) {
                    const style = document.createElement('style');
                    style.id = 'spinner-keyframes';
                    style.textContent = '@keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }';
                    document.head.appendChild(style);
                }

                // Navigate with ID detection
                window.location.href = buildSearchURL(searchTerm);
            }
        });
    }

    // Initialize toggles based on session state
    const timeFormatToggle = document.getElementById('time-format-toggle');
    const compactToggle = document.getElementById('compact-toggle');

    // Set initial states
    timeFormatToggle.checked = window.pageData.statistics.use_24hour_format;
    compactToggle.checked = window.pageData.statistics.compact_view;

    // Update displays
    updateActiveDownloadsDisplay();
    updateUsageStatsDisplay();

    // Apply color gradient for tangerine theme
    applyPercentageColorGradient();

    // Add click handler for rescrape buttons
    document.querySelectorAll('.rescrape-btn').forEach(button => {
        button.addEventListener('click', handleRescrapeClick);
    });

    // Initialize the external links modal
    if (typeof initializeExternalLinksModal === 'function') {
        initializeExternalLinksModal();
    } else if (typeof reattachExternalLinksListeners === 'function') {
        reattachExternalLinksListeners();
    }

    // --- Library Size Refresh Button (only if not in limited environment) ---
    const refreshSizeButton = document.getElementById('refresh-library-size-btn');
    const sizeDisplaySpan = document.getElementById('library-size-display');
    if (refreshSizeButton && sizeDisplaySpan) {
        refreshSizeButton.addEventListener('click', function() {
            const icon = refreshSizeButton.querySelector('i');
            // Indicate loading
            sizeDisplaySpan.textContent = 'Loading...';
            refreshSizeButton.disabled = true;
            if (icon) icon.classList.add('fa-spin');

            fetch('/statistics/api/library_size') // Ensure this path matches your blueprint prefix + route
                .then(response => {
                    if (!response.ok) {
                        throw new Error(`HTTP error! status: ${response.status}`);
                    }
                    return response.json();
                })
                .then(data => {
                    sizeDisplaySpan.textContent = data.total_library_size || 'Error';
                })
                .catch(error => {
                    console.error('Error fetching library size:', error);
                    sizeDisplaySpan.textContent = 'Error';
                })
                .finally(() => {
                    // Reset button
                    refreshSizeButton.disabled = false;
                    if (icon) icon.classList.remove('fa-spin');
                });
        });
    }

});

document.getElementById('time-format-toggle').addEventListener('change', function() {
    const isChecked = this.checked;

    // Disable the toggle while processing
    this.disabled = true;

    fetch('/set_time_preference', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        },
        body: JSON.stringify({
            use24HourFormat: isChecked
        })
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        if (data.status === 'OK') {
            // Log the data structure for debugging
            console.log('Time preference data:', data);

            // Update the UI with the new data without page reload
            updateUIWithTimePreference(data);
        } else {
            throw new Error('Failed to update time preference');
        }
    })
    .catch(error => {
        console.error('Error updating time preference:', error);
        // Revert the toggle state
        this.checked = !isChecked;
    })
    .finally(() => {
        // Re-enable the toggle
        this.disabled = false;
    });
});

// Function to update UI with new time preference data
function updateUIWithTimePreference(data) {
    console.log('updateUIWithTimePreference called with data:', data);
    // Update recently aired items
    const recentlyAiredList = document.querySelector('#recently_aired ul');
    if (recentlyAiredList && data.recently_aired) {
        console.log('Updating recently aired items');
        recentlyAiredList.innerHTML = data.recently_aired.map(item => {
            // Determine the class based on display_status
            let statusClass = '';
            if (item.display_status === 'uncollected') {
                statusClass = ' uncollected';
            } else if (item.display_status === 'checking_upgrade') {
                statusClass = ' checking_upgrade';
            }
            // Determine which buttons to show (matching template logic)
            let showSearchButton = item.display_status === 'uncollected' || item.display_status === 'checking_upgrade';
            let showMoveToWanted = item.display_status === 'checking_upgrade';
            let showRescrape = item.display_status === 'collected';

            return `
            <li>
                <div class="show-info clickable"
                     data-title="${item.title}"
                     data-show-title="${item.show_title || item.title}"
                     data-year="${item.year || ''}"
                     data-imdb-id="${item.imdb_id}"
                     data-tmdb-id="${item.tmdb_id}"
                     data-season="${item.season_number}"
                     data-episode="${item.episode_number}">
                    <span class="show-title${statusClass}">${item.title}</span>
                    <span class="air-time">${item.formatted_datetime}</span>
                </div>
                <div class="episode-actions">
                    ${showMoveToWanted ? `
                        <button class="refresh-btn"
                            data-imdb-id="${item.imdb_id}"
                            data-tmdb-id="${item.tmdb_id}"
                            data-season="${item.season_number}"
                            data-episode="${item.episode_number}"
                            title="Move back to Wanted state">
                            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M12 15V3"></path>
                                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                                <path d="m7 10 5 5 5-5"></path>
                            </svg>
                        </button>
                    ` : ''}
                </div>
                ${showRescrape ? `
                    <button class="rescrape-btn"
                        data-imdb-id="${item.imdb_id}"
                        data-tmdb-id="${item.tmdb_id}"
                        data-season="${item.season_number}"
                        data-episode="${item.episode_number}"
                        title="Move back to Wanted state">
                        <i class="recrape-icon"></i>
                    </button>
                ` : ''}
                ${showSearchButton ? `
                    <button class="search-episode-btn"
                        data-imdb-id="${item.imdb_id}"
                        data-tmdb-id="${item.tmdb_id}"
                        data-season="${item.season_number}"
                        data-episode="${item.episode_number}"
                        title="Search for this episode">
                        <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" width="20" height="20">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M10 21h7a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v11m0 5l4.879-4.879m0 0a3 3 0 104.243-4.242 3 3 0 00-4.243 4.242z"></path>
                        </svg>
                    </button>
                ` : ''}
            </li>
        `;
        }).join('');

        // Re-attach event listeners to all button types
        recentlyAiredList.querySelectorAll('.search-episode-btn').forEach(button => {
            button.addEventListener('click', handleSearchEpisode);
        });
        recentlyAiredList.querySelectorAll('.refresh-btn').forEach(button => {
            button.addEventListener('click', handleRefreshClick);
        });
        recentlyAiredList.querySelectorAll('.rescrape-btn').forEach(button => {
            button.addEventListener('click', handleRescrapeClick);
        });
    }

    // Update airing soon items
    const airingSoonList = document.querySelector('#airing_soon ul');
    if (airingSoonList && data.airing_soon) {
        console.log('Updating airing soon items');
        airingSoonList.innerHTML = data.airing_soon.map(item => `
            <li class="clickable"
                data-title="${item.title}"
                data-imdb-id="${item.imdb_id}"
                data-tmdb-id="${item.tmdb_id}"
                data-season="${item.season_number}"
                data-episode="${item.episode_number}">
                <span class="show-title">${item.title}</span>
                <span class="air-time${item.formatted_datetime.startsWith('Today') ? ' today' : ''}">${item.formatted_datetime}</span>
            </li>
        `).join('');
    }

    // Update upcoming releases
    const upcomingReleasesList = document.querySelector('#upcoming_releases ul');
    if (upcomingReleasesList && data.upcoming_releases) {
        console.log('Updating upcoming releases');
        upcomingReleasesList.innerHTML = data.upcoming_releases.map(release => `
            <li class="clickable"
                data-title="${release.titles || ''}"
                data-release-date="${release.release_date || ''}"
                data-tmdb-id="${release.tmdb_ids && release.tmdb_ids.length > 0 ? release.tmdb_ids : ''}"
                data-imdb-id="${release.imdb_ids && release.imdb_ids.length > 0 ? release.imdb_ids : ''}">
                <span class="titles">${release.titles}</span>
                <span class="release-date${release.is_soon ? ' soon' : ''}">${release.formatted_date}</span>
            </li>
        `).join('');
    }

    // Update recently added movies
    const recentMoviesContainer = document.querySelector('#recently_added_movies .stats-cards-container');
    if (recentMoviesContainer && data.recently_added && data.recently_added.movies) {
        console.log('Updating recently added movies');
        console.log('Recently added movies data:', JSON.stringify(data.recently_added.movies, null, 2));
        recentMoviesContainer.innerHTML = data.recently_added.movies.map(movie => {
            const collectedAt = movie.formatted_collected_at || movie.formatted_date || 'Unknown';
            const sizeStr = movie.formatted_size ? ` <span style="color:#e17c52;">•</span> Size: ${movie.formatted_size}` : '';
            const meta = `Version: ${movie.version}${sizeStr} <span style="color:#e17c52;">•</span> ${collectedAt}`;
            const isCompact = document.querySelector('.statistics-wrapper').classList.contains('compact-view');
            return `
            <div class="stats-card">
                ${isCompact ? `
                    <div class="file-details">
                        <span class="title">${movie.title} (${movie.year})</span>
                        <span class="metadata">${meta}</span>
                        <span class="filename">${movie.filled_by_file}</span>
                    </div>
                ` : `
                    <div class="stats-poster-container">
                        <img src="${movie.poster_url}" alt="${movie.title} poster" class="poster" loading="lazy">
                        <div class="stats-poster-hover">
                            <span class="title">${movie.title} (${movie.year})</span>
                            <span class="metadata">${meta}</span>
                            <span class="filename">${movie.filled_by_file}</span>
                        </div>
                    </div>
                `}
            </div>
        `;
        }).join('');
    }

    // Update recently added shows
    const recentShowsContainer = document.querySelector('#recently_added_shows .stats-cards-container');
    if (recentShowsContainer && data.recently_added && data.recently_added.shows) {
        console.log('Updating recently added shows');
        console.log('Recently added shows data:', JSON.stringify(data.recently_added.shows, null, 2));
        recentShowsContainer.innerHTML = data.recently_added.shows.map(show => {
            const collectedAt = show.formatted_collected_at || show.formatted_date || 'Unknown';
            const epStr = (show.season_number && show.episode_number) ? `S${String(show.season_number).padStart(2,'0')}E${String(show.episode_number).padStart(2,'0')} <span style="color:#e17c52;">•</span> ` : '';
            const sizeStr = show.formatted_size ? ` <span style="color:#e17c52;">•</span> Size: ${show.formatted_size}` : '';
            const verStr = show.version ? `Version: ${show.version}${sizeStr} <span style="color:#e17c52;">•</span> ` : '';
            const meta = `${epStr}${verStr}${collectedAt}`;
            const isCompact = document.querySelector('.statistics-wrapper').classList.contains('compact-view');
            return `
            <div class="stats-card">
                ${isCompact ? `
                    <div class="file-details">
                        <span class="title">${show.title}${show.year ? ` (${show.year})` : ''}</span>
                        <span class="metadata">${meta}</span>
                        <span class="filename">${show.filled_by_file}</span>
                    </div>
                ` : `
                    <div class="stats-poster-container">
                        <img src="${show.poster_url}" alt="${show.title} poster" class="poster" loading="lazy">
                        <div class="stats-poster-hover">
                            <span class="title">${show.title}${show.year ? ` (${show.year})` : ''}</span>
                            <span class="metadata">${meta}</span>
                            <span class="filename">${show.filled_by_file}</span>
                        </div>
                    </div>
                `}
            </div>
        `;
        }).join('');
    }

    // Update recently upgraded items if they exist
    const recentlyUpgradedContainer = document.querySelector('#recently_upgraded .stats-cards-container');
    if (recentlyUpgradedContainer && data.recently_upgraded) {
        console.log('Updating recently upgraded items');
        console.log('Recently upgraded items data:', JSON.stringify(data.recently_upgraded, null, 2));

        // Debug each item's date fields
        data.recently_upgraded.forEach((item, index) => {
            console.log(`Item ${index} dates:`, {
                title: item.title,
                formatted_date: item.formatted_date,
                original_collected_at: item.original_collected_at,
                collected_at: item.collected_at,
                last_updated: item.last_updated
            });
        });

        const upgradeHtml = data.recently_upgraded.map(item => {
            console.log('Processing upgraded item:', item);
            const yearInfo = item.year ? ` (${item.year})` : '';
            const episodeInfo = (item.type === 'episode' && item.season_number !== null && item.episode_number !== null && ' - ') ? 
                `S${String(item.season_number).padStart(2, '0')}E${String(item.episode_number).padStart(2, '0')}` : '';
            const versionInfo = item.version ? `Version: ${item.version}` : '';

            // Ensure dates are properly formatted
            const toDate = item.formatted_date || 'Unknown';
            const fromDate = item.original_collected_at || 'Unknown';

            if (document.querySelector('.statistics-wrapper').classList.contains('compact-view')) {
                return `<div class="stats-card">
                    <div class="file-details">
                        <span class="title">${item.title}${yearInfo}</span>
                        <span class="metadata">${episodeInfo}${versionInfo} - ${toDate}</span>
                        <span class="filename" data-label="From:">${item.upgrading_from || 'Unknown'}</span>
                        <span class="filename" data-label="To:">${item.filled_by_file || 'Unknown'}</span>
                    </div>
                </div>`;
            } else {
                return `<div class="stats-card">
                    <div class="stats-poster-container">
                        <img src="${item.poster_url || ''}" alt="${item.title} poster" class="poster" loading="lazy">
                        <div class="stats-poster-hover">
                            <span class="title">${item.title}${yearInfo}</span>
                            <span class="metadata">${episodeInfo}${versionInfo} - ${toDate}</span>
                            <span class="filename" data-label="From:">${item.upgrading_from || 'Unknown'}</span>
                            <span class="filename" data-label="To:">${item.filled_by_file || 'Unknown'}</span>
                        </div>
                    </div>
                </div>`;
            }
        }).join('');

        recentlyUpgradedContainer.innerHTML = upgradeHtml;
    }

    // Reattach click event listeners for external links modal after updating content
    reattachExternalLinksListeners();

    // Re-apply quality badges to newly rendered cards
    document.querySelectorAll('.stats-card[data-badges-applied]').forEach(c => c.removeAttribute('data-badges-applied'));
    setTimeout(applyQualityBadges, 100);

    // Update the toggle-bar Broken/Sub pill, mode-aware
    try {
        updateStatsToggleBarPill(data);
    } catch (e) {}
}

// Updates the toggle-bar Sub and Broken pills. Each has its own
// element: Combined mode shows both, usenet mode only Broken and
// debrid mode only Sub, so a missing element is simply skipped.
function updateStatsToggleBarPill(data) {
    const activeDownloadsData = data.active_downloads_data ||
                              (data.stats && data.stats.active_downloads_data) ||
                              null;
    const source = activeDownloadsData && activeDownloadsData.source;

    const brokenToggle = document.getElementById('broken-count-toggle');
    if (brokenToggle && (source === 'climount' || source === 'combined')) {
        const nzbs = activeDownloadsData.broken_nzbs || 0;
        const debrid = activeDownloadsData.broken_debrid;
        const total = nzbs + (debrid || 0);
        brokenToggle.textContent = (debrid === null || debrid === undefined)
            ? `${nzbs}`
            : `${nzbs} NZB \u00b7 ${debrid} debrid`;
        brokenToggle.classList.toggle('critical', total > 0);
        const brokenIcon = document.getElementById('broken-count-icon');
        if (brokenIcon) brokenIcon.style.color = total > 0 ? 'var(--st-red)' : 'var(--st-yellow)';
    }

    const subToggle = document.getElementById('subscription-days-toggle');
    if (!subToggle) return;
    const subData = data.subscription_status || (data.stats && data.stats.subscription_status) || null;
    if (subData && subData.days_remaining !== null && subData.days_remaining !== undefined) {
        subToggle.textContent = `${subData.days_remaining} days`;
        subToggle.classList.toggle('critical', Number(subData.days_remaining) < 10);
    } else if (subData) {
        subToggle.textContent = 'N/A';
        subToggle.classList.remove('critical');
    }
}

// Function to reattach external links listeners
function reattachExternalLinksListeners() {
    console.log('Reattaching external links event listeners');

    // Get modal elements
    const modal = document.getElementById('external-links-modal');
    const modalTitle = document.getElementById('modal-title');
    const traktLink = document.getElementById('trakt-link');
    const tmdbLink = document.getElementById('tmdb-link');
    const imdbLink = document.getElementById('imdb-link');

    // Add click event listeners to clickable items
    document.querySelectorAll('.clickable').forEach(item => {
        // Remove existing event listeners to prevent duplicates
        const newItem = item.cloneNode(true);
        item.parentNode.replaceChild(newItem, item);

        newItem.addEventListener('click', function(e) {
            // Don't trigger if clicking on action buttons
            if (e.target.closest('.search-episode-btn, .refresh-btn, .rescrape-btn, .episode-actions')) {
                return;
            }

            // Prevent event from bubbling to parent elements
            e.stopPropagation();

            // Get data attributes
            const imdbId = this.dataset.imdbId;
            const tmdbId = this.dataset.tmdbId;
            const inLibrary = this.dataset.inLibrary === 'true';
            const mediaType = this.dataset.mediaType;
            const season = this.dataset.season;
            const episode = this.dataset.episode;

            const episodeParams = (mediaType === 'episode' && season)
                ? `?season=${season}${episode ? `&episode=${episode}` : ''}`
                : '';

            // Determine which page to navigate to
            let url;

            if (inLibrary) {
                // Navigate to library page
                if (mediaType === 'episode' && imdbId) {
                    url = `/library/show/${imdbId}${episodeParams}`;
                } else if (mediaType === 'movie' && imdbId) {
                    url = `/library/movie/${imdbId}`;
                } else if (tmdbId) {
                    if (mediaType === 'episode') {
                        url = `/library/show/${tmdbId}${episodeParams}`;
                    } else {
                        url = `/library/movie/${tmdbId}`;
                    }
                }
            } else {
                // Navigate to discover details page
                if (tmdbId) {
                    const type = mediaType === 'episode' ? 'tv' : 'movie';
                    url = `/discover/details/${tmdbId}/${type}${episodeParams}`;
                } else if (imdbId) {
                    const type = mediaType === 'episode' ? 'tv' : 'movie';
                    url = `/discover/details/${imdbId}/${type}${episodeParams}`;
                }
            }

            // Navigate to the URL
            if (url) {
                window.location.href = url;
            }
        });
    });
}

// Separate the click handler function for reusability
function handleRescrapeClick() {
    const btn = this;
    const data = {
        imdb_id: btn.dataset.imdbId,
        tmdb_id: btn.dataset.tmdbId,
        season_number: parseInt(btn.dataset.season),
        episode_number: parseInt(btn.dataset.episode)
    };

    // Disable button while processing
    btn.disabled = true;

    fetch('/statistics/move_to_wanted', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(data)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Update UI to show item is no longer collected
            const showTitle = btn.closest('li').querySelector('.show-title');
            showTitle.classList.add('uncollected');
            btn.remove(); // Remove the button
        } else {
            throw new Error(data.error || 'Failed to move item to Wanted state');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showPopup({ type: 'error', title: 'Error', message: 'Error moving item to Wanted state: ' + error.message, autoClose: 5000 });
        btn.disabled = false;
    });
}

// Handle search episode button click
async function handleSearchEpisode(event) {
    const btn = event.currentTarget;
    const showInfo = btn.closest('li').querySelector('.show-info');

    const imdbId = btn.dataset.imdbId;
    const tmdbId = btn.dataset.tmdbId;
    const season = parseInt(btn.dataset.season);
    const episode = parseInt(btn.dataset.episode);

    // Use pre-parsed show_title and year from data attributes (avoids regex parsing issues)
    const cleanTitle = showInfo.dataset.showTitle || showInfo.dataset.title.replace(/\s+S\d+E\d+.*$/i, '').replace(/\s*\(\d{4}\)\s*$/, '').trim();
    const year = showInfo.dataset.year || new Date().getFullYear().toString();

    // Version is looked up server-side for recently_aired fixes; pass null here
    const version = null;

    // Debug logging
    console.log('[Statistics] Search episode click - data:', {
        title: cleanTitle,
        year: year,
        imdbId: imdbId,
        tmdbId: tmdbId,
        mediaId: tmdbId || imdbId,
        season: season,
        episode: episode,
        version: version
    });

    // Use selectMedia function from scraper.js
    if (typeof selectMedia === 'function') {
        await selectMedia(
            tmdbId || imdbId,
            cleanTitle,
            year,
            'tv',
            season,
            episode,
            false, // multi = false for single episode
            null, // genre_ids
            version,
            'recently_aired' // source_context: fix missed item, update existing entry
        );
    } else {
        console.error('[Statistics] selectMedia function not found - scraper.js may not be loaded');
        showPopup({ type: 'error', title: 'Error', message: 'Error: Search functionality not available', autoClose: 5000 });
    }
}

// Handle refresh button click (move back to wanted)
function handleRefreshClick(event) {
    const btn = event.currentTarget;
    const data = {
        imdb_id: btn.dataset.imdbId,
        tmdb_id: btn.dataset.tmdbId,
        season_number: parseInt(btn.dataset.season),
        episode_number: parseInt(btn.dataset.episode)
    };

    // Disable button while processing
    btn.disabled = true;

    fetch('/statistics/move_to_wanted', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(data)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Reload the page to reflect the updated state
            window.location.reload();
        } else {
            throw new Error(data.error || 'Failed to move item to Wanted state');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showPopup({ type: 'error', title: 'Error', message: 'Error moving item to Wanted state: ' + error.message, autoClose: 5000 });
        btn.disabled = false;
    });
}

// Function to attach event listeners - will be called after DOM is ready
function attachRecentlyAiredListeners() {
    console.log('[Statistics] Attaching recently aired button listeners');

    // Attach event listeners to recently aired action buttons
    const searchBtns = document.querySelectorAll('.recently-aired .search-episode-btn');
    console.log('[Statistics] Found', searchBtns.length, 'search episode buttons');
    searchBtns.forEach(btn => {
        btn.addEventListener('click', handleSearchEpisode);
    });

    const refreshBtns = document.querySelectorAll('.recently-aired .refresh-btn');
    console.log('[Statistics] Found', refreshBtns.length, 'refresh buttons');
    refreshBtns.forEach(btn => {
        btn.addEventListener('click', handleRefreshClick);
    });

    // Attach event listeners to old rescrape buttons for collected episodes
    const rescrapeBtns = document.querySelectorAll('.recently-aired .rescrape-btn');
    console.log('[Statistics] Found', rescrapeBtns.length, 'rescrape buttons');
    rescrapeBtns.forEach(btn => {
        btn.addEventListener('click', handleRescrapeClick);
    });
}

// Call the function to attach listeners after DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    console.log('[Statistics] DOM Content Loaded - attaching recently aired listeners');
    attachRecentlyAiredListeners();

    // Fetch available versions for torrent modal (from scraper.js)
    if (typeof fetchVersions === 'function') {
        fetchVersions().catch(err => console.warn('[Statistics] Failed to fetch versions:', err));
    }
});

document.getElementById('compact-toggle').addEventListener('change', function() {
    const isChecked = this.checked;

    // Disable the toggle while processing
    this.disabled = true;

    fetch('/set_compact_preference', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        },
        body: JSON.stringify({
            compactView: isChecked
        })
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        if (data.success) {
            // Instead of immediate reload, use a small timeout
            setTimeout(() => {
                window.location.reload();
            }, 100);
        } else {
            throw new Error('Failed to update compact preference');
        }
    })
    .catch(error => {
        // Only log and revert if it's not an AbortError (caused by navigation)
        if (error.name !== 'AbortError') {
            console.error('Error updating compact preference:', error);
            // Revert the toggle state
            this.checked = !isChecked;
        }
    })
    .finally(() => {
        // Re-enable the toggle
        this.disabled = false;
    });
});
