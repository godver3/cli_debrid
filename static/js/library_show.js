/**
 * Library Show Detail - TV Show episode browser
 * Displays show metadata, seasons, and episodes with details
 * Auto-ghostlist scope fix v2.0 - 2026-01-13
 */

// Deletion timing constants
const DELETION_PROGRESS_INTERVAL_MS = 800;      // How often to update progress steps
const DELETION_TIMEOUT_WARNING_MS = 10000;      // When to show "taking longer than expected" message
const API_COOLDOWN_MAX_SECONDS = 96;            // Maximum API cooldown period (Trakt rate limit)

// State management
let showData = null;
let seasonsData = [];
let brokenFiles = new Set();  // Set of broken filenames from __unplayable__ folder

// DOM elements
let showMainGrid, seasonsContainer, emptyState;
let pageBackdrop, pageBackdropImg;

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    console.log('[Show Detail] Page loaded, initializing...');

    try {
        initializeElements();
        attachEventListeners();
        loadShowData();
    } catch (error) {
        console.error('[Show Detail] Initialization error:', error);
    }
});

function initializeElements() {
    showMainGrid = document.getElementById('show-main-grid');
    seasonsContainer = document.getElementById('seasons-container');
    emptyState = document.getElementById('empty-state');
    pageBackdrop = document.getElementById('page-backdrop');
    pageBackdropImg = document.getElementById('page-backdrop-img');
}

function attachEventListeners() {
    // Action buttons
    const btnGetMissing = document.getElementById('btn-get-missing');
    const btnSeasonPacks = document.getElementById('btn-season-packs');
    const btnRefreshTMDB = document.getElementById('btn-refresh-tmdb');
    const btnSettings = document.getElementById('btn-settings');

    if (btnGetMissing) {
        btnGetMissing.addEventListener('click', handleGetMissing);
    }

    if (btnSeasonPacks) {
        btnSeasonPacks.addEventListener('click', handleSeasonPacks);
    }

    if (btnRefreshTMDB) {
        btnRefreshTMDB.addEventListener('click', handleRefreshTMDB);
    }

    if (btnSettings) {
        btnSettings.addEventListener('click', handleSettings);
    }

    // Close overlay when pressing Escape key
    const overlay = document.getElementById('overlay');
    window.addEventListener('keydown', function(event) {
        if (event.key === 'Escape') {
            if (overlay && overlay.style.display === 'flex') {
                closeOverlay();
            }
        }
    });

    // Initialize Loading object
    Loading.init();
    Loading.setOnClose(() => Loading.hide());
}

async function loadShowData() {
    const container = document.querySelector('.show-container');
    const mediaId = container.dataset.mediaId;

    if (!mediaId) {
        showError('No media ID provided');
        return;
    }

    try {

        // Fetch show data and broken files in parallel
        const [showResponse, brokenResponse] = await Promise.all([
            fetch(`/library/show/${mediaId}/data`),
            fetch('/library/check_broken_files', { method: 'POST' })
        ]);

        const data = await showResponse.json();
        const brokenData = await brokenResponse.json();

        // Store broken files in Set for fast lookup
        if (brokenData.success && brokenData.broken_files) {
            brokenFiles = new Set(brokenData.broken_files);
            console.log(`[Show Detail] Loaded ${brokenFiles.size} broken files`);
        }

        if (data.success) {
            showData = data.show;
            seasonsData = data.seasons;

            // Add phantom rows for missing episodes before stats calculation
            addPhantomRowsToSeasonData(seasonsData);

            renderShowHeader(showData);
            renderSeasons(seasonsData);

            // Check for season parameter in URL and switch to that season
            const urlParams = new URLSearchParams(window.location.search);
            const seasonParam = urlParams.get('season');
            if (seasonParam) {
                const seasonNumber = parseInt(seasonParam);
                // Use setTimeout to ensure tabs are fully rendered before switching
                setTimeout(() => {
                    switchTab(seasonNumber);
                }, 100);
            }

            // Initialize deletion handlers after data is loaded
            initializeDeletionHandlers();

            if (seasonsData.length > 0) {
                showMainGrid.style.display = 'grid';

                // Align sidebar with seasons container
                alignSidebarWithSeasons();

                // Load cast from TMDB if we have a tmdb_id
                if (showData.tmdb_id) {
                    loadCast(showData.tmdb_id);
                }
            } else {
                emptyState.style.display = 'flex';
            }
        } else {
            showError(data.error || 'Failed to load show details');
        }
    } catch (error) {
        console.error('[Show Detail] Error loading data:', error);
        showError('Failed to load show details. Please try again.');
    }
}

/**
 * Add phantom rows for missing episodes to seasonsData before stats calculation
 * This ensures the "Get Missing" count includes gaps in episode numbering
 */
function addPhantomRowsToSeasonData(seasons) {
    seasons.forEach(season => {
        // Only process if season has episodes
        if (!season.episodes || season.episodes.length === 0) {
            return;
        }

        // Group episodes by episode_number
        const episodeGroups = {};
        season.episodes.forEach(ep => {
            const epNum = ep.episode_number;
            if (!episodeGroups[epNum]) {
                episodeGroups[epNum] = [];
            }
            episodeGroups[epNum].push(ep);
        });

        // Find gaps from episode 1 to max episode
        const episodeNumbers = Object.keys(episodeGroups).map(n => parseInt(n));
        if (episodeNumbers.length > 0) {
            const maxEp = Math.max(...episodeNumbers);

            // Add phantom episodes for gaps
            for (let i = 1; i <= maxEp; i++) {
                if (!episodeGroups[i]) {
                    season.episodes.push({
                        episode_number: i,
                        episode_title: `Episode ${i}`,
                        state: 'Missing',
                        filled_by_file: null,
                        is_phantom: true,
                        imdb_id: null,
                        tmdb_id: null,
                        size: null,
                        version: null
                    });
                }
            }
        }
    });
}

function renderShowHeader(show) {
    console.log('[Show Detail] Rendering header with data:', show);
    console.log('[Show Detail] Metadata values - overview:', show.overview, 'genres:', show.genres, 'network:', show.network, 'status:', show.status);

    // Set title with year in parentheses
    const titleText = show.title + (show.year ? ` (${show.year})` : '');
    const titleEl = document.getElementById('show-title');
    if (titleEl) {
        titleEl.textContent = titleText;
    } else {
        console.error('[Show Detail] show-title element not found');
    }

    // Set poster
    if (show.poster_url) {
        let posterUrl = show.poster_url;

        // Handle different poster URL formats
        if (posterUrl.startsWith('plex:')) {
            const plexPath = posterUrl.substring(5);
            posterUrl = `/library/plex_image${plexPath}`;
        } else if (posterUrl.startsWith('/') && !posterUrl.startsWith('/static') && !posterUrl.startsWith('/library')) {
            // TMDB poster path
            posterUrl = `/scraper/tmdb_image/w500${posterUrl}`;
        }

        document.getElementById('poster-img').src = posterUrl;
    }

    // Set backdrop for full page
    if (show.backdrop_url) {
        let backdropUrl = show.backdrop_url;

        // Handle different backdrop URL formats
        if (backdropUrl.startsWith('plex:')) {
            const plexPath = backdropUrl.substring(5);
            backdropUrl = `/library/plex_image${plexPath}`;
        } else if (backdropUrl.startsWith('/') && !backdropUrl.startsWith('/static') && !backdropUrl.startsWith('/library')) {
            // TMDB backdrop path
            backdropUrl = `/scraper/tmdb_image/w1280${backdropUrl}`;
        }

        pageBackdropImg.src = backdropUrl;
        pageBackdropImg.alt = `${show.title} Backdrop`;
        pageBackdrop.style.display = 'block';
    }

    // Set status badge and inline metadata
    const statusBadge = document.getElementById('show-status-badge');
    if (statusBadge && show.status) {
        statusBadge.textContent = show.status;

        // Add color-coded class based on status
        const statusLower = show.status.toLowerCase().replace(/\s+/g, '-');
        statusBadge.className = `status-badge status-${statusLower}`;
    }

    // Set rating with star icon
    const ratingText = document.getElementById('show-rating-text');
    const ratingSeparator = document.getElementById('show-rating-separator');
    if (ratingText) {
        if (show.rating) {
            // Display rating with 1 decimal point and star icon
            const ratingValue = parseFloat(show.rating).toFixed(1);
            ratingText.innerHTML = `
                <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="width: 1em; height: 1em; display: inline-block; vertical-align: bottom; margin-right: 0.25rem;">
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

    const networkText = document.getElementById('show-network-text');
    if (networkText && show.network) {
        networkText.textContent = show.network;
    }

    const genresText = document.getElementById('show-genres-text');
    if (genresText && show.genres) {
        genresText.textContent = show.genres;
    }

    // Calculate episode stats - count unique episode numbers, not files
    let totalEpisodes = 0;
    let collectedEpisodes = 0;
    let missingMovableEpisodes = 0; // Episodes in Blacklisted/Error/Ghostlisted states (not Unreleased)

    seasonsData.forEach(season => {
        // Group episodes by episode_number to count unique episodes
        const episodeGroups = {};
        season.episodes.forEach(ep => {
            const epNum = ep.episode_number;
            if (!episodeGroups[epNum]) {
                episodeGroups[epNum] = [];
            }
            episodeGroups[epNum].push(ep);
        });

        // Count unique episodes
        totalEpisodes += Object.keys(episodeGroups).length;

        // Count collected episodes and missing movable episodes
        Object.values(episodeGroups).forEach(episodes => {
            const hasCollected = episodes.some(ep => ep.state === 'Collected' || ep.state === 'Upgrading');
            if (hasCollected) {
                collectedEpisodes++;
            } else {
                // Check if any episode is in movable state (Blacklisted/Error/Ghostlisted, not Unreleased)
                // Note: Phantom rows are excluded because they don't exist in DB and can't be moved to Wanted
                const hasMovableState = episodes.some(ep => {
                    // Skip phantom rows (client-side only, not in database)
                    if (ep.is_phantom === true) {
                        return false;
                    }
                    // Include episodes in movable states
                    return ['Blacklisted', 'Error', 'Ghostlisted'].includes(ep.state);
                });
                if (hasMovableState) {
                    missingMovableEpisodes++;
                }
            }
        });
    });

    const progressPercent = totalEpisodes > 0 ? Math.round((collectedEpisodes / totalEpisodes) * 100) : 0;

    // Update missing badge (show count of movable episodes, not all missing)
    const missingBadge = document.getElementById('missing-badge');
    const btnGetMissing = document.getElementById('btn-get-missing');
    if (missingBadge) {
        missingBadge.textContent = missingMovableEpisodes;
    }
    // Disable Get Missing button if there are no movable episodes
    if (btnGetMissing) {
        btnGetMissing.disabled = missingMovableEpisodes === 0;
    }

    // Update progress section
    const progressText = document.getElementById('progress-text');
    if (progressText) {
        progressText.textContent = `${collectedEpisodes} / ${totalEpisodes} Episodes`;
    }

    const progressPercText = document.getElementById('progress-percent');
    if (progressPercText) {
        progressPercText.textContent = `(${progressPercent}% complete)`;
    }

    const progressFill = document.getElementById('progress-fill');
    if (progressFill) {
        progressFill.style.width = `${progressPercent}%`;
    }

    // Update details row
    const qualityValue = document.getElementById('quality-value');
    if (qualityValue && show.version) {
        qualityValue.textContent = show.version;
    }

    const pathValue = document.getElementById('path-value');
    if (pathValue && show.path) {
        pathValue.textContent = show.path;
    }

    const addedValue = document.getElementById('added-value');
    if (addedValue && show.added_date) {
        addedValue.textContent = formatDate(show.added_date);
    }

    const sizeValue = document.getElementById('size-value');
    if (sizeValue && show.total_size !== null && show.total_size !== undefined) {
        sizeValue.textContent = `${show.total_size.toFixed(2)} GB`;
    } else if (sizeValue) {
        sizeValue.textContent = '-';
    }

    // Update external links in header
    const tmdbLink = document.getElementById('link-tmdb');
    if (tmdbLink && show.tmdb_id) {
        tmdbLink.href = `https://www.themoviedb.org/tv/${show.tmdb_id}`;
    }

    const imdbLink = document.getElementById('link-imdb');
    if (imdbLink && show.imdb_id) {
        imdbLink.href = `https://www.imdb.com/title/${show.imdb_id}`;
    }

    const traktLink = document.getElementById('link-trakt');
    if (traktLink) {
        // Prefer IMDb ID, fall back to TMDB ID
        if (show.imdb_id) {
            traktLink.href = `https://trakt.tv/shows/${show.imdb_id}`;
        } else if (show.tmdb_id) {
            traktLink.href = `https://trakt.tv/shows/${show.tmdb_id}`;
        }
    }

    // Initialize trailer button
    if (show.tmdb_id && typeof initializeTrailerButton === 'function') {
        initializeTrailerButton(show.tmdb_id, 'show');
    }

    // Set overview in sidebar
    const overviewEl = document.getElementById('show-overview');
    if (overviewEl && show.overview) {
        overviewEl.textContent = show.overview;
    }

    // Set details in sidebar
    const networkEl = document.getElementById('show-network');
    if (networkEl && show.network) {
        networkEl.textContent = show.network;
    }

    const statusEl = document.getElementById('show-status');
    if (statusEl && show.status) {
        statusEl.textContent = show.status;
    }

    const ratingEl = document.getElementById('show-rating');
    if (ratingEl) {
        if (show.rating) {
            const ratingValue = parseFloat(show.rating).toFixed(1);
            const voteCount = show.vote_count ? ` (${show.vote_count.toLocaleString()} votes)` : '';
            ratingEl.textContent = `${ratingValue}/10${voteCount}`;
        } else {
            ratingEl.textContent = '-';
        }
    }

    const genresEl = document.getElementById('show-genres');
    if (genresEl && show.genres) {
        genresEl.textContent = show.genres;
    }

    // Set external links in sidebar
    const tmdbLinkDetail = document.getElementById('tmdb-link-detail');
    if (tmdbLinkDetail && show.tmdb_id) {
        tmdbLinkDetail.href = `https://www.themoviedb.org/tv/${show.tmdb_id}`;
        tmdbLinkDetail.textContent = show.tmdb_id;
    }

    const imdbLinkDetail = document.getElementById('imdb-link-detail');
    if (imdbLinkDetail && show.imdb_id) {
        imdbLinkDetail.href = `https://www.imdb.com/title/${show.imdb_id}`;
        imdbLinkDetail.textContent = show.imdb_id;
    }

    // Set storage info in sidebar
    const sourcesEl = document.getElementById('show-sources');
    if (sourcesEl && show.content_sources && show.content_sources.length > 0) {
        sourcesEl.textContent = show.content_sources.join(', ');
    }

    const rcloneEl = document.getElementById('show-rclone');
    if (rcloneEl && show.rclone_path) {
        rcloneEl.textContent = show.rclone_path;
    }

    const pathEl = document.getElementById('show-path');
    if (pathEl && show.path) {
        pathEl.textContent = show.path;
    }

    const seasonFoldersEl = document.getElementById('show-season-folders');
    if (seasonFoldersEl && show.season_folders !== undefined) {
        seasonFoldersEl.textContent = show.season_folders ? 'Yes' : 'No';
    }

    // Update delete show button icon based on auto-ghostlist setting
    const deleteShowBtn = document.getElementById('delete-show-btn');
    if (deleteShowBtn) {
        // Check if auto-ghostlist setting is enabled
        const useGhostIcon = show.auto_ghostlist_enabled === true;

        // Update button icon and title
        const ghostIcon = `
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 640 640" fill="currentColor">
                <path d="M168.1 531.1L156.9 540.1C153.7 542.6 149.8 544 145.8 544C136 544 128 536 128 526.2L128 256C128 150 214 64 320 64C426 64 512 150 512 256L512 526.2C512 536 504 544 494.2 544C490.2 544 486.3 542.6 483.1 540.1L471.9 531.1C458.5 520.4 439.1 522.1 427.8 535L397.3 570C394 573.8 389.1 576 384 576C378.9 576 374.1 573.8 370.7 570L344.1 539.5C331.4 524.9 308.7 524.9 295.9 539.5L269.3 570C266 573.8 261.1 576 256 576C250.9 576 246.1 573.8 242.7 570L212.2 535C200.9 522.1 181.5 520.4 168.1 531.1zM288 256C288 238.3 273.7 224 256 224C238.3 224 224 238.3 224 256C224 273.7 238.3 288 256 288C273.7 288 288 273.7 288 256zM384 288C401.7 288 416 273.7 416 256C416 238.3 401.7 224 384 224C366.3 224 352 238.3 352 256C352 273.7 366.3 288 384 288z"/>
            </svg>
        `;

        const trashIcon = `
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M10 11v6"></path>
                <path d="M14 11v6"></path>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"></path>
                <path d="M3 6h18"></path>
                <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
            </svg>
        `;

        deleteShowBtn.innerHTML = useGhostIcon ? ghostIcon : trashIcon;
        deleteShowBtn.title = useGhostIcon ? 'Ghostlist Show' : 'Delete Show';
        deleteShowBtn.setAttribute('aria-label', useGhostIcon ? 'Ghostlist entire show' : 'Delete entire show');
    }

    console.log('[Show Detail] Header rendering complete');
}

function renderSeasons(seasons) {
    const tabsNav = document.getElementById('season-tabs-nav');
    const tabsContent = document.getElementById('season-tabs-content');

    tabsNav.innerHTML = '';
    tabsContent.innerHTML = '';

    // Count unique episodes, not files
    let totalEpisodes = 0;
    let collectedEpisodes = 0;

    seasons.forEach(season => {
        // Group episodes by episode_number to count unique episodes
        const episodeGroups = {};
        season.episodes.forEach(ep => {
            const epNum = ep.episode_number;
            if (!episodeGroups[epNum]) {
                episodeGroups[epNum] = [];
            }
            episodeGroups[epNum].push(ep);
        });

        // Count unique episodes
        totalEpisodes += Object.keys(episodeGroups).length;

        // Count collected episodes (at least one file is collected or upgrading)
        Object.values(episodeGroups).forEach(episodes => {
            if (episodes.some(ep => ep.state === 'Collected' || ep.state === 'Upgrading')) {
                collectedEpisodes++;
            }
        });
    });

    // Update header counts
    const seasonsCountEl = document.getElementById('seasons-count');
    if (seasonsCountEl) {
        seasonsCountEl.textContent = `${seasons.length} Season${seasons.length !== 1 ? 's' : ''} • `;
    }

    // Create tabs for each season
    seasons.forEach((season, index) => {
        // Create tab button
        const tabBtn = createSeasonTab(season, index === 0);
        tabsNav.appendChild(tabBtn);

        // Create tab panel
        const tabPanel = createSeasonPanel(season, index === 0);
        tabsContent.appendChild(tabPanel);

        // Add click listener
        tabBtn.addEventListener('click', () => switchTab(season.season_number));
    });
}

function createSeasonTab(season, isActive) {
    // Group episodes by episode_number to count unique episodes
    const episodeGroups = {};
    season.episodes.forEach(ep => {
        const epNum = ep.episode_number;
        if (!episodeGroups[epNum]) {
            episodeGroups[epNum] = [];
        }
        episodeGroups[epNum].push(ep);
    });

    // Count unique episodes
    const totalEpisodes = Object.keys(episodeGroups).length;

    // Count collected episodes (at least one file is collected or upgrading)
    let collectedEpisodes = 0;
    Object.values(episodeGroups).forEach(episodes => {
        if (episodes.some(ep => ep.state === 'Collected' || ep.state === 'Upgrading')) {
            collectedEpisodes++;
        }
    });

    const progressPercent = totalEpisodes > 0 ? Math.round((collectedEpisodes / totalEpisodes) * 100) : 0;

    const tab = document.createElement('button');
    tab.className = `season-tab ${isActive ? 'active' : ''}`;
    tab.dataset.season = season.season_number;
    tab.setAttribute('type', 'button');

    tab.innerHTML = `
        <div class="season-tab-title">Season ${season.season_number}</div>
        <div class="season-tab-stats">${collectedEpisodes} / ${totalEpisodes}</div>
        <div class="season-tab-progress">
            <div class="season-tab-progress-bar">
                <div class="season-tab-progress-fill" style="width: ${progressPercent}%"></div>
            </div>
        </div>
    `;

    return tab;
}

function createSeasonPanel(season, isActive) {
    const panel = document.createElement('div');
    panel.className = `season-panel ${isActive ? 'active' : ''}`;
    panel.dataset.season = season.season_number;

    // Check permissions for delete button
    const hasAdminPermissions = document.getElementById('has_admin_permissions')?.value === 'True';

    // Season delete button - only for admins
    let deleteButtonHtml = '';
    if (hasAdminPermissions) {
        const deleteIconSvg = `
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M10 11v6"></path>
                <path d="M14 11v6"></path>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"></path>
                <path d="M3 6h18"></path>
                <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
            </svg>
        `;

        deleteButtonHtml = `
            <button class="btn btn-sm btn-danger delete-season-btn"
                    data-season-number="${season.season_number}"
                    data-imdb-id="${showData.imdb_id}"
                    title="Delete entire season">
                ${deleteIconSvg}
                Delete Season
            </button>
        `;
    }

    // Add season header with optional delete button
    const seasonHeader = document.createElement('div');
    seasonHeader.className = 'season-panel-header';
    seasonHeader.innerHTML = `
        <h3>Season ${season.season_number}</h3>
        ${deleteButtonHtml}
    `;
    panel.appendChild(seasonHeader);

    // Attach delete handler if button exists
    if (hasAdminPermissions) {
        const deleteBtn = seasonHeader.querySelector('.delete-season-btn');
        if (deleteBtn) {
            deleteBtn.addEventListener('click', handleDeleteSeason);
        }
    }

    // Group episodes by episode_number (handle multiple files per episode)
    const episodeGroups = {};
    season.episodes.forEach(ep => {
        const epNum = ep.episode_number;
        if (!episodeGroups[epNum]) {
            episodeGroups[epNum] = [];
        }
        episodeGroups[epNum].push(ep);
    });

    // Detect gaps in episode numbering and create phantom rows
    const episodeNumbers = Object.keys(episodeGroups).map(n => parseInt(n)).sort((a, b) => a - b);
    if (episodeNumbers.length > 0) {
        const maxEp = Math.max(...episodeNumbers);

        // Check for gaps from episode 1 to max episode
        // This catches missing episodes at the start and in the middle
        for (let i = 1; i <= maxEp; i++) {
            if (!episodeGroups[i]) {
                // Create phantom row for missing episode
                episodeGroups[i] = [{
                    episode_number: i,
                    episode_title: `Episode ${i}`,
                    state: 'Missing',
                    filled_by_file: null,
                    is_phantom: true,
                    imdb_id: null,
                    tmdb_id: null,
                    size: null,
                    version: null
                }];
            }
        }
    }

    // Render each episode
    Object.keys(episodeGroups).sort((a, b) => parseInt(a) - parseInt(b)).forEach(epNum => {
        const episodes = episodeGroups[epNum];
        const episodeRow = createEpisodeRow(episodes, season.season_number);
        panel.appendChild(episodeRow);
    });

    return panel;
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

function getQualityScore(episode) {
    // Calculate quality score based on version and file properties
    let score = 0;
    const version = (episode.version || '').toLowerCase();

    // Resolution quality (highest priority)
    if (version.includes('8k')) score += 10000;
    else if (version.includes('4k') || version.includes('2160p')) score += 5000;
    else if (version.includes('1080p') || version.includes('fhd')) score += 3000;
    else if (version.includes('720p') || version.includes('hd')) score += 2000;
    else if (version.includes('480p')) score += 1000;
    else if (version.includes('360p')) score += 500;

    // Source quality
    if (version.includes('remux')) score += 1000;
    else if (version.includes('bluray') || version.includes('blu-ray')) score += 800;
    else if (version.includes('webdl') || version.includes('web-dl')) score += 600;
    else if (version.includes('webrip')) score += 500;
    else if (version.includes('hdtv')) score += 300;
    else if (version.includes('dvd')) score += 200;

    // Codec quality
    if (version.includes('h265') || version.includes('hevc') || version.includes('x265')) score += 150;
    else if (version.includes('h264') || version.includes('avc') || version.includes('x264')) score += 100;

    // HDR variants
    if (version.includes('hdr10+')) score += 80;
    else if (version.includes('hdr10')) score += 70;
    else if (version.includes('hdr')) score += 60;
    else if (version.includes('dolby vision') || version.includes('dv')) score += 90;

    // Audio quality
    if (version.includes('atmos')) score += 50;
    else if (version.includes('truehd') || version.includes('dts-hd')) score += 40;
    else if (version.includes('dts')) score += 30;
    else if (version.includes('ac3') || version.includes('dd')) score += 20;
    else if (version.includes('aac')) score += 10;

    // Count asterisks in version (indicators of quality/preference)
    const asteriskCount = (version.match(/\*/g) || []).length;
    score += asteriskCount * 100;

    return score;
}

function getHighestQualityEpisode(episodes) {
    // Return the episode with the highest quality score
    return episodes.reduce((best, current) => {
        const bestScore = getQualityScore(best);
        const currentScore = getQualityScore(current);
        return currentScore > bestScore ? current : best;
    });
}

function createEpisodeRow(episodes, seasonNumber) {
    // episodes is an array of entries for the same episode number
    // (can have multiple entries if there are multiple files/versions)
    const firstEp = episodes[0];
    const isPhantom = firstEp.is_phantom || false;
    const isCollected = episodes.some(ep => ep.state === 'Collected');
    const isUpgrading = episodes.some(ep => ep.state === 'Upgrading');
    const isBlacklisted = episodes.some(ep => ep.state === 'Blacklisted');
    const isUnreleased = episodes.some(ep => ep.state === 'Unreleased');
    const isWanted = episodes.some(ep => ep.state === 'Wanted');
    const isFinalScrape = episodes.some(ep => ep.state === 'Final_Scrape');
    const isFinalCheck = episodes.some(ep => ep.state === 'Final_Check');
    const isSleeping = episodes.some(ep => ep.state === 'Sleeping');

    const row = document.createElement('div');
    row.className = isPhantom ? 'episode-row phantom-row' : 'episode-row';

    // Status icon
    let statusIcon = '';
    if (isPhantom) {
        statusIcon = `
            <svg class="episode-status-icon phantom-missing" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="stroke-dasharray: 4 4;">
                <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
        `;
    } else if (isCollected) {
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

    // Episode metadata
    let metaParts = [];

    // Show only the highest quality version
    if (episodes.length > 0) {
        const highestQualityEp = getHighestQualityEpisode(episodes);
        if (highestQualityEp.version) {
            metaParts.push(`<span class="episode-version">${escapeHtml(highestQualityEp.version)}</span>`);
        }
        // Add largest size badge if available (in case of multiple files per episode)
        const largestSize = Math.max(...episodes.map(ep => ep.size || 0));
        if (largestSize > 0) {
            metaParts.push(`<span class="episode-version">${largestSize.toFixed(2)} GB</span>`);
        }
    }

    // Air date
    const episodeWithAirdate = episodes.find(ep => ep.release_date || ep.airtime);
    if (episodeWithAirdate) {
        const airdate = episodeWithAirdate.release_date || episodeWithAirdate.airtime;
        if (airdate) {
            // Determine if airing in future by comparing date strings
            const today = new Date().toISOString().split('T')[0];
            const label = airdate > today ? 'Airing' : 'Aired';
            metaParts.push(`<span>${label}: ${formatDate(airdate)}</span>`);
        }
    }

    // Collected date or status
    const collectedEpisode = episodes.find(ep => ep.collected_at);
    if (collectedEpisode && collectedEpisode.collected_at) {
        metaParts.push(`<span>Collected: ${formatDate(collectedEpisode.collected_at)}</span>`);
    } else if (firstEp.state) {
        // Show status for non-collected episodes
        metaParts.push(`<span>Status: ${escapeHtml(firstEp.state)}</span>`);
    }

    // Check if any file is broken (matches location_basename in __unplayable__ folder)
    // Get file names - prefer filled_by_file, fall back to extracting from location_on_disk
    const files = episodes.map(ep => {
        if (ep.filled_by_file) return ep.filled_by_file;
        if (ep.location_on_disk) {
            // Extract filename from full path
            const parts = ep.location_on_disk.split('/');
            return parts[parts.length - 1];
        }
        return null;
    }).filter(Boolean);
    const isBroken = episodes.some(ep => {
        // Use location_basename to match with files/folders in __unplayable__
        return ep.location_basename && brokenFiles.has(ep.location_basename);
    });

    // Broken file icon
    let brokenIcon = '';
    if (isBroken) {
        brokenIcon = `
            <svg class="broken-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 640 640" title="File is broken/unplayable">
                <path fill="currentColor" d="M73 39.1C63.6 29.7 48.4 29.7 39.1 39.1C29.8 48.5 29.7 63.7 39 73.1L567 601.1C576.4 610.5 591.6 610.5 600.9 601.1C610.2 591.7 610.3 576.5 600.9 567.2L478.9 445.2C483.1 441.8 487.2 438.1 491 434.3L562.1 363.2C591.4 333.9 607.9 294.1 607.9 252.6C607.9 166.2 537.9 96.1 451.4 96.1C414.1 96.1 378.3 109.4 350.1 133.3C370.4 143.4 388.8 156.8 404.6 172.8C418.7 164.5 434.8 160.1 451.4 160.1C502.5 160.1 543.9 201.5 543.9 252.6C543.9 277.1 534.2 300.6 516.8 318L445.7 389.1C441.8 393 437.6 396.5 433.1 399.6L385.6 352.1C402.1 351.2 415.3 337.7 415.8 321C415.8 319.7 415.8 318.4 415.8 317.1C415.8 230.8 345.9 160.2 259.3 160.2C240.1 160.2 221.4 163.7 203.8 170.4L73 39.1zM257.9 224C258.5 224 259 224 259.6 224C274.7 224 289.1 227.7 301.7 234.2C303.5 235.4 305.3 236.5 307.2 237.3C334 253.6 352 283.2 352 316.9C352 317.3 352 317.7 352 318.1L257.9 224zM378.2 480L224 325.8C225.2 410.4 293.6 478.7 378.1 479.9zM171.7 273.5L126.4 228.2L77.8 276.8C48.5 306.1 32 345.9 32 387.4C32 473.8 102 543.9 188.5 543.9C225.7 543.9 261.6 530.6 289.8 506.7C269.5 496.6 251 483.2 235.2 467.2C221.2 475.4 205.1 479.8 188.5 479.8C137.4 479.8 96 438.4 96 387.3C96 362.8 105.7 339.3 123.1 321.9L171.7 273.3z"/>
            </svg>
        `;
    }

    // Check permissions for conditional button rendering
    const hasAdminPermissions = document.getElementById('has_admin_permissions')?.value === 'True';
    const hasUserPermissions = document.getElementById('has_user_permissions')?.value === 'True';

    // Search icon - search for this specific episode (User + Admin only, not Requester)
    const searchIcon = hasUserPermissions ? `
        <button class="search-episode-btn" type="button" title="Search for this episode"
                data-imdb-id="${episodes[0].imdb_id || ''}"
                data-tmdb-id="${episodes[0].tmdb_id || ''}"
                data-season="${seasonNumber}"
                data-episode="${firstEp.episode_number || 0}">
            <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" width="16" height="16">
                <path stroke-linecap="round" stroke-linejoin="round" d="M10 21h7a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v11m0 5l4.879-4.879m0 0a3 3 0 104.243-4.242 3 3 0 00-4.243 4.242z"></path>
            </svg>
        </button>
    ` : '';

    // Refresh icon - moves episode back to "wanted" state (All authenticated users)
    const refreshIcon = `
        <button class="refresh-btn" type="button" title="Move back to Wanted state"
                data-imdb-id="${episodes[0].imdb_id || ''}"
                data-tmdb-id="${episodes[0].tmdb_id || ''}"
                data-season="${seasonNumber}"
                data-episode="${firstEp.episode_number || 0}">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 15V3"></path>
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                <path d="m7 10 5 5 5-5"></path>
            </svg>
        </button>
    `;

    // Delete icon - deletes episode (Admin only)
    let deleteIcon = '';
    if (hasAdminPermissions) {
        const episodeIds = episodes.map(ep => ep.id).join(',');
        const episodeFilesData = JSON.stringify(episodes.map(ep => {
            // Get filename - prefer filled_by_file, fall back to extracting from location_on_disk
            let fileName = ep.filled_by_file;
            if (!fileName && ep.location_on_disk) {
                const parts = ep.location_on_disk.split('/');
                fileName = parts[parts.length - 1];
            }
            return {
                id: ep.id,
                file: fileName || 'Unknown file',
                version: ep.version || 'Unknown'
            };
        }));

        // Episode delete button - always use delete (not ghostlist) even if auto_ghostlist_enabled
        const deleteIconSvg = `
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M10 11v6"></path>
                <path d="M14 11v6"></path>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"></path>
                <path d="M3 6h18"></path>
                <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
            </svg>
        `;

        deleteIcon = `
            <button class="delete-episode-btn" type="button" title="Delete episode"
                    data-imdb-id="${episodes[0].imdb_id || ''}"
                    data-season="${seasonNumber}"
                    data-episode="${firstEp.episode_number || 0}"
                    data-episode-ids="${episodeIds}"
                    data-episode-files='${episodeFilesData.replace(/'/g, "&#39;")}'>
                ${deleteIconSvg}
            </button>
        `;
    }

    // Files - show all files in popup
    // Show action icons for collected episodes OR blacklisted episodes OR unknown state OR final_scrape state OR final_check state OR sleeping state
    let filesHtml = '';
    if (isPhantom) {
        // Phantom rows: show search button only (no delete or move-to-wanted buttons)
        // Since phantom rows don't exist in database, they can't be moved to wanted
        const searchIcon = hasUserPermissions ? `
            <button class="search-episode-btn" type="button" title="Search for this episode"
                    data-imdb-id=""
                    data-tmdb-id=""
                    data-season="${seasonNumber}"
                    data-episode="${firstEp.episode_number}">
                <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" width="16" height="16">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M10 21h7a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v11m0 5l4.879-4.879m0 0a3 3 0 104.243-4.242 3 3 0 00-4.243 4.242z"></path>
                </svg>
            </button>
        ` : '';

        filesHtml = `${searchIcon}`;
    } else if (files.length > 0 || isBlacklisted || isCollected || isUpgrading || isFinalScrape || isFinalCheck || isSleeping || isUnreleased || isWanted || firstEp.state === 'Unknown') {
        const filesButtonHtml = files.length > 0 ? `
            <button class="files-toggle-btn" type="button" title="Toggle file list">
                <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path>
                </svg>
                <span class="files-count">${files.length}</span>
            </button>
            <div class="episode-files-panel">
                <button class="files-panel-close" type="button" aria-label="Close">
                    <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"></path>
                    </svg>
                </button>
                <div class="episode-files">
                    ${files.map(file => `<div class="episode-file" title="${escapeHtml(file)}">${escapeHtml(file)}</div>`).join('')}
                </div>
            </div>
        ` : `
            <button class="files-toggle-btn" type="button" title="No files" disabled>
                <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path>
                </svg>
                <span class="files-count">0</span>
            </button>
        `;

        filesHtml = `
            ${brokenIcon}
            ${searchIcon}
            ${refreshIcon}
            ${deleteIcon}
            ${filesButtonHtml}
        `;
    }

    row.innerHTML = `
        <div class="episode-number">${firstEp.episode_number}</div>
        ${statusIcon}
        <div class="episode-info-section">
            <div class="episode-title">${escapeHtml(firstEp.episode_title || `Episode ${firstEp.episode_number}`)}</div>
            <div class="episode-meta">
                ${metaParts.join(' • ')}
            </div>
        </div>
        ${filesHtml}
    `;

    // Add event listener for search button
    const searchBtn = row.querySelector('.search-episode-btn');
    if (searchBtn) {
        searchBtn.addEventListener('click', handleSearchEpisode);
    }

    // Add event listener for refresh button
    const refreshBtn = row.querySelector('.refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', handleRefreshClick);
    }

    // Add event listener for delete button
    const deleteBtn = row.querySelector('.delete-episode-btn');
    if (deleteBtn) {
        deleteBtn.addEventListener('click', handleDeleteEpisode);
    }

    // Add event listener for files toggle button
    if (files.length > 0) {
        const toggleBtn = row.querySelector('.files-toggle-btn');
        const filesPanel = row.querySelector('.episode-files-panel');

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

        // Close button for mobile
        const closeBtn = row.querySelector('.files-panel-close');
        if (closeBtn) {
            closeBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                filesPanel.classList.remove('open');
                toggleBtn.classList.remove('active');
            });
        }
    }

    return row;
}

function showError(message) {
    emptyState.style.display = 'flex';
    emptyState.querySelector('h3').textContent = 'Error';
    emptyState.querySelector('p').textContent = message;
}

function alignSidebarWithSeasons() {
    // Align the sidebar to start at the same height as the seasons container
    const showHeader = document.getElementById('show-header');
    const sidebar = document.querySelector('.show-sidebar');

    if (showHeader && sidebar) {
        const headerHeight = showHeader.offsetHeight;
        const headerMargin = parseFloat(getComputedStyle(showHeader).marginBottom);
        const topOffset = headerHeight + headerMargin;

        if (window.innerWidth > 768) {
            sidebar.style.marginTop = `${topOffset}px`;
        } else {
            sidebar.style.marginTop = '0px'; // Fixed for mobile
        }
    }
}

// Add resize listener for responsive sidebar margin adjustment
// Handles orientation changes and window resizing without page refresh
let sidebarResizeTimeout;
window.addEventListener('resize', function() {
    clearTimeout(sidebarResizeTimeout);
    sidebarResizeTimeout = setTimeout(alignSidebarWithSeasons, 150);
});

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function handleSearchEpisode(event) {
    if (!showData) return;

    const btn = event.currentTarget;
    const version = showData.version || 'Default';
    const season = parseInt(btn.dataset.season);
    const episode = parseInt(btn.dataset.episode);

    // Call selectMedia to search for this specific episode
    // Convert genres string to array if needed for auto-select
    const genres = showData.genres ?
        (typeof showData.genres === 'string' ? showData.genres.split(',').map(g => g.trim()) : showData.genres)
        : [];

    await selectMedia(
        showData.tmdb_id || showData.imdb_id,
        showData.title,
        showData.year || '',
        'tv',
        season,
        episode,
        false, // multi = false for single episode
        genres, // genre_ids - pass genres for auto-select
        version
    );
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
            // Reload the show data to reflect the updated state
            loadShowData();
        } else {
            throw new Error(data.error || 'Failed to move item to Wanted state');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: `Error moving item to Wanted state: ${error.message}`,
            autoClose: 5000
        });
        btn.disabled = false;
    });
}

function formatDate(dateInput) {
    // Handle both Date objects and date strings
    let dateStr;
    if (typeof dateInput === 'string') {
        // Extract just the date part if it has a timestamp
        dateStr = dateInput.split('T')[0].split(' ')[0];
    } else if (dateInput instanceof Date) {
        dateStr = dateInput.toISOString().split('T')[0];
    } else {
        return '';
    }
    
    // Parse manually to avoid timezone conversion issues
    const parts = dateStr.split('-');
    if (parts.length === 3) {
        const year = parts[0];
        const month = parseInt(parts[1], 10) - 1;
        const day = parseInt(parts[2], 10);
        const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        return `${months[month]} ${day}, ${year}`;
    }
    return dateStr;
}

// ============================================================================
// Action Button Handlers
// ============================================================================

function handleGetMissing() {
    if (!showData) return;

    const btn = document.getElementById('btn-get-missing');
    btn.disabled = true;

    fetch('/library/move_missing_to_wanted', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            imdb_id: showData.imdb_id,
            tmdb_id: showData.tmdb_id
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showPopup({
                type: POPUP_TYPES.SUCCESS,
                message: data.message || `Moved ${data.updated_count} episode(s) to Wanted state`,
                autoClose: 3000
            });
            // Reload show data to reflect changes
            loadShowData();
        } else {
            throw new Error(data.error || 'Failed to move episodes to Wanted state');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: `Error moving episodes to Wanted state: ${error.message}`,
            autoClose: 5000
        });
        btn.disabled = false;
    });
}

async function handleSeasonPacks() {
    if (!showData) return;

    // Use the version from showData metadata, or 'Default' if not available
    const version = showData.version || 'Default';

    // Get the currently active season tab
    const activeTab = document.querySelector('.season-tab.active');
    const activeSeason = activeTab ? parseInt(activeTab.dataset.season) : 1;

    // Call selectMedia from scraper.js with show data for season pack
    // Convert genres string to array if needed for auto-select
    const genres = showData.genres ?
        (typeof showData.genres === 'string' ? showData.genres.split(',').map(g => g.trim()) : showData.genres)
        : [];

    await selectMedia(
        showData.tmdb_id || showData.imdb_id,
        showData.title,
        showData.year || '',
        'tv',
        activeSeason, // currently selected season
        null, // episode null for season pack
        true, // multi - season packs are multi-file
        genres, // genre_ids - pass genres for auto-select
        version
    );
}

function handleRefreshTMDB() {
    if (!showData) return;

    const btn = document.getElementById('btn-refresh-tmdb');
    const originalHTML = btn.innerHTML;

    // Show loading state
    btn.disabled = true;
    btn.innerHTML = '<svg class="spin" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"></path></svg>';

    const mediaId = showData.tmdb_id || showData.imdb_id;

    fetch(`/library/refresh_metadata/show/${mediaId}`, {
        method: 'POST'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Reload show data to reflect changes
            loadShowData();
        } else {
            throw new Error(data.error || 'Failed to refresh metadata');
        }
        // Reset button state
        btn.disabled = false;
        btn.innerHTML = originalHTML;
    })
    .catch(error => {
        console.error('Error:', error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: `Error refreshing metadata: ${error.message}`,
            autoClose: 5000
        });
        btn.disabled = false;
        btn.innerHTML = originalHTML;
    });
}

function handleSettings() {
    // Open library settings modal instead of navigating to settings page
    if (typeof window.openLibrarySettingsModal === 'function') {
        window.openLibrarySettingsModal();
    } else {
        // Fallback to old behavior if modal not loaded
        console.warn('[Library Show] Library settings modal not loaded, redirecting to settings page');
        window.location.href = '/settings#library-manager';
    }
}

// =============================================================================
// Multi-Level Deletion System
// =============================================================================

/**
 * Initialize deletion handlers
 */
function initializeDeletionHandlers() {
    // Show-level delete button
    const deleteShowBtn = document.getElementById('delete-show-btn');
    if (deleteShowBtn && showData) {
        deleteShowBtn.dataset.imdbId = showData.imdb_id;
        deleteShowBtn.addEventListener('click', handleDeleteShow);
    }
}

/**
 * Handle delete show button click
 * @param {Event|Object} event - Event or object with skipConfirmation flag
 */
async function handleDeleteShow(event) {
    const imdbId = event.currentTarget ? event.currentTarget.dataset.imdbId : event.imdbId;
    const skipConfirmation = event.skipConfirmation || false;

    if (!imdbId) {
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Cannot delete: No show ID found',
            autoClose: 3000
        });
        return;
    }

    // Custom deletion with progress tracking
    if (!skipConfirmation) {
        const action = showData && showData.auto_ghostlist_enabled ? 'ghostlist' : 'delete';
        const actionUpper = action === 'ghostlist' ? 'ghostlist' : 'delete';
        const canUndo = action === 'ghostlist' ? 'Ghostlisted items can be recovered.' : 'This action cannot be undone.';
        const confirmed = confirm(`This will ${actionUpper} ALL episodes of "${showData.title}". ${canUndo}`);
        if (!confirmed) {
            return;
        }
    }

    // Simulate progress updates
    const steps = [
        'Removing from database...',
        'Removing from media server...',
        'Removing from content sources...',
        'Cleaning up files...',
        'Finalizing deletion...'
    ];

    let currentStep = 0;
    let showedContinueButton = false;
    let progressInterval = null;
    let continueButtonTimeout = null;

    // Cleanup function to clear intervals/timeouts
    const cleanup = () => {
        if (progressInterval) {
            clearInterval(progressInterval);
            progressInterval = null;
        }
        if (continueButtonTimeout) {
            clearTimeout(continueButtonTimeout);
            continueButtonTimeout = null;
        }
    };

    // Show deletion progress loading box with cleanup callback
    showDeletionLoading(`Deleting "${showData.title}"`, cleanup);

    // Start progress interval
    progressInterval = setInterval(() => {
        if (currentStep < steps.length) {
            updateDeletionLoading(steps[currentStep], `Step ${currentStep + 1} of ${steps.length}`);
            currentStep++;

            // Stop at the last step (don't cycle)
            if (currentStep >= steps.length) {
                clearInterval(progressInterval);
                progressInterval = null;
            }
        }
    }, DELETION_PROGRESS_INTERVAL_MS);

    // After 10 seconds, show continue in background button IN THE SAME LOADING BOX
    continueButtonTimeout = setTimeout(() => {
        if (!showedContinueButton) {
            showedContinueButton = true;
            // Update the SAME loading box to show continue button
            updateDeletionLoading(
                'Deletion taking longer than expected...',
                'Processing',
                `This may be delayed due to API cooldown periods (up to ${API_COOLDOWN_MAX_SECONDS} seconds). You can continue in background - deletion will complete automatically.`,
                true  // Show continue button in THIS loading box
            );
        }
    }, DELETION_TIMEOUT_WARNING_MS);

    try {

        const response = await fetch(`/library/delete_show/${imdbId}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                blacklist: false,
                layers: ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache', 'content_source']
            })
        });

        // Check for timeout errors FIRST (504 Gateway Timeout)
        if (response.status === 504) {
            cleanup();
            // Don't hide the loading box - update it to show timeout message with continue button
            updateDeletionLoading(
                'Request Timed Out',
                'Still Processing',
                `The deletion request timed out (this can happen when waiting for API cooldown up to ${API_COOLDOWN_MAX_SECONDS} seconds), but the deletion is likely still running in the background. You can continue in background - deletion will complete automatically. Check logs or refresh library page later to verify completion.`,
                true  // Show continue button
            );
            return;
        }

        // Handle non-JSON responses (like HTML error pages)
        const contentType = response.headers.get('content-type');
        if (!contentType || !contentType.includes('application/json')) {
            const text = await response.text();
            if (text.includes('504 Gateway Time-out') || text.includes('Gateway Timeout')) {
                cleanup();
                // Don't hide the loading box - update it to show timeout message with continue button
                updateDeletionLoading(
                    'Request Timed Out',
                    'Still Processing',
                    `The deletion request timed out (this can happen when waiting for API cooldown up to ${API_COOLDOWN_MAX_SECONDS} seconds), but the deletion is likely still running in the background. You can continue in background - deletion will complete automatically. Check logs or refresh library page later to verify completion.`,
                    true  // Show continue button
                );
                return;
            }
            cleanup();
            hideDeletionLoading();
            throw new Error('Unexpected response format from server');
        }

        const result = await response.json();

        // Now that we have a successful response, clean up the loading UI
        cleanup();
        hideDeletionLoading();

        if (result && result.success) {
                // Build deletion report using shared utility
                const reportMessage = buildDeletionReport(result, showData.title);

                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    message: reportMessage,
                    autoClose: false,  // Require user to close manually
                    onConfirm: () => {
                        // Redirect to library page after user closes notification
                        window.location.href = '/library';
                    }
                });

                // Add close button callback for redirect
                setTimeout(() => {
                    const closeButton = document.querySelector('.universal-popup #popupClose');
                    if (closeButton) {
                        closeButton.onclick = () => {
                            window.location.href = '/library';
                        };
                    }
                }, 100);
            } else {
                throw new Error(result.error || 'Failed to delete show');
            }
    } catch (error) {
        cleanup();
        hideDeletionLoading();
        console.error('Error deleting show:', error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: `Error deleting show: ${error.message}`,
            autoClose: 5000
        });
    }
}

/**
 * Handle delete season button click
 * Based on handleDeleteShow pattern with progress tracking
 */
async function handleDeleteSeason(event) {
    const button = event.currentTarget;
    const seasonNumber = parseInt(button.dataset.seasonNumber);
    const imdbId = button.dataset.imdbId;

    if (!imdbId || isNaN(seasonNumber)) {
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Cannot delete: Invalid season information',
            autoClose: 3000
        });
        return;
    }

    const seasonData = seasonsData.find(s => s.season_number === seasonNumber);
    const episodeCount = seasonData ? seasonData.episodes.length : 0;
    const seasonTitle = `Season ${seasonNumber}`;

    // Check if this is the last season
    const totalSeasons = seasonsData.length;
    if (totalSeasons === 1) {
        // This is the last season - offer to delete entire show
        const choice = await showChoicePopup(
            'Last Season',
            'This is the only season left. Deleting it will leave the show empty.',
            [
                { label: 'Delete Entire Show', value: 'show', primary: true },
                { label: 'Delete Season Only', value: 'season' },
                { label: 'Cancel', value: 'cancel' }
            ]
        );

        if (choice === 'show') {
            // Trigger show deletion directly with confirmation already handled
            await handleDeleteShow({
                imdbId: imdbId,
                skipConfirmation: true
            });
            return;
        } else if (choice !== 'season') {
            // User cancelled or closed popup
            return;
        }
        // If choice === 'season', continue with season deletion below
    }

    // Confirm deletion - season deletion always uses delete (not ghostlist)
    const confirmed = confirm(`This will delete all ${episodeCount} episodes in ${seasonTitle}. This action cannot be undone.`);
    if (!confirmed) {
        return;
    }

    // Progress steps for season deletion (no content source removal)
    const steps = [
        'Removing from database...',
        'Removing from media server...',
        'Cleaning up files...',
        'Removing from debrid...',
        'Finalizing deletion...'
    ];

    let currentStep = 0;
    let showedContinueButton = false;
    let progressInterval = null;
    let continueButtonTimeout = null;

    // Cleanup function to clear intervals/timeouts
    const cleanup = () => {
        if (progressInterval) {
            clearInterval(progressInterval);
            progressInterval = null;
        }
        if (continueButtonTimeout) {
            clearTimeout(continueButtonTimeout);
            continueButtonTimeout = null;
        }
    };

    // Show deletion progress loading box with cleanup callback
    showDeletionLoading(`Deleting ${seasonTitle}`, cleanup);

    // Start progress interval
    progressInterval = setInterval(() => {
        if (currentStep < steps.length) {
            updateDeletionLoading(steps[currentStep], `Step ${currentStep + 1} of ${steps.length}`);
            currentStep++;

            // Stop at the last step (don't cycle)
            if (currentStep >= steps.length) {
                clearInterval(progressInterval);
                progressInterval = null;
            }
        }
    }, DELETION_PROGRESS_INTERVAL_MS);

    // After 10 seconds, show continue in background button IN THE SAME LOADING BOX
    continueButtonTimeout = setTimeout(() => {
        if (!showedContinueButton) {
            showedContinueButton = true;
            // Update the SAME loading box to show continue button
            updateDeletionLoading(
                'Deletion taking longer than expected...',
                'Processing',
                `This may be delayed due to API cooldown periods (up to ${API_COOLDOWN_MAX_SECONDS} seconds). You can continue in background - deletion will complete automatically.`,
                true  // Show continue button in THIS loading box
            );
        }
    }, DELETION_TIMEOUT_WARNING_MS);

    try {
        const response = await fetch(`/library/delete_season/${imdbId}/${seasonNumber}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                blacklist: false,
                layers: ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache']
            })
        });

        // Check for timeout errors FIRST (504 Gateway Timeout)
        if (response.status === 504) {
            cleanup();
            // Don't hide the loading box - update it to show timeout message with continue button
            updateDeletionLoading(
                'Request Timed Out',
                'Still Processing',
                `The deletion request timed out (this can happen when waiting for API cooldown up to ${API_COOLDOWN_MAX_SECONDS} seconds), but the deletion is likely still running in the background. You can continue in background - deletion will complete automatically. Refresh this page later to verify completion.`,
                true  // Show continue button
            );
            return;
        }

        // Handle non-JSON responses (like HTML error pages)
        const contentType = response.headers.get('content-type');
        if (!contentType || !contentType.includes('application/json')) {
            const text = await response.text();
            if (text.includes('504 Gateway Time-out') || text.includes('Gateway Timeout')) {
                cleanup();
                // Don't hide the loading box - update it to show timeout message with continue button
                updateDeletionLoading(
                    'Request Timed Out',
                    'Still Processing',
                    `The deletion request timed out (this can happen when waiting for API cooldown up to ${API_COOLDOWN_MAX_SECONDS} seconds), but the deletion is likely still running in the background. You can continue in background - deletion will complete automatically. Refresh this page later to verify completion.`,
                    true  // Show continue button
                );
                return;
            }
            cleanup();
            hideDeletionLoading();
            throw new Error('Unexpected response format from server');
        }

        const result = await response.json();

        // Now that we have a successful response, clean up the loading UI
        cleanup();
        hideDeletionLoading();

        if (result && result.success) {
            // Build deletion report using shared utility
            const reportMessage = buildDeletionReport(result, seasonTitle);

            showPopup({
                type: POPUP_TYPES.SUCCESS,
                message: reportMessage,
                autoClose: false,  // Require user to close manually
                onConfirm: () => {
                    // Reload show data after user closes notification
                    loadShowData();
                }
            });

            // Add close button callback for reload
            setTimeout(() => {
                const closeButton = document.querySelector('.universal-popup #popupClose');
                if (closeButton) {
                    closeButton.onclick = () => {
                        loadShowData();
                    };
                }
            }, 100);
        } else {
            throw new Error(result.error || 'Failed to delete season');
        }
    } catch (error) {
        cleanup();
        hideDeletionLoading();
        console.error('Error deleting season:', error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: `Error deleting season: ${error.message}`,
            autoClose: 5000
        });
    }
}

/**
 * Show a choice popup with multiple options
 */
function showChoicePopup(title, message, choices) {
    return new Promise((resolve) => {
        const popupHtml = `
            <div class="file-selection-popup-overlay" id="choicePopup">
                <div class="file-selection-popup">
                    <h3>${escapeHtml(title)}</h3>
                    <p class="file-selection-subtitle">${escapeHtml(message)}</p>
                    <div class="file-selection-actions" style="flex-direction: column; gap: 0.75rem;">
                        ${choices.map(choice => `
                            <button class="file-selection-btn choice-btn" data-value="${choice.value}" >
                                ${escapeHtml(choice.label)}
                            </button>
                        `).join('')}
                    </div>
                </div>
            </div>
        `;

        document.body.insertAdjacentHTML('beforeend', popupHtml);

        const popup = document.getElementById('choicePopup');
        const buttons = popup.querySelectorAll('.choice-btn');

        buttons.forEach(button => {
            button.addEventListener('click', () => {
                const value = button.dataset.value;
                popup.remove();
                resolve(value);
            });
        });

        // Close on overlay click
        popup.addEventListener('click', (e) => {
            if (e.target === popup) {
                popup.remove();
                resolve(null);
            }
        });
    });
}

/**
 * Show file selection popup for episodes with multiple files
 */
function showFileSelectionPopup(files, episodeTitle) {
    return new Promise((resolve) => {
        // Create popup HTML
        const popupHtml = `
            <div class="file-selection-popup-overlay" id="fileSelectionPopup">
                <div class="file-selection-popup">
                    <h3>Select Files to Delete</h3>
                    <p class="file-selection-subtitle">${episodeTitle}</p>
                    <div class="file-selection-list">
                        ${files.map((file, index) => `
                            <div class="file-selection-item">
                                <input type="checkbox"
                                       id="file-${file.id}"
                                       value="${file.id}"
                                       ${files.length === 1 ? 'checked' : ''}>
                                <label for="file-${file.id}">
                                    <span class="file-number">${index + 1}.</span>
                                    <span class="file-name">${escapeHtml(file.file)}</span>
                                    <span class="file-version">${escapeHtml(file.version)}</span>
                                </label>
                            </div>
                        `).join('')}
                    </div>
                    <div class="file-selection-actions">
                        <button class="file-selection-btn file-selection-cancel">Cancel</button>
                        <button class="file-selection-btn file-selection-delete">Delete</button>
                    </div>
                </div>
            </div>
        `;

        // Insert into body
        document.body.insertAdjacentHTML('beforeend', popupHtml);

        const popup = document.getElementById('fileSelectionPopup');
        const deleteBtn = popup.querySelector('.file-selection-delete');
        const cancelBtn = popup.querySelector('.file-selection-cancel');

        // Handle delete button click
        deleteBtn.addEventListener('click', () => {
            const checkboxes = popup.querySelectorAll('input[type="checkbox"]:checked');
            const selectedIds = Array.from(checkboxes).map(cb => parseInt(cb.value));

            if (selectedIds.length === 0) {
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'Please select at least one file to delete',
                    autoClose: 3000
                });
                return;
            }

            popup.remove();
            resolve(selectedIds);
        });

        // Handle cancel button click
        cancelBtn.addEventListener('click', () => {
            popup.remove();
            resolve(null);
        });

        // Close on overlay click
        popup.addEventListener('click', (e) => {
            if (e.target === popup) {
                popup.remove();
                resolve(null);
            }
        });
    });
}

/**
 * Handle delete episode button click
 * Supports single and multi-file episodes with progress tracking
 */
async function handleDeleteEpisode(event) {
    const button = event.currentTarget;
    const imdbId = button.dataset.imdbId;
    const seasonNumber = parseInt(button.dataset.season);
    const episodeNumber = parseInt(button.dataset.episode);
    const episodeIdsStr = button.dataset.episodeIds;
    const episodeFilesStr = button.dataset.episodeFiles;

    if (!imdbId || !seasonNumber || !episodeNumber || !episodeIdsStr) {
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Cannot delete: Missing episode information',
            autoClose: 3000
        });
        return;
    }

    try {
        // Parse episode files data
        const episodeFiles = JSON.parse(episodeFilesStr);
        const episodeTitle = `S${seasonNumber.toString().padStart(2, '0')}E${episodeNumber.toString().padStart(2, '0')}`;

        // Check if this is the last episode of the last season
        console.log('[EPISODE DELETE] Checking if last episode...', {
            totalSeasons: seasonsData.length,
            currentSeasonNumber: seasonNumber,
            currentEpisodeNumber: episodeNumber,
            seasonsData: seasonsData
        });

        const totalSeasons = seasonsData.length;
        if (totalSeasons === 1) {
            // Count total episodes with real presence (collected or blacklisted)
            // EXCLUDING the current episode we're about to delete
            const season = seasonsData[0];
            const visibleEpisodes = season.episodes.filter(ep => {
                // Count COLLECTED or BLACKLISTED episodes (episodes with real presence)
                // Exclude: ghostlisted episodes and the current episode being deleted
                const isNotGhostlisted = !ep.ghostlisted || ep.ghostlisted === 0;  // Handle undefined/null/0
                const hasRealPresence = isNotGhostlisted &&
                                       (ep.state === 'Collected' || ep.state === 'Blacklisted');
                const isNotCurrentEpisode = ep.episode_number !== episodeNumber;
                return hasRealPresence && isNotCurrentEpisode;
            });
            const totalEpisodes = visibleEpisodes.length;

            console.log('[EPISODE DELETE] Last season check:', {
                seasonNumber: season.season_number,
                totalVisibleEpisodesExcludingCurrent: totalEpisodes,
                allEpisodes: season.episodes.length,
                visibleEpisodes: visibleEpisodes.map(ep => `E${ep.episode_number} (${ep.state})`),
                allEpisodesWithStates: season.episodes.map(ep => `E${ep.episode_number} (${ep.state}, ghostlisted=${ep.ghostlisted})`)
            });

            if (totalEpisodes === 0) {
                console.log('[EPISODE DELETE] This is the last visible episode - showing choice popup');
                // This is the last episode of the only season - offer to delete entire show
                const choice = await showChoicePopup(
                    'Last Episode',
                    'This is the only episode left in this show. Would you like to delete the entire show instead?',
                    [
                        { label: 'Delete Entire Show', value: 'show', primary: true },
                        { label: 'Delete Episode Only', value: 'episode' },
                        { label: 'Cancel', value: 'cancel' }
                    ]
                );

                console.log('[EPISODE DELETE] User choice:', choice);

                if (choice === 'show') {
                    // Trigger show deletion directly with confirmation already handled
                    await handleDeleteShow({
                        imdbId: imdbId,
                        skipConfirmation: true
                    });
                    return;
                } else if (choice !== 'episode') {
                    // User cancelled or closed popup
                    return;
                }
                // If choice === 'episode', continue with episode deletion below
            }
        }

        // For multiple files, show file selection popup
        let selectedItemIds;
        if (episodeFiles.length > 1) {
            selectedItemIds = await showFileSelectionPopup(episodeFiles, episodeTitle);

            // User cancelled
            if (!selectedItemIds) {
                return;
            }
        } else {
            // Single file - episode deletion always uses delete (not ghostlist)
            const confirmed = confirm(`Delete ${episodeTitle}?\n\nThis action cannot be undone.`);
            if (!confirmed) return;

            selectedItemIds = episodeFiles.map(f => f.id);
        }

        // Progress steps (no content source removal)
        const steps = [
            'Removing from database...',
            'Removing from media server...',
            'Cleaning up files...',
            'Removing from debrid...',
            'Finalizing deletion...'
        ];

        // Progress tracking with cleanup
        let currentStep = 0;
        let showedContinueButton = false;
        let progressInterval = null;
        let continueButtonTimeout = null;

        const cleanup = () => {
            if (progressInterval) clearInterval(progressInterval);
            if (continueButtonTimeout) clearTimeout(continueButtonTimeout);
        };

        // Show loading box
        showDeletionLoading(`Deleting ${episodeTitle}`, cleanup);

        // Start progress updates
        progressInterval = setInterval(() => {
            if (currentStep < steps.length) {
                updateDeletionLoading(steps[currentStep], `Step ${currentStep + 1} of ${steps.length}`);
                currentStep++;
                if (currentStep >= steps.length) {
                    clearInterval(progressInterval);
                    progressInterval = null;
                }
            }
        }, DELETION_PROGRESS_INTERVAL_MS);

        // After 10 seconds, show continue button
        continueButtonTimeout = setTimeout(() => {
            if (!showedContinueButton) {
                showedContinueButton = true;
                updateDeletionLoading(
                    'Deletion taking longer than expected...',
                    'Processing',
                    `This may be delayed due to API cooldown periods (up to ${API_COOLDOWN_MAX_SECONDS} seconds). You can continue in background - deletion will complete automatically.`,
                    true
                );
            }
        }, DELETION_TIMEOUT_WARNING_MS);

        // Make deletion request
        const response = await fetch(`/library/delete_episode/${imdbId}/${seasonNumber}/${episodeNumber}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                item_ids: selectedItemIds,
                layers: ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache']
            })
        });

        // Handle 504 timeout
        if (response.status === 504) {
            cleanup();
            updateDeletionLoading(
                'Request Timed Out',
                'Still Processing',
                `The deletion request timed out (this can happen when waiting for API cooldown up to ${API_COOLDOWN_MAX_SECONDS} seconds), but the deletion is likely still running in the background. You can continue in background - deletion will complete automatically. Refresh this page later to verify completion.`,
                true
            );
            return;
        }

        // Handle non-JSON responses
        const contentType = response.headers.get('content-type');
        if (!contentType || !contentType.includes('application/json')) {
            const text = await response.text();
            if (text.includes('504 Gateway Time-out') || text.includes('Gateway Timeout')) {
                cleanup();
                updateDeletionLoading(
                    'Request Timed Out',
                    'Still Processing',
                    `The deletion request timed out...`,
                    true
                );
                return;
            }
            cleanup();
            hideDeletionLoading();
            throw new Error('Unexpected response format from server');
        }

        const result = await response.json();
        cleanup();
        hideDeletionLoading();

        if (result && result.success) {
            // Build detailed deletion report
            const reportMessage = buildDeletionReport(result, episodeTitle);

            showPopup({
                type: POPUP_TYPES.SUCCESS,
                message: reportMessage,
                autoClose: false,
                onConfirm: () => loadShowData()
            });

            // Add close button callback
            setTimeout(() => {
                const closeButton = document.querySelector('.universal-popup #popupClose');
                if (closeButton) {
                    closeButton.onclick = () => loadShowData();
                }
            }, 100);
        } else {
            throw new Error(result.error || 'Failed to delete episode');
        }
    } catch (error) {
        cleanup();
        hideDeletionLoading();
        console.error('Error deleting episode:', error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: `Error deleting episode: ${error.message}`,
            autoClose: 5000
        });
    }
}

// =============================================================================
// Cast Section
// =============================================================================

/**
 * Load cast data from TMDB API
 */
async function loadCast(tmdbId) {
    try {
        const response = await fetch(`/library/cast/tv/${tmdbId}`);
        const data = await response.json();

        if (data.success && data.cast && data.cast.length > 0) {
            displayCast(data.cast);
        }
    } catch (error) {
        console.error('[Show Detail] Error loading cast:', error);
    }
}

/**
 * Display cast in the cast grid
 */
function displayCast(cast) {
    const castSection = document.getElementById('cast-section');
    const castGrid = document.getElementById('cast-grid');
    const castHeader = document.getElementById('cast-header');

    if (!castSection || !castGrid) return;

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

