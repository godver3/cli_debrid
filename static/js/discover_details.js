/**
 * Discover Details Page JavaScript
 * Handles display of TMDB content details for items not in library
 */

document.addEventListener('DOMContentLoaded', function() {
    initDiscoverDetails();
});

let contentData = null;
let discoverVersions = [];

async function loadDiscoverSettings() {
    if (window.discoverState && window.discoverState.discoverSettings) return;
    try {
        const response = await fetch('/settings/api/config');
        if (!response.ok) return;
        const config = await response.json();
        const s = config['Discover Settings'] || {};
        if (!window.discoverState) window.discoverState = {};
        window.discoverState.discoverSettings = {
            hide_no_rating: s.hide_no_rating || false,
            hide_no_poster: s.hide_no_poster || false,
            only_show_missing: s.only_show_missing || false,
            tv_show_episode_view: s.tv_show_episode_view || 'discover',
            hide_specials: s.hide_specials !== false
        };
    } catch (e) {
        console.warn('[Discover Details] Could not load settings, using defaults:', e);
    }
}

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

    // Load settings before content so season filtering uses saved preferences
    await loadDiscoverSettings();

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

    // Set certification (check both certification and content_rating fields)
    const certificationText = document.getElementById('movie-certification-text');
    const certificationSeparator = document.getElementById('certification-separator');
    if (certificationText && certificationSeparator) {
        const cert = data.certification || data.content_rating;
        if (cert) {
            certificationText.textContent = cert;
            certificationText.style.display = 'inline';
            certificationSeparator.style.display = 'inline';
        } else {
            certificationText.style.display = 'none';
            certificationSeparator.style.display = 'none';
        }
    }

    // Set poster
    const posterImg = document.getElementById('poster-img');
    if (data.poster_path) {
        posterImg.src = `https://image.tmdb.org/t/p/w500${data.poster_path}`;
    }

    // Set title
    const titleAlreadyHasYear = data.year && data.title.trim().endsWith(`(${data.year})`);
    document.getElementById('movie-title').textContent = data.title + (data.year && !titleAlreadyHasYear ? ` (${data.year})` : '');
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

    // Set network (for TV shows in meta section)
    const networkText = document.getElementById('movie-network-text');
    const networkSeparator = document.getElementById('network-separator');
    if (data.media_type === 'tv' && networkText && networkSeparator) {
        const networks = (data.networks || []).join(', ');
        if (networks) {
            networkText.textContent = networks;
            networkText.style.display = 'inline';
            networkSeparator.style.display = 'inline';
        } else {
            networkText.style.display = 'none';
            networkSeparator.style.display = 'none';
        }
    } else if (networkText && networkSeparator) {
        // Hide network for movies
        networkText.style.display = 'none';
        networkSeparator.style.display = 'none';
    }

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

    // Set TVDB link if available (TV shows only)
    if (data.media_type === 'tv' && (data.tvdb_slug || data.title)) {
        const tvdbLink = document.getElementById('link-tvdb');
        // Use real TVDB slug from battery metadata when available, otherwise generate from title
        const slug = data.tvdb_slug || data.title
            .toLowerCase()
            .replace(/[^\w\s-]/g, '') // Remove special characters except spaces and hyphens
            .replace(/\s+/g, '-')      // Replace spaces with hyphens
            .replace(/-+/g, '-')       // Replace multiple hyphens with single hyphen
            .trim();
        tvdbLink.href = `https://thetvdb.com/series/${slug}`;
        tvdbLink.style.display = 'inline-flex';

        const tvdbRow = document.getElementById('tvdb-row');
        tvdbRow.style.display = 'flex';
        const tvdbLinkDetail = document.getElementById('tvdb-link-detail');
        tvdbLinkDetail.href = `https://thetvdb.com/series/${slug}`;
        tvdbLinkDetail.textContent = data.tvdb_id || 'View on TVDB';
    }

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

    // Initialize trailer button
    if (data.tmdb_id && typeof initializeTrailerButton === 'function') {
        const mediaType = data.media_type === 'tv' ? 'show' : 'movie';
        initializeTrailerButton(data.tmdb_id, mediaType);
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
        <div class="cast-card" title="${person.name}${person.character ? ` · ${person.character}` : ''}">
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

    // Filter out specials (season 0) based on setting — default true matches previous hardcoded behaviour
    const hideSpecials = (window.discoverState && window.discoverState.discoverSettings)
        ? window.discoverState.discoverSettings.hide_specials !== false
        : true;
    const regularSeasons = data.seasons
        .filter(s => hideSpecials ? s.season_number > 0 : true)
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

    // Switch to season specified in URL ?season=N&episode=N params
    const urlParams = new URLSearchParams(window.location.search);
    const seasonParam = urlParams.get('season');
    const episodeParam = urlParams.get('episode');
    if (seasonParam) {
        const seasonNumber = parseInt(seasonParam);
        setTimeout(() => {
            switchTab(seasonNumber);
        }, 150);
        if (episodeParam) {
            const epNumber = parseInt(episodeParam);
            let attempts = 0;
            const scrollInterval = setInterval(() => {
                const panel = document.querySelector(`.season-panel[data-season="${seasonNumber}"]`);
                const scope = panel || document;
                const epRow = scope.querySelector(`.episode-row[data-episode="${epNumber}"], .discover-episode-row[data-episode="${epNumber}"]`);
                if (epRow) {
                    clearInterval(scrollInterval);
                    setTimeout(() => {
                        epRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        epRow.classList.add('cal-episode-highlight');
                        setTimeout(() => epRow.classList.remove('cal-episode-highlight'), 6000);
                    }, 100);
                } else if (++attempts > 40) {
                    clearInterval(scrollInterval);
                }
            }, 250);
        }
    }
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

const MAGNET_SVG = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="transform:rotate(270deg)"><path d="M21 18.5V20.5C21 21.3284 20.3284 22 19.5 22H17H13C7.47715 22 3 17.5228 3 12C3 6.47715 7.47715 2 13 2H17H19.5C20.3284 2 21 2.67157 21 3.5V5.5C21 6.32843 20.3284 7 19.5 7H17H13C10.2386 7 8 9.23858 8 12C8 14.7614 10.2386 17 13 17H17H19.5C20.3284 17 21 17.6716 21 18.5Z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path opacity="0.5" d="M17 2V7M17 17V22" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

function createSeasonPanel(season, isActive, showData) {
    const panel = document.createElement('div');
    panel.className = `season-panel ${isActive ? 'active' : ''}`;
    panel.dataset.season = season.season_number;

    // Add season header with magnet assign button
    const seasonHeader = document.createElement('div');
    seasonHeader.className = 'discover-season-header';
    const magnetBtn = document.createElement('button');
    magnetBtn.className = 'search-episode-btn magnet-assign-episode-btn';
    magnetBtn.type = 'button';
    magnetBtn.title = `Assign magnet for Season ${season.season_number}`;
    magnetBtn.innerHTML = MAGNET_SVG;
    magnetBtn.addEventListener('click', () => handleMagnetAssignSeason(showData, season.season_number));
    seasonHeader.innerHTML = `<h3>Season ${season.season_number}</h3>`;
    seasonHeader.appendChild(magnetBtn);
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

    // Count collected episodes for season tab update (includes Upgrading state)
    const collectedCount = episodes.filter(ep =>
        ep.db_data && (ep.db_data.state === 'Collected' || ep.db_data.state === 'Upgrading')
    ).length;
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
    row.dataset.episode = episode.episode_number;

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

        const magnetIconHtml = `<button class="search-episode-btn magnet-assign-episode-btn" type="button" title="Assign magnet for this episode">${MAGNET_SVG}</button>`;

        row.innerHTML = `
            <div class="episode-number">${episode.episode_number}</div>
            ${statusIcon}
            <div class="episode-info-section">
                <div class="episode-title">${escapeHtml(episode.name || `Episode ${episode.episode_number}`)}</div>
                <div class="episode-meta">${metaParts.join(' • ')}</div>
            </div>
            ${searchIcon}
            ${refreshIcon}
            ${magnetIconHtml}
            ${filesIcon}
        `;

        row.querySelector('.magnet-assign-episode-btn')?.addEventListener('click', () => handleMagnetAssignEpisode(showData, seasonNumber, episode.episode_number));

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

        // Add event listener for refresh button (move to wanted)
        const refreshBtn = row.querySelector('.refresh-btn');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', handleRefreshClick);
        }

        // Note: delete functionality would need to be implemented similar to library_show.js
        // For now, just adding the button for visual parity

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
        const searchBtnInner = hasUserPermissions ? `
                <button class="search-episode-btn" type="button" title="Search for this episode"
                        data-imdb-id="${showData.imdb_id || ''}"
                        data-tmdb-id="${showData.tmdb_id || ''}"
                        data-season="${seasonNumber}"
                        data-episode="${episode.episode_number}"
                        data-title="${escapeHtml(showData.title || '')}">
                    <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" width="16" height="16">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M10 21h7a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v11m0 5l4.879-4.879m0 0a3 3 0 104.243-4.242 3 3 0 00-4.243 4.242z"></path>
                    </svg>
                </button>` : '';
        const searchButtonHTML = `
            <div class="discover-episode-actions">
                ${searchBtnInner}
                <button class="search-episode-btn magnet-assign-episode-btn" type="button" title="Assign magnet for this episode">${MAGNET_SVG}</button>
            </div>
        `;

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

        row.querySelector('.magnet-assign-episode-btn')?.addEventListener('click', () => handleMagnetAssignEpisode(showData, seasonNumber, episode.episode_number));
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

    versionCheckboxes.innerHTML = '';

    // Title
    const titleEl = document.createElement('div');
    titleEl.className = 'dialog-title';
    titleEl.textContent = 'Select Versions';
    versionCheckboxes.appendChild(titleEl);

    // Subtitle pill
    const subEl = document.createElement('div');
    subEl.className = 'dialog-sub';
    const isTV = data.media_type === 'tv';
    const year = data.release_date ? data.release_date.split('-')[0] : (data.first_air_date ? data.first_air_date.split('-')[0] : '');
    subEl.innerHTML = `<i class="fa-solid fa-${isTV ? 'tv' : 'film'}"></i> Requesting: ${data.title}${year ? ` (${year})` : ''}`;
    versionCheckboxes.appendChild(subEl);

    // TV show: request type radio rows
    if (isTV) {
        const typeLabel = document.createElement('div');
        typeLabel.className = 'section-label';
        typeLabel.textContent = 'Select Request Type';
        versionCheckboxes.appendChild(typeLabel);

        const wholeRow = document.createElement('div');
        wholeRow.className = 'option-row selected';
        wholeRow.id = 'opt-whole-show';
        wholeRow.innerHTML = `<div class="custom-radio"><div class="custom-radio-dot"></div></div><span class="option-label">Whole Show</span>`;
        versionCheckboxes.appendChild(wholeRow);

        const seasonsRow = document.createElement('div');
        seasonsRow.className = 'option-row';
        seasonsRow.id = 'opt-specific-seasons';
        seasonsRow.innerHTML = `<div class="custom-radio"><div class="custom-radio-dot"></div></div><span class="option-label">Specific Seasons</span>`;
        versionCheckboxes.appendChild(seasonsRow);

        // Season container (hidden initially)
        const seasonSelectionContainer = document.createElement('div');
        seasonSelectionContainer.className = 'season-selection-container';
        seasonSelectionContainer.id = 'season-selection-container';
        seasonSelectionContainer.style.display = 'none';
        versionCheckboxes.appendChild(seasonSelectionContainer);

        if (data.seasons && data.seasons.length > 0) {
            const list = document.createElement('div');
            list.className = 'seasons-list';
            seasonSelectionContainer.appendChild(list);
            const _hideSpecials2 = (window.discoverState && window.discoverState.discoverSettings)
                ? window.discoverState.discoverSettings.hide_specials !== false
                : true;
            data.seasons.filter(s => _hideSpecials2 ? s.season_number > 0 : true).forEach(season => {
                const row = document.createElement('div');
                row.className = 'option-row';
                row.dataset.value = String(season.season_number);
                row.innerHTML = `<div class="custom-cb"><i class="fa-solid fa-check"></i></div><span class="option-label">Season ${season.season_number}</span>`;
                row.addEventListener('click', () => row.classList.toggle('checked'));
                list.appendChild(row);
            });
        }

        wholeRow.addEventListener('click', () => {
            wholeRow.classList.add('selected');
            seasonsRow.classList.remove('selected');
            seasonSelectionContainer.style.display = 'none';
        });

        seasonsRow.addEventListener('click', () => {
            seasonsRow.classList.add('selected');
            wholeRow.classList.remove('selected');
            seasonSelectionContainer.style.display = 'block';
        });

        const divider = document.createElement('div');
        divider.className = 'vm-divider';
        versionCheckboxes.appendChild(divider);
    }

    // Version section label
    const verLabel = document.createElement('div');
    verLabel.className = 'section-label';
    verLabel.textContent = 'Select Versions';
    versionCheckboxes.appendChild(verLabel);

    discoverVersions.forEach(version => {
        const row = document.createElement('div');
        row.className = 'option-row';
        row.dataset.value = version;
        row.dataset.type = 'version';
        row.innerHTML = `<div class="custom-cb"><i class="fa-solid fa-check"></i></div><span class="option-label">${version}</span>`;
        row.addEventListener('click', () => row.classList.toggle('checked'));
        versionCheckboxes.appendChild(row);

        if (discoverVersions.length === 1) row.classList.add('checked');
    });

    // Folder dropdown — symlink mode only
    const folderContainer = document.createElement('div');
    folderContainer.id = 'request-folder-container';
    versionCheckboxes.appendChild(folderContainer);
    (async () => {
        try {
            const fRes = await fetch('/scraper/get_symlink_folders');
            const fData = await fRes.json();
            if (!fData.enabled || !fData.folders || !fData.folders.length) return;
            const fs = fData.folder_settings || {};
            const genreList = (data.genre_ids || data.genres || []).map(g => String(g).trim().toLowerCase());
            const isAnime = genreList.some(g => g.includes('anime') || g.includes('animation') || g === '16');
            const isDoc = genreList.some(g => g.includes('documentary') || g === '99');
            const mediaType = data.media_type === 'movie' ? 'movie' : 'tv';
            let autoFolder = mediaType === 'movie'
                ? ((isAnime && fs.enable_separate_anime_folders) ? fs.anime_movies_folder_name : (isDoc && fs.enable_separate_documentary_folders) ? fs.documentary_movies_folder_name : fs.movies_folder_name)
                : ((isAnime && fs.enable_separate_anime_folders) ? fs.anime_tv_shows_folder_name : (isDoc && fs.enable_separate_documentary_folders) ? fs.documentary_tv_shows_folder_name : fs.tv_shows_folder_name);
            const filtered = fData.folders.filter(f => {
                if (f.is_custom) return true;
                const n = f.name.toLowerCase();
                return mediaType === 'movie' ? (n.includes('movie') || n === (fs.movies_folder_name||'').toLowerCase()) : (n.includes('show') || n.includes('tv') || n === (fs.tv_shows_folder_name||'').toLowerCase());
            });
            if (!filtered.length) return;
            const divEl = document.createElement('div'); divEl.className = 'vm-divider'; folderContainer.appendChild(divEl);
            const lbl = document.createElement('div'); lbl.className = 'section-label'; lbl.textContent = 'Folder'; folderContainer.appendChild(lbl);
            const sel = document.createElement('select'); sel.id = 'request-folder-select';
            sel.style.cssText = 'width:100%;padding:8px 10px;background:#1a1a1a;color:#fff;border:1px solid #333;border-radius:6px;font-size:12px;margin-top:4px;';
            filtered.forEach(f => { const o = document.createElement('option'); o.value = f.name; o.dataset.isCustom = f.is_custom ? 'true' : 'false'; o.textContent = f.is_custom ? `${f.name} (${mediaType === 'movie' ? fs.movies_folder_name : fs.tv_shows_folder_name})` : f.name; if (f.name === autoFolder) o.selected = true; sel.appendChild(o); });
            folderContainer.appendChild(sel);
        } catch(e) {}
    })();

    // Tags multi-select — Plex mode only
    const tagsContainer = document.createElement('div');
    tagsContainer.id = 'request-tags-container';
    versionCheckboxes.appendChild(tagsContainer);
    (async () => {
        try {
            const cfgR = await fetch('/settings/api/config');
            const cfgD = await cfgR.json();
            const globalTags = (cfgD['Tags'] || {})['tags_list'] || [];
            const fileMode = (cfgD['File Management'] || {})['file_collection_management'] || '';
            if (fileMode !== 'Plex' || !globalTags.length) return;
            const div2 = document.createElement('div'); div2.className = 'vm-divider'; tagsContainer.appendChild(div2);
            const lbl2 = document.createElement('div'); lbl2.className = 'section-label'; lbl2.textContent = 'Tags'; tagsContainer.appendChild(lbl2);
            const pillWrap2 = document.createElement('div');
            pillWrap2.id = 'request-tags-pills';
            pillWrap2.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;';
            globalTags.forEach(tag => {
                const pill = document.createElement('div');
                pill.className = 'option-row';
                pill.dataset.value = tag;
                pill.dataset.type = 'tag';
                pill.style.cssText = 'padding:5px 14px;border-radius:14px;cursor:pointer;font-size:12px;flex:none;';
                pill.innerHTML = `<span class="option-label">${tag}</span>`;
                pill.addEventListener('click', () => pill.classList.toggle('checked'));
                pillWrap2.appendChild(pill);
            });
            tagsContainer.appendChild(pillWrap2);
        } catch(e) {}
    })();

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
    const selectedVersions = Array.from(document.querySelectorAll('#versionCheckboxes .option-row.checked[data-type="version"]'))
        .map(row => row.dataset.value);

    if (selectedVersions.length === 0) {
        window.showPopup({
            type: window.POPUP_TYPES.WARNING,
            title: 'Warning',
            message: 'Please select at least one version'
        });
        return;
    }

    // Check if TV show and specific seasons selected
    const wholeShowRow = document.getElementById('opt-whole-show');
    const wholeShowSelected = wholeShowRow ? wholeShowRow.classList.contains('selected') : true;
    let seasons = null;

    if (!wholeShowSelected) {
        seasons = Array.from(document.querySelectorAll('#season-selection-container .option-row.checked'))
            .map(row => parseInt(row.dataset.value, 10));

        if (seasons.length === 0) {
            window.showPopup({
                type: window.POPUP_TYPES.WARNING,
                title: 'Warning',
                message: 'Please select at least one season or choose "Whole Show"'
            });
            return;
        }
    }

    const folderSelect = document.getElementById('request-folder-select');
    const selectedFolder = folderSelect ? folderSelect.value : null;
    const selectedFolderIsCustom = folderSelect ? (folderSelect.options[folderSelect.selectedIndex]?.dataset?.isCustom === 'true') : false;

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

        if (selectedFolder) {
            payload.selected_folder = selectedFolder;
            payload.selected_folder_is_custom = selectedFolderIsCustom;
        }
        const tagPills = document.querySelectorAll('#request-tags-container .option-row.checked[data-type="tag"]');
const selTags = Array.from(tagPills).map(p => p.dataset.value).filter(Boolean).join(',');
if (selTags) payload.selected_tags = selTags;

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

function handleMagnetAssignSeason(showData, seasonNumber) {
    const params = new URLSearchParams({
        prefill_title: showData.title || '',
        prefill_year: showData.year || contentData?.year || '',
        prefill_type: 'show',
        prefill_selection: 'seasons',
        prefill_seasons: String(seasonNumber),
    });
    if (showData.imdb_id) params.set('prefill_id', showData.imdb_id);
    else if (showData.tmdb_id) params.set('prefill_id', String(showData.tmdb_id));
    window.location.href = `/magnet/assign_magnet?${params.toString()}`;
}

function handleMagnetAssignEpisode(showData, seasonNumber, episodeNumber) {
    const params = new URLSearchParams({
        prefill_title: showData.title || '',
        prefill_year: showData.year || contentData?.year || '',
        prefill_type: 'show',
        prefill_selection: 'episode',
        prefill_seasons: String(seasonNumber),
        prefill_episode: String(episodeNumber),
    });
    if (showData.imdb_id) params.set('prefill_id', showData.imdb_id);
    else if (showData.tmdb_id) params.set('prefill_id', String(showData.tmdb_id));
    window.location.href = `/magnet/assign_magnet?${params.toString()}`;
}

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
        showPopup({ type: 'error', title: 'Error', message: `Error moving item to Wanted state: ${error.message}`, autoClose: 4000 });
        btn.disabled = false;
    });
}
