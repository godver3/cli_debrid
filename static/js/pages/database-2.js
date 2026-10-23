/* Moved from templates/database.html. */
// Import statements removed - using global functions from loaded scripts instead
// showPopup and POPUP_TYPES are available globally from notifications.js
// Database tooltips are automatically initialized through tooltips.js event delegation

// Configuration Constants
const CONFIG = {
    BATCH_SIZE: 450,              // Number of items to process in each batch
    ITEMS_PER_PAGE: window.pageData.database.per_page,  // Items per page for pagination
    MAX_PAGES_IN_DOM: 2,          // Keep only 2 pages in DOM (for windowing)
    SCROLL_TRIGGER_PX: 3000,      // Pixels from edge to trigger scroll load
    BATCH_DELAY_MS: 100,          // Delay between batch processing
    PERFORMANCE_WARNING_MS: 1000  // Warn if operation takes longer than this
};

// Performance Metrics Tracking
const performanceMetrics = {
    pageLoadTime: 0,
    filterApplyTime: 0,
    scrollLoadTime: 0,
    columnValueLoadTime: 0,

    measure(name, fn) {
        const start = performance.now();
        const result = fn();
        const duration = performance.now() - start;
        this[name] = duration;
        if (duration > CONFIG.PERFORMANCE_WARNING_MS) {
            console.warn(`[PERFORMANCE] Slow operation: ${name} took ${duration.toFixed(2)}ms`);
        } else {
            console.log(`[PERFORMANCE] ${name}: ${duration.toFixed(2)}ms`);
        }
        return result;
    },

    async measureAsync(name, fn) {
        const start = performance.now();
        const result = await fn();
        const duration = performance.now() - start;
        this[name] = duration;
        if (duration > CONFIG.PERFORMANCE_WARNING_MS) {
            console.warn(`[PERFORMANCE] Slow async operation: ${name} took ${duration.toFixed(2)}ms`);
        } else {
            console.log(`[PERFORMANCE] ${name}: ${duration.toFixed(2)}ms`);
        }
        return result;
    },

    logSummary() {
        console.log('[PERFORMANCE SUMMARY]');
        console.log(`  Page Load: ${this.pageLoadTime.toFixed(2)}ms`);
        console.log(`  Filter Apply: ${this.filterApplyTime.toFixed(2)}ms`);
        console.log(`  Scroll Load: ${this.scrollLoadTime.toFixed(2)}ms`);
        console.log(`  Column Value Load: ${this.columnValueLoadTime.toFixed(2)}ms`);
    }
};

// Cache for lazy-loaded column values (stores Promises to prevent duplicate requests)
const columnValuesCache = {};

// Fetch column values on-demand
async function fetchColumnValues(columnName) {
    // Return cached promise if request is already in flight or completed
    if (columnValuesCache[columnName]) {
        return await columnValuesCache[columnName];
    }

    // Cache the promise immediately to prevent duplicate requests
    columnValuesCache[columnName] = performanceMetrics.measureAsync(`columnValueLoad_${columnName}`, async () => {
        try {
            const response = await fetch(`/database/get_column_values/${columnName}`);
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            const errorMsg = errorData.error || `Failed to load filter values (HTTP ${response.status})`;

            if (response.status === 503 || errorData.database_locked) {
                showPopup({
                    type: POPUP_TYPES.WARNING,
                    title: 'Database Busy',
                    message: errorMsg
                });
            } else {
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error Loading Filter Values',
                    message: errorMsg
                });
            }
            return [];
        }
        const data = await response.json();
        if (data.success) {
            return data.values;
        } else {
            console.error(`Failed to fetch column values for ${columnName}:`, data.error);
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error Loading Filter Values',
                message: data.error || `Failed to load values for ${columnName}`
            });
            return [];
        }
    } catch (error) {
        console.error(`Error fetching column values for ${columnName}:`, error);
        showPopup({
            type: POPUP_TYPES.ERROR,
            title: 'Network Error',
            message: `Unable to load filter values for ${columnName}. Please check your connection and try again.`
        });
        return [];
        }
    });

    // Return the cached promise
    return await columnValuesCache[columnName];
}

function renderCell(column, displayContent) {
    if (column === 'title') return `<td class="truncate db-col-title" data-full-content="${displayContent}">${displayContent}</td>`;
    if (column === 'imdb_id' && displayContent !== 'None') {
        return `<td class="truncate" data-full-content="${displayContent}"><span class="db-imdb-link">${displayContent}</span></td>`;
    }
    if (column === 'state') {
        const stateMap = {
            'Collected': 'db-pill db-pill-collected',
            'Wanted': 'db-pill db-pill-wanted',
            'Scraping': 'db-pill db-pill-scraping',
            'Upgrading': 'db-pill db-pill-upgrading',
            'Blacklisted': 'db-pill db-pill-blacklisted',
            'Checking': 'db-pill db-pill-checking',
            'Sleeping': 'db-pill db-pill-sleeping',
            'Unreleased': 'db-pill db-pill-unreleased',
        };
        const cls = stateMap[displayContent] || 'db-pill db-pill-other';
        return `<td class="truncate" data-full-content="${displayContent}"><span class="${cls}">${displayContent}</span></td>`;
    }
    if (column === 'type') {
        const cls = displayContent === 'movie' ? 'db-pill db-pill-movie' : 'db-pill db-pill-episode';
        return `<td class="truncate" data-full-content="${displayContent}"><span class="${cls}">${displayContent}</span></td>`;
    }
    if (column === 'version' && displayContent !== 'None') {
        return `<td class="truncate" data-full-content="${displayContent}"><span class="db-pill-version">${displayContent}</span></td>`;
    }
    return `<td class="truncate" data-full-content="${displayContent}">${displayContent}</td>`;
}

document.addEventListener('DOMContentLoaded', function() {
    // Get data from window object
    const templateData = window.templateData;
    const contentSourceDisplayMap = templateData.contentSourceDisplayMap || {};

    // Initialize state variables at the top
    let currentContentType = templateData.contentType;
    if (currentContentType !== 'movie' && currentContentType !== 'episode' && currentContentType !== 'all') {
        currentContentType = 'all';
    }
    let currentLetter = window.pageData.database.current_letter;
    let lastChecked = null;
    let allSelected = false;

    // Add localStorage keys
    const STORAGE_KEYS = {
        FILTERS: 'db_filters',
        SORT_COLUMN: 'db_sort_column',
        SORT_ORDER: 'db_sort_order',
        SELECTED_COLUMNS: 'db_selected_columns',
        CONTENT_TYPE: 'db_content_type',
        FILTER_LOGIC: 'db_filter_logic',
        LETTER: 'db_current_letter', // Persist current letter
        COLUMN_WIDTHS: 'db_column_widths' // Persist custom column widths
    };

    // Function to validate and sanitize localStorage data
    function cleanupLocalStorage() {
        const savedContentType = localStorage.getItem(STORAGE_KEYS.CONTENT_TYPE);
        const savedLetter = localStorage.getItem(STORAGE_KEYS.LETTER);

        // Migrate old defaults to new defaults
        if (savedContentType === 'movie') {
            localStorage.setItem(STORAGE_KEYS.CONTENT_TYPE, 'all');
        } else if (savedContentType !== 'episode' && savedContentType !== 'all') {
            localStorage.setItem(STORAGE_KEYS.CONTENT_TYPE, 'all');
        }

        // Migrate old letter default 'A' to empty (All)
        if (savedLetter === 'A') {
            localStorage.setItem(STORAGE_KEYS.LETTER, '');
        }

        try {
            // Validate filters
            const savedFilters = localStorage.getItem(STORAGE_KEYS.FILTERS);
            if (savedFilters) {
                JSON.parse(savedFilters); // Will throw if invalid JSON
            }
        } catch (e) {
            localStorage.removeItem(STORAGE_KEYS.FILTERS);
        }

        try {
            // Validate columns
            const savedColumns = localStorage.getItem(STORAGE_KEYS.SELECTED_COLUMNS);
            if (savedColumns) {
                JSON.parse(savedColumns); // Will throw if invalid JSON
            }
        } catch (e) {
            localStorage.removeItem(STORAGE_KEYS.SELECTED_COLUMNS);
        }
    }

    // Clean up localStorage on page load
    cleanupLocalStorage();

    function createFilterRow(filter = null) {
        const row = document.createElement('div');
        row.className = 'filter-row';
        const uniqueId = `filter-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

        // Column select - use templateData.all_columns but filter out 'size' for filtering
        const filterableColumns = templateData.all_columns.filter(col => col !== 'size').sort();
        const columnSelect = document.createElement('select');
        columnSelect.id = `${uniqueId}-column`;
        columnSelect.name = `filter_column`;
        columnSelect.className = 'filter-column';

        // Use DocumentFragment for better performance
        const columnFragment = document.createDocumentFragment();
        const defaultColOption = document.createElement('option');
        defaultColOption.value = '';
        defaultColOption.textContent = 'Select Column';
        columnFragment.appendChild(defaultColOption);

        filterableColumns.forEach(column => {
            const option = document.createElement('option');
            option.value = column;
            option.textContent = column;
            if (filter && filter.column === column) option.selected = true;
            columnFragment.appendChild(option);
        });
        columnSelect.appendChild(columnFragment);

        // Operator select
        const operatorSelect = document.createElement('select');
        operatorSelect.id = `${uniqueId}-operator`;
        operatorSelect.name = `filter_operator`;
        operatorSelect.className = 'filter-operator';

        // Use DocumentFragment for better performance
        const operatorFragment = document.createDocumentFragment();
        templateData.operators.forEach(op => {
            const option = document.createElement('option');
            option.value = op.value;
            option.textContent = op.label;
            if (filter && filter.operator === op.value) option.selected = true;
            operatorFragment.appendChild(option);
        });
        operatorSelect.appendChild(operatorFragment);

        // Value input/select
        const valueContainer = document.createElement('div');
        valueContainer.className = 'filter-value-container';
        updateValueInput(valueContainer, filter?.column, filter?.value, contentSourceDisplayMap, uniqueId);

        // Remove button
        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'remove-filter';
        removeBtn.textContent = '×';
        removeBtn.onclick = () => row.remove();

        // Add elements to row
        row.appendChild(columnSelect);
        row.appendChild(operatorSelect);
        row.appendChild(valueContainer);
        row.appendChild(removeBtn);

        // Add event listeners
        columnSelect.addEventListener('change', () => {
            // Use requestAnimationFrame for smoother UI update
            requestAnimationFrame(() => {
                updateValueInput(valueContainer, columnSelect.value, '', contentSourceDisplayMap, uniqueId);
            });
        });

        return row;
    }

    async function updateValueInput(container, column, value = '', displayMap = {}, uniqueId = '') {
        container.innerHTML = '';

        // Define which columns should use dropdowns (only specific columns)
        const dropdownColumns = ['state', 'type', 'version', 'content_source', 'upgraded', 'early_release'];

        // Only fetch column values for dropdown columns
        if (dropdownColumns.includes(column)) {
            container.innerHTML = '<span>Loading...</span>';
            const columnValues = await fetchColumnValues(column);
            container.innerHTML = '';

            if (column === 'content_source' && columnValues.length > 0) {
                const select = document.createElement('select');
                select.id = `${uniqueId}-value`;
                select.name = 'filter_value';
                select.className = 'filter-value';

                // Use DocumentFragment for better performance
                const fragment = document.createDocumentFragment();

                // Add default option
                const defaultOption = document.createElement('option');
                defaultOption.value = '';
                defaultOption.textContent = 'Select Source';
                fragment.appendChild(defaultOption);

                const availableSourceIds = columnValues || [];

                if (availableSourceIds.includes("None") || availableSourceIds.includes("")) {
                    const noneOption = document.createElement('option');
                    noneOption.value = 'None';
                    noneOption.textContent = 'None';
                    if (value === "None") noneOption.selected = true;
                    fragment.appendChild(noneOption);
                }

                availableSourceIds.forEach(sourceId => {
                    if (sourceId === "None" || sourceId === "") return;

                    const displayName = displayMap[sourceId] || sourceId;
                    const option = document.createElement('option');
                    option.value = sourceId;
                    option.textContent = displayName;
                    if (value === sourceId) option.selected = true;
                    fragment.appendChild(option);
                });

                select.appendChild(fragment);
                container.appendChild(select);
            }
            else if (columnValues && columnValues.length > 0) {
                const select = document.createElement('select');
                select.id = `${uniqueId}-value`;
                select.name = 'filter_value';
                select.className = 'filter-value';

                // Use DocumentFragment for better performance
                const fragment = document.createDocumentFragment();

                // Add default option
                const defaultOption = document.createElement('option');
                defaultOption.value = '';
                defaultOption.textContent = 'Select Value';
                fragment.appendChild(defaultOption);

                columnValues.forEach(val => {
                    let displayVal = val;
                    if (column === 'state' && val === 'Final_Check') {
                        displayVal = 'Final Scrape';
                    } else if (column === 'state' && val === 'ghostlisted') {
                        displayVal = 'Ghostlisted';
                    } else if (column === 'state' && val === 'all_blacklisted') {
                        displayVal = 'All Versions Blacklisted';
                    }
                    const option = document.createElement('option');
                    option.value = val;
                    option.textContent = displayVal;
                    if (value === val) option.selected = true;
                    fragment.appendChild(option);
                });

                select.appendChild(fragment);
                container.appendChild(select);
            }
            else {
                // No values returned, use text input as fallback
                const input = document.createElement('input');
                input.type = 'text';
                input.id = `${uniqueId}-value`;
                input.name = 'filter_value';
                input.className = 'filter-value';
                input.value = value || '';
                container.appendChild(input);
            }
        }
        else {
            // For all other columns, use text input
            const input = document.createElement('input');
            input.type = 'text';
            input.id = `${uniqueId}-value`;
            input.name = 'filter_value';
            input.className = 'filter-value';
            input.value = value || '';
            container.appendChild(input);
        }
    }

    // Initialize filters from template data if they exist
    if (templateData.currentFilters && templateData.currentFilters.length > 0) {
        const filtersContainer = document.getElementById('filters-container');
        templateData.currentFilters.forEach(filter => {
            filtersContainer.appendChild(createFilterRow(filter));
        });
    }

    // Debounced save to prevent excessive localStorage writes
    let saveSettingsTimeout = null;
    function collectFilters() {
        return Array.from(document.querySelectorAll('.filter-row')).map(row => ({
            column: row.querySelector('.filter-column').value,
            operator: row.querySelector('.filter-operator').value,
            value: row.querySelector('.filter-value').value
        })).filter(f => f.column);
    }

    function saveSettings() {
        // Clear any pending save
        if (saveSettingsTimeout) {
            clearTimeout(saveSettingsTimeout);
        }

        // Debounce for 150ms
        saveSettingsTimeout = setTimeout(() => {
            const filters = collectFilters();

            const sortColumn = document.getElementById('sort-column').value;
            const sortOrder = document.getElementById('sort-order').value;
            const filterLogic = document.querySelector('input[name="filter_logic"]:checked').value;

            // Validate content type before saving
            const validContentType = (currentContentType === 'movie' || currentContentType === 'episode' || currentContentType === 'all')
                ? currentContentType
                : 'all';

            localStorage.setItem(STORAGE_KEYS.FILTERS, JSON.stringify(filters));
            localStorage.setItem(STORAGE_KEYS.SORT_COLUMN, sortColumn);
            localStorage.setItem(STORAGE_KEYS.SORT_ORDER, sortOrder);
            localStorage.setItem(STORAGE_KEYS.CONTENT_TYPE, validContentType);
            localStorage.setItem(STORAGE_KEYS.FILTER_LOGIC, filterLogic);
            localStorage.setItem(STORAGE_KEYS.LETTER, currentLetter); // Save current letter

            saveSettingsTimeout = null;
        }, 150);
    }

    // Function to restore settings from localStorage
    function restoreSettings() {
        const savedFiltersJSON = localStorage.getItem(STORAGE_KEYS.FILTERS);
        const sortColumn = localStorage.getItem(STORAGE_KEYS.SORT_COLUMN);
        const sortOrder = localStorage.getItem(STORAGE_KEYS.SORT_ORDER);
        const savedContentType = localStorage.getItem(STORAGE_KEYS.CONTENT_TYPE);
        // initializeColumnSelection now ensures SELECTED_COLUMNS in localStorage is set and valid.
        const savedColumnsJSON = localStorage.getItem(STORAGE_KEYS.SELECTED_COLUMNS);
        const savedFilterLogic = localStorage.getItem(STORAGE_KEYS.FILTER_LOGIC) || 'AND';

        let columnValuesForUpdate;
        try {
            // This should always be a valid array of strings due to initializeColumnSelection's logic
            columnValuesForUpdate = JSON.parse(savedColumnsJSON);
            if (!Array.isArray(columnValuesForUpdate) || columnValuesForUpdate.length === 0) {
                // This is a fallback for an unexpected state, initializeColumnSelection should prevent this.
                console.error("CRITICAL: Columns from localStorage were empty/invalid in restoreSettings even after initializeColumnSelection. Using hardcoded defaults.");
                columnValuesForUpdate = ['imdb_id', 'title', 'year', 'release_date', 'state', 'type', 'season_number', 'episode_number', 'collected_at', 'version'];
            }
        } catch (e) {
            console.error("CRITICAL: Error parsing columns from localStorage in restoreSettings (should have been valid). Using hardcoded defaults.", e);
            columnValuesForUpdate = ['imdb_id', 'title', 'year', 'release_date', 'state', 'type', 'season_number', 'episode_number', 'collected_at', 'version'];
        }

        const validContentType = (savedContentType === 'movie' || savedContentType === 'episode' || savedContentType === 'all')
            ? savedContentType
            : 'all';

        if (savedFiltersJSON) {
            try {
                const filters = JSON.parse(savedFiltersJSON);
                const filtersContainer = document.getElementById('filters-container');
                filtersContainer.innerHTML = ''; // Clear existing
                filters.forEach(filter => {
                    if (filter && typeof filter === 'object' && filter.hasOwnProperty('column') && filter.hasOwnProperty('operator') && filter.hasOwnProperty('value')) {
                        // Ensure the column for the filter is actually among the currently selected displayable columns
                        // or allow it if it's a valid column from all_columns (more flexible)
                        if (columnValuesForUpdate.includes(filter.column) || templateData.all_columns.includes(filter.column)) {
                       filtersContainer.appendChild(createFilterRow(filter));
                        } else {
                            console.warn(`Skipping saved filter for column "${filter.column}" as it's not currently selected or available.`);
                        }
                    } else {
                         console.warn('Skipping invalid filter structure from localStorage:', filter);
                    }
                });
            } catch (e) {
                 console.error("Error parsing saved filters from localStorage:", e);
                 localStorage.removeItem(STORAGE_KEYS.FILTERS); // Clean up invalid entry
            }
        }

        if (sortColumn) document.getElementById('sort-column').value = sortColumn;
        if (sortOrder) document.getElementById('sort-order').value = sortOrder;

        const logicRadio = document.querySelector(`input[name="filter_logic"][value="${savedFilterLogic}"]`);
        if (logicRadio) logicRadio.checked = true;
        else document.getElementById('filter-logic-and').checked = true;

        currentContentType = validContentType;
        document.querySelectorAll('.content-type-link').forEach(link => {
            link.classList.toggle('active', link.getAttribute('data-content-type') === validContentType);
        });
        if (validContentType !== savedContentType) {
            localStorage.setItem(STORAGE_KEYS.CONTENT_TYPE, validContentType);
        }

        // The updateContent call below is removed to prevent the second AJAX load.
        // The UI controls are now set. The displayed data is from the initial server render.
        // If these differ, the user can click "Apply" or "Update View" to synchronize.
    }

    // Add filter button handler with throttling to prevent rapid clicks from locking up
    let isAddingFilter = false;
    document.getElementById('add-filter').addEventListener('click', () => {
        if (isAddingFilter) return; // Prevent rapid additions
        isAddingFilter = true;

        requestAnimationFrame(() => {
            document.getElementById('filters-container').appendChild(createFilterRow());
            // Reset flag after a short delay
            setTimeout(() => {
                isAddingFilter = false;
            }, 100);
        });
    });

    // Quick filter buttons handler with throttling
    let isAddingQuickFilter = false;
    document.querySelectorAll('.quick-filter').forEach(button => {
        button.addEventListener('click', () => {
            if (isAddingQuickFilter) return; // Prevent rapid additions
            isAddingQuickFilter = true;

            const column = button.getAttribute('data-column');
            requestAnimationFrame(() => {
                const newRow = createFilterRow({ column: column, operator: 'contains', value: '' });
                document.getElementById('filters-container').appendChild(newRow);
                // Reset flag after a short delay
                setTimeout(() => {
                    isAddingQuickFilter = false;
                }, 100);
            });
        });
    });

    // Filter logic radio buttons - submit form on change
    document.querySelectorAll('input[name="filter_logic"]').forEach(radio => {
        radio.addEventListener('change', function() {
            const filterForm = document.getElementById('filter-form');
            if (filterForm) {
                saveSettings();
                filterForm.requestSubmit();
            }
        });
    });

    // Sort Column/Order Change - update content on change
    const sortColumnSelect = document.getElementById('sort-column');
    const sortOrderSelect = document.getElementById('sort-order');
    const handleSortChange = () => {
        saveSettings();
        const currentFilters = collectFilters();
        const currentSortColumn = document.getElementById('sort-column').value;
        const currentSortOrder = document.getElementById('sort-order').value;
        const currentFilterLogic = document.querySelector('input[name="filter_logic"]:checked').value || 'AND';
        const currentSelectedColumns = Array.from(document.getElementById('selected-columns').options).map(option => option.value);
        const params = {
            content_type: currentContentType || 'all',
            letter: currentLetter || '',
            selected_columns: JSON.stringify(currentSelectedColumns),
            filter_logic: currentFilterLogic
        };
        if (currentFilters.length > 0) params.filters = JSON.stringify(currentFilters);
        if (currentSortColumn) { params.sort_column = currentSortColumn; params.sort_order = currentSortOrder; }
        updateContent(window.pageData.database.database_index_url, params);
    };
    if (sortColumnSelect) sortColumnSelect.addEventListener('change', handleSortChange);
    if (sortOrderSelect) sortOrderSelect.addEventListener('change', handleSortChange);

    // Column header sort click
    document.addEventListener('click', function(e) {
        const th = e.target.closest('.table-container th[data-col]');
        if (!th) return;
        const col = th.getAttribute('data-col');
        const sortColEl = document.getElementById('sort-column');
        const sortOrdEl = document.getElementById('sort-order');
        if (!sortColEl || !sortOrdEl) return;
        if (sortColEl.value === col) {
            sortOrdEl.value = sortOrdEl.value === 'asc' ? 'desc' : 'asc';
        } else {
            sortColEl.value = col;
            sortOrdEl.value = 'asc';
        }
        handleSortChange();
    });

    // Filter form submit (no page reset)
        const filterForm = document.getElementById('filter-form');
        if (filterForm) {
            filterForm.addEventListener('submit', function(e) {
                e.preventDefault();
                saveSettings();

            // --- Corrected filter extraction ---
                const filterRows = document.querySelectorAll('.filter-row');
            const filters = Array.from(filterRows).map(row => {
                return { // Explicit return
                    column: row.querySelector('.filter-column').value,
                    operator: row.querySelector('.filter-operator').value,
                    value: row.querySelector('.filter-value')?.value || ''
                };
            }).filter(f => f.column);
            // --- End correction ---

            // --- Get other parameters ---
                const sortColumn = document.getElementById('sort-column').value;
                const sortOrder = document.getElementById('sort-order').value;
                const filterLogic = document.querySelector('input[name="filter_logic"]:checked')?.value || 'AND';
            const selectedColumnValues = Array.from(document.getElementById('selected-columns').options).map(option => option.value);

            // --- Construct params ---
                const params = {
                    filters: JSON.stringify(filters),
                    sort_column: sortColumn,
                    sort_order: sortOrder,
                    content_type: currentContentType || 'all',
                    selected_columns: JSON.stringify(selectedColumnValues),
                    filter_logic: filterLogic,
                    letter: currentLetter || ''
                };

            // --- Call updateContent ---
                updateContent(window.pageData.database.database_index_url, params);

        }); // Close addEventListener
    } // Close if (filterForm)

    // Clear filter button (no page reset)
        document.getElementById('clear-filter-sort').addEventListener('click', function(e) {
            e.preventDefault();
        clearFilterAndSort();
    });

    // Revert clearFilterAndSort
    function clearFilterAndSort() {
            localStorage.removeItem(STORAGE_KEYS.FILTERS);
            localStorage.removeItem(STORAGE_KEYS.SORT_COLUMN);
            localStorage.removeItem(STORAGE_KEYS.SORT_ORDER);
            localStorage.removeItem(STORAGE_KEYS.FILTER_LOGIC);

            document.getElementById('filters-container').innerHTML = '';

        const sortColumn = document.getElementById('sort-column');
        const sortOrder = document.getElementById('sort-order');
        if (sortColumn) {
            sortColumn.value = '';
        }
        if (sortOrder) {
            sortOrder.value = 'asc';
        }

            currentContentType = 'all';
            currentLetter = '';

        localStorage.setItem(STORAGE_KEYS.CONTENT_TYPE, 'all');
        localStorage.setItem(STORAGE_KEYS.LETTER, '');

        const contentTypeLinks = document.querySelectorAll('.content-type-link');
        contentTypeLinks.forEach(function(link) {
            if (link.getAttribute('data-content-type') === 'all') {
                link.classList.add('active');
                    } else {
                link.classList.remove('active');
            }
        });

        const paginationLinks = document.querySelectorAll('.pagination-link');
        paginationLinks.forEach(function(link) {
            if (link.getAttribute('data-letter') === '') {
                link.classList.add('active');
            } else {
                link.classList.remove('active');
            }
        });

        updateContent(window.pageData.database.database_index_url, {
            content_type: 'all',
            letter: '',
            filter_logic: 'AND'
        });
    }

    // Add CSS styles for the new filter UI
    const style = document.createElement('style');
    style.textContent = `
        #filters-container {
            margin-bottom: 1rem;
        }
        .filter-row {
            display: flex;
            gap: 1rem;
            margin-bottom: 0.5rem;
            align-items: center;
        }
        .filter-column, .filter-operator, .filter-value {
            padding: 0.25rem;
            border: 1px solid #ccc;
            border-radius: 4px;
        }
        .remove-filter {
            background: #ff4444;
            color: white;
            border: none;
            border-radius: 50%;
            width: 24px;
            height: 24px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .remove-filter:hover {
            background: #cc0000;
        }
        .filter-section {
            margin-bottom: 1rem;
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
            align-items: center;
        }
        #add-filter {
            background: #007bff;
            color: white;
            font-weight: 500;
        }
        #add-filter:hover {
            background: #0056b3;
        }
        .quick-filter {
            background: #6c757d;
            color: white;
            font-size: 0.85rem;
            padding: 0.35rem 0.75rem;
        }
        .quick-filter:hover {
            background: #5a6268;
        }
        .sort-container {
            display: flex;
            gap: 1rem;
            align-items: center;
        }
        .filter-logic-section {
            margin-top: 1rem;
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .filter-logic-section label {
            margin-right: 0.5rem;
        }
        .filter-logic-section input[type="radio"] {
            margin-right: 0.25rem;
        }
    `;
    document.head.appendChild(style);

    // Call restoreSettings when the page loads
    restoreSettings();

    // Add tap handlers for mobile devices
    function isMobileDevice() {
        const hasTouchScreen = (
            'ontouchstart' in window ||
            navigator.maxTouchPoints > 0 ||
            navigator.msMaxTouchPoints > 0
        );
        const isMobileViewport = window.innerWidth <= 768;
        return hasTouchScreen && isMobileViewport;
    }

    function copyToClipboard(text) {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        document.body.appendChild(textarea);
        textarea.select();
        try {
            document.execCommand('copy');
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                message: 'Content copied to clipboard!',
                    title: 'Success',
                autoClose: 2000
            });
        } catch (err) {
            showPopup({
                type: POPUP_TYPES.ERROR,
                message: 'Failed to copy content',
                title: 'Error'
            });
        }
        document.body.removeChild(textarea);
    }

    // Only add mobile functionality if on a mobile device
    if (isMobileDevice()) {
        // Add pointer cursor to all truncated cells
        document.querySelectorAll('.truncate').forEach(cell => {
            cell.classList.add('mobile-clickable');
        });

        // Use event delegation for cell clicks
        document.querySelector('.table-container').addEventListener('click', function(e) {
            const cell = e.target.closest('.truncate');
            if (cell && !e.target.closest('button')) {
                const content = cell.getAttribute('data-full-content');
            showPopup({
                        type: POPUP_TYPES.INFO,
                        title: 'Cell Content',
                        message: `<div style="word-break: break-word;">${content}</div>`,
                    });
                }
            });
    }

    const availableColumns = document.getElementById('available-columns');
    const selectedColumns = document.getElementById('selected-columns');

    // Only run the column selection logic if both select elements exist
    if (availableColumns && selectedColumns) {
        const addColumnBtn = document.getElementById('add-column');
        const removeColumnBtn = document.getElementById('remove-column');
        const toggleColumnSelectorBtn = document.getElementById('toggle-column-selector');
        const columnSelector = document.getElementById('column-selector');
        const updateColumnsBtn = document.getElementById('update-columns');

        const defaultColumns = ['imdb_id', 'title', 'year', 'release_date', 'state', 'type', 'season_number', 'episode_number', 'collected_at', 'version'];

        const absoluteOrder = [
            'id', 'imdb_id', 'tmdb_id', 'title', 'year', 'release_date', 'state', 'type',
            'episode_title', 'season_number', 'episode_number', 'airtime', 'collected_at', 'version'
        ];

        function sortOptionsWithAbsoluteOrder(selectElement) {
            const options = Array.from(selectElement.options);
            options.sort((a, b) => {
                const indexA = absoluteOrder.indexOf(a.value.toLowerCase());
                const indexB = absoluteOrder.indexOf(b.value.toLowerCase());

                if (indexA !== -1 && indexB !== -1) {
                    return indexA - indexB;
                } else if (indexA !== -1) {
                    return -1;
                } else if (indexB !== -1) {
                    return 1;
                } else {
                    return a.text.localeCompare(b.text);
                }
            });

            // Clear and re-add options in sorted order
            while (selectElement.firstChild) {
                selectElement.removeChild(selectElement.firstChild);
            }
            options.forEach(option => selectElement.appendChild(option));
        }

        function moveOptions(fromSelect, toSelect) {
            Array.from(fromSelect.selectedOptions).forEach(option => {
                const newOption = option.cloneNode(true);
                toSelect.appendChild(newOption);
                fromSelect.removeChild(option);
            });
            // Only sort the available-columns list; selected-columns order is user-defined
            const availEl = document.getElementById('available-columns');
            if (fromSelect === availEl) sortOptionsWithAbsoluteOrder(fromSelect);
            if (toSelect === availEl) sortOptionsWithAbsoluteOrder(toSelect);
        }

        function initializeColumnSelection() {
            const defaultColumns = ['imdb_id', 'title', 'year', 'release_date', 'state', 'type', 'season_number', 'episode_number', 'collected_at', 'version'];
            const savedColumnsJSON = localStorage.getItem(STORAGE_KEYS.SELECTED_COLUMNS);
            let columnsToUse;
            let sourceOfColumns = "unknown";

            if (savedColumnsJSON) {
                try {
                    const parsedColumns = JSON.parse(savedColumnsJSON);
                    if (Array.isArray(parsedColumns) && parsedColumns.length > 0) {
                        // Validate that these columns actually exist in all_columns
                        const validSavedColumns = parsedColumns.filter(col => templateData.all_columns.includes(col));
                        if (validSavedColumns.length === parsedColumns.length) {
                            columnsToUse = validSavedColumns;
                            sourceOfColumns = "localStorage (valid)";
                        } else {
                            console.warn("Some saved columns from localStorage were invalid, falling back to JS defaults.");
                            columnsToUse = defaultColumns;
                            sourceOfColumns = "js_defaults_after_invalid_localstorage_entries";
                            localStorage.setItem(STORAGE_KEYS.SELECTED_COLUMNS, JSON.stringify(columnsToUse));
                        }
                } else {
                        console.warn("Invalid or empty columns array from localStorage, using JS defaults.");
                        columnsToUse = defaultColumns;
                        sourceOfColumns = "js_defaults_after_empty_or_invalid_array_localstorage";
                        localStorage.setItem(STORAGE_KEYS.SELECTED_COLUMNS, JSON.stringify(columnsToUse));
                    }
                } catch (e) {
                    console.warn("Error parsing columns from localStorage, using JS defaults.", e);
                    columnsToUse = defaultColumns;
                    sourceOfColumns = "js_defaults_after_parse_error_localstorage";
                    localStorage.setItem(STORAGE_KEYS.SELECTED_COLUMNS, JSON.stringify(columnsToUse));
                }
            } else {
                columnsToUse = defaultColumns;
                sourceOfColumns = "js_defaults_due_to_empty_localstorage";
                localStorage.setItem(STORAGE_KEYS.SELECTED_COLUMNS, JSON.stringify(columnsToUse));
            }

            // Clear both select elements
            availableColumns.innerHTML = '';
            selectedColumns.innerHTML = '';

            // Add all columns to available first
            templateData.all_columns.forEach(column => {
                const option = document.createElement('option');
                option.value = column;
                option.textContent = column;
                availableColumns.appendChild(option);
            });

            // Move selected columns (columnsToUse) to the 'selectedColumns' select element
            columnsToUse.forEach(column => {
                const option = availableColumns.querySelector(`option[value="${column}"]`);
                if (option) {
                    selectedColumns.appendChild(option.cloneNode(true)); // Add to selected
                    // Do not remove from available if you want to keep them there for re-selection
                } else {
                    console.warn(`Default column "${column}" not found in all_columns from templateData.`);
                }
            });

            // Now, remove the selected columns from the available list
            Array.from(selectedColumns.options).forEach(selectedOption => {
                const availableOption = availableColumns.querySelector(`option[value="${selectedOption.value}"]`);
                if (availableOption) {
                    availableColumns.removeChild(availableOption);
                }
            });


            sortOptionsWithAbsoluteOrder(availableColumns);
            // Do NOT sort selectedColumns — user's custom order must be preserved
        }

        // Add event listeners for column selection
        addColumnBtn.addEventListener('click', () => moveOptions(availableColumns, selectedColumns));
        removeColumnBtn.addEventListener('click', () => moveOptions(selectedColumns, availableColumns));

        // Add event listeners for column reordering
        const moveUpBtn = document.getElementById('move-up');
        const moveDownBtn = document.getElementById('move-down');

        moveUpBtn.addEventListener('click', () => {
            const selectedIndex = selectedColumns.selectedIndex;
            if (selectedIndex > 0) {
                const option = selectedColumns.options[selectedIndex];
                const prevOption = selectedColumns.options[selectedIndex - 1];
                selectedColumns.insertBefore(option, prevOption);
                option.selected = true;
            }
        });

        moveDownBtn.addEventListener('click', () => {
            const selectedIndex = selectedColumns.selectedIndex;
            if (selectedIndex >= 0 && selectedIndex < selectedColumns.options.length - 1) {
                const option = selectedColumns.options[selectedIndex];
                const nextOption = selectedColumns.options[selectedIndex + 1];
                selectedColumns.insertBefore(nextOption, option);
                option.selected = true;
            }
        });

        toggleColumnSelectorBtn.addEventListener('click', function() {
            columnSelector.classList.toggle('hidden');
            this.classList.toggle('active');
            this.textContent = columnSelector.classList.contains('hidden') 
                ? 'Select Columns to Display' 
                : 'Hide Column Selector';
        });

        // Update columns functionality
        updateColumnsBtn.addEventListener('click', function() {
            const selectedColumnValues = Array.from(selectedColumns.options).map(option => option.value);

            if (selectedColumnValues.length === 0) {
                    showPopup({
                        type: POPUP_TYPES.ERROR,
                        title: 'Error',
                    message: 'Please select at least one column to display.',
                });
                return;
            }

            // Save selected columns to localStorage
            localStorage.setItem(STORAGE_KEYS.SELECTED_COLUMNS, JSON.stringify(selectedColumnValues));
            saveSettings(); // Saves other relevant settings like filters, sort, content type, logic

            // Hide the column selector
            columnSelector.classList.add('hidden');
                 toggleColumnSelectorBtn.classList.remove('active');
                 toggleColumnSelectorBtn.textContent = 'Select Columns to Display';

            // --- START CHANGE ---
            // Gather all current settings to refresh the view correctly
            const currentFilters = JSON.parse(localStorage.getItem(STORAGE_KEYS.FILTERS) || '[]');
            const currentSortColumn = localStorage.getItem(STORAGE_KEYS.SORT_COLUMN) || '';
            const currentSortOrder = localStorage.getItem(STORAGE_KEYS.SORT_ORDER) || 'asc';
            const currentFilterLogic = localStorage.getItem(STORAGE_KEYS.FILTER_LOGIC) || 'AND';
            // Ensure currentContentType is up-to-date from localStorage
            const savedContentType = localStorage.getItem(STORAGE_KEYS.CONTENT_TYPE) || 'all';
            // Use the globally tracked currentContentType, validated against saved value
            currentContentType = (savedContentType === 'movie' || savedContentType === 'episode' || savedContentType === 'all') ? savedContentType : 'all';

            const params = {
                selected_columns: JSON.stringify(selectedColumnValues), // Use the newly selected columns
                filters: JSON.stringify(currentFilters),              // Include current filters
                sort_column: currentSortColumn,                       // Include current sort column
                sort_order: currentSortOrder,                         // Include current sort order
                filter_logic: currentFilterLogic,                     // Include current filter logic
                content_type: currentContentType,                     // Include current content type
                letter: currentLetter || '',                          // Include current letter pagination state
            };

            // Call updateContent to refresh the table view with new columns and existing settings
            updateContent(window.pageData.database.database_index_url, params);
            // --- END CHANGE ---
        });

        // Initialize column selection
        initializeColumnSelection();
    }

    function setUpdatingContent(isUpdating) {
        if (isUpdating) {
            // Show loading with progress - timer will start automatically
            if (Loading && typeof Loading.show === 'function') {
                Loading.show('Loading...', null, true, false);
            }
        } else {
            if (Loading && typeof Loading.hide === 'function') {
                Loading.hide();
            }
        }
    }

    // Helper function to fetch with progress tracking
    async function fetchWithProgress(url, onProgress = null) {
        const response = await fetch(url);

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const contentLength = response.headers.get('content-length');

        // If no content length, keep using automatic timer
        if (!contentLength) {
            return response.json();
        }

        // Don't stop the timer - let it continue running
        // We'll override with real progress if chunks arrive gradually
        const total = parseInt(contentLength, 10);
        let loaded = 0;
        let chunkCount = 0;

        const reader = response.body.getReader();
        const stream = new ReadableStream({
            start(controller) {
                function push() {
                    reader.read().then(({ done, value }) => {
                        if (done) {
                            controller.close();
                            // Only set to 100% if we had multiple chunks (gradual download)
                            if (chunkCount > 1 && onProgress) {
                                onProgress(100);
                            }
                            // For single-chunk responses, let the timer complete naturally
                            return;
                        }

                        chunkCount++;
                        loaded += value.byteLength;
                        const progress = (loaded / total) * 100;

                        // Only override timer progress if we're getting multiple chunks
                        if (chunkCount > 1 && onProgress) {
                            onProgress(progress);
                        }

                        controller.enqueue(value);
                        push();
                    });
                }
                push();
            }
        });

        return new Response(stream).json();
    }

    function renderTable(items, selectedColumns) {
        // Load saved column widths
        let savedWidths = {};
        try {
            const widthsData = localStorage.getItem('db_column_widths');
            if (widthsData) {
                savedWidths = JSON.parse(widthsData);
            }
        } catch (e) {
            console.error('Error loading saved column widths:', e);
        }

        let tableHTML = '<table><thead><tr>';
        let columnIndex = 0;

        const activeSortCol = document.getElementById('sort-column')?.value || '';
        const activeSortOrder = document.getElementById('sort-order')?.value || 'asc';

        // Helper to create header with optional width and sort indicator
        const createHeader = (text, index, colKey = null) => {
            const width = savedWidths[index];
            const style = width ? ` style="width: ${width}px; min-width: ${width}px;"` : '';
            if (colKey) {
                const isActive = colKey === activeSortCol;
                const activeClass = isActive ? ' class="db-sort-active"' : '';
                const icon = isActive ? (activeSortOrder === 'desc' ? '▼' : '▲') : '⇅';
                return `<th${style} data-col="${colKey}"${activeClass}>${text}<span class="db-sort-icon">${icon}</span></th>`;
            }
            return `<th${style}>${text}</th>`;
        };

        // Unified column order: Select | # | Actions
        tableHTML += createHeader('Select', columnIndex++);
        tableHTML += `<th class="db-col-rownum">#</th>`;
        columnIndex++;
        tableHTML += createHeader('Actions', columnIndex++);

        selectedColumns.forEach(column => {
            let displayName = column;
            if (column === 'season_number') displayName = 'S#';
            else if (column === 'episode_number') displayName = 'E#';
            else if (column === 'size') displayName = 'Size (GB)';
            tableHTML += createHeader(displayName, columnIndex++, column);
        });
        tableHTML += '</tr></thead><tbody>';

        const deleteSVG = `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>`;
        const refreshSVG = `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>`;
        const historySVG = `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>`;

        items.forEach((item, index) => {
            const rowNumber = index + 1;
            tableHTML += `<tr data-page="1">`;
            tableHTML += `<td><input type="checkbox" class="item-select" data-item-id="${item['id']}"></td>`;
            tableHTML += `<td class="db-row-num">${rowNumber}</td>`;
            tableHTML += `<td><span style="display:flex;gap:4px;align-items:center">` +
                `<button class="db-action-btn db-action-btn-delete delete-item" data-item-id="${item['id']}" title="Delete">${deleteSVG}</button>` +
                `<button class="db-action-btn db-action-btn-refresh rescrape-item" data-item-id="${item['id']}" title="Rescrape">${refreshSVG}</button>` +
                `<button class="db-action-btn db-action-btn-history history-item" data-item-id="${item['id']}" title="State history">${historySVG}</button>` +
                `</span></td>`;
            selectedColumns.forEach(column => {
                let cellContentKey = column;
                let displayContent;

                if (column === 'size') {
                    cellContentKey = 'size_gb';
                    const rawSize = item[cellContentKey];
                    if (rawSize === null || rawSize === undefined || typeof rawSize !== 'number') {
                        displayContent = 'N/A';
                    } else {
                        displayContent = rawSize.toFixed(2);
                    }
                } else {
                    const rawCellContent = item[cellContentKey];
                    displayContent = (rawCellContent === null || rawCellContent === undefined || String(rawCellContent).trim() === '')
                        ? 'None'
                        : String(rawCellContent).replace(/"/g, '&quot;');

                    if (column === 'state' && displayContent === 'Final_Check') {
                        displayContent = 'Final Scrape';
                    } else if (column === 'state' && displayContent === 'ghostlisted') {
                        displayContent = 'Ghostlisted';
                    } else if (column === 'state' && displayContent === 'all_blacklisted') {
                        displayContent = 'All Versions Blacklisted';
                    }
                }
                tableHTML += renderCell(column, displayContent);
            });
            tableHTML += '</tr>';
        });

        tableHTML += '</tbody></table>';
        return tableHTML;
    }

    function updateContent(url, params = {}) {
        const perfStart = performance.now();

        // Validate content type
        if (params.content_type !== 'movie' && params.content_type !== 'episode' && params.content_type !== 'all') {
            params.content_type = 'all';
        }

        // Ensure content type is always set
        params.content_type = params.content_type || currentContentType || 'all';

        // Validate again after potentially using currentContentType
        if (params.content_type !== 'movie' && params.content_type !== 'episode' && params.content_type !== 'all') {
            params.content_type = 'all';
        }

        // Update currentContentType with what we're using
        currentContentType = params.content_type;

        // Ensure letter is always set (empty string means "All")
        params.letter = (params.letter !== undefined) ? params.letter : (currentLetter !== undefined ? currentLetter : '');

        // Add selected columns if not present
        if (!params.selected_columns) {
            const selectedColumnValues = Array.from(document.getElementById('selected-columns').options).map(option => option.value);
            params.selected_columns = JSON.stringify(selectedColumnValues);
        }

        // Add default sort parameters if not present
        if (!params.sort_column) params.sort_column = window.pageData.database.sort_column;
        if (!params.sort_order) params.sort_order = window.pageData.database.sort_order;

        // Add filter logic if not present
        if (!params.filter_logic) {
            params.filter_logic = document.querySelector('input[name="filter_logic"]:checked')?.value || 'AND';
        }

        // Add ajax parameter
        params.ajax = '1';

        const queryString = new URLSearchParams(params).toString();
        const fetchUrl = `${window.pageData.database.database_index_url}?${queryString}`; // Use base URL

        setUpdatingContent(true);

        fetchWithProgress(fetchUrl, (progress) => {
            if (Loading && typeof Loading.setProgress === 'function') {
                Loading.setProgress(progress);
            }
        })
            .then(data => {
                if (data.error) { // Should be caught by the !response.ok above, but good as a fallback
                    throw new Error(data.error);
                }

                const tableContainer = document.querySelector('.table-container');
                if (tableContainer) {
                    tableContainer.innerHTML = data.items.length > 0
                        ? renderTable(data.items, data.selected_columns)
                        : '<p>No items found in the database.</p>';
                } else {
                    console.error('Table container not found');
                }

                const paginationElement = document.querySelector('.pagination');
                if (paginationElement) {
                    paginationElement.innerHTML = renderPagination(data);
                    // Re-attach letter pagination listeners after rebuilding
                    if (typeof window.attachLetterPaginationListeners === 'function') {
                        window.attachLetterPaginationListeners();
                    }
                }

                document.querySelectorAll('.content-type-filter').forEach(el => {
                    el.innerHTML = renderContentTypeFilter(data);
                });

                // IMPORTANT: Check for column changes BEFORE updating templateData.all_columns!
                const filterableColumns = data.all_columns.filter(col => col !== 'size').sort();
                const previousAllColumns = templateData.all_columns ?
                    templateData.all_columns.filter(col => col !== 'size').sort() : [];
                const columnsChanged = JSON.stringify(filterableColumns) !== JSON.stringify(previousAllColumns);

                // Update other states
                currentContentType = data.content_type;
                currentLetter = data.current_letter;
                templateData.selectedColumns = data.selected_columns;
                templateData.all_columns = data.all_columns;  // Track all available columns for change detection

                // Sync the #selected-columns panel select with what the server returned.
                // This keeps sort/filter requests consistent — they read from this select.
                const selColEl = document.getElementById('selected-columns');
                const availColEl = document.getElementById('available-columns');
                if (selColEl && availColEl && data.selected_columns) {
                    // Rebuild both lists from all_columns, preserving server-returned order for selected
                    selColEl.innerHTML = '';
                    availColEl.innerHTML = '';
                    data.selected_columns.forEach(col => {
                        const opt = document.createElement('option');
                        opt.value = col;
                        opt.textContent = col;
                        selColEl.appendChild(opt);
                    });
                    data.all_columns.forEach(col => {
                        if (!data.selected_columns.includes(col)) {
                            const opt = document.createElement('option');
                            opt.value = col;
                            opt.textContent = col;
                            availColEl.appendChild(opt);
                        }
                    });
                    localStorage.setItem(STORAGE_KEYS.SELECTED_COLUMNS, JSON.stringify(data.selected_columns));
                }

                // Reset infinite scroll state when table is rebuilt
                if (typeof window.resetInfiniteScroll === 'function') {
                    window.resetInfiniteScroll(data.total_pages);
                }
                templateData.columnValues = data.column_values;
                templateData.contentSourceDisplayMap = data.content_source_display_map || {};

                // Update sort column dropdown with new selected columns
                const sortColumnSelect = document.getElementById('sort-column');
                if (sortColumnSelect) {
                    const currentSortValueInDropdown = sortColumnSelect.value;
                    sortColumnSelect.innerHTML = `<option value="">None</option>`; // Clear existing

                    // data.all_columns from JSON response (includes 'size')
                    data.all_columns.forEach(column => {
                        const option = document.createElement('option');
                        option.value = column;
                        option.textContent = column;
                        // data.sort_column is the actual sorted column from backend
                        if (column === data.sort_column) {
                            option.selected = true;
                        }
                        sortColumnSelect.appendChild(option);
                    });

                    // Ensure the dropdown reflects the actual sorted column, or a sensible default
                    if (data.sort_column) {
                        sortColumnSelect.value = data.sort_column;
                    } else if (data.all_columns.includes(currentSortValueInDropdown)) {
                        sortColumnSelect.value = currentSortValueInDropdown; // Preserve if valid
                    } else {
                        sortColumnSelect.value = ""; // Default to "None"
                    }
                }

                // Only update filter dropdowns if the available columns have actually changed

                if (columnsChanged) {
                    console.log('[DEBUG] Columns changed, updating filter dropdowns');
                    document.querySelectorAll('.filter-row').forEach(row => {
                        const columnSelect = row.querySelector('.filter-column');
                        const valueContainer = row.querySelector('.filter-value-container');
                        const currentValueInput = valueContainer.querySelector('.filter-value');
                        const currentFilterValue = currentValueInput ? currentValueInput.value : '';

                        // Store the column that was selected in this filter row *before* we update its options
                        const previouslySelectedColumnInThisFilter = columnSelect.value;

                        // Update column dropdown options for this filter row
                        columnSelect.innerHTML = `
                            <option value="">Select Column</option>
                            ${filterableColumns.map(column => `
                                <option value="${column}" ${previouslySelectedColumnInThisFilter === column ? 'selected' : ''}>
                                    ${column}
                                </option>
                            `).join('')}
                        `;

                        // Now, columnSelect.value reflects the actual selected column *after* the options were updated.
                        // If previouslySelectedColumnInThisFilter is no longer in data.selected_columns,
                        // columnSelect.value will be different (e.g., "" or the first available option).

                        let valueToUseForFilterInput = currentFilterValue;
                        if (columnSelect.value !== previouslySelectedColumnInThisFilter) {
                            // The column for this filter row has effectively changed because the old one
                            // is no longer available or selected. Reset the filter value.
                            valueToUseForFilterInput = '';
                        }

                        // Re-trigger value input update based on the *actual current* selected column in the dropdown
                        // and the potentially reset filter value.
                        updateValueInput(valueContainer, columnSelect.value, valueToUseForFilterInput, templateData.contentSourceDisplayMap);
                    });
                } else {
                    console.log('[DEBUG] Columns unchanged, skipping filter dropdown updates');
                }

                // Update UI to reflect current state
                updateUIForCurrentState(data);

                // Reattach event listeners
                attachEventListeners();
                attachDeleteListeners();
                attachRescrapeListeners();

                // Reinitialize column resize handles
                if (typeof window.initColumnResize === 'function') {
                    window.initColumnResize();
                }
                // Reinitialize floating header
                if (typeof window.reinitFloatHeader === 'function') {
                    window.reinitFloatHeader();
                }

                // Tooltips are automatically handled by event delegation in tooltips.js

                // Update result count
                const resultCountElement = document.querySelector('.result-count');
                if (resultCountElement && typeof data.result_count !== "undefined") {
                    resultCountElement.innerHTML = `Showing ${data.result_count} results <span class="unique-count">(Unique: ${data.unique_result_count})</span> | Total pages: ${data.total_pages} | Total items: ${data.total_count}`;
                }
                // Tangerine: update showing count in tabs row
                const dbShowingCount = document.getElementById('db-showing-count');
                if (dbShowingCount && typeof data.result_count !== "undefined") {
                    dbShowingCount.textContent = `Showing ${data.result_count} of ${data.total_count} results`;
                }
                // Tangerine: update top stat boxes to reflect current view
                if (typeof data.total_count !== "undefined") {
                    const statVals = document.querySelectorAll('.db-stat-boxes .db-stat-val');
                    if (statVals[0]) statVals[0].textContent = data.total_count;
                    if (statVals[1]) statVals[1].textContent = data.unique_result_count;
                    if (statVals[2]) statVals[2].textContent = data.total_pages;
                }
            })
            .catch(error => {
                console.error('Error updating content:', error);
                let popupMessage = 'Failed to update database content: ' + error.message;
                let showRetryButton = false;
                let retryAction = null;

                // Check if the error is a database lock error
                // The backend error message is "Database error: database is locked"
                if (error.message && (error.message.toLowerCase().includes('database is locked'))) {
                    popupMessage = 'The database is currently busy. Would you like to try again?';
                    showRetryButton = true;
                    // Capture current url and params for retry.
                    // Ensure 'url' and 'params' are the ones used for the current fetch.
                    // The 'fetchUrl' is already constructed, but updateContent itself takes 'url' and 'params'
                    // which are more fundamental to reconstruct the call.
                    retryAction = () => updateContent(window.pageData.database.database_index_url, params);
                }

                const popupOptions = {
                    type: POPUP_TYPES.ERROR,
                    title: 'Database Busy',
                    message: popupMessage,
                };

                if (showRetryButton && retryAction) {
                    popupOptions.type = POPUP_TYPES.CONFIRM; // Change type to CONFIRM to get buttons
                    popupOptions.confirmText = 'Retry';
                    popupOptions.onConfirm = retryAction;
                    popupOptions.cancelText = 'Close';
                    popupOptions.onCancel = () => { /* Just close the popup */ };
                } else {
                    // For non-retryable errors, perhaps just an OK button or auto-close
                    popupOptions.confirmText = 'OK'; // Or remove if not needed for simple error
                    popupOptions.onConfirm = () => { /* Just close */ };
                }

                showPopup(popupOptions);
            })
            .finally(() => {
                setUpdatingContent(false);

                // Log performance metrics
                const duration = performance.now() - perfStart;
                performanceMetrics.filterApplyTime = duration;
                if (duration > CONFIG.PERFORMANCE_WARNING_MS) {
                    console.warn(`[PERFORMANCE] Slow updateContent: ${duration.toFixed(2)}ms`);
                } else {
                    console.log(`[PERFORMANCE] updateContent: ${duration.toFixed(2)}ms`);
                }
            });
    }

    function renderPagination(data) {
        if (!data.alphabet || data.alphabet.length === 0) {
            return '<p>No pagination data available</p>';
        }

        let paginationHTML = '<a href="#" data-letter="" class="pagination-link ' + (data.current_letter === '' ? 'active' : '') + '">All</a>';
        paginationHTML += '<a href="#" data-letter="#" class="pagination-link ' + (data.current_letter === '#' ? 'active' : '') + '">#</a>';
        data.alphabet.forEach(letter => {
            paginationHTML += '<a href="#" data-letter="' + letter + '" class="pagination-link ' + (letter === data.current_letter ? 'active' : '') + '">' + letter + '</a>';
        });

        return paginationHTML;
    }

    function renderContentTypeFilter(data) {
        return `
            <a href="#" data-content-type="all" class="content-type-link ${data.content_type === 'all' ? 'active' : ''}">All</a>
            <a href="#" data-content-type="movie" class="content-type-link ${data.content_type === 'movie' ? 'active' : ''}">Movies</a>
            <a href="#" data-content-type="episode" class="content-type-link ${data.content_type === 'episode' ? 'active' : ''}">Episodes</a>
        `;
    }

    function updateUIForCurrentState(data) {
        // Update content type buttons
        document.querySelectorAll('.content-type-link').forEach(link => {
            if (link.getAttribute('data-content-type') === data.content_type) {
                link.classList.add('active');
            } else {
                link.classList.remove('active');
            }
        });

        // Update letter pagination
        document.querySelectorAll('.pagination-link').forEach(link => {
            if (link.getAttribute('data-letter') === data.current_letter) {
                link.classList.add('active');
            } else {
                link.classList.remove('active');
            }
        });

        // Update the sort selects
        const sortColumn = document.getElementById('sort-column');
        const sortOrder = document.getElementById('sort-order');
        if (sortColumn && data.sort_column) {
            sortColumn.value = data.sort_column;
        }
        if (sortOrder && data.sort_order) {
            sortOrder.value = data.sort_order;
        }

        // Update filter logic radio buttons
        const filterLogic = data.filter_logic || 'AND';
        const logicRadio = document.querySelector(`input[name="filter_logic"][value="${filterLogic}"]`);
        if (logicRadio) {
            logicRadio.checked = true;
        } else {
            console.warn(`Unexpected filter_logic value received: ${data.filter_logic}. Defaulting to AND.`);
            document.getElementById('filter-logic-and').checked = true;
        }
    }

    // Function to attach letter pagination listeners (define globally, called after DOM updates)
    window.attachLetterPaginationListeners = function() {
        const links = document.querySelectorAll('.pagination-link');

        links.forEach(link => {
            // Clone and replace to remove old listeners
            const newLink = link.cloneNode(true);
            link.parentNode.replaceChild(newLink, link);

            newLink.addEventListener('click', function(e) {
                e.preventDefault();

                currentLetter = this.getAttribute('data-letter');
                saveSettings();
                const currentFilters = collectFilters();
                const currentSortColumn = document.getElementById('sort-column').value;
                const currentSortOrder = document.getElementById('sort-order').value;
                const currentFilterLogic = document.querySelector('input[name="filter_logic"]:checked').value || 'AND';
                const currentSelectedColumns = Array.from(document.getElementById('selected-columns').options).map(option => option.value);
                const params = {
                    content_type: currentContentType || 'all',
                    letter: currentLetter,
                    selected_columns: JSON.stringify(currentSelectedColumns),
                    filter_logic: currentFilterLogic
                };
                if (currentFilters.length > 0) params.filters = JSON.stringify(currentFilters);
                if (currentSortColumn) { params.sort_column = currentSortColumn; params.sort_order = currentSortOrder; }

                updateContent(window.pageData.database.database_index_url, params);
            });
        });
    };

    function attachEventListeners() {
        const contentTypeLinks = document.querySelectorAll('.content-type-link');
        contentTypeLinks.forEach(link => {
            // Clone and replace to remove old listeners
            const newLink = link.cloneNode(true);
            link.parentNode.replaceChild(newLink, link);

            newLink.addEventListener('click', function(e) {
                e.preventDefault();
                if (!this.classList.contains('active')) {
                    const newContentType = this.getAttribute('data-content-type');
                    if (newContentType === 'movie' || newContentType === 'episode' || newContentType === 'all') {
                        currentContentType = newContentType;
                    saveSettings();
                        const currentFilters = collectFilters();
                        const currentSortColumn = document.getElementById('sort-column').value;
                        const currentSortOrder = document.getElementById('sort-order').value;
                        const currentFilterLogic = document.querySelector('input[name="filter_logic"]:checked').value || 'AND';
                        const currentSelectedColumns = Array.from(document.getElementById('selected-columns').options).map(option => option.value);
                    const params = {
                        content_type: currentContentType,
                            letter: currentLetter,
                            selected_columns: JSON.stringify(currentSelectedColumns),
                            filter_logic: currentFilterLogic
                        };
                        if (currentFilters.length > 0) params.filters = JSON.stringify(currentFilters);
                        if (currentSortColumn) { params.sort_column = currentSortColumn; params.sort_order = currentSortOrder; }
                    updateContent(window.pageData.database.database_index_url, params);
                    } else {
                        console.error('Invalid content type:', newContentType);
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            title: 'Error',
                            message: 'Invalid content type specified',
                        });
                    }
                }
            });
        });

        // Attach pagination listeners (DOM was just replaced)
        window.attachLetterPaginationListeners();
    }

    // Event delegation — one listener on the table container handles all rows
    // including dynamically added infinite scroll rows. No per-button attachment needed.
    let _delegationAttached = false;
    function attachDeleteListeners() {
        if (_delegationAttached) return;
        _delegationAttached = true;
        document.addEventListener('click', function(e) {
            const btn = e.target.closest('.delete-item');
            if (btn) {
                e.preventDefault();
                showDeleteConfirmation(btn.getAttribute('data-item-id'));
            }
        });
        document.addEventListener('click', function(e) {
            const btn = e.target.closest('.rescrape-item');
            if (btn) {
                e.preventDefault();
                showRescrapeConfirmation(btn.getAttribute('data-item-id'));
            }
        });
        document.addEventListener('click', function(e) {
            const btn = e.target.closest('.history-item');
            if (btn) {
                e.preventDefault();
                showStateHistory(btn.getAttribute('data-item-id'));
            }
        });
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    }

    async function showStateHistory(itemId) {
        let data;
        try {
            const response = await fetch(`${window.pageData.database.database_index_url}api/item_history/${encodeURIComponent(itemId)}`);
            data = await response.json();
            if (!data.success) throw new Error(data.error || 'Request failed');
        } catch (error) {
            showPopup({ type: POPUP_TYPES.ERROR, title: 'State history', message: `Could not load history: ${escapeHtml(error.message)}` });
            return;
        }
        const item = data.item;
        let label = item.title || `Item ${item.id}`;
        if (item.type === 'episode') {
            label += ` S${String(item.season_number).padStart(2, '0')}E${String(item.episode_number).padStart(2, '0')}`;
        }
        let rows = '';
        if (!data.history.length) {
            rows = '<li class="db-history-empty">No state changes recorded yet. History starts with this version of cli_debrid.</li>';
        }
        data.history.forEach(entry => {
            const from = entry.from_state ? escapeHtml(entry.from_state) : '<em>added</em>';
            let by = entry.source ? escapeHtml(entry.source) : '<em>direct database update</em>';
            let tags = '';
            if (entry.manual) tags += '<span class="db-history-tag">manual</span>';
            if (!entry.expected) tags += '<span class="db-history-tag db-history-tag-warn" title="Not a move the pipeline normally makes">unexpected</span>';
            rows += `<li class="${entry.expected ? '' : 'db-history-unexpected'}">
                <div class="db-history-line"><span>${from} &rarr; <strong>${escapeHtml(entry.to_state)}</strong></span>${tags}</div>
                <div class="db-history-meta">${escapeHtml(String(entry.changed_at).slice(0, 19))} &middot; ${by}</div>
            </li>`;
        });
        showPopup({
            type: POPUP_TYPES.INFO,
            title: `State history: ${escapeHtml(label)}`,
            message: `<div class="db-history"><div class="db-history-now">Now: <strong>${escapeHtml(item.state)}</strong>${item.version ? ` &middot; ${escapeHtml(item.version)}` : ''}</div>
                <ol class="db-history-list">${rows}</ol></div>`,
            autoClose: 0
        });
    }

    function attachRescrapeListeners() {
        // Handled by delegation in attachDeleteListeners — no-op kept for call-site compatibility
    }

    function showDeleteConfirmation(itemId) {
        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Confirm Deletion',
            message: `
                <div>Are you sure you want to delete this item?</div>
                <div style="margin-top: 10px;">
                    <label>
                        <input type="checkbox" id="blacklist-item-checkbox"> Blacklist item to avoid re-addition?
                    </label>
                </div>
            `,
            confirmText: 'Delete',
            cancelText: 'Cancel',
            onConfirm: () => {
                const blacklistItem = document.getElementById('blacklist-item-checkbox').checked;
                deleteItem(itemId, blacklistItem);
            },
            onCancel: () => {} // Do nothing on cancel
        });
    }

    function showRescrapeConfirmation(itemId) {
        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Confirm Rescrape',
            message: 'Are you sure you want to delete this item and move it to the Wanted queue for rescraping? This will delete the file from disk and remove it from Plex.',
            confirmText: 'Delete & Rescrape',
            cancelText: 'Cancel',
            onConfirm: () => rescrapeItem(itemId),
            onCancel: () => {} // Do nothing on cancel
        });
    }

    function deleteItem(itemId, blacklistItem) {
        if (Loading && typeof Loading.show === 'function') {
            Loading.show('Deleting item...', null, true, false);
        }
        fetch(window.pageData.database.database_delete_item_url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ 
                item_id: itemId,
                blacklist: blacklistItem 
            })
        })
        .then(async response => {
            if (!response.ok) {
                let errorMsg = `Default error: Network response was not ok. Status: ${response.status}`;
                let databaseLocked = false;
                try {
                    const contentType = response.headers.get("content-type");
                    if (contentType && contentType.includes("application/json")) {
                        const errData = await response.json();
                        if (errData && errData.error) {
                            errorMsg = errData.error;
                            if (errData.database_locked === true) {
                                databaseLocked = true;
                            }
                        } else if (errData) {
                            errorMsg = `Unexpected JSON error format. Status: ${response.status}`;
                        }
                    } else {
                        const textError = await response.text();
                        if (textError && textError.length < 500 && !textError.toLowerCase().includes("<html")) {
                             errorMsg = textError;
                        }
                    }
                } catch (parseError) {
                    // errorMsg remains the default one
                }
                const errorToThrow = new Error(errorMsg);
                if (databaseLocked || response.status === 503) {
                    errorToThrow.database_locked = true;
                }
                throw errorToThrow;
            }
            return response.json();
        })
        .then(data => {
            if (data.success) {
                const row = document.querySelector(`button[data-item-id="${itemId}"]`).closest('tr');
                if (row) { // Check if row exists
                    row.remove();
                }
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    title: 'Success',
                    message: 'Item deleted successfully',
                    autoClose: 3000
                });
            } else {
                const errorToThrow = new Error(data.error || 'Unknown error deleting item.');
                if (data.database_locked === true) {
                    errorToThrow.database_locked = true;
                }
                throw errorToThrow;
            }
        })
        .catch(error => {
            console.error('Error deleting item:', error);
            let popupMessage = 'An error occurred while deleting the item: ' + error.message;
            let showRetryButton = false;
            let retryAction = null;

            if (error.database_locked) {
                popupMessage = 'The database is currently busy. Would you like to try deleting again?';
                showRetryButton = true;
                retryAction = () => deleteItem(itemId, blacklistItem);
            } else if (error.message && error.message.includes(`Status: 503`)) {
                popupMessage = 'The server is temporarily unavailable (503). This might be due to a database lock. Would you like to try again?';
                showRetryButton = true;
                retryAction = () => deleteItem(itemId, blacklistItem);
            }

            const popupOptions = {
                type: POPUP_TYPES.ERROR,
                title: 'Database Busy',
                message: popupMessage,
            };
             if (showRetryButton && retryAction) {
                popupOptions.type = POPUP_TYPES.CONFIRM;
                popupOptions.confirmText = 'Retry';
                popupOptions.onConfirm = retryAction;
                popupOptions.cancelText = 'Close';
            } else {
                popupOptions.confirmText = 'OK';
            }
            showPopup(popupOptions);
        })
        .finally(() => {
            if (Loading && typeof Loading.hide === 'function') {
                Loading.hide();
            }
        });
    }

    function rescrapeItem(itemId) {
        if (Loading && typeof Loading.show === 'function') {
            Loading.show('Rescraping item...', null, true, false);
        }
        fetch(window.pageData.database.debug_rescrape_item_url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ item_id: itemId })
        })
        .then(async response => {
            if (!response.ok) {
                let errorMsg = `Default error: Network response was not ok. Status: ${response.status}`;
                let databaseLocked = false;
                try {
                    const contentType = response.headers.get("content-type");
                    if (contentType && contentType.includes("application/json")) {
                        const errData = await response.json();
                        if (errData && errData.error) {
                            errorMsg = errData.error;
                            if (errData.database_locked === true) {
                                databaseLocked = true;
                            }
                        } else if (errData) {
                            errorMsg = `Unexpected JSON error format. Status: ${response.status}`;
                        }
                    } else {
                        const textError = await response.text();
                        if (textError && textError.length < 500 && !textError.toLowerCase().includes("<html")) {
                             errorMsg = textError;
                        } else if (textError) {
                            console.warn("Error response was text, but seems like an HTML page or too long. Sticking with default status error.");
                        }
                    }
                } catch (parseError) {
                    console.error('Error parsing error response (JSON or text):', parseError);
                    // errorMsg remains the default one from above if parsing fails
                }
                const errorToThrow = new Error(errorMsg);
                if (databaseLocked || response.status === 503) {
                    errorToThrow.database_locked = true;
                }
                throw errorToThrow;
            }
            return response.json();
        })
        .then(data => {
            // This block is now only processed if response.ok was true and JSON parsing succeeded
            if (data.success) {
                const row = document.querySelector(`button[data-item-id="${itemId}"]`).closest('tr');
                if (row) { // Check if row exists before trying to remove
                    row.remove();
                }
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    title: 'Success',
                    message: 'Item deleted and moved to Wanted queue for rescraping',
                    autoClose: 3000
                });
            } else {
                // If data.success is false, but response was ok (e.g. 200 OK but logical error)
                const errorToThrow = new Error(data.error || 'An unknown error occurred processing the request.');
                if (data.database_locked === true) {
                    errorToThrow.database_locked = true;
                }
                throw errorToThrow;
            }
        })
        .catch(error => {
            console.error('Final error caught for popup:', error);
            console.error('Error message for popup:', error.message);

            let popupTitle = 'Error';
            let popupMessageText = 'An error occurred while processing the item: ' + error.message;
            let popupType = POPUP_TYPES.ERROR;
            let confirmButtonText = 'OK';
            let cancelButtonText = undefined;
            let confirmAction = undefined;
            let cancelAction = undefined;

            if (error.database_locked) {
                popupTitle = 'Database Busy';
                popupMessageText = 'The database is currently busy. Would you like to try again?';
                popupType = POPUP_TYPES.CONFIRM;
                confirmButtonText = 'Retry';
                cancelButtonText = 'Close';
                confirmAction = () => rescrapeItem(itemId);
            } else if ( (error.message && error.message.includes(`Status: 503`)) ||
                        (error.message && error.message.toLowerCase().includes('database is locked')) ) {
                popupTitle = 'Database Busy';
                popupMessageText = 'The database appears to be busy. Would you like to try again?';
                popupType = POPUP_TYPES.CONFIRM;
                confirmButtonText = 'Retry';
                cancelButtonText = 'Close';
                confirmAction = () => rescrapeItem(itemId);
            }

            showPopup({
                type: popupType,
                title: popupTitle,
                message: popupMessageText,
                confirmText: confirmButtonText,
                cancelText: cancelButtonText,
                onConfirm: confirmAction,
                onCancel: cancelAction 
            });
        })
        .finally(() => {
            if (Loading && typeof Loading.hide === 'function') {
                Loading.hide();
            }
        });
    }

    // Add bulk action handlers
    const selectAllBtn = document.getElementById('select-all');
    const bulkDeleteBtn = document.getElementById('bulk-delete');
    const bulkMoveQueue = document.getElementById('bulk-move-queue');
    const bulkChangeVersion = document.getElementById('bulk-change-version');
    const bulkAssignTags = document.getElementById('bulk-assign-tags');
    const bulkEarlyReleaseBtn = document.getElementById('bulk-early-release');
    const bulkRescrapeBtn = document.getElementById('bulk-rescrape');
    const bulkForcePriorityBtn = document.getElementById('bulk-force-priority');
    // NEW constant for resync button
    const bulkResyncBtn = document.getElementById('bulk-resync');
    // NEW constant for verify symlinks button
    const bulkVerifySymlinksBtn = document.getElementById('bulk-verify-symlinks');

    // Fetch available versions and populate the version dropdown
    fetch('/settings/get_scraping_versions')
        .then(response => response.json())
        .then(data => {
            if (data.versions) {
                data.versions.forEach(version => {
            const option = document.createElement('option');
                    option.value = version;
                    option.textContent = version;
                    bulkChangeVersion.appendChild(option);
                });
            }
        })
        .catch(error => {
            console.error('Error fetching versions:', error);
                    showPopup({
                        type: POPUP_TYPES.ERROR,
                message: 'Failed to load available versions',
                        title: 'Error',
        });
    });

    // Fetch the global tag list and populate the Assign Tags dropdown
    fetch('/settings/api/config')
        .then(response => response.json())
        .then(cfgD => {
            const globalTags = (cfgD['Tags'] || {})['tags_list'] || [];
            globalTags.forEach(tag => {
                const option = document.createElement('option');
                option.value = tag;
                option.textContent = tag;
                bulkAssignTags.appendChild(option);
            });
        })
        .catch(error => {
            console.error('Error fetching tags:', error);
        });

    // Handle bulk version change
    bulkChangeVersion.addEventListener('change', function() {
        const selectedVersion = this.value;
        if (!selectedVersion) return;

        const selectedItems = Array.from(document.querySelectorAll('.item-select:checked'))
            .map(checkbox => checkbox.dataset.itemId);

        if (selectedItems.length === 0) return;

        showPopup({
            type: POPUP_TYPES.CONFIRM,
            message: `Are you sure you want to change the version of ${selectedItems.length} item(s) to ${selectedVersion}?`,
            title: 'Confirm Version Change',
            onConfirm: async () => {
                if (Loading && typeof Loading.show === 'function') {
                    Loading.show('Changing versions...', null, false, false);
                }
                try {
                    const result = await processBatchedItems(selectedItems, 'change_version', selectedVersion);
                    showPopup({
                        type: result.warning ? POPUP_TYPES.WARNING : POPUP_TYPES.SUCCESS,
                        message: result.message,
                        title: result.warning ? 'Partial Success' : 'Success',
                    });
                    setTimeout(() => {
                        window.location.reload();
                    }, 2000);
                } catch (error) {
                    console.error('Error:', error);
                    showPopup({
                        type: POPUP_TYPES.ERROR,
                        message: error.message,
                        title: 'Error',
                    });
                } finally {
                    if (Loading && typeof Loading.hide === 'function') {
                        Loading.hide();
                    }
                }
            },
        });

        // Reset the select element
        this.value = '';
    });

    // Select all functionality
    selectAllBtn.addEventListener('click', function() {
        allSelected = !allSelected;
        const checkboxes = document.querySelectorAll('.item-select');
        checkboxes.forEach(checkbox => {
            checkbox.checked = allSelected;
        });
        this.textContent = allSelected ? 'Unselect All' : 'Select All';
        updateBulkActionButtons();
    });

    // Handle individual checkbox changes
    document.addEventListener('change', function(e) {
        if (e.target.classList.contains('item-select')) {
            updateBulkActionButtons();
            // Update select all button text based on checkbox state
            const checkboxes = Array.from(document.querySelectorAll('.item-select'));
            const allChecked = checkboxes.every(cb => cb.checked);
            const noneChecked = checkboxes.every(cb => !cb.checked);
            selectAllBtn.textContent = allChecked ? 'Unselect All' : 'Select All';
            allSelected = allChecked;
        }
    });

    // Handle shift+click for range selection
    document.addEventListener('click', function(e) {
        if (e.target.classList.contains('item-select')) {
            if (!lastChecked) {
                lastChecked = e.target;
                return;
            }

            if (e.shiftKey) {
                const checkboxes = Array.from(document.querySelectorAll('.item-select'));
                const start = checkboxes.indexOf(e.target);
                const end = checkboxes.indexOf(lastChecked);

                checkboxes.slice(
                    Math.min(start, end),
                    Math.max(start, end) + 1
                ).forEach(checkbox => {
                    checkbox.checked = lastChecked.checked;
                });
            }

            lastChecked = e.target;
            updateBulkActionButtons();
        }
    });

    // Update bulk action button states
    function updateBulkActionButtons() {
        const selectedCount = document.querySelectorAll('.item-select:checked').length;
        bulkDeleteBtn.disabled = selectedCount === 0;
        bulkMoveQueue.disabled = selectedCount === 0;
        bulkChangeVersion.disabled = selectedCount === 0;
        bulkAssignTags.disabled = selectedCount === 0;
        bulkEarlyReleaseBtn.disabled = selectedCount === 0;
        bulkRescrapeBtn.disabled = selectedCount === 0;
        bulkForcePriorityBtn.disabled = selectedCount === 0;
        // NEW: enable/disable resync button
        bulkResyncBtn.disabled = selectedCount === 0;
        // NEW: enable/disable verify symlinks button
        bulkVerifySymlinksBtn.disabled = selectedCount === 0;
    }

    async function processBatchedItems(items, action, targetQueue = null) {
        let totalProcessed = 0;
        let errors = [];
        let batches = Math.ceil(items.length / CONFIG.BATCH_SIZE);
        let databaseLockedInAnyBatch = false;

        if (Loading && typeof Loading.show === 'function') {
            Loading.show(`Processing items in ${batches} batches...`, null, false, false);
        }

        for (let i = 0; i < items.length; i += CONFIG.BATCH_SIZE) {
            const batchNumber = Math.floor(i / CONFIG.BATCH_SIZE) + 1;
            const progress = Math.round((batchNumber / batches) * 100);
            if (Loading && typeof Loading.setProgress === 'function') {
                Loading.setProgress(progress);
            }
            if (Loading && typeof Loading.updateMessage === 'function') {
                Loading.updateMessage(`Processing batch ${batchNumber}/${batches}...`);
            }

            const batch = items.slice(i, i + CONFIG.BATCH_SIZE);
            const formData = new FormData();
            formData.append('action', action);

            if (action === 'delete') {
                formData.append('blacklist', targetQueue === 'true');
            } else if (targetQueue) {
                formData.append('target_queue', targetQueue);
            }

            batch.forEach(id => {
                formData.append('selected_items', id);
            });

            try {
                const response = await fetch('/database/bulk_queue_action', {
                    method: 'POST',
                    body: formData,
                });

                if (!response.ok) {
                    let errorMsg = `HTTP error! status: ${response.status}`;
                    let isDbLock = response.status === 503; 
                    let responseDataForError = null;

                    try {
                        const errData = await response.json();
                        responseDataForError = errData;
                        errorMsg = errData.error || errorMsg;
                        if (errData.database_locked === true) {
                            isDbLock = true;
                        }
                    } catch (e) { /* JSON parsing failed */ }

                    if (isDbLock) {
                        databaseLockedInAnyBatch = true;
                        const err = new Error(errorMsg);
                        err.database_locked = true;
                        err.batchNumber = batchNumber;
                        err.details = responseDataForError; // Optionally add more details
                        throw err;
                    }
                    throw new Error(errorMsg); 
                }

                const data = await response.json();

                if (data.success) {
                    totalProcessed += batch.length;
                     if (data.warning) { 
                        errors.push(`Batch ${batchNumber}: ${data.message}`);
                    }
                } else {
                    if (data.database_locked === true) {
                        databaseLockedInAnyBatch = true;
                        const err = new Error(data.error || 'Database locked');
                        err.database_locked = true;
                        err.batchNumber = batchNumber;
                        err.details = data; // Optionally add more details
                        throw err;
                    }
                    errors.push(`Batch ${batchNumber}: ${data.error || 'Unknown error'}`);
                }
            } catch (error) { // This is the inner catch for each batch
                console.error(`Error in batch ${batchNumber}:`, error);
                // We always re-throw to stop processing on any batch error and let executeBulkAction handle it.
                // The 'error' object here will be what executeBulkAction receives.
                throw error; 
            }

            if (batchNumber < batches) {
                 await new Promise(resolve => setTimeout(resolve, CONFIG.BATCH_DELAY_MS));
            }
        }

        if (Loading && typeof Loading.hide === 'function') {
            Loading.hide();
        }

        return {
            success: errors.length === 0 && !databaseLockedInAnyBatch,
            warning: errors.length > 0 && totalProcessed > 0,
            message: errors.length > 0
                ? `Processed ${totalProcessed}/${items.length} items. Batch errors: ${errors.join('; ')}`
                : `Successfully processed ${totalProcessed} items`,
            database_locked: databaseLockedInAnyBatch
        };
    }

    // --- Helper to create the main logic for bulk actions to enable retry ---
    async function executeBulkAction(selectedItems, actionType, targetQueueOrBlacklist, confirmationTitle, successMessagePrefix) {
        if (Loading && typeof Loading.show === 'function') {
            Loading.show('Processing...', null, false, false); // Show progress for bulk operations
        }
        try {
            const result = await processBatchedItems(selectedItems, actionType, targetQueueOrBlacklist);

            // This part is reached if processBatchedItems completes without throwing a db_lock error
            showPopup({
                type: result.warning || result.database_locked ? POPUP_TYPES.WARNING : POPUP_TYPES.SUCCESS,
                message: result.message,
                title: result.warning || result.database_locked ? 'Process Ended' : 'Success',
            });
            // Reload only on full success without db lock issues from any batch
            if (result.success && !result.database_locked) {
                 setTimeout(() => { window.location.reload(); }, 2000);
            }

        } catch (error) {
            console.error('Error during bulk ' + actionType + ':', error);
            if (error.database_locked) {
                showPopup({
                    type: POPUP_TYPES.CONFIRM,
                    title: 'Database Busy',
                    message: `The database is currently busy (encountered in batch ${error.batchNumber || 'N/A'}). Would you like to retry the entire bulk ${actionType} operation?`,
                    confirmText: 'Retry All',
                    cancelText: 'Close',
                    onConfirm: () => executeBulkAction(selectedItems, actionType, targetQueueOrBlacklist, confirmationTitle, successMessagePrefix), // Retry the whole thing
                });
            } else {
                let popupTitle = 'Error';
                let popupMessageText = `Failed to ${actionType} items: ${error.message || 'Unknown error'}`;
                let popupType = POPUP_TYPES.ERROR;
                let confirmButtonText = 'OK';
                let cancelButtonText = undefined;
                let confirmAction = undefined;

                if (error.message && 
                    (
                        (error.message.toLowerCase().indexOf('database') > -1 && error.message.toLowerCase().indexOf('locked') > -1) || 
                        error.message.includes('Status: 503')
                    )
                   ) {
                    popupTitle = 'Database Busy';
                    popupMessageText = `The database appears to be busy. Would you like to retry the entire bulk ${actionType} operation?`;
                    popupType = POPUP_TYPES.CONFIRM;
                    confirmButtonText = 'Retry All';
                    cancelButtonText = 'Close';
                    confirmAction = () => executeBulkAction(selectedItems, actionType, targetQueueOrBlacklist, confirmationTitle, successMessagePrefix);
                }

                showPopup({
                    type: popupType,
                    title: popupTitle,
                    message: popupMessageText,
                    confirmText: confirmButtonText,
                    cancelText: cancelButtonText,
                    onConfirm: confirmAction,
                    // onCancel can be omitted if it's just closing
                });
            }
        } finally {
            if (Loading && typeof Loading.hide === 'function') {
                Loading.hide();
            }
        }
    }


    // Handle bulk delete
    bulkDeleteBtn.addEventListener('click', function() {
        // Prevent multiple rapid clicks
        if (this.disabled) return;

        const selectedItems = Array.from(document.querySelectorAll('.item-select:checked'))
            .map(checkbox => checkbox.dataset.itemId);

        if (selectedItems.length === 0) return;

        // Disable button temporarily to prevent multiple clicks
        this.disabled = true;

        // Helper to re-enable the button
        function reenableBulkDeleteBtn() {
            bulkDeleteBtn.disabled = false;
        }

        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Confirm Deletion',
            message: `
                <div>Are you sure you want to delete ${selectedItems.length} item(s)?</div>
                <div style="margin-top: 10px;">
                    <label>
                        <input type="checkbox" id="bulk-blacklist-checkbox"> Blacklist item to avoid re-addition?
                    </label>
                </div>
            `,
            confirmText: 'Delete',
            cancelText: 'Cancel',
            onConfirm: () => {
                const blacklistItems = document.getElementById('bulk-blacklist-checkbox').checked;
                executeBulkAction(selectedItems, 'delete', blacklistItems.toString(), 'Deletion', 'deleted');
                reenableBulkDeleteBtn();
            },
            onCancel: reenableBulkDeleteBtn,
            onHiddenClose: reenableBulkDeleteBtn
        });
    });

    // Handle bulk move to queue
    bulkMoveQueue.addEventListener('change', function() {
        const selectedQueue = this.value;
        if (!selectedQueue) return;

        const selectedItems = Array.from(document.querySelectorAll('.item-select:checked'))
            .map(checkbox => checkbox.dataset.itemId);

        if (selectedItems.length === 0) { this.value = ''; return; }

        // Handle ghostlist action specially
        if (selectedQueue === 'ghostlist') {
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: `Are you sure you want to ghostlist ${selectedItems.length} item(s)? This will blacklist them and hide them from the database unless specifically filtered for.`,
                title: 'Confirm Ghostlist',
                onConfirm: () => {
                    executeBulkAction(selectedItems, 'ghostlist', null, 'Ghostlist', 'ghostlisted');
                },
            });
        } else {
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: `Are you sure you want to move ${selectedItems.length} item(s) to ${selectedQueue} queue?`,
                title: 'Confirm Move',
                onConfirm: () => { // Removed async
                    executeBulkAction(selectedItems, 'move', selectedQueue, 'Move', `moved to ${selectedQueue}`);
                },
            });
        }
        this.value = ''; // Reset select after confirmation
    });

    // Handle bulk version change
    bulkChangeVersion.addEventListener('change', function() {
        const selectedVersion = this.value;
        if (!selectedVersion) return;

        const selectedItems = Array.from(document.querySelectorAll('.item-select:checked'))
            .map(checkbox => checkbox.dataset.itemId);

        if (selectedItems.length === 0) {this.value = ''; return;}

        showPopup({
            type: POPUP_TYPES.CONFIRM,
            message: `Are you sure you want to change the version of ${selectedItems.length} item(s) to ${selectedVersion}?`,
            title: 'Confirm Version Change',
            onConfirm: () => { // Removed async
                executeBulkAction(selectedItems, 'change_version', selectedVersion, 'Version Change', `version changed for`);
            },
        });
        this.value = ''; // Reset select
    });

    // Handle bulk tags
    bulkAssignTags.addEventListener('change', function() {
        const selectedTag = this.value;
        if (!selectedTag) return;

        const selectedItems = Array.from(document.querySelectorAll('.item-select:checked'))
            .map(checkbox => checkbox.dataset.itemId);

        if (selectedItems.length === 0) {this.value = ''; return;}

        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Tags',
            message: `How do you want to apply the tag "${selectedTag}" to ${selectedItems.length} item(s)?`,
            confirmText: 'Add',
            extraButtonText: 'Update',
            cancelText: 'Remove',
            suppressEscapeAction: true,
            onConfirm: () => {
                executeBulkAction(selectedItems, 'assign_tags', selectedTag, 'Tags', `tagged with "${selectedTag}"`);
            },
            onExtra: () => {
                executeBulkAction(selectedItems, 'update_tags', selectedTag, 'Tags', `tags set to "${selectedTag}"`);
            },
            onCancel: () => {
                executeBulkAction(selectedItems, 'remove_tags', selectedTag, 'Tags', `tag "${selectedTag}" removed`);
            },
        });
        this.value = ''; // Reset select
    });


    // Handle bulk early release
    bulkEarlyReleaseBtn.addEventListener('click', function() {
        const selectedItems = Array.from(document.querySelectorAll('.item-select:checked'))
            .map(checkbox => checkbox.dataset.itemId);

        if (selectedItems.length === 0) return;

        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Confirm Early Release',
            message: `
                <div>Are you sure you want to mark ${selectedItems.length} item(s) as early release?</div>
            `,
            confirmText: 'Mark',
            cancelText: 'Cancel',
            onConfirm: () => { // Removed async
                executeBulkAction(selectedItems, 'early_release', null, 'Early Release', 'marked as early release for');
            },
        });
    });

    // Handle bulk rescrape
    bulkRescrapeBtn.addEventListener('click', function() {
        const selectedItems = Array.from(document.querySelectorAll('.item-select:checked'))
            .map(checkbox => checkbox.dataset.itemId);

        if (selectedItems.length === 0) return;

        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Confirm Rescrape',
            message: 'Are you sure you want to delete these items and move them to the Wanted queue for rescraping? This will delete the files from disk and remove them from Plex.',
            confirmText: 'Delete & Rescrape',
            cancelText: 'Cancel',
            onConfirm: () => { // Removed async
                 executeBulkAction(selectedItems, 'rescrape', null, 'Rescrape', 'rescrape initiated for');
            },
        });
    });

    // Handle bulk force priority
    bulkForcePriorityBtn.addEventListener('click', function() {
        const selectedItems = Array.from(document.querySelectorAll('.item-select:checked'))
            .map(checkbox => checkbox.dataset.itemId);

        if (selectedItems.length === 0) return;

        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Confirm Force Priority',
            message: `Are you sure you want to mark ${selectedItems.length} item(s) for forced priority? This will make them jump to the front of their respective queues.`,
            confirmText: 'Mark for Priority',
            cancelText: 'Cancel',
            onConfirm: () => {
                executeBulkAction(selectedItems, 'force_priority', null, 'Force Priority', 'marked for forced priority');
            },
        });
    });

    // NEW: Bulk resync handler
    bulkResyncBtn.addEventListener('click', function() {
        const selectedItems = Array.from(document.querySelectorAll('.item-select:checked'))
            .map(cb => cb.dataset.itemId);
        if (selectedItems.length === 0) return;
        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Confirm Resync',
            message: `Resync symlinks and DB paths for ${selectedItems.length} item(s)?`,
            confirmText: 'Resync',
            cancelText: 'Cancel',
            onConfirm: () => {
                executeBulkAction(selectedItems, 'resync', null, 'Resync', 'resynchronized');
            }
        });
    });

    // NEW: Bulk verify symlinks handler
    bulkVerifySymlinksBtn.addEventListener('click', function() {
        const selectedItems = Array.from(document.querySelectorAll('.item-select:checked'))
            .map(cb => cb.dataset.itemId);
        if (selectedItems.length === 0) return;
        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: 'Confirm Symlink Verification',
            message: `Add ${selectedItems.length} item(s) to the symlink verification queue? This will verify that the symlinked files are properly scanned into Plex.`,
            confirmText: 'Verify',
            cancelText: 'Cancel',
            onConfirm: () => {
                executeBulkAction(selectedItems, 'verify_symlinks', null, 'Symlink Verification', 'added to verification queue');
            }
        });
    });

    // Initial attachment of event listeners
    attachEventListeners();
    attachDeleteListeners();
    attachRescrapeListeners();

    // Tooltips are automatically handled by event delegation in tooltips.js
    updateBulkActionButtons();

});
