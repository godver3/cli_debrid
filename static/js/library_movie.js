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
    if (window.DEBUG) console.log('[Movie Detail] Page loaded, initializing...');

    try {
        initializeElements();
        attachEventListeners();
        loadMovieData();
    } catch (error) {
        if (window.DEBUG) console.error('[Movie Detail] Initialization error:', error);
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
    const btnRequestMovie = document.getElementById('btn-request-movie');

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

    if (btnRequestMovie) {
        btnRequestMovie.addEventListener('click', handleRequestMovie);
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
            if (window.DEBUG) console.log(`[Movie Detail] Loaded ${brokenFiles.size} broken files`);
        }

        if (data.success) {
            movieData = data.movie;
            filesData = data.files;

            renderMovieHeader(movieData);
            renderFiles(filesData,movieData);

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

                // Load cast from TMDB if we have a tmdb_id
                if (movieData.tmdb_id) {
                    loadCast(movieData.tmdb_id);
                }
            } else {
                emptyState.style.display = 'flex';
            }
        } else {
            movieError(data.error || 'Failed to load movie details');
        }
    } catch (error) {
        if (window.DEBUG) console.error('[Movie Detail] Error loading data:', error);
        movieError('Failed to load movie details. Please try again.');
    }
}

function renderMovieHeader(movie) {
    if (window.DEBUG) console.log('[Movie Detail] Rendering header with data:', movie);
    if (window.DEBUG) console.log('[Movie Detail] Metadata values - overview:', movie.overview, 'genres:', movie.genres, 'network:', movie.network, 'status:', movie.status);

    // Set title with year in parentheses
    const titleText = movie.title + (movie.year ? ` (${movie.year})` : '');
    const titleEl = document.getElementById('movie-title');
    if (titleEl) {
        titleEl.textContent = titleText;
    } else {
        if (window.DEBUG) console.error('[Movie Detail] movie-title element not found');
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
    if (pathValue && movie.path) {
        pathValue.textContent = movie.path;
    }

    const addedValue = document.getElementById('added-value');
    if (addedValue && movie.collected_at) {
        addedValue.textContent = formatDate(movie.collected_at);
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
        releaseDateEl.textContent = movie.release_date ? formatDate(movie.release_date) : '-';
    }

    const runtimeEl = document.getElementById('movie-runtime');
    if (runtimeEl) {
        runtimeEl.textContent = movie.runtime ? `${movie.runtime} min` : '-';
    }

    const ratingEl = document.getElementById('movie-rating');
    if (ratingEl) {
        if (movie.rating) {
            const ratingValue = parseFloat(movie.rating).toFixed(1);
            const voteCount = movie.vote_count ? ` (${movie.vote_count.toLocaleString()} votes)` : '';
            ratingEl.textContent = `${ratingValue}/10${voteCount}`;
        } else {
            ratingEl.textContent = '-';
        }
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
    if (pathEl && movie.path) {
        pathEl.textContent = movie.path;
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

    if (window.DEBUG) console.log('[Movie Detail] Header rendering complete');
}

function renderFiles(files,movie) {
    const filesContent = document.getElementById('files-content');

    if (!filesContent) {
        if (window.DEBUG) console.error('[Movie Detail] files-content element not found');
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
        const fileRow = createFileRow(file, index + 1, movie);
        fileList.appendChild(fileRow);
    });

    filesContent.appendChild(fileList);
    if (window.DEBUG) console.log('[Movie Detail] Rendered', files.length, 'file(s)');
}

function createFileRow(file, rowNumber, movie) {
    const row = document.createElement('div');
    row.className = 'movie-file-row';
    // Hover effect now handled by CSS

    // Row number
    const number = document.createElement('div');
    number.className = 'movie-file-number'
    number.textContent = rowNumber;

    // Status indicator (green checkmark for Collected, red X for Blacklisted, blue clock for Unreleased, purple magnifying glass for Wanted)
    const status = document.createElement('div');
    status.className = 'movie-file-status-icon';
    const isCollected = file.state === 'Collected';
    const isUpgrading = file.state === 'Upgrading';
    const isBlacklisted = file.state === 'Blacklisted';
    const isUnreleased = file.state === 'Unreleased';
    const isWanted = file.state === 'Wanted';

    if (isCollected) {
        status.innerHTML = '<svg width="20" height="20" viewBox="0 0 20 20" fill="#4CAF50"><path fill-rule="evenodd" d="M1.875 10c0-4.487 3.638-8.125 8.125-8.125s8.125 3.638 8.125 8.125-3.638 8.125-8.125 8.125S1.875 14.487 1.875 10zm11.133-1.512a.625.625 0 10-1.016-.726l-2.697 3.775-1.42-1.42a.625.625 0 00-.884.883l1.875 1.875a.625.625 0 00.95-.078l3.125-4.375z" clip-rule="evenodd" /></svg>';
    } else if (isBlacklisted) {
        status.innerHTML = '<svg width="20" height="20" viewBox="0 0 20 20" fill="#ef4444"><path fill-rule="evenodd" d="M10 1.875c-4.487 0-8.125 3.638-8.125 8.125s3.638 8.125 8.125 8.125 8.125-3.638 8.125-8.125S14.487 1.875 10 1.875zm-1.433 5.808a.625.625 0 10-.884.884L9.117 10l-1.434 1.433a.625.625 0 10.884.884L10 10.883l1.433 1.434a.625.625 0 10.884-.884L10.883 10l1.434-1.433a.625.625 0 10-.884-.884L10 9.117l-1.433-1.434z" clip-rule="evenodd" /></svg>';
    } else if (isUpgrading) {
        status.innerHTML = '<svg width="20" height="20" viewBox="0 0 20 20" fill="#60a5fa"><path fill-rule="evenodd" d="M10 1.875c-4.487 0-8.125 3.638-8.125 8.125s3.638 8.125 8.125 8.125 8.125-3.638 8.125-8.125S14.487 1.875 10 1.875zm.442 4.558a.625.625 0 00-.884 0l-2.5 2.5a.625.625 0 10.884.884l1.433-1.434v5.159a.625.625 0 001.25 0V8.383l1.433 1.434a.625.625 0 10.884-.884l-2.5-2.5z" clip-rule="evenodd" /></svg>';
    } else if (isUnreleased) {
        status.innerHTML = '<svg width="20" height="20" viewBox="0 0 20 20" fill="#e0e0e0"><path fill-rule="evenodd" d="M10 1.875c-4.487 0-8.125 3.638-8.125 8.125s3.638 8.125 8.125 8.125 8.125-3.638 8.125-8.125S14.487 1.875 10 1.875zM10 6.875a.625.625 0 01.625.625v3.075l1.9 1.9a.625.625 0 11-.884.884l-2.083-2.083a.625.625 0 01-.183-.442V7.5a.625.625 0 01.625-.625z" clip-rule="evenodd" /></svg>';
    } else if (isWanted) {
        status.innerHTML = '<svg width="20" height="20" viewBox="0 0 20 20" fill="#fbbf24"><path fill-rule="evenodd" d="M10 1.875c-4.487 0-8.125 3.638-8.125 8.125s3.638 8.125 8.125 8.125 8.125-3.638 8.125-8.125S14.487 1.875 10 1.875zm3.567 6.017a.625.625 0 010 .884l-2.5 2.5a.625.625 0 01-.884-.884l1.434-1.434H7.5a.625.625 0 010-1.25h4.117l-1.434-1.434a.625.625 0 01.884-.884l2.5 2.5zm-7.134 4.633a.625.625 0 010-.884l2.5-2.5a.625.625 0 01.884.884l-1.434 1.434H12.5a.625.625 0 010 1.25H8.383l1.434 1.434a.625.625 0 01-.884.884l-2.5-2.5z" clip-rule="evenodd" /></svg>';
    } else {
        status.innerHTML = '<svg width="20" height="20" viewBox="0 0 20 20" fill="#666"><path fill-rule="evenodd" d="M10 1.875c-4.487 0-8.125 3.638-8.125 8.125s3.638 8.125 8.125 8.125 8.125-3.638 8.125-8.125S14.487 1.875 10 1.875zm-1.433 5.808a.625.625 0 10-.884.884L9.117 10l-1.434 1.433a.625.625 0 10.884.884L10 10.883l1.433 1.434a.625.625 0 10.884-.884L10.883 10l1.434-1.433a.625.625 0 10-.884-.884L10 9.117l-1.433-1.434z" clip-rule="evenodd" /></svg>';
    }

    // File info section
    const info = document.createElement('div');
    info.className = 'movie-file-info-section';

    const fileName = file.basename || file.filename || 'Unknown file';
    const titleText = movie.title + (movie.year ? ` (${movie.year})` : '');
    const qualityTags = extractQualityTags(file.basename || file.filename || '');
    const tags = qualityTags.map(tag => createQualityBadge(tag)).join('');
    const version = file.version || 'Default';

    // Determine status label and value
    let statusLabel, statusValue;
    if (isCollected && file.collected_at) {
        statusLabel = 'Collected';
        statusValue = formatDate(file.collected_at);
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
        <div class="release-title-wrapper">
            <div class="movie-file-title" data-clean-title="${titleText}" title="${fileName}">${titleText}</div>
            <div class="release-tags">${tags}</div>
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

    // Add move to wanted button (always visible)
    actions.appendChild(wantedBtn);

    // Individual file delete button - only for admins
    const hasAdminPermissions = document.getElementById('has_admin_permissions')?.value === 'True';
    if (hasAdminPermissions) {
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

        actions.appendChild(deleteBtn);
    }

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
        if (window.DEBUG) console.error('Error:', error);
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: `Error moving item to Wanted state: ${error.message}`,
            autoClose: 5000
        });
    });
}

function movieError(message) {
    if (window.DEBUG) console.error('[Movie Detail] Error:', message);
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

// Add resize listener for responsive sidebar margin adjustment
// Handles orientation changes and window resizing without page refresh
let movieSidebarResizeTimeout;
window.addEventListener('resize', function() {
    clearTimeout(movieSidebarResizeTimeout);
    movieSidebarResizeTimeout = setTimeout(alignSidebarWithFiles, 150);
});

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

async function handleSearchMovie() {
    if (!movieData) return;

    const version = movieData.version || 'Default';

    // Call selectMedia to search for this movie
    // Convert genres string to array if needed for auto-select
    const genres = movieData.genres ?
        (typeof movieData.genres === 'string' ? movieData.genres.split(',').map(g => g.trim()) : movieData.genres)
        : [];

    await selectMedia(
        movieData.tmdb_id || movieData.id,
        movieData.title,
        movieData.year || '',
        'movie',
        null,  // season (not used for movies)
        null,  // episode (not used for movies)
        false, // multi = false for single movie
        genres,  // genre_ids - pass genres for auto-select
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
        if (window.DEBUG) console.error('Error:', error);
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
    // Convert genres string to array if needed for auto-select
    const genres = movieData.genres ?
        (typeof movieData.genres === 'string' ? movieData.genres.split(',').map(g => g.trim()) : movieData.genres)
        : [];

    await selectMedia(
        movieData.tmdb_id || movieData.imdb_id,
        movieData.title,
        movieData.year || '',
        'tv',
        activeFile, // currently selected file
        null, // video null for file pack
        true, // multi - file packs are multi-file
        genres, // genre_ids - pass genres for auto-select
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

    fetch(`/library/refresh_metadata/movie/${mediaId}`, {
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
        if (window.DEBUG) console.error('Error:', error);
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
        if (window.DEBUG) console.warn('[Library Movie] Library settings modal not loaded, redirecting to settings page');
        window.location.href = '/settings#library-manager';
    }
}

function handleRequestMovie() {
    if (window.DEBUG) console.log('[Movie Request] Opening version modal for movie:', movieData);

    if (!movieData || !movieData.tmdb_id) {
        if (window.DEBUG) console.error('[Movie Request] No movie data or TMDB ID available');
        showPopup({
            message: 'Movie data not available',
            type: POPUP_TYPES.ERROR
        });
        return;
    }

    // Get available versions from the version select
    const versionSelect = document.getElementById('version-select');
    if (!versionSelect) {
        if (window.DEBUG) console.error('[Movie Request] Version select not found');
        showPopup({
            message: 'Version configuration not available',
            type: POPUP_TYPES.ERROR
        });
        return;
    }

    const availableVersions = Array.from(versionSelect.options)
        .map(opt => opt.value)
        .filter(v => v !== 'No Version');

    // Show version modal
    showVersionModal(availableVersions);
}

function showVersionModal(versions) {
    const modal = document.getElementById('versionModal');
    const versionCheckboxes = document.getElementById('versionCheckboxes');

    if (!modal || !versionCheckboxes) {
        if (window.DEBUG) console.error('[Movie Request] Modal elements not found');
        return;
    }

    // Clear existing checkboxes
    versionCheckboxes.innerHTML = '';

    // Add heading
    const header = document.createElement('div');
    header.className = 'version-section-header';
    header.innerHTML = `<h4>Requesting: ${movieData.title}${movieData.year ? ` (${movieData.year})` : ''}</h4>`;
    versionCheckboxes.appendChild(header);

    const separator = document.createElement('hr');
    versionCheckboxes.appendChild(separator);

    const versionHeader = document.createElement('div');
    versionHeader.className = 'version-section-header';
    versionHeader.innerHTML = '<h4>Select Versions:</h4>';
    versionCheckboxes.appendChild(versionHeader);

    // Create checkboxes for each version
    versions.forEach(version => {
        const div = document.createElement('div');
        div.className = 'version-checkbox';
        div.innerHTML = `
            <input type="checkbox" id="version-${version}" name="versions" value="${version}">
            <label for="version-${version}">${version}</label>
        `;
        versionCheckboxes.appendChild(div);

        // Auto-select if only one version
        if (versions.length === 1) {
            div.querySelector('input[type="checkbox"]').checked = true;
        }
    });

    // Show modal
    modal.style.display = 'flex';

    // Attach event listeners
    const confirmBtn = document.getElementById('confirmVersions');
    const cancelBtn = document.getElementById('cancelVersions');

    const handleConfirm = async () => {
        const selectedVersions = Array.from(document.querySelectorAll('#versionCheckboxes input[type="checkbox"]:checked'))
            .map(cb => cb.value);

        if (selectedVersions.length === 0) {
            showPopup({
                message: 'Please select at least one version',
                type: POPUP_TYPES.ERROR
            });
            return;
        }

        await submitRequest(selectedVersions);
        modal.style.display = 'none';

        // Remove event listeners
        confirmBtn.removeEventListener('click', handleConfirm);
        cancelBtn.removeEventListener('click', handleCancel);
    };

    const handleCancel = () => {
        modal.style.display = 'none';
        confirmBtn.removeEventListener('click', handleConfirm);
        cancelBtn.removeEventListener('click', handleCancel);
    };

    confirmBtn.addEventListener('click', handleConfirm);
    cancelBtn.addEventListener('click', handleCancel);

    // Close on Escape key
    const handleEscape = (e) => {
        if (e.key === 'Escape' && modal.style.display === 'flex') {
            handleCancel();
            document.removeEventListener('keydown', handleEscape);
        }
    };
    document.addEventListener('keydown', handleEscape);
}

async function submitRequest(selectedVersions) {
    if (window.DEBUG) console.log('[Movie Request] Submitting request with versions:', selectedVersions);

    try {
        Loading.show('Requesting movie...');

        const response = await fetch('/content/request', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                id: movieData.tmdb_id,
                mediaType: 'movie',
                title: movieData.title,
                versions: selectedVersions
            })
        });

        const data = await response.json();

        if (data.success || response.ok) {
            showPopup({ message: `Successfully requested: ${movieData.title}`, type: POPUP_TYPES.SUCCESS });
        } else {
            throw new Error(data.error || 'Request failed');
        }
    } catch (error) {
        if (window.DEBUG) console.error('[Movie Request] Error:', error);
        showPopup({ message: `Failed to request movie: ${error.message}`, type: POPUP_TYPES.ERROR });
    } finally {
        Loading.hide();
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

    // Bulk file deletion button - only exists for admin users
    const hasAdminPermissions = document.getElementById('has_admin_permissions')?.value === 'True';
    if (hasAdminPermissions) {
        const deleteFilesBtn = document.querySelector('.delete-movie-file-btn');
        if (deleteFilesBtn) {
            // Show/hide button based on file count
            if (filesData && filesData.length >= 2) {
                deleteFilesBtn.style.display = 'inline-flex';
                deleteFilesBtn.addEventListener('click', handleDeleteMovieFiles);
            } else {
                deleteFilesBtn.style.display = 'none';
            }
        }
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
        if (window.DEBUG) console.error('Error deleting movie:', error);
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
    if (window.DEBUG) console.log('[Movie Detail] handleDeleteMovieFiles called');
    if (window.DEBUG) console.log('[Movie Detail] filesData:', filesData);
    if (window.DEBUG) console.log('[Movie Detail] movieData:', movieData);

    if (!filesData || filesData.length === 0) {
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: 'No files available to delete',
            autoClose: 3000
        });
        return;
    }

    if (window.DEBUG) console.log('[Movie Detail] Showing file selection popup');
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

            // Check if there are remaining files
            const deletedCount = result.deleted_count || 0;
            const remainingFiles = filesData.length - deletedCount;
            const shouldRedirect = remainingFiles === 0;

            moviePopup({
                type: window.POPUP_TYPES.SUCCESS,
                message: reportMessage,
                autoClose: false,  // Require manual close
                onConfirm: () => {
                    if (shouldRedirect) {
                        // All files deleted - go back to library
                        window.location.href = '/library';
                    } else {
                        // Some files remain - reload page to show updated list
                        window.location.reload();
                    }
                }
            });

            // Add close button callback
            setTimeout(() => {
                const closeButton = document.querySelector('.universal-popup #popupClose');
                if (closeButton) {
                    closeButton.onclick = () => {
                        if (shouldRedirect) {
                            window.location.href = '/library';
                        } else {
                            window.location.reload();
                        }
                    };
                }
            }, 100);
        } else {
            throw new Error(result.error || 'Failed to delete files');
        }
    } catch (error) {
        window.hideDeletionLoading();
        if (window.DEBUG) console.error('Error deleting movie files:', error);
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
        if (window.DEBUG) console.error('Error deleting file:', error);
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
        if (window.DEBUG) console.error('Error deleting video:', error);
        moviePopup({
            type: window.POPUP_TYPES.ERROR,
            message: `Error deleting video: ${error.message}`,
            autoClose: 5000
        });
    }
}

// =============================================================================
// Cast Section
// =============================================================================

/**
 * Load cast and director data from TMDB API
 */
async function loadCast(tmdbId) {
    try {
        const response = await fetch(`/library/cast/movie/${tmdbId}`);
        const data = await response.json();

        if (data.success) {
            // Display cast if available
            if (data.cast && data.cast.length > 0) {
                displayCast(data.cast);
            }

            // Display director if available
            if (data.directors && data.directors.length > 0) {
                const directorEl = document.getElementById('movie-director');
                if (directorEl) {
                    directorEl.textContent = data.directors.join(', ');
                }
            }
        }
    } catch (error) {
        if (window.DEBUG) console.error('[Movie Detail] Error loading cast:', error);
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
