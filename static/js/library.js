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
    duplicatesStateFilter: 'all',
    typeFilter: 'all',
    resolutionFilter: 'all',
    sortBy: 'title_asc',
    totalCount: 0,
    currentView: localStorage.getItem('libraryView') || 'grid', // Remember last view
    autoGhostlistEnabled: false // Will be fetched from server
};

// DOM elements
let mediaGrid, loadingIndicator, emptyState, resultsInfo;
let searchInput, statusFilter, duplicatesStateFilter, typeFilter, resolutionFilter, sortSelect, refreshBtn, searchClearBtn;

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
    duplicatesStateFilter = document.getElementById('duplicates-state-filter');
    typeFilter = document.getElementById('type-filter');
    resolutionFilter = document.getElementById('resolution-filter');
    sortSelect = document.getElementById('sort-select');
    refreshBtn = document.getElementById('refresh-btn');
    searchClearBtn = document.getElementById('search-clear-btn');

    // Check for URL parameters first (takes precedence over localStorage)
    const urlParams = new URLSearchParams(window.location.search);
    const urlType = urlParams.get('type');
    const urlStatus = urlParams.get('status');
    const urlSort = urlParams.get('sort');

    // Load filter values from localStorage if available
    const savedFilters = {
        statusFilter: localStorage.getItem('libraryStatusFilter'),
        typeFilter: localStorage.getItem('libraryTypeFilter'),
        resolutionFilter: localStorage.getItem('libraryResolutionFilter'),
        sortBy: localStorage.getItem('librarySortBy'),
        searchTerm: localStorage.getItem('librarySearchTerm'),
        duplicatesStateFilter: localStorage.getItem('libraryDuplicatesStateFilter')
    };

    // Apply values to DOM elements and state (URL params take precedence)
    if (urlStatus) {
        statusFilter.value = urlStatus;
        libraryState.statusFilter = urlStatus;
    } else if (savedFilters.statusFilter) {
        statusFilter.value = savedFilters.statusFilter;
        libraryState.statusFilter = savedFilters.statusFilter;
    } else {
        libraryState.statusFilter = statusFilter.value;
    }

    if (urlType) {
        typeFilter.value = urlType;
        libraryState.typeFilter = urlType;
    } else if (savedFilters.typeFilter) {
        typeFilter.value = savedFilters.typeFilter;
        libraryState.typeFilter = savedFilters.typeFilter;
    } else {
        libraryState.typeFilter = typeFilter.value;
    }

    // Resolution filter
    if (savedFilters.resolutionFilter) {
        resolutionFilter.value = savedFilters.resolutionFilter;
        libraryState.resolutionFilter = savedFilters.resolutionFilter;
    } else {
        libraryState.resolutionFilter = resolutionFilter.value;
    }

    // Update sort options based on status filter (before restoring sort value)
    const isUpcoming = libraryState.statusFilter === 'upcoming';
    updateSortOptions(isUpcoming);

    // Show duplicates state filter if duplicates is selected
    if (duplicatesStateFilter && libraryState.statusFilter === 'duplicates') {
        duplicatesStateFilter.style.display = '';
        // Restore saved duplicates state filter value
        if (savedFilters.duplicatesStateFilter) {
            duplicatesStateFilter.value = savedFilters.duplicatesStateFilter;
            libraryState.duplicatesStateFilter = savedFilters.duplicatesStateFilter;
        }
    }

    if (urlSort) {
        sortSelect.value = urlSort;
        libraryState.sortBy = urlSort;
    } else if (savedFilters.sortBy) {
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
    if (duplicatesStateFilter) {
        duplicatesStateFilter.addEventListener('change', handleDuplicatesStateChange);
    }
    typeFilter.addEventListener('change', handleFilterChange);
    resolutionFilter.addEventListener('change', handleFilterChange);
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
    const isDuplicates = statusFilter.value === 'duplicates';

    // Show/hide duplicates state filter
    if (duplicatesStateFilter) {
        duplicatesStateFilter.style.display = isDuplicates ? '' : 'none';
        if (!isDuplicates) {
            duplicatesStateFilter.value = 'all';
            libraryState.duplicatesStateFilter = 'all';
        }
    }

    updateSortOptions(isUpcoming);
    handleFilterChange();
}

function handleDuplicatesStateChange() {
    libraryState.duplicatesStateFilter = duplicatesStateFilter.value;
    localStorage.setItem('libraryDuplicatesStateFilter', libraryState.duplicatesStateFilter);
    resetAndReload();
}

function handleFilterChange() {
    libraryState.statusFilter = statusFilter.value;
    libraryState.typeFilter = typeFilter.value;
    libraryState.resolutionFilter = resolutionFilter.value;
    libraryState.sortBy = sortSelect.value;

    // Save filter values to localStorage
    localStorage.setItem('libraryStatusFilter', libraryState.statusFilter);
    localStorage.setItem('libraryTypeFilter', libraryState.typeFilter);
    localStorage.setItem('libraryResolutionFilter', libraryState.resolutionFilter);
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
    // Check if user has admin permissions
    const hasAdminPermissions = document.getElementById('has_admin_permissions');
    if (hasAdminPermissions && hasAdminPermissions.value !== 'True') {
        console.warn('[Library] Settings access requires admin permissions');
        return; // Silently block for non-admins
    }

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
        duplicates_state: libraryState.duplicatesStateFilter,
        media_type: libraryState.typeFilter,
        resolution: libraryState.resolutionFilter,
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
            // total is provided on first page (offset=0) and on the last page; preserve it across subsequent pages
            if (data.total !== null && data.total !== undefined) {
                libraryState.totalCount = data.total;
            }

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


    // Build poster URL - handle both TMDB and Plex images
    let posterUrl = '/static/images/placeholder.png';
    let needsFetch = false;
    let fetchUseImdb = false;  // Flag to use IMDB fallback

    if (item.poster_path && item.poster_path.startsWith('plex:')) {
        // Plex image - use Plex proxy endpoint
        const plexPath = item.poster_path.substring(5);
        posterUrl = `/library/plex_image${plexPath}`;
    } else if (item.poster_path && item.poster_path.startsWith('/') && !item.poster_path.startsWith('/static/')) {
        // TMDB image - use TMDB proxy endpoint
        posterUrl = `/scraper/tmdb_image/w92${item.poster_path}`; // Smaller size for list view
    } else if (item.poster_path && !item.poster_path.startsWith('/static/')) {
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
    // Detect mixed states for TV shows (for split badges)
    const mixedStates = detectMixedStates(item);

    // Status badge using Discover-style icons
    // Don't apply state class if we have a split badge (to avoid CSS conflicts)
    const iconState = mixedStates.primary || mapLibraryStateToStatusClass(item.state, item.status_label, item.episode_info);
    const statusClass = mixedStates.isSplit ? '' : iconState;
    const statusIcon = getStatusIcon(iconState);

    // Create split badge if mixed states detected
    const splitClass = mixedStates.isSplit ? `split-badge split-${mixedStates.primary}-${mixedStates.secondary}` : '';
    const statusTitle = mixedStates.isSplit
        ? `${mixedStates.primary} + ${mixedStates.secondary}`
        : iconState;

    const statusBadge = `<span class="db-status-badge ${statusClass} ${splitClass}" title="${statusTitle}">
        ${statusIcon}
    </span>`;

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

    // Fetch missing poster asynchronously (similar to grid view)
    if (needsFetch) {
        const posterImg = row.querySelector('.list-col-poster img');
        if (posterImg) {
            if (fetchUseImdb) {
                // Use IMDB ID to look up poster (TMDB ID not available)
                fetchMissingPosterByImdb(item.imdb_id, item.type, posterImg);
            } else {
                // Use TMDB ID directly
                fetchMissingPoster(item.tmdb_id, item.type, posterImg);
            }
        }
    }

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
    } else if (item.poster_path && item.poster_path.startsWith('/') && !item.poster_path.startsWith('/static/')) {
        // TMDB image - use TMDB proxy endpoint (w185 matches 160px card width)
        posterUrl = `/scraper/tmdb_image/w185${item.poster_path}`;
    } else if (item.poster_path && !item.poster_path.startsWith('/static/')) {
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

    // Detect mixed states for TV shows (for split badges)
    const mixedStates = detectMixedStates(item);

    // Status badge using Discover-style icons
    // Don't apply state class if we have a split badge (to avoid CSS conflicts)
    const iconState = mixedStates.primary || mapLibraryStateToStatusClass(item.state, item.status_label, item.episode_info);
    const statusClass = mixedStates.isSplit ? '' : iconState;
    const statusIcon = getStatusIcon(iconState);

    // Create split badge if mixed states detected
    const splitClass = mixedStates.isSplit ? `split-badge split-${mixedStates.primary}-${mixedStates.secondary}` : '';
    const statusTitle = mixedStates.isSplit
        ? `${mixedStates.primary} + ${mixedStates.secondary}`
        : iconState;

    const statusBadge = `<div class="db-status-badge ${statusClass} ${splitClass}" title="${statusTitle}">
        ${statusIcon}
    </div>`;

    // Media type badge using Discover-style icons (top-right)
    const mediaTypeIcon = getMediaTypeIcon(item.type);
    const mediaTypeClass = item.type === 'show' ? 'badge-tv' : 'badge-movie';
    const typeBadge = `<div class="media-type-badge ${mediaTypeClass}" title="${item.type === 'show' ? 'TV Show' : 'Movie'}">
        ${mediaTypeIcon}
    </div>`;

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
            // Parse manually to avoid timezone conversion issues
            const parts = item.release_date.split('-');
            if (parts.length === 3) {
                const year = parts[0];
                const month = parseInt(parts[1], 10) - 1;
                const day = parseInt(parts[2], 10);
                const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
                progressInfo = `${months[month]} ${day}, ${year}`;
            } else {
                progressInfo = item.release_date;
            }
        } else {
            progressInfo = 'Release date TBA';
        }
    } else {
        // For all other items, show progress percentage
        progressInfo = `${progressPercent}% complete`;
    }

    // Build card HTML (Cinephage style with Discover badges)
    // For movies: status badge in top-left, type badge in top-right
    // For shows: both badges stacked in top-right (type on top, status below)
    if (item.type === 'show') {
        card.innerHTML = `
            <div class="media-poster">
                <img src="${posterUrl}"
                     alt="${escapeHtml(item.title)}"
                     loading="lazy"
                     onerror="this.onerror=null; this.src='/static/images/placeholder.png'; this.classList.add('placeholder');">

                <!-- Top-right badges (Type + Status stacked) for shows -->
                <div class="badge-top-right">
                    ${typeBadge}
                    ${statusBadge}
                </div>

                <!-- Episode count badge - top left -->
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
    } else {
        // Movie layout: status in top-left, type in top-right
        card.innerHTML = `
            <div class="media-poster">
                <img src="${posterUrl}"
                     alt="${escapeHtml(item.title)}"
                     loading="lazy"
                     onerror="this.onerror=null; this.src='/static/images/placeholder.png'; this.classList.add('placeholder');">

                <!-- Status badge - top left for movies -->
                <div class="badge-top-left">
                    ${statusBadge}
                </div>

                <!-- Type badge - top right for movies -->
                <div class="badge-top-right">
                    ${typeBadge}
                </div>

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
    }

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

/**
 * Detect mixed states for TV shows and return split badge info
 * Returns: { primary, secondary, isSplit } where primary is the higher priority state
 * Priority order: Partial/Wanted > Unreleased > Collected > Blacklisted
 */
function detectMixedStates(item) {
    // Only for TV shows with episode info
    if (item.type !== 'show' || !item.episode_info) {
        return { primary: null, secondary: null, isSplit: false };
    }

    const info = item.episode_info;
    const states = [];

    // Determine which states are present (must have at least 1 episode in that state)
    if (info.collected > 0) states.push('collected');
    if (info.wanted > 0) states.push('wanted');  // Partial/wanted episodes
    if (info.unreleased > 0) states.push('unreleased');
    if (info.blacklisted > 0) states.push('blacklisted');

    // If only one state or no states, no split needed
    if (states.length <= 1) {
        return { primary: states[0] || null, secondary: null, isSplit: false };
    }

    // Define priority order (highest to lowest)
    const priority = ['wanted', 'unreleased', 'collected', 'blacklisted'];

    // Sort states by priority
    const sortedStates = states.sort((a, b) => {
        return priority.indexOf(a) - priority.indexOf(b);
    });

    // Return primary (highest priority) and secondary (next highest) with split flag
    return {
        primary: sortedStates[0],
        secondary: sortedStates[1],
        isSplit: true
    };
}

/**
 * Map library state to Discover-style status class
 */
function mapLibraryStateToStatusClass(state, statusLabel, episodeInfo) {
    // For TV shows, check for mixed states first
    if (episodeInfo && episodeInfo.total > 0) {
        const collected = episodeInfo.collected || 0;
        const total = episodeInfo.total || 0;
        const blacklisted = episodeInfo.blacklisted || 0;
        const unreleased = episodeInfo.unreleased || 0;
        const wanted = episodeInfo.wanted || 0;

        // If all collected -> collected
        if (collected === total) return 'collected';

        // If all blacklisted -> blacklisted
        if (blacklisted === total) return 'blacklisted';

        // If all unreleased -> unreleased
        if (unreleased === total) return 'unreleased';

        // If partially collected -> wanted (partial)
        if (collected > 0 && collected < total) return 'wanted';

        // If nothing collected but some wanted -> wanted
        if (wanted > 0) return 'wanted';
    }

    if (!state) {
        // Use status_label as fallback
        if (statusLabel === 'Collected') return 'collected';
        if (statusLabel === 'Missing') return 'blacklisted';
        return 'missing';
    }

    const stateLower = state.toLowerCase();

    // Direct mappings
    if (stateLower === 'collected') return 'collected';
    // Treat upgrading as collected (we have it, just getting better version)
    if (stateLower === 'upgrading') return 'collected';
    if (stateLower === 'blacklisted') return 'blacklisted';
    if (stateLower === 'unreleased') return 'unreleased';

    // In-progress states map to 'wanted' (yellow)
    if (['wanted', 'scraping', 'adding', 'checking', 'sleeping', 'partial'].includes(stateLower)) {
        return 'wanted';
    }

    // Default to missing (red X)
    return 'missing';
}

/**
 * Get status icon HTML (from Discover page)
 */
function getStatusIcon(status) {
    switch (status) {
        case 'collected':
        case 'present':
            // Archive box icon - collected/in library (green)
            return `<svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                <path stroke-linecap="round" stroke-linejoin="round" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"></path>
            </svg>`;
        case 'partial':
        case 'wanted':
            // Partial file icon - partially collected/wanted (yellow)
            return `<svg xmlns="http://www.w3.org/2000/svg" fill="currentColor" viewBox="0 0 640 640">
                <path d="M192 64C156.7 64 128 92.7 128 128L128 384L512 384L512 234.5C512 217.5 505.3 201.2 493.3 189.2L386.7 82.7C374.7 70.7 358.5 64 341.5 64L192 64zM453.5 240L360 240C346.7 240 336 229.3 336 216L336 122.5L453.5 240zM128 416L128 480L192 480L192 416L128 416zM192 576L192 512L128 512C128 547.3 156.7 576 192 576zM224 576L304 576L304 512L224 512L224 576zM336 576L416 576L416 512L336 512L336 576zM448 576C483.3 576 512 547.3 512 512L448 512L448 576zM512 416L448 416L448 480L512 480L512 416z"/>
            </svg>`;
        case 'upgrading':
            // Upload arrow icon - upgrading (blue)
            return `<svg xmlns="http://www.w3.org/2000/svg" fill="currentColor" viewBox="0 0 640 640">
                <path d="M342.6 73.4C330.1 60.9 309.8 60.9 297.3 73.4L169.3 201.4C156.8 213.9 156.8 234.2 169.3 246.7C181.8 259.2 202.1 259.2 214.6 246.7L288 173.3L288 384C288 401.7 302.3 416 320 416C337.7 416 352 401.7 352 384L352 173.3L425.4 246.7C437.9 259.2 458.2 259.2 470.7 246.7C483.2 234.2 483.2 213.9 470.7 201.4L342.7 73.4zM160 416C160 398.3 145.7 384 128 384C110.3 384 96 398.3 96 416L96 480C96 533 139 576 192 576L448 576C501 576 544 533 544 480L544 416C544 398.3 529.7 384 512 384C494.3 384 480 398.3 480 416L480 480C480 497.7 465.7 512 448 512L192 512C174.3 512 160 497.7 160 480L160 416z"/>
            </svg>`;
        case 'blacklisted':
            // Ban/circle with slash icon - blacklisted (black)
            return `<svg xmlns="http://www.w3.org/2000/svg" fill="currentColor" viewBox="0 0 640 640">
                <path d="M431.2 476.5L163.5 208.8C141.1 240.2 128 278.6 128 320C128 426 214 512 320 512C361.5 512 399.9 498.9 431.2 476.5zM476.5 431.2C498.9 399.8 512 361.4 512 320C512 214 426 128 320 128C278.5 128 240.1 141.1 208.8 163.5L476.5 431.2zM64 320C64 178.6 178.6 64 320 64C461.4 64 576 178.6 576 320C576 461.4 461.4 576 320 576C178.6 576 64 461.4 64 320z"/>
            </svg>`;
        case 'unreleased':
            // Calendar X icon - unreleased/coming soon (orange)
            return `<svg xmlns="http://www.w3.org/2000/svg" fill="currentColor" viewBox="0 0 640 640">
                <path d="M224 64C241.7 64 256 78.3 256 96L256 128L384 128L384 96C384 78.3 398.3 64 416 64C433.7 64 448 78.3 448 96L448 128L480 128C515.3 128 544 156.7 544 192L544 480C544 515.3 515.3 544 480 544L160 544C124.7 544 96 515.3 96 480L96 192C96 156.7 124.7 128 160 128L192 128L192 96C192 78.3 206.3 64 224 64zM387.9 284.1C378.5 274.7 363.3 274.7 354 284.1L320.1 318L286.2 284.1C276.8 274.7 261.6 274.7 252.3 284.1C243 293.5 242.9 308.7 252.3 318L286.2 351.9L252.3 385.8C242.9 395.2 242.9 410.4 252.3 419.7C261.7 429 276.9 429.1 286.2 419.7L320.1 385.8L354 419.7C363.4 429.1 378.6 429.1 387.9 419.7C397.2 410.3 397.3 395.1 387.9 385.8L354 351.9L387.9 318C397.3 308.6 397.3 293.4 387.9 284.1z"/>
            </svg>`;
        case 'missing':
        default:
            // X icon - missing/not in library (red)
            return `<svg xmlns="http://www.w3.org/2000/svg" fill="currentColor" viewBox="0 0 640 640">
                <path d="M504.6 148.5C515.9 134.9 514.1 114.7 500.5 103.4C486.9 92.1 466.7 93.9 455.4 107.5L320 270L184.6 107.5C173.3 93.9 153.1 92.1 139.5 103.4C125.9 114.7 124.1 134.9 135.4 148.5L278.3 320L135.4 491.5C124.1 505.1 125.9 525.3 139.5 536.6C153.1 547.9 173.3 546.1 184.6 532.5L320 370L455.4 532.5C466.7 546.1 486.9 547.9 500.5 536.6C514.1 525.3 515.9 505.1 504.6 491.5L361.7 320L504.6 148.5z"/>
            </svg>`;
    }
}

/**
 * Get media type icon HTML (from Discover page)
 */
function getMediaTypeIcon(mediaType) {
    if (mediaType === 'movie') {
        // Film/movie icon
        return `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-4 h-4">
            <path stroke-linecap="round" stroke-linejoin="round" d="M3.375 19.5h17.25m-17.25 0a1.125 1.125 0 01-1.125-1.125M3.375 19.5h1.5C5.496 19.5 6 18.996 6 18.375m-2.625 0V5.625m0 12.75v-1.5c0-.621.504-1.125 1.125-1.125m18.375 2.625V5.625m0 12.75c0 .621-.504 1.125-1.125 1.125m1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125m0 3.75h-1.5A1.125 1.125 0 0118 18.375M20.625 4.5H3.375m17.25 0c.621 0 1.125.504 1.125 1.125M20.625 4.5h-1.5C18.504 4.5 18 5.004 18 5.625m3.75 0v1.5c0 .621-.504 1.125-1.125 1.125M3.375 4.5c-.621 0-1.125.504-1.125 1.125M3.375 4.5h1.5C5.496 4.5 6 5.004 6 5.625m-2.625 0v1.5c0 .621.504 1.125 1.125 1.125m0 0h1.5m-1.5 0c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125m1.5-3.75C5.496 8.25 6 7.746 6 7.125v-1.5M4.875 8.25C5.496 8.25 6 8.754 6 9.375v1.5m0-5.25v5.25m0-5.25C6 5.004 6.504 4.5 7.125 4.5h9.75c.621 0 1.125.504 1.125 1.125m1.125 2.625h1.5m-1.5 0A1.125 1.125 0 0118 7.125v-1.5m1.125 2.625c-.621 0-1.125.504-1.125 1.125v1.5m2.625-2.625c.621 0 1.125.504 1.125 1.125v1.5c0 .621-.504 1.125-1.125 1.125M18 5.625v5.25M7.125 12h9.75m-9.75 0A1.125 1.125 0 016 10.875M7.125 12C6.504 12 6 12.504 6 13.125m0-2.25C6 11.496 5.496 12 4.875 12M18 10.875c0 .621-.504 1.125-1.125 1.125M18 10.875c0 .621.504 1.125 1.125 1.125m-2.25 0c.621 0 1.125.504 1.125 1.125m-12 5.25v-5.25m0 5.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125m-12 0v-1.5c0-.621-.504-1.125-1.125-1.125M18 18.375v-5.25m0 5.25v-1.5c0-.621.504-1.125 1.125-1.125M18 13.125v1.5c0 .621.504 1.125 1.125 1.125M18 13.125c0-.621.504-1.125 1.125-1.125M6 13.125v1.5c0 .621-.504 1.125-1.125 1.125M6 13.125C6 12.504 5.496 12 4.875 12m-1.5 0h1.5m-1.5 0c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125M19.125 12h1.5m0 0c.621 0 1.125.504 1.125 1.125v1.5c0 .621-.504 1.125-1.125 1.125m-17.25 0h1.5m14.25 0h1.5" />
        </svg>`;
    } else {
        // TV icon (for shows)
        return `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-4 h-4">
            <path stroke-linecap="round" stroke-linejoin="round" d="M6 20.25h12m-7.5-3v3m3-3v3m-10.125-3h17.25c.621 0 1.125-.504 1.125-1.125V4.875c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125z" />
        </svg>`;
    }
}

// Helper function to format file size
function formatFileSize(bytes) {
    if (!bytes || bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const k = 1024;
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + units[i];
}

// Concurrency-limited queue for on-demand poster fetches (prevents flooding TMDB API)
const _posterFetchQueue = {
    queue: [],
    running: 0,
    maxConcurrent: 5,
    add(fn) {
        this.queue.push(fn);
        this._run();
    },
    _run() {
        while (this.running < this.maxConcurrent && this.queue.length > 0) {
            const fn = this.queue.shift();
            this.running++;
            fn().finally(() => {
                this.running--;
                this._run();
            });
        }
    }
};

// Fetch missing poster from TMDB on-demand using TMDB ID
function fetchMissingPoster(tmdbId, mediaType, card) {
    _posterFetchQueue.add(async () => {
        try {
            const response = await fetch(`/library/fetch_poster/${tmdbId}/${mediaType}`);
            const data = await response.json();

            if (data.success && data.poster_path) {
                // Update the poster image - handle both grid view (.media-poster img) and list view (.list-col-poster img)
                // Also handle case where 'card' is already the img element itself
                const img = card.tagName === 'IMG' ? card :
                            (card.querySelector('.media-poster img') ||
                             card.querySelector('.list-col-poster img'));
                if (img) {
                    img.src = `/scraper/tmdb_image/w185${data.poster_path}`;
                    img.classList.remove('placeholder');
                }
            }
        } catch (error) {
            console.debug(`Could not fetch poster for ${tmdbId}:`, error);
            // Silent fail - placeholder already shown
        }
    });
}

// Fetch missing poster from TMDB on-demand using IMDB ID (fallback when no TMDB ID)
function fetchMissingPosterByImdb(imdbId, mediaType, card) {
    _posterFetchQueue.add(async () => {
        try {
            const response = await fetch(`/library/fetch_poster_imdb/${imdbId}/${mediaType}`);
            const data = await response.json();

            if (data.success && data.poster_path) {
                // Update the poster image - handle both grid view (.media-poster img) and list view (.list-col-poster img)
                // Also handle case where 'card' is already the img element itself
                const img = card.tagName === 'IMG' ? card :
                            (card.querySelector('.media-poster img') ||
                             card.querySelector('.list-col-poster img'));
                if (img) {
                    img.src = `/scraper/tmdb_image/w185${data.poster_path}`;
                    img.classList.remove('placeholder');
                }
            }
        } catch (error) {
            console.debug(`Could not fetch poster for IMDB ${imdbId}:`, error);
            // Silent fail - placeholder already shown
        }
    });
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
    showPopup({
        type: 'confirm',
        title: 'Delete Item',
        message: `Delete "${item.title}"? This will remove it from the library.`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
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
    });
}

function showSuccess(message) {
    // TODO: Implement success toast/notification
    console.log('SUCCESS:', message);
}

function updateResultsInfo() {
    const displayedCount = libraryState.offset;
    let totalText;
    if (libraryState.totalCount > 0) {
        totalText = `Showing ${displayedCount}/${libraryState.totalCount} items`;
    } else if (displayedCount > 0) {
        totalText = `Showing ${displayedCount} items`;
    } else {
        totalText = 'Loading...';
    }
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
    lastSelectedIndex: null,
    containerClickHandler: null // Store handler for event delegation
};

// Initialize multi-select functionality
function initializeMultiSelect() {
    const selectBtn = document.getElementById('select-btn');
    const deleteBtn = document.getElementById('delete-selected-btn');

    // Only initialize if buttons exist (admin-only)
    if (selectBtn) {
        selectBtn.addEventListener('click', handleSelectButtonClick);
    }

    if (deleteBtn) {
        deleteBtn.addEventListener('click', deleteSelectedItems);
    }

    // Keyboard shortcuts (only for admins since non-admins won't have select/delete buttons)
    if (selectBtn || deleteBtn) {
        document.addEventListener('keydown', handleKeyboardShortcuts);
    }
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

    // Add event delegation listener to container (ONE listener for all items)
    selectionState.containerClickHandler = handleContainerClick;
    mediaGrid.addEventListener('click', selectionState.containerClickHandler);

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

    // Remove event delegation listener from container
    if (selectionState.containerClickHandler) {
        mediaGrid.removeEventListener('click', selectionState.containerClickHandler);
        selectionState.containerClickHandler = null;
    }

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

        // Add change handler for checkbox input
        const input = checkbox.querySelector('input');
        input.addEventListener('change', (e) => handleCheckboxChange(e, index));

        // NOTE: Item click handler now uses event delegation on container (see handleContainerClick)
        // No individual listeners attached to each item - improves performance and prevents memory leaks
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

// Handle container click with event delegation (ONE listener for all items)
function handleContainerClick(event) {
    if (!selectionState.isSelectionMode) return;

    // Find the clicked card/row (event delegation)
    const isListView = libraryState.currentView === 'list';
    const card = event.target.closest(isListView ? '.list-row' : '.media-card');

    if (!card) return; // Click wasn't on a card/row

    // Get the index of the clicked item
    const checkbox = card.querySelector('input[type="checkbox"]');
    if (!checkbox) return;

    const index = parseInt(checkbox.dataset.index);

    // Delegate to the existing selection logic
    handleSelectionCardClick(event, card, index);
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
    showPopup({
        type: 'confirm',
        title: 'Confirm Deletion',
        message: `This will ${action} ${itemsToDelete.length} ${itemWord}. ${canUndo}`,
        confirmText: action.charAt(0).toUpperCase() + action.slice(1),
        cancelText: 'Cancel',
        onConfirm: async function() {

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

    const allLayers = ['database', 'media_server', 'filesystem', 'debrid', 'symlinks', 'cache', 'content_source'];
    const layersWithoutPlex = allLayers.filter(l => l !== 'media_server');

    const doDelete = async (item, layers) => {
        const endpoint = item.type === 'movie'
            ? `/library/delete_movie/${item.imdb_id}`
            : `/library/delete_show/${item.imdb_id}`;
        const resp = await fetch(endpoint, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ blacklist: false, layers })
        });
        return await resp.json();
    };

    try {
        // Phase 1: Delete all items in parallel with full layers
        const deletePromises = itemsToDelete.map(async (item, index) => {
            try {
                const progress = Math.round((completedCount / totalCount) * 100);
                window.updateDeletionLoading(
                    `Processing ${item.title}...`,
                    progress,
                    `${completedCount} of ${totalCount} completed`
                );

                const result = await doDelete(item, allLayers);

                completedCount++;
                const newProgress = Math.round((completedCount / totalCount) * 100);
                window.updateDeletionLoading(
                    result.plex_not_found ? `Not in Plex: ${item.title}` : `Completed ${item.title}`,
                    newProgress,
                    `${completedCount} of ${totalCount} completed`
                );

                if (result && result.success) {
                    return { success: true, item, result };
                } else if (result && result.plex_not_found) {
                    return { success: false, plexNotFound: true, item, result };
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

        // Wait for all Phase 1 deletions to complete
        const results = await Promise.all(deletePromises);

        console.log('[DELETE] Phase 1 results:', JSON.stringify(results.map(r => ({success: r.success, plexNotFound: r.plexNotFound, title: r.item && r.item.title, error: r.error}))));

        // Separate plex-not-found items from normal results
        const plexNotFoundItems = results.filter(r => r.plexNotFound);
        const normalResults = results.filter(r => !r.plexNotFound);

        console.log('[DELETE] plexNotFoundItems:', plexNotFoundItems.length, 'normalResults:', normalResults.length);

        // Phase 2: If any items were not found in Plex, prompt user once for all of them
        if (plexNotFoundItems.length > 0) {
            window.hideDeletionLoading();

            console.log('[DELETE] Phase 2: showing CONFIRM popup, window.showPopup:', typeof window.showPopup, 'window.POPUP_TYPES:', window.POPUP_TYPES);

            const titles = plexNotFoundItems.map(r => `<strong>${r.item.title}</strong>`).join('<br>');
            const itemWord = plexNotFoundItems.length === 1 ? 'item' : 'items';
            const confirmed = await new Promise((resolve) => {
                if (window.POPUP_TYPES && window.showPopup) {
                    window.showPopup({
                        type: window.POPUP_TYPES.CONFIRM,
                        title: `${plexNotFoundItems.length} ${itemWord} not found in Plex`,
                        message: `The following ${itemWord} were not found in Plex (already removed):<br><br>${titles}<br><br>Continue removing from database, debrid/usenet and other layers?`,
                        confirmText: 'Continue',
                        cancelText: 'Cancel',
                        onConfirm: () => { console.log('[DELETE] User clicked Continue'); resolve(true); },
                        onCancel: () => { console.log('[DELETE] User clicked Cancel'); resolve(false); }
                    });
                    console.log('[DELETE] showPopup called, waiting for user...');
                } else {
                    console.log('[DELETE] Falling back to showPopup confirm dialog');
                    const titleList = plexNotFoundItems.map(r => r.item.title).join(', ');
                    showPopup({
                        type: 'confirm',
                        title: 'Not Found in Plex',
                        message: `"${titleList}" not found in Plex. Continue removing from database and other layers?`,
                        confirmText: 'Continue',
                        cancelText: 'Cancel',
                        onConfirm: () => resolve(true),
                        onCancel: () => resolve(false)
                    });
                }
            });
            console.log('[DELETE] Phase 2 confirmed:', confirmed);

            if (confirmed) {
                window.showDeletionLoading(deletionTitle, null);
                // Re-run deletions without media_server layer for these items
                const retryPromises = plexNotFoundItems.map(async (r) => {
                    try {
                        window.updateDeletionLoading(
                            `Processing ${r.item.title}...`,
                            Math.round((completedCount / totalCount) * 100),
                            `${completedCount} of ${totalCount} completed`
                        );
                        const retryResult = await doDelete(r.item, layersWithoutPlex);
                        completedCount++;
                        if (retryResult && retryResult.success) {
                            return { success: true, item: r.item, result: retryResult };
                        } else {
                            return { success: false, item: r.item, error: retryResult.error || 'Unknown error', result: retryResult };
                        }
                    } catch (error) {
                        completedCount++;
                        return { success: false, item: r.item, error: error.message };
                    }
                });
                const retryResults = await Promise.all(retryPromises);
                normalResults.push(...retryResults);
            } else {
                // User cancelled — treat as failed
                plexNotFoundItems.forEach(r => {
                    normalResults.push({ success: false, item: r.item, error: 'Cancelled — not found in Plex', result: r.result });
                });
                window.showDeletionLoading(deletionTitle, null);
            }
        }

        // Count successes and failures across all results
        normalResults.forEach(r => {
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
                            reportLines.push('✓ Removed from debrid/usenet provider');
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
                            sourceName = 'Seerr';
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
            showPopup({
                type: successCount > 0 ? 'success' : 'error',
                title: successCount > 0 ? 'Success' : 'Error',
                message: message,
                autoClose: false,
                onConfirm: () => {
                    exitSelectionMode();
                    window.location.reload();
                }
            });
        }

    } catch (error) {
        window.hideDeletionLoading();
        console.error('Error deleting items:', error);
        showPopup({
            type: 'error',
            title: 'Error',
            message: `Error deleting items: ${error.message}`,
            autoClose: 5000
        });
    }

        } // end onConfirm
    }); // end showPopup
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
