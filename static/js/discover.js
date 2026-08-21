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
    sortBy: 'none',
    sortOrder: 'desc',
    page: 1,
    hasMore: true,
    isLoading: false,
    listModeActive: false, // true when list content (MDBList/FlixPatrol/Personal/sidebar) is displayed
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
        sortBy: 'none',
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
        networkCache: {},  // Cache network names by ID for display
        selectedCompanies: [],
        excludedCompanies: [],
        companyCache: {},  // Cache company names by ID for display
        selectedKeywords: [],
        excludedKeywords: [],
        keywordCache: {},  // Cache keyword names by ID for display
        titleFilter: '',  // Client-side title filter (supports text or regex)
        runtimeMin: 0,
        runtimeMax: 300,
        seasonsMax: 0,

        // Active filters for UI
        activeFilters: []
    },
    // Discover settings (loaded from global settings)
    discoverSettings: {
        hide_no_rating: false,
        hide_no_poster: false,
        only_show_missing: false,
        hide_specials: true
    }
};

// DOM elements
let searchInput, searchClearBtn, filterToggleBtn, filterDrawer, filterOverlay, filterCloseBtn;
let tabButtons, resultsGrid, loadingState, emptyState, errorState, pagination;
let ratingSlider, ratingDisplay, yearFromInput, yearToInput, genresContainer;
let loadMoreBtn;
const _listKeywordCache = {}; // tmdbId_mediaType -> array of keyword IDs
let _filterFetchController = null; // AbortController for in-flight filter fetches
// Category grids for trending rows
let trendingContent, searchResults, moviesGrid, showsGrid, animeGrid;
// Recommendations
let recommendationsContent, recMoviesGrid, recShowsGrid;

/**
 * Save current filter state to localStorage
 */
function saveFiltersToStorage() {
    // Don't persist state when editing an adaptive list — it would overwrite the user's saved session
    if (window.adaptiveListEditMode) return;
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
            seasonsMax: state.filters.seasonsMax,
            certificationMin: state.filters.certificationMin,
            certificationMax: state.filters.certificationMax,
            includeVideo: state.filters.includeVideo,

            // Cache keyword/company/network names (needed for chip display)
            keywordCache: state.filters.keywordCache || {},
            companyCache: state.filters.companyCache || {},
            networkCache: state.filters.networkCache || {}
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
    localStorage.removeItem('discoverPersonal');
}

/**
 * Save sidebar lists selection to localStorage
 */
function saveSidebarListsToStorage() {
    // Don't persist state when editing an adaptive list — it would overwrite the user's saved session
    if (window.adaptiveListEditMode) return;
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
            tv_show_episode_view: discoverSettings.tv_show_episode_view || 'discover',
            hide_specials: discoverSettings.hide_specials !== false
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
            savedFilters.seasonsMax > 0 ||
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
        // Restore persisted search term
        const savedSearchTerm = localStorage.getItem('discoverSearchTerm');
        if (savedSearchTerm) {
            searchInput.value = savedSearchTerm;
            handleSearch();
            return;
        }

   // Check for saved FlixPatrol, MDBList, sidebar list, or personal list selections
        const savedFlixPatrol = localStorage.getItem('discoverFlixPatrol');
        const savedMDBList = localStorage.getItem('discoverMDBList');
        const savedSidebarLists = localStorage.getItem('discoverSidebarLists');
        const savedPersonal = localStorage.getItem('discoverPersonal');
        const hasSavedSidebarLists = savedSidebarLists && JSON.parse(savedSidebarLists).length > 0;

        if (savedFlixPatrol || savedMDBList || savedPersonal || hasRestoredFilters || hasSavedSidebarLists || isAdaptiveListEntry) {
            // Hide trending, show search results area immediately
            const trendingContent = document.getElementById('trending-content');
            const searchResults = document.getElementById('search-results');
            if (trendingContent) trendingContent.style.display = 'none';
            if (searchResults) searchResults.style.display = 'block';
        } else {
            // Only load trending if nothing is saved
            loadTrending();
        }
        // Always sync tab active state after init, regardless of which path was taken
        updateTabState();
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
    state.seasonsMaxInput = document.getElementById('seasons-max');

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
    // Recommendations
    recommendationsContent = document.getElementById('recommendations-content');
    recMoviesGrid = document.getElementById('rec-movies-grid');
    recShowsGrid  = document.getElementById('rec-shows-grid');

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
            if (this.classList.contains('flixpatrol-dropdown-trigger') || this.classList.contains('mdblist-dropdown-trigger')) return;
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

    if (state.seasonsMaxInput) {
        state.seasonsMaxInput.addEventListener('input', function() {
            state.filters.seasonsMax = parseInt(this.value) || 0;
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

    // Merge with adaptive discover checkbox — re-runs the list load (capped at 5
    // pages of TMDB Discover, same as the scheduled task) so the preview updates.
    const mergeWithAdaptiveCheckbox = document.getElementById('merge-with-adaptive');
    if (mergeWithAdaptiveCheckbox) {
        mergeWithAdaptiveCheckbox.addEventListener('change', (e) => {
            state.filters.mergeWithAdaptive = e.target.checked;
            const listsState = window.sidebarListsState;
            if (listsState && listsState.selectedLists && listsState.selectedLists.length > 0) {
                loadAllSelectedLists();
            }
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
            loadProviders(e.target.value);
            updateActiveFilters();
        });
        // Load certifications for initial region on page load
        loadCertifications(watchRegionSelect.value);
    }

    // Initialize all chips input containers with include/exclude support.
    // Pass getter functions so the handlers always read the live arrays from state.filters
    // even after clearAllFilters() replaces the entire state.filters object.
    initializeChipsInput('genres',
        () => window.discoverState.filters.selectedGenres,
        () => window.discoverState.filters.excludedGenres,
        () => updateActiveFilters());
    initializeChipsInput('language',
        () => window.discoverState.filters.selectedLanguages,
        () => window.discoverState.filters.excludedLanguages,
        () => updateActiveFilters());
    initializeChipsInput('country',
        () => window.discoverState.filters.selectedCountries,
        () => window.discoverState.filters.excludedCountries,
        () => updateActiveFilters());
    initializeChipsInput('provider',
        () => window.discoverState.filters.selectedProviders,
        () => window.discoverState.filters.excludedProviders,
        () => updateActiveFilters());
    loadProviders(state.filters.watchRegion || 'US');
    initializeNetworkFilter();

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

    // Re-render chips and re-apply filters (needed for list mode client-side keyword filtering)
    renderKeywordChips(chipsWrapper);
    updateActiveFilters();
    if (window.discoverState.listModeActive) {
        applyAdvancedFilters(false);
    }
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
            applyAdvancedFilters(false);
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
            applyAdvancedFilters(false);
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
function initializeChipsInput(name, includeArrayOrGetter, excludeArrayOrGetter, onChange) {
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

    // Support both direct array refs (legacy) and getter functions.
    // Getter functions are preferred: they always return the live array from state.filters
    // even after clearAllFilters() replaces the entire state.filters object.
    const getInclude = typeof includeArrayOrGetter === 'function'
        ? includeArrayOrGetter
        : () => includeArrayOrGetter;
    const getExclude = typeof excludeArrayOrGetter === 'function'
        ? excludeArrayOrGetter
        : () => excludeArrayOrGetter;

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

        // Always read the live arrays via getters so stale closure refs are never used
        const includeArray = getInclude();
        const excludeArray = getExclude();

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

        const existingLabel = item.querySelector('.chips-item-label');
        const labelHtml = existingLabel ? existingLabel.outerHTML : `<span class="chips-item-label">${item.textContent.trim()}</span>`;
        item.innerHTML = `
            ${labelHtml}
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
        localStorage.removeItem('discoverSearchTerm');
        loadTrending();
        return;
    }

    localStorage.setItem('discoverSearchTerm', query);

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
    localStorage.removeItem('discoverSearchTerm');
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

function clearSeasonsMaxFilter() {
    const state = window.discoverState;
    state.filters.seasonsMax = 0;
    if (state.seasonsMaxInput) state.seasonsMaxInput.value = '';
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
    // Close the filter drawer first
    if (typeof closeFilters === 'function') closeFilters();
    // Delegate to the full clear implementation
    clearFiltersAndReload();
}

/**
 * Apply advanced filters
 * @param {boolean} closeDrawer - Whether to close the filter drawer (default: true)
 */
function applyAdvancedFilters(closeDrawer = true) {
    const state = window.discoverState;
    const listsState = window.sidebarListsState;
    const currentTab = state.currentTab;

    // In adaptive list edit/create mode: only allow client-side list re-filtering, no server fetches.
    if (window.adaptiveListEditMode) {
        if (state.listModeActive && listsState.rawResults && listsState.rawResults.length > 0) {
            filterAndRenderListResults();
        }
        if (closeDrawer) closeFilters();
        return;
    }

    // For tabs that show list content, re-fetch (respects mediaType) then client-side filter.
    // MDBList and FlixPatrol re-call their loaders so mediaType is sent to the API.
    if (currentTab === 'mdblist' && window.mdblistState && window.mdblistState.currentList) {
        if (trendingContent) trendingContent.style.display = 'none';
        if (searchResults) searchResults.style.display = 'block';
        if (closeDrawer && typeof closeFilters === 'function') closeFilters();
        loadMDBListContent(window.mdblistState.currentList.key);
        return;
    }
    if (currentTab === 'flixpatrol' && window.flixpatrolState && window.flixpatrolState.currentPlatform) {
        if (trendingContent) trendingContent.style.display = 'none';
        if (searchResults) searchResults.style.display = 'block';
        if (closeDrawer && typeof closeFilters === 'function') closeFilters();
        loadFlixPatrolContent(window.flixpatrolState.currentPlatform.id, window.flixpatrolState.currentPeriod || 'today');
        return;
    }
    if (currentTab === 'personal' && window.personalState && window.personalState.currentSelection) {
        if (trendingContent) trendingContent.style.display = 'none';
        if (searchResults) searchResults.style.display = 'block';
        if (closeDrawer && typeof closeFilters === 'function') closeFilters();
        loadPersonalContent();
        return;
    }

    // If a preset is active and hasn't been modified yet, mark it dirty now.
    // activePresetId is null during loadFilterPreset's own applyAdvancedFilters call
    // (it's set to null at the top of loadFilterPreset), so this only triggers for
    // subsequent user-initiated filter changes.
    if (activePresetId && !presetDirty) {
        presetDirty = true;
        updatePresetButtonState();
    }

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

    // If sidebar lists are selected, never fall through to the TMDB discover API path.
    // • rawResults populated → re-filter client-side now
    // • rawResults empty (fetch in-flight) → bail; loadAllSelectedLists will apply current
    //   filters via filterAndRenderListResults() once data arrives
    if (listsState.selectedLists && listsState.selectedLists.length > 0) {
        if (listsState.rawResults.length > 0) {
            filterAndRenderListResults();
        }
        return;  // Don't proceed to TMDB API search
    }

    runDiscoverFilterQuery();
}

/**
 * Build TMDB discover params from the current filter state and fetch results.
 * This is the generic (non-list-mode, non-tab-specific) query path shared by
 * applyAdvancedFilters() and the adaptive-list edit one-time initial load
 * (see loadAdaptiveListForEdit) — the latter calls this directly to bypass
 * applyAdvancedFilters()'s deliberate no-fetch-while-editing guard, since an
 * initial load is a one-time fetch, not a live-tweak reaction.
 */
/**
 * Build the full /discover/api/filter query params from the current advanced-filter
 * state. Single source of truth for every field TMDB discover supports in this app —
 * reused by both the normal Discover browsing path (runDiscoverFilterQuery) and the
 * "merge with adaptive list" preview (loadAllSelectedLists), so the two can never
 * drift apart by one hand-picking a subset of fields the other includes.
 */
function buildDiscoverFilterParams(page) {
    const state = window.discoverState;

    // Build sort_by parameter - dropdown values already include order (e.g., "popularity.desc")
    // If sortBy already has the order, use it directly; otherwise combine with sortOrder
    // 'none' is only meaningful for list mode; fall back to popularity for TMDB API
    let sortByValue = (state.filters.sortBy === 'none') ? 'popularity' : state.filters.sortBy;
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

    return new URLSearchParams({
        type: mediaType,
        page: page,
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
        // Seasons max (TV only, client-side filter)
        seasons_max: state.filters.seasonsMax > 0 ? state.filters.seasonsMax : '',
        // Certification (range with gte/lte)
        'certification.gte': state.filters.certificationMin || '',
        'certification.lte': state.filters.certificationMax || '',
        certification_country: state.filters.watchRegion || 'US',
        // Production Company (include and exclude)
        production_company: state.filters.selectedCompanies.join(','),
        production_company_exclude: state.filters.excludedCompanies.join(',')
    });
}

function runDiscoverFilterQuery() {
    const state = window.discoverState;
    const params = buildDiscoverFilterParams(state.page);

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
    window.discoverState.listModeActive = false;

    // Cancel any in-flight filter request before starting a new one
    if (_filterFetchController) {
        _filterFetchController.abort();
    }
    _filterFetchController = new AbortController();
    const signal = _filterFetchController.signal;

    try {
        setLoadingFlag();

        const response = await fetch(`/discover/api/filter?${params}`, { signal });
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
        if (error.name === 'AbortError') return; // superseded by a newer request
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
    // Don't override if list mode is active (sidebar lists take priority)
    if (window.discoverState.listModeActive) return;

    // Show tab content regardless, even if already loaded
    if (trendingContent) trendingContent.style.display = 'block';
    if (searchResults) searchResults.style.display = 'none';
    if (recommendationsContent) recommendationsContent.style.display = 'none';
    window.discoverState.currentTab = 'trending';
    updateTabState();

    if (_trendingLoaded) return;

    try {
        setLoadingFlag();

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
        _trendingLoaded = true;

    } catch (error) {
        console.error('[Discover] Trending error:', error);
        showError();
    } finally {
        clearLoadingFlag();
    }
}

/**
 * Load personalised recommendations from the best connected provider.
 * The backend prefers Trakt and automatically falls back to Scrob For You.
 */
let _trendingLoaded = false; // Only fetch once per page load
let _recLoaded = false;  // Only fetch once per page load

async function loadRecommendations() {
    if (recommendationsContent) recommendationsContent.style.display = 'block';
    if (trendingContent) trendingContent.style.display = 'none';
    if (searchResults) searchResults.style.display = 'none';
    window.discoverState.currentTab = 'recommendations';
    updateTabState();

    if (_recLoaded) return;

    try {
        setLoadingFlag();

        const noProviderMsg = document.getElementById('rec-no-provider');
        const noResultsMsg = document.getElementById('rec-no-results');

        const resp = await fetch('/discover/api/recommendations?type=all');
        if (!resp.ok) throw new Error('HTTP error fetching recommendations');
        const data = await resp.json();

        if (!data.success) {
            if (noProviderMsg) noProviderMsg.style.display = 'block';
            if (noResultsMsg) noResultsMsg.style.display = 'none';
            if (document.getElementById('rec-movies-section')) document.getElementById('rec-movies-section').style.display = 'none';
            if (document.getElementById('rec-shows-section'))  document.getElementById('rec-shows-section').style.display  = 'none';
            _recLoaded = true;
            return;
        }

        if (noProviderMsg) noProviderMsg.style.display = 'none';

        const all    = filterValidResults(data.results || []);
        if (all.length === 0) {
            if (noResultsMsg) noResultsMsg.style.display = 'block';
            if (document.getElementById('rec-movies-section')) document.getElementById('rec-movies-section').style.display = 'none';
            if (document.getElementById('rec-shows-section')) document.getElementById('rec-shows-section').style.display = 'none';
            _recLoaded = true;
            return;
        }

        if (noResultsMsg) noResultsMsg.style.display = 'none';
        if (document.getElementById('rec-movies-section')) document.getElementById('rec-movies-section').style.display = 'block';
        if (document.getElementById('rec-shows-section')) document.getElementById('rec-shows-section').style.display = 'block';
        const movies = all.filter(i => i.media_type === 'movie');
        const shows  = all.filter(i => i.media_type === 'tv');

        window.discoverState.currentResults = all;

        renderCategoryGrid(recMoviesGrid, movies);
        renderCategoryGrid(recShowsGrid,  shows);

        bindResultEvents();
        _recLoaded = true;

    } catch (error) {
        console.error('[Discover] Recommendations error:', error);
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

    if (state.filters.seasonsMax > 0) {
        activeFilters.push({
            key: 'seasons_max',
            label: `Max Seasons: ${state.filters.seasonsMax}`,
            removeFn: () => clearSeasonsMaxFilter()
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
            .map(networkId => state.filters.networkCache?.[networkId] || networkId);
        activeFilters.push({
            key: 'networks',
            label: `Networks: ${networkNames.join(', ')}`,
            removeFn: () => { state.filters.selectedNetworks = []; renderNetworkChipsFromState(); updateActiveFilters(); }
        });
    }

    // Networks (excluded) - TV only
    if (state.filters.excludedNetworks.length > 0) {
        const networkNames = state.filters.excludedNetworks
            .map(networkId => state.filters.networkCache?.[networkId] || networkId);
        activeFilters.push({
            key: 'excluded_networks',
            label: `Excluded Networks: ${networkNames.join(', ')}`,
            removeFn: () => { state.filters.excludedNetworks = []; renderNetworkChipsFromState(); updateActiveFilters(); }
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

    // In adaptive list edit/create mode, skip entirely — applyAdvancedFilters handles list re-filtering directly
    if (window.adaptiveListEditMode) return;

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
        f.seasonsMax > 0 ||
        f.certificationMin ||
        f.certificationMax;

    // Clear any existing timeout
    if (window.liveFilterTimeout) {
        clearTimeout(window.liveFilterTimeout);
    }

    // If lists are selected, never fall through to the TMDB discover API path.
    // • rawResults already populated → re-filter client-side immediately
    // • rawResults empty (list fetch in-flight) → bail out; loadAllSelectedLists will call
    //   filterAndRenderListResults() once the data arrives, picking up current filter state.
    if (listsState && listsState.selectedLists && listsState.selectedLists.length > 0) {
        if (listsState.rawResults.length > 0) {
            // Debounce the list filtering (500ms delay)
            window.liveFilterTimeout = setTimeout(() => {
                filterAndRenderListResults();
            }, 500);
        }
        // Either way, do not proceed to TMDB API search
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
    // Clear list mode when switching to non-list tabs so TMDB pagination works
    if (tab === 'trending' || tab === 'recommendations') {
        window.discoverState.listModeActive = false;
        // Clear personal/list selections so refresh stays on this tab
        localStorage.removeItem('discoverPersonal');
        localStorage.removeItem('discoverMDBList');
        localStorage.removeItem('discoverFlixPatrol');
        if (window.personalState) { window.personalState.currentSelection = null; }
    }

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

    // Show/hide content sections
    if (trendingContent) trendingContent.style.display = tab === 'trending' ? 'block' : 'none';
    if (recommendationsContent) recommendationsContent.style.display = tab === 'recommendations' ? 'block' : 'none';

    if (tab === 'search' && window.discoverState.searchTerm) {
        searchContent(window.discoverState.searchTerm);
    } else if (tab === 'trending') {
        loadTrending();
    } else if (tab === 'recommendations') {
        loadRecommendations();
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
    // If clearing while in adaptive list edit mode, exit edit mode and clean the URL
    if (window.adaptiveListEditMode) {
        window.adaptiveListEditMode = null;
        if (window.history.replaceState) {
            window.history.replaceState({}, document.title, window.location.pathname);
        }
    }

    // Clear active preset tracking
    activePresetId = null;
    activePresetName = null;
    presetDirty = false;
    updatePresetButtonState();
    const presetSelectEl = document.getElementById('preset-select');
    if (presetSelectEl) presetSelectEl.value = '';
    if (typeof saveActivePresetToStorage === 'function') saveActivePresetToStorage();

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
        networkCache: {},
        selectedCompanies: [],
        excludedCompanies: [],
        companyCache: {},
        selectedKeywords: [],
        excludedKeywords: [],
        keywordCache: {},
        runtimeMin: 0,
        runtimeMax: 300,
        seasonsMax: 0,
        titleFilter: '',
        activeFilters: []
    };

    // Clear sidebar lists selection and list mode flag
    state.listModeActive = false;
    if (window.sidebarListsState) {
        window.sidebarListsState.selectedLists = [];
        window.sidebarListsState.rawResults = [];
        if (typeof clearSavedSidebarLists === 'function') clearSavedSidebarLists();
        renderSidebarListChips();
        const listsDropdown = document.getElementById('lists-dropdown');
        if (listsDropdown) {
            listsDropdown.querySelectorAll('.included').forEach(item => item.classList.remove('included'));
        }
        updateFilterAvailability();
    }

    // Clear keyword ID cache
    Object.keys(_listKeywordCache).forEach(k => delete _listKeywordCache[k]);

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
    if (state.sortByDropdown) state.sortByDropdown.value = 'none';

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
    if (state.tmdbRatingMinInput) { state.tmdbRatingMinInput.value = '0'; state.tmdbRatingMinInput.dispatchEvent(new Event('input')); }
    if (state.tmdbRatingMaxInput) { state.tmdbRatingMaxInput.value = '10'; state.tmdbRatingMaxInput.dispatchEvent(new Event('input')); }
    if (state.tmdbVotesMinInput) { state.tmdbVotesMinInput.value = '0'; state.tmdbVotesMinInput.dispatchEvent(new Event('input')); }
    if (state.imdbRatingMinInput) { state.imdbRatingMinInput.value = '0'; state.imdbRatingMinInput.dispatchEvent(new Event('input')); }
    if (state.imdbRatingMaxInput) { state.imdbRatingMaxInput.value = '10'; state.imdbRatingMaxInput.dispatchEvent(new Event('input')); }
    if (state.imdbVotesMinInput) { state.imdbVotesMinInput.value = '0'; state.imdbVotesMinInput.dispatchEvent(new Event('input')); }
    if (state.runtimeMinInput) state.runtimeMinInput.value = '0';
    if (state.runtimeMaxInput) state.runtimeMaxInput.value = '300';
    if (state.seasonsMaxInput) state.seasonsMaxInput.value = '';

    // Reset certification selects
    const certMinSel = document.getElementById('certification-min-select');
    if (certMinSel) certMinSel.value = '';
    const certMaxSel = document.getElementById('certification-max-select');
    if (certMaxSel) certMaxSel.value = '';

    // Reset title filter
    const titleFilterEl = document.getElementById('title-filter');
    if (titleFilterEl) titleFilterEl.value = '';
    state.filters.titleFilter = '';

    // Reset company and network search inputs
    const companySrch = document.getElementById('company-search');
    if (companySrch) companySrch.value = '';
    const networkSrch = document.getElementById('network-search');
    if (networkSrch) networkSrch.value = '';

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
    if (state.seasonsMaxInput && f.seasonsMax) state.seasonsMaxInput.value = f.seasonsMax;

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
            renderNetworkChipsFromState();
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
        f.seasonsMax > 0 ||
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
    if (!resultsGrid) resultsGrid = document.getElementById('results-grid');
    if (!resultsGrid) return;

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
    const posterUrl = item.poster_path
        ? (item.poster_path.startsWith('http') ? item.poster_path : `https://image.tmdb.org/t/p/w342${item.poster_path}`)
        : '/static/images/placeholder.png';
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
                <!-- Action buttons row -->
                <div class="hover-action-bar" style="display:flex!important;flex-direction:row!important;flex-wrap:nowrap!important;gap:6px;align-items:center;justify-content:center;width:100%;position:absolute;bottom:8px;left:0;padding:0 8px;box-sizing:border-box;z-index:10">
                    <button class="hover-action-btn request-icon" title="Request this content" style="width:32px;height:32px;min-width:32px;min-height:32px;border-radius:8px;border:1px solid rgba(74,222,128,0.35);background:rgba(10,10,10,0.7);color:#4ade80;display:flex;align-items:center;justify-content:center;padding:0;cursor:pointer;flex-shrink:0;box-sizing:border-box;transition:transform 0.15s ease,box-shadow 0.15s ease,background 0.15s ease">
                        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" style="display:block;flex-shrink:0;pointer-events:none"><path d="M10 1H6V6L1 6V10H6V15H10V10H15V6L10 6V1Z" fill="currentColor"></path></svg>
                    </button>
                    ${hasAdminPermissions ? `<button class="hover-action-btn tester-icon" title="Test this content" style="width:32px;height:32px;min-width:32px;min-height:32px;border-radius:8px;border:1px solid rgba(251,191,36,0.35);background:rgba(10,10,10,0.7);color:#fbbf24;display:flex;align-items:center;justify-content:center;padding:0;cursor:pointer;flex-shrink:0;box-sizing:border-box;transition:transform 0.15s ease,box-shadow 0.15s ease,background 0.15s ease">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" style="display:block;flex-shrink:0;pointer-events:none"><path d="M9.74872 2.49415L18.1594 7.31987M9.74872 2.49415L8.91283 2M9.74872 2.49415L6.19982 8.61981M18.1594 7.31987L15.902 11.2163M18.1594 7.31987L19 7.80374M15.902 11.2163L14.1886 14.1738M15.902 11.2163L13.344 9.74451M14.1886 14.1738L12.5511 17.0003M14.1886 14.1738L9.98568 11.7556M12.5511 17.0003L11.0558 19.5813C9.7158 21.8942 6.74803 22.6867 4.42709 21.3513C2.10615 20.0159 1.31093 17.0584 2.65093 14.7455L3.95184 12.5M12.5511 17.0003L9.93838 15.4971" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"></path><path d="M22 14.9166C22 16.0672 21.1046 16.9999 20 16.9999C18.8954 16.9999 18 16.0672 18 14.9166C18 14.1967 18.783 13.2358 19.3691 12.6174C19.7161 12.2512 20.2839 12.2512 20.6309 12.6174C21.217 13.2358 22 14.1967 22 14.9166Z" stroke="currentColor" stroke-width="1.5"></path></svg>
                    </button>` : ''}
                    <button class="hover-action-btn magnet-assign-icon" title="Assign magnet" style="width:32px;height:32px;min-width:32px;min-height:32px;border-radius:8px;border:1px solid rgba(192,132,252,0.35);background:rgba(10,10,10,0.7);color:#c084fc;display:flex;align-items:center;justify-content:center;padding:0;cursor:pointer;flex-shrink:0;box-sizing:border-box;transition:transform 0.15s ease,box-shadow 0.15s ease,background 0.15s ease">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" style="display:block;flex-shrink:0;pointer-events:none;transform:rotate(270deg)"><path d="M21 18.5V20.5C21 21.3284 20.3284 22 19.5 22H17H13C7.47715 22 3 17.5228 3 12C3 6.47715 7.47715 2 13 2H17H19.5C20.3284 2 21 2.67157 21 3.5V5.5C21 6.32843 20.3284 7 19.5 7H17H13C10.2386 7 8 9.23858 8 12C8 14.7614 10.2386 17 13 17H17H19.5C20.3284 17 21 17.6716 21 18.5Z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path><path opacity="0.5" d="M17 2V7M17 17V22" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path></svg>
                    </button>
                    <button class="hover-action-btn blacklist-icon" title="Add to manual blacklist" style="width:32px;height:32px;min-width:32px;min-height:32px;border-radius:8px;border:1px solid rgba(248,113,113,0.35);background:rgba(10,10,10,0.7);color:#f87171;display:flex;align-items:center;justify-content:center;padding:0;cursor:pointer;flex-shrink:0;box-sizing:border-box;transition:transform 0.15s ease,box-shadow 0.15s ease,background 0.15s ease">
                        <svg width="14" height="14" viewBox="0 -0.5 17 17" style="display:block;flex-shrink:0;pointer-events:none"><path d="M9.016,0.06 C4.616,0.06 1.047,3.629 1.047,8.029 C1.047,12.429 4.615,15.998 9.016,15.998 C13.418,15.998 16.985,12.429 16.985,8.029 C16.985,3.629 13.418,0.06 9.016,0.06 L9.016,0.06 Z M3.049,8.028 C3.049,4.739 5.726,2.062 9.016,2.062 C10.37,2.062 11.616,2.52 12.618,3.283 L4.271,11.631 C3.508,10.629 3.049,9.381 3.049,8.028 L3.049,8.028 Z M9.016,13.994 C7.731,13.994 6.544,13.583 5.569,12.889 L13.878,4.58 C14.571,5.555 14.982,6.743 14.982,8.028 C14.981,11.317 12.306,13.994 9.016,13.994 L9.016,13.994 Z" fill="currentColor"></path></svg>
                    </button>
                </div>
            </div>
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

        // Hover scale + inset glow effect on action buttons
        const btnGlowColors = {
            'request-icon':      'rgba(74,222,128,0.45)',
            'tester-icon':       'rgba(251,191,36,0.45)',
            'magnet-assign-icon':'rgba(192,132,252,0.45)',
            'blacklist-icon':    'rgba(248,113,113,0.45)',
        };
        item.querySelectorAll('.hover-action-btn').forEach(btn => {
            const colorClass = Object.keys(btnGlowColors).find(c => btn.classList.contains(c));
            const glow = btnGlowColors[colorClass] || 'rgba(255,255,255,0.2)';
            btn.addEventListener('mouseenter', () => {
                btn.style.transform = 'scale(1.13)';
                btn.style.boxShadow = `inset 0 0 10px ${glow}, 0 0 8px ${glow}`;
                btn.style.background = `rgba(10,10,10,0.85)`;
            });
            btn.addEventListener('mouseleave', () => {
                btn.style.transform = '';
                btn.style.boxShadow = '';
                btn.style.background = 'rgba(10,10,10,0.7)';
            });
        });

        // Add click handlers for action buttons
        const requestIcon = item.querySelector('.request-icon');
        const testerIcon = item.querySelector('.tester-icon');
        const magnetIcon = item.querySelector('.magnet-assign-icon');
        const blacklistIcon = item.querySelector('.blacklist-icon');

        if (requestIcon && resultItem) {
            requestIcon.addEventListener('click', function(e) {
                e.stopPropagation();
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
                e.stopPropagation();
                e.preventDefault();
                const params = new URLSearchParams({
                    title: resultItem.title || resultItem.name || 'Unknown',
                    id: resultItem.id,
                    year: (resultItem.release_date || resultItem.first_air_date || '').substring(0, 4) || '',
                    media_type: type
                });
                window.location.href = `/scraper/scraper_tester?${params.toString()}`;
            });
        }

        if (magnetIcon && resultItem) {
            magnetIcon.addEventListener('click', async function(e) {
                e.stopPropagation();
                e.preventDefault();
                const title = resultItem.title || resultItem.name || 'Unknown';
                const year = (resultItem.release_date || resultItem.first_air_date || '').substring(0, 4) || '';
                let imdbId = resultItem.imdb_id || resultItem.external_ids?.imdb_id || null;
                if (!imdbId) {
                    try {
                        const res = await fetch(`/discover/api/details/${resultItem.id}?type=${type}`);
                        const data = await res.json();
                        imdbId = data.imdb_id || null;
                    } catch(err) {}
                }
                const params = new URLSearchParams({
                    prefill_title: title,
                    prefill_year: year,
                    prefill_type: type === 'movie' ? 'movie' : 'show'
                });
                if (imdbId) params.set('prefill_id', imdbId);
                window.location.href = `/magnet/assign_magnet?${params.toString()}`;
            });
        }

        if (blacklistIcon && resultItem) {
            blacklistIcon.addEventListener('click', async function(e) {
                e.stopPropagation();
                e.preventDefault();
                const title = resultItem.title || resultItem.name || 'Unknown';
                // Try imdb_id on item first, otherwise fetch from details API
                let imdbId = resultItem.imdb_id || resultItem.external_ids?.imdb_id || null;
                if (!imdbId) {
                    try {
                        const detailRes = await fetch(`/discover/api/details/${resultItem.id}?type=${type}`);
                        const detailData = await detailRes.json();
                        imdbId = detailData.imdb_id || null;
                    } catch(err) {}
                }
                if (!imdbId) {
                    showPopup({ type: 'warning', title: 'Required', message: `Cannot blacklist "${title}": no IMDb ID found for this item.` });
                    return;
                }
                showPopup({
                    type: 'confirm',
                    title: 'Confirm Blacklist',
                    message: `Add "${title}" (${imdbId}) to manual blacklist?`,
                    onConfirm: async function() {
                        try {
                            const itemMediaType = resultItem.media_type || type;
                            const fd = new FormData();
                            fd.append('action', 'add');
                            fd.append('imdb_id', imdbId);
                            fd.append('media_type', itemMediaType === 'tv' ? 'episode' : 'movie');
                            const res = await fetch('/debug/manual_blacklist', { method: 'POST', body: fd });
                            if (res.ok) {
                                blacklistIcon.style.color = '#22c55e';
                                blacklistIcon.title = 'Added to blacklist';
                            } else {
                                showPopup({ type: 'error', title: 'Error', message: 'Failed to add to blacklist. Please try again.' });
                            }
                        } catch(err) {
                            showPopup({ type: 'error', title: 'Error', message: 'Error adding to blacklist.' });
                        }
                    }
                });
                return;
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

    // Never paginate via TMDB when list or personal content is showing (fully loaded at once)
    const _listsState = window.sidebarListsState;
    const _onPersonalTab = window.discoverState.currentTab === 'personal';
    const _hasSidebarLists = _listsState && _listsState.selectedLists && _listsState.selectedLists.length > 0;
    if (_hasSidebarLists || _onPersonalTab) {
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
        let sortByValue = (state.filters.sortBy === 'none') ? 'popularity' : state.filters.sortBy;
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
    if (resultsGrid) resultsGrid.classList.add('hidden-state');
    if (emptyState) emptyState.classList.add('active');
    if (errorState) errorState.classList.remove('active');
}

/**
 * Hide empty state
 */
function hideEmpty() {
    if (resultsGrid) resultsGrid.classList.remove('hidden-state');
    if (emptyState) emptyState.classList.remove('active');
}

/**
 * Show error state
 */
function showError() {
    if (resultsGrid) resultsGrid.classList.add('hidden-state');
    if (emptyState) emptyState.classList.remove('active');
    if (errorState) errorState.classList.add('active');
}

/**
 * Hide error state
 */
function hideError() {
    if (errorState) errorState.classList.remove('active');
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
    if (pagination) {
        if (!window.discoverState.hasMore && window.discoverState.page > 1) {
            pagination.style.display = 'flex';
            pagination.innerHTML = '<div class="end-of-results">No more results</div>';
        } else {
            pagination.style.display = 'none';
        }
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
 * Adaptive List state for edit mode — null when not editing, object when active
 */
window.adaptiveListEditMode = null;

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
        // Create new adaptive list mode — set flag first so saves are blocked
        window.adaptiveListEditMode = {
            isEditing: false,
            sourceId: null  // Will be assigned when saved
        };

        // Ensure localStorage is clean — nothing from this session should persist
        clearSavedFilters();

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
            // Set edit mode FIRST so all subsequent saves are blocked
            window.adaptiveListEditMode = {
                isEditing: true,
                sourceId: sourceId
            };

            // Ensure localStorage is clean — nothing from this edit session should persist
            clearSavedFilters();

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
    if (filters.seasons_max) {
        const seasonsMax = document.getElementById('seasons-max');
        if (seasonsMax) seasonsMax.value = filters.seasons_max;
        state.filters.seasonsMax = parseInt(filters.seasons_max);
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
        renderNetworkChipsFromState();
    }

    // Companies
    if (filters.company) {
        const companyIds = filters.company.split(',').filter(v => v);
        state.filters.selectedCompanies = companyIds;
        applyChipsFromSavedFilters('company', companyIds, []);
    }

    // Merge with adaptive discover checkbox — set BEFORE loadSavedLists() below,
    // since that asynchronously triggers loadAllSelectedLists(), which reads this flag.
    if (filters.merge_with_adaptive) {
        const mergeWithAdaptiveCheckbox = document.getElementById('merge-with-adaptive');
        if (mergeWithAdaptiveCheckbox) {
            mergeWithAdaptiveCheckbox.checked = true;
        }
        state.filters.mergeWithAdaptive = true;
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
        // Adaptive-list edit/create mode has a deliberate guard (applyAdvancedFilters)
        // that skips TMDB fetches on every filter tweak while editing, to avoid firing
        // an API call per click. But loading a saved list for editing needs exactly one
        // initial fetch so the results grid isn't blank — list-sourced lists (filters.lists)
        // already get that via loadSavedLists() above, so only pure-filter lists need this.
        if (window.adaptiveListEditMode && !filters.lists) {
            runDiscoverFilterQuery();
        } else {
            applyFilters();
        }
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
        f.runtimeMax < 300 ||
        f.seasonsMax > 0
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
    if (window.adaptiveListEditMode && window.adaptiveListEditMode.isEditing) {
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
    if (state.filters.seasonsMax > 0) {
        filters.seasons_max = state.filters.seasonsMax;
    }

    // Production company
    if (state.filters.selectedCompanies && state.filters.selectedCompanies.length > 0) {
        filters.production_company = state.filters.selectedCompanies.join(',');
    }

    // Lists filter (sidebar lists) - save as array
    if (window.sidebarListsState && window.sidebarListsState.selectedLists && window.sidebarListsState.selectedLists.length > 0) {
        // Save as comma-separated "source:id" pairs
        filters.lists = window.sidebarListsState.selectedLists.map(l => `${l.source}:${l.listId}`).join(',');

        // Merge with adaptive discover — only meaningful alongside a selected list.
        if (state.filters.mergeWithAdaptive) {
            filters.merge_with_adaptive = true;
        }
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

// --- Preset active state tracking ---
let activePresetId = null;
let activePresetName = null;
let presetDirty = false;

const PRESET_STORAGE_KEY = 'discoverActivePreset';

// Get the last non-empty text node from the button (the visible label, not whitespace before SVG)
function getPresetBtnTextNode(btn) {
    const textNodes = [...btn.childNodes].filter(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim() !== '');
    return textNodes[textNodes.length - 1] || null;
}

function saveActivePresetToStorage() {
    if (activePresetId) {
        localStorage.setItem(PRESET_STORAGE_KEY, JSON.stringify({ id: activePresetId, name: activePresetName }));
    } else {
        localStorage.removeItem(PRESET_STORAGE_KEY);
    }
}

function restoreActivePresetFromStorage() {
    try {
        const stored = localStorage.getItem(PRESET_STORAGE_KEY);
        if (!stored) return;
        const { id, name } = JSON.parse(stored);
        if (!id) return;
        // Verify the preset still exists in the dropdown (already populated at this point)
        const presetSelect = document.getElementById('preset-select');
        if (presetSelect && [...presetSelect.options].some(opt => opt.value === id)) {
            activePresetId = id;
            activePresetName = name;
            presetDirty = false;
            presetSelect.value = id;
            const deleteBtn = document.getElementById('delete-preset-btn');
            if (deleteBtn) deleteBtn.disabled = false;
            updatePresetButtonState();
        } else {
            // Preset no longer exists — clear stale storage
            localStorage.removeItem(PRESET_STORAGE_KEY);
        }
    } catch (e) {
        localStorage.removeItem(PRESET_STORAGE_KEY);
    }
}

function updatePresetButtonState() {
    const btn = document.getElementById('save-preset-btn');
    if (!btn) return;
    const textNode = getPresetBtnTextNode(btn);
    if (activePresetId && presetDirty) {
        if (textNode) textNode.textContent = ' Update';
        btn.title = 'Update loaded preset with current filters';
    } else {
        if (textNode) textNode.textContent = ' Preset';
        btn.title = 'Save current filters as a preset';
    }
    saveActivePresetToStorage();
}

async function updateActivePreset() {
    if (!activePresetId) return;
    const filters = buildPresetFiltersObject();
    const btn = document.getElementById('save-preset-btn');
    const textNode = btn ? getPresetBtnTextNode(btn) : null;

    if (btn) btn.disabled = true;
    if (textNode) textNode.textContent = ' Saving...';

    try {
        const response = await fetch(`/discover/api/presets/${activePresetId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filters })
        });
        const data = await response.json();
        if (data.success) {
            presetDirty = false;
            showNotification(`Preset "${activePresetName}" updated!`, 'success');
        } else {
            throw new Error(data.error || 'Failed to update preset');
        }
    } catch (error) {
        showNotification(error.message || 'Failed to update preset', 'error');
    } finally {
        if (btn) btn.disabled = false;
        updatePresetButtonState();
    }
}
// --- End preset active state tracking ---

/**
 * Initialize filter preset functionality
 */
async function initFilterPresets() {
    const savePresetBtn = document.getElementById('save-preset-btn');
    const presetModal = document.getElementById('preset-modal');
    const presetCloseBtn = document.getElementById('preset-modal-close');
    const presetCancelBtn = document.getElementById('preset-cancel-btn');
    const presetSaveBtn = document.getElementById('preset-save-btn');
    const presetSelect = document.getElementById('preset-select');
    const deletePresetBtn = document.getElementById('delete-preset-btn');

    // Open preset save modal, or update the active preset if one is loaded and dirty
    if (savePresetBtn) {
        savePresetBtn.addEventListener('click', function() {
            if (activePresetId && presetDirty) {
                updateActivePreset();
            } else {
                openPresetModal();
            }
        });
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

    // Load presets into dropdown, then restore active preset from localStorage
    await loadPresetsIntoDropdown();
    restoreActivePresetFromStorage();
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
    // Save network names for display on restore
    if (state.filters.networkCache) {
        const networkNames = {};
        const allNetworkIds = [
            ...(state.filters.selectedNetworks || []),
            ...(state.filters.excludedNetworks || [])
        ];
        allNetworkIds.forEach(id => {
            if (state.filters.networkCache[id]) {
                networkNames[id] = state.filters.networkCache[id];
            }
        });
        if (Object.keys(networkNames).length > 0) {
            filters.network_names = networkNames;
        }
    }

    // Runtime
    if (state.filters.runtimeMin) {
        filters.runtime_min = state.filters.runtimeMin;
    }
    if (state.filters.runtimeMax && state.filters.runtimeMax < 300) {
        filters.runtime_max = state.filters.runtimeMax;
    }
    if (state.filters.seasonsMax > 0) {
        filters.seasons_max = state.filters.seasonsMax;
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
    // Reset active preset state immediately so that the applyAdvancedFilters call
    // during loading doesn't trigger dirty marking.
    activePresetId = null;
    presetDirty = false;
    updatePresetButtonState();

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

            // Cancel any live filter timeout triggered by updateActiveFilters above,
            // since we're about to call applyAdvancedFilters directly.
            if (window.liveFilterTimeout) {
                clearTimeout(window.liveFilterTimeout);
                window.liveFilterTimeout = null;
            }

            showNotification(`Preset "${data.preset.name}" loaded!`, 'success');

            // Auto-apply filters to refresh results (keep sidebar open)
            console.log('[Presets] Applying filters with sidebar open - v3');
            applyAdvancedFilters(false);

            // Set active preset state AFTER applyAdvancedFilters so the above call
            // doesn't trigger dirty marking.
            activePresetId = presetId;
            activePresetName = data.preset.name;
            presetDirty = false;
            updatePresetButtonState();
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
    if (filters.seasons_max) {
        const seasonsMax = document.getElementById('seasons-max');
        if (seasonsMax) seasonsMax.value = filters.seasons_max;
        state.filters.seasonsMax = parseInt(filters.seasons_max);
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
        // Restore network names from saved data
        if (filters.network_names) {
            state.filters.networkCache = { ...state.filters.networkCache, ...filters.network_names };
        }
        renderNetworkChipsFromState();
    }
    if (filters.network_exclude) {
        state.filters.excludedNetworks = filters.network_exclude.split(',').filter(v => v);
        // Restore network names from saved data
        if (filters.network_names) {
            state.filters.networkCache = { ...state.filters.networkCache, ...filters.network_names };
        }
        renderNetworkChipsFromState();
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

    // Clear network chips display
    const networkChips = document.getElementById('network-chips');
    if (networkChips) networkChips.innerHTML = '';

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
 * Load streaming providers from TMDB API and populate the provider dropdown
 */
async function loadProviders(region) {
    const state = window.discoverState;
    const dropdown = document.getElementById('provider-dropdown');
    if (!dropdown) return;

    region = region || state.filters.watchRegion || 'US';

    dropdown.innerHTML = '<div class="chips-dropdown-empty">Loading providers...</div>';

    try {
        const response = await fetch(`/discover/api/providers?region=${encodeURIComponent(region)}`);
        if (!response.ok) throw new Error('Failed to load providers');

        const data = await response.json();
        const providers = data.providers || [];

        dropdown.innerHTML = '';
        providers.forEach(provider => {
            const item = document.createElement('div');
            item.className = 'chips-dropdown-item';
            item.setAttribute('data-value', provider.id.toString());

            const logoHtml = provider.logo_path
                ? `<img src="https://image.tmdb.org/t/p/w45${provider.logo_path}" alt="" style="width:28px;height:18px;object-fit:contain;margin-right:6px;flex-shrink:0;border-radius:3px;">`
                : `<span style="width:28px;margin-right:6px;flex-shrink:0;"></span>`;
            item.innerHTML = `<span class="chips-item-label" style="display:flex;align-items:center;">${logoHtml}${provider.name}</span>`;

            if (state.filters.selectedProviders.includes(provider.id.toString())) {
                item.classList.add('included');
            } else if (state.filters.excludedProviders.includes(provider.id.toString())) {
                item.classList.add('excluded');
            }

            dropdown.appendChild(item);
        });

        // Setup +/- buttons on the newly added items
        setupDropdownItemButtons(dropdown);

    } catch (error) {
        console.error('[Discover] Failed to load providers:', error);
        dropdown.innerHTML = '<div class="chips-dropdown-empty">Failed to load providers</div>';
    }
}

/**
 * Initialize network filter with dynamic API search
 */
function initializeNetworkFilter() {
    const container = document.getElementById('network-container');
    if (!container) return;

    const chipsWrapper = container.querySelector('#network-chips');
    const searchInput = container.querySelector('#network-search');
    const dropdown = container.querySelector('#network-dropdown');
    const dropdownToggle = container.querySelector('#network-dropdown-toggle');

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

    // Search networks on input with debounce
    searchInput.addEventListener('input', (e) => {
        const query = e.target.value.trim();

        if (searchTimeout) clearTimeout(searchTimeout);

        if (query.length < 2) {
            dropdown.innerHTML = '<div class="chips-dropdown-empty">Type to search networks...</div>';
            return;
        }

        dropdown.innerHTML = '<div class="chips-dropdown-empty">Searching...</div>';
        dropdown.classList.add('show');
        positionDropdown(dropdown, container);

        searchTimeout = setTimeout(async () => {
            try {
                const response = await fetch(`/discover/api/networks?query=${encodeURIComponent(query)}`);
                if (!response.ok) throw new Error('Search failed');

                const data = await response.json();
                renderNetworkDropdown(data.networks || [], dropdown, chipsWrapper);
                positionDropdown(dropdown, container);
            } catch (error) {
                console.error('[Discover] Network search error:', error);
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
 * Render network search results in dropdown
 */
function renderNetworkDropdown(networks, dropdown, chipsWrapper) {
    const state = window.discoverState;

    if (networks.length === 0) {
        dropdown.innerHTML = '<div class="chips-dropdown-empty">No networks found</div>';
        return;
    }

    dropdown.innerHTML = '';

    networks.forEach(network => {
        const item = document.createElement('div');
        item.className = 'chips-dropdown-item';
        item.setAttribute('data-value', network.id.toString());

        // Check if already selected/excluded
        if (state.filters.selectedNetworks.includes(network.id.toString())) {
            item.classList.add('included');
        } else if (state.filters.excludedNetworks.includes(network.id.toString())) {
            item.classList.add('excluded');
        }

        const logoHtml = network.logo_path
            ? `<img src="https://image.tmdb.org/t/p/w45${network.logo_path}" alt="" class="network-logo" style="width:28px;height:18px;object-fit:contain;margin-right:6px;flex-shrink:0;">`
            : `<span style="width:28px;margin-right:6px;flex-shrink:0;"></span>`;

        item.innerHTML = `
            <span class="chips-item-label" style="display:flex;align-items:center;">${logoHtml}${network.name}</span>
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
            toggleNetwork(network.id.toString(), network.name, 'include', item, chipsWrapper);
        });

        // Exclude button
        item.querySelector('.chips-exclude-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            toggleNetwork(network.id.toString(), network.name, 'exclude', item, chipsWrapper);
        });

        dropdown.appendChild(item);
    });
}

/**
 * Toggle network selection (include/exclude)
 */
function toggleNetwork(networkId, networkName, action, dropdownItem, chipsWrapper) {
    const state = window.discoverState;
    const selectedArray = state.filters.selectedNetworks;
    const excludedArray = state.filters.excludedNetworks;

    // Cache the network name for display
    state.filters.networkCache[networkId] = networkName;

    // Remove from both arrays first
    const selectedIdx = selectedArray.indexOf(networkId);
    const excludedIdx = excludedArray.indexOf(networkId);
    if (selectedIdx > -1) selectedArray.splice(selectedIdx, 1);
    if (excludedIdx > -1) excludedArray.splice(excludedIdx, 1);

    // Update dropdown item state
    dropdownItem.classList.remove('included', 'excluded');

    if (action === 'include' && selectedIdx === -1) {
        selectedArray.push(networkId);
        dropdownItem.classList.add('included');
    } else if (action === 'exclude' && excludedIdx === -1) {
        excludedArray.push(networkId);
        dropdownItem.classList.add('excluded');
    }

    // Re-render chips
    renderNetworkChips(chipsWrapper);
    updateActiveFilters();
}

/**
 * Render network chips
 */
function renderNetworkChips(chipsWrapper) {
    const state = window.discoverState;
    chipsWrapper.innerHTML = '';

    // Render included networks
    state.filters.selectedNetworks.forEach(networkId => {
        const name = state.filters.networkCache[networkId] || networkId;
        const chip = document.createElement('span');
        chip.className = 'chip included';
        chip.setAttribute('data-value', networkId);
        chip.innerHTML = `${name} <button type="button" class="chip-remove">&times;</button>`;
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            const idx = state.filters.selectedNetworks.indexOf(networkId);
            if (idx > -1) state.filters.selectedNetworks.splice(idx, 1);
            renderNetworkChips(chipsWrapper);
            updateActiveFilters();
        });
        chipsWrapper.appendChild(chip);
    });

    // Render excluded networks
    state.filters.excludedNetworks.forEach(networkId => {
        const name = state.filters.networkCache[networkId] || networkId;
        const chip = document.createElement('span');
        chip.className = 'chip excluded';
        chip.setAttribute('data-value', networkId);
        chip.innerHTML = `${name} <button type="button" class="chip-remove">&times;</button>`;
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            const idx = state.filters.excludedNetworks.indexOf(networkId);
            if (idx > -1) state.filters.excludedNetworks.splice(idx, 1);
            renderNetworkChips(chipsWrapper);
            updateActiveFilters();
        });
        chipsWrapper.appendChild(chip);
    });
}

/**
 * Render network chips from state after loading preset/filters
 */
function renderNetworkChipsFromState() {
    const state = window.discoverState;
    const chipsWrapper = document.getElementById('network-chips');
    if (!chipsWrapper || !state || !state.filters) return;

    chipsWrapper.innerHTML = '';

    state.filters.selectedNetworks.forEach(networkId => {
        const networkName = state.filters.networkCache?.[networkId] || `Network ${networkId}`;
        const chip = document.createElement('span');
        chip.className = 'chip chip-include';
        chip.innerHTML = `<span class="chip-icon">+</span>${networkName} <button type="button" class="chip-remove">&times;</button>`;
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            const idx = state.filters.selectedNetworks.indexOf(networkId);
            if (idx > -1) state.filters.selectedNetworks.splice(idx, 1);
            renderNetworkChipsFromState();
            updateActiveFilters();
        });
        chipsWrapper.appendChild(chip);
    });

    state.filters.excludedNetworks.forEach(networkId => {
        const networkName = state.filters.networkCache?.[networkId] || `Network ${networkId}`;
        const chip = document.createElement('span');
        chip.className = 'chip chip-exclude';
        chip.innerHTML = `<span class="chip-icon">-</span>${networkName} <button type="button" class="chip-remove">&times;</button>`;
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            const idx = state.filters.excludedNetworks.indexOf(networkId);
            if (idx > -1) state.filters.excludedNetworks.splice(idx, 1);
            renderNetworkChipsFromState();
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

    showPopup({
        type: 'confirm',
        title: 'Confirm Delete',
        message: `Are you sure you want to delete "${presetName}"?`,
        onConfirm: async function() {
            try {
                const response = await fetch(`/discover/api/presets/${presetId}`, {
                    method: 'DELETE'
                });

                const data = await response.json();

                if (data.success) {
                    showNotification(`Preset "${presetName}" deleted!`, 'success');

                    // If the deleted preset was the active one, clear active preset state
                    if (presetId === activePresetId) {
                        activePresetId = null;
                        activePresetName = null;
                        presetDirty = false;
                        updatePresetButtonState();
                    }

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
    });
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
            // Append static TMDB show lists
            const tmdbShowLists = [
                { key: 'tmdb_shows_popular',      name: 'TMDB Popular Shows',      icon: 'tmdb', category: 'tmdb_shows', source: 'tmdb_shows', listType: 'popular' },
                { key: 'tmdb_shows_top_rated',    name: 'TMDB Top Rated Shows',    icon: 'tmdb', category: 'tmdb_shows', source: 'tmdb_shows', listType: 'top_rated' },
                { key: 'tmdb_shows_airing_today', name: 'TMDB Airing Today',       icon: 'tmdb', category: 'tmdb_shows', source: 'tmdb_shows', listType: 'airing_today' },
                { key: 'tmdb_shows_trending',     name: 'TMDB Trending Shows',     icon: 'tmdb', category: 'tmdb_shows', source: 'tmdb_shows', listType: 'trending' },
            ];
            const allLists = [...data.lists, ...tmdbShowLists];
            window.mdblistState.availableLists = allLists;
            populateMDBListDropdown(allLists);

            // Restore saved selection
            const saved = localStorage.getItem('discoverMDBList');
            if (saved) {
                try {
                    const savedList = JSON.parse(saved);
                    const list = allLists.find(l => l.key === savedList.key);
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
        'tmdb_shows': 'TMDB Shows',
        'other': 'Other'
    };

    // Define category order
    const categoryOrder = ['mdblist', 'streaming', 'originals', 'curated', 'tmdb_shows', 'other'];

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

function closeAllDropdowns(except) {
    [
        { btn: 'mdblist-dropdown-btn', menu: 'mdblist-dropdown-menu' },
        { btn: 'personal-dropdown-btn', menu: 'personal-dropdown-menu' },
        { btn: 'flixpatrol-dropdown-btn', menu: 'flixpatrol-dropdown-menu' }
    ].forEach(({ btn, menu }) => {
        const b = document.getElementById(btn);
        const m = document.getElementById(menu);
        if (b && m && b !== except) {
            m.classList.remove('open');
            b.classList.remove('open');
        }
    });
}

/**
 * Bind MDBList dropdown events
 */
function bindMDBListEvents() {
    const dropdownBtn = document.getElementById('mdblist-dropdown-btn');
    const dropdownMenu = document.getElementById('mdblist-dropdown-menu');

    if (dropdownBtn && dropdownMenu) {
        dropdownBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const isOpen = dropdownMenu.classList.toggle('open');
            dropdownBtn.classList.toggle('open', isOpen);
            if (isOpen) {
                closeAllDropdowns(dropdownBtn);
                if (window.innerWidth <= 768) {
                    const rect = dropdownBtn.getBoundingClientRect();
                    dropdownMenu.style.top = (rect.bottom + 8) + 'px';
                    const menuWidth = dropdownMenu.offsetWidth || 200;
                    const left = Math.min(rect.left, window.innerWidth - menuWidth - 8);
                    dropdownMenu.style.left = Math.max(8, left) + 'px';
                }
            }
        });

        document.addEventListener('click', (e) => {
            if (!dropdownBtn.contains(e.target) && !dropdownMenu.contains(e.target)) {
                dropdownMenu.classList.remove('open');
                dropdownBtn.classList.remove('open');
            }
        });
    }
}

/**
 * Select an MDBList and load its content
 */
async function selectMDBList(list) {
    window.mdblistState.currentList = list;

    // Save selection to localStorage and clear FlixPatrol (skip in adaptive list edit mode)
    if (!window.adaptiveListEditMode) {
        localStorage.setItem('discoverMDBList', JSON.stringify({key: list.key, name: list.name}));
        localStorage.removeItem('discoverFlixPatrol');
    }

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

    window.discoverState.listModeActive = true;
    window.mdblistState.isLoading = true;
    setLoadingFlag();

    // Clear existing results
    if (resultsGrid) {
        resultsGrid.innerHTML = '';
    }

    try {
        const mediaType = window.discoverState.filters.mediaType || 'all';

        // TMDB show lists use a different endpoint
        const currentList = window.mdblistState.currentList;
        let response;
        if (currentList && currentList.source === 'tmdb_shows') {
            response = await fetch(`/discover/api/tmdb/shows/${currentList.listType}`);
        } else {
            response = await fetch(`/discover/api/mdblist/list/${listKey}?type=${mediaType}&limit=40`);
        }
        const data = await response.json();

        if (data.success && data.results) {
            // Store raw results for client-side filtering/sorting
            window.sidebarListsState.rawResults = data.results;
            window.sidebarListsState.listSource = 'mdblist';
            window.sidebarListsState.listId = listKey;

            await applyListFiltersAndRender(data.results);
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
    currentPeriod: 'today',
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
                        await selectFlixPatrolPlatform(platform, savedPlatform.period || 'today');
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
 * Populate the FlixPatrol dropdown menu with available platforms (Today + Weekly)
 */
function populateFlixPatrolDropdown(platforms) {
    const dropdown = document.getElementById('flixpatrol-dropdown-menu');
    if (!dropdown) return;

    dropdown.innerHTML = '';

    // Today section
    const todayHeader = document.createElement('div');
    todayHeader.className = 'flixpatrol-dropdown-header';
    todayHeader.textContent = 'Today Top 10';
    dropdown.appendChild(todayHeader);

    platforms.forEach(platform => {
        const item = document.createElement('div');
        item.className = 'flixpatrol-dropdown-item';
        item.dataset.platformId = platform.id;
        item.dataset.period = 'today';
        item.innerHTML = `
            <span class="platform-icon platform-${platform.icon}"></span>
            <span class="platform-name">${platform.name}</span>
        `;
        item.addEventListener('click', () => selectFlixPatrolPlatform(platform, 'today'));
        dropdown.appendChild(item);
    });

    // Weekly section
    const weeklyHeader = document.createElement('div');
    weeklyHeader.className = 'flixpatrol-dropdown-header';
    weeklyHeader.textContent = 'Weekly Top 10';
    dropdown.appendChild(weeklyHeader);

    platforms.forEach(platform => {
        const item = document.createElement('div');
        item.className = 'flixpatrol-dropdown-item';
        item.dataset.platformId = platform.id;
        item.dataset.period = 'weekly';
        item.innerHTML = `
            <span class="platform-icon platform-${platform.icon}"></span>
            <span class="platform-name">${platform.name}</span>
        `;
        item.addEventListener('click', () => selectFlixPatrolPlatform(platform, 'weekly'));
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
        dropdownBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const isOpen = dropdownMenu.classList.toggle('open');
            dropdownBtn.classList.toggle('open', isOpen);
            if (isOpen) {
                closeAllDropdowns(dropdownBtn);
                if (window.innerWidth <= 768) {
                    const rect = dropdownBtn.getBoundingClientRect();
                    dropdownMenu.style.top = (rect.bottom + 8) + 'px';
                    const menuWidth = dropdownMenu.offsetWidth || 200;
                    const left = Math.min(rect.left, window.innerWidth - menuWidth - 8);
                    dropdownMenu.style.left = Math.max(8, left) + 'px';
                }
            }
        });

        document.addEventListener('click', (e) => {
            if (!dropdownBtn.contains(e.target) && !dropdownMenu.contains(e.target)) {
                dropdownMenu.classList.remove('open');
                dropdownBtn.classList.remove('open');
            }
        });
    }
}

/**
 * Select a FlixPatrol platform and load its Top 10 content
 */
async function selectFlixPatrolPlatform(platform, period = 'today') {
    window.flixpatrolState.currentPlatform = platform;
    window.flixpatrolState.currentPeriod = period;

    // Save selection to localStorage and clear MDBList
    localStorage.setItem('discoverFlixPatrol', JSON.stringify({id: platform.id, name: platform.name, period}));
    localStorage.removeItem('discoverMDBList');

    // Update dropdown label
    const label = document.getElementById('flixpatrol-dropdown-label');
    if (label) {
        label.textContent = platform.name + (period === 'weekly' ? ' Weekly Top 10' : ' Top 10');
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
    await loadFlixPatrolContent(platform.id, period);
}

/**
 * Load Top 10 content from selected FlixPatrol platform
 */
async function loadFlixPatrolContent(platformId, period = 'today') {
    if (window.flixpatrolState.isLoading) return;

    window.discoverState.listModeActive = true;
    window.flixpatrolState.isLoading = true;
    setLoadingFlag();

    // Clear existing results
    if (resultsGrid) {
        resultsGrid.innerHTML = '';
    }

    try {
        const mediaType = window.discoverState.filters.mediaType || 'all';
        const baseUrl = period === 'weekly'
            ? `/discover/api/flixpatrol/top10/${platformId}/weekly`
            : `/discover/api/flixpatrol/top10/${platformId}`;
        const response = await fetch(`${baseUrl}?type=${mediaType}`);
        const data = await response.json();

        if (data.success && data.results) {
            // Store raw results for client-side filtering/sorting
            window.sidebarListsState.rawResults = data.results;
            window.sidebarListsState.listSource = 'flixpatrol';
            window.sidebarListsState.listId = platformId;

            await applyListFiltersAndRender(data.results);
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
    window.flixpatrolState.currentPeriod = 'today';

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
    mdblistPersonalLists: [],
    traktSpecialLists: [],
    traktMyLists: [],
    scrobSpecialLists: [],
    scrobMyLists: [],
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
            const tmdbShowLists = [
                { key: 'tmdb_shows_popular',      name: 'TMDB Popular Shows',   icon: 'tmdb', category: 'tmdb_shows', source: 'tmdb_shows', listType: 'popular' },
                { key: 'tmdb_shows_top_rated',    name: 'TMDB Top Rated Shows', icon: 'tmdb', category: 'tmdb_shows', source: 'tmdb_shows', listType: 'top_rated' },
                { key: 'tmdb_shows_airing_today', name: 'TMDB Airing Today',    icon: 'tmdb', category: 'tmdb_shows', source: 'tmdb_shows', listType: 'airing_today' },
                { key: 'tmdb_shows_trending',     name: 'TMDB Trending Shows',  icon: 'tmdb', category: 'tmdb_shows', source: 'tmdb_shows', listType: 'trending' },
            ];
            window.sidebarListsState.mdblistLists = [...(mdbData.lists || []), ...tmdbShowLists];
        }

        // Load Trakt special lists (static, always available)
        // Mirrors PERSONAL_SPECIAL_LISTS defined later in file — keep in sync if adding items
        window.sidebarListsState.traktSpecialLists = [
            { key: 'trending',        name: 'Trending' },
            { key: 'popular',         name: 'Popular' },
            { key: 'recommendations', name: 'Recommendations' },
            { key: 'favorited',       name: 'Favorited' },
            { key: 'played',          name: 'Played' },
            { key: 'watched',         name: 'Watched' },
            { key: 'collected',       name: 'Collected' },
            { key: 'anticipated',     name: 'Anticipated' },
            { key: 'boxoffice',       name: 'Box Office' },
        ];

        // Load Trakt My Lists (user-specific, may fail if not configured)
        try {
            const traktResp = await fetch('/discover/api/trakt/lists');
            if (traktResp.ok) {
                const traktData = await traktResp.json();
                if (traktData.success) {
                    window.sidebarListsState.traktMyLists = traktData.lists || [];
                }
            }
        } catch (_e) {}

        // Load MDBList Personal Lists (user-specific, requires API key)
        try {
            const mdbPersonalResp = await fetch('/discover/api/mdblist/personal-lists');
            if (mdbPersonalResp.ok) {
                const mdbPersonalData = await mdbPersonalResp.json();
                if (mdbPersonalData.success) {
                    window.sidebarListsState.mdblistPersonalLists = mdbPersonalData.lists || [];
                }
            }
        } catch (_e) {}

        // Load Scrob special lists (static, always available)
        // Mirrors SCROB_SPECIAL_LISTS defined later in file — keep in sync if adding items
        window.sidebarListsState.scrobSpecialLists = [
            { key: 'Trending',         name: 'Trending' },
            { key: 'Popular',          name: 'Popular' },
            { key: 'Top Rated',        name: 'Top Rated' },
            { key: 'Now Playing',      name: 'Now Playing' },
            { key: 'Upcoming',         name: 'Upcoming' },
            { key: 'On Air Today',     name: 'On Air Today' },
            { key: 'On Air This Week', name: 'On Air This Week' },
            { key: 'New Episodes',     name: 'New Episodes' },
            { key: 'Hidden Gems',      name: 'Hidden Gems' },
            { key: 'For You',          name: 'For You' },
            { key: 'Recently Added',   name: 'Recently Added' },
        ];

        // Load Scrob My Lists (user-specific, requires Scrob to be configured)
        try {
            const scrobResp = await fetch('/discover/api/scrob/lists');
            if (scrobResp.ok) {
                const scrobData = await scrobResp.json();
                if (scrobData.success) {
                    window.sidebarListsState.scrobMyLists = scrobData.lists || [];
                }
            }
        } catch (_e) {}

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
            'curated': 'Curated Collections',
            'tmdb_shows': 'TMDB Shows',
        };

        // Order categories
        const categoryOrder = ['mdblist', 'streaming', 'originals', 'curated', 'tmdb_shows'];

        categoryOrder.forEach(catKey => {
            if (categories[catKey] && categories[catKey].length > 0) {
                const catHeader = document.createElement('div');
                catHeader.className = 'chips-dropdown-header';
                catHeader.textContent = categoryNames[catKey] || catKey;
                dropdown.appendChild(catHeader);

                categories[catKey].forEach(list => {
                    const itemSource = list.source || 'mdblist';
                    const item = document.createElement('div');
                    item.className = 'chips-dropdown-item';
                    item.dataset.value = `${itemSource}:${list.key}`;
                    item.dataset.source = itemSource;
                    item.dataset.listId = list.key;
                    item.dataset.name = list.name;

                    // Check if already selected
                    const isSelected = state.selectedLists.some(l => l.source === itemSource && l.listId === list.key);
                    if (isSelected) item.classList.add('included');

                    item.innerHTML = `<span class="list-icon">${getPlatformIcon(list.icon)}</span> ${list.name}`;
                    item.addEventListener('click', () => toggleSidebarList(item, itemSource, list.key, list.name));
                    dropdown.appendChild(item);
                });
            }
        });
    }

    // Add Personal — Trakt Special Lists
    if (state.traktSpecialLists.length > 0) {
        const specialHeader = document.createElement('div');
        specialHeader.className = 'chips-dropdown-header';
        specialHeader.textContent = 'Personal — Special Lists';
        dropdown.appendChild(specialHeader);

        state.traktSpecialLists.forEach(list => {
            const item = document.createElement('div');
            item.className = 'chips-dropdown-item';
            item.dataset.value = `trakt-special:${list.key}`;
            item.dataset.source = 'trakt-special';
            item.dataset.listId = list.key;
            item.dataset.name = list.name;

            const isSelected = state.selectedLists.some(l => l.source === 'trakt-special' && l.listId === list.key);
            if (isSelected) item.classList.add('included');

            item.innerHTML = `<span class="list-icon">${getPlatformIcon('trakt')}</span> ${list.name}`;
            item.addEventListener('click', () => toggleSidebarList(item, 'trakt-special', list.key, list.name));
            dropdown.appendChild(item);
        });
    }

    // Add Personal — My Lists
    if (state.traktMyLists.length > 0) {
        const myHeader = document.createElement('div');
        myHeader.className = 'chips-dropdown-header';
        myHeader.textContent = 'Personal — My Lists';
        dropdown.appendChild(myHeader);

        state.traktMyLists.forEach(list => {
            const item = document.createElement('div');
            item.className = 'chips-dropdown-item';
            item.dataset.value = `trakt-mylist:${list.slug}`;
            item.dataset.source = 'trakt-mylist';
            item.dataset.listId = list.slug;
            item.dataset.name = list.name;

            const isSelected = state.selectedLists.some(l => l.source === 'trakt-mylist' && l.listId === list.slug);
            if (isSelected) item.classList.add('included');

            item.innerHTML = `<span class="list-icon">${getPlatformIcon('trakt')}</span> ${list.name}`;
            item.addEventListener('click', () => toggleSidebarList(item, 'trakt-mylist', list.slug, list.name));
            dropdown.appendChild(item);
        });
    }

    // Add Personal — MDBList Lists
    if (state.mdblistPersonalLists.length > 0) {
        const mdbPersonalHeader = document.createElement('div');
        mdbPersonalHeader.className = 'chips-dropdown-header';
        mdbPersonalHeader.textContent = 'Personal — MDBList';
        dropdown.appendChild(mdbPersonalHeader);

        state.mdblistPersonalLists.forEach(list => {
            const item = document.createElement('div');
            item.className = 'chips-dropdown-item';
            item.dataset.value = `mdblist-personal:${list.id}`;
            item.dataset.source = 'mdblist-personal';
            item.dataset.listId = String(list.id);
            item.dataset.name = list.name;

            const isSelected = state.selectedLists.some(l => l.source === 'mdblist-personal' && l.listId === String(list.id));
            if (isSelected) item.classList.add('included');

            item.innerHTML = `<span class="list-icon">${getPlatformIcon('mdblist')}</span> ${list.name}`;
            item.addEventListener('click', () => toggleSidebarList(item, 'mdblist-personal', String(list.id), list.name));
            dropdown.appendChild(item);
        });
    }

    // Add Scrob — Special Lists
    if (state.scrobSpecialLists.length > 0) {
        const scrobSpecialHeader = document.createElement('div');
        scrobSpecialHeader.className = 'chips-dropdown-header';
        scrobSpecialHeader.textContent = 'Scrob — Special Lists';
        dropdown.appendChild(scrobSpecialHeader);

        state.scrobSpecialLists.forEach(list => {
            const item = document.createElement('div');
            item.className = 'chips-dropdown-item';
            item.dataset.value = `scrob-special:${list.key}`;
            item.dataset.source = 'scrob-special';
            item.dataset.listId = list.key;
            item.dataset.name = list.name;

            const isSelected = state.selectedLists.some(l => l.source === 'scrob-special' && l.listId === list.key);
            if (isSelected) item.classList.add('included');

            item.innerHTML = `<span class="list-icon">${getPlatformIcon('scrob')}</span> ${list.name}`;
            item.addEventListener('click', () => toggleSidebarList(item, 'scrob-special', list.key, list.name));
            dropdown.appendChild(item);
        });
    }

    // Add Scrob — My Lists
    if (state.scrobMyLists.length > 0) {
        const scrobMyHeader = document.createElement('div');
        scrobMyHeader.className = 'chips-dropdown-header';
        scrobMyHeader.textContent = 'Scrob — My Lists';
        dropdown.appendChild(scrobMyHeader);

        state.scrobMyLists.forEach(list => {
            const item = document.createElement('div');
            item.className = 'chips-dropdown-item';
            item.dataset.value = `scrob-mylist:${list.id}`;
            item.dataset.source = 'scrob-mylist';
            item.dataset.listId = String(list.id);
            item.dataset.name = list.name;

            const isSelected = state.selectedLists.some(l => l.source === 'scrob-mylist' && l.listId === String(list.id));
            if (isSelected) item.classList.add('included');

            item.innerHTML = `<span class="list-icon">${getPlatformIcon('scrob')}</span> ${list.name}`;
            item.addEventListener('click', () => toggleSidebarList(item, 'scrob-mylist', String(list.id), list.name));
            dropdown.appendChild(item);
        });
    }

    // If no lists loaded at all
    if (state.flixpatrolPlatforms.length === 0 && state.mdblistLists.length === 0 &&
        state.mdblistPersonalLists.length === 0 &&
        state.traktSpecialLists.length === 0 && state.traktMyLists.length === 0 &&
        state.scrobSpecialLists.length === 0 && state.scrobMyLists.length === 0) {
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
    const base = '/api/overlays/logos/serve';
    const icons = {
        'netflix':       `${base}/network/color/Netflix.png`,
        'disney':        `${base}/network/color/Disney+.png`,
        'amazon':        `${base}/network/color/Prime Video.png`,
        'hbo':           `${base}/network/color/Max.png`,
        'apple':         `${base}/network/color/Apple TV+.png`,
        'paramount':     `${base}/network/color/Paramount+.png`,
        'hulu':          `${base}/network/color/Hulu.png`,
        'peacock':       `${base}/network/color/Peacock.png`,
        'bbc':           `${base}/network/color/BBC.png`,
        'discovery':     `${base}/network/color/discovery+.png`,
        'mdblist':       `${base}/rating/MDBList.png`,
        'tmdb':          `${base}/rating/TMDb.png`,
        'rottentomatoes':`${base}/rating/RT-Crit-Fresh.png`,
        'metacritic':    `${base}/rating/Metacritic.png`,
        'commonsense':   `${base}/rating/common_sense.png`,
        'trakt':         `${base}/rating/Trakt.png`,
    };
    const src = icons[iconName];
    if (src) {
        return `<img src="${src}" alt="${iconName}" style="height:20px;width:auto;max-width:56px;object-fit:contain;vertical-align:middle;">`;
    }
    return '<span style="display:inline-block;width:20px;text-align:center;">📋</span>';
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

    // Update active filters display — suppress live filtering here: rawResults is still empty
    // and the list load below will populate it and render correctly on its own.
    window.discoverState.isClearing = true;
    updateActiveFilters();
    window.discoverState.isClearing = false;

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

    // Update active filters display — suppress live filtering here: rawResults is still empty
    // and the list load below will populate it and render correctly on its own.
    window.discoverState.isClearing = true;
    updateActiveFilters();
    window.discoverState.isClearing = false;

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
    
    // Show search results area, hide trending — clear grid immediately so old results
    // don't flash while the new list fetch is in-flight
    const trendingContent = document.getElementById('trending-content');
    const searchResults = document.getElementById('search-results');
    if (trendingContent) trendingContent.style.display = 'none';
    if (searchResults) searchResults.style.display = 'block';
    window.discoverState.listModeActive = true;
    const _earlyGrid = document.getElementById('results-grid');
    if (_earlyGrid) _earlyGrid.innerHTML = '';

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
                } else if (list.source === 'trakt-special') {
                    const mediaType = window.discoverState.mediaType || 'all';
                    const response = await fetch(`/discover/api/trakt/special/${list.listId}?type=${mediaType}`);
                    if (!response.ok) throw new Error(`Failed to load ${list.listName}`);
                    data = await response.json();
                } else if (list.source === 'trakt-mylist') {
                    const mediaType = window.discoverState.mediaType || 'all';
                    const response = await fetch(`/discover/api/trakt/mylist/${list.listId}?type=${mediaType}`);
                    if (!response.ok) throw new Error(`Failed to load ${list.listName}`);
                    data = await response.json();
                } else if (list.source === 'mdblist-personal') {
                    const response = await fetch(`/discover/api/mdblist/personal-list/${list.listId}`);
                    if (!response.ok) throw new Error(`Failed to load ${list.listName}`);
                    data = await response.json();
                } else if (list.source === 'tmdb_shows') {
                    const listMeta = window.sidebarListsState.mdblistLists.find(l => l.key === list.listId);
                    const listType = listMeta ? listMeta.listType : list.listId.replace('tmdb_shows_', '');
                    const response = await fetch(`/discover/api/tmdb/shows/${listType}`);
                    if (!response.ok) throw new Error(`Failed to load ${list.listName}`);
                    data = await response.json();
                } else if (list.source === 'scrob-special') {
                    const mediaType = window.discoverState.mediaType || 'all';
                    const response = await fetch(`/discover/api/scrob/special/${encodeURIComponent(list.listId)}?type=${mediaType}`);
                    if (!response.ok) throw new Error(`Failed to load ${list.listName}`);
                    data = await response.json();
                } else if (list.source === 'scrob-mylist') {
                    const mediaType = window.discoverState.mediaType || 'all';
                    const response = await fetch(`/discover/api/scrob/list/${list.listId}?type=${mediaType}`);
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

        // "Merge with Adaptive List": also pull in TMDB Discover results (the same
        // date-filtered query the scheduled adaptive-list task runs) and combine them
        // with the list results above. Capped at 30 pages (600 items) to match
        // fetch_from_tmdb_discover's own max_pages in content_checkers/adaptive_list.py —
        // the scheduled task never fetches more than that either, so this is a
        // byte-accurate preview, not an approximation. Pages are fetched concurrently
        // (page 1 first to learn total_pages, then any remaining pages up to the cap
        // in parallel) rather than sequentially, consistent with the existing
        // ThreadPoolExecutor(max_workers=10) precedent for TMDB calls elsewhere in
        // this codebase (content_checkers/adaptive_list.py's apply_list_filters).
        if (state.filters.mergeWithAdaptive) {
            try {
                const MAX_PAGES = 30;
                const firstResponse = await fetch(`/discover/api/filter?${buildDiscoverFilterParams(1)}`);
                if (!firstResponse.ok) throw new Error('Failed to load Adaptive Discover results');
                const firstData = await firstResponse.json();
                const pagesToFetch = Math.min(MAX_PAGES, firstData.total_pages || 1);

                const addResults = (results) => {
                    (results || []).forEach(item => {
                        const itemId = item.id || item.tmdb_id;
                        if (itemId && !seenIds.has(itemId)) {
                            seenIds.add(itemId);
                            allResults.push(item);
                        }
                    });
                };
                addResults(firstData.results);

                if (pagesToFetch > 1) {
                    const remainingPages = [];
                    for (let page = 2; page <= pagesToFetch; page++) {
                        remainingPages.push(page);
                    }
                    const remainingResponses = await Promise.all(
                        remainingPages.map(page => fetch(`/discover/api/filter?${buildDiscoverFilterParams(page)}`))
                    );
                    for (const response of remainingResponses) {
                        if (response.ok) {
                            const data = await response.json();
                            addResults(data.results);
                        }
                    }
                }
            } catch (error) {
                console.error('[Lists] Error loading Adaptive Discover results:', error);
                showNotification('Failed to load Adaptive Discover results', 'error');
            }
        }

        // Store merged results for filtering
        listsState.rawResults = allResults;

        // Apply filters and render
        await filterAndRenderListResults();

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
    window.discoverState.listModeActive = false;
    
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
        } else if (source === 'trakt-special') {
            const special = state.traktSpecialLists.find(l => l.key === listId);
            listName = special ? special.name : listId;
        } else if (source === 'trakt-mylist') {
            const mylist = state.traktMyLists.find(l => l.slug === listId);
            listName = mylist ? mylist.name : listId;
        } else if (source === 'mdblist-personal') {
            const personal = state.mdblistPersonalLists.find(l => String(l.id) === listId);
            listName = personal ? personal.name : `MDBList ${listId}`;
        } else if (source === 'tmdb_shows') {
            const tmdbList = state.mdblistLists.find(l => l.key === listId && l.source === 'tmdb_shows');
            listName = tmdbList ? tmdbList.name : listId;
        } else if (source === 'scrob-special') {
            const special = state.scrobSpecialLists.find(l => l.key === listId);
            listName = special ? special.name : listId;
        } else if (source === 'scrob-mylist') {
            const mylist = state.scrobMyLists.find(l => String(l.id) === listId);
            listName = mylist ? mylist.name : listId;
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
    
    // Show search results area, hide trending — clear grid immediately so old results
    // don't flash while the new list fetch is in-flight
    if (trendingContent) trendingContent.style.display = 'none';
    if (searchResults) searchResults.style.display = 'block';
    if (resultsGrid) resultsGrid.innerHTML = '';

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
async function filterAndRenderListResults() {
    const state = window.discoverState;
    const listsState = window.sidebarListsState;
    const resultsGrid = document.getElementById('results-grid');
    const filters = state.filters;
    const hasKeywordFilter = (filters.selectedKeywords && filters.selectedKeywords.length > 0) ||
                              (filters.excludedKeywords && filters.excludedKeywords.length > 0);

    if (hasKeywordFilter) {
        await prefetchKeywordsForItems(listsState.rawResults);
    }

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
        updateResultsInfo({ total_results: filteredResults.length, page: 1, total_pages: 1 });
    } else {
        showEmpty();
    }

    state.hasMore = false;
    updatePagination();
}

/**
 * Filter, (optionally prefetch keywords), render and update info for a list result set.
 * Use this everywhere a list fetch ends and needs to be rendered.
 */
async function applyListFiltersAndRender(results) {
    const filters = window.discoverState.filters;
    const hasKeywordFilter = (filters.selectedKeywords && filters.selectedKeywords.length > 0) ||
                              (filters.excludedKeywords && filters.excludedKeywords.length > 0);
    if (hasKeywordFilter) {
        await prefetchKeywordsForItems(results);
    }
    const filtered = filterListResults(results);
    renderResults(filtered);
    window.discoverState.hasMore = false;
    updateResultsInfo({ total_results: filtered.length, page: 1, total_pages: 1 });
    updatePagination();
}

/**
 * Prefetch TMDB keyword IDs for a list of items (for keyword filtering in list mode).
 * Populates _listKeywordCache. Returns a promise that resolves when done.
 */
async function prefetchKeywordsForItems(items) {
    const uncached = items.filter(item => {
        const key = `${item.id}_${item.media_type || 'movie'}`;
        return item.id && !(key in _listKeywordCache);
    });
    if (!uncached.length) return;

    await Promise.all(uncached.map(async item => {
        const key = `${item.id}_${item.media_type || 'movie'}`;
        const endpoint = item.media_type === 'tv' ? 'tv' : 'movie';
        try {
            const r = await fetch(`/discover/api/keywords/${endpoint}/${item.id}`);
            if (r.ok) {
                const data = await r.json();
                _listKeywordCache[key] = (data.keywords || data.results || []).map(k => k.id);
            } else {
                _listKeywordCache[key] = [];
            }
        } catch (e) {
            _listKeywordCache[key] = [];
        }
    }));
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

        // Seasons max filter — TV only, only when number_of_seasons is present
        if (filters.seasonsMax > 0 && item.media_type === 'tv' && item.number_of_seasons) {
            if (item.number_of_seasons > filters.seasonsMax) return false;
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

        // Keyword filters — use pre-fetched cache populated by prefetchKeywordsForItems
        const hasKeywordFilter = (filters.selectedKeywords && filters.selectedKeywords.length > 0) ||
                                 (filters.excludedKeywords && filters.excludedKeywords.length > 0);
        if (hasKeywordFilter) {
            const cacheKey = `${item.id}_${item.media_type || 'movie'}`;
            const itemKeywords = _listKeywordCache[cacheKey];
            if (itemKeywords !== undefined) {
                if (filters.excludedKeywords && filters.excludedKeywords.length > 0) {
                    const excIds = filters.excludedKeywords.map(k => parseInt(k));
                    if (excIds.some(k => itemKeywords.includes(k))) return false;
                }
                if (filters.selectedKeywords && filters.selectedKeywords.length > 0) {
                    const incIds = filters.selectedKeywords.map(k => parseInt(k));
                    if (!incIds.some(k => itemKeywords.includes(k))) return false;
                }
            }
            // If not yet cached, allow item through (will be correct after prefetch re-renders)
        }

        return true;
    });

    // Sort filtered results — skip if sortBy is 'none' to preserve original list order
    const sortBy = filters.sortBy || 'popularity';
    if (sortBy === 'none') return filtered;

    const sortOrder = filters.sortOrder || 'desc';
    const sortDir = sortOrder === 'asc' ? 1 : -1;

    filtered.sort((a, b) => {
        let aVal, bVal;
        if (sortBy === 'vote_average') {
            aVal = a.vote_average || 0;
            bVal = b.vote_average || 0;
        } else if (sortBy === 'vote_count') {
            aVal = a.vote_count || 0;
            bVal = b.vote_count || 0;
        } else if (sortBy === 'primary_release_date') {
            aVal = a.release_date || a.first_air_date || '';
            bVal = b.release_date || b.first_air_date || '';
        } else {
            // popularity (default)
            aVal = a.popularity || 0;
            bVal = b.popularity || 0;
        }
        if (aVal < bVal) return -1 * sortDir;
        if (aVal > bVal) return 1 * sortDir;
        return 0;
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

    const titleEl = document.createElement('div');
    titleEl.className = 'dialog-title';
    titleEl.textContent = 'Select Version to Scrape';
    versionRadios.appendChild(titleEl);

    const subEl = document.createElement('div');
    subEl.className = 'dialog-sub';
    const icon = content.mediaType === 'tv' ? 'fa-tv' : 'fa-film';
    subEl.innerHTML = `<i class="fa-solid ${icon}"></i> ${content.title}${content.year ? ' (' + content.year + ')' : ''}`;
    versionRadios.appendChild(subEl);

    const verLabel = document.createElement('div');
    verLabel.className = 'section-label';
    verLabel.textContent = 'Version';
    versionRadios.appendChild(verLabel);

    const allVersions = [...availableVersions, 'No Version'];
    allVersions.forEach((version, index) => {
        const row = document.createElement('div');
        row.className = 'option-row' + (index === 0 ? ' selected' : '');
        row.dataset.value = version;
        row.innerHTML = `<div class="custom-radio"><div class="custom-radio-dot"></div></div><span class="option-label">${version}</span>`;
        row.addEventListener('click', () => {
            versionRadios.querySelectorAll('.option-row').forEach(r => r.classList.remove('selected'));
            row.classList.add('selected');
        });
        versionRadios.appendChild(row);
    });

    document.body.classList.add('modal-open');
    modal.style.display = 'flex';
}

// Show version selection modal for requests
function showVersionModal(content) {
    selectedContent = content;
    const modal = document.getElementById('versionModal');
    const versionCheckboxes = document.getElementById('versionCheckboxes');

    versionCheckboxes.innerHTML = '';

    const titleEl = document.createElement('div');
    titleEl.className = 'dialog-title';
    titleEl.textContent = 'Select Versions to Request';
    versionCheckboxes.appendChild(titleEl);

    const subEl = document.createElement('div');
    subEl.className = 'dialog-sub';
    const icon = content.mediaType === 'tv' ? 'fa-tv' : 'fa-film';
    subEl.innerHTML = `<i class="fa-solid ${icon}"></i> ${content.title}${content.year ? ' (' + content.year + ')' : ''}`;
    versionCheckboxes.appendChild(subEl);

    if (content.mediaType === 'tv') {
        const typeLabel = document.createElement('div');
        typeLabel.className = 'section-label';
        typeLabel.textContent = 'Request Type';
        versionCheckboxes.appendChild(typeLabel);

        const wholeRow = document.createElement('div');
        wholeRow.className = 'option-row selected';
        wholeRow.id = 'opt-whole-show';
        wholeRow.dataset.value = 'whole-show';
        wholeRow.innerHTML = '<div class="custom-radio"><div class="custom-radio-dot"></div></div><span class="option-label">Whole Show</span>';
        versionCheckboxes.appendChild(wholeRow);

        const seasonsRow = document.createElement('div');
        seasonsRow.className = 'option-row';
        seasonsRow.dataset.value = 'specific-seasons';
        seasonsRow.innerHTML = '<div class="custom-radio"><div class="custom-radio-dot"></div></div><span class="option-label">Specific Seasons</span>';
        versionCheckboxes.appendChild(seasonsRow);

        const seasonSelectionContainer = document.createElement('div');
        seasonSelectionContainer.id = 'season-selection-container';
        seasonSelectionContainer.style.display = 'none';
        versionCheckboxes.appendChild(seasonSelectionContainer);

        const toggleTypeRow = (selectedRow) => {
            [wholeRow, seasonsRow].forEach(r => r.classList.remove('selected'));
            selectedRow.classList.add('selected');
            if (selectedRow === seasonsRow) {
                seasonSelectionContainer.style.display = 'block';
                if (!seasonSelectionContainer.dataset.loaded) {
                    fetchShowSeasons(content.id);
                }
            } else {
                seasonSelectionContainer.style.display = 'none';
            }
        };
        wholeRow.addEventListener('click', () => toggleTypeRow(wholeRow));
        seasonsRow.addEventListener('click', () => toggleTypeRow(seasonsRow));

        const divider = document.createElement('div');
        divider.className = 'vm-divider';
        versionCheckboxes.appendChild(divider);
    }

    const verLabel = document.createElement('div');
    verLabel.className = 'section-label';
    verLabel.textContent = 'Version';
    versionCheckboxes.appendChild(verLabel);

    availableVersions.forEach(version => {
        const row = document.createElement('div');
        row.className = 'option-row' + (availableVersions.length === 1 ? ' checked' : '');
        row.dataset.value = version;
        row.dataset.type = 'version';
        row.innerHTML = `<div class="custom-cb"></div><span class="option-label">${version}</span>`;
        row.addEventListener('click', () => row.classList.toggle('checked'));
        versionCheckboxes.appendChild(row);
    });

    // Folder dropdown — symlink mode only, loaded asynchronously
    const folderContainer = document.createElement('div');
    folderContainer.id = 'request-folder-container';
    versionCheckboxes.appendChild(folderContainer);
    (async () => {
        try {
            const fRes = await fetch('/scraper/get_symlink_folders');
            const fData = await fRes.json();
            if (!fData.enabled || !fData.folders || !fData.folders.length) return;

            const genreList = (content.genres || []).map(g => String(g).trim().toLowerCase());
            const fs = fData.folder_settings || {};
            const isAnime = genreList.some(g => g.includes('anime') || g.includes('animation') || g === '16');
            const isDoc = genreList.some(g => g.includes('documentary') || g === '99');
            let autoFolder = null;
            if (content.mediaType === 'movie') {
                autoFolder = (isAnime && fs.enable_separate_anime_folders) ? fs.anime_movies_folder_name
                    : (isDoc && fs.enable_separate_documentary_folders) ? fs.documentary_movies_folder_name
                    : fs.movies_folder_name;
            } else {
                autoFolder = (isAnime && fs.enable_separate_anime_folders) ? fs.anime_tv_shows_folder_name
                    : (isDoc && fs.enable_separate_documentary_folders) ? fs.documentary_tv_shows_folder_name
                    : fs.tv_shows_folder_name;
            }

            const divider = document.createElement('div');
            divider.className = 'vm-divider';
            folderContainer.appendChild(divider);

            const label = document.createElement('div');
            label.className = 'section-label';
            label.textContent = 'Folder';
            folderContainer.appendChild(label);

            // Filter folders by media type — same logic as scraper.js
            const mediaType = content.mediaType === 'movie' ? 'movie' : 'tv';
            const filteredFolders = fData.folders.filter(folder => {
                if (folder.is_custom) return true; // custom folders show for both
                const nameLower = folder.name.toLowerCase();
                if (mediaType === 'movie') {
                    return nameLower.includes('movie') || nameLower === (fs.movies_folder_name || '').toLowerCase();
                } else {
                    return nameLower.includes('show') || nameLower.includes('tv') || nameLower === (fs.tv_shows_folder_name || '').toLowerCase();
                }
            });
            if (!filteredFolders.length) return;

            const select = document.createElement('select');
            select.id = 'request-folder-select';
            select.style.cssText = 'width:100%;padding:8px 10px;background:#1a1a1a;color:#fff;border:1px solid #333;border-radius:6px;font-size:12px;margin-top:4px;';
            filteredFolders.forEach(folder => {
                const opt = document.createElement('option');
                opt.value = folder.name;
                opt.dataset.isCustom = folder.is_custom ? 'true' : 'false';
                const displayName = folder.is_custom
                    ? `${folder.name} (${mediaType === 'movie' ? fs.movies_folder_name : fs.tv_shows_folder_name})`
                    : folder.name;
                opt.textContent = displayName;
                if (folder.name === autoFolder) opt.selected = true;
                select.appendChild(opt);
            });
            folderContainer.appendChild(select);
        } catch (e) {
            // not symlink mode or fetch failed — silently ignore
        }
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
            const divider = document.createElement('div'); divider.className = 'vm-divider'; tagsContainer.appendChild(divider);
            const lbl = document.createElement('div'); lbl.className = 'section-label'; lbl.textContent = 'Tags'; tagsContainer.appendChild(lbl);
            const pillWrap = document.createElement('div');
            pillWrap.id = 'request-tags-pills';
            pillWrap.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;';
            globalTags.forEach(tag => {
                const pill = document.createElement('div');
                pill.className = 'option-row';
                pill.dataset.value = tag;
                pill.dataset.type = 'tag';
                pill.style.cssText = 'padding:5px 14px;border-radius:14px;cursor:pointer;font-size:12px;flex:none;';
                pill.innerHTML = `<span class="option-label">${tag}</span>`;
                pill.addEventListener('click', () => pill.classList.toggle('checked'));
                pillWrap.appendChild(pill);
            });
            tagsContainer.appendChild(pillWrap);
        } catch(e) {}
    })();

    document.body.classList.add('modal-open');
    modal.style.display = 'flex';
}

// Function to fetch show seasons from the server
async function fetchShowSeasons(tmdbId) {
    const seasonContainer = document.getElementById('season-selection-container');
    seasonContainer.innerHTML = '<p style="padding:8px;opacity:0.6;">Loading seasons...</p>';
    try {
        const response = await fetch(`/content/show_seasons?tmdb_id=${tmdbId}`, { method: 'GET' });
        const data = await response.json();

        if (data.success && data.seasons && data.seasons.length > 0) {
            seasonContainer.innerHTML = '';
            seasonContainer.dataset.loaded = '1';
            const seasons = data.seasons.sort((a, b) => a - b);
            seasons.forEach(season => {
                const row = document.createElement('div');
                row.className = 'option-row';
                row.dataset.value = season;
                row.innerHTML = `<div class="custom-cb"></div><span class="option-label">Season ${season}</span>`;
                row.addEventListener('click', () => row.classList.toggle('checked'));
                seasonContainer.appendChild(row);
            });
        } else {
            const msg = data.error ? `Error: ${data.error}` : 'Could not load seasons. Please try again or request the whole show.';
            seasonContainer.innerHTML = `<p style="padding:8px;opacity:0.6;">${msg}</p>`;
        }
    } catch (error) {
        console.error('Error fetching show seasons:', error);
        seasonContainer.innerHTML = '<p style="padding:8px;opacity:0.6;">Error loading seasons. Please try again later.</p>';
    }
}

// Handle scrape version confirmation
async function handleScrapeVersionConfirm() {
    const selectedRow = document.querySelector('#scrapeVersionRadios .option-row.selected');
    const selectedVersion = selectedRow ? selectedRow.dataset.value : undefined;
    if (selectedVersion === undefined) {
        showPopup({ type: 'warning', title: 'Required', message: 'Please select a version.' });
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
    const selectedVersions = Array.from(document.querySelectorAll('#versionCheckboxes .option-row.checked[data-type="version"]'))
        .map(row => row.dataset.value);

    if (selectedVersions.length === 0) {
        showPopup({ type: 'warning', title: 'Required', message: 'Please select at least one version' });
        return;
    }

    if (selectedContent.mediaType === 'tv') {
        const wholeShowRow = document.getElementById('opt-whole-show');
        const wholeShowSelected = wholeShowRow ? wholeShowRow.classList.contains('selected') : true;

        if (!wholeShowSelected) {
            const selectedSeasons = Array.from(document.querySelectorAll('#season-selection-container .option-row.checked'))
                .map(row => parseInt(row.dataset.value));

            if (selectedSeasons.length === 0) {
                showPopup({ type: 'warning', title: 'Required', message: 'Please select at least one season or choose "Whole Show"' });
                return;
            }

            selectedContent.seasons = selectedSeasons;
        }
    }

    // Read folder selection if dropdown is present (symlink mode)
    const folderSelect = document.getElementById('request-folder-select');
    const selectedFolder = folderSelect ? folderSelect.value : null;
    const selectedFolderIsCustom = folderSelect
        ? (folderSelect.options[folderSelect.selectedIndex]?.dataset?.isCustom === 'true')
        : false;

    // Read tags selection if present (Plex mode)
    const _tagPills = document.querySelectorAll('#request-tags-pills .option-row.checked[data-type="tag"]');
    const selectedTags = _tagPills.length ? Array.from(_tagPills).map(p=>p.dataset.value).join(',') : null;

    closeVersionModal();
    await requestContent(selectedContent, selectedVersions, selectedFolder, selectedFolderIsCustom, selectedTags);
}

// Request content from backend
async function requestContent(content, selectedVersions, selectedFolder = null, selectedFolderIsCustom = false, selectedTags = null) {
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

        // Add folder selection if provided (symlink mode)
        if (selectedFolder) {
            requestData.selected_folder = selectedFolder;
            requestData.selected_folder_is_custom = selectedFolderIsCustom;
        }

        // Add tags if provided (Plex mode)
        if (selectedTags) {
            requestData.selected_tags = selectedTags;
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
            showPopup({ type: 'success', title: 'Success', message: result.message || `Successfully requested ${content.title}` });
        } else {
            showPopup({ type: 'error', title: 'Error', message: result.error || 'Failed to request content' });
        }
    } catch (error) {
        hideLoadingMessage();
        console.error('Error requesting content:', error);
        showPopup({ type: 'error', title: 'Error', message: 'An error occurred while requesting content' });
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

// ── Personal Lists (Trakt special + personal) ─────────────────────────────────

window.personalState = {
    myLists: [],
    scrobLists: [],
    currentSelection: null,  // { type: 'special'|'mylist'|'scrob-special'|'scrob-mylist', key: string, name: string }
    myListsLoaded: false,
    isLoading: false,
};

const PERSONAL_SPECIAL_LISTS = [
    { key: 'trending',      name: 'Trending' },
    { key: 'popular',       name: 'Popular' },
    { key: 'recommendations', name: 'Recommendations' },
    { key: 'favorited',     name: 'Favorited' },
    { key: 'played',        name: 'Played' },
    { key: 'watched',       name: 'Watched' },
    { key: 'collected',     name: 'Collected' },
    { key: 'anticipated',   name: 'Anticipated' },
    { key: 'boxoffice',     name: 'Box Office' },
];

// Keys must match SPECIAL_LIST_ENDPOINTS in content_checkers/scrob.py exactly
// (case-sensitive — sent as-is to /discover/api/scrob/special/<key>).
const SCROB_SPECIAL_LISTS = [
    { key: 'Trending',         name: 'Trending' },
    { key: 'Popular',          name: 'Popular' },
    { key: 'Top Rated',        name: 'Top Rated' },
    { key: 'Now Playing',      name: 'Now Playing' },
    { key: 'Upcoming',         name: 'Upcoming' },
    { key: 'On Air Today',     name: 'On Air Today' },
    { key: 'On Air This Week', name: 'On Air This Week' },
    { key: 'New Episodes',     name: 'New Episodes' },
    { key: 'Hidden Gems',      name: 'Hidden Gems' },
    { key: 'For You',          name: 'For You' },
    { key: 'Recently Added',   name: 'Recently Added' },
];

function populatePersonalDropdown(myLists, mdblistPersonalLists, scrobLists) {
    const menu = document.getElementById('personal-dropdown-menu');
    if (!menu) return;
    menu.innerHTML = '';

    // Special Lists group (Trakt)
    const specialHeader = document.createElement('div');
    specialHeader.className = 'mdblist-dropdown-header';
    specialHeader.textContent = 'Special Lists';
    menu.appendChild(specialHeader);

    PERSONAL_SPECIAL_LISTS.forEach(list => {
        const item = document.createElement('div');
        item.className = 'mdblist-dropdown-item';
        item.dataset.key = list.key;
        item.innerHTML = `<span class="list-name">${list.name}</span>`;
        item.addEventListener('click', () => selectPersonalList({ type: 'special', key: list.key, name: list.name }));
        menu.appendChild(item);
    });

    // Scrob Special Lists group — separate from Trakt's since several names
    // overlap (Trending, Popular) but hit a different backend/results.
    const scrobSpecialHeader = document.createElement('div');
    scrobSpecialHeader.className = 'mdblist-dropdown-header';
    scrobSpecialHeader.textContent = 'Scrob — Special Lists';
    menu.appendChild(scrobSpecialHeader);

    SCROB_SPECIAL_LISTS.forEach(list => {
        const item = document.createElement('div');
        item.className = 'mdblist-dropdown-item';
        item.dataset.scrobSpecialKey = list.key;
        item.innerHTML = `<span class="list-name">${list.name}</span>`;
        item.addEventListener('click', () => selectPersonalList({ type: 'scrob-special', key: list.key, name: list.name }));
        menu.appendChild(item);
    });

    // Trakt My Lists group — only if we have any
    if (myLists && myLists.length > 0) {
        const myHeader = document.createElement('div');
        myHeader.className = 'mdblist-dropdown-header';
        myHeader.textContent = 'Trakt — My Lists';
        menu.appendChild(myHeader);

        myLists.forEach(list => {
            const item = document.createElement('div');
            item.className = 'mdblist-dropdown-item';
            item.dataset.slug = list.slug;
            item.innerHTML = `<span class="list-name">${list.name}</span>`;
            item.addEventListener('click', () => selectPersonalList({ type: 'mylist', key: list.slug, name: list.name }));
            menu.appendChild(item);
        });
    }

    // MDBList Personal Lists group — only if we have any
    if (mdblistPersonalLists && mdblistPersonalLists.length > 0) {
        const mdbHeader = document.createElement('div');
        mdbHeader.className = 'mdblist-dropdown-header';
        mdbHeader.textContent = 'MDBList — My Lists';
        menu.appendChild(mdbHeader);

        mdblistPersonalLists.forEach(list => {
            const item = document.createElement('div');
            item.className = 'mdblist-dropdown-item';
            item.dataset.mdblistId = list.id;
            item.innerHTML = `<span class="list-name">${list.name}</span>`;
            item.addEventListener('click', () => selectPersonalList({ type: 'mdblist-personal', key: String(list.id), name: list.name }));
            menu.appendChild(item);
        });
    }

    // Scrob My Lists group — only if we have any
    if (scrobLists && scrobLists.length > 0) {
        const scrobHeader = document.createElement('div');
        scrobHeader.className = 'mdblist-dropdown-header';
        scrobHeader.textContent = 'Scrob — My Lists';
        menu.appendChild(scrobHeader);

        scrobLists.forEach(list => {
            const item = document.createElement('div');
            item.className = 'mdblist-dropdown-item';
            item.dataset.scrobListId = list.id;
            item.innerHTML = `<span class="list-name">${list.name}</span>`;
            item.addEventListener('click', () => selectPersonalList({ type: 'scrob-mylist', key: String(list.id), name: list.name }));
            menu.appendChild(item);
        });
    }
}

async function initPersonal() {
    // Populate Special Lists immediately (static)
    populatePersonalDropdown([], [], []);
    bindPersonalEvents();

    // Restore saved selection
    const saved = localStorage.getItem('discoverPersonal');
    if (saved) {
        try {
            const sel = JSON.parse(saved);
            // Validate the saved object has required fields
            if (sel && sel.type && sel.key && sel.name) {
                // Defer until after page init so grid is ready
                setTimeout(() => selectPersonalList(sel, true), 0);
            } else {
                localStorage.removeItem('discoverPersonal');
            }
        } catch (e) {
            localStorage.removeItem('discoverPersonal');
        }
    }
}

async function loadPersonalMyLists() {
    if (window.personalState.myListsLoaded) return;
    try {
        const [traktResp, mdbResp, scrobResp] = await Promise.allSettled([
            fetch('/discover/api/trakt/lists'),
            fetch('/discover/api/mdblist/personal-lists'),
            fetch('/discover/api/scrob/lists'),
        ]);

        if (traktResp.status === 'fulfilled' && traktResp.value.ok) {
            const data = await traktResp.value.json();
            if (data.success && data.lists) {
                window.personalState.myLists = data.lists;
            }
        }

        let mdblistPersonalLists = [];
        if (mdbResp.status === 'fulfilled' && mdbResp.value.ok) {
            const data = await mdbResp.value.json();
            if (data.success && data.lists) {
                mdblistPersonalLists = data.lists;
                window.personalState.mdblistPersonalLists = mdblistPersonalLists;
            }
        }

        let scrobLists = [];
        if (scrobResp.status === 'fulfilled' && scrobResp.value.ok) {
            const data = await scrobResp.value.json();
            if (data.success && data.lists) {
                scrobLists = data.lists;
                window.personalState.scrobLists = scrobLists;
            }
        }

        window.personalState.myListsLoaded = true;
        populatePersonalDropdown(window.personalState.myLists || [], mdblistPersonalLists, scrobLists);
    } catch (e) {
        console.error('[Personal] Failed to load my lists:', e);
    }
}

async function selectPersonalList(sel, restoring = false) {
    window.personalState.currentSelection = sel;

    if (!restoring) {
        localStorage.setItem('discoverPersonal', JSON.stringify(sel));
        localStorage.removeItem('discoverMDBList');
        localStorage.removeItem('discoverFlixPatrol');
    }

    // Update dropdown label
    const label = document.getElementById('personal-dropdown-label');
    if (label) label.textContent = sel.name;

    // Close dropdown
    const menu = document.getElementById('personal-dropdown-menu');
    const btn  = document.getElementById('personal-dropdown-btn');
    if (menu) menu.classList.remove('open');
    if (btn)  { btn.classList.remove('open'); btn.classList.add('selected'); }

    // Mark active tab
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');

    // Reset other dropdowns
    if (typeof resetFlixPatrolSelection === 'function') resetFlixPatrolSelection();
    const mdbBtn = document.getElementById('mdblist-dropdown-btn');
    if (mdbBtn) { mdbBtn.classList.remove('active', 'selected'); }
    document.getElementById('mdblist-dropdown-label').textContent = 'Lists';

    window.discoverState.currentTab = 'personal';
    window.discoverState.page = 1;

    if (trendingContent) trendingContent.style.display = 'none';
    if (searchResults)   searchResults.style.display = 'block';

    await loadPersonalContent();
}

async function loadPersonalContent() {
    const sel = window.personalState.currentSelection;
    if (!sel || window.personalState.isLoading) return;

    if (!resultsGrid) resultsGrid = document.getElementById('results-grid');
    window.discoverState.listModeActive = true;
    window.personalState.isLoading = true;
    setLoadingFlag();
    if (resultsGrid) resultsGrid.innerHTML = '';

    try {
        const mediaType = window.discoverState.filters.mediaType || 'all';
        let url;
        if (sel.type === 'special') {
            url = `/discover/api/trakt/special/${sel.key}?type=${mediaType}`;
        } else if (sel.type === 'mdblist-personal') {
            url = `/discover/api/mdblist/personal-list/${sel.key}`;
        } else if (sel.type === 'scrob-special') {
            url = `/discover/api/scrob/special/${encodeURIComponent(sel.key)}?type=${mediaType}`;
        } else if (sel.type === 'scrob-mylist') {
            url = `/discover/api/scrob/list/${sel.key}?type=${mediaType}`;
        } else {
            url = `/discover/api/trakt/mylist/${sel.key}?type=${mediaType}`;
        }

        const resp = await fetch(url);
        const data = await resp.json();

        if (data.success && data.results) {
            // Store raw results for client-side filtering/sorting
            window.sidebarListsState.rawResults = data.results;
            window.sidebarListsState.listSource = 'personal';
            window.sidebarListsState.listId = sel.key;

            await applyListFiltersAndRender(data.results);
        } else {
            showError(data.error || 'Failed to load personal list');
        }
    } catch (e) {
        console.error('[Personal] Load error:', e);
        showError('Failed to load personal list');
    } finally {
        window.personalState.isLoading = false;
        clearLoadingFlag();
    }
}

function bindPersonalEvents() {
    const btn  = document.getElementById('personal-dropdown-btn');
    const menu = document.getElementById('personal-dropdown-menu');
    if (!btn || !menu) return;

    btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const isOpen = menu.classList.contains('open');
        closeAllDropdowns(btn);

        if (!isOpen) {
            menu.classList.add('open');
            btn.classList.add('open');
            if (window.innerWidth <= 768) {
                const rect = btn.getBoundingClientRect();
                menu.style.top = (rect.bottom + 8) + 'px';
                const menuWidth = menu.offsetWidth || 200;
                const left = Math.min(rect.left, window.innerWidth - menuWidth - 8);
                menu.style.left = Math.max(8, left) + 'px';
            }
            await loadPersonalMyLists();
        }
    });

    document.addEventListener('click', (e) => {
        if (!btn.contains(e.target) && !menu.contains(e.target)) {
            menu.classList.remove('open');
            btn.classList.remove('open');
        }
    });
}

// Init on page load
document.addEventListener('DOMContentLoaded', function() {
    setTimeout(initPersonal, 150);
});
