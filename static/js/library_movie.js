/**
 * Library Movie Detail - TV Movie video browser
 * Displays movie metadata, files, and videos with details
 * Auto-ghostlist scope fix v2.0 - 2026-01-13
 * Episode-style file boxes v3.0 - 2026-01-13
 * Removed inline badge styles v4.0 - 2026-01-13
 */

// Deletion timing constants
const DELETION_PROGRESS_INTERVAL_MS = 800;      // How often to update progress steps
const DELETION_TIMEOUT_WARNING_MS = 10000;      // When to show "taking longer than expected" message
const API_COOLDOWN_MAX_SECONDS = 96;            // Maximum API cooldown period (Trakt rate limit)

/**
 * Format a file path for display with 25 character limit, rounded down to nearest folder
 * Example: /192.168.1.124_Movies/2.Fast.2.Furious.2003.PROPER.2160p.BluRay.REMUX.HEVC.DTS-X.7.1-FGT.mkv
 *   -> Truncate to 25 chars: "/192.168.1.124_Movies/2.F"
 *   -> Find last "/" before char 25: position 21
 *   -> Result: "/192.168.1.124_Movies"
 * @param {string} path - Full file path
 * @returns {string} Formatted path truncated to 25 chars at nearest folder break
 */
function formatPathDisplay(path) {
    if (!path) return '';

    const MAX_LENGTH = 25;

    // If path is already short enough, return as-is
    if (path.length <= MAX_LENGTH) return path;

    // Truncate to MAX_LENGTH
    let truncated = path.substring(0, MAX_LENGTH);

    // Find the last "/" in the truncated string
    const lastSlash = truncated.lastIndexOf('/');

    // If we found a slash, truncate there, otherwise use the full truncated string
    return lastSlash > 0 ? truncated.substring(0, lastSlash) : truncated;
}

// Deletion wrapper functions - these call shared deletion utilities from notifications.js
// Using movie-specific names for clarity, but internally use the same shared functions
// These functions are exposed globally via window object from notifications.js module
function movieDeletionLoading(title, cleanup) {
    return window.showDeletionLoading(title, cleanup);
}

function moviePopup(options) {
    return window.showPopup(options);
}

// State management
let movieData = null;
let filesData = [];
let brokenFiles = new Set();  // Set of broken filenames from __unplayable__ folder

// DOM elements
let movieMainGrid, filesContainer, emptyState;
let pageBackdrop, pageBackdropImg;

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    console.log('[Movie Detail] Page loaded, initializing...');

    try {
        initializeElements();
        attachEventListeners();
        loadMovieData();
    } catch (error) {
        console.error('[Movie Detail] Initialization error:', error);
    }
});

function initializeElements() {
    movieMainGrid = document.getElementById('movie-main-grid');
    filesContainer = document.getElementById('files-container');
    emptyState = document.getElementById('empty-state');
    pageBackdrop = document.getElementById('page-backdrop');
    pageBackdropImg = document.getElementById('page-backdrop-img');
}

function attachEventListeners() {
    // Action buttons
    const btnGetMissing = document.getElementById('btn-get-missing');
    const btnFilePacks = document.getElementById('btn-file-packs');
    const btnRefreshTMDB = document.getElementById('btn-refresh-tmdb');
    const btnSettings = document.getElementById('btn-settings');
    const btnSearchMovie = document.getElementById('btn-search-movie');

    if (btnGetMissing) {
        btnGetMissing.addEventListener('click', handleGetMissing);
    }

    if (btnFilePacks) {
        btnFilePacks.addEventListener('click', handleFilePacks);
    }

    if (btnRefreshTMDB) {
        btnRefreshTMDB.addEventListener('click', handleRefreshTMDB);
    }

    if (btnSettings) {
        btnSettings.addEventListener('click', handleSettings);
    }

    if (btnSearchMovie) {
        btnSearchMovie.addEventListener('click', handleSearchMovie);
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

async function loadMovieData() {
    const container = document.querySelector('.movie-container');
    const mediaId = container.dataset.mediaId;

    if (!mediaId) {
        movieError('No media ID provided');
        return;
    }

    try {

        // Fetch movie data and broken files in parallel
        const [movieResponse, brokenResponse] = await Promise.all([
            fetch(`/library/movie/${mediaId}/data`),
            fetch('/library/check_broken_files', { method: 'POST' })
        ]);

        const data = await movieResponse.json();
        const brokenData = await brokenResponse.json();

        // Store broken files in Set for fast lookup
        if (brokenData.success && brokenData.broken_files) {
            brokenFiles = new Set(brokenData.broken_files);
            console.log(`[Movie Detail] Loaded ${brokenFiles.size} broken files`);
        }

        if (data.success) {
            movieData = data.movie;
            filesData = data.files;

            renderMovieHeader(movieData);
            renderFiles(filesData);

            // Initialize deletion handlers after data is loaded
            initializeDeletionHandlers();

            if (filesData.length > 0) {
                movieMainGrid.style.display = 'grid';

                // Show page actions
                const pageActions = document.getElementById('page-actions');
                if (pageActions) {
                    pageActions.style.display = 'flex';
                }

                // Align sidebar with files container
                alignSidebarWithFiles();
            } else {
                emptyState.style.display = 'flex';
            }
        } else {
            movieError(data.error || 'Failed to load movie details');
        }
    } catch (error) {
        console.error('[Movie Detail] Error loading data:', error);
        movieError('Failed to load movie details. Please try again.');
    }
}

function renderMovieHeader(movie) {
    console.log('[Movie Detail] Rendering header with data:', movie);
    console.log('[Movie Detail] Metadata values - overview:', movie.overview, 'genres:', movie.genres, 'network:', movie.network, 'status:', movie.status);

    // Set title with year in parentheses
    const titleText = movie.title + (movie.year ? ` (${movie.year})` : '');
    const titleEl = document.getElementById('movie-title');
    if (titleEl) {
        titleEl.textContent = titleText;
    } else {
        console.error('[Movie Detail] movie-title element not found');
    }

    // Set poster
    if (movie.poster_url) {
        let posterUrl = movie.poster_url;

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
    if (movie.backdrop_url) {
        let backdropUrl = movie.backdrop_url;

        // Handle different backdrop URL formats
        if (backdropUrl.startsWith('plex:')) {
            const plexPath = backdropUrl.substring(5);
            backdropUrl = `/library/plex_image${plexPath}`;
        } else if (backdropUrl.startsWith('/') && !backdropUrl.startsWith('/static') && !backdropUrl.startsWith('/library')) {
            // TMDB backdrop path
            backdropUrl = `/scraper/tmdb_image/w1280${backdropUrl}`;
        }

        pageBackdropImg.src = backdropUrl;
        pageBackdropImg.alt = `${movie.title} Backdrop`;
        pageBackdrop.style.display = 'block';
    }

    // Set inline metadata for movies
    const yearText = document.getElementById('movie-year-text');
    if (yearText && movie.rating) {
        // Display rating with 1 decimal point and star icon
        const ratingValue = parseFloat(movie.rating).toFixed(1);
        yearText.innerHTML = `
            <span style="display: inline-flex; align-items: center; gap: 0.25rem; vertical-align: bottom;">
                <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="width: 1em; height: 1em; display: block;">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z"></path>
                </svg>
                <span>${ratingValue}</span>
            </span>
        `;
    } else if (yearText) {
        yearText.textContent = '-';
    }

    const runtimeText = document.getElementById('movie-runtime-text');
    if (runtimeText && movie.runtime) {
        runtimeText.textContent = `${movie.runtime} min`;
    }

    const genresText = document.getElementById('movie-genres-text');
    if (genresText && movie.genres) {
        genresText.textContent = movie.genres;
    }

    // Set tagline if available
    const taglineEl = document.getElementById('movie-tagline');
    if (taglineEl) {
        if (movie.tagline && movie.tagline.trim()) {
            taglineEl.textContent = movie.tagline;
            taglineEl.style.display = 'block';
        } else {
            taglineEl.style.display = 'none';
        }
    }

    // Update details row
    const qualityValue = document.getElementById('quality-value');
    if (qualityValue && movie.version) {
        qualityValue.textContent = movie.version;
    }

    const pathValue = document.getElementById('path-value');
    if (pathValue && movie.location_on_disk) {
        pathValue.textContent = formatPathDisplay(movie.location_on_disk);
    }

    const addedValue = document.getElementById('added-value');
    if (addedValue && movie.collected_at) {
        const date = new Date(movie.collected_at);
        addedValue.textContent = formatDate(date);
    }

    const sizeValue = document.getElementById('size-value');
    if (sizeValue && movie.size !== null && movie.size !== undefined) {
        sizeValue.textContent = `${movie.size.toFixed(2)} GB`;
    } else if (sizeValue) {
        sizeValue.textContent = '-';
    }

    // Update external links in header
    const tmdbLink = document.getElementById('link-tmdb');
    if (tmdbLink && movie.tmdb_id) {
        tmdbLink.href = `https://www.themoviedb.org/movie/${movie.tmdb_id}`;
    }

    const imdbLink = document.getElementById('link-imdb');
    if (imdbLink && movie.imdb_id) {
        imdbLink.href = `https://www.imdb.com/title/${movie.imdb_id}`;
    }

    const traktLink = document.getElementById('link-trakt');
    if (traktLink) {
        // Prefer IMDb ID, fall back to TMDB ID
        if (movie.imdb_id) {
            traktLink.href = `https://trakt.tv/movies/${movie.imdb_id}`;
        } else if (movie.tmdb_id) {
            traktLink.href = `https://trakt.tv/movies/${movie.tmdb_id}`;
        }
    }

    // Set overview in sidebar
    const overviewEl = document.getElementById('movie-overview');
    if (overviewEl) {
        overviewEl.textContent = movie.overview || '-';
    }

    // Set details in sidebar - movie specific fields
    const releaseDateEl = document.getElementById('movie-release-date');
    if (releaseDateEl) {
        releaseDateEl.textContent = movie.release_date || '-';
    }

    const runtimeEl = document.getElementById('movie-runtime');
    if (runtimeEl) {
        runtimeEl.textContent = movie.runtime ? `${movie.runtime} min` : '-';
    }

    const ratingEl = document.getElementById('movie-rating');
    if (ratingEl) {
        ratingEl.textContent = movie.rating ? `${movie.rating}/10` : '-';
    }

    const genresEl = document.getElementById('movie-genres');
    if (genresEl) {
        genresEl.textContent = movie.genres || '-';
    }

    // Set external links in sidebar
    const tmdbLinkDetail = document.getElementById('tmdb-link-detail');
    if (tmdbLinkDetail && movie.tmdb_id) {
        tmdbLinkDetail.href = `https://www.themoviedb.org/movie/${movie.tmdb_id}`;
        tmdbLinkDetail.textContent = movie.tmdb_id;
    }

    const imdbLinkDetail = document.getElementById('imdb-link-detail');
    if (imdbLinkDetail && movie.imdb_id) {
        imdbLinkDetail.href = `https://www.imdb.com/title/${movie.imdb_id}`;
        imdbLinkDetail.textContent = movie.imdb_id;
    }

    // Set storage info in sidebar
    const sourcesEl = document.getElementById('movie-sources');
    if (sourcesEl) {
        sourcesEl.textContent = (movie.content_sources && movie.content_sources.length > 0) ? movie.content_sources.join(', ') : '-';
    }

    const rcloneEl = document.getElementById('movie-rclone');
    if (rcloneEl) {
        rcloneEl.textContent = movie.rclone_path || '-';
    }

    const pathEl = document.getElementById('movie-path');
    if (pathEl && movie.location_on_disk) {
        pathEl.textContent = formatPathDisplay(movie.location_on_disk);
    } else if (pathEl) {
        pathEl.textContent = '-';
    }

    // Store movie ID in delete button and update icon based on auto-ghostlist setting
    const deleteBtn = document.getElementById('delete-movie-btn');
    if (deleteBtn) {
        deleteBtn.dataset.imdbId = movie.imdb_id || movie.tmdb_id;
        deleteBtn.dataset.tmdbId = movie.tmdb_id;

        // Check if auto-ghostlist setting is enabled
        const useGhostIcon = movie.auto_ghostlist_enabled === true;

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

        deleteBtn.innerHTML = useGhostIcon ? ghostIcon : trashIcon;
        deleteBtn.title = useGhostIcon ? 'Ghostlist Movie' : 'Delete Movie';
        deleteBtn.setAttribute('aria-label', useGhostIcon ? 'Ghostlist entire movie' : 'Delete entire movie');
    }

    console.log('[Movie Detail] Header rendering complete');
}

function renderFiles(files) {
    const filesContent = document.getElementById('files-content');

    if (!filesContent) {
        console.error('[Movie Detail] files-content element not found');
        return;
    }

    filesContent.innerHTML = '';

    if (!files || files.length === 0) {
        filesContent.innerHTML = '<p style="text-align: center; padding: 2rem; color: rgba(255, 255, 255, 0.6);">No files found for this movie.</p>';
        return;
    }

    // Create file list container
    const fileList = document.createElement('div');
    fileList.className = 'movie-files-list';
    fileList.style.cssText = 'display: flex; flex-direction: column; gap: 0.75rem; padding: 1rem;';

    files.forEach((file, index) => {
        const fileRow = createFileRow(file, index + 1);
        fileList.appendChild(fileRow);
    });

    filesContent.appendChild(fileList);
    console.log('[Movie Detail] Rendered', files.length, 'file(s)');
}

function createFileRow(file, rowNumber) {
    const row = document.createElement('div');
    row.className = 'movie-file-row';
    // Hover effect now handled by CSS

    // Row number
    const number = document.createElement('div');
    number.className = 'movie-file-number'
    number.textContent = rowNumber;

    // Status indicator (green checkmark for Collected, red X for Blacklisted)
    const status = document.createElement('div');
    status.className = 'movie-file-status-icon';
    const isCollected = file.state === 'Collected';
    const isBlacklisted = file.state === 'Blacklisted';

    if (isCollected) {
        status.innerHTML = '<svg width="20" height="20" viewBox="0 0 20 20" fill="#4CAF50"><circle cx="10" cy="10" r="10"/><path d="M6 10l3 3 5-6" stroke="white" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    } else if (isBlacklisted) {
        status.innerHTML = '<svg width="20" height="20" viewBox="0 0 20 20" fill="#ef4444"><circle cx="10" cy="10" r="10"/><path d="M6 6l8 8M14 6l-8 8" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    } else {
        status.innerHTML = '<svg width="20" height="20" viewBox="0 0 20 20" fill="#666"><circle cx="10" cy="10" r="10"/></svg>';
    }

    // File info section
    const info = document.createElement('div');
    info.className = 'movie-file-info-section';

    const fileName = file.basename || file.filename || 'Unknown file';
    const version = file.version || 'Default';

    // Determine status label and value
    let statusLabel, statusValue;
    if (isCollected && file.collected_at) {
        statusLabel = 'Collected';
        statusValue = new Date(file.collected_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    } else if (isBlacklisted) {
        statusLabel = 'Status';
        statusValue = 'Blacklisted';
    } else {
        statusLabel = 'Status';
        statusValue = file.state || 'Unknown';
    }

    // Format size
    const sizeText = (file.size !== null && file.size !== undefined) ? `${file.size.toFixed(2)} GB` : '';

    info.innerHTML = `
        <div style="display: flex; align-items: center; gap: 0.5rem;">
            <span class="movie-file-title">${fileName}</span>
        </div>
        <div class="movie-file-meta">
            <span class="file-version">${version}</span>
            ${sizeText ? `<span class="file-version">${sizeText}</span>` : ''}
            <span>•</span>
            <span>${statusLabel}: ${statusValue}</span>
        </div>
    `;

    // Action buttons
    const actions = document.createElement('div');
    actions.style.cssText = 'display: flex; align-items: center; gap: 0.5rem;';

    // Move to Wanted button
    const wantedBtn = document.createElement('button');
    wantedBtn.className = 'file-action-btn wanted-btn';
    wantedBtn.title = 'Move to Wanted state';
    wantedBtn.dataset.fileId = file.id;
    wantedBtn.style.cssText = `
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 0.5rem;
        background: transparent;
        border: 1px solid rgba(255, 255, 255, 0.2);
        border-radius: 0.375rem;
        color: rgba(255, 255, 255, 0.7);
        cursor: pointer;
        transition: all 0.2s ease;
    `;
    wantedBtn.innerHTML = `
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 15V3"></path>
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
            <path d="m7 10 5 5 5-5"></path>
        </svg>
    `;

    wantedBtn.addEventListener('mouseenter', () => {
        wantedBtn.style.background = 'rgba(255, 255, 255, 0.1)';
        wantedBtn.style.borderColor = 'rgba(255, 255, 255, 0.3)';
    });
    wantedBtn.addEventListener('mouseleave', () => {
        wantedBtn.style.background = 'transparent';
        wantedBtn.style.borderColor = 'rgba(255, 255, 255, 0.2)';
    });
    wantedBtn.addEventListener('click', () => handleMoveFileToWanted(file.id));

    // Individual file delete button - always use delete (not ghostlist) even if auto_ghostlist_enabled
    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'file-action-btn delete-btn';
    deleteBtn.title = 'Delete this file';
    deleteBtn.dataset.fileId = file.id;
    deleteBtn.style.cssText = `
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 0.5rem;
        background: transparent;
        border: 1px solid rgba(239, 68, 68, 0.3);
        border-radius: 0.375rem;
        color: #ef4444;
        cursor: pointer;
        transition: all 0.2s ease;
    `;

    // Always use trash icon for individual file deletion
    deleteBtn.innerHTML = `
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M10 11v6"></path>
            <path d="M14 11v6"></path>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"></path>
            <path d="M3 6h18"></path>
            <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
        </svg>
    `;

    deleteBtn.addEventListener('mouseenter', () => {
        deleteBtn.style.background = 'rgba(239, 68, 68, 0.15)';
        deleteBtn.style.borderColor = 'rgba(239, 68, 68, 0.5)';
    });
    deleteBtn.addEventListener('mouseleave', () => {
        deleteBtn.style.background = 'transparent';
        deleteBtn.style.borderColor = 'rgba(239, 68, 68, 0.3)';
    });

    actions.appendChild(wantedBtn);
    actions.appendChild(deleteBtn);

    row.appendChild(number);
    row.appendChild(status);
    row.appendChild(info);
    row.appendChild(actions);

    return row;
}

function handleMoveFileToWanted(fileId) {
    if (!movieData) return;

    const data = {
        imdb_id: movieData.imdb_id,
        tmdb_id: movieData.tmdb_id
    };

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
            // Reload the movie data to reflect the updated state
            loadMovieData();
        } else {
            throw new Error(data.error || 'Failed to move item to Wanted state');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: `Error moving item to Wanted state: ${error.message}`,
            autoClose: 5000
        });
    });
}

function movieError(message) {
    console.error('[Movie Detail] Error:', message);
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
            sidebar.style.marginTop = '20px'; // Fixed for mobile
        }
    }
}

// Add resize listener for responsive sidebar margin adjustment
// Handles orientation changes and window resizing without page refresh
let movieSidebarResizeTimeout;
window.addEventListener('resize', function() {
    clearTimeout(movieSidebarResizeTimeout);
    movieSidebarResizeTimeout = setTimeout(alignSidebarWithFiles, 150);
});

function formatDate(date) {
    const options = { year: 'numeric', month: 'short', day: 'numeric' };
    return date.toLocaleDateString(undefined, options);
}

// ============================================================================
// Action Button Handlers
// ============================================================================

async function handleSearchMovie() {
    if (!movieData) return;

    const version = movieData.version || 'Default';

    // Call selectMedia to search for this movie
    await selectMedia(
        movieData.tmdb_id || movieData.id,
        movieData.title,
        movieData.year || '',
        'movie',
        null,  // season (not used for movies)
        null,  // episode (not used for movies)
        false, // multi = false for single movie
        null,  // genre_ids (leave blank for movies)
        version
    );
}

function handleGetMissing() {
    if (!movieData) return;

    const btn = document.getElementById('btn-get-missing');
    btn.disabled = true;

    fetch('/library/move_missing_to_wanted', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            imdb_id: movieData.imdb_id,
            tmdb_id: movieData.tmdb_id
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            moviePopup({
                type: window.POPUP_TYPES.SUCCESS,
                message: data.message || `Moved ${data.updated_count} video(s) to Wanted state`,
                autoClose: 3000
            });
            // Reload movie data to reflect changes
            loadMovieData();
        } else {
            throw new Error(data.error || 'Failed to move videos to Wanted state');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: `Error moving videos to Wanted state: ${error.message}`,
            autoClose: 5000
        });
        btn.disabled = false;
    });
}

async function handleFilePacks() {
    if (!movieData) return;

    // Use the version from movieData metadata, or 'Default' if not available
    const version = movieData.version || 'Default';

    // Get the currently active file tab
    const activeTab = document.querySelector('.file-tab.active');
    const activeFile = activeTab ? parseInt(activeTab.dataset.file) : 1;

    // Call selectMedia from scraper.js with movie data for file pack
    await selectMedia(
        movieData.tmdb_id || movieData.imdb_id,
        movieData.title,
        movieData.year || '',
        'tv',
        activeFile, // currently selected file
        null, // video null for file pack
        true, // multi - file packs are multi-file
        null, // genre_ids
        version
    );
}

function handleRefreshTMDB() {
    if (!movieData) return;

    const btn = document.getElementById('btn-refresh-tmdb');
    const originalHTML = btn.innerHTML;

    // Movie loading state
    btn.disabled = true;
    btn.innerHTML = '<svg class="spin" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"></path></svg>';

    const mediaId = movieData.tmdb_id || movieData.imdb_id;

    fetch(`/library/refresh_metadata/${mediaId}`, {
        method: 'POST'
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Reload movie data to reflect changes
            loadMovieData();
        } else {
            throw new Error(data.error || 'Failed to refresh metadata');
        }
        // Reset button state
        btn.disabled = false;
        btn.innerHTML = originalHTML;
    })
    .catch(error => {
        console.error('Error:', error);
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
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
        console.warn('[Library Movie] Library settings modal not loaded, redirecting to settings page');
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
    // Movie-level delete button
    const deleteMovieBtn = document.getElementById('delete-movie-btn');
    if (deleteMovieBtn && movieData) {
        deleteMovieBtn.dataset.imdbId = movieData.imdb_id;
        deleteMovieBtn.addEventListener('click', handleDeleteMovie);
    }

    // Bulk file deletion button
    const deleteFilesBtn = document.querySelector('.delete-movie-file-btn');
    console.log('[Movie Detail] Delete files button found:', deleteFilesBtn);
    console.log('[Movie Detail] Files data:', filesData);
    if (deleteFilesBtn) {
        // Show/hide button based on file count
        if (filesData && filesData.length >= 2) {
            console.log('[Movie Detail] Showing delete files button - attaching event listener');
            deleteFilesBtn.style.display = 'inline-flex';
            deleteFilesBtn.addEventListener('click', handleDeleteMovieFiles);
        } else {
            console.log('[Movie Detail] Hiding delete files button - less than 2 files');
            deleteFilesBtn.style.display = 'none';
        }
    } else {
        console.error('[Movie Detail] Delete files button not found in DOM');
    }

    // Individual file delete buttons
    document.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', handleDeleteSingleFile);
    });
}

/**
 * Handle delete movie button click
 */
async function handleDeleteMovie(event) {
    const imdbId = event.currentTarget.dataset.imdbId;

    if (!imdbId) {
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: 'Cannot delete: No movie ID found',
            autoClose: 3000
        });
        return;
    }

    // Custom deletion with progress tracking
    const action = movieData && movieData.auto_ghostlist_enabled ? 'ghostlist' : 'delete';
    const canUndo = action === 'ghostlist' ? 'Ghostlisted items can be recovered.' : 'This action cannot be undone.';
    const confirmed = confirm(`This will ${action} ALL videos of "${movieData.title}". ${canUndo}`);
    if (!confirmed) {
        return;
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
    let movieedContinueButton = false;
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

    // Movie deletion progress loading box with cleanup callback
    movieDeletionLoading(`Deleting "${movieData.title}"`, cleanup);

    // Start progress interval
    progressInterval = setInterval(() => {
        if (currentStep < steps.length) {
            window.updateDeletionLoading(steps[currentStep], `Step ${currentStep + 1} of ${steps.length}`);
            currentStep++;

            // Stop at the last step (don't cycle)
            if (currentStep >= steps.length) {
                clearInterval(progressInterval);
                progressInterval = null;
            }
        }
    }, DELETION_PROGRESS_INTERVAL_MS);

    // After 10 seconds, movie continue in background button IN THE SAME LOADING BOX
    continueButtonTimeout = setTimeout(() => {
        if (!movieedContinueButton) {
            movieedContinueButton = true;
            // Update the SAME loading box to movie continue button
            window.updateDeletionLoading(
                'Deletion taking longer than expected...',
                'Processing',
                `This may be delayed due to API cooldown periods (up to ${API_COOLDOWN_MAX_SECONDS} seconds). You can continue in background - deletion will complete automatically.`,
                true  // Movie continue button in THIS loading box
            );
        }
    }, DELETION_TIMEOUT_WARNING_MS);

    try {

        const response = await fetch(`/library/delete_movie/${imdbId}`, {
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
            // Don't hide the loading box - update it to movie timeout message with continue button
            window.updateDeletionLoading(
                'Request Timed Out',
                'Still Processing',
                `The deletion request timed out (this can happen when waiting for API cooldown up to ${API_COOLDOWN_MAX_SECONDS} seconds), but the deletion is likely still running in the background. You can continue in background - deletion will complete automatically. Check logs or refresh library page later to verify completion.`,
                true  // Movie continue button
            );
            return;
        }

        // Handle non-JSON responses (like HTML error pages)
        const contentType = response.headers.get('content-type');
        if (!contentType || !contentType.includes('application/json')) {
            const text = await response.text();
            if (text.includes('504 Gateway Time-out') || text.includes('Gateway Timeout')) {
                cleanup();
                // Don't hide the loading box - update it to movie timeout message with continue button
                window.updateDeletionLoading(
                    'Request Timed Out',
                    'Still Processing',
                    `The deletion request timed out (this can happen when waiting for API cooldown up to ${API_COOLDOWN_MAX_SECONDS} seconds), but the deletion is likely still running in the background. You can continue in background - deletion will complete automatically. Check logs or refresh library page later to verify completion.`,
                    true  // Movie continue button
                );
                return;
            }
            cleanup();
            window.hideDeletionLoading();
            throw new Error('Unexpected response format from server');
        }

        const result = await response.json();

        // Now that we have a successful response, clean up the loading UI
        cleanup();
        window.hideDeletionLoading();

        if (result && result.success) {
                // Build deletion report using shared utility (pass 'movie' as media type)
                const reportMessage = window.buildDeletionReport(result, movieData.title, 'movie');

                moviePopup({
                    type: window.POPUP_TYPES.SUCCESS,
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
                throw new Error(result.error || 'Failed to delete movie');
            }
    } catch (error) {
        cleanup();
        window.hideDeletionLoading();
        console.error('Error deleting movie:', error);
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: `Error deleting movie: ${error.message}`,
            autoClose: 5000
        });
    }
}

/**
 * Handle bulk movie files deletion
 */
async function handleDeleteMovieFiles() {
    console.log('[Movie Detail] handleDeleteMovieFiles called');
    console.log('[Movie Detail] filesData:', filesData);
    console.log('[Movie Detail] movieData:', movieData);

    if (!filesData || filesData.length === 0) {
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: 'No files available to delete',
            autoClose: 3000
        });
        return;
    }

    console.log('[Movie Detail] Showing file selection popup');
    // Show file selection popup
    const selectedFileIds = await showMovieFileSelectionPopup(filesData, movieData.title);

    // User cancelled
    if (!selectedFileIds || selectedFileIds.length === 0) {
        return;
    }

    // Delete selected files
    await deleteMovieFilesByIds(selectedFileIds);
}

/**
 * Handle single file deletion
 */
async function handleDeleteSingleFile(event) {
    const fileId = parseInt(event.currentTarget.dataset.fileId);

    if (!fileId) {
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: 'Cannot delete: No file ID found',
            autoClose: 3000
        });
        return;
    }

    // Individual file deletion always uses delete (not ghostlist)
    const confirmed = confirm('Delete this file?\n\nThis action cannot be undone.');
    if (!confirmed) return;

    await deleteMovieFilesByIds([fileId]);
}

/**
 * Show file selection popup for bulk deletion
 */
function showMovieFileSelectionPopup(files, movieTitle) {
    return new Promise((resolve) => {
        // Create popup HTML
        const popupHtml = `
            <div class="file-selection-popup-overlay" id="movieFileSelectionPopup">
                <div class="file-selection-popup">
                    <h3>Select Files to Delete</h3>
                    <p class="file-selection-subtitle">${escapeHtml(movieTitle)}</p>
                    <div class="file-selection-list">
                        ${files.map((file, index) => `
                            <div class="file-selection-item">
                                <input type="checkbox"
                                       id="movie-file-${file.id}"
                                       value="${file.id}"
                                       ${files.length === 1 ? 'checked' : ''}>
                                <label for="movie-file-${file.id}">
                                    <span class="file-number">${index + 1}.</span>
                                    <span class="file-name">${escapeHtml(file.basename || file.filename || 'Unknown file')}</span>
                                    <span class="file-version">${escapeHtml(file.version || 'Default')}</span>
                                </label>
                            </div>
                        `).join('')}
                    </div>
                    <div class="file-selection-actions">
                        <button class="file-selection-btn file-selection-cancel">Cancel</button>
                        <button class="file-selection-btn file-selection-delete">Delete Selected</button>
                    </div>
                </div>
            </div>
        `;

        // Insert into body
        document.body.insertAdjacentHTML('beforeend', popupHtml);

        const popup = document.getElementById('movieFileSelectionPopup');
        const deleteBtn = popup.querySelector('.file-selection-delete');
        const cancelBtn = popup.querySelector('.file-selection-cancel');

        // Handle delete button click
        deleteBtn.addEventListener('click', () => {
            const checkboxes = popup.querySelectorAll('input[type="checkbox"]:checked');
            const selectedIds = Array.from(checkboxes).map(cb => parseInt(cb.value));

            if (selectedIds.length === 0) {
                moviePopup({
                    type: window.POPUP_TYPES.ERROR,
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
 * Delete movie files by their IDs
 */
async function deleteMovieFilesByIds(fileIds) {
    if (!fileIds || fileIds.length === 0) return;

    const fileCount = fileIds.length;
    const fileWord = fileCount === 1 ? 'file' : 'files';

    // Show loading indicator
    movieDeletionLoading(`Deleting ${fileCount} ${fileWord}...`, null);

    try {
        const response = await fetch(`/library/delete_movie_files`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                file_ids: fileIds,
                blacklist: false,
                // Note: No content source removal - movie still exists, just fewer files
                layers: ['database', 'media_server', 'filesystem', 'debrid', 'symlinks']
            })
        });

        window.hideDeletionLoading();

        if (!response.ok) {
            throw new Error(`Server returned ${response.status}`);
        }

        const result = await response.json();

        if (result && result.success) {
            // Build detailed deletion report using shared utility (pass 'movie' as media type)
            const reportMessage = window.buildDeletionReport(result, movieData.title, 'movie');

            moviePopup({
                type: window.POPUP_TYPES.SUCCESS,
                message: reportMessage,
                autoClose: false,  // Require manual close
                onConfirm: () => {
                    // Redirect to library page
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
            throw new Error(result.error || 'Failed to delete files');
        }
    } catch (error) {
        window.hideDeletionLoading();
        console.error('Error deleting movie files:', error);
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: `Error deleting files: ${error.message}`,
            autoClose: 5000
        });
    }
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Handle delete file button click
 */
async function handleDeleteFile(event) {
    const button = event.currentTarget;
    const fileNumber = parseInt(button.dataset.fileNumber);
    const imdbId = button.dataset.imdbId;

    if (!imdbId || isNaN(fileNumber)) {
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: 'Cannot delete: Invalid file information',
            autoClose: 3000
        });
        return;
    }

    const fileData = filesData.find(s => s.file_number === fileNumber);
    const videoCount = fileData ? fileData.videos.length : 0;

    try {
        if (window.DeletionCommon) {
            const result = await window.DeletionCommon.executeDelete([], {
                endpoint: `/library/delete_file/${imdbId}/${fileNumber}`,
                confirmTitle: `Delete File ${fileNumber}?`,
                confirmMessage: `This will delete all ${videoCount} videos in file ${fileNumber}. This action cannot be undone.`,
                layers: ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache']
            });

            if (result && result.success) {
                moviePopup({
                    type: window.POPUP_TYPES.SUCCESS,
                    message: `Successfully deleted ${result.deleted_count} videos`,
                    autoClose: 3000
                });

                // Reload movie data
                setTimeout(() => loadMovieData(), 1500);
            }
        } else {
            // Fallback
            if (!confirm(`Delete all ${videoCount} videos in file ${fileNumber}? This cannot be undone.`)) {
                return;
            }

            const response = await fetch(`/library/delete_file/${imdbId}/${fileNumber}`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    blacklist: false,
                    layers: ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache']
                })
            });

            const result = await response.json();

            if (result.success) {
                moviePopup({
                    type: window.POPUP_TYPES.SUCCESS,
                    message: result.message || 'File deleted successfully',
                    autoClose: 3000
                });
                setTimeout(() => loadMovieData(), 1500);
            } else {
                throw new Error(result.error || 'Failed to delete file');
            }
        }
    } catch (error) {
        console.error('Error deleting file:', error);
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: `Error deleting file: ${error.message}`,
            autoClose: 5000
        });
    }
}

/**
 * Handle delete video button click
 */
async function handleDeleteVideo(event) {
    const button = event.currentTarget;
    const itemId = parseInt(button.dataset.itemId);

    if (!itemId || isNaN(itemId)) {
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: 'Cannot delete: Invalid video ID',
            autoClose: 3000
        });
        return;
    }

    try {
        if (window.DeletionCommon) {
            const result = await window.DeletionCommon.executeDelete([itemId], {
                layers: ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache']
            });

            if (result && result.success) {
                moviePopup({
                    type: window.POPUP_TYPES.SUCCESS,
                    message: 'Video deleted successfully',
                    autoClose: 3000
                });

                // Reload movie data
                setTimeout(() => loadMovieData(), 1500);
            }
        } else {
            // Fallback
            const response = await fetch('/library/delete_items', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    item_ids: [itemId],
                    blacklist: false,
                    layers: ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache']
                })
            });

            const result = await response.json();

            if (result.success) {
                moviePopup({
                    type: window.POPUP_TYPES.SUCCESS,
                    message: 'Video deleted successfully',
                    autoClose: 3000
                });
                setTimeout(() => loadMovieData(), 1500);
            } else {
                throw new Error(result.error || 'Failed to delete video');
            }
        }
    } catch (error) {
        console.error('Error deleting video:', error);
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: `Error deleting video: ${error.message}`,
            autoClose: 5000
        });
    }
}
