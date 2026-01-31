/**
 * Discover Details Page JavaScript
 * Handles display of TMDB content details for items not in library
 */

document.addEventListener('DOMContentLoaded', function() {
    initDiscoverDetails();
});

let contentData = null;
let discoverVersions = [];

async function initDiscoverDetails() {
    const container = document.querySelector('.movie-container');
    if (!container) return;

    const tmdbId = container.dataset.tmdbId;
    const mediaType = container.dataset.mediaType;

    if (!tmdbId || !mediaType) {
        showError('Invalid content ID or type');
        return;
    }

    try {
        // Initialize modal event listeners (safe to fail)
        initializeModalListeners();
    } catch (e) {
        console.warn('Modal initialization failed:', e);
    }

    // Fetch available versions in background (don't block page load)
    fetchVersions().catch(e => console.warn('Failed to fetch versions:', e));

    try {
        await loadContentDetails(tmdbId, mediaType);
    } catch (error) {
        console.error('Failed to load content details:', error);
        showError(error.message || 'Failed to load content details');
    }
}

async function fetchVersions() {
    try {
        const response = await fetch('/content/versions');
        const data = await response.json();
        if (data.versions) {
            discoverVersions = data.versions;

            // Also set scraper.js global variable so torrent modal can access versions
            if (typeof availableVersions !== 'undefined') {
                availableVersions = data.versions;
            }
        }
    } catch (error) {
        console.error('Error fetching versions:', error);
    }
}

function initializeModalListeners() {
    // Confirm button
    const confirmBtn = document.getElementById('confirmVersions');
    if (confirmBtn) {
        confirmBtn.addEventListener('click', handleConfirmRequest);
    }

    // Cancel button
    const cancelBtn = document.getElementById('cancelVersions');
    if (cancelBtn) {
        cancelBtn.addEventListener('click', closeVersionModal);
    }

    // Close modal when clicking outside
    const modal = document.getElementById('versionModal');
    if (modal) {
        modal.addEventListener('click', function(event) {
            if (event.target === modal) {
                closeVersionModal();
            }
        });
    }
}

async function loadContentDetails(tmdbId, mediaType) {
    const response = await fetch(`/discover/details/${tmdbId}/${mediaType}/data`);

    if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error || 'Failed to fetch content details');
    }

    contentData = await response.json();
    displayContent(contentData);
    // Align sidebar with files container
    alignSidebarWithFiles(); 
}

function displayContent(data) {
    // Hide loading, show content
    document.getElementById('loading-state').style.display = 'none';
    document.getElementById('page-actions').style.display = 'flex';
    document.getElementById('movie-main-grid').style.display = 'grid';

    // Set backdrop
    if (data.backdrop_path) {
        const backdrop = document.getElementById('page-backdrop');
        const backdropImg = document.getElementById('page-backdrop-img');
        backdropImg.src = `https://image.tmdb.org/t/p/w1280${data.backdrop_path}`;
        backdrop.style.display = 'block';
    }

    // Set status badge and inline metadata
    const statusBadge = document.getElementById('show-status-badge');
    if (statusBadge && data.status) {
        statusBadge.textContent = data.status;

        // Add color-coded class based on status
        const statusLower = data.status.toLowerCase().replace(/\s+/g, '-');
        statusBadge.className = `status-badge status-${statusLower}`;
    }

    // Set rating with star icon
    const ratingText = document.getElementById('show-rating-text');
    const ratingSeparator = document.getElementById('show-rating-separator');
    if (ratingText) {
        if (data.vote_average) {
            // Display rating with 1 decimal point and star icon
            const ratingValue = parseFloat(data.vote_average).toFixed(1);
            ratingText.innerHTML = `
                <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="width: 1em; height: 1em; display: inline-block; vertical-align: text-top; margin-right: 0.25rem;">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z"></path>
                </svg>${ratingValue}
            `;
            // Show the separator after rating
            if (ratingSeparator) {
                ratingSeparator.style.display = 'inline';
            }
        } else {
            ratingText.style.display = 'none';
            // Hide the separator if no rating
            if (ratingSeparator) {
                ratingSeparator.style.display = 'none';
            }
        }
    }

    // Set poster
    const posterImg = document.getElementById('poster-img');
    if (data.poster_path) {
        posterImg.src = `https://image.tmdb.org/t/p/w500${data.poster_path}`;
    }

    // Set title
    document.getElementById('movie-title').textContent = data.title + (data.year ? ` (${data.year})` : '');
    document.title = `${data.title} - Discover`;

    // Update library status badge based on db_status
    const libraryStatusBadge = document.querySelector('.status-badge.missing');
    if (libraryStatusBadge && data.db_status) {
        const dbStatus = data.db_status.toLowerCase();
        // Find the text node after the SVG
        const textNodes = Array.from(libraryStatusBadge.childNodes).filter(node => node.nodeType === Node.TEXT_NODE);

        if (dbStatus === 'partial') {
            if (textNodes.length > 0) {
                textNodes[textNodes.length - 1].textContent = 'Partially in Library';
            }
            libraryStatusBadge.classList.remove('missing');
            libraryStatusBadge.classList.add('partial');
        } else if (dbStatus === 'collected' || dbStatus === 'present') {
            if (textNodes.length > 0) {
                textNodes[textNodes.length - 1].textContent = 'In Library';
            }
            libraryStatusBadge.classList.remove('missing');
            libraryStatusBadge.classList.add('collected');
        }
    }

    // Set year
    //document.getElementById('movie-year-text').textContent = data.year || '-';

    // Set runtime/episodes
    if (data.media_type === 'tv') {
        const episodeCount = data.number_of_episodes || 0;
        const seasonCount = data.number_of_seasons || 0;
        document.getElementById('movie-runtime-text').textContent = `${seasonCount} Seasons, ${episodeCount} Episodes`;

        // Show TV-specific fields
        document.getElementById('director-row').style.display = 'none';
        document.getElementById('status-row').style.display = 'flex';
        document.getElementById('network-row').style.display = 'flex';
        document.getElementById('movie-status').textContent = data.status || '-';
        document.getElementById('movie-network').textContent = (data.networks || []).join(', ') || '-';

        // Show Season Packs button for TV shows
        const seasonPacksBtn = document.getElementById('btn-season-packs');
        if (seasonPacksBtn) {
            seasonPacksBtn.style.display = 'inline-flex';
        }
    } else {
        const runtime = data.runtime ? `${data.runtime} min` : '-';
        document.getElementById('movie-runtime-text').textContent = runtime;
        document.getElementById('movie-runtime').textContent = runtime;

        // Show movie-specific fields
        if (data.directors && data.directors.length > 0) {
            document.getElementById('movie-director').textContent = data.directors.join(', ');
        }

        // Show Search button for movies
        const searchMovieBtn = document.getElementById('btn-search-movie');
        if (searchMovieBtn) {
            searchMovieBtn.style.display = 'inline-flex';
        }
    }

    // Set genres
    const genres = (data.genres || []).join(', ') || '-';
    document.getElementById('movie-genres-text').textContent = genres;
    document.getElementById('movie-genres').textContent = genres;

    // Set tagline
    if (data.tagline) {
        const taglineEl = document.getElementById('movie-tagline');
        taglineEl.textContent = `"${data.tagline}"`;
        taglineEl.style.display = 'block';
    }

    // Set overview
    document.getElementById('movie-overview').textContent = data.overview || 'No overview available.';

    // Set release date
    const releaseDate = data.release_date || data.first_air_date || '-';
    document.getElementById('movie-release-date').textContent = releaseDate;

    // Set rating
    if (data.vote_average) {
        const rating = data.vote_average.toFixed(1);
        const voteCount = data.vote_count ? ` (${data.vote_count.toLocaleString()} votes)` : '';
        document.getElementById('movie-rating').textContent = `${rating}/10${voteCount}`;
    }

    // Set TMDB link
    const tmdbType = data.media_type === 'tv' ? 'tv' : 'movie';
    const tmdbLink = document.getElementById('link-tmdb');
    tmdbLink.href = `https://www.themoviedb.org/${tmdbType}/${data.tmdb_id}`;

    const tmdbLinkDetail = document.getElementById('tmdb-link-detail');
    tmdbLinkDetail.href = `https://www.themoviedb.org/${tmdbType}/${data.tmdb_id}`;
    tmdbLinkDetail.textContent = data.tmdb_id;

    // Set IMDb link if available
    if (data.imdb_id) {
        const imdbLink = document.getElementById('link-imdb');
        imdbLink.href = `https://www.imdb.com/title/${data.imdb_id}`;
        imdbLink.style.display = 'inline-flex';

        const imdbRow = document.getElementById('imdb-row');
        imdbRow.style.display = 'flex';
        const imdbLinkDetail = document.getElementById('imdb-link-detail');
        imdbLinkDetail.href = `https://www.imdb.com/title/${data.imdb_id}`;
        imdbLinkDetail.textContent = data.imdb_id;

        // Set Trakt link (uses IMDb ID)
        const traktLink = document.getElementById('link-trakt');
        const traktType = data.media_type === 'tv' ? 'shows' : 'movies';
        traktLink.href = `https://trakt.tv/${traktType}/${data.imdb_id}`;
        traktLink.style.display = 'inline-flex';
    }

    // Display cast
    if (data.cast && data.cast.length > 0) {
        displayCast(data.cast);
    }

    // Display seasons with episodes for TV shows
    if (data.media_type === 'tv' && data.seasons && data.seasons.length > 0) {
        loadAndDisplaySeasons(data);
    }

    // Set up request button (for whole show/movie)
    setupRequestButton(data);

    // Set up season packs button
    setupSeasonPacksButton(data);

    // Set up search movie button (movies only)
    setupSearchMovieButton(data);
}

function displayCast(cast) {
    const castSection = document.getElementById('cast-section');
    const castGrid = document.getElementById('cast-grid');
    const castHeader = document.getElementById('cast-header');

    castGrid.innerHTML = cast.map(person => `
        <div class="cast-card">
            <img src="${person.profile_path ? `https://image.tmdb.org/t/p/w185${person.profile_path}` : '/static/images/placeholder.png'}"
                 alt="${person.name}"
                 class="cast-photo"
                 onerror="this.src='/static/images/placeholder.png'">
            <div class="cast-name">${person.name}</div>
            <div class="cast-character">${person.character || ''}</div>
        </div>
    `).join('');

    // Add click handler for collapse/expand toggle
    if (castHeader) {
        castHeader.addEventListener('click', function() {
            castSection.classList.toggle('collapsed');
        });
    }

    castSection.style.display = 'block';
}

async function loadAndDisplaySeasons(data) {
    const seasonsContainer = document.getElementById('seasons-container');
    const tabsNav = document.getElementById('season-tabs-nav');
    const tabsContent = document.getElementById('season-tabs-content');

    if (!seasonsContainer || !tabsNav || !tabsContent) return;

    // Filter out specials (season 0) and sort by season number
    const regularSeasons = data.seasons
        .filter(s => s.season_number > 0)
        .sort((a, b) => a.season_number - b.season_number);

    if (regularSeasons.length === 0) return;

    tabsNav.innerHTML = '';
    tabsContent.innerHTML = '';

    // Fetch episode details for each season
    for (let i = 0; i < regularSeasons.length; i++) {
        const season = regularSeasons[i];
        const isActive = i === 0;

        // Create tab button
        const tabBtn = createSeasonTab(season, isActive);
        tabsNav.appendChild(tabBtn);

        // Create tab panel (initially with loading state, will be filled with episodes)
        const tabPanel = createSeasonPanel(season, isActive, data);
        tabsContent.appendChild(tabPanel);

        // Add click listener for tab switching
        tabBtn.addEventListener('click', () => switchTab(season.season_number));

        // Load episodes for this season asynchronously
        loadSeasonEpisodes(data.tmdb_id, season.season_number, data);
    }

    seasonsContainer.style.display = 'block';
}

function createSeasonTab(season, isActive) {
    const totalEpisodes = season.episode_count || 0;

    const tab = document.createElement('button');
    tab.className = `season-tab ${isActive ? 'active' : ''}`;
    tab.dataset.season = season.season_number;
    tab.setAttribute('type', 'button');

    tab.innerHTML = `
        <div class="season-tab-title">Season ${season.season_number}</div>
        <div class="season-tab-stats">0 / ${totalEpisodes}</div>
        <div class="season-tab-progress">
            <div class="season-tab-progress-bar">
                <div class="season-tab-progress-fill" style="width: 0%"></div>
            </div>
        </div>
    `;

    return tab;
}

function createSeasonPanel(season, isActive, showData) {
    const panel = document.createElement('div');
    panel.className = `season-panel ${isActive ? 'active' : ''}`;
    panel.dataset.season = season.season_number;

    // Add season header (without delete button for discover)
    const seasonHeader = document.createElement('div');
    seasonHeader.className = 'discover-season-header';
    seasonHeader.innerHTML = `<h3>Season ${season.season_number}</h3>`;
    panel.appendChild(seasonHeader);

    // Add loading placeholder
    const loadingDiv = document.createElement('div');
    loadingDiv.className = 'episodes-loading';
    loadingDiv.innerHTML = '<p style="opacity: 0.5; padding: 1rem;">Loading episodes...</p>';
    panel.appendChild(loadingDiv);

    return panel;
}

async function loadSeasonEpisodes(tmdbId, seasonNumber, showData) {
    try {
        const response = await fetch(`/discover/details/${tmdbId}/tv/season/${seasonNumber}`);
        if (!response.ok) {
            throw new Error('Failed to fetch season data');
        }

        const seasonData = await response.json();
        displaySeasonEpisodes(seasonNumber, seasonData.episodes || [], showData);
    } catch (error) {
        console.error(`Failed to load episodes for season ${seasonNumber}:`, error);
        const panel = document.querySelector(`.season-panel[data-season="${seasonNumber}"]`);
        if (panel) {
            const loading = panel.querySelector('.episodes-loading');
            if (loading) {
                loading.innerHTML = '<p style="opacity: 0.5; padding: 1rem; color: #f87171;">Failed to load episodes</p>';
            }
        }
    }
}

function displaySeasonEpisodes(seasonNumber, episodes, showData) {
    const panel = document.querySelector(`.season-panel[data-season="${seasonNumber}"]`);
    if (!panel) return;

    // Remove loading placeholder
    const loading = panel.querySelector('.episodes-loading');
    if (loading) {
        loading.remove();
    }

    // Sort episodes by episode number
    episodes.sort((a, b) => a.episode_number - b.episode_number);

    // Count collected episodes for season tab update
    const collectedCount = episodes.filter(ep => ep.db_data && ep.db_data.state === 'Collected').length;
    updateSeasonTabProgress(seasonNumber, collectedCount, episodes.length);

    // Render each episode
    episodes.forEach(episode => {
        const episodeRow = createEpisodeRow(episode, seasonNumber, showData);
        panel.appendChild(episodeRow);
    });
}

function updateSeasonTabProgress(seasonNumber, collectedCount, totalCount) {
    const tab = document.querySelector(`.season-tab[data-season="${seasonNumber}"]`);
    if (!tab) return;

    const statsEl = tab.querySelector('.season-tab-stats');
    const progressFill = tab.querySelector('.season-tab-progress-fill');

    if (statsEl) {
        statsEl.textContent = `${collectedCount} / ${totalCount}`;
    }

    if (progressFill) {
        const percentage = totalCount > 0 ? (collectedCount / totalCount) * 100 : 0;
        progressFill.style.width = `${percentage}%`;
    }
}

function createEpisodeRow(episode, seasonNumber, showData) {
    const row = document.createElement('div');
    const hasDbData = episode.db_data;

    // Check permissions
    const hasUserPermissions = document.getElementById('has_user_permissions')?.value === 'True';

    // Use episode-row class for collected episodes (to match library styling)
    row.className = hasDbData ? 'episode-row' : 'discover-episode-row';

    if (hasDbData) {
        // Episode exists in database - show full details like Library page
        const dbData = episode.db_data;
        const state = dbData.state || '';
        const isCollected = state === 'Collected';
        const isUpgrading = state === 'Upgrading';
        const isBlacklisted = state === 'Blacklisted';
        const isUnreleased = state === 'Unreleased';
        const isWanted = state === 'Wanted';

        // Status icon
        let statusIcon = '';
        if (isCollected) {
            statusIcon = `
                <svg class="episode-status-icon collected" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
                    <path fill-rule="evenodd" d="M2.25 12c0-5.385 4.365-9.75 9.75-9.75s9.75 4.365 9.75 9.75-4.365 9.75-9.75 9.75S2.25 17.385 2.25 12zm13.36-1.814a.75.75 0 10-1.22-.872l-3.236 4.53L9.53 12.22a.75.75 0 00-1.06 1.06l2.25 2.25a.75.75 0 001.14-.094l3.75-5.25z" clip-rule="evenodd" />
                </svg>
            `;
        } else if (isUpgrading) {
            statusIcon = `
                <svg class="episode-status-icon up-arrow" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
                    <path fill-rule="evenodd" d="M12 2.25c-5.385 0-9.75 4.365-9.75 9.75s4.365 9.75 9.75 9.75 9.75-4.365 9.75-9.75S17.385 2.25 12 2.25zm.53 5.47a.75.75 0 00-1.06 0l-3 3a.75.75 0 101.06 1.06l1.72-1.72v6.19a.75.75 0 001.5 0V10.06l1.72 1.72a.75.75 0 101.06-1.06l-3-3z" clip-rule="evenodd"></path>
                </svg>
            `;
        } else if (isBlacklisted) {
            statusIcon = `
                <svg class="episode-status-icon unavailable" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
                    <path fill-rule="evenodd" d="M12 2.25c-5.385 0-9.75 4.365-9.75 9.75s4.365 9.75 9.75 9.75 9.75-4.365 9.75-9.75S17.385 2.25 12 2.25zm-1.72 6.97a.75.75 0 10-1.06 1.06L10.94 12l-1.72 1.72a.75.75 0 101.06 1.06L12 13.06l1.72 1.72a.75.75 0 101.06-1.06L13.06 12l1.72-1.72a.75.75 0 10-1.06-1.06L12 10.94l-1.72-1.72z" clip-rule="evenodd" />
                </svg>
            `;
        } else if (isUnreleased) {
            statusIcon = `
                <svg class="episode-status-icon unreleased" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
                    <path fill-rule="evenodd" d="M12 2.25c-5.385 0-9.75 4.365-9.75 9.75s4.365 9.75 9.75 9.75 9.75-4.365 9.75-9.75S17.385 2.25 12 2.25zM12 8.25a.75.75 0 01.75.75v3.69l2.28 2.28a.75.75 0 11-1.06 1.06l-2.5-2.5a.75.75 0 01-.22-.53V9a.75.75 0 01.75-.75z" clip-rule="evenodd" />
                </svg>
            `;
        } else if (isWanted) {
            statusIcon = `
                <svg class="episode-status-icon wanted" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
                    <path fill-rule="evenodd" d="M12 2.25c-5.385 0-9.75 4.365-9.75 9.75s4.365 9.75 9.75 9.75 9.75-4.365 9.75-9.75S17.385 2.25 12 2.25zm4.28 7.22a.75.75 0 010 1.06l-3 3a.75.75 0 01-1.06-1.06l1.72-1.72H9a.75.75 0 010-1.5h4.94l-1.72-1.72a.75.75 0 011.06-1.06l3 3zm-8.56 5.56a.75.75 0 010-1.06l3-3a.75.75 0 011.06 1.06l-1.72 1.72H15a.75.75 0 010 1.5h-4.94l1.72 1.72a.75.75 0 01-1.06 1.06l-3-3z" clip-rule="evenodd" />
                </svg>
            `;
        } else {
            statusIcon = `
                <svg class="episode-status-icon unavailable" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
                    <path fill-rule="evenodd" d="M12 2.25c-5.385 0-9.75 4.365-9.75 9.75s4.365 9.75 9.75 9.75 9.75-4.365 9.75-9.75S17.385 2.25 12 2.25zm-1.72 6.97a.75.75 0 10-1.06 1.06L10.94 12l-1.72 1.72a.75.75 0 101.06 1.06L12 13.06l1.72 1.72a.75.75 0 101.06-1.06L13.06 12l1.72-1.72a.75.75 0 10-1.06-1.06L12 10.94l-1.72-1.72z" clip-rule="evenodd" />
                </svg>
            `;
        }

        // Build metadata parts
        let metaParts = [];
        if (dbData.version) {
            metaParts.push(`<span class="episode-version">${escapeHtml(dbData.version)}</span>`);
        }
        if (dbData.files && dbData.files.length > 0) {
            const totalSize = dbData.files.reduce((sum, f) => sum + (f.size || 0), 0);
            if (totalSize > 0) {
                metaParts.push(`<span class="episode-version">${totalSize.toFixed(2)} GB</span>`);
            }
        }
        if (episode.air_date) {
            const today = new Date().toISOString().split('T')[0];
            const label = episode.air_date > today ? 'Airing' : 'Aired';
            metaParts.push(`<span>${label}: ${formatDate(episode.air_date)}</span>`);
        }
        if (dbData.collected_at) {
            metaParts.push(`<span>Collected: ${formatDate(dbData.collected_at)}</span>`);
        } else if (state) {
            metaParts.push(`<span>Status: ${escapeHtml(state)}</span>`);
        }

        // Action buttons (search button only for User/Admin)
        const searchIcon = hasUserPermissions ? `
            <button class="search-episode-btn" type="button" title="Search for this episode"
                    data-imdb-id="${dbData.imdb_id || showData.imdb_id || ''}"
                    data-tmdb-id="${showData.tmdb_id || ''}"
                    data-season="${seasonNumber}"
                    data-episode="${episode.episode_number}"
                    data-title="${escapeHtml(showData.title || '')}">
                <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" width="16" height="16">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M10 21h7a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v11m0 5l4.879-4.879m0 0a3 3 0 104.243-4.242 3 3 0 00-4.243 4.242z"></path>
                </svg>
            </button>
        ` : '';

        const refreshIcon = `
            <button class="refresh-btn" type="button" title="Move back to Wanted state"
                    data-imdb-id="${dbData.imdb_id || showData.imdb_id || ''}"
                    data-tmdb-id="${showData.tmdb_id || ''}"
                    data-season="${seasonNumber}"
                    data-episode="${episode.episode_number}"
                    data-title="${escapeHtml(showData.title || '')}">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M12 15V3"></path>
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                    <path d="m7 10 5 5 5-5"></path>
                </svg>
            </button>
        `;

        const deleteIcon = `
            <button class="delete-episode-btn" type="button" title="Delete this episode"
                    data-imdb-id="${dbData.imdb_id || showData.imdb_id || ''}"
                    data-season="${seasonNumber}"
                    data-episode="${episode.episode_number}"
                    data-files='${JSON.stringify(dbData.files || [])}'>
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="3 6 5 6 21 6"></polyline>
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                </svg>
            </button>
        `;

        const filesIcon = dbData.files && dbData.files.length > 0 ? `
            <button class="files-toggle-btn" type="button" title="View files (${dbData.files.length})">
                <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path>
                </svg>
                <span class="files-count">${dbData.files.length}</span>
            </button>
            <div class="episode-files-panel">
                <button class="files-panel-close" type="button" aria-label="Close">
                    <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"></path>
                    </svg>
                </button>
                <div class="episode-files">
                    ${dbData.files.map(f => `<div class="episode-file" title="${escapeHtml(f.basename || f.path)}">${escapeHtml(f.basename || f.path)}</div>`).join('')}
                </div>
            </div>
        ` : `
            <button class="files-toggle-btn" type="button" title="No files" disabled>
                <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path>
                </svg>
                <span class="files-count">0</span>
            </button>
        `;

        row.innerHTML = `
            <div class="episode-number">${episode.episode_number}</div>
            ${statusIcon}
            <div class="episode-info-section">
                <div class="episode-title">${escapeHtml(episode.name || `Episode ${episode.episode_number}`)}</div>
                <div class="episode-meta">${metaParts.join(' • ')}</div>
            </div>
            ${searchIcon}
            ${refreshIcon}
            ${filesIcon}
        `;

        // Add event listeners
        const searchBtn = row.querySelector('.search-episode-btn');
        if (searchBtn) {
            searchBtn.addEventListener('click', handleSearchEpisode);
        }

        // Add event listener for files toggle button
        if (dbData.files && dbData.files.length > 0) {
            const toggleBtn = row.querySelector('.files-toggle-btn');
            const filesPanel = row.querySelector('.episode-files-panel');

            if (toggleBtn && filesPanel) {
                toggleBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    filesPanel.classList.toggle('open');
                    toggleBtn.classList.toggle('active');
                });

                // Auto-close when mouse leaves the panel
                filesPanel.addEventListener('mouseleave', () => {
                    filesPanel.classList.remove('open');
                    toggleBtn.classList.remove('active');
                });

                // Close button
                const closeBtn = row.querySelector('.files-panel-close');
                if (closeBtn) {
                    closeBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        filesPanel.classList.remove('open');
                        toggleBtn.classList.remove('active');
                    });
                }
            }
        }

        // Note: refresh and delete functionality would need to be implemented similar to library_show.js
        // For now, just adding the buttons for visual parity

    } else {
        // Episode not in database - show minimal info with search button
        let airDateText = '';
        if (episode.air_date) {
            const airDate = new Date(episode.air_date);
            const now = new Date();
            const label = airDate > now ? 'Airing' : 'Aired';
            airDateText = `${label}: ${formatDate(airDate)}`;
        }

        let runtimeText = '';
        if (episode.runtime) {
            runtimeText = `${episode.runtime} min`;
        }

        const metaParts = [airDateText, runtimeText].filter(Boolean).join(' • ');

        // Conditionally add search button for User/Admin only
        const searchButtonHTML = hasUserPermissions ? `
            <div class="discover-episode-actions">
                <button class="search-episode-btn" type="button" title="Search for this episode"
                        data-imdb-id="${showData.imdb_id || ''}"
                        data-tmdb-id="${showData.tmdb_id || ''}"
                        data-season="${seasonNumber}"
                        data-episode="${episode.episode_number}"
                        data-title="${escapeHtml(showData.title || '')}">
                    <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" width="16" height="16">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M10 21h7a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v11m0 5l4.879-4.879m0 0a3 3 0 104.243-4.242 3 3 0 00-4.243 4.242z"></path>
                    </svg>
                </button>
            </div>
        ` : '';

        row.innerHTML = `
            <div class="discover-episode-number">${episode.episode_number}</div>
            <div class="discover-episode-info">
                <div class="discover-episode-title">${escapeHtml(episode.name || `Episode ${episode.episode_number}`)}</div>
                <div class="discover-episode-meta">${metaParts}</div>
            </div>
            ${searchButtonHTML}
        `;

        const searchBtn = row.querySelector('.search-episode-btn');
        if (searchBtn) {
            searchBtn.addEventListener('click', handleSearchEpisode);
        }
    }

    return row;
}

function switchTab(seasonNumber) {
    // Update tab buttons
    document.querySelectorAll('.season-tab').forEach(tab => {
        if (tab.dataset.season === String(seasonNumber)) {
            tab.classList.add('active');
        } else {
            tab.classList.remove('active');
        }
    });

    // Update tab panels
    document.querySelectorAll('.season-panel').forEach(panel => {
        if (panel.dataset.season === String(seasonNumber)) {
            panel.classList.add('active');
        } else {
            panel.classList.remove('active');
        }
    });
}

async function handleSearchEpisode(event) {
    const btn = event.currentTarget;
    const tmdbId = btn.dataset.tmdbId;
    const season = parseInt(btn.dataset.season, 10);
    const episode = parseInt(btn.dataset.episode, 10);
    const title = btn.dataset.title;
    const version = contentData?.default_version || 'Default';

    // Call selectMedia to search for this episode
    await selectMedia(
        tmdbId,
        title,
        contentData?.year || '',
        'tv',
        season,
        episode,
        false, // multi
        contentData?.genres || [],  // genre_ids - pass genre names for auto-select
        version
    );
}

function setupRequestButton(data) {
    const requestBtn = document.getElementById('btn-request');
    if (!requestBtn) return;

    requestBtn.addEventListener('click', function() {
        // Show the version modal for both TV shows and movies
        showVersionModal(data);
    });
}

function showVersionModal(data) {
    const modal = document.getElementById('versionModal');
    const versionCheckboxes = document.getElementById('versionCheckboxes');

    if (!modal || !versionCheckboxes) return;

    // Clear existing content
    versionCheckboxes.innerHTML = '';

    // Add request type selection for TV shows
    if (data.media_type === 'tv') {
        const showSelectionHeader = document.createElement('div');
        showSelectionHeader.className = 'version-section-header';
        showSelectionHeader.innerHTML = '<h4>Select Request Type:</h4>';
        versionCheckboxes.appendChild(showSelectionHeader);

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
        versionCheckboxes.appendChild(seasonSelectionContainer);

        // Populate season checkboxes
        if (data.seasons && data.seasons.length > 0) {
            const regularSeasons = data.seasons.filter(s => s.season_number > 0);
            regularSeasons.forEach(season => {
                const div = document.createElement('div');
                div.className = 'version-checkbox';
                div.innerHTML = `
                    <input type="checkbox" id="season-${season.season_number}" name="seasons" value="${season.season_number}">
                    <label for="season-${season.season_number}">Season ${season.season_number}</label>
                `;
                seasonSelectionContainer.appendChild(div);
            });
        }

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
            }
        });

        // Add separator
        const separator = document.createElement('hr');
        versionCheckboxes.appendChild(separator);
    }

    // Add version selection header
    const versionHeader = document.createElement('div');
    versionHeader.className = 'version-section-header';
    versionHeader.innerHTML = '<h4>Select Versions:</h4>';
    versionCheckboxes.appendChild(versionHeader);

    // Create checkboxes for each version
    discoverVersions.forEach(version => {
        const div = document.createElement('div');
        div.className = 'version-checkbox';
        div.innerHTML = `
            <input type="checkbox" id="version-${version}" name="versions" value="${version}">
            <label for="version-${version}">${version}</label>
        `;
        versionCheckboxes.appendChild(div);

        // If there's only one version available, auto-select it
        if (discoverVersions.length === 1) {
            div.querySelector('input[type="checkbox"]').checked = true;
        }
    });

    // Show the modal
    document.body.classList.add('modal-open');
    modal.style.display = 'flex';
}

function closeVersionModal() {
    const modal = document.getElementById('versionModal');
    if (modal) {
        modal.style.display = 'none';
        document.body.classList.remove('modal-open');
    }
}

async function handleConfirmRequest() {
    const selectedVersions = Array.from(document.querySelectorAll('input[name="versions"]:checked'))
        .map(checkbox => checkbox.value);

    if (selectedVersions.length === 0) {
        window.showPopup({
            type: window.POPUP_TYPES.WARNING,
            title: 'Warning',
            message: 'Please select at least one version'
        });
        return;
    }

    // Check if TV show and specific seasons selected
    const selectionType = document.querySelector('input[name="selection-type"]:checked');
    let seasons = null;

    if (selectionType && selectionType.value === 'specific-seasons') {
        seasons = Array.from(document.querySelectorAll('input[name="seasons"]:checked'))
            .map(checkbox => parseInt(checkbox.value, 10));

        if (seasons.length === 0) {
            window.showPopup({
                type: window.POPUP_TYPES.WARNING,
                title: 'Warning',
                message: 'Please select at least one season or choose "Whole Show"'
            });
            return;
        }
    }

    closeVersionModal();

    // Make the request
    const mediaType = contentData.media_type === 'tv' ? 'tv' : 'movie';

    window.Loading.show();
    try {
        const payload = {
            id: contentData.tmdb_id || contentData.id,
            mediaType: mediaType,
            title: contentData.title,
            versions: selectedVersions
        };

        // Add seasons if specific seasons were selected
        if (seasons && seasons.length > 0) {
            payload.seasons = seasons;
        }

        const response = await fetch('/content/request', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        });

        const result = await response.json();
        if (result.success) {
            window.showPopup({
                type: window.POPUP_TYPES.SUCCESS,
                title: 'Success',
                message: `Successfully requested ${contentData.title}`,
                autoClose: 3000
            });
        } else {
            window.showPopup({
                type: window.POPUP_TYPES.ERROR,
                title: 'Error',
                message: result.error || 'Error requesting content'
            });
        }
    } catch (error) {
        console.error('Error requesting content:', error);
        window.showPopup({
            type: window.POPUP_TYPES.ERROR,
            title: 'Error',
            message: 'Error requesting content'
        });
    } finally {
        window.Loading.hide();
    }
}

async function requestContentDirectly(data) {
    const mediaType = data.media_type === 'tv' ? 'tv' : 'movie';
    const version = data.default_version || 'Default';

    window.Loading.show();
    try {
        const response = await fetch('/content/request', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                id: data.tmdb_id || data.id,
                mediaType: mediaType,
                title: data.title,
                versions: [version]
            })
        });

        const result = await response.json();
        if (result.success) {
            window.showPopup({
                type: window.POPUP_TYPES.SUCCESS,
                title: 'Success',
                message: `Successfully requested ${data.title}`,
                autoClose: 3000
            });
        } else {
            window.showPopup({
                type: window.POPUP_TYPES.ERROR,
                title: 'Error',
                message: result.error || 'Error requesting content'
            });
        }
    } catch (error) {
        console.error('Error requesting content:', error);
        window.showPopup({
            type: window.POPUP_TYPES.ERROR,
            title: 'Error',
            message: 'Error requesting content'
        });
    } finally {
        window.Loading.hide();
    }
}

function alignSidebarWithFiles() {
    // Align the sidebar to start at the same height as the files container
    const movieHeader = document.getElementById('movie-header');
    const sidebar = document.querySelector('.movie-sidebar');

    if (movieHeader && sidebar) {
        const headerHeight = movieHeader.offsetHeight;
        const headerMargin = parseFloat(getComputedStyle(movieHeader).marginBottom);
        const topOffset = headerHeight + headerMargin;

        if (window.innerWidth > 768) {
            sidebar.style.marginTop = `${topOffset}px`;
        } else {
            sidebar.style.marginTop = '0px'; // Fixed for mobile
        }
    }
}

function setupSeasonPacksButton(data) {
    const seasonPacksBtn = document.getElementById('btn-season-packs');
    if (!seasonPacksBtn || data.media_type !== 'tv') return;

    seasonPacksBtn.addEventListener('click', async function() {
        const version = data.default_version || 'Default';

        // Get the currently active season tab
        const activeSeasonTab = document.querySelector('.season-tab.active');
        const seasonNumber = activeSeasonTab ? parseInt(activeSeasonTab.dataset.season) : 1;

        // Call selectMedia with multi=true for season packs
        await selectMedia(
            data.tmdb_id || data.id,
            data.title,
            data.year || '',
            'tv',
            seasonNumber,  // season - use currently active season
            null,  // episode
            true,  // multi - true for season packs
            data.genres || [],  // genre_ids - pass genre names for auto-select
            version
        );
    });
}

function setupSearchMovieButton(data) {
    const searchMovieBtn = document.getElementById('btn-search-movie');
    if (!searchMovieBtn || data.media_type === 'tv') return;

    searchMovieBtn.addEventListener('click', async function() {
        const version = data.default_version || 'Default';

        // Call selectMedia to search for this movie
        await selectMedia(
            data.tmdb_id || data.id,
            data.title,
            data.year || '',
            'movie',
            null,  // season (not used for movies)
            null,  // episode (not used for movies)
            false, // multi = false for single movie
            data.genres || [],  // genre_ids - pass genre names for auto-select
            version
        );
    });
}

function showError(message) {
    document.getElementById('loading-state').style.display = 'none';
    document.getElementById('error-state').style.display = 'block';
    document.getElementById('error-message').textContent = message;
}

function formatDate(date) {
    // Handle both Date objects and date strings
    const dateObj = date instanceof Date ? date : new Date(date);
    const options = { year: 'numeric', month: 'short', day: 'numeric' };
    return dateObj.toLocaleDateString('en-US', options);
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
