/**
 * Library - Fast infinite scroll media browser
 * Optimized for performance with lazy loading and batch rendering
 */

/**
 * Format file size for display
 * @param {number} sizeInGB - Size in gigabytes
 * @returns {string} Formatted size string
 */
function formatSize(sizeInGB) {
    if (sizeInGB === null || sizeInGB === undefined) return '-';
    if (sizeInGB === 0) return '0 GB';
    return `${sizeInGB.toFixed(2)} GB`;
}

// State management - attach to window for global access (prevents scoping issues)
window.libraryState = {
    offset: 0,
    limit: 50,
    isLoading: false,
    hasMore: true,
    searchTerm: '',
    statusFilter: 'collected',
    typeFilter: 'all',
    sortBy: 'title_asc',
    totalCount: 0,
    currentView: localStorage.getItem('libraryView') || 'grid', // Remember last view
    autoGhostlistEnabled: false // Will be fetched from server
};

// DOM elements
let mediaGrid, loadingIndicator, emptyState, resultsInfo;
let searchInput, statusFilter, typeFilter, sortSelect, refreshBtn, searchClearBtn;

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    console.log('[Library] Page loaded, initializing...');
    try {
        // Check if either Plex or TMDB is configured
        const container = document.querySelector('.library-container');
        const plexConfigured = container.dataset.plexConfigured === 'true';
        const tmdbConfigured = container.dataset.tmdbConfigured === 'true';

        if (!plexConfigured && !tmdbConfigured) {
            console.warn('[Library] Neither Plex nor TMDB is configured. Skipping data load.');
            return; // Don't initialize if no artwork source is configured
        }

        initializeElements();
        attachEventListeners();
        fetchAutoGhostlistSetting(); // Fetch the setting before loading data
        loadInitialData();
    } catch (error) {
        console.error('[Library] Initialization error:', error);
    }
});

function initializeElements() {
    mediaGrid = document.getElementById('media-grid');
    loadingIndicator = document.getElementById('loading-indicator');
    emptyState = document.getElementById('empty-state');
    resultsInfo = document.getElementById('results-info');

    searchInput = document.getElementById('search-input');
    statusFilter = document.getElementById('status-filter');
    typeFilter = document.getElementById('type-filter');
    sortSelect = document.getElementById('sort-select');
    refreshBtn = document.getElementById('refresh-btn');
    searchClearBtn = document.getElementById('search-clear-btn');

    // Load filter values from localStorage if available
    const savedFilters = {
        statusFilter: localStorage.getItem('libraryStatusFilter'),
        typeFilter: localStorage.getItem('libraryTypeFilter'),
        sortBy: localStorage.getItem('librarySortBy'),
        searchTerm: localStorage.getItem('librarySearchTerm')
    };

    // Apply saved values to DOM elements and state
    if (savedFilters.statusFilter) {
        statusFilter.value = savedFilters.statusFilter;
        libraryState.statusFilter = savedFilters.statusFilter;
    } else {
        libraryState.statusFilter = statusFilter.value;
    }

    if (savedFilters.typeFilter) {
        typeFilter.value = savedFilters.typeFilter;
        libraryState.typeFilter = savedFilters.typeFilter;
    } else {
        libraryState.typeFilter = typeFilter.value;
    }

    // Update sort options based on status filter (before restoring sort value)
    const isUpcoming = libraryState.statusFilter === 'upcoming';
    updateSortOptions(isUpcoming);

    if (savedFilters.sortBy) {
        sortSelect.value = savedFilters.sortBy;
        libraryState.sortBy = savedFilters.sortBy;
    } else {
        libraryState.sortBy = sortSelect.value;
    }

    if (savedFilters.searchTerm) {
        searchInput.value = savedFilters.searchTerm;
        libraryState.searchTerm = savedFilters.searchTerm;
        // Show clear button if there's a saved search term
        if (searchClearBtn && savedFilters.searchTerm) {
            searchClearBtn.style.display = 'flex';
        }
    } else {
        libraryState.searchTerm = searchInput.value.trim();
    }

    // Initialize view from localStorage
    initializeView();
}

function fetchAutoGhostlistSetting() {
    // Fetch the auto-ghostlist setting from server
    fetch('/settings/api/config')
        .then(response => response.json())
        .then(config => {
            libraryState.autoGhostlistEnabled = config['Library Manager']?.['ghostlist_mode'] || false;
            console.log('[Library] Auto-ghostlist setting:', libraryState.autoGhostlistEnabled);
        })
        .catch(error => {
            console.error('[Library] Failed to fetch auto-ghostlist setting:', error);
            libraryState.autoGhostlistEnabled = false; // Default to false on error
        });
}

function attachEventListeners() {
    // Infinite scroll
    window.addEventListener('scroll', handleScroll);

    // Search with debounce
    searchInput.addEventListener('input', debounce(handleSearch, 300));

    // Search input - show/hide clear button
    searchInput.addEventListener('input', function() {
        if (searchClearBtn) {
            searchClearBtn.style.display = searchInput.value.trim() ? 'flex' : 'none';
        }
    });

    // Search clear button
    if (searchClearBtn) {
        searchClearBtn.addEventListener('click', function() {
            searchInput.value = '';
            searchClearBtn.style.display = 'none';
            libraryState.searchTerm = '';
            localStorage.setItem('librarySearchTerm', '');
            resetAndReload();
        });
    }

    // Filters
    statusFilter.addEventListener('change', handleStatusFilterChange);
    typeFilter.addEventListener('change', handleFilterChange);
    sortSelect.addEventListener('change', handleFilterChange);

    // Refresh button
    refreshBtn.addEventListener('click', handleRefresh);

    // Settings button
    const settingsBtn = document.getElementById('settings-btn');
    if (settingsBtn) {
        settingsBtn.addEventListener('click', handleSettings);
    }

    // View toggle
    const viewButtons = document.querySelectorAll('[data-view]');
    viewButtons.forEach(btn => {
        btn.addEventListener('click', () => handleViewToggle(btn));
    });
}

function handleScroll() {
    // Check if user is near the bottom (500px threshold for smooth loading)
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 500) {
        fetchItems();
    }
}

function handleSearch() {
    const newSearchTerm = searchInput.value.trim();
    if (newSearchTerm !== libraryState.searchTerm) {
        libraryState.searchTerm = newSearchTerm;

        // Save search term to localStorage
        localStorage.setItem('librarySearchTerm', libraryState.searchTerm);

        resetAndReload();
    }
}

function handleStatusFilterChange() {
    const isUpcoming = statusFilter.value === 'upcoming';
    updateSortOptions(isUpcoming);
    handleFilterChange();
}

function handleFilterChange() {
    libraryState.statusFilter = statusFilter.value;
    libraryState.typeFilter = typeFilter.value;
    libraryState.sortBy = sortSelect.value;

    // Save filter values to localStorage
    localStorage.setItem('libraryStatusFilter', libraryState.statusFilter);
    localStorage.setItem('libraryTypeFilter', libraryState.typeFilter);
    localStorage.setItem('librarySortBy', libraryState.sortBy);

    resetAndReload();
}

function updateSortOptions(isUpcoming) {
    const currentSort = sortSelect.value;

    // Store default sort options
    const defaultOptions = [
        { value: 'title_asc', text: 'Title (A-Z)' },
        { value: 'title_desc', text: 'Title (Z-A)' },
        { value: 'year_desc', text: 'Year (Newest)' },
        { value: 'year_asc', text: 'Year (Oldest)' },
        { value: 'added_desc', text: 'Added (Newest)' },
        { value: 'added_asc', text: 'Added (Oldest)' }
    ];

    // Additional sort options for Upcoming filter
    const releaseOptions = [
        { value: 'release_asc', text: 'Release (Soonest)' },
        { value: 'release_desc', text: 'Release (Furthest)' }
    ];

    // Clear current options
    sortSelect.innerHTML = '';

    // Add appropriate options
    let options;
    if (isUpcoming) {
        // For Upcoming: Add Release options first, then default options
        options = [...releaseOptions, ...defaultOptions];
    } else {
        // For other filters: Only default options
        options = defaultOptions;
    }

    options.forEach(opt => {
        const option = document.createElement('option');
        option.value = opt.value;
        option.textContent = opt.text;
        sortSelect.appendChild(option);
    });

    // Try to maintain the current selection if it exists in new options
    const availableValues = options.map(o => o.value);
    if (availableValues.includes(currentSort)) {
        sortSelect.value = currentSort;
    } else {
        // Default to first option
        sortSelect.value = options[0].value;
    }
}

function handleRefresh() {
    resetAndReload();
}

function handleSettings() {
    if (typeof window.openLibrarySettingsModal === 'function') {
        window.openLibrarySettingsModal();
    } else {
        console.warn('[Library] Library settings modal not loaded, redirecting to settings page');
        window.location.href = '/settings#library-manager';
    }
}

function initializeView() {
    const view = libraryState.currentView;

    // Update button states
    document.querySelectorAll('[data-view]').forEach(btn => {
        if (btn.dataset.view === view) {
            btn.classList.add('action-btn-primary', 'active');
        } else {
            btn.classList.remove('action-btn-primary', 'active');
        }
    });

    // Update grid class
    if (view === 'list') {
        mediaGrid.classList.add('list-view');
        addListViewHeader();
    } else {
        mediaGrid.classList.remove('list-view');
        removeListViewHeader();
    }
}

function addListViewHeader() {
    // Remove existing header if present
    removeListViewHeader();

    // Create header row
    const header = document.createElement('div');
    header.className = 'list-header';
    header.id = 'list-view-header';
    header.innerHTML = `
        <div class="list-col list-col-badge"></div>
        <div class="list-col list-col-poster"></div>
        <div class="list-col list-col-title">Title</div>
        <div class="list-col list-col-year">Year</div>
        <div class="list-col list-col-status">Status</div>
        <div class="list-col list-col-quality">Version</div>
        <div class="list-col list-col-state">State</div>
        <div class="list-col list-col-size">Size</div>
    `;

    // Insert before the media-grid
    mediaGrid.parentNode.insertBefore(header, mediaGrid);
}

function removeListViewHeader() {
    const existingHeader = document.getElementById('list-view-header');
    if (existingHeader) {
        existingHeader.remove();
    }
}

function handleViewToggle(button) {
    const view = button.dataset.view;

    // Don't reload if already in this view
    if (libraryState.currentView === view) {
        return;
    }

    // Save to localStorage
    localStorage.setItem('libraryView', view);
    libraryState.currentView = view;

    // Update button states
    document.querySelectorAll('[data-view]').forEach(btn => {
        if (btn.dataset.view === view) {
            btn.classList.add('action-btn-primary', 'active');
        } else {
            btn.classList.remove('action-btn-primary', 'active');
        }
    });

    // Update grid class and header
    if (view === 'list') {
        mediaGrid.classList.add('list-view');
        addListViewHeader();
    } else {
        mediaGrid.classList.remove('list-view');
        removeListViewHeader();
    }

    // Re-render all items with new view
    resetAndReload();
}

function resetAndReload() {
    libraryState.offset = 0;
    libraryState.hasMore = true;
    libraryState.isLoading = false;
    libraryState.totalCount = 0;
    mediaGrid.innerHTML = '';
    emptyState.style.display = 'none';
    fetchItems(true);
}

function loadInitialData() {
    fetchItems(true);
}

async function fetchItems(isReset = false) {
    // Prevent multiple simultaneous requests
    if (libraryState.isLoading || (!libraryState.hasMore && !isReset)) {
        return;
    }

    libraryState.isLoading = true;

    // Show loading indicator
    if (isReset) {
        loadingIndicator.style.display = 'flex';
        loadingIndicator.textContent = 'Loading...';
    } else {
        loadingIndicator.style.display = 'flex';
    }

    // Build URL with query parameters
    const params = new URLSearchParams({
        limit: libraryState.limit,
        offset: libraryState.offset,
        search: libraryState.searchTerm,
        status: libraryState.statusFilter,
        media_type: libraryState.typeFilter,
        sort: libraryState.sortBy
    });

    const url = `/library/data?${params.toString()}`;
    console.log('[Library] Fetching from:', url);

    try {
        const response = await fetch(url);
        console.log('[Library] Response status:', response.status);

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        console.log('[Library] Received data:', data);

        if (data.success) {
            // Update state
            libraryState.hasMore = data.has_more;
            libraryState.totalCount = data.total;

            // Render items
            if (data.items && data.items.length > 0) {
                // Use DocumentFragment for efficient DOM manipulation
                const fragment = document.createDocumentFragment();

                data.items.forEach(item => {
                    const card = createMediaCard(item);
                    fragment.appendChild(card);
                });

                mediaGrid.appendChild(fragment);

                // Update offset for next batch
                libraryState.offset += data.items.length;

                // Hide empty state
                emptyState.style.display = 'none';
            } else if (libraryState.offset === 0) {
                // No items at all
                emptyState.style.display = 'flex';
            }

            // Update results count
            updateResultsInfo();
        } else {
            console.error('API returned error:', data.error);
            showError(data.error || 'Failed to load items');
        }
    } catch (error) {
        console.error('Error fetching library items:', error);
        showError('Network error. Please try again.');
    } finally {
        libraryState.isLoading = false;
        loadingIndicator.style.display = 'none';
    }
}

function createMediaCard(item) {
    // Check current view mode
    const isListView = libraryState.currentView === 'list';

    if (isListView) {
        return createListItem(item);
    } else {
        return createGridCard(item);
    }
}

function createListItem(item) {
    const row = document.createElement('div');
    row.className = 'list-row';
    row.dataset.id = item.id;
    row.dataset.imdbId = item.imdb_id || '';
    row.dataset.type = item.type || '';
    row.dataset.title = item.title || '';
    row.dataset.state = item.state || '';
    row.dataset.ghostlisted = item.ghostlisted || '';


    // Build poster URL
    let posterUrl = '/static/images/placeholder.png';
    if (item.poster_path && item.poster_path.startsWith('plex:')) {
        const plexPath = item.poster_path.substring(5);
        posterUrl = `/library/plex_image${plexPath}`;
    } else if (item.poster_path && item.poster_path.startsWith('/')) {
        posterUrl = `/scraper/tmdb_image/w92${item.poster_path}`; // Smaller size for list view
    } else if (item.poster_path) {
        posterUrl = item.poster_path;
    }

    // Type badge
    const typeClass = item.type === 'show' ? 'type-tv' : 'type-movie';
    const typeLabel = `<span class="type-badge ${typeClass}">${item.type === 'show' ? 'TV' : 'Movie'}</span>`;

    // Episode info badge (top-left) for shows
    let episodeBadge = '';
    if (item.type === 'show' && item.episode_info) {
        const { collected, total } = item.episode_info;
        episodeBadge = `<span class="episode-badge" title="${collected} of ${total} episodes">${collected}/${total}</span>`;
    }

    // Calculate progress percentage
    let progressPercent = 0;
    if (item.type === 'show' && item.episode_info && item.episode_info.total > 0) {
        //progressPercent = Math.round((item.episode_info.collected / item.episode_info.total) * 100);
        if (episodeBadge) {
            progressPercent = episodeBadge
        }else {
            progressPercent = Math.round((item.episode_info.collected / item.episode_info.total) * 100)+'%';
        }
    } else if (item.status === 'collected') {
        //progressPercent = 100 +'%';
        progressPercent = '';
    }
    // Status badge with circle icon
    let statusBadge = '';
    let statusClass = '';
    if (item.status_label === 'Missing') {
        statusClass = 'status-blacklisted';
        statusBadge = `<span class="status-badge ${statusClass}" title="Blacklisted">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="4.93" y1="4.93" x2="19.07" y2="19.07"></line>
            </svg>
        </span>`;
    } else if (item.status_label === 'Collected') {
        statusClass = 'status-collected';
        statusBadge = `<span class="status-badge ${statusClass}" title="Collected">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <circle cx="12" cy="12" r="10"></circle>
            </svg>
        </span>`;
    }

    // Quality badge (placeholder - you can add actual quality data if available)
    const qualityBadge = item.quality || '—';

    // File size
    const fileSize = item.file_size ? formatFileSize(item.file_size) : '0 B';

    row.innerHTML = `
        <div class="list-col list-col-badge">
            ${statusBadge}
        </div>
        <div class="list-col list-col-poster">
            <img src="${posterUrl}"
                 alt="${escapeHtml(item.title)}"
                 loading="lazy"
                 onerror="this.onerror=null; this.src='/static/images/placeholder.png';">
        </div>
        <div class="list-col list-col-title">
            <div class="list-title-wrapper">
                <span class="list-title">${escapeHtml(item.title)}</span>
                ${typeLabel}
            </div>
        </div>
        <div class="list-col list-col-year">${item.year || 'N/A'}</div>
        <div class="list-col list-col-status">${progressPercent}</div>
        <div class="list-col list-col-quality">${item.version || 'N/A'}</div>
        <div class="list-col list-col-state">${item.state}</div>
        <div class="list-col list-col-size">${formatSize(item.size)}</div>
    `;

    // Add click handler
    row.addEventListener('click', () => handleCardClick(item));

    return row;
}

function createGridCard(item) {
    const card = document.createElement('div');
    card.className = 'media-card';
    card.dataset.id = item.id;
    card.dataset.imdbId = item.imdb_id || '';
    card.dataset.type = item.type || '';
    card.dataset.title = item.title || '';
    card.dataset.state = item.state || '';
    card.dataset.ghostlisted = item.ghostlisted || '';

    // Build poster URL - handle both TMDB and Plex images
    let posterUrl = '/static/images/placeholder.png';
    let needsFetch = false;
    let fetchUseImdb = false;  // Flag to use IMDB fallback

    if (item.poster_path && item.poster_path.startsWith('plex:')) {
        // Plex image - use Plex proxy endpoint
        const plexPath = item.poster_path.substring(5); // Remove 'plex:' prefix
        posterUrl = `/library/plex_image${plexPath}`;
    } else if (item.poster_path && item.poster_path.startsWith('/')) {
        // TMDB image - use TMDB proxy endpoint
        posterUrl = `/scraper/tmdb_image/w300${item.poster_path}`;
    } else if (item.poster_path) {
        // Full URL or other format - use as-is
        posterUrl = item.poster_path;
    } else if (item.tmdb_id) {
        // No poster cached - fetch from TMDB on-demand using TMDB ID
        needsFetch = true;
    } else if (item.imdb_id) {
        // No TMDB ID - try to fetch using IMDB ID
        needsFetch = true;
        fetchUseImdb = true;
    }

    // Type badge (top-right)
    const typeClass = item.type === 'show' ? 'badge-tv' : 'badge-movie';
    const typeBadge = `<span class="type-badge ${typeClass}">${item.type === 'show' ? 'TV' : 'Movie'}</span>`;

    // Blacklisted badge (below type badge) - using provided SVG
    let blacklistedBadge = '';
    if (item.state === 'Blacklisted') {
        blacklistedBadge = `<span class="type-badge badge-blacklisted" title="Blacklisted">
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align: middle;">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="4.93" y1="4.93" x2="19.07" y2="19.07"></line>
            </svg>
        </span>`;
    }

    // Episode info badge (top-left) for shows
    let episodeBadge = '';
    if (item.type === 'show' && item.episode_info) {
        const { collected, total } = item.episode_info;
        episodeBadge = `<span class="episode-badge" title="${collected} of ${total} episodes">${collected}/${total}</span>`;
    }

    // Calculate progress percentage
    let progressPercent = 0;
    if (item.type === 'show' && item.episode_info && item.episode_info.total > 0) {
        progressPercent = Math.round((item.episode_info.collected / item.episode_info.total) * 100);
    } else if (item.status === 'collected') {
        progressPercent = 100;
    }

    // Format progress/release info for hover overlay
    let progressInfo = '';
    if (libraryState.statusFilter === 'upcoming') {
        // For upcoming items, show release date instead of progress
        if (item.release_date && item.release_date !== 'Unknown') {
            // Format the date nicely (e.g., "Feb 15, 2026")
            const releaseDate = new Date(item.release_date);
            const options = { year: 'numeric', month: 'short', day: 'numeric' };
            progressInfo = releaseDate.toLocaleDateString('en-US', options);
        } else {
            progressInfo = 'Release date TBA';
        }
    } else {
        // For all other items, show progress percentage
        progressInfo = `${progressPercent}% complete`;
    }

    // Build card HTML (Cinephage style)
    card.innerHTML = `
        <div class="media-poster">
            <img src="${posterUrl}"
                 alt="${escapeHtml(item.title)}"
                 loading="lazy"
                 onerror="this.onerror=null; this.src='/static/images/placeholder.png'; this.classList.add('placeholder');">

            <!-- Top-right badge (Type) -->
            <div class="badge-top-right">
                ${typeBadge}
                ${blacklistedBadge}
            </div>

            <!-- Top-left badge (Episode count) -->
            ${episodeBadge ? `<div class="badge-top-left">${episodeBadge}</div>` : ''}

            <!-- Progress bar at bottom -->
            <div class="progress-bar-container">
                <div class="progress-bar" style="width: ${progressPercent}%"></div>
            </div>

            <!-- Hover overlay with info -->
            <div class="hover-overlay">
                <div class="hover-content">
                    <h3 class="hover-title">${escapeHtml(item.title)}</h3>
                    <div class="hover-meta">
                        <span class="hover-year">${item.year || 'N/A'}</span>
                        <span class="hover-progress">${progressInfo}</span>
                    </div>

                </div>
            </div>
        </div>
    `;

    // Add click handler for card (navigate to details)
    card.addEventListener('click', () => handleCardClick(item));

    // Add action button handlers (prevent event bubbling to card click)
    const actionButtons = card.querySelectorAll('.action-icon');
    actionButtons.forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation(); // Prevent card click
            handleActionClick(btn.dataset.action, item);
        });
    });

    // Fetch missing poster asynchronously
    if (needsFetch) {
        if (fetchUseImdb) {
            // Use IMDB ID to look up poster (TMDB ID not available)
            fetchMissingPosterByImdb(item.imdb_id, item.type, card);
        } else {
            // Use TMDB ID directly
            fetchMissingPoster(item.tmdb_id, item.type, card);
        }
    }

    return card;
}

// Helper function to format file size
function formatFileSize(bytes) {
    if (!bytes || bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const k = 1024;
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + units[i];
}

// Fetch missing poster from TMDB on-demand using TMDB ID
async function fetchMissingPoster(tmdbId, mediaType, card) {
    try {
        const response = await fetch(`/library/fetch_poster/${tmdbId}/${mediaType}`);
        const data = await response.json();

        if (data.success && data.poster_path) {
            // Update the poster image
            const img = card.querySelector('.media-poster');
            if (img) {
                img.src = `/scraper/tmdb_image/w300${data.poster_path}`;
                img.classList.remove('placeholder');
            }
        }
    } catch (error) {
        console.debug(`Could not fetch poster for ${tmdbId}:`, error);
        // Silent fail - placeholder already shown
    }
}

// Fetch missing poster from TMDB on-demand using IMDB ID (fallback when no TMDB ID)
async function fetchMissingPosterByImdb(imdbId, mediaType, card) {
    try {
        const response = await fetch(`/library/fetch_poster_imdb/${imdbId}/${mediaType}`);
        const data = await response.json();

        if (data.success && data.poster_path) {
            // Update the poster image
            const img = card.querySelector('.media-poster');
            if (img) {
                img.src = `/scraper/tmdb_image/w300${data.poster_path}`;
                img.classList.remove('placeholder');
            }
        }
    } catch (error) {
        console.debug(`Could not fetch poster for IMDB ${imdbId}:`, error);
        // Silent fail - placeholder already shown
    }
}

function handleCardClick(item) {
    // Don't navigate if in selection mode
    if (selectionState.isSelectionMode) {
        return;
    }

    // Navigate to show detail page for TV shows (prefer tmdb_id, fallback to imdb_id)
    if (item.type === 'show') {
        const mediaId = item.tmdb_id || item.imdb_id;
        if (mediaId) {
            window.location.href = `/library/show/${mediaId}`;
        } else {
            console.log('Show has no tmdb_id or imdb_id:', item);
        }
    } else if (item.type === 'movie') {
        // Navigate to movie detail page (prefer tmdb_id, fallback to imdb_id)
        const mediaId = item.tmdb_id || item.imdb_id;
        if (mediaId) {
            window.location.href = `/library/movie/${mediaId}`;
        } else {
            console.log('Movie has no tmdb_id or imdb_id:', item);
        }
    } else {
        console.log('Clicked item:', item);
    }
}

function handleActionClick(action, item) {
    switch (action) {
        case 'refresh':
            // Refresh metadata for this item
            refreshItemMetadata(item);
            break;
        case 'delete':
            // Delete this item
            deleteItem(item);
            break;
        case 'details':
            // Navigate to detail page (same as card click)
            handleCardClick(item);
            break;
        default:
            console.warn('Unknown action:', action);
    }
}

async function refreshItemMetadata(item) {
    try {
        const response = await fetch(`/library/refresh_metadata/${item.tmdb_id}`, {
            method: 'POST'
        });

        if (response.ok) {
            showSuccess(`Refreshing metadata for "${item.title}"`);
            // Reload library after a delay
            setTimeout(() => resetAndReload(), 2000);
        } else {
            showError(`Failed to refresh metadata for "${item.title}"`);
        }
    } catch (error) {
        console.error('Error refreshing metadata:', error);
        showError('Network error while refreshing metadata');
    }
}

async function deleteItem(item) {
    if (!confirm(`Delete "${item.title}"? This will remove it from the library.`)) {
        return;
    }

    try {
        if (window.DeletionCommon) {
            // Use deletion common if available
            const result = await window.DeletionCommon.executeDelete([item.id], {
                layers: ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache']
            });

            if (result && result.success) {
                showSuccess(`Successfully deleted "${item.title}"`);
                resetAndReload();
            }
        } else {
            showError('Deletion system not available');
        }
    } catch (error) {
        console.error('Error deleting item:', error);
        showError(`Error deleting "${item.title}"`);
    }
}

function showSuccess(message) {
    // TODO: Implement success toast/notification
    console.log('SUCCESS:', message);
}

function updateResultsInfo() {
    const displayedCount = libraryState.offset;
    const totalText = libraryState.totalCount > 0
        ? `Showing ${displayedCount} of ${libraryState.totalCount} items`
        : 'Loading...';

    resultsInfo.textContent = totalText;
}

function showError(message) {
    // Create error message
    const errorDiv = document.createElement('div');
    errorDiv.className = 'error-message';
    errorDiv.textContent = message;

    // Insert at top of grid
    mediaGrid.insertBefore(errorDiv, mediaGrid.firstChild);

    // Remove after 5 seconds
    setTimeout(() => {
        errorDiv.remove();
    }, 5000);
}

// Utility functions
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

function escapeHtml(unsafe) {
    if (!unsafe) return '';
    return unsafe
        .toString()
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

// Export for debugging
window.libraryState = libraryState;
window.refreshLibrary = resetAndReload;

// ============================================================================
// MULTI-SELECT DELETION FUNCTIONALITY
// ============================================================================

// Selection state
const selectionState = {
    isSelectionMode: false,
    selectedItems: new Set(),
    lastSelectedIndex: null
};

// Initialize multi-select functionality
function initializeMultiSelect() {
    const selectBtn = document.getElementById('select-btn');
    const deleteBtn = document.getElementById('delete-selected-btn');

    if (selectBtn) {
        selectBtn.addEventListener('click', handleSelectButtonClick);
    }

    if (deleteBtn) {
        deleteBtn.addEventListener('click', deleteSelectedItems);
    }

    // Keyboard shortcuts
    document.addEventListener('keydown', handleKeyboardShortcuts);
}

// Handle Select/Cancel button click
function handleSelectButtonClick() {
    if (!selectionState.isSelectionMode) {
        // Enter selection mode
        enterSelectionMode();
    } else {
        // Cancel selection mode (exit even if items are selected)
        exitSelectionMode();
    }
}

// Enter selection mode
function enterSelectionMode() {
    selectionState.isSelectionMode = true;
    selectionState.selectedItems.clear();

    // Update UI
    const selectBtn = document.getElementById('select-btn');
    const deleteBtn = document.getElementById('delete-selected-btn');
    const mediaGrid = document.getElementById('media-grid');

    // Change Select button to Cancel
    selectBtn.textContent = 'Cancel';
    selectBtn.classList.remove('action-btn-primary');
    selectBtn.classList.add('action-btn-ghost');

    // Hide delete button initially (will show when items selected)
    deleteBtn.style.display = 'none';

    mediaGrid.classList.add('selection-mode');

    // Add checkboxes to all cards
    addCheckboxesToCards();

    // Update delete button visibility
    updateDeleteButton();
}

// Exit selection mode
function exitSelectionMode() {
    selectionState.isSelectionMode = false;
    selectionState.selectedItems.clear();
    selectionState.lastSelectedIndex = null;

    // Update UI
    const selectBtn = document.getElementById('select-btn');
    const deleteBtn = document.getElementById('delete-selected-btn');
    const mediaGrid = document.getElementById('media-grid');

    // Reset Select button
    selectBtn.innerHTML = `
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="w-4 h-4">
            <path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <span class="hidden sm:inline ml-1">Select</span>
    `;
    selectBtn.classList.remove('action-btn-ghost', 'btn-danger');
    selectBtn.classList.add('action-btn-ghost');

    // Hide delete button
    deleteBtn.style.display = 'none';

    mediaGrid.classList.remove('selection-mode');

    // Remove checkboxes from rows
    const checkboxes = document.querySelectorAll('.selection-checkbox');
    checkboxes.forEach(cb => cb.remove());

    // Remove checkbox column from list header if in list view
    const isListView = libraryState.currentView === 'list';
    if (isListView) {
        const header = document.getElementById('list-view-header');
        if (header) {
            const headerCheckbox = header.querySelector('.list-col-checkbox');
            if (headerCheckbox) {
                headerCheckbox.remove();
            }
        }
    }
}

// Add checkboxes to all media cards
function addCheckboxesToCards() {
    const isListView = libraryState.currentView === 'list';
    const items = isListView
        ? document.querySelectorAll('.list-row')
        : document.querySelectorAll('.media-card');

    // Add checkbox column to list header if in list view
    if (isListView) {
        const header = document.getElementById('list-view-header');
        if (header) {
            const headerCheckbox = document.createElement('div');
            headerCheckbox.className = 'list-col list-col-checkbox';
            headerCheckbox.innerHTML = ''; // Empty column for alignment
            header.insertBefore(headerCheckbox, header.firstChild);
        }
    }

    items.forEach((item, index) => {
        const checkbox = document.createElement('div');
        checkbox.className = 'selection-checkbox';
        checkbox.innerHTML = `<input type="checkbox"
            data-item-id="${item.dataset.id}"
            data-imdb-id="${item.dataset.imdbId}"
            data-item-type="${item.dataset.type}"
            data-title="${item.dataset.title || ''}"
            data-state="${item.dataset.state || ''}"
            data-ghostlisted="${item.dataset.ghostlisted || ''}"
            data-index="${index}">`;

        if (isListView) {
            // For list view, prepend checkbox as first column
            item.insertBefore(checkbox, item.firstChild);
            checkbox.classList.add('list-col', 'list-col-checkbox');
        } else {
            // For grid view, append to card
            item.appendChild(checkbox);
        }

        // Prevent checkbox container clicks from bubbling
        checkbox.addEventListener('click', (e) => {
            e.stopPropagation();
        });

        // Add click handler
        const input = checkbox.querySelector('input');
        input.addEventListener('change', (e) => handleCheckboxChange(e, index));
        item.addEventListener('click', (e) => handleSelectionCardClick(e, item, index));
    });
}

// Handle checkbox change
function handleCheckboxChange(event, index) {
    const itemId = parseInt(event.target.dataset.itemId);

    if (event.target.checked) {
        selectionState.selectedItems.add(itemId);
    } else {
        selectionState.selectedItems.delete(itemId);
    }

    updateSelectButtonText();
}

// Handle card click in selection mode
function handleSelectionCardClick(event, card, index) {
    if (!selectionState.isSelectionMode) return;

    // Prevent navigation when in selection mode
    event.preventDefault();
    event.stopPropagation();

    // Ignore if clicking on checkbox directly
    if (event.target.type === 'checkbox') return;

    const checkbox = card.querySelector('input[type="checkbox"]');
    if (!checkbox) return;

    const itemId = parseInt(checkbox.dataset.itemId);

    if (event.ctrlKey || event.metaKey) {
        // Ctrl+Click: Toggle individual item
        checkbox.checked = !checkbox.checked;
        if (checkbox.checked) {
            selectionState.selectedItems.add(itemId);
        } else {
            selectionState.selectedItems.delete(itemId);
        }
        selectionState.lastSelectedIndex = index;
    } else if (event.shiftKey && selectionState.lastSelectedIndex !== null) {
        // Shift+Click: Range selection
        const start = Math.min(selectionState.lastSelectedIndex, index);
        const end = Math.max(selectionState.lastSelectedIndex, index);
        const isListView = libraryState.currentView === 'list';
        const allCards = isListView
            ? document.querySelectorAll('.list-row')
            : document.querySelectorAll('.media-card');

        for (let i = start; i <= end; i++) {
            const cb = allCards[i].querySelector('input[type="checkbox"]');
            if (cb) {
                cb.checked = true;
                selectionState.selectedItems.add(parseInt(cb.dataset.itemId));
            }
        }
    } else {
        // Regular click: Toggle single item
        checkbox.checked = !checkbox.checked;
        if (checkbox.checked) {
            selectionState.selectedItems.add(itemId);
        } else {
            selectionState.selectedItems.delete(itemId);
        }
        selectionState.lastSelectedIndex = index;
    }

    updateSelectButtonText();
}

// Update select button text based on selection
// Update delete button visibility and count
function updateDeleteButton() {
    const deleteBtn = document.getElementById('delete-selected-btn');
    const count = selectionState.selectedItems.size;

    if (count === 0) {
        // Hide delete button when no items selected
        deleteBtn.style.display = 'none';
    } else {
        // Check if auto-ghostlist is enabled (determines whether deletion will ghostlist or permanently delete)
        const willGhostlist = libraryState.autoGhostlistEnabled === true;

        // Update button icon based on auto-ghostlist setting
        const ghostIcon = `
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 640 640" fill="currentColor" class="w-4 h-4">
                <path d="M168.1 531.1L156.9 540.1C153.7 542.6 149.8 544 145.8 544C136 544 128 536 128 526.2L128 256C128 150 214 64 320 64C426 64 512 150 512 256L512 526.2C512 536 504 544 494.2 544C490.2 544 486.3 542.6 483.1 540.1L471.9 531.1C458.5 520.4 439.1 522.1 427.8 535L397.3 570C394 573.8 389.1 576 384 576C378.9 576 374.1 573.8 370.7 570L344.1 539.5C331.4 524.9 308.7 524.9 295.9 539.5L269.3 570C266 573.8 261.1 576 256 576C250.9 576 246.1 573.8 242.7 570L212.2 535C200.9 522.1 181.5 520.4 168.1 531.1zM288 256C288 238.3 273.7 224 256 224C238.3 224 224 238.3 224 256C224 273.7 238.3 288 256 288C273.7 288 288 273.7 288 256zM384 288C401.7 288 416 273.7 416 256C416 238.3 401.7 224 384 224C366.3 224 352 238.3 352 256C352 273.7 366.3 288 384 288z"/>
            </svg>
        `;

        const trashIcon = `
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" class="w-4 h-4">
                <path stroke-linecap="round" stroke-linejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
            </svg>
        `;

        // Update button content
        deleteBtn.innerHTML = `
            ${willGhostlist ? ghostIcon : trashIcon}
            <span class="hidden sm:inline ml-1">${willGhostlist ? 'Ghostlist' : 'Delete'}</span>
            <span class="badge" id="delete-count-badge">${count}</span>
        `;
        deleteBtn.title = willGhostlist ? 'Ghostlist selected items' : 'Delete selected items';

        // Show delete button and update count
        deleteBtn.style.display = 'inline-flex';
    }
}

// Legacy function name for compatibility (redirect to new function)
function updateSelectButtonText() {
    updateDeleteButton();
}

// Delete selected items
async function deleteSelectedItems() {
    const selectedIds = Array.from(selectionState.selectedItems);

    if (selectedIds.length === 0) {
        return;
    }

    // Get the actual items to determine their types
    const isListView = libraryState.currentView === 'list';
    const cards = isListView
        ? document.querySelectorAll('.list-row')
        : document.querySelectorAll('.media-card');
    const itemsToDelete = [];

    cards.forEach(card => {
        const checkbox = card.querySelector('input[type="checkbox"]');
        if (checkbox && checkbox.checked) {
            const itemId = parseInt(checkbox.dataset.itemId);
            const imdbId = checkbox.dataset.imdbId;
            const itemType = checkbox.dataset.itemType;
            const title = checkbox.dataset.title;
            itemsToDelete.push({ id: itemId, imdb_id: imdbId, type: itemType, title });
        }
    });

    if (itemsToDelete.length === 0) {
        return;
    }

    // Confirm deletion
    const itemWord = itemsToDelete.length === 1 ? 'item' : 'items';
    const action = libraryState.autoGhostlistEnabled ? 'ghostlist' : 'delete';
    const canUndo = libraryState.autoGhostlistEnabled ? 'Ghostlisted items can be recovered.' : 'This action cannot be undone.';
    const confirmed = confirm(`This will ${action} ${itemsToDelete.length} ${itemWord}. ${canUndo}`);
    if (!confirmed) {
        return;
    }

    // Show loading using shared deletion loading with progress tracking
    const deletionTitle = itemsToDelete.length === 1 ? itemsToDelete[0].title : `${itemsToDelete.length} ${itemWord}`;
    window.showDeletionLoading(deletionTitle, null);

    let successCount = 0;
    let failCount = 0;
    const errors = [];
    const deletionResults = [];

    // Track progress
    let completedCount = 0;
    const totalCount = itemsToDelete.length;

    try {
        // Delete all items in parallel (not one by one)
        const deletePromises = itemsToDelete.map(async (item, index) => {
            try {
                const endpoint = item.type === 'movie'
                    ? `/library/delete_movie/${item.imdb_id}`
                    : `/library/delete_show/${item.imdb_id}`;

                // Update progress before starting
                const progress = Math.round((completedCount / totalCount) * 100);
                window.updateDeletionLoading(
                    `Processing ${item.title}...`,
                    progress,
                    `${completedCount} of ${totalCount} completed`
                );

                const response = await fetch(endpoint, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        blacklist: false,
                        layers: ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache', 'content_source']
                    })
                });

                const result = await response.json();

                completedCount++;
                const newProgress = Math.round((completedCount / totalCount) * 100);
                window.updateDeletionLoading(
                    `Completed ${item.title}`,
                    newProgress,
                    `${completedCount} of ${totalCount} completed`
                );

                if (result && result.success) {
                    return { success: true, item, result };
                } else {
                    return { success: false, item, error: result.error || 'Unknown error', result };
                }
            } catch (error) {
                completedCount++;
                const newProgress = Math.round((completedCount / totalCount) * 100);
                window.updateDeletionLoading(
                    `Failed: ${item.title}`,
                    newProgress,
                    `${completedCount} of ${totalCount} completed`
                );
                return { success: false, item, error: error.message };
            }
        });

        // Wait for all deletions to complete
        const results = await Promise.all(deletePromises);

        // Count successes and failures
        results.forEach(r => {
            if (r.success) {
                successCount++;
                deletionResults.push(r);
            } else {
                failCount++;
                errors.push(`${r.item.title}: ${r.error}`);
            }
        });

        window.hideDeletionLoading();

        // Build detailed report using deletion_common.js buildDeletionReport if single item
        // Or build summary message for multiple items
        let message;

        if (itemsToDelete.length === 1 && deletionResults.length === 1 && window.buildDeletionReport) {
            // Single item - use full detailed report
            const item = deletionResults[0].item;
            const result = deletionResults[0].result;
            const mediaType = item.type === 'movie' ? 'movie' : 'show';
            message = window.buildDeletionReport(result, item.title, mediaType);
        } else {
            // Multiple items - build summary with layer details
            const reportLines = [];

            // Check if items were ghostlisted
            let wasGhostlisted = false;
            if (deletionResults.length > 0) {
                wasGhostlisted = deletionResults.some(dr => dr.result && dr.result.auto_ghostlisted === true);
            }

            // Header - use "Ghostlisted" or "Deleted" based on auto_ghostlisted flag
            const action = wasGhostlisted ? 'Ghostlisted' : 'Deleted';
            reportLines.push(`<strong>${action} ${successCount} of ${totalCount} ${itemWord}</strong>`);
            reportLines.push('');

            // If we have successful deletions with detailed results, show aggregate stats
            if (deletionResults.length > 0) {
                const aggregateStats = {
                    totalEpisodes: 0,
                    layersExecuted: new Set(),
                    contentSourcesRemoved: new Set(),
                    layersSkipped: new Set()
                };

                deletionResults.forEach(dr => {
                    const result = dr.result;
                    if (result) {
                        aggregateStats.totalEpisodes += result.deleted_count || 0;

                        if (result.layers_executed) {
                            result.layers_executed.forEach(layer => aggregateStats.layersExecuted.add(layer));
                        }

                        // Only include content sources where items were actually removed
                        if (result.content_source_removal && result.content_source_removal.details) {
                            Object.keys(result.content_source_removal.details).forEach(source => {
                                const detail = result.content_source_removal.details[source];

                                // Only add if items were actually removed
                                // For Trakt sources: check removed > 0
                                // For Overseerr: check request_id exists (successful removal)
                                // For Plex Watchlist: check success flag
                                // For MDBList: commented out for now
                                if (detail.removed > 0 ||
                                    (source === 'Overseerr' && detail.request_id) ||
                                    (source === 'Plex_Watchlist' && detail.success)) {
                                    // || (source.startsWith('MDBList_') && detail.success)
                                    aggregateStats.contentSourcesRemoved.add(source);
                                }
                            });
                        }

                        if (result.layers_skipped) {
                            result.layers_skipped.forEach(skip => {
                                const layerName = typeof skip === 'string' ? skip : skip.layer;
                                aggregateStats.layersSkipped.add(layerName);
                            });
                        }
                    }
                });

                // Show total episodes/files deleted
                if (aggregateStats.totalEpisodes > 0) {
                    reportLines.push(`<strong>Total:</strong> ${aggregateStats.totalEpisodes} items processed`);
                    reportLines.push('');
                }

                // Show executed layers
                if (aggregateStats.layersExecuted.size > 0) {
                    Array.from(aggregateStats.layersExecuted).forEach(layer => {
                        if (layer.includes('Database')) {
                            // Show "Ghostlisted" or "Removed" based on auto_ghostlisted flag
                            if (wasGhostlisted) {
                                reportLines.push('✓ Ghostlisted in database (not deleted, prevents re-addition)');
                            } else {
                                reportLines.push('✓ Removed from database');
                            }
                        } else if (layer.includes('Media Server')) {
                            reportLines.push('✓ Removed from media server (Plex/Jellyfin)');
                        } else if (layer.includes('Filesystem')) {
                            reportLines.push('✓ Removed files from filesystem');
                        } else if (layer.includes('Debrid')) {
                            reportLines.push('✓ Removed from debrid provider');
                        } else if (layer.includes('Symlinks')) {
                            reportLines.push('✓ Removed symlinks');
                        } else if (layer.includes('Cache')) {
                            reportLines.push('✓ Cleared cache');
                        } else if (layer.includes('Content Source')) {
                            // Content sources are listed separately below
                        }
                    });
                }

                // Show content sources
                if (aggregateStats.contentSourcesRemoved.size > 0) {
                    Array.from(aggregateStats.contentSourcesRemoved).forEach(source => {
                        let sourceName = source;
                        if (source.startsWith('Trakt_List_')) {
                            const slug = source.replace('Trakt_List_', '');
                            sourceName = slug.split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
                        } else if (source.includes('Collection')) {
                            sourceName = 'Trakt Collection';
                        } else if (source === 'Plex_Watchlist') {
                            sourceName = 'Plex Watchlist';
                        // } else if (source.startsWith('MDBList_')) {
                        //     sourceName = source.replace('MDBList_', '').replace(/_/g, ' ');
                        } else if (source === 'Overseerr') {
                            sourceName = 'Overseerr';
                        }
                        reportLines.push(`✓ Removed from ${sourceName}`);
                    });
                }
            }

            // Show failures
            if (failCount > 0) {
                reportLines.push('');
                reportLines.push(`<strong>Failed: ${failCount} ${failCount === 1 ? 'item' : 'items'}</strong>`);
                errors.slice(0, 3).forEach(err => reportLines.push(`✗ ${err}`));
                if (errors.length > 3) {
                    reportLines.push(`... and ${errors.length - 3} more`);
                }
            }

            message = reportLines.join('<br>');
        }

        // Show notification using shared popup (same as movie/show pages)
        if (window.POPUP_TYPES && window.showPopup) {
            window.showPopup({
                type: successCount > 0 ? window.POPUP_TYPES.SUCCESS : window.POPUP_TYPES.ERROR,
                message: message,
                autoClose: false,
                onConfirm: () => {
                    // Exit selection mode and reload library
                    exitSelectionMode();
                    window.location.reload();
                }
            });

            // Add close button callback
            setTimeout(() => {
                const closeButton = document.querySelector('.universal-popup #popupClose');
                if (closeButton) {
                    closeButton.onclick = () => {
                        exitSelectionMode();
                        window.location.reload();
                    };
                }
            }, 100);
        } else {
            alert(message);
            exitSelectionMode();
            window.location.reload();
        }

    } catch (error) {
        window.hideDeletionLoading();
        console.error('Error deleting items:', error);
        if (window.POPUP_TYPES && window.showPopup) {
            window.showPopup({
                type: window.POPUP_TYPES.ERROR,
                message: `Error deleting items: ${error.message}`,
                autoClose: 5000
            });
        } else {
            alert(`Error deleting items: ${error.message}`);
        }
    }
}

// Keyboard shortcuts
function handleKeyboardShortcuts(event) {
    if (!selectionState.isSelectionMode) return;

    // Ctrl+A: Select all
    if ((event.ctrlKey || event.metaKey) && event.key === 'a') {
        event.preventDefault();
        const checkboxes = document.querySelectorAll('.selection-checkbox input[type="checkbox"]');
        checkboxes.forEach(cb => {
            cb.checked = true;
            selectionState.selectedItems.add(parseInt(cb.dataset.itemId));
        });
        updateSelectButtonText();
    }

    // Escape: Cancel selection
    if (event.key === 'Escape') {
        exitSelectionMode();
    }

    // Delete key: Delete selected
    if (event.key === 'Delete' && selectionState.selectedItems.size > 0) {
        deleteSelectedItems();
    }
}

// Initialize multi-select when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    // Small delay to ensure other initializations are complete
    setTimeout(initializeMultiSelect, 100);
});
