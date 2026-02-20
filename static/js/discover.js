/**
 * Discover - Cinephage-style content discovery
 * Integrates with TMDB API for movies and TV shows
 * Features advanced filtering, search, and visual browsing
 */

/* global showToast */

// State management
window.discoverState = {
    currentTab: 'trending',
    searchTerm: '',
    mediaType: 'all',
    sortBy: 'popularity.desc',
    sortOrder: 'desc',
    page: 1,
    hasMore: true,
    isLoading: false,
    autoLoadCount: 0, // Track consecutive auto-loads to prevent infinite loops
    maxAutoLoads: 1,  // Maximum pages to auto-load before requiring user scroll
    liveFilterEnabled: true, // Enable live filtering when filter values change
    genres: {
        movie: [],
        tv: []
    },
    filters: {
        // Search & Sort
        searchQuery: '',
        sortBy: 'popularity',
        sortOrder: 'desc',
        
        // Basic Filters
        mediaType: 'all',
        yearFrom: '',
        yearTo: '',
        releasedWithin: '',
        upcomingDays: '',
        
        // Ratings & Votes
        tmdbRatingMin: 0,
        tmdbRatingMax: 10,
        imdbRatingMin: 0,
        imdbRatingMax: 10,
        tmdbVotesMin: 0,
        imdbVotesMin: 0,
        
        // Genres & Categories (include/exclude)
        selectedGenres: [],
        excludedGenres: [],
        selectedLanguages: [],
        excludedLanguages: [],
        selectedCountries: [],
        excludedCountries: [],
        certificationMin: '',
        certificationMax: '',
        selectedProviders: [],
        excludedProviders: [],
        watchRegion: 'US',
        selectedNetworks: [],
        excludedNetworks: [],
        selectedCompanies: [],
        excludedCompanies: [],
        companyCache: {},  // Cache company names by ID for display
        selectedKeywords: [],
        excludedKeywords: [],
        keywordCache: {},  // Cache keyword names by ID for display
        titleFilter: '',  // Client-side title filter (supports text or regex)
        runtimeMin: 0,
        runtimeMax: 300,

        // Active filters for UI
        activeFilters: []
    },
    // Discover settings (loaded from global settings)
    discoverSettings: {
        hide_no_rating: false,
        hide_no_poster: false,
        only_show_missing: false
    }
};

// DOM elements
let searchInput, searchClearBtn, filterToggleBtn, filterDrawer, filterOverlay, filterCloseBtn;
let tabButtons, resultsGrid, loadingState, emptyState, errorState, pagination;
let ratingSlider, ratingDisplay, yearFromInput, yearToInput, genresContainer;
let loadMoreBtn;
// Category grids for trending rows
let trendingContent, searchResults, moviesGrid, showsGrid, animeGrid;

/**
 * Save current filter state to localStorage
 */
function saveFiltersToStorage() {
    try {
        const state = window.discoverState;
        const filtersToSave = {
            // Search & Sort
            searchQuery: state.filters.searchQuery,
            sortBy: state.filters.sortBy,
            sortOrder: state.filters.sortOrder,
            
            // Basic Filters
            mediaType: state.filters.mediaType,
            yearFrom: state.filters.yearFrom,
            yearTo: state.filters.yearTo,
            releasedWithin: state.filters.releasedWithin,
            upcomingDays: state.filters.upcomingDays,
            
            // Ratings & Votes
            tmdbRatingMin: state.filters.tmdbRatingMin,
            tmdbRatingMax: state.filters.tmdbRatingMax,
            imdbRatingMin: state.filters.imdbRatingMin,
            imdbRatingMax: state.filters.imdbRatingMax,
            tmdbVotesMin: state.filters.tmdbVotesMin,
            imdbVotesMin: state.filters.imdbVotesMin,
            
            // Genres & Categories (include/exclude)
            selectedGenres: state.filters.selectedGenres,
            excludedGenres: state.filters.excludedGenres,
            selectedLanguages: state.filters.selectedLanguages,
            excludedLanguages: state.filters.excludedLanguages,
            selectedCountries: state.filters.selectedCountries,
            excludedCountries: state.filters.excludedCountries,
            selectedProviders: state.filters.selectedProviders,
            excludedProviders: state.filters.excludedProviders,
            watchRegion: state.filters.watchRegion,
            selectedNetworks: state.filters.selectedNetworks,
            excludedNetworks: state.filters.excludedNetworks,
            selectedCompanies: state.filters.selectedCompanies,
            excludedCompanies: state.filters.excludedCompanies,
            selectedKeywords: state.filters.selectedKeywords,
            excludedKeywords: state.filters.excludedKeywords,
            
            // Other
            titleFilter: state.filters.titleFilter,
            runtimeMin: state.filters.runtimeMin,
            runtimeMax: state.filters.runtimeMax,
            certificationMin: state.filters.certificationMin,
            certificationMax: state.filters.certificationMax,
            includeVideo: state.filters.includeVideo,

            // Cache keyword/company names (needed for chip display)
            keywordCache: state.filters.keywordCache || {},
            companyCache: state.filters.companyCache || {}
        };
        
        localStorage.setItem('discoverFilters', JSON.stringify(filtersToSave));
    } catch (e) {
        console.error('[Discover] Failed to save filters:', e);
    }
}

/**
 * Load filter state from localStorage
 */
function loadFiltersFromStorage() {
    try {
        const saved = localStorage.getItem('discoverFilters');
        if (!saved) {
            return null;
        }

        const filters = JSON.parse(saved);
        return filters;
    } catch (e) {
        console.error('[Discover] Failed to load saved filters:', e);
        return null;
    }
}

/**
 * Clear saved filters from localStorage
 */
function clearSavedFilters() {
    localStorage.removeItem('discoverFilters');
    localStorage.removeItem('discoverSearchTerm');
    localStorage.removeItem('discoverSidebarLists');
    localStorage.removeItem('discoverFlixPatrol');
    localStorage.removeItem('discoverMDBList');
}

/**
 * Save sidebar lists selection to localStorage
 */
function saveSidebarListsToStorage() {
    try {
        const listsToSave = window.sidebarListsState.selectedLists;
        localStorage.setItem('discoverSidebarLists', JSON.stringify(listsToSave));
    } catch (e) {
        console.error('[Lists Filter] Failed to save lists:', e);
    }
}

/**
 * Load sidebar lists selection from localStorage
 */
function loadSidebarListsFromStorage() {
    try {
        const saved = localStorage.getItem('discoverSidebarLists');
        if (!saved) {
            return null;
        }

        const lists = JSON.parse(saved);
        return lists;
    } catch (e) {
        console.error('[Lists Filter] Failed to load saved lists:', e);
        return null;
    }
}

/**
 * Clear sidebar lists from localStorage
 */
function clearSavedSidebarLists() {
    try {
        localStorage.removeItem('discoverSidebarLists');
    } catch (e) {
        console.error('[Lists Filter] Failed to clear saved lists:', e);
    }
}

/**
 * Load discover settings from API
 */
async function loadDiscoverSettings() {
    try {
        const response = await fetch('/settings/api/config');
        if (!response.ok) {
            console.error('[Discover] Failed to load settings');
            return;
        }

        const config = await response.json();
        const discoverSettings = config['Discover Settings'] || {};

        // Update state with loaded settings
        window.discoverState.discoverSettings = {
            hide_no_rating: discoverSettings.hide_no_rating || false,
            hide_no_poster: discoverSettings.hide_no_poster || false,
            only_show_missing: discoverSettings.only_show_missing || false,
            tv_show_episode_view: discoverSettings.tv_show_episode_view || 'discover'
        };
    } catch (error) {
        console.error('[Discover] Error loading settings:', error);
    }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', async function() {

    const container = document.querySelector('.discover-container');
    const tmdbConfigured = container.dataset.tmdbConfigured === 'true';

    if (!tmdbConfigured) {
        console.warn('[Discover] TMDB not configured, skipping initialization');
        return;
    }

    // Load discover settings from API
    await loadDiscoverSettings();

    // Check for special entry methods and clear saved state if needed
    const urlParams = new URLSearchParams(window.location.search);
    const isSearchRedirect = urlParams.has('q') || urlParams.has('imdb_id') || urlParams.has('tmdb_id');
    const isAdaptiveListEntry = urlParams.has('edit_adaptive_list') || urlParams.get('mode') === 'create_adaptive_list';
    const isSpecialEntry = isSearchRedirect || isAdaptiveListEntry;

    if (isSpecialEntry) {
        clearSavedFilters();
    }

    // Load saved filters from localStorage
    const savedFilters = loadFiltersFromStorage();
    let hasRestoredFilters = false;
    if (savedFilters) {
        // Check if there are actual filter values (not just empty defaults)
        const hasActiveFilters = savedFilters.mediaType !== 'all' ||
            savedFilters.yearFrom || savedFilters.yearTo ||
            savedFilters.releasedWithin || savedFilters.upcomingDays ||
            savedFilters.tmdbRatingMin > 0 || savedFilters.tmdbRatingMax < 10 ||
            savedFilters.imdbRatingMin > 0 || savedFilters.imdbRatingMax < 10 ||
            savedFilters.tmdbVotesMin > 0 || savedFilters.imdbVotesMin > 0 ||
            (savedFilters.selectedGenres && savedFilters.selectedGenres.length > 0) ||
            (savedFilters.excludedGenres && savedFilters.excludedGenres.length > 0) ||
            (savedFilters.selectedKeywords && savedFilters.selectedKeywords.length > 0) ||
            (savedFilters.excludedKeywords && savedFilters.excludedKeywords.length > 0) ||
            (savedFilters.selectedLanguages && savedFilters.selectedLanguages.length > 0) ||
            (savedFilters.excludedLanguages && savedFilters.excludedLanguages.length > 0) ||
            (savedFilters.selectedCountries && savedFilters.selectedCountries.length > 0) ||
            (savedFilters.excludedCountries && savedFilters.excludedCountries.length > 0) ||
            (savedFilters.selectedProviders && savedFilters.selectedProviders.length > 0) ||
            (savedFilters.excludedProviders && savedFilters.excludedProviders.length > 0) ||
            (savedFilters.selectedNetworks && savedFilters.selectedNetworks.length > 0) ||
            (savedFilters.excludedNetworks && savedFilters.excludedNetworks.length > 0) ||
            (savedFilters.selectedCompanies && savedFilters.selectedCompanies.length > 0) ||
            (savedFilters.excludedCompanies && savedFilters.excludedCompanies.length > 0) ||
            savedFilters.runtimeMin > 0 || savedFilters.runtimeMax < 300 ||
            savedFilters.certificationMin || savedFilters.certificationMax;
        
        if (hasActiveFilters) {
            // Merge saved filters with current state (preserving any new filter properties)
            window.discoverState.filters = {
                ...window.discoverState.filters,
                ...savedFilters
            };
            hasRestoredFilters = true;
        } else {
            clearSavedFilters();
        }
    }

    // Pass the flag to initializeElements
    window.discoverState.hasRestoredFilters = hasRestoredFilters;

    initializeElements();
    bindEvents();
    loadGenres();

    // Check for URL search parameters (from external searches like mobile nav, statistics page)
    // Reuse urlParams from above special entry check
    const searchQuery = urlParams.get('q');
    const imdbId = urlParams.get('imdb_id');
    const tmdbId = urlParams.get('tmdb_id');

    if (imdbId || tmdbId || searchQuery) {
        // Determine the search term to use
        const searchTerm = imdbId || (tmdbId ? `tmdb:${tmdbId}` : searchQuery);

        // Set the search input value
        if (searchInput) {
            searchInput.value = searchTerm;
        }

        // Trigger the search
        handleSearch();

        // Clean up URL (remove search params) to avoid re-triggering on refresh
        if (window.history.replaceState) {
            const cleanUrl = window.location.pathname;
            window.history.replaceState({}, document.title, cleanUrl);
        }
    } else {
   // Check for saved FlixPatrol or MDBList selections
        const savedFlixPatrol = localStorage.getItem('discoverFlixPatrol');
        const savedMDBList = localStorage.getItem('discoverMDBList');
        
        if (savedFlixPatrol || savedMDBList || hasRestoredFilters) {
            // Hide trending, show search results area immediately
            const trendingContent = document.getElementById('trending-content');
            const searchResults = document.getElementById('search-results');
            if (trendingContent) trendingContent.style.display = 'none';
            if (searchResults) searchResults.style.display = 'block';
        } else {
            // Only load trending if nothing is saved
            loadTrending();
        }
    }
});

/**
 * Initialize DOM elements
 */
function initializeElements() {
    // Search elements
    searchInput = document.getElementById('search-input');
    searchClearBtn = document.getElementById('search-clear-btn');
    
    // Filter elements
    filterToggleBtn = document.getElementById('filter-toggle-btn');
    filterDrawer = document.getElementById('filter-drawer');
    filterOverlay = document.getElementById('filter-overlay');
    filterCloseBtn = document.getElementById('filter-close-btn');
    
    // Tab elements
    tabButtons = document.querySelectorAll('.tab-btn');
    
    // Advanced Filter Elements
    const state = window.discoverState;

    // Sort Controls - native select + toggle button
    state.sortBySelect = document.getElementById('sort-by');
    state.sortOrderToggle = document.getElementById('sort-order-toggle');

    // Basic Filters
    state.yearFromInput = document.getElementById('year-from');
    state.yearToInput = document.getElementById('year-to');
    state.releasedWithinInput = document.getElementById('released-within');
    state.upcomingDaysInput = document.getElementById('upcoming-days');

    // Rating & Vote Filters
    state.tmdbRatingSlider = document.getElementById('tmdb-rating-slider');
    state.tmdbRatingMinInput = document.getElementById('tmdb-rating-min');
    state.tmdbRatingMaxInput = document.getElementById('tmdb-rating-max');

    state.imdbRatingSlider = document.getElementById('imdb-rating-slider');
    state.imdbRatingMinInput = document.getElementById('imdb-rating-min');
    state.imdbRatingMaxInput = document.getElementById('imdb-rating-max');

    state.tmdbVotesSlider = document.getElementById('tmdb-votes-slider');
    state.tmdbVotesMinInput = document.getElementById('tmdb-votes-min');

    state.imdbVotesSlider = document.getElementById('imdb-votes-slider');
    state.imdbVotesMinInput = document.getElementById('imdb-votes-min');

    // Chips Input Containers (MDBlist Style)
    state.genresContainer = document.getElementById('genres-container');
    state.languageContainer = document.getElementById('language-container');
    state.countryContainer = document.getElementById('country-container');
    state.networkContainer = document.getElementById('network-container');
    state.companyContainer = document.getElementById('company-container');

    state.runtimeMinInput = document.getElementById('runtime-min');
    state.runtimeMaxInput = document.getElementById('runtime-max');

    // Active Filters
    state.activeFiltersSection = document.getElementById('active-filters-section');
    state.activeFiltersContainer = document.getElementById('active-filters-container');
    
    // Filter actions
    state.clearFiltersBtn = document.getElementById('clear-filters-btn');
    state.applyFiltersBtn = document.getElementById('apply-filters-btn');
    
    // Results elements
    resultsGrid = document.getElementById('results-grid');
    loadingState = document.getElementById('loading-state');
    emptyState = document.getElementById('empty-state');
    errorState = document.getElementById('error-state');
    pagination = document.getElementById('pagination');
    loadMoreBtn = document.getElementById('load-more-btn');
    
    // Category grid elements
    trendingContent = document.getElementById('trending-content');
    searchResults = document.getElementById('search-results');
    moviesGrid = document.getElementById('movies-grid');
    showsGrid = document.getElementById('shows-grid');
    animeGrid = document.getElementById('anime-grid');

    // Only restore UI if filters were actually loaded from storage
    if (window.discoverState.hasRestoredFilters) {
        restoreFilterUI();
    }
}


/**
 * Bind event listeners
 */
function bindEvents() {
    // Search functionality
    if (searchInput) {
        searchInput.addEventListener('input', debounce(handleSearch, 300));
        searchInput.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                clearSearch();
            }
        });
    }

    if (searchClearBtn) {
        searchClearBtn.addEventListener('click', clearSearch);
    }

    // Filter drawer
    if (filterToggleBtn) {
        filterToggleBtn.addEventListener('click', openFilters);
    }

    if (filterCloseBtn) {
        filterCloseBtn.addEventListener('click', closeFilters);
    }

    if (filterOverlay) {
        filterOverlay.addEventListener('click', closeFilters);
    }

    // Close dropdowns when filter drawer scrolls (since they use fixed positioning)
    if (filterDrawer) {
        filterDrawer.addEventListener('scroll', () => {
            document.querySelectorAll('.chips-dropdown.show').forEach(dropdown => {
                dropdown.classList.remove('show');
            });
        });
    }

    // Escape key to close filter drawer
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            closeFilters();
        }
    });

    // Clear Filters button in header
    const clearFiltersHeaderBtn = document.getElementById('clear-filters-btn-header');
    if (clearFiltersHeaderBtn) {
        clearFiltersHeaderBtn.addEventListener('click', clearFiltersAndReload);
    }

    // Collapsible filter sections (accordion behavior)
    const filterSections = document.querySelectorAll('.filter-section:not(.sort-section)');
    filterSections.forEach(section => {
        const header = section.querySelector('.filter-section-header');
        if (header) {
            header.addEventListener('click', function() {
                toggleFilterSection(section);
            });
        }
    });

    // Tab navigation
    tabButtons.forEach(btn => {
        btn.addEventListener('click', function() {
            const tab = this.dataset.tab;
            switchTab(tab);
        });
    });

    // Infinite scroll - load more when user scrolls near bottom
    window.addEventListener('scroll', handleInfiniteScroll);

    const state = window.discoverState;

    // Sort & Search Controls
    if (state.sortByDropdown) {
        state.sortByDropdown.addEventListener('change', function() {
            state.filters.sortBy = this.value;
            updateActiveFilters();
        });
    }

    if (state.sortOrderDropdown) {
        state.sortOrderDropdown.addEventListener('change', function() {
            state.filters.sortOrder = this.value;
            updateActiveFilters();
        });
    }
    
    // Basic Filter Controls
    if (state.yearFromInput) {
        state.yearFromInput.addEventListener('input', function() {
            state.filters.yearFrom = parseInt(this.value) || 1900;
            updateActiveFilters();
        });
    }
    
    if (state.yearToInput) {
        state.yearToInput.addEventListener('input', function() {
            state.filters.yearTo = parseInt(this.value) || new Date().getFullYear();
            updateActiveFilters();
        });
    }
    
    if (state.releasedWithinInput) {
        state.releasedWithinInput.addEventListener('input', function() {
            state.filters.releasedWithin = this.value;
            updateActiveFilters();
        });
    }

    if (state.upcomingDaysInput) {
        state.upcomingDaysInput.addEventListener('input', function() {
            state.filters.upcomingDays = this.value;
            updateActiveFilters();
        });
    }
    
    // Rating & Vote Controls
    if (state.tmdbRatingMinInput) {
        state.tmdbRatingMinInput.addEventListener('input', function() {
            state.filters.tmdbRatingMin = parseFloat(this.value) || 0;
            updateActiveFilters();
        });
    }
    
    if (state.tmdbRatingMaxInput) {
        state.tmdbRatingMaxInput.addEventListener('input', function() {
            state.filters.tmdbRatingMax = parseFloat(this.value) || 10;
            updateActiveFilters();
        });
    }
    
    if (state.imdbRatingMinInput) {
        state.imdbRatingMinInput.addEventListener('input', function() {
            state.filters.imdbRatingMin = parseFloat(this.value) || 0;
            updateActiveFilters();
        });
    }
    
    if (state.imdbRatingMaxInput) {
        state.imdbRatingMaxInput.addEventListener('input', function() {
            state.filters.imdbRatingMax = parseFloat(this.value) || 10;
            updateActiveFilters();
        });
    }
    
    if (state.tmdbVotesMinInput) {
        state.tmdbVotesMinInput.addEventListener('input', function() {
            state.filters.tmdbVotesMin = parseInt(this.value) || 0;
            updateActiveFilters();
        });
    }
    
    if (state.imdbVotesMinInput) {
        state.imdbVotesMinInput.addEventListener('input', function() {
            state.filters.imdbVotesMin = parseInt(this.value) || 0;
            updateActiveFilters();
        });
    }
    
    // Runtime Controls
    if (state.runtimeMinInput) {
        state.runtimeMinInput.addEventListener('input', function() {
            state.filters.runtimeMin = parseInt(this.value) || 0;
            updateActiveFilters();
        });
    }
    
    if (state.runtimeMaxInput) {
        state.runtimeMaxInput.addEventListener('input', function() {
            state.filters.runtimeMax = parseInt(this.value) || 300;
            updateActiveFilters();
        });
    }

    // Media type filters (existing)
    document.addEventListener('click', function(e) {
        if (e.target.matches('[data-filter="type"]')) {
            setMediaType(e.target.dataset.value);
        }
    });
    
    // Sort options (existing)
    document.addEventListener('click', function(e) {
        if (e.target.matches('[data-filter="sort"]')) {
            setSortBy(e.target.dataset.value);
        }
    });

    // Load more (existing)
    if (loadMoreBtn) {
        loadMoreBtn.addEventListener('click', loadMore);
    }
    
    // Filter actions
    if (state.clearFiltersBtn) {
        state.clearFiltersBtn.addEventListener('click', clearAllFilters);
    }
    
    if (state.applyFiltersBtn) {
        state.applyFiltersBtn.addEventListener('click', applyAdvancedFilters);
    }

    // Include video checkbox
    const includeVideoCheckbox = document.getElementById('include-video');
    if (includeVideoCheckbox) {
        includeVideoCheckbox.addEventListener('change', (e) => {
            state.filters.includeVideo = e.target.checked;
            applyAdvancedFilters(false); // Keep sidebar open
        });
    }

    // Title filter input (client-side filtering)
    const titleFilterInput = document.getElementById('title-filter');
    if (titleFilterInput) {
        titleFilterInput.addEventListener('input', debounce((e) => {
            state.filters.titleFilter = e.target.value.trim();
            // Re-render current results with new filter (client-side only, no API call)
            if (window.discoverState && window.discoverState.currentResults) {
                renderResults(window.discoverState.currentResults);
            }
        }, 300));
    }
    
    // Initialize UI components
    initializeSemanticUI();

    // Initialize range sliders
    initializeRangeSliders();
}

/**
 * Initialize filter controls (Sort, Chips inputs)
 */
function initializeSemanticUI() {
    const state = window.discoverState;

    // Initialize Sort Select
    if (state.sortBySelect) {
        state.sortBySelect.addEventListener('change', (e) => {
            state.filters.sortBy = e.target.value;
            updateActiveFilters();
        });
    }

    // Initialize Sort Order Toggle
    if (state.sortOrderToggle) {
        state.sortOrderToggle.addEventListener('click', () => {
            const currentOrder = state.sortOrderToggle.getAttribute('data-order');
            const newOrder = currentOrder === 'desc' ? 'asc' : 'desc';
            state.sortOrderToggle.setAttribute('data-order', newOrder);
            state.filters.sortOrder = newOrder;

            // Toggle icon visibility
            const descIcon = state.sortOrderToggle.querySelector('.sort-icon-desc');
            const ascIcon = state.sortOrderToggle.querySelector('.sort-icon-asc');
            if (descIcon) descIcon.style.display = newOrder === 'desc' ? 'block' : 'none';
            if (ascIcon) ascIcon.style.display = newOrder === 'asc' ? 'block' : 'none';

            updateActiveFilters();
        });
    }

    // Initialize Watch Region select
    const watchRegionSelect = document.getElementById('watch-region');
    if (watchRegionSelect) {
        watchRegionSelect.addEventListener('change', (e) => {
            state.filters.watchRegion = e.target.value;
            loadCertifications(e.target.value);
            updateActiveFilters();
        });
        // Load certifications for initial region on page load
        loadCertifications(watchRegionSelect.value);
    }

    // Initialize all chips input containers with include/exclude support
    initializeChipsInput('genres', state.filters.selectedGenres, state.filters.excludedGenres, () => updateActiveFilters());
    initializeChipsInput('language', state.filters.selectedLanguages, state.filters.excludedLanguages, () => updateActiveFilters());
    initializeChipsInput('country', state.filters.selectedCountries, state.filters.excludedCountries, () => updateActiveFilters());
    initializeChipsInput('provider', state.filters.selectedProviders, state.filters.excludedProviders, () => updateActiveFilters());
    initializeChipsInput('network', state.filters.selectedNetworks, state.filters.excludedNetworks, () => updateActiveFilters());

    // Initialize certification range selects
    const certMinSelect = document.getElementById('certification-min-select');
    const certMaxSelect = document.getElementById('certification-max-select');
    if (certMinSelect) {
        certMinSelect.addEventListener('change', function() {
            state.filters.certificationMin = this.value;
            updateActiveFilters();
        });
    }
    if (certMaxSelect) {
        certMaxSelect.addEventListener('change', function() {
            state.filters.certificationMax = this.value;
            updateActiveFilters();
        });
    }

    // Initialize keyword filter with dynamic search
    initializeKeywordFilter();
    
    // Initialize company filter with dynamic search
    initializeCompanyFilter();
}

/**
 * Load certifications based on selected watch region into range selects
 */
async function loadCertifications(region) {
    const certMinSelect = document.getElementById('certification-min-select');
    const certMaxSelect = document.getElementById('certification-max-select');
    if (!certMinSelect || !certMaxSelect) return;

    try {
        let mediaType = window.discoverState.filters.mediaType || 'movie';

        // For 'all', fetch both movie and TV certifications
        if (mediaType === 'all') {
            // Fetch both movie and TV certifications and combine them
            const [movieResponse, tvResponse] = await Promise.all([
                fetch(`/discover/api/certifications?region=${region}&type=movie`),
                fetch(`/discover/api/certifications?region=${region}&type=tv`)
            ]);

            const movieData = movieResponse.ok ? await movieResponse.json() : { certifications: [] };
            const tvData = tvResponse.ok ? await tvResponse.json() : { certifications: [] };

            const movieCerts = (movieData.certifications || []).map(c => ({ ...c, type: 'Movie' }));
            const tvCerts = (tvData.certifications || []).map(c => ({ ...c, type: 'TV' }));

            // Combine and sort by order
            const allCertifications = [...movieCerts, ...tvCerts].sort((a, b) => a.order - b.order);

            // Populate both selects
            populateCertificationSelects(certMinSelect, certMaxSelect, allCertifications, true);

        } else {
            // Single media type
            const response = await fetch(`/discover/api/certifications?region=${region}&type=${mediaType}`);

            if (!response.ok) {
                console.warn('[Discover] Failed to load certifications');
                return;
            }

            const data = await response.json();
            const certifications = (data.certifications || []).sort((a, b) => a.order - b.order);

            // Populate both selects
            populateCertificationSelects(certMinSelect, certMaxSelect, certifications, false);
        }

    } catch (error) {
        console.error('[Discover] Error loading certifications:', error);
    }
}

/**
 * Populate certification select dropdowns
 */
function populateCertificationSelects(minSelect, maxSelect, certifications, showType) {
    const state = window.discoverState;

    // Save current selections
    const currentMin = state.filters.certificationMin;
    const currentMax = state.filters.certificationMax;

    // Blur and reset selects to prevent dropdown state issues
    minSelect.blur();
    maxSelect.blur();

    // Clear existing options except "Any"
    minSelect.innerHTML = '<option value="">Any</option>';
    maxSelect.innerHTML = '<option value="">Any</option>';

    // Populate options
    certifications.forEach(cert => {
        const label = showType ? `${cert.certification} (${cert.type})` : cert.certification;

        const minOption = document.createElement('option');
        minOption.value = cert.certification;
        minOption.textContent = label;
        minSelect.appendChild(minOption);

        const maxOption = document.createElement('option');
        maxOption.value = cert.certification;
        maxOption.textContent = label;
        maxSelect.appendChild(maxOption);
    });

    // Restore selections if they still exist in new list
    if (currentMin && Array.from(minSelect.options).some(opt => opt.value === currentMin)) {
        minSelect.value = currentMin;
    }
    if (currentMax && Array.from(maxSelect.options).some(opt => opt.value === currentMax)) {
        maxSelect.value = currentMax;
    }
}

/**
 * Initialize keyword filter with dynamic API search
 */
function initializeKeywordFilter() {
    const container = document.getElementById('keyword-container');
    if (!container) return;

    const chipsWrapper = container.querySelector('#keyword-chips');
    const searchInput = container.querySelector('#keyword-search');
    const dropdown = container.querySelector('#keyword-dropdown');
    const dropdownToggle = container.querySelector('#keyword-dropdown-toggle');

    if (!chipsWrapper || !searchInput || !dropdown) return;

    let searchTimeout = null;

    // Toggle dropdown on button click
    if (dropdownToggle) {
        dropdownToggle.addEventListener('click', (e) => {
            e.stopPropagation();
            const isShowing = dropdown.classList.toggle('show');
            if (isShowing) {
                positionDropdown(dropdown, container);
            }
        });
    }

    // Show dropdown on input focus
    searchInput.addEventListener('focus', () => {
        dropdown.classList.add('show');
        positionDropdown(dropdown, container);
    });

    // Search keywords on input with debounce
    searchInput.addEventListener('input', (e) => {
        const query = e.target.value.trim();

        if (searchTimeout) clearTimeout(searchTimeout);

        if (query.length < 2) {
            dropdown.innerHTML = '<div class="chips-dropdown-empty">Type to search keywords...</div>';
            return;
        }

        dropdown.innerHTML = '<div class="chips-dropdown-empty">Searching...</div>';
        dropdown.classList.add('show');
        positionDropdown(dropdown, container);

        searchTimeout = setTimeout(async () => {
            try {
                const response = await fetch(`/discover/api/keywords?query=${encodeURIComponent(query)}`);
                if (!response.ok) throw new Error('Search failed');

                const data = await response.json();
                renderKeywordDropdown(data.keywords || [], dropdown, chipsWrapper);
                positionDropdown(dropdown, container);
            } catch (error) {
                console.error('[Discover] Keyword search error:', error);
                dropdown.innerHTML = '<div class="chips-dropdown-empty">Search failed</div>';
            }
        }, 300);
    });

    // Close dropdown when clicking outside
    document.addEventListener('click', (e) => {
        if (!container.contains(e.target)) {
            dropdown.classList.remove('show');
        }
    });
}

/**
 * Render keyword search results in dropdown
 */
function renderKeywordDropdown(keywords, dropdown, chipsWrapper) {
    const state = window.discoverState;

    if (keywords.length === 0) {
        dropdown.innerHTML = '<div class="chips-dropdown-empty">No keywords found</div>';
        return;
    }

    dropdown.innerHTML = '';

    keywords.forEach(keyword => {
        const item = document.createElement('div');
        item.className = 'chips-dropdown-item';
        item.setAttribute('data-value', keyword.id.toString());

        // Check if already selected/excluded
        if (state.filters.selectedKeywords.includes(keyword.id.toString())) {
            item.classList.add('included');
        } else if (state.filters.excludedKeywords.includes(keyword.id.toString())) {
            item.classList.add('excluded');
        }

        item.innerHTML = `
            <span class="chips-item-label">${keyword.name}</span>
            <div class="chips-item-actions">
                <button type="button" class="chips-include-btn" title="Include">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4" />
                    </svg>
                </button>
                <button type="button" class="chips-exclude-btn" title="Exclude">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M20 12H4" />
                    </svg>
                </button>
            </div>
        `;

        // Include button
        item.querySelector('.chips-include-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            toggleKeyword(keyword.id.toString(), keyword.name, 'include', item, chipsWrapper);
        });

        // Exclude button
        item.querySelector('.chips-exclude-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            toggleKeyword(keyword.id.toString(), keyword.name, 'exclude', item, chipsWrapper);
        });

        dropdown.appendChild(item);
    });
}

/**
 * Toggle keyword selection (include/exclude)
 */
function toggleKeyword(keywordId, keywordName, action, dropdownItem, chipsWrapper) {
    const state = window.discoverState;
    const selectedArray = state.filters.selectedKeywords;
    const excludedArray = state.filters.excludedKeywords;

    // Cache the keyword name for display
    state.filters.keywordCache[keywordId] = keywordName;

    // Remove from both arrays first
    const selectedIdx = selectedArray.indexOf(keywordId);
    const excludedIdx = excludedArray.indexOf(keywordId);
    if (selectedIdx > -1) selectedArray.splice(selectedIdx, 1);
    if (excludedIdx > -1) excludedArray.splice(excludedIdx, 1);

    // Update dropdown item state
    dropdownItem.classList.remove('included', 'excluded');

    if (action === 'include' && selectedIdx === -1) {
        selectedArray.push(keywordId);
        dropdownItem.classList.add('included');
    } else if (action === 'exclude' && excludedIdx === -1) {
        excludedArray.push(keywordId);
        dropdownItem.classList.add('excluded');
    }

    // Re-render chips
    renderKeywordChips(chipsWrapper);
    updateActiveFilters();
}

/**
 * Render keyword chips
 */
function renderKeywordChips(chipsWrapper) {
    const state = window.discoverState;
    chipsWrapper.innerHTML = '';

    // Render included keywords
    state.filters.selectedKeywords.forEach(keywordId => {
        const name = state.filters.keywordCache[keywordId] || keywordId;
        const chip = document.createElement('span');
        chip.className = 'chip chip-include';
        chip.setAttribute('data-value', keywordId);
        chip.innerHTML = `<span class="chip-icon">+</span>${name} <button type="button" class="chip-remove">&times;</button>`;
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            const idx = state.filters.selectedKeywords.indexOf(keywordId);
            if (idx > -1) state.filters.selectedKeywords.splice(idx, 1);
            renderKeywordChips(chipsWrapper);
            updateActiveFilters();
        });
        chipsWrapper.appendChild(chip);
    });

    // Render excluded keywords
    state.filters.excludedKeywords.forEach(keywordId => {
        const name = state.filters.keywordCache[keywordId] || keywordId;
        const chip = document.createElement('span');
        chip.className = 'chip chip-exclude';
        chip.setAttribute('data-value', keywordId);
        chip.innerHTML = `<span class="chip-icon">-</span>${name} <button type="button" class="chip-remove">&times;</button>`;
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            const idx = state.filters.excludedKeywords.indexOf(keywordId);
            if (idx > -1) state.filters.excludedKeywords.splice(idx, 1);
            renderKeywordChips(chipsWrapper);
            updateActiveFilters();
        });
        chipsWrapper.appendChild(chip);
    });
}

/**
 * Position dropdown - now uses CSS absolute positioning, this is just a no-op placeholder
 * CSS handles: position: absolute; top: 100%; left: 0; right: 0;
 */
function positionDropdown(dropdown, container) {
    // Positioning is now handled entirely by CSS
    // This function is kept for compatibility with existing calls
}

/**
 * Initialize company filter with dynamic API search
 */
function initializeCompanyFilter() {
    const container = document.getElementById('company-container');
    if (!container) return;

    const chipsWrapper = container.querySelector('#company-chips');
    const searchInput = container.querySelector('#company-search');
    const dropdown = container.querySelector('#company-dropdown');
    const dropdownToggle = container.querySelector('#company-dropdown-toggle');

    if (!chipsWrapper || !searchInput || !dropdown) return;

    let searchTimeout = null;

    // Toggle dropdown on button click
    if (dropdownToggle) {
        dropdownToggle.addEventListener('click', (e) => {
            e.stopPropagation();
            const isShowing = dropdown.classList.toggle('show');
            if (isShowing && searchInput.value.trim().length >= 2) {
                positionDropdown(dropdown, container);
            }
        });
    }

    // Search companies on input with debounce
    searchInput.addEventListener('input', (e) => {
        const query = e.target.value.trim();

        if (searchTimeout) clearTimeout(searchTimeout);

        if (query.length < 2) {
            dropdown.innerHTML = '<div class="chips-dropdown-empty">Type to search companies...</div>';
            return;
        }

        dropdown.innerHTML = '<div class="chips-dropdown-empty">Searching...</div>';
        dropdown.classList.add('show');
        positionDropdown(dropdown, container);

        searchTimeout = setTimeout(async () => {
            try {
                const response = await fetch(`/discover/api/companies?query=${encodeURIComponent(query)}`);
                if (!response.ok) throw new Error('Search failed');

                const data = await response.json();
                renderCompanyDropdown(data.companies || [], dropdown, chipsWrapper);
                positionDropdown(dropdown, container);
            } catch (error) {
                console.error('[Discover] Company search error:', error);
                dropdown.innerHTML = '<div class="chips-dropdown-empty">Search failed</div>';
            }
        }, 300);
    });

    // Close dropdown when clicking outside
    document.addEventListener('click', (e) => {
        if (!container.contains(e.target)) {
            dropdown.classList.remove('show');
        }
    });
}

/**
 * Render company search results in dropdown
 */
function renderCompanyDropdown(companies, dropdown, chipsWrapper) {
    const state = window.discoverState;

    if (companies.length === 0) {
        dropdown.innerHTML = '<div class="chips-dropdown-empty">No companies found</div>';
        return;
    }

    dropdown.innerHTML = '';

    companies.forEach(company => {
        const item = document.createElement('div');
        item.className = 'chips-dropdown-item';
        item.setAttribute('data-value', company.id.toString());

        // Check if already selected/excluded
        if (state.filters.selectedCompanies.includes(company.id.toString())) {
            item.classList.add('included');
        } else if (state.filters.excludedCompanies.includes(company.id.toString())) {
            item.classList.add('excluded');
        }

        item.innerHTML = `
            <span class="chips-item-label">${company.name}</span>
            <div class="chips-item-actions">
                <button type="button" class="chips-include-btn" title="Include">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4" />
                    </svg>
                </button>
                <button type="button" class="chips-exclude-btn" title="Exclude">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M20 12H4" />
                    </svg>
                </button>
            </div>
        `;

        // Include button
        item.querySelector('.chips-include-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            toggleCompany(company.id.toString(), company.name, 'include', item, chipsWrapper);
        });

        // Exclude button
        item.querySelector('.chips-exclude-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            toggleCompany(company.id.toString(), company.name, 'exclude', item, chipsWrapper);
        });

        dropdown.appendChild(item);
    });
}

/**
 * Toggle company selection (include/exclude)
 */
function toggleCompany(companyId, companyName, action, dropdownItem, chipsWrapper) {
    const state = window.discoverState;
    const selectedArray = state.filters.selectedCompanies;
    const excludedArray = state.filters.excludedCompanies;

    // Cache the company name for display
    state.filters.companyCache[companyId] = companyName;

    // Remove from both arrays first
    const selectedIdx = selectedArray.indexOf(companyId);
    const excludedIdx = excludedArray.indexOf(companyId);
    if (selectedIdx > -1) selectedArray.splice(selectedIdx, 1);
    if (excludedIdx > -1) excludedArray.splice(excludedIdx, 1);

    // Update dropdown item state
    dropdownItem.classList.remove('included', 'excluded');

    if (action === 'include' && selectedIdx === -1) {
        selectedArray.push(companyId);
        dropdownItem.classList.add('included');
    } else if (action === 'exclude' && excludedIdx === -1) {
        excludedArray.push(companyId);
        dropdownItem.classList.add('excluded');
    }

    // Re-render chips
    renderCompanyChips(chipsWrapper);
    updateActiveFilters();
}

/**
 * Render company chips
 */
function renderCompanyChips(chipsWrapper) {
    const state = window.discoverState;
    chipsWrapper.innerHTML = '';

    // Render included companies
    state.filters.selectedCompanies.forEach(companyId => {
        const name = state.filters.companyCache[companyId] || companyId;
        const chip = document.createElement('span');
        chip.className = 'chip included';
        chip.setAttribute('data-value', companyId);
        chip.innerHTML = `${name} <button type="button" class="chip-remove">&times;</button>`;
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            const idx = state.filters.selectedCompanies.indexOf(companyId);
            if (idx > -1) state.filters.selectedCompanies.splice(idx, 1);
            renderCompanyChips(chipsWrapper);
            updateActiveFilters();
        });
        chipsWrapper.appendChild(chip);
    });

    // Render excluded companies
    state.filters.excludedCompanies.forEach(companyId => {
        const name = state.filters.companyCache[companyId] || companyId;
        const chip = document.createElement('span');
        chip.className = 'chip excluded';
        chip.setAttribute('data-value', companyId);
        chip.innerHTML = `${name} <button type="button" class="chip-remove">&times;</button>`;
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            const idx = state.filters.excludedCompanies.indexOf(companyId);
            if (idx > -1) state.filters.excludedCompanies.splice(idx, 1);
            renderCompanyChips(chipsWrapper);
            updateActiveFilters();
        });
        chipsWrapper.appendChild(chip);
    });
}

/**
 * Initialize MDBlist-style chips input with include/exclude support
 */
function initializeChipsInput(name, includeArray, excludeArray, onChange) {
    const container = document.getElementById(`${name}-container`);
    if (!container) {
        console.warn(`[Discover] Chips container not found: ${name}-container`);
        return;
    }

    const chipsWrapper = container.querySelector(`#${name}-chips`);
    const searchInput = container.querySelector(`#${name}-search`);
    const dropdown = container.querySelector(`#${name}-dropdown`);
    const dropdownToggle = container.querySelector(`#${name}-dropdown-toggle`);
    const hiddenInput = document.getElementById(`selected-${name}`) || document.getElementById(`selected-${name}s`);

    if (!chipsWrapper || !searchInput || !dropdown) {
        console.warn(`[Discover] Missing elements for ${name}:`, { chipsWrapper: !!chipsWrapper, searchInput: !!searchInput, dropdown: !!dropdown });
        return;
    }

    console.log(`[Discover] Initializing chips input: ${name}`);

    // Add +/- buttons to dropdown items
    setupDropdownItemButtons(dropdown);

    // Toggle dropdown on button click
    if (dropdownToggle) {
        dropdownToggle.addEventListener('click', (e) => {
            e.stopPropagation();
            const isShowing = dropdown.classList.toggle('show');
            if (isShowing) {
                positionDropdown(dropdown, container);
            }
        });
    }

    // Show dropdown on input focus
    searchInput.addEventListener('focus', () => {
        dropdown.classList.add('show');
        positionDropdown(dropdown, container);
    });

    // Filter dropdown items as user types
    searchInput.addEventListener('input', (e) => {
        const query = e.target.value.toLowerCase();
        const items = dropdown.querySelectorAll('.chips-dropdown-item');
        items.forEach(item => {
            const labelText = item.querySelector('.chips-item-label');
            const text = labelText ? labelText.textContent.toLowerCase() : item.textContent.toLowerCase();
            const value = item.getAttribute('data-value');
            if (value === 'match_all' || text.includes(query)) {
                item.classList.remove('hidden');
            } else {
                item.classList.add('hidden');
            }
        });
        dropdown.classList.add('show');
        positionDropdown(dropdown, container);
    });

    // Handle clicks on dropdown items - either on +/- buttons or on the item itself
    dropdown.addEventListener('click', (e) => {
        const includeBtn = e.target.closest('.chips-include-btn');
        const excludeBtn = e.target.closest('.chips-exclude-btn');
        const item = e.target.closest('.chips-dropdown-item');

        if (!item) return;

        const value = item.getAttribute('data-value');
        if (value === 'match_all') return;

        e.stopPropagation();

        // Determine action: exclude button = exclude, otherwise include (button or item click)
        const isExclude = excludeBtn && !container.classList.contains('include-only');
        const isInclude = includeBtn || (!excludeBtn && !isExclude);

        if (isInclude) {
            // Remove from exclude if present
            const excludeIdx = excludeArray.indexOf(value);
            if (excludeIdx > -1) excludeArray.splice(excludeIdx, 1);

            // Toggle include
            const includeIdx = includeArray.indexOf(value);
            if (includeIdx === -1) {
                includeArray.push(value);
                item.classList.add('included');
                item.classList.remove('excluded');
            } else {
                includeArray.splice(includeIdx, 1);
                item.classList.remove('included');
            }
        } else if (isExclude) {
            // Remove from include if present
            const includeIdx = includeArray.indexOf(value);
            if (includeIdx > -1) includeArray.splice(includeIdx, 1);

            // Toggle exclude
            const excludeIdx = excludeArray.indexOf(value);
            if (excludeIdx === -1) {
                excludeArray.push(value);
                item.classList.add('excluded');
                item.classList.remove('included');
            } else {
                excludeArray.splice(excludeIdx, 1);
                item.classList.remove('excluded');
            }
        }

        // Update chips display
        renderChips(name, chipsWrapper, includeArray, excludeArray, dropdown, hiddenInput, onChange);

        // Update hidden input (include only for backward compatibility)
        if (hiddenInput) hiddenInput.value = includeArray.join(',');

        // Clear search
        searchInput.value = '';

        // Callback
        onChange();
    });

    // Close dropdown when clicking outside
    document.addEventListener('click', (e) => {
        if (!container.contains(e.target)) {
            dropdown.classList.remove('show');
        }
    });

    // Focus search input when clicking container
    container.addEventListener('click', (e) => {
        if (!e.target.closest('.chip') && !e.target.closest('.chips-dropdown-toggle')) {
            searchInput.focus();
        }
    });
}

/**
 * Setup +/- buttons on dropdown items
 */
function setupDropdownItemButtons(dropdown) {
    const items = dropdown.querySelectorAll('.chips-dropdown-item');
    items.forEach(item => {
        const value = item.getAttribute('data-value');
        if (value === 'match_all') return;

        // Skip if already has buttons
        if (item.querySelector('.chips-item-actions')) return;

        const label = item.textContent.trim();
        item.innerHTML = `
            <span class="chips-item-label">${label}</span>
            <div class="chips-item-actions">
                <button type="button" class="chips-include-btn" title="Include">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4" />
                    </svg>
                </button>
                <button type="button" class="chips-exclude-btn" title="Exclude">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M20 12H4" />
                    </svg>
                </button>
            </div>
        `;
    });
}

/**
 * Render chips in the wrapper with include/exclude support
 */
function renderChips(name, wrapper, includeArray, excludeArray, dropdown, hiddenInput, onChange) {
    // Clear existing chips
    wrapper.innerHTML = '';

    // Helper to get label from dropdown
    function getLabel(value) {
        const dropdownItem = dropdown.querySelector(`[data-value="${value}"]`);
        if (dropdownItem) {
            const labelEl = dropdownItem.querySelector('.chips-item-label');
            return labelEl ? labelEl.textContent : dropdownItem.textContent;
        }
        return value;
    }

    // Create chips for included values (green)
    includeArray.forEach(value => {
        const label = getLabel(value);
        const chip = document.createElement('div');
        chip.className = 'chip chip-include';
        chip.innerHTML = `
            <span class="chip-icon">+</span>
            <span>${label}</span>
            <button type="button" class="chip-remove" data-value="${value}" data-type="include">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                </svg>
            </button>
        `;

        chip.querySelector('.chip-remove').addEventListener('click', (e) => {
            e.stopPropagation();
            const idx = includeArray.indexOf(value);
            if (idx > -1) includeArray.splice(idx, 1);

            // Update dropdown item
            const dropdownItem = dropdown.querySelector(`[data-value="${value}"]`);
            if (dropdownItem) dropdownItem.classList.remove('included');

            if (hiddenInput) hiddenInput.value = includeArray.join(',');
            renderChips(name, wrapper, includeArray, excludeArray, dropdown, hiddenInput, onChange);
            onChange();
        });

        wrapper.appendChild(chip);
    });

    // Create chips for excluded values (red)
    excludeArray.forEach(value => {
        const label = getLabel(value);
        const chip = document.createElement('div');
        chip.className = 'chip chip-exclude';
        chip.innerHTML = `
            <span class="chip-icon">-</span>
            <span>${label}</span>
            <button type="button" class="chip-remove" data-value="${value}" data-type="exclude">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                </svg>
            </button>
        `;

        chip.querySelector('.chip-remove').addEventListener('click', (e) => {
            e.stopPropagation();
            const idx = excludeArray.indexOf(value);
            if (idx > -1) excludeArray.splice(idx, 1);

            // Update dropdown item
            const dropdownItem = dropdown.querySelector(`[data-value="${value}"]`);
            if (dropdownItem) dropdownItem.classList.remove('excluded');

            renderChips(name, wrapper, includeArray, excludeArray, dropdown, hiddenInput, onChange);
            onChange();
        });

        wrapper.appendChild(chip);
    });
}

/**
 * Clear a chips input by name (both include and exclude)
 */
function clearChipsInput(name) {
    const state = window.discoverState;
    const container = document.getElementById(`${name}-container`);
    if (!container) return;

    const chipsWrapper = container.querySelector(`#${name}-chips`);
    const dropdown = container.querySelector(`#${name}-dropdown`);
    const hiddenInput = document.getElementById(`selected-${name}`) || document.getElementById(`selected-${name}s`);

    // Clear both include and exclude filter arrays
    if (name === 'genres') {
        state.filters.selectedGenres = [];
        state.filters.excludedGenres = [];
    } else if (name === 'language') {
        state.filters.selectedLanguages = [];
        state.filters.excludedLanguages = [];
    } else if (name === 'country') {
        state.filters.selectedCountries = [];
        state.filters.excludedCountries = [];
    } else if (name === 'provider') {
        state.filters.selectedProviders = [];
        state.filters.excludedProviders = [];
    } else if (name === 'network') {
        state.filters.selectedNetworks = [];
        state.filters.excludedNetworks = [];
    } else if (name === 'company') {
        state.filters.selectedCompanies = [];
        state.filters.excludedCompanies = [];
    } else if (name === 'certification') {
        state.filters.selectedCertifications = [];
        state.filters.excludedCertifications = [];
    }

    // Clear chips display
    if (chipsWrapper) chipsWrapper.innerHTML = '';

    // Clear hidden input
    if (hiddenInput) hiddenInput.value = '';

    // Clear dropdown selection states (include, exclude, and legacy selected)
    if (dropdown) {
        dropdown.querySelectorAll('.chips-dropdown-item.selected, .chips-dropdown-item.included, .chips-dropdown-item.excluded').forEach(item => {
            item.classList.remove('selected', 'included', 'excluded');
        });
    }
}

/**
 * Legacy function - no longer used
 */
function initializeCustomDropdown() {
    // No longer used
}

/**
 * Legacy function - kept for compatibility
 */
function initializeMultiSelect(dropdownElement, onChange) {
    // No longer used - keeping for backwards compatibility
    const input = dropdownElement?.querySelector('input[type="hidden"]');
    const text = dropdownElement?.querySelector('.default.text');
    const menu = dropdownElement?.querySelector('.menu');
    const items = dropdownElement?.querySelectorAll('.item');

    if (!menu || !items?.length) return;

    let selectedValues = [];

    dropdownElement.addEventListener('click', (e) => {
        e.preventDefault();
        menu.classList.toggle('hidden');
    });

    items.forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const value = item.getAttribute('data-value');
            const isSelected = selectedValues.includes(value);
            
            if (isSelected) {
                selectedValues = selectedValues.filter(v => v !== value);
                item.classList.remove('selected');
            } else {
                selectedValues.push(value);
                item.classList.add('selected');
            }
            
            input.value = selectedValues.join(',');
            text.textContent = selectedValues.length > 0 ? `${selectedValues.length} selected` : 'Select genres';
            
            onChange(selectedValues);
        });
    });
    
    document.addEventListener('click', (e) => {
        if (!dropdownElement.contains(e.target)) {
            menu.classList.add('hidden');
        }
    });
}

/**
 * Initialize range sliders for ratings and votes
 */
function initializeRangeSliders() {
    const state = window.discoverState;

    // TMDB Rating Slider
    initializeDualSlider({
        sliderId: 'tmdb-rating-slider',
        minInputId: 'tmdb-rating-min',
        maxInputId: 'tmdb-rating-max',
        min: 0,
        max: 10,
        step: 0.1,
        onUpdate: (minVal, maxVal) => {
            state.filters.tmdbRatingMin = minVal;
            state.filters.tmdbRatingMax = maxVal;
            updateActiveFilters();
        }
    });

    // IMDb Rating Slider
    initializeDualSlider({
        sliderId: 'imdb-rating-slider',
        minInputId: 'imdb-rating-min',
        maxInputId: 'imdb-rating-max',
        min: 0,
        max: 10,
        step: 0.1,
        onUpdate: (minVal, maxVal) => {
            state.filters.imdbRatingMin = minVal;
            state.filters.imdbRatingMax = maxVal;
            updateActiveFilters();
        }
    });

    // TMDB Votes Slider (single handle)
    initializeSingleSlider({
        sliderId: 'tmdb-votes-slider',
        inputId: 'tmdb-votes-min',
        min: 0,
        max: 5000,
        step: 50,
        onUpdate: (val) => {
            state.filters.tmdbVotesMin = val;
            updateActiveFilters();
        }
    });

    // IMDb Votes Slider (single handle)
    initializeSingleSlider({
        sliderId: 'imdb-votes-slider',
        inputId: 'imdb-votes-min',
        min: 0,
        max: 50000,
        step: 100,
        onUpdate: (val) => {
            state.filters.imdbVotesMin = val;
            updateActiveFilters();
        }
    });
}

/**
 * Initialize dual-handle range slider
 */
function initializeDualSlider(options) {
    const slider = document.getElementById(options.sliderId);
    if (!slider) return;

    const minInput = document.getElementById(options.minInputId);
    const maxInput = document.getElementById(options.maxInputId);
    const inner = slider.querySelector('.inner');
    const trackFill = slider.querySelector('.track-fill');
    const thumbMin = slider.querySelector('.thumb');
    const thumbMax = slider.querySelector('.thumb.second');

    if (!inner || !trackFill || !thumbMin || !thumbMax || !minInput || !maxInput) return;

    let isDragging = null;

    const updateSliderUI = () => {
        const minVal = parseFloat(minInput.value) || options.min;
        const maxVal = parseFloat(maxInput.value) || options.max;
        const range = options.max - options.min;

        const minPercent = ((minVal - options.min) / range) * 100;
        const maxPercent = ((maxVal - options.min) / range) * 100;

        thumbMin.style.left = `calc(${minPercent}% - 0px)`;
        thumbMax.style.left = `calc(${maxPercent}% - 0px)`;
        trackFill.style.left = `${minPercent}%`;
        trackFill.style.right = `${100 - maxPercent}%`;
    };

    const getValueFromPosition = (clientX) => {
        const rect = inner.getBoundingClientRect();
        const percent = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
        let value = options.min + (percent * (options.max - options.min));
        value = Math.round(value / options.step) * options.step;
        return parseFloat(value.toFixed(1));
    };

    const handleMouseDown = (e, thumb) => {
        e.preventDefault();
        isDragging = thumb;
        document.addEventListener('mousemove', handleMouseMove);
        document.addEventListener('mouseup', handleMouseUp);
    };

    const handleMouseMove = (e) => {
        if (!isDragging) return;

        const value = getValueFromPosition(e.clientX);
        const minVal = parseFloat(minInput.value);
        const maxVal = parseFloat(maxInput.value);

        if (isDragging === 'min' && value < maxVal) {
            minInput.value = value;
        } else if (isDragging === 'max' && value > minVal) {
            maxInput.value = value;
        }

        updateSliderUI();
        options.onUpdate(parseFloat(minInput.value), parseFloat(maxInput.value));
    };

    const handleMouseUp = () => {
        isDragging = null;
        document.removeEventListener('mousemove', handleMouseMove);
        document.removeEventListener('mouseup', handleMouseUp);
        document.removeEventListener('touchmove', handleTouchMove);
        document.removeEventListener('touchend', handleTouchEnd);
    };

    // Touch event handlers for mobile
    const handleTouchStart = (e, thumb) => {
        e.preventDefault();
        isDragging = thumb;
        document.addEventListener('touchmove', handleTouchMove, { passive: false });
        document.addEventListener('touchend', handleTouchEnd);
    };

    const handleTouchMove = (e) => {
        if (!isDragging) return;
        e.preventDefault();

        const touch = e.touches[0];
        const value = getValueFromPosition(touch.clientX);
        const minVal = parseFloat(minInput.value);
        const maxVal = parseFloat(maxInput.value);

        if (isDragging === 'min' && value < maxVal) {
            minInput.value = value;
        } else if (isDragging === 'max' && value > minVal) {
            maxInput.value = value;
        }

        updateSliderUI();
        options.onUpdate(parseFloat(minInput.value), parseFloat(maxInput.value));
    };

    const handleTouchEnd = () => {
        isDragging = null;
        document.removeEventListener('touchmove', handleTouchMove);
        document.removeEventListener('touchend', handleTouchEnd);
    };

    thumbMin.addEventListener('mousedown', (e) => handleMouseDown(e, 'min'));
    thumbMax.addEventListener('mousedown', (e) => handleMouseDown(e, 'max'));
    thumbMin.addEventListener('touchstart', (e) => handleTouchStart(e, 'min'), { passive: false });
    thumbMax.addEventListener('touchstart', (e) => handleTouchStart(e, 'max'), { passive: false });

    // Sync input changes to slider
    minInput.addEventListener('input', () => {
        updateSliderUI();
        options.onUpdate(parseFloat(minInput.value) || options.min, parseFloat(maxInput.value) || options.max);
    });

    maxInput.addEventListener('input', () => {
        updateSliderUI();
        options.onUpdate(parseFloat(minInput.value) || options.min, parseFloat(maxInput.value) || options.max);
    });

    // Initial UI update
    updateSliderUI();
}

/**
 * Initialize single-handle slider for votes
 */
function initializeSingleSlider(options) {
    const slider = document.getElementById(options.sliderId);
    if (!slider) return;

    const input = document.getElementById(options.inputId);
    const inner = slider.querySelector('.inner');
    const trackFill = slider.querySelector('.track-fill');
    const thumb = slider.querySelector('.thumb');

    if (!inner || !trackFill || !thumb || !input) return;

    let isDragging = false;

    const updateSliderUI = () => {
        const val = parseInt(input.value) || options.min;
        const range = options.max - options.min;
        const percent = ((val - options.min) / range) * 100;

        thumb.style.left = `calc(${percent}% - 0px)`;
        trackFill.style.left = '0%';
        trackFill.style.right = `${100 - percent}%`;
    };

    const getValueFromPosition = (clientX) => {
        const rect = inner.getBoundingClientRect();
        const percent = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
        let value = options.min + (percent * (options.max - options.min));
        value = Math.round(value / options.step) * options.step;
        return Math.round(value);
    };

    const handleMouseDown = (e) => {
        e.preventDefault();
        isDragging = true;
        document.addEventListener('mousemove', handleMouseMove);
        document.addEventListener('mouseup', handleMouseUp);
    };

    const handleMouseMove = (e) => {
        if (!isDragging) return;
        const value = getValueFromPosition(e.clientX);
        input.value = value;
        updateSliderUI();
        options.onUpdate(value);
    };

    const handleMouseUp = () => {
        isDragging = false;
        document.removeEventListener('mousemove', handleMouseMove);
        document.removeEventListener('mouseup', handleMouseUp);
        document.removeEventListener('touchmove', handleTouchMove);
        document.removeEventListener('touchend', handleTouchEnd);
    };

    // Touch event handlers for mobile
    const handleTouchStart = (e) => {
        e.preventDefault();
        isDragging = true;
        document.addEventListener('touchmove', handleTouchMove, { passive: false });
        document.addEventListener('touchend', handleTouchEnd);
    };

    const handleTouchMove = (e) => {
        if (!isDragging) return;
        e.preventDefault();
        const touch = e.touches[0];
        const value = getValueFromPosition(touch.clientX);
        input.value = value;
        updateSliderUI();
        options.onUpdate(value);
    };

    const handleTouchEnd = () => {
        isDragging = false;
        document.removeEventListener('touchmove', handleTouchMove);
        document.removeEventListener('touchend', handleTouchEnd);
    };

    thumb.addEventListener('mousedown', handleMouseDown);
    thumb.addEventListener('touchstart', handleTouchStart, { passive: false });

    // Sync input changes to slider
    input.addEventListener('input', () => {
        updateSliderUI();
        options.onUpdate(parseInt(input.value) || options.min);
    });

    // Initial UI update
    updateSliderUI();
}

/**
 * Check if search term is an ID (IMDb or TMDB)
 * Backend now handles ID lookups inline, so this just returns true/false
 * to indicate if the term looks like an ID (for any special UI handling if needed)
 * @param {string} searchTerm - The search term to check
 * @returns {boolean} - True if it looks like an ID, false otherwise
 */
function isIDSearch(searchTerm) {
    const term = searchTerm.trim();

    // Check for IMDb ID (tt followed by digits)
    if (/^tt\d+$/i.test(term)) {
        return true;
    }

    // Check for TMDB ID (tmdb: prefix)
    if (/^tmdb:?\d+$/i.test(term)) {
        return true;
    }

    // Check if it's just digits (treat as TMDB ID)
    if (/^\d+$/.test(term) && parseInt(term) > 0) {
        return true;
    }

    return false;
}

/**
 * Handle search input
 */
function handleSearch() {
    const query = searchInput.value.trim();
    window.discoverState.searchTerm = query;

    if (query.length === 0) {
        searchClearBtn.style.display = 'none';
        loadTrending();
        return;
    }

    // ID searches (IMDb/TMDB) are now handled inline by the backend API
    // No page redirect needed - just proceed with normal search flow

    // Show search results, hide trending content
    if (trendingContent) trendingContent.style.display = 'none';
    if (searchResults) searchResults.style.display = 'block';

    // Reset page and auto-load count for new search
    window.discoverState.page = 1;
    window.discoverState.autoLoadCount = 0;
    searchClearBtn.style.display = 'block';
    searchContent(query);
}

/**
 * Clear search
 */
function clearSearch() {
    searchInput.value = '';
    searchClearBtn.style.display = 'none';
    window.discoverState.searchTerm = '';
    window.discoverState.filters.searchQuery = '';
    updateActiveFilters();
    switchTab('trending'); // Fallback to trending
}

/**
 * Clear year filter
 */
function clearYearFilter() {
    const state = window.discoverState;
    state.filters.yearFrom = '';
    state.filters.yearTo = '';
    if (state.yearFromInput) state.yearFromInput.value = '';
    if (state.yearToInput) state.yearToInput.value = '';
    updateActiveFilters();
}

/**
 * Clear released filter
 */
function clearReleasedFilter() {
    const state = window.discoverState;
    state.filters.releasedWithin = '';
    if (state.releasedWithinInput) state.releasedWithinInput.value = '';
    updateActiveFilters();
}

/**
 * Clear upcoming filter
 */
function clearUpcomingFilter() {
    const state = window.discoverState;
    state.filters.upcomingDays = '';
    if (state.upcomingDaysInput) state.upcomingDaysInput.value = '';
    updateActiveFilters();
}

/**
 * Clear genres filter
 */
function clearGenresFilter() {
    const state = window.discoverState;
    state.filters.selectedGenres = [];
    
    // Clear dropdown using vanilla JavaScript
    if (state.genresDropdown) {
        const input = state.genresDropdown.querySelector('input[type="hidden"]');
        const text = state.genresDropdown.querySelector('.default.text');
        if (input) input.value = '';
        if (text) text.textContent = 'Select genres';
        
        // Clear selected items in menu
        const items = state.genresDropdown.querySelectorAll('.item.selected');
        items.forEach(item => item.classList.remove('selected'));
    }
    
    updateSelectedGenres();
    updateActiveFilters();
}

/**
 * Clear TMDB rating filter
 */
function clearTmdbRatingFilter() {
    const state = window.discoverState;
    state.filters.tmdbRatingMin = 0;
    state.filters.tmdbRatingMax = 10;
    if (state.tmdbRatingMinInput) state.tmdbRatingMinInput.value = '0';
    if (state.tmdbRatingMaxInput) state.tmdbRatingMaxInput.value = '10';
    // Re-initialize slider UI
    initializeRangeSliders();
    updateActiveFilters();
}

/**
 * Clear IMDb rating filter
 */
function clearImdbRatingFilter() {
    const state = window.discoverState;
    state.filters.imdbRatingMin = 0;
    state.filters.imdbRatingMax = 10;
    if (state.imdbRatingMinInput) state.imdbRatingMinInput.value = '0';
    if (state.imdbRatingMaxInput) state.imdbRatingMaxInput.value = '10';
    // Re-initialize slider UI
    initializeRangeSliders();
    updateActiveFilters();
}

/**
 * Clear TMDB votes filter
 */
function clearTmdbVotesFilter() {
    const state = window.discoverState;
    state.filters.tmdbVotesMin = 0;
    if (state.tmdbVotesMinInput) state.tmdbVotesMinInput.value = '0';
    // Re-initialize slider UI
    initializeRangeSliders();
    updateActiveFilters();
}

/**
 * Clear IMDb votes filter
 */
function clearImdbVotesFilter() {
    const state = window.discoverState;
    state.filters.imdbVotesMin = 0;
    if (state.imdbVotesMinInput) state.imdbVotesMinInput.value = '0';
    // Re-initialize slider UI
    initializeRangeSliders();
    updateActiveFilters();
}

/**
 * Clear runtime filter
 */
function clearRuntimeFilter() {
    const state = window.discoverState;
    state.filters.runtimeMin = 0;
    state.filters.runtimeMax = 300;
    if (state.runtimeMinInput) state.runtimeMinInput.value = '0';
    if (state.runtimeMaxInput) state.runtimeMaxInput.value = '300';
    updateActiveFilters();
}

/**
 * Clear certification filter
 */
function clearCertificationFilter() {
    const state = window.discoverState;
    state.filters.certificationMin = '';
    state.filters.certificationMax = '';
    const certMinSelect = document.getElementById('certification-min-select');
    const certMaxSelect = document.getElementById('certification-max-select');
    if (certMinSelect) certMinSelect.value = '';
    if (certMaxSelect) certMaxSelect.value = '';
    updateActiveFilters();
}

/**
 * Clear production company filter
 */
function clearProductionCompanyFilter() {
    const state = window.discoverState;
    state.filters.productionCompany = '';

    // Clear dropdown using vanilla JavaScript
    if (state.productionCompanyDropdown) {
        const input = state.productionCompanyDropdown.querySelector('input[type="hidden"]');
        const text = state.productionCompanyDropdown.querySelector('.default.text');
        if (input) input.value = '';
        if (text) text.textContent = 'Select production company';

        // Clear selected items in menu
        const items = state.productionCompanyDropdown.querySelectorAll('.item.selected');
        items.forEach(item => item.classList.remove('selected'));
    }

    updateActiveFilters();
}

/**
 * Clear all filters
 */
function clearAllFilters() {
    const state = window.discoverState;

 // Set flag to prevent saving during clear
    state.isClearing = true;
    
    // Reset restoration flag to prevent any filter restoration
    state.hasRestoredFilters = false;
    
    // Cancel any pending live filter timeout to prevent automatic filter application
    if (window.liveFilterTimeout) {
        clearTimeout(window.liveFilterTimeout);
        window.liveFilterTimeout = null;
    }

    // Reset all filter values (including excluded arrays)
    state.filters = {
        searchQuery: '',
        sortBy: 'popularity',
        sortOrder: 'desc',
        mediaType: 'all',
        yearFrom: '',
        yearTo: '',
        releasedWithin: '',
        upcomingDays: '',
        tmdbRatingMin: 0,
        tmdbRatingMax: 10,
        imdbRatingMin: 0,
        imdbRatingMax: 10,
        tmdbVotesMin: 0,
        imdbVotesMin: 0,
        selectedGenres: [],
        excludedGenres: [],
        selectedLanguages: [],
        excludedLanguages: [],
        selectedCountries: [],
        excludedCountries: [],
        certificationMin: '',
        certificationMax: '',
        selectedProviders: [],
        excludedProviders: [],
        watchRegion: 'US',
        selectedNetworks: [],
        excludedNetworks: [],
        selectedCompanies: [],
        excludedCompanies: [],
        companyCache: {},
        selectedKeywords: [],
        excludedKeywords: [],
        keywordCache: {},
        runtimeMin: 0,
        runtimeMax: 300,
        activeFilters: []
    };

    // Clear saved filters from localStorage BEFORE updating UI
    clearSavedFilters();
    
    // Clear sidebar lists selection
    if (window.sidebarListsState) {
        window.sidebarListsState.selectedLists = [];
        window.sidebarListsState.rawResults = [];
        renderSidebarListChips();
        
        // Clear dropdown visual state
        const dropdown = document.getElementById('lists-dropdown');
        if (dropdown) {
            dropdown.querySelectorAll('.included').forEach(item => {
                item.classList.remove('included');
            });
        }
        
        // Re-enable all filters
        updateFilterAvailability();
    }

    // Reset UI elements
    clearSearch();
    clearYearFilter();
    clearReleasedFilter();
    clearUpcomingFilter();
    clearTmdbRatingFilter();
    clearRuntimeFilter();

    // Clear all chips inputs
    clearChipsInput('genres');
    clearChipsInput('language');
    clearChipsInput('country');
    clearChipsInput('provider');
    clearChipsInput('network');
    clearChipsInput('company');

    // Reset watch region select
    const watchRegionSelect = document.getElementById('watch-region');
    if (watchRegionSelect) {
        watchRegionSelect.value = 'US';
    }

    // Reset sort select
    if (state.sortBySelect) {
        state.sortBySelect.value = 'popularity';
    }

    // Reset sort order toggle
    if (state.sortOrderToggle) {
        state.sortOrderToggle.setAttribute('data-order', 'desc');
        const descIcon = state.sortOrderToggle.querySelector('.sort-icon-desc');
        const ascIcon = state.sortOrderToggle.querySelector('.sort-icon-asc');
        if (descIcon) descIcon.style.display = 'block';
        if (ascIcon) ascIcon.style.display = 'none';
    }

    updateActiveFilters();

    // Apply default filters (keep sidebar open)
    applyAdvancedFilters(false);

    // Clear search input
    if (searchInput) {
        searchInput.value = '';
    }
    if (searchClearBtn) {
        searchClearBtn.style.display = 'none';
    }
    state.searchTerm = '';

    // Return to trending view (all filters are cleared)
    if (trendingContent) trendingContent.style.display = 'block';
    if (searchResults) searchResults.style.display = 'none';

    // Reload trending content
    loadTrending();
    
     // Clear the flag
    state.isClearing = false;
}

/**
 * Apply advanced filters
 * @param {boolean} closeDrawer - Whether to close the filter drawer (default: true)
 */
function applyAdvancedFilters(closeDrawer = true) {
    const state = window.discoverState;
    const listsState = window.sidebarListsState;

    // Reset page and auto-load count for new filter application
    state.page = 1;
    state.autoLoadCount = 0;

    // Switch to filtered results view (hide trending, show search results grid)
    if (trendingContent) trendingContent.style.display = 'none';
    if (searchResults) searchResults.style.display = 'block';

    // Close filter drawer after applying (only if explicitly requested, e.g., from Apply button)
    if (closeDrawer) {
        closeFilters();
    }
    
    // If lists are currently selected, re-filter the list results instead of doing API search
    if (listsState.selectedLists && listsState.selectedLists.length > 0 && listsState.rawResults.length > 0) {
        console.log('[Discover] Re-filtering list results with current filters');
        
        // Apply filters to stored raw results
        const filteredResults = filterListResults(listsState.rawResults);
        
        // Clear and render filtered results
        if (resultsGrid) {
            resultsGrid.innerHTML = '';
        }
        
        if (filteredResults && filteredResults.length > 0) {
            renderResults(filteredResults);
            hideError();
            hideEmpty();
            
            // Update results info
            updateResultsInfo({
                total_results: filteredResults.length,
                page: 1,
                total_pages: 1
            });
        } else {
            showEmpty();
        }
        
        updatePagination();
        clearLoadingFlag();
        return;  // Don't proceed to TMDB API search
    }

    // Build sort_by parameter - dropdown values already include order (e.g., "popularity.desc")
    // If sortBy already has the order, use it directly; otherwise combine with sortOrder
    let sortByValue = state.filters.sortBy;
    if (sortByValue && !sortByValue.includes('.')) {
        sortByValue = `${sortByValue}.${state.filters.sortOrder}`;
    }

    // Determine media type - if 'all', default to 'movie' since TMDB discover API requires specific type
    let mediaType = state.filters.mediaType;
    if (mediaType === 'all') {
        mediaType = 'movie';
    }

    // Check if specific date filters are set (these take priority over year range)
    const hasDateFilter = state.filters.releasedWithin || state.filters.upcomingDays;

    // Build query parameters
    const params = new URLSearchParams({
        type: mediaType,
        page: state.page,
        sort_by: sortByValue,
        // Basic filters - year range only applies if no specific date filter is set and user entered values
        year_from: !hasDateFilter && state.filters.yearFrom ? state.filters.yearFrom : '',
        year_to: !hasDateFilter && state.filters.yearTo ? state.filters.yearTo : '',
        released_within: state.filters.releasedWithin,
        upcoming_days: state.filters.upcomingDays,
        // Ratings & votes
        tmdb_rating_min: state.filters.tmdbRatingMin > 0 ? state.filters.tmdbRatingMin : '',
        tmdb_rating_max: state.filters.tmdbRatingMax < 10 ? state.filters.tmdbRatingMax : '',
        tmdb_votes_min: state.filters.tmdbVotesMin > 0 ? state.filters.tmdbVotesMin : '',
        // Genres (include and exclude)
        genres: state.filters.selectedGenres.join(','),
        genres_exclude: state.filters.excludedGenres.join(','),
        // Keywords (include and exclude)
        keywords: state.filters.selectedKeywords.join(','),
        keywords_exclude: state.filters.excludedKeywords.join(','),
        include_video: state.filters.includeVideo ? 'true' : '',
        // Language, Country, Watch Provider (include and exclude)
        language: state.filters.selectedLanguages.join(','),
        language_exclude: state.filters.excludedLanguages.join(','),
        country: state.filters.selectedCountries.join(','),
        country_exclude: state.filters.excludedCountries.join(','),
        watch_provider: state.filters.selectedProviders.join(','),
        watch_provider_exclude: state.filters.excludedProviders.join(','),
        watch_region: state.filters.watchRegion || 'US',
        // TV Network (TV shows only)
        network: state.filters.selectedNetworks.join(','),
        network_exclude: state.filters.excludedNetworks.join(','),
        // Runtime
        runtime_min: state.filters.runtimeMin > 0 ? state.filters.runtimeMin : '',
        runtime_max: state.filters.runtimeMax < 300 ? state.filters.runtimeMax : '',
        // Certification (range with gte/lte)
        'certification.gte': state.filters.certificationMin || '',
        'certification.lte': state.filters.certificationMax || '',
        certification_country: state.filters.watchRegion || 'US',
        // Production Company (include and exclude)
        production_company: state.filters.selectedCompanies.join(','),
        production_company_exclude: state.filters.excludedCompanies.join(',')
    });

    // Clear results and state before fetching new results to prevent showing stale data
    state.currentResults = [];
    if (resultsGrid) {
        resultsGrid.innerHTML = '';
    }

    // Make API call
    fetchAdvancedFilterResults(params);
}

/**
 * Fetch results with advanced filters
 */
async function fetchAdvancedFilterResults(params) {
    const isInitialLoad = window.discoverState.page === 1;
    try {
        setLoadingFlag();

        const response = await fetch(`/discover/api/filter?${params}`);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        window.discoverState.hasMore = data.page < data.total_pages;

        if (isInitialLoad) {
            renderResults(data.results);
        } else {
            appendResults(data.results);
        }

        updatePagination();
        updateResultsInfo(data);

        // Auto-load more if content doesn't fill viewport (so user can scroll)
        // Use setTimeout to allow DOM to render before checking
        // Limit auto-loads to prevent infinite loops (e.g., when results have no posters)
        // Don't auto-load if lists are selected (list results are complete)
        const listsState = window.sidebarListsState;
        setTimeout(() => {
            if (window.discoverState.hasMore &&
                !window.discoverState.isLoading &&
                window.discoverState.autoLoadCount < window.discoverState.maxAutoLoads &&
                (!listsState.selectedLists || listsState.selectedLists.length === 0)) {
                const scrollPosition = window.innerHeight + window.scrollY;
                const threshold = document.documentElement.scrollHeight - 500;
                if (scrollPosition >= threshold) {
                    window.discoverState.autoLoadCount++;
                    loadMore();
                }
            }
        }, 100);

    } catch (error) {
        console.error('[Discover] Advanced filter error:', error);
        showError();
    } finally {
        clearLoadingFlag();
    }
}

/**
 * Search content
 */
async function searchContent(query) {
    const isInitialLoad = window.discoverState.page === 1;
    try {
        setLoadingFlag();
        
        // Clear results and state on initial load to prevent showing stale data
        if (isInitialLoad) {
            window.discoverState.currentResults = [];
            if (resultsGrid) {
                resultsGrid.innerHTML = '';
            }
        }

        // Build sort_by parameter from filters (matches the advanced filter format)
        const state = window.discoverState;
        let sortByValue = state.filters.sortBy;
        if (sortByValue && !sortByValue.includes('.')) {
            sortByValue = `${sortByValue}.${state.filters.sortOrder}`;
        }

        const params = new URLSearchParams({
            query: query,
            type: state.mediaType,
            page: state.page,
            sort_by: sortByValue
        });

        const response = await fetch(`/discover/api/search?${params}`);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        window.discoverState.hasMore = data.page < data.total_pages;

        if (isInitialLoad) {
            renderResults(data.results);
        } else {
            appendResults(data.results);
        }

        updatePagination();
        updateResultsInfo(data);

        // Auto-load more if content doesn't fill viewport (so user can scroll)
        // Limit auto-loads to prevent infinite loops
        // Don't auto-load if lists are selected (list results are complete)
        const listsState = window.sidebarListsState;
        setTimeout(() => {
            if (window.discoverState.hasMore &&
                !window.discoverState.isLoading &&
                window.discoverState.autoLoadCount < window.discoverState.maxAutoLoads &&
                (!listsState.selectedLists || listsState.selectedLists.length === 0)) {
                const scrollPosition = window.innerHeight + window.scrollY;
                const threshold = document.documentElement.scrollHeight - 500;
                if (scrollPosition >= threshold) {
                    window.discoverState.autoLoadCount++;
                    loadMore();
                }
            }
        }, 100);

    } catch (error) {
        console.error('[Discover] Search error:', error);
        showError();
    } finally {
        clearLoadingFlag();
    }
}

/**
 * Load trending content into category rows (Movies, Shows, Anime)
 */
async function loadTrending() {
    try {
        setLoadingFlag();
        window.discoverState.currentTab = 'trending';

        // Show trending content, hide search results
        if (trendingContent) trendingContent.style.display = 'block';
        if (searchResults) searchResults.style.display = 'none';

        // Hybrid approach:
        // - Movies & Shows: Use trending/week endpoint for accurate weekly trending
        // - Anime: Use discover endpoint with keyword filtering (210024) for full 20 results
        const [moviesResponse, tvResponse, animeResponse] = await Promise.all([
            fetch('/discover/api/trending?type=movie'),           // Trending movies this week
            fetch('/discover/api/trending?type=tv'),              // Trending TV shows this week
            fetch('/discover/api/trending?type=tv&anime=only')    // Popular anime (discover endpoint)
        ]);

        if (!moviesResponse.ok || !tvResponse.ok || !animeResponse.ok) {
            throw new Error('HTTP error fetching trending content');
        }

        const moviesData = await moviesResponse.json();
        const tvData = await tvResponse.json();
        const animeData = await animeResponse.json();

        // Filter valid results (have poster and rating) and limit to 20 items each
        const movies = filterValidResults(moviesData.results || []).slice(0, 20);
        const shows = filterValidResults(tvData.results || []);
        const anime = filterValidResults(animeData.results || []).slice(0, 20);

        // Filter anime from TV shows on frontend (animation genre ID is 16)
        // Then limit to 20 items to ensure consistent display count
        const regularShows = shows.filter(item => !item.genre_ids || !item.genre_ids.includes(16)).slice(0, 20);

        // Store all trending results in currentResults so bindResultEvents can find them
        window.discoverState.currentResults = [...movies, ...regularShows, ...anime];

        // Render each category
        renderCategoryGrid(moviesGrid, movies);
        renderCategoryGrid(showsGrid, regularShows);
        renderCategoryGrid(animeGrid, anime);

        // Bind click events to all items
        bindResultEvents();
        updateTabState();

    } catch (error) {
        console.error('[Discover] Trending error:', error);
        showError();
    } finally {
        clearLoadingFlag();
    }
}

/**
 * Render items into a specific category grid
 */
function renderCategoryGrid(gridElement, items) {
    if (!gridElement) return;

    if (items.length === 0) {
        gridElement.innerHTML = '<p class="empty-category">No content available</p>';
        return;
    }

    const html = items.map(item => createResultItemHTML(item)).join('');
    gridElement.innerHTML = html;
}

/**
 * Load genres from TMDB
 */
async function loadGenres() {
    try {
        const response = await fetch('/discover/api/genres');
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        window.discoverState.genres.movie = data.movie_genres || [];
        window.discoverState.genres.tv = data.tv_genres || [];

        renderGenreFilters();

    } catch (error) {
        console.error('[Discover] Genres error:', error);
    }
}

/**
 * Render genre filters into the chips dropdown
 * Shows different genres based on current media type selection
 */
function renderGenreFilters() {
    const state = window.discoverState;
    const mediaType = state.filters.mediaType || 'all';

    // Find the genres dropdown container
    const genresDropdown = document.getElementById('genres-dropdown');
    if (!genresDropdown) return;

    // Keep the [Match all] option, clear the rest
    const matchAllItem = genresDropdown.querySelector('[data-value="match_all"]');
    genresDropdown.innerHTML = '';
    if (matchAllItem) {
        genresDropdown.appendChild(matchAllItem);
    }

    // Select genres based on media type
    // For "all", combine both lists and deduplicate by name
    let genres = [];
    if (mediaType === 'movie') {
        genres = state.genres.movie || [];
    } else if (mediaType === 'tv') {
        genres = state.genres.tv || [];
    } else {
        // Combine movie and TV genres, deduplicate by name (prefer movie IDs)
        const movieGenres = state.genres.movie || [];
        const tvGenres = state.genres.tv || [];
        const genreMap = new Map();

        // Add movie genres first
        movieGenres.forEach(g => genreMap.set(g.name, g));
        // Add TV genres (only if name doesn't exist)
        tvGenres.forEach(g => {
            if (!genreMap.has(g.name)) {
                genreMap.set(g.name, g);
            }
        });

        genres = Array.from(genreMap.values()).sort((a, b) => a.name.localeCompare(b.name));
    }

    genres.forEach(genre => {
        const item = document.createElement('div');
        item.className = 'chips-dropdown-item';
        item.setAttribute('data-value', genre.id.toString());
        item.innerHTML = `
            <span class="chips-item-label">${genre.name}</span>
            <div class="chips-item-actions">
                <button type="button" class="chips-include-btn" title="Include">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4" />
                    </svg>
                </button>
                <button type="button" class="chips-exclude-btn" title="Exclude">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M20 12H4" />
                    </svg>
                </button>
            </div>
        `;
        genresDropdown.appendChild(item);
    });

    // Restore include/exclude state for already selected genres
    const selectedGenres = state.filters.selectedGenres || [];
    const excludedGenres = state.filters.excludedGenres || [];

    selectedGenres.forEach(genreId => {
        const item = genresDropdown.querySelector(`[data-value="${genreId}"]`);
        if (item) item.classList.add('included');
    });

    excludedGenres.forEach(genreId => {
        const item = genresDropdown.querySelector(`[data-value="${genreId}"]`);
        if (item) item.classList.add('excluded');
    });

    // Re-render chips to match current state
    updateGenreChips();
}

/**
 * Update genre chips display after genre list changes
 */
function updateGenreChips() {
    const state = window.discoverState;
    const chipsWrapper = document.getElementById('genres-chips');
    if (!chipsWrapper) return;

    // Clear existing chips
    chipsWrapper.innerHTML = '';

    // Get all genres (movie + tv combined for lookup)
    const allGenres = new Map();
    (state.genres.movie || []).forEach(g => allGenres.set(g.id.toString(), g));
    (state.genres.tv || []).forEach(g => allGenres.set(g.id.toString(), g));

    // Add included genre chips
    (state.filters.selectedGenres || []).forEach(genreId => {
        const genre = allGenres.get(genreId.toString());
        if (genre) {
            const chip = document.createElement('span');
            chip.className = 'chip chip-include';
            chip.setAttribute('data-value', genreId);
            chip.innerHTML = `<span class="chip-icon">+</span>${genre.name} <button type="button" class="chip-remove">&times;</button>`;
            chip.querySelector('.chip-remove').addEventListener('click', () => {
                const idx = state.filters.selectedGenres.indexOf(genreId);
                if (idx > -1) state.filters.selectedGenres.splice(idx, 1);
                const dropdownItem = document.querySelector(`#genres-dropdown [data-value="${genreId}"]`);
                if (dropdownItem) dropdownItem.classList.remove('included');
                updateGenreChips();
                updateActiveFilters();
            });
            chipsWrapper.appendChild(chip);
        }
    });

    // Add excluded genre chips
    (state.filters.excludedGenres || []).forEach(genreId => {
        const genre = allGenres.get(genreId.toString());
        if (genre) {
            const chip = document.createElement('span');
            chip.className = 'chip chip-exclude';
            chip.setAttribute('data-value', genreId);
            chip.innerHTML = `<span class="chip-icon">-</span>${genre.name} <button type="button" class="chip-remove">&times;</button>`;
            chip.querySelector('.chip-remove').addEventListener('click', () => {
                const idx = state.filters.excludedGenres.indexOf(genreId);
                if (idx > -1) state.filters.excludedGenres.splice(idx, 1);
                const dropdownItem = document.querySelector(`#genres-dropdown [data-value="${genreId}"]`);
                if (dropdownItem) dropdownItem.classList.remove('excluded');
                updateGenreChips();
                updateActiveFilters();
            });
            chipsWrapper.appendChild(chip);
        }
    });
}

/**
 * Update selected genres display
 */
function updateSelectedGenres() {
    const state = window.discoverState;
    
    if (!state.selectedGenresContainer || !state.genresMenu) return;
    
    // Get selected values from dropdown using vanilla JavaScript
    const input = state.genresDropdown.querySelector('input[type="hidden"]');
    const selectedValues = input ? input.value.split(',').filter(v => v.trim()) : [];
    state.filters.selectedGenres = selectedValues;
    
    // Render selected genre chips
    const selectedGenres = state.genres.movie.filter(genre => 
        selectedValues.includes(genre.id.toString())
    );
    
    const html = selectedGenres.map(genre => `
        <div class="genre-chip" data-genre-id="${genre.id}">
            <span>${genre.name}</span>
            <button type="button" class="remove-genre" onclick="removeGenre(${genre.id})">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                </svg>
            </button>
        </div>
    `).join('');
    
    state.selectedGenresContainer.innerHTML = html;
}

/**
 * Remove genre from selection
 */
function removeGenre(genreId) {
    const state = window.discoverState;
    
    if (!state.genresDropdown) return;
    
    // Remove from dropdown using vanilla JavaScript
    const input = state.genresDropdown.querySelector('input[type="hidden"]');
    const currentValues = input ? input.value.split(',').filter(v => v.trim()) : [];
    const newValues = currentValues.filter(id => id !== genreId.toString());
    input.value = newValues.join(',');
    
    // Update visual state
    const items = state.genresDropdown.querySelectorAll('.item');
    items.forEach(item => {
        const value = item.getAttribute('data-value');
        if (value === genreId.toString()) {
            item.classList.remove('selected');
        }
    });
    
    // Update text display
    const text = state.genresDropdown.querySelector('.default.text');
    if (text) {
        text.textContent = newValues.length > 0 ? `${newValues.length} selected` : 'Select genres';
    }
    
    // Update state and display
    updateSelectedGenres();
    updateActiveFilters();
}

/**
 * Update active filters display
 */
function updateActiveFilters() {
    const state = window.discoverState;
    
    if (!state.activeFiltersContainer) return;
    
    const activeFilters = [];
    
    // Build list of active filters
    if (state.filters.searchQuery) {
        activeFilters.push({
            key: 'search',
            label: `Search: "${state.filters.searchQuery}"`,
            removeFn: () => clearSearch()
        });
    }
    
    if (state.filters.mediaType !== 'all') {
        activeFilters.push({
            key: 'type',
            label: `Type: ${state.filters.mediaType}`,
            removeFn: () => setMediaType('all')
        });
    }
    
    if (state.filters.yearFrom || state.filters.yearTo) {
        const fromYear = state.filters.yearFrom || '1900';
        const toYear = state.filters.yearTo || new Date().getFullYear();
        activeFilters.push({
            key: 'year',
            label: `Year: ${fromYear}-${toYear}`,
            removeFn: () => clearYearFilter()
        });
    }
    
    if (state.filters.releasedWithin) {
        activeFilters.push({
            key: 'released',
            label: `Released within ${state.filters.releasedWithin} days`,
            removeFn: () => clearReleasedFilter()
        });
    }
    
    if (state.filters.upcomingDays) {
        activeFilters.push({
            key: 'upcoming',
            label: `Upcoming ${state.filters.upcomingDays} days`,
            removeFn: () => clearUpcomingFilter()
        });
    }
    
    if (state.filters.selectedGenres.length > 0) {
        // Merge movie and TV genres for lookup
        const allGenres = new Map();
        (state.genres.movie || []).forEach(g => allGenres.set(g.id.toString(), g.name));
        (state.genres.tv || []).forEach(g => allGenres.set(g.id.toString(), g.name));

        const genreNames = state.filters.selectedGenres
            .map(genreId => allGenres.get(genreId.toString()) || `Genre ${genreId}`)
            .filter(name => name);
        activeFilters.push({
            key: 'genres',
            label: `Genres: ${genreNames.join(', ')}`,
            removeFn: () => clearGenresFilter()
        });
    }
    
    if (state.filters.tmdbRatingMin > 0 || state.filters.tmdbRatingMax < 10) {
        activeFilters.push({
            key: 'tmdb_rating',
            label: `TMDB Rating: ${state.filters.tmdbRatingMin}-${state.filters.tmdbRatingMax}`,
            removeFn: () => clearTmdbRatingFilter()
        });
    }

    if (state.filters.imdbRatingMin > 0 || state.filters.imdbRatingMax < 10) {
        activeFilters.push({
            key: 'imdb_rating',
            label: `IMDb Rating: ${state.filters.imdbRatingMin}-${state.filters.imdbRatingMax}`,
            removeFn: () => clearImdbRatingFilter()
        });
    }

    if (state.filters.tmdbVotesMin > 0) {
        activeFilters.push({
            key: 'tmdb_votes',
            label: `TMDB Votes: ${state.filters.tmdbVotesMin}+`,
            removeFn: () => clearTmdbVotesFilter()
        });
    }

    if (state.filters.imdbVotesMin > 0) {
        activeFilters.push({
            key: 'imdb_votes',
            label: `IMDb Votes: ${state.filters.imdbVotesMin}+`,
            removeFn: () => clearImdbVotesFilter()
        });
    }

    if (state.filters.runtimeMin > 0 || state.filters.runtimeMax < 300) {
        activeFilters.push({
            key: 'runtime',
            label: `Runtime: ${state.filters.runtimeMin}-${state.filters.runtimeMax}min`,
            removeFn: () => clearRuntimeFilter()
        });
    }

    // Certification Range
    if (state.filters.certificationMin || state.filters.certificationMax) {
        let label = 'Certification: ';
        if (state.filters.certificationMin && state.filters.certificationMax) {
            label += `${state.filters.certificationMin} to ${state.filters.certificationMax}`;
        } else if (state.filters.certificationMin) {
            label += `${state.filters.certificationMin} and above`;
        } else {
            label += `${state.filters.certificationMax} and below`;
        }
        activeFilters.push({
            key: 'certification',
            label: label,
            removeFn: () => { clearCertificationFilter(); updateActiveFilters(); }
        });
    }

    if (state.filters.productionCompany) {
        const companyNames = {
            'universal': 'Universal Pictures',
            'warner': 'Warner Bros',
            'disney': 'Disney',
            'marvel': 'Marvel Studios',
            'pixar': 'Pixar',
            'paramount': 'Paramount',
            'sony': 'Sony Pictures',
            'netflix': 'Netflix',
            'hbo': 'HBO'
        };
        activeFilters.push({
            key: 'production_company',
            label: `Studio: ${companyNames[state.filters.productionCompany] || state.filters.productionCompany}`,
            removeFn: () => clearProductionCompanyFilter()
        });
    }

    // Excluded Genres
    if (state.filters.excludedGenres.length > 0) {
        const allGenres = new Map();
        (state.genres.movie || []).forEach(g => allGenres.set(g.id.toString(), g.name));
        (state.genres.tv || []).forEach(g => allGenres.set(g.id.toString(), g.name));

        const genreNames = state.filters.excludedGenres
            .map(genreId => allGenres.get(genreId.toString()) || `Genre ${genreId}`)
            .filter(name => name);
        activeFilters.push({
            key: 'excluded_genres',
            label: `Excluded Genres: ${genreNames.join(', ')}`,
            removeFn: () => { state.filters.excludedGenres = []; updateActiveFilters(); }
        });
    }

    // Keywords (included)
    if (state.filters.selectedKeywords.length > 0) {
        const keywordNames = state.filters.selectedKeywords
            .map(kwId => state.filters.keywordCache?.[kwId] || kwId);
        activeFilters.push({
            key: 'keywords',
            label: `Keywords: ${keywordNames.join(', ')}`,
            removeFn: () => { state.filters.selectedKeywords = []; updateActiveFilters(); }
        });
    }

    // Keywords (excluded)
    if (state.filters.excludedKeywords.length > 0) {
        const keywordNames = state.filters.excludedKeywords
            .map(kwId => state.filters.keywordCache?.[kwId] || kwId);
        activeFilters.push({
            key: 'excluded_keywords',
            label: `Excluded Keywords: ${keywordNames.join(', ')}`,
            removeFn: () => { state.filters.excludedKeywords = []; updateActiveFilters(); }
        });
    }

    // Languages (included)
    if (state.filters.selectedLanguages.length > 0) {
        const languageNames = state.filters.selectedLanguages
            .map(langCode => {
                const lang = state.availableLanguages?.find(l => l.iso_639_1 === langCode);
                return lang ? lang.english_name : langCode.toUpperCase();
            });
        activeFilters.push({
            key: 'languages',
            label: `Languages: ${languageNames.join(', ')}`,
            removeFn: () => { state.filters.selectedLanguages = []; updateActiveFilters(); }
        });
    }

    // Languages (excluded)
    if (state.filters.excludedLanguages.length > 0) {
        const languageNames = state.filters.excludedLanguages
            .map(langCode => {
                const lang = state.availableLanguages?.find(l => l.iso_639_1 === langCode);
                return lang ? lang.english_name : langCode.toUpperCase();
            });
        activeFilters.push({
            key: 'excluded_languages',
            label: `Excluded Languages: ${languageNames.join(', ')}`,
            removeFn: () => { state.filters.excludedLanguages = []; updateActiveFilters(); }
        });
    }

    // Countries (included)
    if (state.filters.selectedCountries.length > 0) {
        const countryNames = state.filters.selectedCountries
            .map(countryCode => {
                const country = state.availableCountries?.find(c => c.iso_3166_1 === countryCode);
                return country ? country.english_name : countryCode.toUpperCase();
            });
        activeFilters.push({
            key: 'countries',
            label: `Countries: ${countryNames.join(', ')}`,
            removeFn: () => { state.filters.selectedCountries = []; updateActiveFilters(); }
        });
    }

    // Countries (excluded)
    if (state.filters.excludedCountries.length > 0) {
        const countryNames = state.filters.excludedCountries
            .map(countryCode => {
                const country = state.availableCountries?.find(c => c.iso_3166_1 === countryCode);
                return country ? country.english_name : countryCode.toUpperCase();
            });
        activeFilters.push({
            key: 'excluded_countries',
            label: `Excluded Countries: ${countryNames.join(', ')}`,
            removeFn: () => { state.filters.excludedCountries = []; updateActiveFilters(); }
        });
    }

    // Watch Providers (included)
    if (state.filters.selectedProviders.length > 0) {
        const providerNames = state.filters.selectedProviders
            .map(providerId => {
                const provider = state.availableProviders?.find(p => p.provider_id.toString() === providerId.toString());
                return provider ? provider.provider_name : providerId;
            });
        activeFilters.push({
            key: 'providers',
            label: `Providers: ${providerNames.join(', ')}`,
            removeFn: () => { state.filters.selectedProviders = []; updateActiveFilters(); }
        });
    }

    // Watch Providers (excluded)
    if (state.filters.excludedProviders.length > 0) {
        const providerNames = state.filters.excludedProviders
            .map(providerId => {
                const provider = state.availableProviders?.find(p => p.provider_id.toString() === providerId.toString());
                return provider ? provider.provider_name : providerId;
            });
        activeFilters.push({
            key: 'excluded_providers',
            label: `Excluded Providers: ${providerNames.join(', ')}`,
            removeFn: () => { state.filters.excludedProviders = []; updateActiveFilters(); }
        });
    }

    // Networks (included) - TV only
    if (state.filters.selectedNetworks.length > 0) {
        const networkNames = state.filters.selectedNetworks
            .map(networkId => {
                const network = state.availableNetworks?.find(n => n.id.toString() === networkId.toString());
                return network ? network.name : networkId;
            });
        activeFilters.push({
            key: 'networks',
            label: `Networks: ${networkNames.join(', ')}`,
            removeFn: () => { state.filters.selectedNetworks = []; updateActiveFilters(); }
        });
    }

    // Networks (excluded) - TV only
    if (state.filters.excludedNetworks.length > 0) {
        const networkNames = state.filters.excludedNetworks
            .map(networkId => {
                const network = state.availableNetworks?.find(n => n.id.toString() === networkId.toString());
                return network ? network.name : networkId;
            });
        activeFilters.push({
            key: 'excluded_networks',
            label: `Excluded Networks: ${networkNames.join(', ')}`,
            removeFn: () => { state.filters.excludedNetworks = []; updateActiveFilters(); }
        });
    }

    // Production Companies (included)
    if (state.filters.selectedCompanies.length > 0) {
        const companyNames = state.filters.selectedCompanies
            .map(companyId => {
                const company = state.availableCompanies?.find(c => c.id.toString() === companyId.toString());
                return company ? company.name : companyId;
            });
        activeFilters.push({
            key: 'companies',
            label: `Companies: ${companyNames.join(', ')}`,
            removeFn: () => { state.filters.selectedCompanies = []; updateActiveFilters(); }
        });
    }

    // Production Companies (excluded)
    if (state.filters.excludedCompanies.length > 0) {
        const companyNames = state.filters.excludedCompanies
            .map(companyId => {
                const company = state.availableCompanies?.find(c => c.id.toString() === companyId.toString());
                return company ? company.name : companyId;
            });
        activeFilters.push({
            key: 'excluded_companies',
            label: `Excluded Companies: ${companyNames.join(', ')}`,
            removeFn: () => { state.filters.excludedCompanies = []; updateActiveFilters(); }
        });
    }

    // Release Type (movies only)
    if (state.filters.releaseType) {
        const releaseTypeNames = {
            '1': 'Premiere',
            '2': 'Theatrical (limited)',
            '3': 'Theatrical',
            '4': 'Digital',
            '5': 'Physical',
            '6': 'TV'
        };
        const types = state.filters.releaseType.split(',').map(t => releaseTypeNames[t] || t);
        activeFilters.push({
            key: 'release_type',
            label: `Release Type: ${types.join(', ')}`,
            removeFn: () => { state.filters.releaseType = ''; updateActiveFilters(); }
        });
    }

    // Sort By
    if (state.filters.sortBy && state.filters.sortBy !== 'popularity.desc') {
        const sortNames = {
            'popularity.desc': 'Popularity (High to Low)',
            'popularity.asc': 'Popularity (Low to High)',
            'vote_average.desc': 'Rating (High to Low)',
            'vote_average.asc': 'Rating (Low to High)',
            'primary_release_date.desc': 'Release Date (Newest First)',
            'primary_release_date.asc': 'Release Date (Oldest First)',
            'first_air_date.desc': 'Air Date (Newest First)',
            'first_air_date.asc': 'Air Date (Oldest First)',
            'title.asc': 'Title (A-Z)',
            'title.desc': 'Title (Z-A)',
            'vote_count.desc': 'Vote Count (High to Low)'
        };
        activeFilters.push({
            key: 'sort',
            label: `Sort: ${sortNames[state.filters.sortBy] || state.filters.sortBy}`,
            removeFn: () => { state.filters.sortBy = 'popularity.desc'; updateActiveFilters(); }
        });
    }

    // Watch Region
    if (state.filters.watchRegion && state.filters.watchRegion !== 'US') {
        activeFilters.push({
            key: 'watch_region',
            label: `Watch Region: ${state.filters.watchRegion}`,
            removeFn: () => { state.filters.watchRegion = 'US'; updateActiveFilters(); }
        });
    }

    // Include Video
    if (state.filters.includeVideo) {
        activeFilters.push({
            key: 'include_video',
            label: 'Include Video Content',
            removeFn: () => { state.filters.includeVideo = false; updateActiveFilters(); }
        });
    }

    // Title Filter
    if (state.filters.titleFilter) {
        activeFilters.push({
            key: 'title_filter',
            label: `Title Filter: ${state.filters.titleFilter}`,
            removeFn: () => { state.filters.titleFilter = ''; updateActiveFilters(); }
        });
    }

    // Update state
    state.filters.activeFilters = activeFilters;
    
    // Render active filters with proper click handlers
    state.activeFiltersContainer.innerHTML = '';

    activeFilters.forEach(filter => {
        const chip = document.createElement('div');
        chip.className = 'active-filter-chip';
        chip.innerHTML = `
            <span>${filter.label}</span>
            <button type="button" class="remove-filter">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
                </svg>
            </button>
        `;
        chip.addEventListener('click', filter.removeFn);
        state.activeFiltersContainer.appendChild(chip);
    });
    
    // Show/hide active filters section
    if (state.activeFiltersSection) {
        state.activeFiltersSection.style.display = activeFilters.length > 0 ? 'block' : 'none';
    }

    // Update clear button visibility in header
    updateClearButtonVisibility();

    // Save filters to localStorage after updating (unless we're clearing filters)
    if (!window.discoverState.isClearing) {
        saveFiltersToStorage();
    }

    // Trigger live filtering if enabled and filters are active (but not when clearing)
    if (!window.discoverState.isClearing) {
        triggerLiveFiltering();
    }
}

// Debounced function for live filtering (500ms delay)
window.liveFilterTimeout = null;
function triggerLiveFiltering() {
    const state = window.discoverState;
    const listsState = window.sidebarListsState;

    // Skip if live filtering is disabled
    if (!state.liveFilterEnabled) return;

    // Check if any non-default filters are active (excluding sort which always has a value)
    const f = state.filters;
    const hasActiveFilters =
        f.mediaType !== 'all' ||
        f.yearFrom > 1900 ||
        f.yearTo < new Date().getFullYear() ||
        f.releasedWithin ||
        f.upcomingDays ||
        f.tmdbRatingMin > 0 ||
        f.tmdbRatingMax < 10 ||
        f.tmdbVotesMin > 0 ||
        f.selectedGenres.length > 0 ||
        f.excludedGenres.length > 0 ||
        f.selectedKeywords.length > 0 ||
        f.excludedKeywords.length > 0 ||
        f.selectedLanguages.length > 0 ||
        f.excludedLanguages.length > 0 ||
        f.selectedCountries.length > 0 ||
        f.excludedCountries.length > 0 ||
        f.selectedProviders.length > 0 ||
        f.excludedProviders.length > 0 ||
        f.selectedNetworks.length > 0 ||
        f.excludedNetworks.length > 0 ||
        f.selectedCompanies.length > 0 ||
        f.excludedCompanies.length > 0 ||
        f.runtimeMin > 0 ||
        f.runtimeMax < 300 ||
        f.certificationMin ||
        f.certificationMax;

    // Clear any existing timeout
    if (window.liveFilterTimeout) {
        clearTimeout(window.liveFilterTimeout);
    }

    // If lists are selected, re-filter the list results instead of doing TMDB search
    if (listsState && listsState.selectedLists && listsState.selectedLists.length > 0 && listsState.rawResults.length > 0) {
        // Debounce the list filtering (500ms delay)
        window.liveFilterTimeout = setTimeout(() => {
            filterAndRenderListResults();
        }, 500);
        return;
    }

    // Only apply filters if we have active filters, otherwise don't switch to filter results
    if (hasActiveFilters) {
        // Debounce the filter application (500ms delay)
        window.liveFilterTimeout = setTimeout(() => {
            // Pass false to keep the filter drawer open during live filtering
            applyAdvancedFilters(false);
        }, 500);
    }
}

/**
 * Set media type filter
 */
function setMediaType(type) {
    window.discoverState.mediaType = type;
    window.discoverState.filters.mediaType = type;  // Also update filters state

    // Update UI
    document.querySelectorAll('[data-filter="type"]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.value === type);
    });

    // Update genre dropdown to show appropriate genres for this media type
    renderGenreFilters();

    // Reload certifications for the new media type
    const watchRegionSelect = document.getElementById('watch-region');
    if (watchRegionSelect) {
        loadCertifications(watchRegionSelect.value);
    }

    // Update active filters display
    updateActiveFilters();

    // Reset page and auto-load count
    window.discoverState.page = 1;
    window.discoverState.autoLoadCount = 0;

    // Re-search if we have a search term, otherwise apply filters for live update
    if (window.discoverState.searchTerm) {
        searchContent(window.discoverState.searchTerm);
    } else {
        // Trigger live filter update
        applyAdvancedFilters(false);
    }
}

/**
 * Set sort option
 */
function setSortBy(sortBy) {
    window.discoverState.sortBy = sortBy;

    // Update UI
    document.querySelectorAll('[data-filter="sort"]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.value === sortBy);
    });

    // Re-search if we have a search term
    if (window.discoverState.searchTerm) {
        window.discoverState.page = 1;
        window.discoverState.autoLoadCount = 0;
        searchContent(window.discoverState.searchTerm);
    }
}

/**
 * Toggle genre filter
 */
function toggleGenre(genreId) {
    const genres = window.discoverState.filters.genres;
    const index = genres.indexOf(parseInt(genreId));
    
    if (index > -1) {
        genres.splice(index, 1);
    } else {
        genres.push(parseInt(genreId));
    }
    
    // Update UI
    const btn = document.querySelector(`[data-genre="${genreId}"]`);
    if (btn) {
        btn.classList.toggle('active', index === -1);
    }
}

/**
 * Clear all filters
 */
function clearFilters() {
    window.discoverState.filters.genres = [];
    window.discoverState.filters.minRating = 0;
    window.discoverState.filters.yearFrom = 1900;
    window.discoverState.filters.yearTo = new Date().getFullYear();
    window.discoverState.sortBy = 'relevance';

    // Reset UI
    if (ratingSlider) {
        ratingSlider.value = 0;
        ratingDisplay.textContent = '0.0';
    }

    if (yearFromInput) yearFromInput.value = 1900;
    if (yearToInput) yearToInput.value = new Date().getFullYear();

    document.querySelectorAll('[data-filter="genre"]').forEach(btn => {
        btn.classList.remove('active');
    });

    // Reset sort UI
    document.querySelectorAll('[data-filter="sort"]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.value === 'relevance');
    });
}

/**
 * Apply filters and re-search
 */
function applyFilters() {
    if (window.discoverState.searchTerm) {
        window.discoverState.page = 1;
        window.discoverState.autoLoadCount = 0;
        searchContent(window.discoverState.searchTerm);
    }

    closeFilters();
}

/**
 * Switch between tabs
 */
function switchTab(tab) {
    window.discoverState.currentTab = tab;
    window.discoverState.page = 1;
    window.discoverState.autoLoadCount = 0;

    // Update UI
    tabButtons.forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tab);
    });

    // Reset MDBList selection when switching to other tabs
    if (tab !== 'mdblist' && typeof resetMDBListSelection === 'function') {
        resetMDBListSelection();
    }

    // Reset FlixPatrol selection when switching to other tabs
    if (tab !== 'flixpatrol' && typeof resetFlixPatrolSelection === 'function') {
        resetFlixPatrolSelection();
    }

    if (tab === 'search' && window.discoverState.searchTerm) {
        searchContent(window.discoverState.searchTerm);
    } else if (tab === 'trending') {
        loadTrending();
    }

    updateTabState();
}

/**
 * Update tab button states
 */
function updateTabState() {
    tabButtons.forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === window.discoverState.currentTab);
    });
}

/**
 * Open filter drawer
 */
function openFilters() {
    filterDrawer.classList.add('open');
    filterOverlay.classList.add('active');
    document.body.style.overflow = 'hidden';
}

/**
 * Close filter drawer
 */
function closeFilters() {
    filterDrawer.classList.remove('open');
    filterOverlay.classList.remove('active');
    document.body.style.overflow = '';
}

/**
 * Toggle filter section (accordion behavior - one open at a time)
 */
function toggleFilterSection(section) {
    const isOpen = section.classList.contains('open');

    // Close all other sections (accordion behavior)
    document.querySelectorAll('.filter-section:not(.sort-section)').forEach(s => {
        s.classList.remove('open');
    });

    // Toggle clicked section
    if (!isOpen) {
        section.classList.add('open');
    }
}

/**
 * Clear all filters and reload trending content
 */
function clearFiltersAndReload() {
    // Clear saved filters from localStorage FIRST
    clearSavedFilters();
    
    // Reset filter state (including excluded arrays)
    const state = window.discoverState;
    
    // Set flag to prevent saving during clear
    state.isClearing = true;
    
    // Reset restoration flag to prevent any filter restoration
    state.hasRestoredFilters = false;
    
    // Cancel any pending live filter timeout to prevent automatic filter application
    if (window.liveFilterTimeout) {
        clearTimeout(window.liveFilterTimeout);
        window.liveFilterTimeout = null;
    }
    
    state.filters = {
        searchQuery: '',
        sortBy: 'popularity',
        sortOrder: 'desc',
        mediaType: 'all',
        yearFrom: '',
        yearTo: '',
        releasedWithin: '',
        upcomingDays: '',
        tmdbRatingMin: 0,
        tmdbRatingMax: 10,
        imdbRatingMin: 0,
        imdbRatingMax: 10,
        tmdbVotesMin: 0,
        imdbVotesMin: 0,
        selectedGenres: [],
        excludedGenres: [],
        selectedLanguages: [],
        excludedLanguages: [],
        selectedCountries: [],
        excludedCountries: [],
        certificationMin: '',
        certificationMax: '',
        selectedProviders: [],
        excludedProviders: [],
        watchRegion: 'US',
        selectedNetworks: [],
        excludedNetworks: [],
        selectedCompanies: [],
        excludedCompanies: [],
        companyCache: {},
        selectedKeywords: [],
        excludedKeywords: [],
        keywordCache: {},
        runtimeMin: 0,
        runtimeMax: 300,
        activeFilters: []
    };

    // Reset UI elements
    resetFilterUI();

    // Reset watch region select
    const watchRegionSelect = document.getElementById('watch-region');
    if (watchRegionSelect) {
        watchRegionSelect.value = 'US';
    }

    // Clear search
    if (searchInput) {
        searchInput.value = '';
    }
    if (searchClearBtn) {
        searchClearBtn.style.display = 'none';
    }
    state.searchTerm = '';

    // Hide clear button
    const clearBtn = document.getElementById('clear-filters-btn-header');
    if (clearBtn) {
        clearBtn.style.display = 'none';
    }

    // Return to trending view
    if (trendingContent) trendingContent.style.display = 'block';
    if (searchResults) searchResults.style.display = 'none';

    // Reload trending
    loadTrending();
    
    // Clear the flag
    state.isClearing = false;
}

/**
 * Reset all filter UI elements to defaults
 */
function resetFilterUI() {
    const state = window.discoverState;

    // Reset sort dropdown
    if (state.sortByDropdown) state.sortByDropdown.value = 'popularity';

    // Reset sort order toggle
    const sortOrderToggle = document.getElementById('sort-order-toggle');
    if (sortOrderToggle) {
        sortOrderToggle.dataset.order = 'desc';
        const descIcon = sortOrderToggle.querySelector('.sort-icon-desc');
        const ascIcon = sortOrderToggle.querySelector('.sort-icon-asc');
        if (descIcon) descIcon.style.display = '';
        if (ascIcon) ascIcon.style.display = 'none';
    }

    // Reset media type buttons
    document.querySelectorAll('[data-filter="type"]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.value === 'all');
    });

    // Reset year inputs
    if (state.yearFromInput) state.yearFromInput.value = '1900';
    if (state.yearToInput) state.yearToInput.value = new Date().getFullYear();

    // Reset other inputs
    if (state.releasedWithinInput) state.releasedWithinInput.value = '';
    if (state.upcomingDaysInput) state.upcomingDaysInput.value = '';
    if (state.tmdbRatingMinInput) state.tmdbRatingMinInput.value = '0';
    if (state.tmdbRatingMaxInput) state.tmdbRatingMaxInput.value = '10';
    if (state.tmdbVotesMinInput) state.tmdbVotesMinInput.value = '0';
    if (state.runtimeMinInput) state.runtimeMinInput.value = '0';
    if (state.runtimeMaxInput) state.runtimeMaxInput.value = '300';

    // Clear all chips and dropdown states
    ['genres', 'keyword', 'language', 'country', 'provider', 'network', 'company'].forEach(type => {
        const chipsWrapper = document.getElementById(`${type}-chips`);
        if (chipsWrapper) chipsWrapper.innerHTML = '';

        // Clear dropdown item states
        const dropdown = document.getElementById(`${type}-dropdown`);
        if (dropdown) {
            dropdown.querySelectorAll('.chips-dropdown-item.included, .chips-dropdown-item.excluded, .chips-dropdown-item.selected').forEach(item => {
                item.classList.remove('included', 'excluded', 'selected');
            });
        }
    });

    // Reset keyword dropdown to initial state
    const keywordDropdown = document.getElementById('keyword-dropdown');
    if (keywordDropdown) {
        keywordDropdown.innerHTML = '<div class="chips-dropdown-empty">Type to search keywords...</div>';
    }
    const keywordSearch = document.getElementById('keyword-search');
    if (keywordSearch) {
        keywordSearch.value = '';
    }

    // Update active filters display
    updateActiveFilters();
}

/**
 * Restore UI elements to match loaded filter state
 */
function restoreFilterUI() {
    const state = window.discoverState;
    const f = state.filters;
    
    if (!f) return;
    
    console.log('[Discover] Restoring filter UI state');
    
    // Sort controls
    if (state.sortBySelect && f.sortBy) {
        state.sortBySelect.value = f.sortBy;
    }
    
    if (state.sortOrderToggle && f.sortOrder) {
        state.sortOrderToggle.setAttribute('data-order', f.sortOrder);
        const descIcon = state.sortOrderToggle.querySelector('.sort-icon-desc');
        const ascIcon = state.sortOrderToggle.querySelector('.sort-icon-asc');
        if (descIcon) descIcon.style.display = f.sortOrder === 'desc' ? 'block' : 'none';
        if (ascIcon) ascIcon.style.display = f.sortOrder === 'asc' ? 'block' : 'none';
    }
    
    // Media type buttons
    document.querySelectorAll('[data-filter="type"]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.value === (f.mediaType || 'all'));
    });
    
    // Year inputs
    if (state.yearFromInput && f.yearFrom) state.yearFromInput.value = f.yearFrom;
    if (state.yearToInput && f.yearTo) state.yearToInput.value = f.yearTo;
    
    // Date range inputs
    if (state.releasedWithinInput && f.releasedWithin) state.releasedWithinInput.value = f.releasedWithin;
    if (state.upcomingDaysInput && f.upcomingDays) state.upcomingDaysInput.value = f.upcomingDays;
    
    // Rating inputs
    if (state.tmdbRatingMinInput && f.tmdbRatingMin !== undefined) state.tmdbRatingMinInput.value = f.tmdbRatingMin;
    if (state.tmdbRatingMaxInput && f.tmdbRatingMax !== undefined) state.tmdbRatingMaxInput.value = f.tmdbRatingMax;
    if (state.imdbRatingMinInput && f.imdbRatingMin !== undefined) state.imdbRatingMinInput.value = f.imdbRatingMin;
    if (state.imdbRatingMaxInput && f.imdbRatingMax !== undefined) state.imdbRatingMaxInput.value = f.imdbRatingMax;
    
    // Vote count inputs
    if (state.tmdbVotesMinInput && f.tmdbVotesMin) state.tmdbVotesMinInput.value = f.tmdbVotesMin;
    if (state.imdbVotesMinInput && f.imdbVotesMin) state.imdbVotesMinInput.value = f.imdbVotesMin;
    
    // Runtime inputs
    if (state.runtimeMinInput && f.runtimeMin) state.runtimeMinInput.value = f.runtimeMin;
    if (state.runtimeMaxInput && f.runtimeMax) state.runtimeMaxInput.value = f.runtimeMax;

    // Revenue inputs

    // Watch region
    const watchRegionSelect = document.getElementById('watch-region');
    if (watchRegionSelect && f.watchRegion) {
        watchRegionSelect.value = f.watchRegion;
    }
    
    // Title filter - always clear/set to ensure old values don't persist
    const titleFilterInput = document.getElementById('title-filter');
    if (titleFilterInput) {
        titleFilterInput.value = f.titleFilter || '';
        state.filters.titleFilter = f.titleFilter || '';
    }
    
    // Restore chips for all multi-select filters after genres are loaded
    // This will be called again after loadGenres() completes via updateActiveFilters
    setTimeout(() => {
        // Only apply if filters haven't been cleared in the meantime
        if (!window.discoverState.hasRestoredFilters) {
            return;
        }

        restoreChipsFromState();
        updateActiveFilters();
        // Also restore range sliders
        if (typeof initializeRangeSliders === 'function') {
            initializeRangeSliders();
        }

        // Apply the restored filters to show results
        applyAdvancedFilters(false); // Don't close drawer, just apply filters
    }, 700);
}

/**
 * Restore chips for all filter types from state
 */
function restoreChipsFromState() {
    const f = window.discoverState.filters;
    
    // Restore genre chips
    if ((f.selectedGenres && f.selectedGenres.length > 0) || (f.excludedGenres && f.excludedGenres.length > 0)) {
        renderGenreFilters();
    }
    
    // Restore keyword chips - use loadAndRenderKeywordChips to fetch missing names from API
    if ((f.selectedKeywords && f.selectedKeywords.length > 0) || (f.excludedKeywords && f.excludedKeywords.length > 0)) {
        if (typeof loadAndRenderKeywordChips === 'function') {
            loadAndRenderKeywordChips(f.selectedKeywords || [], f.excludedKeywords || []);
        } else if (typeof renderKeywordChipsFromState === 'function') {
            renderKeywordChipsFromState();
        }
    }
    
    // Restore company chips  
    if ((f.selectedCompanies && f.selectedCompanies.length > 0) || (f.excludedCompanies && f.excludedCompanies.length > 0)) {
        if (typeof renderCompanyChipsFromState === 'function') {
            renderCompanyChipsFromState();
        }
    }
    
    // Restore other chips using existing applyChipsFromSavedFilters if available
    if (typeof applyChipsFromSavedFilters === 'function') {
        if (f.selectedLanguages || f.excludedLanguages) {
            applyChipsFromSavedFilters('language', f.selectedLanguages || [], f.excludedLanguages || []);
        }
        if (f.selectedCountries || f.excludedCountries) {
            applyChipsFromSavedFilters('country', f.selectedCountries || [], f.excludedCountries || []);
        }
        if (f.selectedProviders || f.excludedProviders) {
            applyChipsFromSavedFilters('provider', f.selectedProviders || [], f.excludedProviders || []);
        }
        if (f.selectedNetworks || f.excludedNetworks) {
            applyChipsFromSavedFilters('network', f.selectedNetworks || [], f.excludedNetworks || []);
        }
        if (f.selectedCertifications || f.excludedCertifications) {
            applyChipsFromSavedFilters('certification', f.selectedCertifications || [], f.excludedCertifications || []);
        }
    }
}

/**
 * Check if any filters are active and show/hide clear button
 */
function updateClearButtonVisibility() {
    const state = window.discoverState;
    const f = state.filters;

    const hasActiveFilters =
        f.mediaType !== 'all' ||
        f.yearFrom > 1900 ||
        f.yearTo < new Date().getFullYear() ||
        f.releasedWithin ||
        f.upcomingDays ||
        f.tmdbRatingMin > 0 ||
        f.tmdbRatingMax < 10 ||
        f.tmdbVotesMin > 0 ||
        f.selectedGenres.length > 0 ||
        f.excludedGenres.length > 0 ||
        f.selectedKeywords.length > 0 ||
        f.excludedKeywords.length > 0 ||
        f.selectedLanguages.length > 0 ||
        f.excludedLanguages.length > 0 ||
        f.selectedCountries.length > 0 ||
        f.excludedCountries.length > 0 ||
        f.selectedProviders.length > 0 ||
        f.excludedProviders.length > 0 ||
        f.selectedNetworks.length > 0 ||
        f.excludedNetworks.length > 0 ||
        f.selectedCompanies.length > 0 ||
        f.excludedCompanies.length > 0 ||
        f.runtimeMin > 0 ||
        f.runtimeMax < 300 ||
        f.sortBy !== 'popularity' ||
        f.sortOrder !== 'desc';

    const clearBtn = document.getElementById('clear-filters-btn-header');
    if (clearBtn) {
        clearBtn.style.display = hasActiveFilters ? '' : 'none';
    }
}

/**
 * Filter results based on discover settings:
 * - Hide items without poster (if enabled in settings)
 * - Hide items without rating (if enabled in settings, with exceptions for upcoming/release date sorting)
 * - Only show missing items (if enabled in settings, filter by db_status)
 */
/**
 * Helper function to check if a title matches the filter (plain text or regex)
 * @param {string} titleFilter - The filter string (can be plain text or regex pattern like /pattern/flags)
 * @param {string} itemTitle - The title to match against
 * @returns {boolean} - True if matches, false otherwise
 */
function matchesTitleFilter(titleFilter, itemTitle) {
    if (!titleFilter || !itemTitle) return true; // No filter or no title = pass through
    
    // Check if it's a regex pattern (starts with / and has a closing /)
    const regexMatch = titleFilter.match(/^\/(.+?)\/([gimsuy]*)$/);
    
    if (regexMatch) {
        // It's a regex pattern
        try {
            const pattern = regexMatch[1];
            const flags = regexMatch[2] || '';
            const regex = new RegExp(pattern, flags);
            return regex.test(itemTitle);
        } catch (e) {
            console.warn('[Discover] Invalid regex pattern:', titleFilter, e);
            return true; // On error, don't filter out
        }
    } else {
        // It's plain text - case-insensitive substring match
        return itemTitle.toLowerCase().includes(titleFilter.toLowerCase());
    }
}

function filterValidResults(results) {
    const filters = window.discoverState.filters;
    const settings = window.discoverState.discoverSettings;
    const sortBy = filters.sortBy || '';
    const upcomingDays = filters.upcomingDays;

    const isReleaseDateSort = sortBy.includes('release_date') || sortBy.includes('primary_release_date') || sortBy.includes('first_air_date');
    const hasUpcomingFilter = upcomingDays !== '' && upcomingDays !== null && upcomingDays !== undefined;

    console.log('[Discover] filterValidResults - settings:', settings);
    console.log('[Discover] filterValidResults - total items:', results.length);

    const filtered = results.filter(item => {
        // Check poster filter
        if (settings.hide_no_poster && !item.poster_path) return false;

        // Check rating filter (skip if sorting by release date or using upcoming filter)
        if (settings.hide_no_rating && !isReleaseDateSort && !hasUpcomingFilter) {
            if (!item.vote_average || item.vote_average <= 0) return false;
        }

        // Check only show missing filter
        if (settings.only_show_missing) {
            // Only show items with db_status === 'Missing' (case-insensitive check)
            // Items with other statuses like 'Collected', 'Unreleased', etc. should be filtered out
            if (item.db_status && item.db_status.toLowerCase() !== 'missing') {
                return false;
            }
        }

        // Check title filter (client-side)
        if (filters.titleFilter) {
            const itemTitle = item.title || item.name || '';
            if (!matchesTitleFilter(filters.titleFilter, itemTitle)) {
                return false;
            }
        }

        // Certification filters are handled entirely by TMDB API on backend
        // No client-side filtering needed - TMDB returns pre-filtered results

        return true;
    });

    return filtered;
}

/**
 * Render results
 */
function renderResults(results) {
    // Store raw results for re-rendering when display options change
    window.discoverState.currentResults = results || [];

    // Filter based on display options
    const filteredResults = filterValidResults(results || []);

    if (filteredResults.length === 0) {
        showEmpty();
        return;
    }

    hideEmpty();
    hideError();

    const html = filteredResults.map(item => createResultItemHTML(item)).join('');
    resultsGrid.innerHTML = html;

    // Bind result item events
    bindResultEvents();
}

/**
 * Append results (for pagination)
 */
function appendResults(results) {
    // Filter out items without poster or rating
    const filteredResults = filterValidResults(results || []);
    if (filteredResults.length === 0) return;

    const html = filteredResults.map(item => createResultItemHTML(item)).join('');
    resultsGrid.insertAdjacentHTML('beforeend', html);

    // Bind result events for new items
    bindResultEvents();
}

/**
 * Get status icon HTML based on db_status
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
 * Get media type icon HTML
 */
function getMediaTypeIcon(mediaType) {
    if (mediaType === 'movie') {
        // Film/movie icon
        return `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-4 h-4">
            <path stroke-linecap="round" stroke-linejoin="round" d="M3.375 19.5h17.25m-17.25 0a1.125 1.125 0 01-1.125-1.125M3.375 19.5h1.5C5.496 19.5 6 18.996 6 18.375m-2.625 0V5.625m0 12.75v-1.5c0-.621.504-1.125 1.125-1.125m18.375 2.625V5.625m0 12.75c0 .621-.504 1.125-1.125 1.125m1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125m0 3.75h-1.5A1.125 1.125 0 0118 18.375M20.625 4.5H3.375m17.25 0c.621 0 1.125.504 1.125 1.125M20.625 4.5h-1.5C18.504 4.5 18 5.004 18 5.625m3.75 0v1.5c0 .621-.504 1.125-1.125 1.125M3.375 4.5c-.621 0-1.125.504-1.125 1.125M3.375 4.5h1.5C5.496 4.5 6 5.004 6 5.625m-2.625 0v1.5c0 .621.504 1.125 1.125 1.125m0 0h1.5m-1.5 0c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125m1.5-3.75C5.496 8.25 6 7.746 6 7.125v-1.5M4.875 8.25C5.496 8.25 6 8.754 6 9.375v1.5m0-5.25v5.25m0-5.25C6 5.004 6.504 4.5 7.125 4.5h9.75c.621 0 1.125.504 1.125 1.125m1.125 2.625h1.5m-1.5 0A1.125 1.125 0 0118 7.125v-1.5m1.125 2.625c-.621 0-1.125.504-1.125 1.125v1.5m2.625-2.625c.621 0 1.125.504 1.125 1.125v1.5c0 .621-.504 1.125-1.125 1.125M18 5.625v5.25M7.125 12h9.75m-9.75 0A1.125 1.125 0 016 10.875M7.125 12C6.504 12 6 12.504 6 13.125m0-2.25C6 11.496 5.496 12 4.875 12M18 10.875c0 .621-.504 1.125-1.125 1.125M18 10.875c0 .621.504 1.125 1.125 1.125m-2.25 0c.621 0 1.125.504 1.125 1.125m-12 5.25v-5.25m0 5.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125m-12 0v-1.5c0-.621-.504-1.125-1.125-1.125M18 18.375v-5.25m0 5.25v-1.5c0-.621.504-1.125 1.125-1.125M18 13.125v1.5c0 .621.504 1.125 1.125 1.125M18 13.125c0-.621.504-1.125 1.125-1.125M6 13.125v1.5c0 .621-.504 1.125-1.125 1.125M6 13.125C6 12.504 5.496 12 4.875 12m-1.5 0h1.5m-1.5 0c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125M19.125 12h1.5m0 0c.621 0 1.125.504 1.125 1.125v1.5c0 .621-.504 1.125-1.125 1.125m-17.25 0h1.5m14.25 0h1.5" />
        </svg>`;
    } else {
        // TV icon
        return `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-4 h-4">
            <path stroke-linecap="round" stroke-linejoin="round" d="M6 20.25h12m-7.5-3v3m3-3v3m-10.125-3h17.25c.621 0 1.125-.504 1.125-1.125V4.875c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125z" />
        </svg>`;
    }
}

/**
 * Detect mixed states for TV shows with partial episodes
 * Returns { primary, secondary, isSplit } for split badge rendering
 */
function detectMixedStates(item) {
    // Only applies to TV shows with episode info
    if (item.media_type !== 'tv' || !item.episode_info) {
        return { primary: null, secondary: null, isSplit: false };
    }

    const episodeInfo = item.episode_info;
    const total = episodeInfo.total_episodes || 0;
    const collected = episodeInfo.collected_episodes || 0;
    const blacklisted = episodeInfo.blacklisted_episodes || 0;
    const unreleased = episodeInfo.unreleased_episodes || 0;
    const wanted = episodeInfo.wanted_episodes || 0;

    // Need at least 2 episodes to have a mixed state
    if (total < 2) {
        return { primary: null, secondary: null, isSplit: false };
    }

    // Count distinct states (only count if > 0 episodes in that state)
    const states = [];
    if (collected > 0) states.push('collected');
    if (wanted > 0) states.push('wanted');
    if (unreleased > 0) states.push('unreleased');
    if (blacklisted > 0) states.push('blacklisted');

    // Only split if we have exactly 2 states
    if (states.length === 2) {
        // Priority order: wanted > unreleased > collected > blacklisted
        const priority = { 'wanted': 0, 'unreleased': 1, 'collected': 2, 'blacklisted': 3 };
        states.sort((a, b) => priority[a] - priority[b]);

        return {
            primary: states[0],
            secondary: states[1],
            isSplit: true
        };
    }

    return { primary: null, secondary: null, isSplit: false };
}

/**
 * Create HTML for a result item
 */
function createResultItemHTML(item) {
    const title = item.title || item.name || 'Unknown Title';
    const year = (item.release_date || item.first_air_date || '').substring(0, 4) || '';
    const releaseDate = item.release_date || item.first_air_date || '';
    const formattedDate = releaseDate ? formatReleaseDate(releaseDate) : '';
    const posterUrl = item.poster_path ? `https://image.tmdb.org/t/p/w342${item.poster_path}` : '/static/images/placeholder.png';
    const overview = item.overview || 'No overview available.';
    const rating = item.vote_average ? item.vote_average.toFixed(1) : '';

    // Check for mixed states (TV shows only)
    const mixedStates = detectMixedStates(item);
    // Don't apply state class if we have a split badge (to avoid CSS conflicts)
    const iconState = mixedStates.primary || (item.db_status || 'missing').toLowerCase();
    const statusClass = mixedStates.isSplit ? '' : iconState;
    const statusIcon = getStatusIcon(iconState);
    const splitClass = mixedStates.isSplit ? `split-badge split-${mixedStates.primary}-${mixedStates.secondary}` : '';
    const statusTitle = mixedStates.isSplit ? `${mixedStates.primary} + ${mixedStates.secondary}` : iconState;

    const mediaTypeIcon = getMediaTypeIcon(item.media_type);
    const mediaTypeClass = item.media_type === 'movie' ? 'badge-movie' : 'badge-tv';

    // Get release type for color-coding (digital or theatrical) - only for movies
    const releaseClass = item.media_type === 'movie' && item.release_type ? `release-${item.release_type}` : '';

    // Build compact info string: "⭐ 7.9  •  Jan 15  •  2025"
    let infoText = '';
    if (rating) infoText += `⭐ ${rating}`;
    if (formattedDate) {
        // Only apply color class for movies with release_type
        if (releaseClass) {
            infoText += (infoText ? '  •  ' : '') + `<span class="${releaseClass}">${formattedDate}</span>`;
        } else {
            infoText += (infoText ? '  •  ' : '') + formattedDate;
        }
    }
    if (year) infoText += (infoText ? '  •  ' : '') + year;

    // Check admin permissions for tester icon
    const hasAdminPermissions = document.getElementById('has_admin_permissions')?.value === 'True';
    const testerIconHtml = hasAdminPermissions ? `
            <!-- Tester icon - bottom left corner -->
            <div class="tester-icon" title="Test this content">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M9 3h6v4H9zM6 7h12l-3 10H9z"></path>
                    <path d="M10 17h4v4h-4z"></path>
                </svg>
            </div>` : '';

    return `
        <div class="result-item" data-id="${item.id}" data-type="${item.media_type}" data-status="${statusClass}">
            <!-- Status badge - top left with icon -->
            <div class="db-status-badge ${statusClass} ${splitClass}" title="${statusTitle}">
                ${statusIcon}
            </div>
            <!-- Media type badge - top right with icon -->
            <div class="media-type-badge ${mediaTypeClass}" title="${item.media_type === 'movie' ? 'Movie' : 'TV Show'}">
                ${mediaTypeIcon}
            </div>
            <!-- Rank overlay - top center (only for FlixPatrol Top 10 lists) -->
            ${item.rank && window.flixpatrolState && window.flixpatrolState.currentPlatform ? `<div class="rank-overlay">${String(item.rank).padStart(2, '0')}</div>` : ''}
            <img src="${posterUrl}" alt="${title}" class="result-poster" loading="lazy">
            <!-- Bottom info bar - compact text -->
            <div class="result-bottom-bar">
                <span class="result-info">${infoText}</span>
            </div>
            <!-- Hover overlay with full details -->
            <div class="result-hover-overlay">
                <h3 class="result-title">${title}</h3>
                <div class="result-meta">
                    <span class="result-year">${year}</span>
                    <span class="result-rating">
                        <svg xmlns="http://www.w3.org/2000/svg" fill="currentColor" viewBox="0 0 24 24">
                            <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
                        </svg>
                        ${rating}
                    </span>
                </div>
                <p class="result-overview">${overview}</p>
            </div>
            <!-- Request icon - bottom right corner -->
            <div class="request-icon" title="Request this content">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="8" x2="12" y2="16"></line>
                    <line x1="8" y1="12" x2="16" y2="12"></line>
                </svg>
            </div>
            ${testerIconHtml}
        </div>
    `;
}

/**
 * Format release date to short month and day format
 * Parse manually to avoid timezone issues
 */
function formatReleaseDate(dateString) {
    if (!dateString) return '';
    
    // Parse date manually to avoid timezone conversion
    const parts = dateString.split('-');
    if (parts.length !== 3) return dateString;
    
    const year = parseInt(parts[0], 10);
    const month = parseInt(parts[1], 10) - 1; // Month is 0-indexed
    const day = parseInt(parts[2], 10);
    
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return `${months[month]} ${day}`;
}

/**
 * Bind events to result items
 * All items link to their library pages
 */
function bindResultEvents() {
    document.querySelectorAll('.result-item').forEach(item => {
        const id = item.dataset.id;
        const type = item.dataset.type;
        const status = item.dataset.status;

        // Find the full item data from currentResults
        const resultItem = window.discoverState.currentResults.find(r => r.id == id);

        item.style.cursor = 'pointer';
        item.addEventListener('click', function() {
            // Navigate to the appropriate page based on status
            let targetPath;
            if (status === 'missing' || status === 'partial') {
                // Check setting for TV shows not in library
                const discoverEpisodeView = window.discoverState.discoverSettings.tv_show_episode_view || 'discover';

                // TV shows can route to either discover details or addmedia page based on setting
                // Movies always go to discover details
                if (type === 'tv' && discoverEpisodeView === 'add_media') {
                    if (resultItem) {
                        // Navigate to dedicated addmedia page with URL parameters
                        event.preventDefault();

                        // Convert genre IDs to names if needed (TMDB uses integer IDs)
                        const genreIdToName = {
                            28: 'Action', 12: 'Adventure', 16: 'Animation', 35: 'Comedy',
                            80: 'Crime', 99: 'Documentary', 18: 'Drama', 10751: 'Family',
                            14: 'Fantasy', 36: 'History', 27: 'Horror', 10402: 'Music',
                            9648: 'Mystery', 10749: 'Romance', 878: 'Science Fiction',
                            10770: 'TV Movie', 53: 'Thriller', 10752: 'War', 37: 'Western',
                            10759: 'Action & Adventure', 10762: 'Kids', 10763: 'News',
                            10764: 'Reality', 10765: 'Sci-Fi & Fantasy', 10766: 'Soap',
                            10767: 'Talk', 10768: 'War & Politics'
                        };

                        let genreNames = resultItem.genre_ids || [];
                        if (genreNames.length > 0 && typeof genreNames[0] === 'number') {
                            genreNames = genreNames.map(id => genreIdToName[id] || String(id)).filter(g => g);
                        }

                        // Build URL with parameters
                        const params = new URLSearchParams({
                            id: resultItem.id,
                            title: resultItem.title || resultItem.name || 'Unknown',
                            year: (resultItem.release_date || resultItem.first_air_date || '').substring(0, 4) || '',
                            type: 'tv',
                            rating: resultItem.vote_average || 0,
                            vote_average: resultItem.vote_average || 0,
                            genres: JSON.stringify(genreNames),
                            backdrop: resultItem.backdrop_path || '',
                            overview: resultItem.overview || 'No overview available'
                        });

                        targetPath = `/discover/addmedia?${params.toString()}`;
                        console.log('Navigating to addmedia page:', targetPath);
                    } else {
                        // Fallback to discover details if item not found
                        targetPath = `/discover/details/${id}/${type}`;
                    }
                } else {
                    // Route to discover detail page (default for movies, optional for TV)
                    targetPath = `/discover/details/${id}/${type}`;
                }
            } else {
                // Collected items go to library page
                targetPath = type === 'movie' ? `/library/movie/${id}` : `/library/show/${id}`;
            }
            window.location.href = targetPath;
        });

        // Add click handlers for request and tester icons
        const requestIcon = item.querySelector('.request-icon');
        const testerIcon = item.querySelector('.tester-icon');

        if (requestIcon && resultItem) {
            requestIcon.addEventListener('click', function(e) {
                e.stopPropagation(); // Prevent card click
                e.preventDefault();

                const content = {
                    id: resultItem.id,
                    title: resultItem.title || resultItem.name || 'Unknown',
                    year: (resultItem.release_date || resultItem.first_air_date || '').substring(0, 4) || '',
                    mediaType: type,
                    rating: resultItem.vote_average || 0,
                    genres: resultItem.genre_ids || [],
                    backdrop: resultItem.backdrop_path || '',
                    overview: resultItem.overview || ''
                };

                showVersionModal(content);
            });
        }

        if (testerIcon && resultItem) {
            testerIcon.addEventListener('click', function(e) {
                e.stopPropagation(); // Prevent card click
                e.preventDefault();

                // Redirect directly to scraper_tester page with URL parameters
                const params = new URLSearchParams({
                    title: resultItem.title || resultItem.name || 'Unknown',
                    id: resultItem.id,
                    year: (resultItem.release_date || resultItem.first_air_date || '').substring(0, 4) || '',
                    media_type: type
                });

                window.location.href = `/scraper/scraper_tester?${params.toString()}`;
            });
        }
    });
}

/**
 * Convert genre IDs to genre names for scraper compatibility
 * @param {Array<number>} genreIds - Array of genre IDs
 * @returns {Array<string>} - Array of genre names
 */
function convertGenreIdsToNames(genreIds) {
    if (!genreIds || genreIds.length === 0) return [];

    const state = window.discoverState;
    const allGenres = new Map();

    // Build genre mapping from both movie and TV genres
    (state.genres.movie || []).forEach(g => allGenres.set(g.id, g.name));
    (state.genres.tv || []).forEach(g => allGenres.set(g.id, g.name));

    // Convert IDs to names
    return genreIds
        .map(id => allGenres.get(id))
        .filter(name => name); // Remove undefined entries
}

/**
 * Handle infinite scroll - triggered when user scrolls near bottom
 */
function handleInfiniteScroll() {
    // Only trigger infinite scroll when search results are visible (not trending view)
    if (!searchResults) return;

    // Use computed style for reliable visibility check
    const computedDisplay = window.getComputedStyle(searchResults).display;
    const isVisible = computedDisplay !== 'none';

    if (isVisible) {
        const scrollPosition = window.innerHeight + window.scrollY;
        const threshold = document.documentElement.scrollHeight - 500;

        // Check if user is near the bottom (500px threshold for smooth loading)
        if (scrollPosition >= threshold) {
            // Reset auto-load counter on manual scroll, allowing more auto-loads after user action
            window.discoverState.autoLoadCount = 0;
            loadMore();
        }
    }
}

/**
 * Load more results
 */
function loadMore() {
    if (!window.discoverState.hasMore || window.discoverState.isLoading) {
        return;
    }

    window.discoverState.page++;
    console.log('[Discover] Loading more results, page:', window.discoverState.page);

    // Check if we're in filter mode or search mode
    const state = window.discoverState;
    const searchResultsVisible = searchResults &&
        window.getComputedStyle(searchResults).display !== 'none';

    if (state.searchTerm) {
        // Search mode
        searchContent(state.searchTerm);
    } else if (searchResultsVisible) {
        // Filter mode - rebuild params and fetch more
        let sortByValue = state.filters.sortBy;
        if (sortByValue && !sortByValue.includes('.')) {
            sortByValue = `${sortByValue}.${state.filters.sortOrder}`;
        }

        let mediaType = state.filters.mediaType;
        if (mediaType === 'all') {
            mediaType = 'movie';
        }

        // Check if specific date filters are set (these take priority over year range)
        const hasDateFilter = state.filters.releasedWithin || state.filters.upcomingDays;

        const params = new URLSearchParams({
            type: mediaType,
            page: state.page,
            sort_by: sortByValue,
            year_from: !hasDateFilter && state.filters.yearFrom ? state.filters.yearFrom : '',
            year_to: !hasDateFilter && state.filters.yearTo ? state.filters.yearTo : '',
            released_within: state.filters.releasedWithin,
            upcoming_days: state.filters.upcomingDays,
            tmdb_rating_min: state.filters.tmdbRatingMin > 0 ? state.filters.tmdbRatingMin : '',
            tmdb_rating_max: state.filters.tmdbRatingMax < 10 ? state.filters.tmdbRatingMax : '',
            tmdb_votes_min: state.filters.tmdbVotesMin > 0 ? state.filters.tmdbVotesMin : '',
            genres: state.filters.selectedGenres.join(','),
            genres_exclude: state.filters.excludedGenres.join(','),
            keywords: state.filters.selectedKeywords.join(','),
            keywords_exclude: state.filters.excludedKeywords.join(','),
            language: state.filters.selectedLanguages.join(','),
            language_exclude: state.filters.excludedLanguages.join(','),
            country: state.filters.selectedCountries.join(','),
            country_exclude: state.filters.excludedCountries.join(','),
            watch_provider: state.filters.selectedProviders.join(','),
            watch_provider_exclude: state.filters.excludedProviders.join(','),
            watch_region: state.filters.watchRegion || 'US',
            network: state.filters.selectedNetworks.join(','),
            network_exclude: state.filters.excludedNetworks.join(','),
            runtime_min: state.filters.runtimeMin > 0 ? state.filters.runtimeMin : '',
            runtime_max: state.filters.runtimeMax < 300 ? state.filters.runtimeMax : '',
            production_company: state.filters.selectedCompanies.join(','),
            production_company_exclude: state.filters.excludedCompanies.join(',')
        });

        fetchAdvancedFilterResults(params);
    } else if (window.discoverState.currentTab === 'trending') {
        // Trending doesn't support pagination, so just reload
        loadTrending();
    }
}

/**
 * Set loading flag - just sets the flag for infinite scroll protection
 * No visual loading indicator - the discover page is fast enough
 * Named setLoadingFlag to avoid conflict with global window.showLoading from notifications.js
 */
function setLoadingFlag() {
    window.discoverState.isLoading = true;
}

/**
 * Clear loading flag - clears the flag to allow more loading
 * Named clearLoadingFlag to avoid conflict with global window.hideLoading from notifications.js
 */
function clearLoadingFlag() {
    window.discoverState.isLoading = false;
}

/**
 * Show empty state
 */
function showEmpty() {
    resultsGrid.classList.add('hidden-state');
    emptyState.classList.add('active');
    errorState.classList.remove('active');
}

/**
 * Hide empty state
 */
function hideEmpty() {
    resultsGrid.classList.remove('hidden-state');
    emptyState.classList.remove('active');
}

/**
 * Show error state
 */
function showError() {
    resultsGrid.classList.add('hidden-state');
    emptyState.classList.remove('active');
    errorState.classList.add('active');
}

/**
 * Hide error state
 */
function hideError() {
    errorState.classList.remove('active');
}

/**
 * Update pagination - hide load more button, show end message when no more results
 */
function updatePagination() {
    // Hide the load more button - we use infinite scroll now
    if (loadMoreBtn) {
        loadMoreBtn.style.display = 'none';
    }

    // Only show "No more results" message when we've reached the end
    if (!window.discoverState.hasMore && window.discoverState.page > 1) {
        pagination.style.display = 'flex';
        pagination.innerHTML = '<div class="end-of-results">No more results</div>';
    } else {
        pagination.style.display = 'none';
    }
}

/**
 * Update results info display (total results and current page)
 * @param {Object} data - Response data containing page, total_pages, total_results
 */
function updateResultsInfo(data) {
    const resultsCount = document.getElementById('results-count');
    const resultsPage = document.getElementById('results-page');

    if (!resultsCount || !resultsPage) return;

    // Format total results count
    const totalResults = data.total_results || 0;
    let countText;
    if (totalResults >= 10000) {
        countText = '10,000+ results';
    } else if (totalResults > 0) {
        countText = totalResults.toLocaleString() + ' results';
    } else {
        countText = 'No results';
    }

    // Format page info
    const currentPage = data.page || 1;
    const totalPages = data.total_pages || 1;
    const pageText = `Page ${currentPage} of ${totalPages}`;

    resultsCount.textContent = countText;
    resultsPage.textContent = pageText;
}

/**
 * Debounce function
 */
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

/**
 * Add item to library
 */
async function addToLibrary(tmdbId, mediaType, title) {
    try {
        const response = await fetch('/discover/api/add', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                tmdb_id: tmdbId,
                media_type: mediaType,
                title: title
            })
        });

        const data = await response.json();

        if (data.success) {
            // Show success toast/notification
            showNotification(`Added "${title}" to library`, 'success');

            // Update the card's status badge
            const itemElement = document.querySelector(`[data-id="${tmdbId}"]`);
            if (itemElement) {
                const badge = itemElement.querySelector('.db-status-badge');
                if (badge) {
                    badge.className = 'db-status-badge wanted';
                    badge.textContent = 'wanted';
                }
            }

            // If we have an IMDB ID, optionally redirect to scraper
            if (data.imdb_id) {
                console.log(`[Discover] IMDB ID: ${data.imdb_id}`);
            }
        } else {
            showNotification(data.error || 'Failed to add to library', 'error');
        }

    } catch (error) {
        console.error('[Discover] Add to library error:', error);
        showNotification('Failed to add to library', 'error');
    }
}

/**
 * Show notification toast
 */
function showNotification(message, type = 'info') {
    // Try to use existing toast system if available
    if (typeof showToast === 'function') {
        showToast(message, type);
        return;
    }

    // Fallback: Create a simple notification
    const notification = document.createElement('div');
    notification.className = `discover-notification ${type}`;
    notification.textContent = message;
    notification.style.cssText = `
        position: fixed;
        bottom: 2rem;
        right: 2rem;
        padding: 1rem 1.5rem;
        border-radius: 0.5rem;
        background-color: ${type === 'success' ? '#10b981' : type === 'error' ? '#ef4444' : '#3b82f6'};
        color: white;
        font-weight: 500;
        z-index: 9999;
        animation: slideIn 0.3s ease-out;
    `;

    document.body.appendChild(notification);

    setTimeout(() => {
        notification.style.animation = 'slideOut 0.3s ease-in';
        setTimeout(() => notification.remove(), 300);
    }, 3000);
}

/**
 * Filter content using advanced filters
 */
async function filterContent() {
    try {
        setLoadingFlag();

        const params = new URLSearchParams({
            type: window.discoverState.mediaType === 'all' ? 'movie' : window.discoverState.mediaType,
            page: window.discoverState.page
        });

        // Add filters
        if (window.discoverState.filters.genres.length > 0) {
            params.append('genres', window.discoverState.filters.genres.join(','));
        }

        if (window.discoverState.filters.minRating > 0) {
            params.append('min_rating', window.discoverState.filters.minRating);
        }

        if (window.discoverState.filters.yearFrom > 1900) {
            params.append('year_from', window.discoverState.filters.yearFrom);
        }

        if (window.discoverState.filters.yearTo < new Date().getFullYear()) {
            params.append('year_to', window.discoverState.filters.yearTo);
        }

        const response = await fetch(`/discover/api/filter?${params}`);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();
        window.discoverState.hasMore = data.page < data.total_pages;

        if (window.discoverState.page === 1) {
            renderResults(data.results);
        } else {
            appendResults(data.results);
        }

        updatePagination();

    } catch (error) {
        console.error('[Discover] Filter error:', error);
        showError();
    } finally {
        clearLoadingFlag();
    }
}

// =============================================================================
// ADAPTIVE LIST FUNCTIONALITY
// =============================================================================

/**
 * Adaptive List state for edit mode
 */
window.adaptiveListEditMode = {
    isEditing: false,
    editIndex: null,
    sourceId: null
};

/**
 * Initialize adaptive list functionality
 */
function initAdaptiveList() {
    const saveBtn = document.getElementById('save-adaptive-list-btn');
    const modal = document.getElementById('adaptive-list-modal');
    const closeBtn = document.getElementById('adaptive-list-modal-close');
    const cancelBtn = document.getElementById('adaptive-list-cancel-btn');
    const confirmSaveBtn = document.getElementById('adaptive-list-save-btn');

    if (saveBtn) {
        saveBtn.addEventListener('click', openAdaptiveListModal);
    }

    if (closeBtn) {
        closeBtn.addEventListener('click', closeAdaptiveListModal);
    }

    if (cancelBtn) {
        cancelBtn.addEventListener('click', closeAdaptiveListModal);
    }

    if (confirmSaveBtn) {
        confirmSaveBtn.addEventListener('click', saveAdaptiveList);
    }

    // Close modal when clicking overlay
    if (modal) {
        modal.addEventListener('click', function(e) {
            if (e.target === modal) {
                closeAdaptiveListModal();
            }
        });
    }

    // Check for edit mode from URL params
    checkEditMode();
}

/**
 * Check if we're in edit or create mode (from URL params)
 */
function checkEditMode() {
    const urlParams = new URLSearchParams(window.location.search);
    const editSourceId = urlParams.get('edit_adaptive_list');  // Now this is the source_id
    const mode = urlParams.get('mode');

    if (editSourceId !== null) {
        // Edit existing adaptive list - editSourceId is now the full source_id like "Adaptive List_1"
        loadAdaptiveListForEdit(editSourceId);
    } else if (mode === 'create_adaptive_list') {
        // Create new adaptive list mode
        window.adaptiveListEditMode = {
            isEditing: false,
            sourceId: null  // Will be assigned when saved
        };

        // Show notification
        showNotification('Configure your filters, then click "Save as Adaptive List" to save.', 'info');

        // Open the filter drawer after a short delay
        setTimeout(() => {
            openFilters();
        }, 500);
    }
}

/**
 * Load an adaptive list for editing
 * @param {string} sourceId - The content source ID like "Adaptive List_1"
 */
async function loadAdaptiveListForEdit(sourceId) {
    try {
        const response = await fetch(`/discover/api/adaptive-lists/${encodeURIComponent(sourceId)}`);
        if (!response.ok) {
            throw new Error('Failed to load adaptive list');
        }

        const data = await response.json();
        if (data.success && data.list) {
            // Set edit mode
            window.adaptiveListEditMode = {
                isEditing: true,
                sourceId: sourceId
            };

            // Store the list name for later
            window.adaptiveListEditMode.originalName = data.list.name;

            // Set the media type filter FIRST (using filter buttons, not a select)
            if (data.list.media_type) {
                const mediaType = data.list.media_type;
                // Update state
                window.discoverState.mediaType = mediaType;
                window.discoverState.filters.mediaType = mediaType;
                // Update UI - toggle active class on filter buttons
                document.querySelectorAll('[data-filter="type"]').forEach(btn => {
                    btn.classList.toggle('active', btn.dataset.value === mediaType);
                });
                console.log('[Adaptive List] Set media type to:', mediaType);
            }

            // Wait for genres to be loaded before applying filters
            // This ensures the dropdown has items to match against
            await waitForGenresLoaded();

            // Pre-load genre selections into state BEFORE rendering
            // so renderGenreFilters() can mark them as selected
            if (data.list.filters) {
                if (data.list.filters.genres) {
                    window.discoverState.filters.selectedGenres = data.list.filters.genres.split(',').filter(v => v);
                }
                if (data.list.filters.genres_exclude) {
                    window.discoverState.filters.excludedGenres = data.list.filters.genres_exclude.split(',').filter(v => v);
                }
            }

            // Update genres dropdown for the selected media type
            renderGenreFilters();

            // Wait a bit for dropdown to update
            await new Promise(resolve => setTimeout(resolve, 100));

            // Apply the saved filters to the UI (this handles all other filters)
            applyFiltersToUI(data.list.filters);

            // Update the save button text
            const saveBtn = document.getElementById('save-adaptive-list-btn');
            if (saveBtn) {
                saveBtn.innerHTML = `
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" style="width: 1rem; height: 1rem; margin-right: 0.25rem;">
                        <path stroke-linecap="round" stroke-linejoin="round" d="m16.862 4.487 1.687-1.688a1.875 1.875 0 1 1 2.652 2.652L10.582 16.07a4.5 4.5 0 0 1-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 0 1 1.13-1.897l8.932-8.931Zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0 1 15.75 21H5.25A2.25 2.25 0 0 1 3 18.75V8.25A2.25 2.25 0 0 1 5.25 6H10" />
                    </svg>
                    Update Adaptive List
                `;
            }

            // Show notification
            showNotification(`Editing adaptive list: ${data.list.name}`, 'info');

            // Open the filter drawer
            setTimeout(() => {
                openFilters();
            }, 300);
        }
    } catch (error) {
        console.error('[Adaptive List] Error loading list for edit:', error);
        showNotification('Failed to load adaptive list for editing', 'error');
    }
}

/**
 * Wait for genres to be loaded (with timeout)
 */
async function waitForGenresLoaded(maxWait = 3000) {
    const startTime = Date.now();
    while (Date.now() - startTime < maxWait) {
        const state = window.discoverState;
        if (state && state.genres &&
            ((state.genres.movie && state.genres.movie.length > 0) ||
             (state.genres.tv && state.genres.tv.length > 0))) {
            return true;
        }
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    return false;
}

/**
 * Apply saved filters to the UI elements
 */
function applyFiltersToUI(filters) {
    if (!filters) return;

    const state = window.discoverState;
    if (!state || !state.filters) {
        return;
    }

    // Sort - correct element ID is 'sort-by'
    if (filters.sort_by) {
        const sortSelect = document.getElementById('sort-by');
        if (sortSelect) {
            sortSelect.value = filters.sort_by;
        }
        state.filters.sortBy = filters.sort_by;
    }

    // Year range
    if (filters.year_from) {
        const yearFrom = document.getElementById('year-from');
        if (yearFrom) yearFrom.value = filters.year_from;
        state.filters.yearFrom = parseInt(filters.year_from);
    }
    if (filters.year_to) {
        const yearTo = document.getElementById('year-to');
        if (yearTo) yearTo.value = filters.year_to;
        state.filters.yearTo = parseInt(filters.year_to);
    }

    // Released within
    if (filters.released_within) {
        const releasedWithin = document.getElementById('released-within');
        if (releasedWithin) releasedWithin.value = filters.released_within;
        state.filters.releasedWithin = parseInt(filters.released_within);
    }

    // Upcoming days
    if (filters.upcoming_days) {
        const upcomingDays = document.getElementById('upcoming-days');
        if (upcomingDays) upcomingDays.value = filters.upcoming_days;
        state.filters.upcomingDays = parseInt(filters.upcoming_days);
    }

    // TMDB Rating - dispatch input events to update slider UI
    if (filters.tmdb_rating_min !== undefined) {
        const ratingMin = document.getElementById('tmdb-rating-min');
        if (ratingMin) {
            ratingMin.value = filters.tmdb_rating_min;
            ratingMin.dispatchEvent(new Event('input', { bubbles: true }));
        }
        state.filters.tmdbRatingMin = parseFloat(filters.tmdb_rating_min);
    }
    if (filters.tmdb_rating_max !== undefined) {
        const ratingMax = document.getElementById('tmdb-rating-max');
        if (ratingMax) {
            ratingMax.value = filters.tmdb_rating_max;
            ratingMax.dispatchEvent(new Event('input', { bubbles: true }));
        }
        state.filters.tmdbRatingMax = parseFloat(filters.tmdb_rating_max);
    }

    // Vote count - dispatch input event to update slider UI
    if (filters.tmdb_votes_min !== undefined) {
        const votesMin = document.getElementById('tmdb-votes-min');
        if (votesMin) {
            votesMin.value = filters.tmdb_votes_min;
            votesMin.dispatchEvent(new Event('input', { bubbles: true }));
        }
        state.filters.tmdbVotesMin = parseInt(filters.tmdb_votes_min);
    }

    // Runtime
    if (filters.runtime_min) {
        const runtimeMin = document.getElementById('runtime-min');
        if (runtimeMin) runtimeMin.value = filters.runtime_min;
        state.filters.runtimeMin = parseInt(filters.runtime_min);
    }
    if (filters.runtime_max) {
        const runtimeMax = document.getElementById('runtime-max');
        if (runtimeMax) runtimeMax.value = filters.runtime_max;
        state.filters.runtimeMax = parseInt(filters.runtime_max);
    }

    // Genres - load into state and update UI
    if (filters.genres) {
        const genreIds = filters.genres.split(',').filter(v => v);
        state.filters.selectedGenres = genreIds;
        applyChipsFromSavedFilters('genres', genreIds, []);
    }
    if (filters.genres_exclude) {
        const excludedIds = filters.genres_exclude.split(',').filter(v => v);
        state.filters.excludedGenres = excludedIds;
        applyChipsFromSavedFilters('genres', state.filters.selectedGenres, excludedIds);
    }

    // Keywords - load into state and render chips
    // Keywords are dynamic (searched via API), so we need to fetch names
    if (filters.keywords || filters.keywords_exclude) {
        const keywordIds = filters.keywords ? filters.keywords.split(',').filter(v => v) : [];
        const excludedKeywordIds = filters.keywords_exclude ? filters.keywords_exclude.split(',').filter(v => v) : [];
        state.filters.selectedKeywords = keywordIds;
        state.filters.excludedKeywords = excludedKeywordIds;
        // Fetch keyword names and render chips
        loadAndRenderKeywordChips(keywordIds, excludedKeywordIds);
    }

    // Languages
    if (filters.language) {
        const langCodes = filters.language.split(',').filter(v => v);
        state.filters.selectedLanguages = langCodes;
        applyChipsFromSavedFilters('language', langCodes, []);
    }

    // Countries
    if (filters.country) {
        const countryCodes = filters.country.split(',').filter(v => v);
        state.filters.selectedCountries = countryCodes;
        applyChipsFromSavedFilters('country', countryCodes, []);
    }

    // Watch providers
    if (filters.watch_provider) {
        const providerIds = filters.watch_provider.split(',').filter(v => v);
        state.filters.selectedProviders = providerIds;
        applyChipsFromSavedFilters('provider', providerIds, []);
    }
    if (filters.watch_region) {
        state.filters.watchRegion = filters.watch_region;
    }

    // Networks
    if (filters.network) {
        const networkIds = filters.network.split(',').filter(v => v);
        state.filters.selectedNetworks = networkIds;
        applyChipsFromSavedFilters('network', networkIds, []);
    }

    // Companies
    if (filters.company) {
        const companyIds = filters.company.split(',').filter(v => v);
        state.filters.selectedCompanies = companyIds;
        applyChipsFromSavedFilters('company', companyIds, []);
    }

    // Lists filter (sidebar lists) - load array
    if (filters.lists) {
        // Parse "source:id" pairs
        const listPairs = filters.lists.split(',').filter(v => v);
        loadSavedLists(listPairs);
    }

    // Include video filter
    if (filters.include_video) {
        const includeVideoCheckbox = document.getElementById('include-video');
        if (includeVideoCheckbox) {
            includeVideoCheckbox.checked = true;
        }
        state.filters.includeVideo = true;
    }

    // Title filter (client-side regex/text filter)
    if (filters.title_filter) {
        const titleFilterInput = document.getElementById('title-filter');
        if (titleFilterInput) {
            titleFilterInput.value = filters.title_filter;
        }
        state.filters.titleFilter = filters.title_filter;
    }

    // Apply filters and update UI
    setTimeout(() => {
        applyFilters();
        updateActiveFilters();
    }, 100);
}

/**
 * Apply chips display for saved filter values
 * Updates dropdown item states and renders chips in the container
 */
function applyChipsFromSavedFilters(name, includeArray, excludeArray) {
    const container = document.getElementById(`${name}-container`);
    if (!container) {
        console.warn(`[Adaptive List] Container not found: ${name}-container`);
        return;
    }

    const chipsWrapper = container.querySelector(`#${name}-chips`);
    const dropdown = container.querySelector(`#${name}-dropdown`);
    const hiddenInput = document.getElementById(`selected-${name}`) || document.getElementById(`selected-${name}s`);

    if (!chipsWrapper || !dropdown) {
        console.warn(`[Adaptive List] Missing elements for ${name}`);
        return;
    }

    // Update dropdown item states
    includeArray.forEach(value => {
        const item = dropdown.querySelector(`[data-value="${value}"]`);
        if (item) {
            item.classList.add('included');
            item.classList.remove('excluded');
        }
    });

    excludeArray.forEach(value => {
        const item = dropdown.querySelector(`[data-value="${value}"]`);
        if (item) {
            item.classList.add('excluded');
            item.classList.remove('included');
        }
    });

    // Render chips
    renderChips(name, chipsWrapper, includeArray, excludeArray, dropdown, hiddenInput, () => updateActiveFilters());

    // Update hidden input
    if (hiddenInput) hiddenInput.value = includeArray.join(',');
}

/**
 * Load keyword names from API and render chips
 * Keywords are dynamic (searched via API) so we need to fetch their names
 */
async function loadAndRenderKeywordChips(includedIds, excludedIds) {
    const state = window.discoverState;
    const chipsWrapper = document.getElementById('keyword-chips');
    if (!chipsWrapper) return;

    // Initialize keyword cache if not exists
    if (!state.filters.keywordCache) {
        state.filters.keywordCache = {};
    }

    // Fetch names for all keyword IDs we don't have cached
    const allIds = [...includedIds, ...excludedIds];
    const uncachedIds = allIds.filter(id => !state.filters.keywordCache[id]);

    if (uncachedIds.length > 0) {
        // Fetch keyword details from TMDB API
        for (const keywordId of uncachedIds) {
            try {
                const response = await fetch(`/discover/api/keyword/${keywordId}`);
                if (response.ok) {
                    const data = await response.json();
                    if (data.name) {
                        state.filters.keywordCache[keywordId] = data.name;
                    }
                }
            } catch (error) {
                console.warn(`[Adaptive List] Failed to fetch keyword ${keywordId}:`, error);
                // Use ID as fallback name
                state.filters.keywordCache[keywordId] = `Keyword ${keywordId}`;
            }
        }
    }

    // Now render the chips using the existing function
    renderKeywordChips(chipsWrapper);

    // Update active filters display with newly fetched keyword names
    updateActiveFilters();
}

/**
 * Check if any filters are currently active (non-default values)
 * @returns {boolean} True if any filters are set
 */
function hasActiveFilters() {
    const state = window.discoverState;
    if (!state || !state.filters) return false;
    const f = state.filters;

    return (
        f.mediaType !== 'all' ||
        f.yearFrom > 1900 ||
        f.yearTo < new Date().getFullYear() ||
        f.releasedWithin ||
        f.upcomingDays ||
        f.tmdbRatingMin > 0 ||
        f.tmdbRatingMax < 10 ||
        f.tmdbVotesMin > 0 ||
        f.selectedGenres.length > 0 ||
        f.excludedGenres.length > 0 ||
        f.selectedKeywords.length > 0 ||
        f.excludedKeywords.length > 0 ||
        f.selectedLanguages.length > 0 ||
        f.excludedLanguages.length > 0 ||
        f.selectedCountries.length > 0 ||
        f.excludedCountries.length > 0 ||
        f.selectedProviders.length > 0 ||
        f.excludedProviders.length > 0 ||
        f.selectedNetworks.length > 0 ||
        f.excludedNetworks.length > 0 ||
        f.selectedCompanies.length > 0 ||
        f.excludedCompanies.length > 0 ||
        f.runtimeMin > 0 ||
        f.runtimeMax < 300
    );
}

/**
 * Open the adaptive list save modal
 */
function openAdaptiveListModal() {
    const modal = document.getElementById('adaptive-list-modal');
    const nameInput = document.getElementById('adaptive-list-name');
    const preview = document.getElementById('adaptive-list-filter-preview');
    const modalTitle = document.getElementById('adaptive-list-modal-title');
    const saveText = document.getElementById('adaptive-list-save-text');

    if (!modal) return;

    // Check if we have any active filters
    if (!hasActiveFilters()) {
        showNotification('Please set at least one filter before saving as an Adaptive List', 'warning');
        return;
    }

    // Update modal for edit mode vs create mode
    if (window.adaptiveListEditMode.isEditing) {
        modalTitle.textContent = 'Update Adaptive List';
        saveText.textContent = 'Update Adaptive List';
        nameInput.value = window.adaptiveListEditMode.originalName || '';
    } else {
        modalTitle.textContent = 'Save as Adaptive List';
        saveText.textContent = 'Save Adaptive List';
        nameInput.value = '';
    }

    // Generate filter preview
    if (preview) {
        preview.innerHTML = generateFilterPreview();
    }

    // Show modal
    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';

    // Focus name input
    if (nameInput) {
        setTimeout(() => nameInput.focus(), 100);
    }
}

/**
 * Close the adaptive list save modal
 */
function closeAdaptiveListModal() {
    const modal = document.getElementById('adaptive-list-modal');
    if (modal) {
        modal.style.display = 'none';
        document.body.style.overflow = '';
    }
}

/**
 * Generate a preview of the active filters
 */
function generateFilterPreview() {
    const chips = [];
    const state = window.discoverState;
    if (!state || !state.filters) return '<span class="preview-chip warning">No filters set</span>';
    const filters = state.filters;

    // Media type - get from state, not a select element
    const mediaType = filters.mediaType || 'movie';
    chips.push(`<span class="preview-chip type">${mediaType === 'movie' ? 'Movies' : mediaType === 'tv' ? 'TV Shows' : 'All'}</span>`);

    // Released within
    if (filters.releasedWithin) {
        chips.push(`<span class="preview-chip time">Released within ${filters.releasedWithin} days</span>`);
    }

    // Upcoming days
    if (filters.upcomingDays) {
        chips.push(`<span class="preview-chip time">Upcoming ${filters.upcomingDays} days</span>`);
    }

    // Year range
    if (filters.yearFrom || filters.yearTo) {
        const yearRange = filters.yearFrom && filters.yearTo
            ? `${filters.yearFrom}-${filters.yearTo}`
            : filters.yearFrom
                ? `${filters.yearFrom}+`
                : `Up to ${filters.yearTo}`;
        chips.push(`<span class="preview-chip">Years: ${yearRange}</span>`);
    }

    // Rating
    if (filters.tmdbRatingMin > 0 || filters.tmdbRatingMax < 10) {
        chips.push(`<span class="preview-chip">Rating: ${filters.tmdbRatingMin}-${filters.tmdbRatingMax}</span>`);
    }

    // Votes
    if (filters.tmdbVotesMin > 0) {
        chips.push(`<span class="preview-chip">Min ${filters.tmdbVotesMin} votes</span>`);
    }

    // Genres (included)
    if (filters.selectedGenres && filters.selectedGenres.length > 0) {
        chips.push(`<span class="preview-chip">${filters.selectedGenres.length} genre(s)</span>`);
    }

    // Genres (excluded)
    if (filters.excludedGenres && filters.excludedGenres.length > 0) {
        chips.push(`<span class="preview-chip excluded">${filters.excludedGenres.length} excluded genre(s)</span>`);
    }

    // Keywords (included)
    if (filters.selectedKeywords && filters.selectedKeywords.length > 0) {
        chips.push(`<span class="preview-chip">${filters.selectedKeywords.length} keyword(s)</span>`);
    }

    // Keywords (excluded)
    if (filters.excludedKeywords && filters.excludedKeywords.length > 0) {
        chips.push(`<span class="preview-chip excluded">${filters.excludedKeywords.length} excluded keyword(s)</span>`);
    }

    // Languages (included)
    if (filters.selectedLanguages && filters.selectedLanguages.length > 0) {
        chips.push(`<span class="preview-chip">${filters.selectedLanguages.length} language(s)</span>`);
    }

    // Languages (excluded)
    if (filters.excludedLanguages && filters.excludedLanguages.length > 0) {
        chips.push(`<span class="preview-chip excluded">${filters.excludedLanguages.length} excluded language(s)</span>`);
    }

    // Countries (included)
    if (filters.selectedCountries && filters.selectedCountries.length > 0) {
        chips.push(`<span class="preview-chip">${filters.selectedCountries.length} country(ies)</span>`);
    }

    // Countries (excluded)
    if (filters.excludedCountries && filters.excludedCountries.length > 0) {
        chips.push(`<span class="preview-chip excluded">${filters.excludedCountries.length} excluded country(ies)</span>`);
    }

    // Watch Providers (included)
    if (filters.selectedProviders && filters.selectedProviders.length > 0) {
        chips.push(`<span class="preview-chip">${filters.selectedProviders.length} provider(s)</span>`);
    }

    // Watch Providers (excluded)
    if (filters.excludedProviders && filters.excludedProviders.length > 0) {
        chips.push(`<span class="preview-chip excluded">${filters.excludedProviders.length} excluded provider(s)</span>`);
    }

    // Networks (included) - TV only
    if (filters.selectedNetworks && filters.selectedNetworks.length > 0) {
        chips.push(`<span class="preview-chip">${filters.selectedNetworks.length} network(s)</span>`);
    }

    // Networks (excluded) - TV only
    if (filters.excludedNetworks && filters.excludedNetworks.length > 0) {
        chips.push(`<span class="preview-chip excluded">${filters.excludedNetworks.length} excluded network(s)</span>`);
    }

    // Production Companies (included)
    if (filters.selectedCompanies && filters.selectedCompanies.length > 0) {
        chips.push(`<span class="preview-chip">${filters.selectedCompanies.length} company(ies)</span>`);
    }

    // Production Companies (excluded)
    if (filters.excludedCompanies && filters.excludedCompanies.length > 0) {
        chips.push(`<span class="preview-chip excluded">${filters.excludedCompanies.length} excluded company(ies)</span>`);
    }

    // Release Type
    if (filters.releaseType) {
        const types = filters.releaseType.split(',').length;
        chips.push(`<span class="preview-chip">${types} release type(s)</span>`);
    }

    // Sort
    if (filters.sortBy && filters.sortBy !== 'popularity.desc') {
        chips.push(`<span class="preview-chip">Sort: ${filters.sortBy}</span>`);
    }

    // Runtime
    if (filters.runtimeMin > 0 || filters.runtimeMax < 300) {
        const runtime = filters.runtimeMin && filters.runtimeMax
            ? `${filters.runtimeMin}-${filters.runtimeMax} min`
            : filters.runtimeMin
                ? `>${filters.runtimeMin} min`
                : `<${filters.runtimeMax} min`;
        chips.push(`<span class="preview-chip">${runtime}</span>`);
    }

    // Watch Region
    if (filters.watchRegion && filters.watchRegion !== 'US') {
        chips.push(`<span class="preview-chip">Region: ${filters.watchRegion}</span>`);
    }

    // Include Video
    if (filters.includeVideo) {
        chips.push(`<span class="preview-chip">Include video</span>`);
    }

    // Title Filter
    if (filters.titleFilter) {
        chips.push(`<span class="preview-chip">Title filter</span>`);
    }

    // Lists filter - show count of selected lists
    if (window.sidebarListsState && window.sidebarListsState.selectedLists && window.sidebarListsState.selectedLists.length > 0) {
        const count = window.sidebarListsState.selectedLists.length;
        const label = count === 1 ? window.sidebarListsState.selectedLists[0].listName : `${count} lists`;
        chips.push(`<span class="preview-chip">Lists: ${label}</span>`);
    }

    return chips.length > 1 ? chips.join('') : '<span class="preview-chip warning">No filters set</span>';
}

/**
 * Save the adaptive list
 */
async function saveAdaptiveList() {
    const nameInput = document.getElementById('adaptive-list-name');
    const saveBtn = document.getElementById('adaptive-list-save-btn');
    const saveText = document.getElementById('adaptive-list-save-text');
    const saveLoading = document.getElementById('adaptive-list-save-loading');

    const name = nameInput?.value?.trim();
    if (!name) {
        showNotification('Please enter a name for the adaptive list', 'warning');
        nameInput?.focus();
        return;
    }

    // Get media type from state
    const mediaType = window.discoverState?.filters?.mediaType || 'movie';

    // Build filters object from current state
    const filters = buildFiltersObject();

    // Show loading state
    if (saveBtn) saveBtn.disabled = true;
    if (saveText) saveText.style.display = 'none';
    if (saveLoading) saveLoading.style.display = 'inline';

    try {
        let response;
        let method;
        let url;

        if (window.adaptiveListEditMode && window.adaptiveListEditMode.isEditing) {
            // Update existing list - use source_id instead of index
            method = 'PUT';
            url = `/discover/api/adaptive-lists/${encodeURIComponent(window.adaptiveListEditMode.sourceId)}`;
        } else {
            // Create new list - each list becomes its own content source
            method = 'POST';
            url = '/discover/api/adaptive-lists';
        }

        response = await fetch(url, {
            method: method,
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                name: name,
                media_type: mediaType,
                filters: filters
            })
        });

        const data = await response.json();

        if (data.success) {
            closeAdaptiveListModal();

            if (window.adaptiveListEditMode && window.adaptiveListEditMode.isEditing) {
                showNotification(`Adaptive list "${name}" updated successfully!`, 'success');
                // Reset edit mode
                window.adaptiveListEditMode = {
                    isEditing: false,
                    sourceId: null
                };
                // Update button text
                const saveAdaptiveBtn = document.getElementById('save-adaptive-list-btn');
                if (saveAdaptiveBtn) {
                    saveAdaptiveBtn.innerHTML = `
                        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor" style="width: 1rem; height: 1rem; margin-right: 0.25rem;">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
                        </svg>
                        Save as Adaptive List
                    `;
                }
                // Clear URL params
                window.history.replaceState({}, document.title, window.location.pathname);
            } else {
                showNotification(`Adaptive list "${name}" created! It will appear in your Content Sources.`, 'success');
            }
        } else {
            throw new Error(data.error || 'Failed to save adaptive list');
        }
    } catch (error) {
        console.error('[Adaptive List] Save error:', error);
        showNotification(error.message || 'Failed to save adaptive list', 'error');
    } finally {
        // Reset button state
        if (saveBtn) saveBtn.disabled = false;
        if (saveText) saveText.style.display = 'inline';
        if (saveLoading) saveLoading.style.display = 'none';
    }
}

/**
 * Build filters object from current state for API
 */
function buildFiltersObject() {
    const filters = {};
    const state = window.discoverState;

    if (!state || !state.filters) {
        console.warn('[Adaptive List] State not available for building filters');
        return filters;
    }

    // Sort
    if (state.filters.sortBy) {
        filters.sort_by = state.filters.sortBy;
    }

    // Year range
    if (state.filters.yearFrom) {
        filters.year_from = state.filters.yearFrom;
    }
    if (state.filters.yearTo) {
        filters.year_to = state.filters.yearTo;
    }

    // Released within (time-sensitive)
    if (state.filters.releasedWithin) {
        filters.released_within = state.filters.releasedWithin;
    }

    // Upcoming days (time-sensitive)
    if (state.filters.upcomingDays) {
        filters.upcoming_days = state.filters.upcomingDays;
    }

    // TMDB Rating
    if (state.filters.tmdbRatingMin > 0) {
        filters.tmdb_rating_min = state.filters.tmdbRatingMin;
    }
    if (state.filters.tmdbRatingMax < 10) {
        filters.tmdb_rating_max = state.filters.tmdbRatingMax;
    }

    // Vote count
    if (state.filters.tmdbVotesMin > 0) {
        filters.tmdb_votes_min = state.filters.tmdbVotesMin;
    }

    // Genres
    if (state.filters.selectedGenres && state.filters.selectedGenres.length > 0) {
        filters.genres = state.filters.selectedGenres.join(',');
    }
    if (state.filters.excludedGenres && state.filters.excludedGenres.length > 0) {
        filters.genres_exclude = state.filters.excludedGenres.join(',');
    }

    // Keywords
    if (state.filters.selectedKeywords && state.filters.selectedKeywords.length > 0) {
        filters.keywords = state.filters.selectedKeywords.join(',');
    }
    if (state.filters.excludedKeywords && state.filters.excludedKeywords.length > 0) {
        filters.keywords_exclude = state.filters.excludedKeywords.join(',');
    }

    // Language
    if (state.filters.selectedLanguages && state.filters.selectedLanguages.length > 0) {
        filters.language = state.filters.selectedLanguages.join(',');
    }

    // Country
    if (state.filters.selectedCountries && state.filters.selectedCountries.length > 0) {
        filters.country = state.filters.selectedCountries.join(',');
    }

    // Watch provider
    if (state.filters.selectedProviders && state.filters.selectedProviders.length > 0) {
        filters.watch_provider = state.filters.selectedProviders.join(',');
    }
    if (state.filters.watchRegion) {
        filters.watch_region = state.filters.watchRegion;
    }

    // Network (TV only)
    if (state.filters.selectedNetworks && state.filters.selectedNetworks.length > 0) {
        filters.network = state.filters.selectedNetworks.join(',');
    }

    // Runtime
    if (state.filters.runtimeMin) {
        filters.runtime_min = state.filters.runtimeMin;
    }
    if (state.filters.runtimeMax) {
        filters.runtime_max = state.filters.runtimeMax;
    }

    // Production company
    if (state.filters.selectedCompanies && state.filters.selectedCompanies.length > 0) {
        filters.production_company = state.filters.selectedCompanies.join(',');
    }

    // Lists filter (sidebar lists) - save as array
    if (window.sidebarListsState && window.sidebarListsState.selectedLists && window.sidebarListsState.selectedLists.length > 0) {
        // Save as comma-separated "source:id" pairs
        filters.lists = window.sidebarListsState.selectedLists.map(l => `${l.source}:${l.listId}`).join(',');
    }

    // Include video filter
    if (state.filters.includeVideo) {
        filters.include_video = true;
    }

    // Title filter (client-side regex/text filter)
    if (state.filters.titleFilter) {
        filters.title_filter = state.filters.titleFilter;
    }

    return filters;
}

// Initialize adaptive list functionality when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    // Initialize after a short delay to ensure other components are ready
    setTimeout(initAdaptiveList, 100);
});


// =============================================================================
// Filter Presets Functionality
// =============================================================================

/**
 * Initialize filter preset functionality
 */
function initFilterPresets() {
    const savePresetBtn = document.getElementById('save-preset-btn');
    const presetModal = document.getElementById('preset-modal');
    const presetCloseBtn = document.getElementById('preset-modal-close');
    const presetCancelBtn = document.getElementById('preset-cancel-btn');
    const presetSaveBtn = document.getElementById('preset-save-btn');
    const presetSelect = document.getElementById('preset-select');
    const deletePresetBtn = document.getElementById('delete-preset-btn');

    // Open preset save modal
    if (savePresetBtn) {
        savePresetBtn.addEventListener('click', openPresetModal);
    }

    // Close modal buttons
    if (presetCloseBtn) {
        presetCloseBtn.addEventListener('click', closePresetModal);
    }

    if (presetCancelBtn) {
        presetCancelBtn.addEventListener('click', closePresetModal);
    }

    // Save preset button
    if (presetSaveBtn) {
        presetSaveBtn.addEventListener('click', saveFilterPreset);
    }

    // Load preset when selection changes
    if (presetSelect) {
        presetSelect.addEventListener('change', function() {
            const presetId = this.value;
            if (presetId) {
                loadFilterPreset(presetId);
            }
            // Enable/disable delete button
            if (deletePresetBtn) {
                deletePresetBtn.disabled = !presetId;
            }
        });
    }

    // Delete preset button
    if (deletePresetBtn) {
        deletePresetBtn.addEventListener('click', deleteSelectedPreset);
    }

    // Close modal when clicking overlay
    if (presetModal) {
        presetModal.addEventListener('click', function(e) {
            if (e.target === presetModal) {
                closePresetModal();
            }
        });
    }

    // Load presets into dropdown
    loadPresetsIntoDropdown();
}

/**
 * Load all presets into the dropdown
 */
async function loadPresetsIntoDropdown() {
    const presetSelect = document.getElementById('preset-select');
    if (!presetSelect) return;

    try {
        const response = await fetch('/discover/api/presets');
        if (!response.ok) {
            throw new Error('Failed to load presets');
        }

        const data = await response.json();
        if (data.success && data.presets) {
            // Clear existing options (except the default)
            presetSelect.innerHTML = '<option value="">-- Select a preset --</option>';

            // Add preset options sorted by name
            const presetEntries = Object.entries(data.presets);
            presetEntries.sort((a, b) => a[1].name.localeCompare(b[1].name));

            presetEntries.forEach(([presetId, preset]) => {
                const option = document.createElement('option');
                option.value = presetId;
                option.textContent = preset.name;
                presetSelect.appendChild(option);
            });

            console.log(`[Presets] Loaded ${presetEntries.length} presets`);
        }
    } catch (error) {
        console.error('[Presets] Error loading presets:', error);
    }
}

/**
 * Open the preset save modal
 */
function openPresetModal() {
    const modal = document.getElementById('preset-modal');
    const nameInput = document.getElementById('preset-name');
    const filterPreview = document.getElementById('preset-filter-preview');

    if (modal) {
        modal.style.display = 'flex';
        document.body.style.overflow = 'hidden';

        // Clear name input
        if (nameInput) {
            nameInput.value = '';
            nameInput.focus();
        }

        // Generate filter preview
        if (filterPreview) {
            filterPreview.innerHTML = generateFilterPreview();
        }
    }
}

/**
 * Close the preset save modal
 */
function closePresetModal() {
    const modal = document.getElementById('preset-modal');
    if (modal) {
        modal.style.display = 'none';
        document.body.style.overflow = '';
    }
}

/**
 * Save the current filters as a preset
 */
async function saveFilterPreset() {
    const nameInput = document.getElementById('preset-name');
    const saveBtn = document.getElementById('preset-save-btn');
    const saveText = document.getElementById('preset-save-text');
    const saveLoading = document.getElementById('preset-save-loading');

    const name = nameInput?.value?.trim();
    if (!name) {
        showNotification('Please enter a name for the preset', 'warning');
        nameInput?.focus();
        return;
    }

    // Build comprehensive filters object including all current settings
    const filters = buildPresetFiltersObject();

    // Check if there are any actual filters
    if (Object.keys(filters).length === 0) {
        showNotification('No filters to save. Please set some filters first.', 'warning');
        return;
    }

    // Show loading state
    if (saveBtn) saveBtn.disabled = true;
    if (saveText) saveText.style.display = 'none';
    if (saveLoading) saveLoading.style.display = 'inline';

    try {
        const response = await fetch('/discover/api/presets', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                name: name,
                filters: filters
            })
        });

        const data = await response.json();

        if (data.success) {
            closePresetModal();
            showNotification(`Preset "${name}" saved successfully!`, 'success');

            // Refresh the presets dropdown
            await loadPresetsIntoDropdown();
        } else {
            throw new Error(data.error || 'Failed to save preset');
        }
    } catch (error) {
        console.error('[Presets] Save error:', error);
        showNotification(error.message || 'Failed to save preset', 'error');
    } finally {
        // Reset button state
        if (saveBtn) saveBtn.disabled = false;
        if (saveText) saveText.style.display = 'inline';
        if (saveLoading) saveLoading.style.display = 'none';
    }
}

/**
 * Build a comprehensive filters object for preset saving
 * This saves ALL filter states, not just active ones
 */
function buildPresetFiltersObject() {
    const filters = {};
    const state = window.discoverState;

    if (!state || !state.filters) {
        console.warn('[Presets] State not available for building filters');
        return filters;
    }

    // Media type
    if (state.filters.mediaType && state.filters.mediaType !== 'all') {
        filters.media_type = state.filters.mediaType;
    }

    // Sort options
    if (state.filters.sortBy) {
        filters.sort_by = state.filters.sortBy;
    }
    if (state.filters.sortOrder) {
        filters.sort_order = state.filters.sortOrder;
    }

    // Year range
    if (state.filters.yearFrom) {
        filters.year_from = state.filters.yearFrom;
    }
    if (state.filters.yearTo) {
        filters.year_to = state.filters.yearTo;
    }

    // Released within (time-sensitive)
    if (state.filters.releasedWithin) {
        filters.released_within = state.filters.releasedWithin;
    }

    // Upcoming days (time-sensitive)
    if (state.filters.upcomingDays) {
        filters.upcoming_days = state.filters.upcomingDays;
    }

    // TMDB Rating
    if (state.filters.tmdbRatingMin > 0) {
        filters.tmdb_rating_min = state.filters.tmdbRatingMin;
    }
    if (state.filters.tmdbRatingMax < 10) {
        filters.tmdb_rating_max = state.filters.tmdbRatingMax;
    }

    // IMDB Rating (if used)
    if (state.filters.imdbRatingMin > 0) {
        filters.imdb_rating_min = state.filters.imdbRatingMin;
    }
    if (state.filters.imdbRatingMax < 10) {
        filters.imdb_rating_max = state.filters.imdbRatingMax;
    }

    // Vote counts
    if (state.filters.tmdbVotesMin > 0) {
        filters.tmdb_votes_min = state.filters.tmdbVotesMin;
    }
    if (state.filters.imdbVotesMin > 0) {
        filters.imdb_votes_min = state.filters.imdbVotesMin;
    }

    // Genres (include and exclude)
    if (state.filters.selectedGenres && state.filters.selectedGenres.length > 0) {
        filters.genres = state.filters.selectedGenres.join(',');
    }
    if (state.filters.excludedGenres && state.filters.excludedGenres.length > 0) {
        filters.genres_exclude = state.filters.excludedGenres.join(',');
    }

    // Keywords (include and exclude)
    if (state.filters.selectedKeywords && state.filters.selectedKeywords.length > 0) {
        filters.keywords = state.filters.selectedKeywords.join(',');
        // Also save keyword names for display
        if (state.filters.keywordCache) {
            const keywordNames = {};
            state.filters.selectedKeywords.forEach(id => {
                if (state.filters.keywordCache[id]) {
                    keywordNames[id] = state.filters.keywordCache[id];
                }
            });
            if (Object.keys(keywordNames).length > 0) {
                filters.keyword_names = keywordNames;
            }
        }
    }
    if (state.filters.excludedKeywords && state.filters.excludedKeywords.length > 0) {
        filters.keywords_exclude = state.filters.excludedKeywords.join(',');
    }

    // Languages (include and exclude)
    if (state.filters.selectedLanguages && state.filters.selectedLanguages.length > 0) {
        filters.language = state.filters.selectedLanguages.join(',');
    }
    if (state.filters.excludedLanguages && state.filters.excludedLanguages.length > 0) {
        filters.language_exclude = state.filters.excludedLanguages.join(',');
    }

    // Countries (include and exclude)
    if (state.filters.selectedCountries && state.filters.selectedCountries.length > 0) {
        filters.country = state.filters.selectedCountries.join(',');
    }
    if (state.filters.excludedCountries && state.filters.excludedCountries.length > 0) {
        filters.country_exclude = state.filters.excludedCountries.join(',');
    }

    // Watch providers (include and exclude)
    if (state.filters.selectedProviders && state.filters.selectedProviders.length > 0) {
        filters.watch_provider = state.filters.selectedProviders.join(',');
    }
    if (state.filters.excludedProviders && state.filters.excludedProviders.length > 0) {
        filters.watch_provider_exclude = state.filters.excludedProviders.join(',');
    }
    if (state.filters.watchRegion) {
        filters.watch_region = state.filters.watchRegion;
    }

    // Networks (TV - include and exclude)
    if (state.filters.selectedNetworks && state.filters.selectedNetworks.length > 0) {
        filters.network = state.filters.selectedNetworks.join(',');
    }
    if (state.filters.excludedNetworks && state.filters.excludedNetworks.length > 0) {
        filters.network_exclude = state.filters.excludedNetworks.join(',');
    }

    // Runtime
    if (state.filters.runtimeMin) {
        filters.runtime_min = state.filters.runtimeMin;
    }
    if (state.filters.runtimeMax && state.filters.runtimeMax < 300) {
        filters.runtime_max = state.filters.runtimeMax;
    }

    // Production companies (include and exclude)
    if (state.filters.selectedCompanies && state.filters.selectedCompanies.length > 0) {
        filters.production_company = state.filters.selectedCompanies.join(',');
        // Also save company names for display
        if (state.filters.companyCache) {
            const companyNames = {};
            state.filters.selectedCompanies.forEach(id => {
                if (state.filters.companyCache[id]) {
                    companyNames[id] = state.filters.companyCache[id];
                }
            });
            if (Object.keys(companyNames).length > 0) {
                filters.company_names = companyNames;
            }
        }
    }
    if (state.filters.excludedCompanies && state.filters.excludedCompanies.length > 0) {
        filters.production_company_exclude = state.filters.excludedCompanies.join(',');
    }

    // Lists filter (sidebar lists) - save as array
    if (window.sidebarListsState && window.sidebarListsState.selectedLists && window.sidebarListsState.selectedLists.length > 0) {
        // Save as comma-separated "source:id" pairs
        filters.lists = window.sidebarListsState.selectedLists.map(l => `${l.source}:${l.listId}`).join(',');
    }

    return filters;
}

/**
 * Load a filter preset by ID
 */
async function loadFilterPreset(presetId) {
    try {
        const response = await fetch(`/discover/api/presets/${presetId}`);
        if (!response.ok) {
            throw new Error('Failed to load preset');
        }

        const data = await response.json();
        if (data.success && data.preset) {
            // First clear all filters
            clearAllFiltersQuietly();

            // Wait for genres to be loaded
            await waitForGenresLoaded();

            // Apply media type first if specified
            if (data.preset.filters.media_type) {
                const mediaType = data.preset.filters.media_type;
                window.discoverState.mediaType = mediaType;
                window.discoverState.filters.mediaType = mediaType;
                document.querySelectorAll('[data-filter="type"]').forEach(btn => {
                    btn.classList.toggle('active', btn.dataset.value === mediaType);
                });
                // Re-render genre filters for the media type
                renderGenreFilters();
                await new Promise(resolve => setTimeout(resolve, 100));
            }

            // Pre-load genre selections into state BEFORE rendering
            if (data.preset.filters.genres) {
                window.discoverState.filters.selectedGenres = data.preset.filters.genres.split(',').filter(v => v);
            }
            if (data.preset.filters.genres_exclude) {
                window.discoverState.filters.excludedGenres = data.preset.filters.genres_exclude.split(',').filter(v => v);
            }

            // Re-render genre filters with selections
            renderGenreFilters();
            await new Promise(resolve => setTimeout(resolve, 100));

            // Apply the rest of the filters to UI
            applyPresetFiltersToUI(data.preset.filters);

            // Update active filters display
            updateActiveFilters();

            showNotification(`Preset "${data.preset.name}" loaded!`, 'success');

            // Auto-apply filters to refresh results (keep sidebar open)
            console.log('[Presets] Applying filters with sidebar open - v3');
            applyAdvancedFilters(false);
        }
    } catch (error) {
        console.error('[Presets] Error loading preset:', error);
        showNotification('Failed to load preset', 'error');
    }
}

/**
 * Apply preset filters to the UI
 * Similar to applyFiltersToUI but handles additional preset-specific fields
 */
function applyPresetFiltersToUI(filters) {
    if (!filters) return;

    const state = window.discoverState;
    if (!state || !state.filters) {
        console.warn('[Presets] State not available for applying filters');
        return;
    }

    console.log('[Presets] Applying filters to UI:', filters);

    // Sort options
    if (filters.sort_by) {
        const sortSelect = document.getElementById('sort-by');
        if (sortSelect) {
            sortSelect.value = filters.sort_by;
        }
        state.filters.sortBy = filters.sort_by;
    }

    if (filters.sort_order) {
        state.filters.sortOrder = filters.sort_order;
        const sortOrderToggle = document.getElementById('sort-order-toggle');
        if (sortOrderToggle) {
            sortOrderToggle.dataset.order = filters.sort_order;
            const descIcon = sortOrderToggle.querySelector('.sort-icon-desc');
            const ascIcon = sortOrderToggle.querySelector('.sort-icon-asc');
            if (descIcon && ascIcon) {
                descIcon.style.display = filters.sort_order === 'desc' ? 'block' : 'none';
                ascIcon.style.display = filters.sort_order === 'asc' ? 'block' : 'none';
            }
        }
    }

    // Year range
    if (filters.year_from) {
        const yearFrom = document.getElementById('year-from');
        if (yearFrom) yearFrom.value = filters.year_from;
        state.filters.yearFrom = parseInt(filters.year_from);
    }
    if (filters.year_to) {
        const yearTo = document.getElementById('year-to');
        if (yearTo) yearTo.value = filters.year_to;
        state.filters.yearTo = parseInt(filters.year_to);
    }

    // Released within
    if (filters.released_within) {
        const releasedWithin = document.getElementById('released-within');
        if (releasedWithin) releasedWithin.value = filters.released_within;
        state.filters.releasedWithin = parseInt(filters.released_within);
    }

    // Upcoming days
    if (filters.upcoming_days) {
        const upcomingDays = document.getElementById('upcoming-days');
        if (upcomingDays) upcomingDays.value = filters.upcoming_days;
        state.filters.upcomingDays = parseInt(filters.upcoming_days);
    }

    // TMDB Rating
    if (filters.tmdb_rating_min !== undefined) {
        const ratingMin = document.getElementById('tmdb-rating-min');
        if (ratingMin) {
            ratingMin.value = filters.tmdb_rating_min;
            ratingMin.dispatchEvent(new Event('input', { bubbles: true }));
        }
        state.filters.tmdbRatingMin = parseFloat(filters.tmdb_rating_min);
    }
    if (filters.tmdb_rating_max !== undefined) {
        const ratingMax = document.getElementById('tmdb-rating-max');
        if (ratingMax) {
            ratingMax.value = filters.tmdb_rating_max;
            ratingMax.dispatchEvent(new Event('input', { bubbles: true }));
        }
        state.filters.tmdbRatingMax = parseFloat(filters.tmdb_rating_max);
    }

    // IMDB Rating
    if (filters.imdb_rating_min !== undefined) {
        const ratingMin = document.getElementById('imdb-rating-min');
        if (ratingMin) {
            ratingMin.value = filters.imdb_rating_min;
            ratingMin.dispatchEvent(new Event('input', { bubbles: true }));
        }
        state.filters.imdbRatingMin = parseFloat(filters.imdb_rating_min);
    }
    if (filters.imdb_rating_max !== undefined) {
        const ratingMax = document.getElementById('imdb-rating-max');
        if (ratingMax) {
            ratingMax.value = filters.imdb_rating_max;
            ratingMax.dispatchEvent(new Event('input', { bubbles: true }));
        }
        state.filters.imdbRatingMax = parseFloat(filters.imdb_rating_max);
    }

    // Vote counts
    if (filters.tmdb_votes_min !== undefined) {
        const votesMin = document.getElementById('tmdb-votes-min');
        if (votesMin) {
            votesMin.value = filters.tmdb_votes_min;
            votesMin.dispatchEvent(new Event('input', { bubbles: true }));
        }
        state.filters.tmdbVotesMin = parseInt(filters.tmdb_votes_min);
    }
    if (filters.imdb_votes_min !== undefined) {
        const votesMin = document.getElementById('imdb-votes-min');
        if (votesMin) {
            votesMin.value = filters.imdb_votes_min;
            votesMin.dispatchEvent(new Event('input', { bubbles: true }));
        }
        state.filters.imdbVotesMin = parseInt(filters.imdb_votes_min);
    }

    // Runtime
    if (filters.runtime_min) {
        const runtimeMin = document.getElementById('runtime-min');
        if (runtimeMin) runtimeMin.value = filters.runtime_min;
        state.filters.runtimeMin = parseInt(filters.runtime_min);
    }
    if (filters.runtime_max) {
        const runtimeMax = document.getElementById('runtime-max');
        if (runtimeMax) runtimeMax.value = filters.runtime_max;
        state.filters.runtimeMax = parseInt(filters.runtime_max);
    }

    // Genres are already handled before this function is called

    // Keywords
    if (filters.keywords) {
        const keywordIds = filters.keywords.split(',').filter(v => v);
        state.filters.selectedKeywords = keywordIds;

        // Restore keyword names from saved data
        if (filters.keyword_names) {
            state.filters.keywordCache = { ...state.filters.keywordCache, ...filters.keyword_names };
        }
    }
    if (filters.keywords_exclude) {
        state.filters.excludedKeywords = filters.keywords_exclude.split(',').filter(v => v);
    }
    // Re-render keyword chips - use loadAndRenderKeywordChips to fetch missing names from API
    if ((state.filters.selectedKeywords && state.filters.selectedKeywords.length > 0) ||
        (state.filters.excludedKeywords && state.filters.excludedKeywords.length > 0)) {
        loadAndRenderKeywordChips(state.filters.selectedKeywords || [], state.filters.excludedKeywords || []);
    }

    // Languages
    if (filters.language) {
        state.filters.selectedLanguages = filters.language.split(',').filter(v => v);
        applyChipsFromSavedFilters('language', state.filters.selectedLanguages, state.filters.excludedLanguages || []);
    }
    if (filters.language_exclude) {
        state.filters.excludedLanguages = filters.language_exclude.split(',').filter(v => v);
        applyChipsFromSavedFilters('language', state.filters.selectedLanguages || [], state.filters.excludedLanguages);
    }

    // Countries
    if (filters.country) {
        state.filters.selectedCountries = filters.country.split(',').filter(v => v);
        applyChipsFromSavedFilters('country', state.filters.selectedCountries, state.filters.excludedCountries || []);
    }
    if (filters.country_exclude) {
        state.filters.excludedCountries = filters.country_exclude.split(',').filter(v => v);
        applyChipsFromSavedFilters('country', state.filters.selectedCountries || [], state.filters.excludedCountries);
    }

    // Watch providers
    if (filters.watch_provider) {
        state.filters.selectedProviders = filters.watch_provider.split(',').filter(v => v);
    }
    if (filters.watch_provider_exclude) {
        state.filters.excludedProviders = filters.watch_provider_exclude.split(',').filter(v => v);
    }
    if (filters.watch_region) {
        state.filters.watchRegion = filters.watch_region;
    }

    // Networks
    if (filters.network) {
        state.filters.selectedNetworks = filters.network.split(',').filter(v => v);
        applyChipsFromSavedFilters('network', state.filters.selectedNetworks, state.filters.excludedNetworks || []);
    }
    if (filters.network_exclude) {
        state.filters.excludedNetworks = filters.network_exclude.split(',').filter(v => v);
        applyChipsFromSavedFilters('network', state.filters.selectedNetworks || [], state.filters.excludedNetworks);
    }

    // Production companies
    if (filters.production_company) {
        const companyIds = filters.production_company.split(',').filter(v => v);
        state.filters.selectedCompanies = companyIds;

        // Restore company names from saved data
        if (filters.company_names) {
            state.filters.companyCache = { ...state.filters.companyCache, ...filters.company_names };
        }

        // Re-render company chips
        renderCompanyChipsFromState();
    }
    if (filters.production_company_exclude) {
        state.filters.excludedCompanies = filters.production_company_exclude.split(',').filter(v => v);
        renderCompanyChipsFromState();
    }

    // Display options now handled by discover settings (persistent)

    // Lists filter (sidebar lists) - load array
    if (filters.lists) {
        // Parse "source:id" pairs
        const listPairs = filters.lists.split(',').filter(v => v);
        loadSavedLists(listPairs);
    }
}

/**
 * Clear all filters without triggering reload (for preset loading)
 */
function clearAllFiltersQuietly() {
    const state = window.discoverState;
    if (!state || !state.filters) return;

    // Reset all filter values to defaults
    state.filters.mediaType = 'all';
    state.filters.sortBy = 'popularity';
    state.filters.sortOrder = 'desc';
    state.filters.yearFrom = '';
    state.filters.yearTo = '';
    state.filters.releasedWithin = '';
    state.filters.upcomingDays = '';
    state.filters.tmdbRatingMin = 0;
    state.filters.tmdbRatingMax = 10;
    state.filters.imdbRatingMin = 0;
    state.filters.imdbRatingMax = 10;
    state.filters.tmdbVotesMin = 0;
    state.filters.imdbVotesMin = 0;
    state.filters.selectedGenres = [];
    state.filters.excludedGenres = [];
    state.filters.selectedLanguages = [];
    state.filters.excludedLanguages = [];
    state.filters.selectedCountries = [];
    state.filters.excludedCountries = [];
    state.filters.selectedProviders = [];
    state.filters.excludedProviders = [];
    state.filters.watchRegion = 'US';
    state.filters.selectedNetworks = [];
    state.filters.excludedNetworks = [];
    state.filters.selectedCompanies = [];
    state.filters.excludedCompanies = [];
    state.filters.selectedKeywords = [];
    state.filters.excludedKeywords = [];
    state.filters.runtimeMin = 0;
    state.filters.runtimeMax = 300;
    state.filters.titleFilter = '';

    // Clear keyword chips display
    const keywordChips = document.getElementById('keyword-chips');
    if (keywordChips) keywordChips.innerHTML = '';

    // Clear company chips display
    const companyChips = document.getElementById('company-chips');
    if (companyChips) companyChips.innerHTML = '';

    // Clear list selections
    if (window.sidebarListsState) {
        window.sidebarListsState.selectedLists = [];
        window.sidebarListsState.rawResults = [];
        
        // Clear list chips display
        const chipsWrapper = document.getElementById('lists-chips');
        if (chipsWrapper) chipsWrapper.innerHTML = '';
        
        // Update filter availability (re-enable all filters)
        if (typeof updateFilterAvailability === 'function') {
            updateFilterAvailability();
        }
    }

    // Reset UI elements
    const sortSelect = document.getElementById('sort-by');
    if (sortSelect) sortSelect.value = 'popularity';

    const yearFrom = document.getElementById('year-from');
    const yearTo = document.getElementById('year-to');
    if (yearFrom) yearFrom.value = '';
    if (yearTo) yearTo.value = '';

    const releasedWithin = document.getElementById('released-within');
    const upcomingDays = document.getElementById('upcoming-days');
    if (releasedWithin) releasedWithin.value = '';
    if (upcomingDays) upcomingDays.value = '';

    const tmdbRatingMin = document.getElementById('tmdb-rating-min');
    const tmdbRatingMax = document.getElementById('tmdb-rating-max');
    if (tmdbRatingMin) tmdbRatingMin.value = 0;
    if (tmdbRatingMax) tmdbRatingMax.value = 10;

    const tmdbVotesMin = document.getElementById('tmdb-votes-min');
    if (tmdbVotesMin) tmdbVotesMin.value = 0;

    const runtimeMin = document.getElementById('runtime-min');
    const runtimeMax = document.getElementById('runtime-max');
    if (runtimeMin) runtimeMin.value = '';
    if (runtimeMax) runtimeMax.value = '';

    // Clear title filter
    const titleFilter = document.getElementById('title-filter');
    if (titleFilter) titleFilter.value = '';

    // Reset media type buttons
    document.querySelectorAll('[data-filter="type"]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.value === 'all');
    });
}

/**
 * Render keyword chips from state (for preset loading)
 */
function renderKeywordChipsFromState() {
    const state = window.discoverState;
    const chipsWrapper = document.getElementById('keyword-chips');
    if (!chipsWrapper || !state || !state.filters) return;

    chipsWrapper.innerHTML = '';

    // Render included keywords
    state.filters.selectedKeywords.forEach(keywordId => {
        const keywordName = state.filters.keywordCache?.[keywordId] || `Keyword ${keywordId}`;
        const chip = document.createElement('span');
        chip.className = 'chip chip-include';
        chip.innerHTML = `<span class="chip-icon">+</span>${keywordName} <button type="button" class="chip-remove">&times;</button>`;
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            const idx = state.filters.selectedKeywords.indexOf(keywordId);
            if (idx > -1) state.filters.selectedKeywords.splice(idx, 1);
            renderKeywordChipsFromState();
            updateActiveFilters();
        });
        chipsWrapper.appendChild(chip);
    });

    // Render excluded keywords
    state.filters.excludedKeywords.forEach(keywordId => {
        const keywordName = state.filters.keywordCache?.[keywordId] || `Keyword ${keywordId}`;
        const chip = document.createElement('span');
        chip.className = 'chip chip-exclude';
        chip.innerHTML = `<span class="chip-icon">-</span>${keywordName} <button type="button" class="chip-remove">&times;</button>`;
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            const idx = state.filters.excludedKeywords.indexOf(keywordId);
            if (idx > -1) state.filters.excludedKeywords.splice(idx, 1);
            renderKeywordChipsFromState();
            updateActiveFilters();
        });
        chipsWrapper.appendChild(chip);
    });
}

/**
 * Render company chips from state after loading preset/filters
 */
function renderCompanyChipsFromState() {
    const state = window.discoverState;
    const chipsWrapper = document.getElementById('company-chips');
    if (!chipsWrapper || !state || !state.filters) return;

    chipsWrapper.innerHTML = '';

    // Render included companies
    state.filters.selectedCompanies.forEach(companyId => {
        const companyName = state.filters.companyCache?.[companyId] || `Company ${companyId}`;
        const chip = document.createElement('span');
        chip.className = 'chip chip-include';
        chip.innerHTML = `<span class="chip-icon">+</span>${companyName} <button type="button" class="chip-remove">&times;</button>`;
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            const idx = state.filters.selectedCompanies.indexOf(companyId);
            if (idx > -1) state.filters.selectedCompanies.splice(idx, 1);
            renderCompanyChipsFromState();
            updateActiveFilters();
        });
        chipsWrapper.appendChild(chip);
    });

    // Render excluded companies
    state.filters.excludedCompanies.forEach(companyId => {
        const companyName = state.filters.companyCache?.[companyId] || `Company ${companyId}`;
        const chip = document.createElement('span');
        chip.className = 'chip chip-exclude';
        chip.innerHTML = `<span class="chip-icon">-</span>${companyName} <button type="button" class="chip-remove">&times;</button>`;
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            const idx = state.filters.excludedCompanies.indexOf(companyId);
            if (idx > -1) state.filters.excludedCompanies.splice(idx, 1);
            renderCompanyChipsFromState();
            updateActiveFilters();
        });
        chipsWrapper.appendChild(chip);
    });
}

/**
 * Delete the currently selected preset
 */
async function deleteSelectedPreset() {
    const presetSelect = document.getElementById('preset-select');
    const presetId = presetSelect?.value;

    if (!presetId) {
        showNotification('No preset selected', 'warning');
        return;
    }

    const presetName = presetSelect.options[presetSelect.selectedIndex]?.textContent || 'this preset';

    if (!confirm(`Are you sure you want to delete "${presetName}"?`)) {
        return;
    }

    try {
        const response = await fetch(`/discover/api/presets/${presetId}`, {
            method: 'DELETE'
        });

        const data = await response.json();

        if (data.success) {
            showNotification(`Preset "${presetName}" deleted!`, 'success');

            // Refresh the dropdown
            await loadPresetsIntoDropdown();

            // Disable delete button
            const deleteBtn = document.getElementById('delete-preset-btn');
            if (deleteBtn) deleteBtn.disabled = true;
        } else {
            throw new Error(data.error || 'Failed to delete preset');
        }
    } catch (error) {
        console.error('[Presets] Delete error:', error);
        showNotification(error.message || 'Failed to delete preset', 'error');
    }
}

// Initialize filter presets when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    // Initialize after a short delay to ensure other components are ready
    setTimeout(initFilterPresets, 150);
});


// =============================================================================
// MDBLIST INTEGRATION
// =============================================================================

/**
 * MDBList state
 */
window.mdblistState = {
    configured: false,
    connected: false,
    availableLists: [],
    currentList: null,
    isLoading: false
};

/**
 * Initialize MDBList functionality
 * Checks if MDBList is configured and loads available lists
 */
async function initMDBList() {
    console.log('[MDBList] Checking configuration...');

    try {
        const response = await fetch('/discover/api/mdblist/status');
        const data = await response.json();

        window.mdblistState.configured = data.configured;
        window.mdblistState.connected = data.connected;

        if (data.configured && data.connected) {
            console.log('[MDBList] Connected, loading available lists...');
            await loadMDBListOptions();
            showMDBListDropdown();
            bindMDBListEvents();
        } else if (data.configured && !data.connected) {
            console.warn('[MDBList] Configured but connection failed:', data.message);
        } else {
            console.log('[MDBList] Not configured');
        }
    } catch (error) {
        console.error('[MDBList] Init error:', error);
    }
}

/**
 * Load available MDBList options into the dropdown
 */
async function loadMDBListOptions() {
    try {
        const response = await fetch('/discover/api/mdblist/lists');
        const data = await response.json();

        if (data.success && data.lists) {
            window.mdblistState.availableLists = data.lists;
            populateMDBListDropdown(data.lists);
            
            // Restore saved selection
            const saved = localStorage.getItem('discoverMDBList');
            if (saved) {
                try {
                    const savedList = JSON.parse(saved);
                    const list = data.lists.find(l => l.key === savedList.key);
                    if (list) {
                        await selectMDBList(list);
                    }
                } catch (e) {
                    console.error('[MDBList] Failed to restore selection:', e);
                }
            }
        }
    } catch (error) {
        console.error('[MDBList] Error loading lists:', error);
    }
}

/**
 * Populate the MDBList dropdown menu with available lists
 */
function populateMDBListDropdown(lists) {
    const dropdown = document.getElementById('mdblist-dropdown-menu');
    if (!dropdown) return;

    dropdown.innerHTML = '';

    // Category display names - matches CURATED_LISTS categories in mdblist_api.py
    const categoryNames = {
        'mdblist': 'Popular Lists',
        'streaming': 'Streaming Top Lists',
        'originals': 'Streaming Originals',
        'curated': 'Curated Collections',
        'other': 'Other'
    };

    // Define category order
    const categoryOrder = ['mdblist', 'streaming', 'originals', 'curated', 'other'];

    // Group lists by category
    const grouped = {};
    lists.forEach(list => {
        const category = list.category || 'other';
        if (!grouped[category]) grouped[category] = [];
        grouped[category].push(list);
    });

    // Create dropdown items in defined order
    categoryOrder.forEach(category => {
        const groupLists = grouped[category];
        if (!groupLists || groupLists.length === 0) return;

        // Add group header
        const header = document.createElement('div');
        header.className = 'mdblist-dropdown-header';
        header.textContent = categoryNames[category] || category;
        dropdown.appendChild(header);

        // Add list items
        groupLists.forEach(list => {
            const item = document.createElement('div');
            item.className = 'mdblist-dropdown-item';
            item.dataset.listKey = list.key;
            item.innerHTML = `<span class="list-name">${list.name}</span>`;
            item.addEventListener('click', () => selectMDBList(list));
            dropdown.appendChild(item);
        });
    });
}

/**
 * Show the MDBList dropdown container
 */
function showMDBListDropdown() {
    const container = document.getElementById('mdblist-tabs');
    if (container) {
        container.style.display = 'flex';
    }
}

/**
 * Bind MDBList dropdown events
 */
function bindMDBListEvents() {
    const dropdownBtn = document.getElementById('mdblist-dropdown-btn');
    const dropdownMenu = document.getElementById('mdblist-dropdown-menu');

    if (dropdownBtn && dropdownMenu) {
        // Toggle dropdown on button click
        dropdownBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const isOpen = dropdownMenu.classList.toggle('open');
            dropdownBtn.classList.toggle('open', isOpen);
            dropdownBtn.classList.toggle('active', isOpen);
        });

        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!dropdownBtn.contains(e.target) && !dropdownMenu.contains(e.target)) {
                dropdownMenu.classList.remove('open');
                dropdownBtn.classList.remove('open');
                dropdownBtn.classList.remove('active');
            }
        });
    }
}

/**
 * Select an MDBList and load its content
 */
async function selectMDBList(list) {
    window.mdblistState.currentList = list;
    
    // Save selection to localStorage and clear FlixPatrol
    localStorage.setItem('discoverMDBList', JSON.stringify({key: list.key, name: list.name}));
    localStorage.removeItem('discoverFlixPatrol');

    // Update dropdown label
    const label = document.getElementById('mdblist-dropdown-label');
    if (label) {
        label.textContent = list.name;
    }

    // Close dropdown
    const dropdownMenu = document.getElementById('mdblist-dropdown-menu');
    const dropdownBtn = document.getElementById('mdblist-dropdown-btn');
    if (dropdownMenu) dropdownMenu.classList.remove('open');
    if (dropdownBtn) {
        dropdownBtn.classList.remove('open');
        dropdownBtn.classList.remove('active');
        dropdownBtn.classList.add('selected');
    }

    // Update tab buttons - deselect trending, mark MDBList as active
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    if (dropdownBtn) dropdownBtn.classList.add('active');

    // Reset FlixPatrol selection if active
    if (typeof resetFlixPatrolSelection === 'function') {
        resetFlixPatrolSelection();
    }

    // Update state
    window.discoverState.currentTab = 'mdblist';
    window.discoverState.page = 1;

    // Show search results container, hide trending content
    if (trendingContent) trendingContent.style.display = 'none';
    if (searchResults) searchResults.style.display = 'block';

    // Load the list content
    await loadMDBListContent(list.key);
}

/**
 * Load content from selected MDBList
 */
async function loadMDBListContent(listKey) {
    if (window.mdblistState.isLoading) return;

    window.mdblistState.isLoading = true;
    setLoadingFlag();

    // Clear existing results
    if (resultsGrid) {
        resultsGrid.innerHTML = '';
    }

    try {
        const mediaType = window.discoverState.filters.mediaType || 'all';
        const response = await fetch(`/discover/api/mdblist/list/${listKey}?type=${mediaType}&limit=40`);
        const data = await response.json();

        if (data.success && data.results) {
            renderResults(data.results);

            // Update pagination info
            window.discoverState.hasMore = false; // MDBList doesn't paginate the same way

            // Update results info display
            updateResultsInfo({
                total_results: data.results.length,
                page: 1,
                total_pages: 1
            });
        } else{
            console.error('[MDBList] API Error:', data.error);
            if (data.error && data.error.includes('API key not configured')) {
                showError('MDBList API key not configured. Go to Settings > Additional Settings to add your API key.');
            } else {
                showError(data.error || 'Failed to load list');
            }
        }
    } catch (error) {
        console.error('[MDBList] Load error:', error);
        showError('Failed to load MDBList content');
    } finally {
        window.mdblistState.isLoading = false;
        clearLoadingFlag();
    }
}

/**
 * Reset MDBList selection when switching to another tab
 */
function resetMDBListSelection() {
    window.mdblistState.currentList = null;

    const label = document.getElementById('mdblist-dropdown-label');
    if (label) {
        label.textContent = 'Lists';
    }

    const dropdownBtn = document.getElementById('mdblist-dropdown-btn');
    if (dropdownBtn) {
        dropdownBtn.classList.remove('active', 'selected');
    }
}

// Initialize MDBList when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    // Initialize after other components are ready
    setTimeout(initMDBList, 200);
});


// =============================================================================
// FLIXPATROL INTEGRATION (No API Key Required)
// =============================================================================

/**
 * FlixPatrol state
 */
window.flixpatrolState = {
    platforms: [],
    currentPlatform: null,
    isLoading: false
};

/**
 * Initialize FlixPatrol functionality
 * Loads available streaming platforms and sets up the dropdown
 */
async function initFlixPatrol() {
    try{
        const response = await fetch('/discover/api/flixpatrol/platforms');
        const data = await response.json();

        if (data.success && data.platforms) {
            window.flixpatrolState.platforms = data.platforms;
            populateFlixPatrolDropdown(data.platforms);
            bindFlixPatrolEvents();

            // Restore saved selection
            const saved = localStorage.getItem('discoverFlixPatrol');
            if (saved) {
                try {
                    const savedPlatform = JSON.parse(saved);
                    const platform = data.platforms.find(p => p.id === savedPlatform.id);
                    if (platform) {
                        await selectFlixPatrolPlatform(platform);
                    }
                } catch (e) {
                    console.error('[FlixPatrol] Failed to restore selection:', e);
                }
            }
        }
    } catch (error) {
        console.error('[FlixPatrol] Init error:', error);
    }
}

/**
 * Populate the FlixPatrol dropdown menu with available platforms
 */
function populateFlixPatrolDropdown(platforms) {
    const dropdown = document.getElementById('flixpatrol-dropdown-menu');
    if (!dropdown) return;

    dropdown.innerHTML = '';

    // Add header
    const header = document.createElement('div');
    header.className = 'flixpatrol-dropdown-header';
    header.textContent = 'Streaming Top 10';
    dropdown.appendChild(header);

    // Add platform items
    platforms.forEach(platform => {
        const item = document.createElement('div');
        item.className = 'flixpatrol-dropdown-item';
        item.dataset.platformId = platform.id;
        item.innerHTML = `
            <span class="platform-icon platform-${platform.icon}"></span>
            <span class="platform-name">${platform.name}</span>
        `;
        item.addEventListener('click', () => selectFlixPatrolPlatform(platform));
        dropdown.appendChild(item);
    });
}

/**
 * Bind FlixPatrol dropdown events
 */
function bindFlixPatrolEvents() {
    const dropdownBtn = document.getElementById('flixpatrol-dropdown-btn');
    const dropdownMenu = document.getElementById('flixpatrol-dropdown-menu');

    if (dropdownBtn && dropdownMenu) {
        // Toggle dropdown on button click
        dropdownBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const isOpen = dropdownMenu.classList.toggle('open');
            dropdownBtn.classList.toggle('open', isOpen);
            dropdownBtn.classList.toggle('active', isOpen);
        });

        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!dropdownBtn.contains(e.target) && !dropdownMenu.contains(e.target)) {
                dropdownMenu.classList.remove('open');
                dropdownBtn.classList.remove('open');
                dropdownBtn.classList.remove('active');
            }
        });
    }
}

/**
 * Select a FlixPatrol platform and load its Top 10 content
 */
async function selectFlixPatrolPlatform(platform) {
    window.flixpatrolState.currentPlatform = platform;
    
    // Save selection to localStorage and clear MDBList
    localStorage.setItem('discoverFlixPatrol', JSON.stringify({id: platform.id, name: platform.name}));
    localStorage.removeItem('discoverMDBList');

    // Update dropdown label
    const label = document.getElementById('flixpatrol-dropdown-label');
    if (label) {
        label.textContent = platform.name + ' Top 10';
    }

    // Close dropdown
    const dropdownMenu = document.getElementById('flixpatrol-dropdown-menu');
    const dropdownBtn = document.getElementById('flixpatrol-dropdown-btn');
    if (dropdownMenu) dropdownMenu.classList.remove('open');
    if (dropdownBtn) {
        dropdownBtn.classList.remove('open');
        dropdownBtn.classList.remove('active');
        dropdownBtn.classList.add('selected');
    }

    // Update tab buttons - deselect trending, mark FlixPatrol as active
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    if (dropdownBtn) dropdownBtn.classList.add('active');

    // Reset MDBList selection if active
    if (typeof resetMDBListSelection === 'function') {
        resetMDBListSelection();
    }

    // Update state
    window.discoverState.currentTab = 'flixpatrol';
    window.discoverState.page = 1;

    // Show search results container, hide trending content
    if (trendingContent) trendingContent.style.display = 'none';
    if (searchResults) searchResults.style.display = 'block';

    // Load the platform's Top 10 content
    await loadFlixPatrolContent(platform.id);
}

/**
 * Load Top 10 content from selected FlixPatrol platform
 */
async function loadFlixPatrolContent(platformId) {
    if (window.flixpatrolState.isLoading) return;

    window.flixpatrolState.isLoading = true;
    setLoadingFlag();

    // Clear existing results
    if (resultsGrid) {
        resultsGrid.innerHTML = '';
    }

    try {
        const mediaType = window.discoverState.filters.mediaType || 'all';
        const response = await fetch(`/discover/api/flixpatrol/top10/${platformId}?type=${mediaType}`);
        const data = await response.json();

        if (data.success && data.results) {
            renderResults(data.results);

            // Update pagination info
            window.discoverState.hasMore = false; // Top 10 doesn't paginate

            // Update results info display
            updateResultsInfo({
                total_results: data.results.length,
                page: 1,
                total_pages: 1
            });
        } else {
            console.error('[FlixPatrol] API Error:', data.error);
            showError(data.error || 'Failed to load Top 10');
        }
    } catch (error) {
        console.error('[FlixPatrol] Load error:', error);
        showError('Failed to load FlixPatrol content');
    } finally {
        window.flixpatrolState.isLoading = false;
        clearLoadingFlag();
    }
}

/**
 * Reset FlixPatrol selection when switching to another tab
 */
function resetFlixPatrolSelection() {
    window.flixpatrolState.currentPlatform = null;

    const label = document.getElementById('flixpatrol-dropdown-label');
    if (label) {
        label.textContent = 'Top 10';
    }

    const dropdownBtn = document.getElementById('flixpatrol-dropdown-btn');
    if (dropdownBtn) {
        dropdownBtn.classList.remove('active', 'selected');
    }
}

// Initialize FlixPatrol when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    // Initialize after other components are ready
    setTimeout(initFlixPatrol, 100);
});


// =============================================================================
// SIDEBAR LISTS FILTER (Combined FlixPatrol + MDBList)
// =============================================================================

/**
 * Sidebar Lists Filter state
 */
window.sidebarListsState = {
    isLoaded: false,
    selectedLists: [],  // Array of {source, listId, listName}
    flixpatrolPlatforms: [],
    mdblistLists: [],
    rawResults: [],  // Store merged raw list results for client-side filtering
};

/**
 * Initialize the sidebar Lists filter dropdown
 */
async function initSidebarListsFilter() {
    const container = document.getElementById('lists-container');
    const dropdown = document.getElementById('lists-dropdown');
    const toggleBtn = document.getElementById('lists-dropdown-toggle');
    const chipsWrapper = document.getElementById('lists-chips');

    if (!container || !dropdown || !toggleBtn) {
        return;
    }

    // Load data from both sources
    await loadSidebarListsData();

    // Populate dropdown
    populateSidebarListsDropdown();
    
    // Restore saved lists from localStorage
    const savedLists = loadSidebarListsFromStorage();
    if (savedLists && savedLists.length > 0) {
        // Restore selection state
        window.sidebarListsState.selectedLists = savedLists;
        
        // Update dropdown visual state
        savedLists.forEach(list => {
            const item = dropdown.querySelector(`[data-source="${list.source}"][data-list-id="${list.listId}"]`);
            if (item) item.classList.add('included');
        });
        
        // Render chips
        renderSidebarListChips();
        
        // Update filter availability
        updateFilterAvailability();
        
        // Load the content
        loadAllSelectedLists();
    }

    // Toggle dropdown on button click
    toggleBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        const isOpen = dropdown.classList.contains('show');
        // Close all other chip dropdowns
        document.querySelectorAll('.chips-dropdown.show').forEach(d => {
            if (d !== dropdown) d.classList.remove('show');
        });
        if (!isOpen) {
            dropdown.classList.add('show', 'dropdown-up'); // Open upward
        } else {
            dropdown.classList.remove('show');
        }
    });

    // Also toggle on container click
    container.addEventListener('click', function(e) {
        if (e.target === container || e.target.closest('.chips-wrapper')) {
            e.stopPropagation();
            const isOpen = dropdown.classList.contains('show');
            // Close all other chip dropdowns
            document.querySelectorAll('.chips-dropdown.show').forEach(d => {
                if (d !== dropdown) d.classList.remove('show');
            });
            if (!isOpen) {
                dropdown.classList.add('show', 'dropdown-up');
            } else {
                dropdown.classList.remove('show');
            }
        }
    });

    // Close dropdown when clicking outside
    document.addEventListener('click', function(e) {
        if (!container.contains(e.target)) {
            dropdown.classList.remove('show');
        }
    });
}

/**
 * Load FlixPatrol platforms and MDBList lists
 */
async function loadSidebarListsData() {
    try {
        // Load FlixPatrol platforms
        const fpResponse = await fetch('/discover/api/flixpatrol/platforms');
        if (fpResponse.ok) {
            const fpData = await fpResponse.json();
            window.sidebarListsState.flixpatrolPlatforms = fpData.platforms || [];
        }

        // Load MDBList curated lists
        const mdbResponse = await fetch('/discover/api/mdblist/lists');
        if (mdbResponse.ok) {
            const mdbData = await mdbResponse.json();
            window.sidebarListsState.mdblistLists = mdbData.lists || [];
        }

        window.sidebarListsState.isLoaded = true;
    } catch (error) {
        console.error('[Lists Filter] Error loading lists data:', error);
    }
}

/**
 * Populate the sidebar lists dropdown with FlixPatrol and MDBList options
 */
function populateSidebarListsDropdown() {
    const dropdown = document.getElementById('lists-dropdown');
    if (!dropdown) return;

    dropdown.innerHTML = '';

    const state = window.sidebarListsState;

    // Add "None" option to clear selection
    const noneItem = document.createElement('div');
    noneItem.className = 'chips-dropdown-item';
    noneItem.dataset.value = '';
    noneItem.dataset.source = 'none';
    noneItem.textContent = '— Clear All —';
    noneItem.addEventListener('click', () => clearSidebarListSelection());
    dropdown.appendChild(noneItem);

    // Add FlixPatrol section
    if (state.flixpatrolPlatforms.length > 0) {
        const fpHeader = document.createElement('div');
        fpHeader.className = 'chips-dropdown-header';
        fpHeader.textContent = 'FlixPatrol Top 10';
        dropdown.appendChild(fpHeader);

        state.flixpatrolPlatforms.forEach(platform => {
            const item = document.createElement('div');
            item.className = 'chips-dropdown-item';
            item.dataset.value = `flixpatrol:${platform.id}`;
            item.dataset.source = 'flixpatrol';
            item.dataset.listId = platform.id;
            item.dataset.name = platform.name;
            
            // Check if already selected
            const isSelected = state.selectedLists.some(l => l.source === 'flixpatrol' && l.listId === platform.id);
            if (isSelected) item.classList.add('included');
            
            item.innerHTML = `<span class="list-icon">${getPlatformIcon(platform.icon)}</span> ${platform.name}`;
            item.addEventListener('click', () => toggleSidebarList(item, 'flixpatrol', platform.id, platform.name));
            dropdown.appendChild(item);
        });
    }

    // Add MDBList section - organized by category
    if (state.mdblistLists.length > 0) {
        // Group by category
        const categories = {};
        state.mdblistLists.forEach(list => {
            const cat = list.category || 'other';
            if (!categories[cat]) categories[cat] = [];
            categories[cat].push(list);
        });

        // Category display names
        const categoryNames = {
            'mdblist': 'MDBList Popular',
            'streaming': 'Streaming Top Lists',
            'originals': 'Streaming Originals',
            'curated': 'Curated Collections'
        };

        // Order categories
        const categoryOrder = ['mdblist', 'streaming', 'originals', 'curated'];

        categoryOrder.forEach(catKey => {
            if (categories[catKey] && categories[catKey].length > 0) {
                const catHeader = document.createElement('div');
                catHeader.className = 'chips-dropdown-header';
                catHeader.textContent = categoryNames[catKey] || catKey;
                dropdown.appendChild(catHeader);

                categories[catKey].forEach(list => {
                    const item = document.createElement('div');
                    item.className = 'chips-dropdown-item';
                    item.dataset.value = `mdblist:${list.key}`;
                    item.dataset.source = 'mdblist';
                    item.dataset.listId = list.key;
                    item.dataset.name = list.name;
                    
                    // Check if already selected
                    const isSelected = state.selectedLists.some(l => l.source === 'mdblist' && l.listId === list.key);
                    if (isSelected) item.classList.add('included');
                    
                    item.innerHTML = `<span class="list-icon">${getPlatformIcon(list.icon)}</span> ${list.name}`;
                    item.addEventListener('click', () => toggleSidebarList(item, 'mdblist', list.key, list.name));
                    dropdown.appendChild(item);
                });
            }
        });
    }

    // If no lists loaded
    if (state.flixpatrolPlatforms.length === 0 && state.mdblistLists.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'chips-dropdown-empty';
        empty.textContent = 'No lists available';
        dropdown.appendChild(empty);
    }
}

/**
 * Get platform icon HTML
 */
function getPlatformIcon(iconName) {
    const icons = {
        'netflix': '🔴',
        'disney': '🏰',
        'amazon': '📦',
        'hbo': '🟣',
        'apple': '🍎',
        'paramount': '⭐',
        'hulu': '💚',
        'peacock': '🦚',
        'mdblist': '📋',
        'rottentomatoes': '🍅',
        'metacritic': '🎯',
        'commonsense': '👨‍👩‍👧‍👦',
        'bbc': '📺',
        'discovery': '🔍'
    };
    return icons[iconName] || '📋';
}

/**
 * Select a list without needing the itemElement (for programmatic selection)
 */
function selectSidebarList(source, listId, listName) {
    const state = window.sidebarListsState;

    // Check if already selected
    const existingIndex = state.selectedLists.findIndex(l => l.source === source && l.listId === listId);

    if (existingIndex < 0) {
        // Add to selection if not already present
        state.selectedLists.push({ source, listId, listName });
    }

    // Update chips display
    renderSidebarListChips();

    // Update filter availability based on list selection
    updateFilterAvailability();

    // Update active filters display
    updateActiveFilters();

    // Save lists selection to localStorage
    saveSidebarListsToStorage();

    // Reload content with selected list
    loadSidebarListContent(source, listId);
}

/**
 * Toggle a list selection (add/remove chip)
 */
function toggleSidebarList(itemElement, source, listId, listName) {
    const state = window.sidebarListsState;

    // Check if already selected
    const existingIndex = state.selectedLists.findIndex(l => l.source === source && l.listId === listId);

    if (existingIndex >= 0) {
        // Remove from selection
        state.selectedLists.splice(existingIndex, 1);
        itemElement.classList.remove('included');
    } else {
        // Add to selection
        state.selectedLists.push({ source, listId, listName });
        itemElement.classList.add('included');
    }

    // Update chips display
    renderSidebarListChips();

    // Update filter availability based on list selection
    updateFilterAvailability();

    // Update active filters display
    updateActiveFilters();

    // Save lists selection to localStorage
    saveSidebarListsToStorage();

    // Reload content with all selected lists
    loadAllSelectedLists();
}

/**
 * Render chips for all selected lists
 */
function renderSidebarListChips() {
    const chipsWrapper = document.getElementById('lists-chips');
    const hiddenInput = document.getElementById('selected-list');
    const state = window.sidebarListsState;
    
    if (!chipsWrapper) return;
    
    chipsWrapper.innerHTML = '';
    
    state.selectedLists.forEach(list => {
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.innerHTML = `
            ${list.listName}
            <button type="button" class="chip-remove" data-source="${list.source}" data-list-id="${list.listId}">×</button>
        `;
        
        // Add remove handler
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            removeSidebarList(list.source, list.listId);
        });
        
        chipsWrapper.appendChild(chip);
    });
    
    // Update hidden input with all selected lists
    if (hiddenInput) {
        const values = state.selectedLists.map(l => `${l.source}:${l.listId}`);
        hiddenInput.value = values.join(',');
    }
}

/**
 * Remove a specific list from selection
 */
function removeSidebarList(source, listId) {
    const state = window.sidebarListsState;
    const index = state.selectedLists.findIndex(l => l.source === source && l.listId === listId);
    
    if (index >= 0) {
        state.selectedLists.splice(index, 1);
        
        // Update dropdown item state
        const dropdown = document.getElementById('lists-dropdown');
        if (dropdown) {
            const item = dropdown.querySelector(`[data-source="${source}"][data-list-id="${listId}"]`);
            if (item) item.classList.remove('included');
        }
        
        // Re-render chips
        renderSidebarListChips();
        
        // Update filter availability
        updateFilterAvailability();
        
        // Update active filters
        updateActiveFilters();
        
        // Save lists selection to localStorage
        saveSidebarListsToStorage();
        
        // Reload content
        loadAllSelectedLists();
    }
}

/**
 * Update filter availability based on whether lists are selected
 * Some filters only work with TMDB API calls, not with pre-fetched list data
 */
function updateFilterAvailability() {
    const listsState = window.sidebarListsState;
    const hasListsSelected = listsState.selectedLists && listsState.selectedLists.length > 0;
    
    // Filters that don't work with lists (require additional TMDB API calls)
    const unavailableFilters = [
        'keywords',      // Requires separate API call
        'certifications', // Requires separate API call  
        'cast',          // Requires credits API call
        'crew'           // Requires credits API call
    ];
    
    const unavailableFilterGroups = [
        'provider-filter-group',  // Streaming providers - not in basic details
        'network-filter-group',   // TV networks - not in basic details
        'watch-region'            // Watch region (for providers)
    ];
    
    // Disable/enable filter sections
    unavailableFilters.forEach(sectionName => {
        const section = document.querySelector(`[data-section="${sectionName}"]`);
        if (section) {
            if (hasListsSelected) {
                section.classList.add('filter-disabled');
                section.setAttribute('title', 'This filter is not available when using Lists');
            } else {
                section.classList.remove('filter-disabled');
                section.removeAttribute('title');
            }
        }
    });
    
    // Disable/enable filter groups
    unavailableFilterGroups.forEach(groupId => {
        const group = document.getElementById(groupId);
        if (group) {
            if (hasListsSelected) {
                group.classList.add('filter-disabled');
                group.setAttribute('title', 'This filter is not available when using Lists');
                // Disable all inputs within the group
                const inputs = group.querySelectorAll('input, select, button');
                inputs.forEach(input => {
                    input.disabled = true;
                    input.setAttribute('data-was-disabled-by-lists', 'true');
                });
            } else {
                group.classList.remove('filter-disabled');
                group.removeAttribute('title');
                // Re-enable inputs
                const inputs = group.querySelectorAll('[data-was-disabled-by-lists]');
                inputs.forEach(input => {
                    input.disabled = false;
                    input.removeAttribute('data-was-disabled-by-lists');
                });
            }
        }
    });
}

/**
 * Load content from all selected lists and merge results
 */
async function loadAllSelectedLists() {
    const state = window.discoverState;
    const listsState = window.sidebarListsState;
    
    // If no lists selected, clear results
    if (listsState.selectedLists.length === 0) {
        const resultsGrid = document.getElementById('results-grid');
        if (resultsGrid) resultsGrid.innerHTML = '';
        listsState.rawResults = [];
        return;
    }
    
    // Show search results area, hide trending
    const trendingContent = document.getElementById('trending-content');
    const searchResults = document.getElementById('search-results');
    if (trendingContent) trendingContent.style.display = 'none';
    if (searchResults) searchResults.style.display = 'block';
    
    // Reset page
    state.page = 1;
    state.autoLoadCount = 0;
    
    setLoadingFlag();
    
    try {
        const allResults = [];
        const seenIds = new Set();  // Track unique TMDB IDs to avoid duplicates
        
        // Load each selected list
        for (const list of listsState.selectedLists) {
            try {
                let data;

                if (list.source === 'flixpatrol') {
                    const response = await fetch(`/discover/api/flixpatrol/top10/${list.listId}`);
                    if (!response.ok) throw new Error(`Failed to load ${list.listName}`);
                    data = await response.json();
                } else if (list.source === 'mdblist') {
                    const response = await fetch(`/discover/api/mdblist/list/${list.listId}`);
                    if (!response.ok) throw new Error(`Failed to load ${list.listName}`);
                    data = await response.json();
                }

                // Add results, avoiding duplicates
                if (data && data.results) {
                    data.results.forEach(item => {
                        const itemId = item.id || item.tmdb_id;
                        if (itemId && !seenIds.has(itemId)) {
                            seenIds.add(itemId);
                            allResults.push(item);
                        }
                    });
                }
            } catch (error) {
                console.error(`[Lists] Error loading ${list.listName}:`, error);
                showNotification(`Failed to load ${list.listName}`, 'error');
            }
        }
        
        // Store merged results for filtering
        listsState.rawResults = allResults;

        // Apply filters and render
        filterAndRenderListResults();
        
    } catch (error) {
        console.error('[Lists] Load error:', error);
        showNotification('Failed to load lists', 'error');
    } finally {
        clearLoadingFlag();
    }
}

/**
 * Clear all sidebar list selections
 */
function clearSidebarListSelection() {
    const state = window.sidebarListsState;
    
    // Clear saved lists from localStorage
    clearSavedSidebarLists();
    
    // Clear all selections
    state.selectedLists = [];
    state.rawResults = [];
    
    // Clear dropdown item states
    const dropdown = document.getElementById('lists-dropdown');
    if (dropdown) {
        dropdown.querySelectorAll('.chips-dropdown-item').forEach(item => {
            item.classList.remove('included');
        });
    }
    
    // Re-render chips (will be empty)
    renderSidebarListChips();
    
    // Update active filters
    updateActiveFilters();
    
    // Clear results
    const resultsGrid = document.getElementById('results-grid');
    if (resultsGrid) resultsGrid.innerHTML = '';
}

/**
 * Load multiple saved lists from preset or adaptive list (for restoring state)
 * @param {string[]} listPairs - Array of "source:listId" strings
 */
async function loadSavedLists(listPairs) {
    // Ensure lists data is loaded
    if (!window.sidebarListsState.isLoaded) {
        await loadSidebarListsData();
    }
    
    const state = window.sidebarListsState;
    state.selectedLists = [];
    
    // Parse each list pair and add to selection
    for (const pair of listPairs) {
        const [source, listId] = pair.split(':');
        if (!source || !listId) continue;
        
        // Find the list name from the loaded data
        let listName = null;
        
        if (source === 'flixpatrol') {
            const platform = state.flixpatrolPlatforms.find(p => p.id === listId);
            listName = platform ? platform.name : `FlixPatrol ${listId}`;
        } else if (source === 'mdblist') {
            const list = state.mdblistLists.find(l => l.key === listId);
            listName = list ? list.name : `MDBList ${listId}`;
        }
        
        if (listName) {
            state.selectedLists.push({ source, listId, listName });
        }
    }
    
    // Update dropdown states
    const dropdown = document.getElementById('lists-dropdown');
    if (dropdown) {
        state.selectedLists.forEach(list => {
            const item = dropdown.querySelector(`[data-source="${list.source}"][data-list-id="${list.listId}"]`);
            if (item) item.classList.add('included');
        });
    }
    
    // Render chips
    renderSidebarListChips();
    
    // Update filter availability based on list selection
    updateFilterAvailability();
    
    // Load content from all selected lists
    if (state.selectedLists.length > 0) {
        await loadAllSelectedLists();
    }
}

/**
 * Load content from the selected list
 */
async function loadSidebarListContent(source, listId) {
    const state = window.discoverState;
    const listsState = window.sidebarListsState;
    
    // Store list metadata for re-filtering
    listsState.listSource = source;
    listsState.listId = listId;
    
    // Show search results area, hide trending
    if (trendingContent) trendingContent.style.display = 'none';
    if (searchResults) searchResults.style.display = 'block';
    
    // Reset page
    state.page = 1;
    state.autoLoadCount = 0;
    
    setLoadingFlag();
    
    try {
        let data;
        
        if (source === 'flixpatrol') {
            // Load FlixPatrol list
            const response = await fetch(`/discover/api/flixpatrol/top10/${listId}`);
            if (!response.ok) throw new Error('Failed to load FlixPatrol list');
            data = await response.json();
        } else if (source === 'mdblist') {
            // Load MDBList
            const response = await fetch(`/discover/api/mdblist/list/${listId}`);
            if (!response.ok) throw new Error('Failed to load MDBList');
            data = await response.json();
        } else {
            throw new Error('Invalid list source');
        }
        
        // Store raw results for filtering
        listsState.rawResults = data.results || [];
        
        // Apply current filters to list results
        const filteredResults = filterListResults(listsState.rawResults);
        
        // Render filtered results
        if (state.page === 1) {
            if (resultsGrid) {
                resultsGrid.innerHTML = '';
            }
        }
        
        if (filteredResults && filteredResults.length > 0) {
            renderResults(filteredResults);
            hideError();
            hideEmpty();
            
            // Update results info display
            updateResultsInfo({
                total_results: filteredResults.length,
                page: 1,
                total_pages: 1
            });
        } else {
            if (state.page === 1) {
                showEmpty();
            }
        }
        
        state.hasMore = false;
        updatePagination();
        
    } catch (error) {
        console.error('[Sidebar Lists] Error loading list:', error);
        showError();
    } finally {
        clearLoadingFlag();
    }
}

/**
 * Apply list filter from saved preset/adaptive list
 */
function applySidebarListFilter(listValue) {
    if (!listValue) {
        clearSidebarListSelection();
        return;
    }

    // Parse "source:listId" format
    const [source, ...listIdParts] = listValue.split(':');
    const listId = listIdParts.join(':'); // Handle list IDs that might contain colons

    if (source && listId) {
        // Find the list name
        let listName = listId;

        if (source === 'flixpatrol') {
            const platform = window.sidebarListsState.flixpatrolPlatforms.find(p => p.id === listId);
            if (platform) listName = platform.name;
        } else if (source === 'mdblist') {
            const list = window.sidebarListsState.mdblistLists.find(l => l.key === listId);
            if (list) listName = list.name;
        }

        selectSidebarList(source, listId, listName);
    }
}

/**
 * Filter and render list results (helper function)
 */
function filterAndRenderListResults() {
    const state = window.discoverState;
    const listsState = window.sidebarListsState;
    const resultsGrid = document.getElementById('results-grid');

    // Apply current filters to list results
    const filteredResults = filterListResults(listsState.rawResults);

    // Clear grid first
    if (resultsGrid) {
        resultsGrid.innerHTML = '';
    }
    
    if (filteredResults && filteredResults.length > 0) {
        renderResults(filteredResults);
        hideError();
        hideEmpty();
        
        // Update results info display
        updateResultsInfo({
            total_results: filteredResults.length,
            page: 1,
            total_pages: 1
        });
    } else {
        // No results after filtering
        showEmpty();
    }
    
    // Disable pagination/auto-load for list results (they're all loaded at once)
    state.hasMore = false;
    updatePagination();
}

/**
 * Filter list results based on current filter state
 * @param {Array} results - Raw list results to filter
 * @returns {Array} - Filtered results
 */
function filterListResults(results) {
    if (!results || results.length === 0) return [];
    
    const state = window.discoverState;
    const filters = state.filters;

    let filtered = results.filter(item => {
        // Media type filter
        if (filters.mediaType && filters.mediaType !== 'all') {
            if (item.media_type !== filters.mediaType) {
                return false;
            }
        }
        
        // Year range filter
        if (filters.yearFrom || filters.yearTo) {
            const releaseDate = item.release_date || item.first_air_date;
            if (releaseDate) {
                const year = parseInt(releaseDate.substring(0, 4));
                if (filters.yearFrom && year < parseInt(filters.yearFrom)) return false;
                if (filters.yearTo && year > parseInt(filters.yearTo)) return false;
            } else if (filters.yearFrom || filters.yearTo) {
                // No release date but year filter is active - exclude
                return false;
            }
        }
        
        // Released within filter
        if (filters.releasedWithin) {
            const releaseDate = item.release_date || item.first_air_date;
            if (releaseDate) {
                const daysAgo = parseInt(filters.releasedWithin);
                const cutoffDate = new Date();
                cutoffDate.setDate(cutoffDate.getDate() - daysAgo);
                const itemDate = new Date(releaseDate);
                if (itemDate < cutoffDate) return false;
            } else {
                return false; // No date, can't match "released within"
            }
        }
        
        // Upcoming days filter
        if (filters.upcomingDays) {
            const releaseDate = item.release_date || item.first_air_date;
            if (releaseDate) {
                const daysAhead = parseInt(filters.upcomingDays);
                const now = new Date();
                const futureDate = new Date();
                futureDate.setDate(futureDate.getDate() + daysAhead);
                const itemDate = new Date(releaseDate);
                if (itemDate < now || itemDate > futureDate) return false;
            } else {
                return false;
            }
        }
        
        // TMDB rating filter
        if (filters.tmdbRatingMin > 0 || filters.tmdbRatingMax < 10) {
            const rating = item.vote_average || 0;
            if (filters.tmdbRatingMin && rating < filters.tmdbRatingMin) return false;
            if (filters.tmdbRatingMax && rating > filters.tmdbRatingMax) return false;
        }
        
        // TMDB votes filter
        if (filters.tmdbVotesMin > 0) {
            const votes = item.vote_count || 0;
            if (votes < filters.tmdbVotesMin) return false;
        }
        
        // Genre filter (include)
        if (filters.selectedGenres && filters.selectedGenres.length > 0) {
            // Handle both genre_ids (array of integers) and genres (array of objects with id)
            let itemGenres = item.genre_ids || [];
            if (!itemGenres.length && item.genres) {
                // If genres is an array of objects like [{id: 28, name: "Action"}]
                itemGenres = item.genres.map(g => typeof g === 'object' ? g.id : g);
            }
            
            const hasGenre = filters.selectedGenres.some(genreId => 
                itemGenres.includes(parseInt(genreId))
            );
            if (!hasGenre) return false;
        }
        
        // Genre filter (exclude)
        if (filters.excludedGenres && filters.excludedGenres.length > 0) {
            // Handle both genre_ids (array of integers) and genres (array of objects with id)
            let itemGenres = item.genre_ids || [];
            if (!itemGenres.length && item.genres) {
                // If genres is an array of objects like [{id: 28, name: "Action"}]
                itemGenres = item.genres.map(g => typeof g === 'object' ? g.id : g);
            }
            
            const hasExcludedGenre = filters.excludedGenres.some(genreId => 
                itemGenres.includes(parseInt(genreId))
            );
            if (hasExcludedGenre) return false;
        }
        
        // Runtime filter (if available)
        if ((filters.runtimeMin > 0 || filters.runtimeMax < 300) && item.runtime) {
            if (filters.runtimeMin && item.runtime < filters.runtimeMin) return false;
            if (filters.runtimeMax && item.runtime > filters.runtimeMax) return false;
        }
        
        // Language filter (include) - only apply if item has language data
        if (filters.selectedLanguages && filters.selectedLanguages.length > 0) {
            const itemLang = item.original_language;
            // Only filter if item has language data
            if (itemLang && !filters.selectedLanguages.includes(itemLang)) return false;
        }
        
        // Language filter (exclude) - only apply if item has language data
        if (filters.excludedLanguages && filters.excludedLanguages.length > 0) {
            const itemLang = item.original_language;
            // Only filter if item has language data
            if (itemLang && filters.excludedLanguages.includes(itemLang)) return false;
        }
        
        // Country filter (include) - only apply if item has country data
        if (filters.selectedCountries && filters.selectedCountries.length > 0) {
            const itemCountries = item.origin_country || [];
            // Only filter if item has country data
            if (itemCountries.length > 0) {
                const hasCountry = filters.selectedCountries.some(countryCode => 
                    itemCountries.includes(countryCode)
                );
                if (!hasCountry) return false;
            }
        }
        
        // Country filter (exclude) - only apply if item has country data
        if (filters.excludedCountries && filters.excludedCountries.length > 0) {
            const itemCountries = item.origin_country || [];
            // Only filter if item has country data
            if (itemCountries.length > 0) {
                const hasExcludedCountry = filters.excludedCountries.some(countryCode => 
                    itemCountries.includes(countryCode)
                );
                if (hasExcludedCountry) return false;
            }
        }
        
        // Company filter (include) - only apply if item has company data
        if (filters.selectedCompanies && filters.selectedCompanies.length > 0) {
            const itemCompanies = item.company_ids || [];
            // Only filter if item has company data
            if (itemCompanies.length > 0) {
                const hasCompany = filters.selectedCompanies.some(companyId => 
                    itemCompanies.includes(parseInt(companyId))
                );
                if (!hasCompany) return false;
            }
        }
        
        // Company filter (exclude) - only apply if item has company data
        if (filters.excludedCompanies && filters.excludedCompanies.length > 0) {
            const itemCompanies = item.company_ids || [];
            // Only filter if item has company data
            if (itemCompanies.length > 0) {
                const hasExcludedCompany = filters.excludedCompanies.some(companyId => 
                    itemCompanies.includes(parseInt(companyId))
                );
                if (hasExcludedCompany) return false;
            }
        }
        
        return true;
    });

    return filtered;
}

// Initialize sidebar lists filter when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    // Initialize after other components are ready
    setTimeout(initSidebarListsFilter, 300);
});

/**
 * Show loading message overlay
 */
function showLoadingMessage(message) {
    let overlay = document.getElementById('discoverLoadingOverlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'discoverLoadingOverlay';
        overlay.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.8);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 10000;
            color: white;
            font-size: 18px;
            font-weight: 500;
        `;
        document.body.appendChild(overlay);
    }
    overlay.textContent = message || 'Loading...';
    overlay.style.display = 'flex';
}

/**
 * Hide loading message overlay
 */
function hideLoadingMessage() {
    const overlay = document.getElementById('discoverLoadingOverlay');
    if (overlay) {
        overlay.style.display = 'none';
    }
}

// ============================================
// Version Modal and Request/Scrape Functionality
// ============================================

let availableVersions = [];
let selectedContent = null;
let scrapeContent = null;

// Fetch available versions from backend
async function fetchVersions() {
    try {
        const response = await fetch('/content/versions');
        const data = await response.json();
        if (data.versions) {
            availableVersions = data.versions;
        }
    } catch (error) {
        console.error('Error fetching versions:', error);
    }
}

// Show scrape version modal
function showScrapeVersionModal(content) {
    scrapeContent = content;
    const modal = document.getElementById('scrapeVersionModal');
    const versionRadios = document.getElementById('scrapeVersionRadios');

    versionRadios.innerHTML = '';

    availableVersions.forEach((version, index) => {
        const div = document.createElement('div');
        div.className = 'version-checkbox';
        div.innerHTML = `
            <input type="radio" id="scrape-version-${version}" name="scrape-versions" value="${version}" ${index === 0 ? 'checked' : ''}>
            <label for="scrape-version-${version}">${version}</label>
        `;
        versionRadios.appendChild(div);
    });

    // Add 'No Version' option
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

// Show version selection modal for requests
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
            <input type="checkbox" id="request-version-${version}" name="versions" value="${version}">
            <label for="request-version-${version}">${version}</label>
        `;
        versionCheckboxes.appendChild(div);

        // If there's only one version available, auto-select it
        if (availableVersions.length === 1) {
            div.querySelector('input[type="checkbox"]').checked = true;
        }
    });

    document.body.classList.add('modal-open');
    modal.style.display = 'flex';
}

// Function to fetch show seasons from the server
async function fetchShowSeasons(tmdbId) {
    try {
        console.log(`Fetching seasons for TMDB ID: ${tmdbId}`);
        const response = await fetch(`/content/show_seasons?tmdb_id=${tmdbId}`, {
            method: 'GET'
        });

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

// Handle scrape version confirmation
async function handleScrapeVersionConfirm() {
    const selectedVersion = document.querySelector('#scrapeVersionRadios input[name="scrape-versions"]:checked')?.value;
    if (selectedVersion === undefined) {
        alert('Please select a version.');
        return;
    }

    closeScrapeVersionModal();

    // Navigate to appropriate page
    if (scrapeContent.mediaType === 'movie') {
        // For movies, redirect to scraper page
        window.location.href = `/scraper?media_id=${scrapeContent.id}&media_type=movie&version=${selectedVersion}&skip_cache_check=true`;
    } else {
        // For TV shows, redirect to addmedia page
        const params = new URLSearchParams({
            id: scrapeContent.id,
            title: scrapeContent.title,
            year: scrapeContent.year,
            type: 'tv',
            rating: scrapeContent.rating || 0,
            vote_average: scrapeContent.rating || 0,
            genres: JSON.stringify(scrapeContent.genres || []),
            backdrop: scrapeContent.backdrop || '',
            overview: scrapeContent.overview || ''
        });
        window.location.href = `/discover/addmedia?${params.toString()}`;
    }
}

// Handle version confirm for requests
async function handleVersionConfirm() {
    const versionCheckboxes = document.querySelectorAll('#versionCheckboxes input[name="versions"]:checked');
    const selectedVersions = Array.from(versionCheckboxes).map(cb => cb.value);

    if (selectedVersions.length === 0) {
        alert('Please select at least one version');
        return;
    }

    // Check if this is a TV show
    if (selectedContent.mediaType === 'tv') {
        // Check if the whole-show radio button exists
        const wholeShowRadio = document.querySelector('#whole-show');

        // If the radio buttons exist, process the selection
        if (wholeShowRadio) {
            const wholeShowSelected = wholeShowRadio.checked;

            if (!wholeShowSelected) {
                // Get selected seasons
                const seasonCheckboxes = document.querySelectorAll('#versionCheckboxes input[name="seasons"]:checked');
                const selectedSeasons = Array.from(seasonCheckboxes).map(cb => parseInt(cb.value));

                if (selectedSeasons.length === 0) {
                    alert('Please select at least one season or choose "Whole Show"');
                    return;
                }

                // Add seasons to selectedContent
                selectedContent.seasons = selectedSeasons;
            }
        }
    }

    closeVersionModal();
    await requestContent(selectedContent, selectedVersions);
}

// Request content from backend
async function requestContent(content, selectedVersions) {
    showLoadingMessage('Requesting content, please wait...');
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
        hideLoadingMessage();

        if (result.success) {
            alert(`Successfully requested ${content.title}`);
        } else {
            alert(result.error || 'Failed to request content');
        }
    } catch (error) {
        hideLoadingMessage();
        console.error('Error requesting content:', error);
        alert('An error occurred while requesting content');
    }
}

// Close scrape version modal
function closeScrapeVersionModal() {
    document.getElementById('scrapeVersionModal').style.display = 'none';
    document.body.classList.remove('modal-open');
}

// Close version modal
function closeVersionModal() {
    document.getElementById('versionModal').style.display = 'none';
    document.body.classList.remove('modal-open');
}

// Initialize modal event listeners
function initializeModalListeners() {
    const confirmScrapeVersion = document.getElementById('confirmScrapeVersion');
    if (confirmScrapeVersion) {
        confirmScrapeVersion.addEventListener('click', handleScrapeVersionConfirm);
    }

    const cancelScrapeVersion = document.getElementById('cancelScrapeVersion');
    if (cancelScrapeVersion) {
        cancelScrapeVersion.addEventListener('click', closeScrapeVersionModal);
    }

    const confirmVersions = document.getElementById('confirmVersions');
    if (confirmVersions) {
        confirmVersions.addEventListener('click', handleVersionConfirm);
    }

    const cancelVersions = document.getElementById('cancelVersions');
    if (cancelVersions) {
        cancelVersions.addEventListener('click', closeVersionModal);
    }

    // ESC key to close modals
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            const scrapeModal = document.getElementById('scrapeVersionModal');
            const versionModal = document.getElementById('versionModal');

            if (scrapeModal && scrapeModal.style.display === 'flex') {
                closeScrapeVersionModal();
            }
            if (versionModal && versionModal.style.display === 'flex') {
                closeVersionModal();
            }
        }
    });
}

// Fetch versions and initialize modals on page load
fetchVersions();
initializeModalListeners();

