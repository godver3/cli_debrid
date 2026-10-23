/* Moved from templates/library_shows_overview.html. */
let selectedVersions = []; // Keep this global
let selectedCollectionStatus = 'all'; // Keep this global, default to 'all'
let observer; 

document.addEventListener('DOMContentLoaded', function () {
    console.log('DOM fully loaded and parsed');
    const showsListContainer = document.getElementById('shows-list-container');
    if (!showsListContainer) {
        console.error('showsListContainer not found!');
        return;
    }

    loadVersionSelection(); 
    loadCollectionStatusSelection(); 

    const showItems = showsListContainer.querySelectorAll('.library-show-item');
    console.log(`Found ${showItems.length} show items.`);

    if (showItems.length > 0) {
        const observerOptions = {
            root: null,
            rootMargin: '300px 0px 300px 0px',
            threshold: 0.01 
        };

        observer = new IntersectionObserver((entries, obs) => {
            // console.log('IntersectionObserver callback triggered. Entries:', entries.length);
            entries.forEach(entry => {
                const imdbId = entry.target.dataset.imdbId || 'UNKNOWN_IMDB_ID';
                // console.log(`Observer entry for ${imdbId}: isIntersecting=${entry.isIntersecting}, intersectionRatio=${entry.intersectionRatio.toFixed(2)}`);
                if (entry.isIntersecting) {
                    // console.log(`Observer: ${imdbId} is intersecting. Attempting fetchShowDetails.`);
                    fetchShowDetails(entry.target); 
                }
            });
        }, observerOptions);

        showItems.forEach(item => {
            observer.observe(item);
            // Add a class to indicate it's pending its first filter check if details are not pre-loaded
            const placeholder = item.querySelector('.show-details-placeholder');
            if (!placeholder || !placeholder.dataset.loaded) {
                // item.classList.add('pending-initial-filter'); // Optional: for styling
            }

            const header = item.querySelector('.show-header');
            const detailsContentDiv = item.querySelector('.show-details-content'); 

            if (header && detailsContentDiv) {
                header.addEventListener('click', () => {
                    const imdbId = item.dataset.imdbId;
                    // console.log(`Header clicked for ${imdbId}`);
                    const placeholder = item.querySelector('.show-details-placeholder');
                    const detailsLoaded = placeholder && placeholder.dataset.loaded === 'true';

                    if (detailsLoaded) { 
                        const isVisible = detailsContentDiv.classList.toggle('visible');
                        header.classList.toggle('expanded', isVisible);
                        filterAndStyleShow(imdbId); 
                    } else if (!placeholder.dataset.loading) {
                        // console.log(`Header click: ${imdbId} not loaded, attempting fetchShowDetails.`);
                        fetchShowDetails(item); 
                        header.classList.toggle('expanded', true); 
                    }
                });
            }
        });
        console.log('IntersectionObserver initialized and all items observed.');
    }

    const checkboxes = document.querySelectorAll('.version-checkbox');
    checkboxes.forEach(checkbox => {
        checkbox.addEventListener('change', handleVersionSelectionChange);
    });

    const radioButtons = document.querySelectorAll('.collection-status-radio');
    radioButtons.forEach(radio => {
        radio.addEventListener('change', handleCollectionStatusChange);
    });

    console.log('Applying initial filterAndStyleShow to all items...');
    showItems.forEach(item => {
        filterAndStyleShow(item.dataset.imdbId);
    });
    console.log('Calling initial triggerLoadForVisibleUnloadedItems...');
    triggerLoadForVisibleUnloadedItems();
    console.log('Initial setup complete.');
});

function fetchShowDetails(showItem) {
    const imdbId = showItem.dataset.imdbId;
    // REMOVED: if (window.getComputedStyle(showItem).display === 'none') return; 
    // We fetch regardless of current display set by a PREVIOUS filterAndStyleShow run.
    // The filterAndStyleShow AFTER fetch will determine final visibility.

    const detailsPlaceholder = showItem.querySelector('.show-details-placeholder');
    const detailsContentDiv = showItem.querySelector('.show-details-content');

    if (!detailsPlaceholder || !detailsContentDiv) {
        // console.error(`[fetchShowDetails for ${imdbId}] Placeholder or content div not found.`);
        return;
    }
    if (detailsPlaceholder.dataset.loaded === 'true') {
        // console.log(`[fetchShowDetails for ${imdbId}] Details already loaded.`);
        return;
    }
    if (detailsPlaceholder.dataset.loading === 'true') {
        // console.log(`[fetchShowDetails for ${imdbId}] Details already loading.`);
        return;
    }

    detailsPlaceholder.dataset.loading = 'true';
    console.log(`[fetchShowDetails for ${imdbId}] Fetching...`);
    // showItem.classList.remove('pending-initial-filter'); // Optional: remove pending class
    const apiUrl = window.pageData.library_shows_overview.library_management_get_library_show_deta.replace('TEMP_IMDB_ID', imdbId);

    fetch(apiUrl)
        .then(response => {
            // console.log(`[fetchShowDetails for ${imdbId}] Response received. OK=${response.ok}`);
            return response.ok ? response.json() : Promise.reject(new Error(`HTTP error! status: ${response.status} for ${imdbId}`));
        })
        .then(data => {
            // console.log(`[fetchShowDetails for ${imdbId}] Data processed. Error: ${data.error ? data.error : 'No'}`);
            detailsPlaceholder.style.display = 'none'; 
            let html = '';
            if (data.error) {
                html = `<p class="text-danger">Error loading details: ${data.details || data.error}</p>`;
            } else {
                if (data.total_show_episodes > 0) {
                    html += `<p class="mb-1">Total Episodes (Overall): ${data.total_show_episodes}</p>`;
                } else if (!data.error) { 
                    html += `<p class="mb-1">Total Episodes (Overall): N/A</p>`;
                }
                if (data.versions_details && data.versions_details.length > 0) {
                    html += `<h6>Collection Status per Version:</h6><ul class="list-unstyled">`;

                    let isMissingAnything = false;

                    data.versions_details.forEach(version_detail => {
                        html += `<li class="version-detail-item" 
                                     data-version-name="${version_detail.name}"
                                     data-collected="${version_detail.collected_episodes}"
                                     data-total="${version_detail.total_episodes_for_version}">
                            <strong>${version_detail.name}:</strong> 
                            ${version_detail.status_text} 
                        </li>`;

                        // Check if anything is missing to decide whether to show the button
                        if (version_detail.next_missing) {
                            isMissingAnything = true;
                        }
                    });
                    html += `</ul>`;

                    if (isMissingAnything) {
                        const scrapeUrl = new URL(window.pageData.library_shows_overview.scraper_index_url, window.location.origin);
                        scrapeUrl.searchParams.set('search_term', imdbId);

                        const buttonStyle = `
                            display: inline-flex; 
                            align-items: center; 
                            justify-content: center; 
                            font-weight: 500; 
                            text-align: center; 
                            white-space: nowrap; 
                            vertical-align: middle; 
                            user-select: none; 
                            border: 1px solid #FFA500; 
                            text-decoration: none; 
                            transition: background-color 0.15s ease-in-out;
                            color: #1a1a1a; 
                            background-color: #FFA500; 
                            padding: 0.25rem 0.6rem; 
                            font-size: 0.875rem; 
                            line-height: 1.5; 
                            border-radius: 0.2rem;
                            cursor: pointer;
                        `;

                        const iconStyle = `margin-right: 0.4rem;`;

                        html += `<div class="mt-2">
                                    <a href="${scrapeUrl.toString()}" 
                                       target="_blank" 
                                       style="${buttonStyle}" 
                                       onmouseover="this.style.backgroundColor='#d98e00'" 
                                       onmouseout="this.style.backgroundColor='#FFA500'"
                                       title="Open scraper page and search for this show">
                                        <i class="fas fa-search" style="${iconStyle}"></i> Search for Show
                                    </a>
                                 </div>`;
                    }

                } else if (!data.error) { 
                    html += `<p class="mb-1">No version-specific collection details available.</p>`;
                }
            }
            detailsContentDiv.innerHTML = html;
            detailsPlaceholder.dataset.loaded = 'true';
            delete detailsPlaceholder.dataset.loading;

            // CRITICAL: Now filter and style based on the new data.
            filterAndStyleShow(imdbId); 

            const header = showItem.querySelector('.show-header');
            if (header.classList.contains('expanded')) {
                detailsContentDiv.classList.add('visible');
                filterAndStyleShow(imdbId); // Re-apply for version items if expanded
            }
        })
        .catch(error => {
            console.error(`[fetchShowDetails for ${imdbId}] Fetch error:`, error);
            detailsPlaceholder.style.display = 'none';
            detailsContentDiv.innerHTML = `<p class="text-danger">Failed to load details. ${error.message}</p>`;
            detailsPlaceholder.dataset.loaded = 'true'; 
            delete detailsPlaceholder.dataset.loading;
            // Still call filterAndStyleShow to ensure it's correctly hidden or styled if it was pending
            filterAndStyleShow(imdbId); 
        });
}

// --- Version Selection Logic ---
function loadVersionSelection() {
    // console.log('Loading version selection from localStorage.');
    const storedSelection = localStorage.getItem('libraryShowSelectedVersions');
    const checkboxes = document.querySelectorAll('.version-checkbox');
    let defaultSelection = [];
    checkboxes.forEach(cb => defaultSelection.push(cb.value)); 

    if (storedSelection) {
        try {
            const parsedSelection = JSON.parse(storedSelection);
            if (Array.isArray(parsedSelection)) { 
                selectedVersions = parsedSelection;
            } else {
                selectedVersions = defaultSelection; 
            }
        } catch (e) {
            console.error("Error parsing stored version selection:", e);
            selectedVersions = defaultSelection; 
        }
    } else {
        selectedVersions = defaultSelection; 
    }
    // console.log('Selected versions:', selectedVersions);
    checkboxes.forEach(checkbox => {
        checkbox.checked = selectedVersions.includes(checkbox.value);
    });
}

function saveVersionSelection() {
    // console.log('Saving version selection to localStorage:', selectedVersions);
    localStorage.setItem('libraryShowSelectedVersions', JSON.stringify(selectedVersions));
}

function handleVersionSelectionChange() {
    console.log('Version selection changed.');
    selectedVersions = [];
    document.querySelectorAll('.version-checkbox:checked').forEach(checkbox => {
        selectedVersions.push(checkbox.value);
    });
    saveVersionSelection();

    document.querySelectorAll('.library-show-item').forEach(showItem => {
        filterAndStyleShow(showItem.dataset.imdbId); // Re-evaluate display based on new filters
    });
    triggerLoadForVisibleUnloadedItems(); // Check if any newly styled items need loading
}

// --- Collection Status Selection Logic ---
function loadCollectionStatusSelection() {
    // console.log('Loading collection status from localStorage.');
    const storedStatus = localStorage.getItem('libraryShowCollectionStatus');
    selectedCollectionStatus = storedStatus || 'all'; 
    // console.log('Selected collection status:', selectedCollectionStatus);
    document.querySelectorAll('.collection-status-radio').forEach(radio => {
        if (radio.value === selectedCollectionStatus) {
            radio.checked = true;
        }
    });
}

function saveCollectionStatusSelection() {
    // console.log('Saving collection status to localStorage:', selectedCollectionStatus);
    localStorage.setItem('libraryShowCollectionStatus', selectedCollectionStatus);
}

function handleCollectionStatusChange(event) {
    console.log('Collection status changed to:', event.target.value);
    selectedCollectionStatus = event.target.value;
    saveCollectionStatusSelection();

    document.querySelectorAll('.library-show-item').forEach(showItem => {
        filterAndStyleShow(showItem.dataset.imdbId); // Re-evaluate display based on new filters
    });
    triggerLoadForVisibleUnloadedItems(); // Check if any newly styled items need loading
}

function triggerLoadForVisibleUnloadedItems() {
    console.log('[triggerLoadForVisibleUnloadedItems] Checking for items to load proactively...');
    let itemsToLoadCount = 0;
    document.querySelectorAll('.library-show-item').forEach(item => {
        const imdbId = item.dataset.imdbId;
        // Check if the item is styled to be visible (this is after filterAndStyleShow has run)
        if (window.getComputedStyle(item).display !== 'none') {
            const placeholder = item.querySelector('.show-details-placeholder');
            const detailsLoaded = placeholder && placeholder.dataset.loaded === 'true';
            if (!detailsLoaded && !(placeholder && placeholder.dataset.loading)) {
                const rect = item.getBoundingClientRect();
                // Check if any part of the item is within the viewport (more generous)
                const isInViewport = rect.top < window.innerHeight && rect.bottom > 0; 
                if (isInViewport) { 
                     console.log(`[triggerLoadForVisibleUnloadedItems] Proactively fetching for ${imdbId} as it's visible and in viewport.`);
                     fetchShowDetails(item);
                     itemsToLoadCount++;
                } else {
                    // console.log(`[triggerLoadForVisibleUnloadedItems] ${imdbId} is visible and unloaded but not currently in viewport.`);
                }
            }
        } else {
            // console.log(`[triggerLoadForVisibleUnloadedItems] Item ${imdbId} is display:none.`);
        }
    });
    console.log(`[triggerLoadForVisibleUnloadedItems] Proactively attempted to load ${itemsToLoadCount} items.`);
}

function filterAndStyleShow(imdbId) {
    const showContainer = document.getElementById(`show-${imdbId}`); 
    if (!showContainer) {
        // console.warn(`[filterAndStyleShow] Show container for ${imdbId} not found.`);
        return;
    }

    const detailsContentDiv = document.getElementById(`content-${imdbId}`);
    const placeholder = showContainer.querySelector('.show-details-placeholder');
    const detailsLoaded = placeholder && placeholder.dataset.loaded === 'true';

    // console.log(`[filterAndStyleShow for ${imdbId}] Details loaded: ${detailsLoaded}, Collection filter: ${selectedCollectionStatus}`);

    let showItemBasedOnCollectionFilter = false;
    let isConsideredFullyCollectedForStyling = false;
    let hasAnySelectedVersionsWithDetails = false;

    if (detailsLoaded) {
        // Logic for when details ARE loaded (same as before)
        if (detailsContentDiv) {
            let selectedVersionItemsDisplayed = [];
            detailsContentDiv.querySelectorAll('.version-detail-item').forEach(item => {
                const versionName = item.dataset.versionName;
                if (selectedVersions.includes(versionName)) {
                    item.style.display = 'list-item'; 
                    hasAnySelectedVersionsWithDetails = true;
                    selectedVersionItemsDisplayed.push(item);
                } else {
                    item.style.display = 'none';
                }
            });

            if (hasAnySelectedVersionsWithDetails) {
                let fullyCollectedRelevantCount = 0;
                let zeroCollectedRelevantCount = 0;
                let partiallyCollectedRelevantCount = 0;
                let relevantDisplayedCount = 0;

                selectedVersionItemsDisplayed.forEach(item => {
                    const collected = parseInt(item.dataset.collected);
                    const total = parseInt(item.dataset.total);
                    if (!isNaN(collected) && !isNaN(total) && total > 0) { 
                        relevantDisplayedCount++;
                        if (collected >= total) {
                            fullyCollectedRelevantCount++;
                        } else if (collected === 0) {
                            zeroCollectedRelevantCount++;
                        } else {
                            partiallyCollectedRelevantCount++;
                        }
                    }
                });

                if (relevantDisplayedCount > 0 && 
                    fullyCollectedRelevantCount > 0 && 
                    partiallyCollectedRelevantCount === 0 &&
                    (fullyCollectedRelevantCount + zeroCollectedRelevantCount === relevantDisplayedCount)) {
                    isConsideredFullyCollectedForStyling = true;
                }
            }
        }

        if (selectedCollectionStatus === 'all') {
            showItemBasedOnCollectionFilter = true;
        } else if (selectedCollectionStatus === 'collected') {
            showItemBasedOnCollectionFilter = isConsideredFullyCollectedForStyling && hasAnySelectedVersionsWithDetails;
        } else if (selectedCollectionStatus === 'uncollected') {
            showItemBasedOnCollectionFilter = !isConsideredFullyCollectedForStyling || !hasAnySelectedVersionsWithDetails;
             if (!hasAnySelectedVersionsWithDetails && detailsLoaded) { 
                showItemBasedOnCollectionFilter = true; // It's "uncollected" if no relevant version data to prove otherwise
            }
        }
    } else {
        // **NEW LOGIC for when details are NOT loaded**
        // If filter is 'all', always show.
        // If filter is 'collected' or 'uncollected', initially show it to allow observer to trigger fetch.
        // The actual filtering (hiding) will happen *after* details are fetched and this function runs again.
        showItemBasedOnCollectionFilter = true; 
        // console.log(`[filterAndStyleShow for ${imdbId}] Details NOT loaded. Defaulting to showItemBasedOnCollectionFilter = true to allow observer.`);
    }

    const currentDisplay = window.getComputedStyle(showContainer).display;
    const newDisplay = showItemBasedOnCollectionFilter ? '' : 'none';

    // console.log(`[filterAndStyleShow for ${imdbId}] Filter: ${selectedCollectionStatus}. DetailsLoaded: ${detailsLoaded}. isCollectedForStyling: ${isConsideredFullyCollectedForStyling}. HasSelectedDetails: ${hasAnySelectedVersionsWithDetails}. ShowItemBasedOnFilter: ${showItemBasedOnCollectionFilter}. NewDisplay: ${newDisplay}`);

    if (currentDisplay !== newDisplay) {
        showContainer.style.display = newDisplay;
        // console.log(`[filterAndStyleShow for ${imdbId}] Display style changed to: ${newDisplay}`);
    }

    showContainer.classList.remove('fully-collected-all-selected', 'targeted-collection-complete', 'pending-initial-filter');
    if (showItemBasedOnCollectionFilter && detailsLoaded) { 
        if (isConsideredFullyCollectedForStyling) { 
            showContainer.classList.add('fully-collected-all-selected');
        }
    }
}
