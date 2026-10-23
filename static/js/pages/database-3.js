/* Moved from templates/database.html. */
// Initialize loading overlay globally
if (typeof Loading !== 'undefined' && typeof Loading.init === 'function') {
    Loading.init();
}

// Stats panel functionality (separate from the module script)
document.addEventListener('DOMContentLoaded', function() {
    const statsPanel = document.querySelector('.stats-panel');
    const statsToggle = document.querySelector('.stats-toggle');

    // Check if panel was open in previous session
    const isPanelOpen = localStorage.getItem('statsPanelOpen') === 'true';
    if (isPanelOpen) {
        statsPanel.classList.add('active');
    }

    if (statsToggle && statsPanel) { // Ensure elements exist before adding listener
        statsToggle.addEventListener('click', function() {
            statsPanel.classList.toggle('active');
            // Save state to localStorage
            localStorage.setItem('statsPanelOpen', statsPanel.classList.contains('active'));
        });
    }

    // Close panel when clicking outside
    document.addEventListener('click', function(e) {
        // Check if panel exists and is active before trying to close
        if (statsPanel && statsPanel.classList.contains('active') && !statsPanel.contains(e.target) && statsToggle && !statsToggle.contains(e.target)) {
            statsPanel.classList.remove('active');
            localStorage.setItem('statsPanelOpen', 'false');
        }
    });

    // ========================================
    // Column Resize Functionality
    // ========================================

    // Store resize state globally to prevent multiple initializations
    let columnResizeState = {
        resizingColumn: null,
        startX: 0,
        startWidth: 0,
        listenersAttached: false,
        rafId: null,
        didResize: false
    };

    // Helper function to stop resizing (defined once at global scope)
    function stopResize() {
        if (columnResizeState.resizingColumn) {
            // Cancel any pending animation frame
            if (columnResizeState.rafId) {
                cancelAnimationFrame(columnResizeState.rafId);
                columnResizeState.rafId = null;
            }

            const table = document.querySelector('.table-container table');
            if (table) {
                table.classList.remove('is-resizing');
                const headers = table.querySelectorAll('thead th');
                const widths = {};
                headers.forEach((header) => {
                    const columnName = header.textContent.trim();
                    if (header.style.width) {
                        widths[columnName] = parseInt(header.style.width);
                    }
                });
                localStorage.setItem('db_column_widths', JSON.stringify(widths));
            }
            columnResizeState.resizingColumn.classList.remove('is-resizing');
            columnResizeState.resizingColumn = null;
            document.body.style.userSelect = '';
            document.body.style.cursor = '';
            if (columnResizeState.didResize) {
                // Block the click event that fires immediately after mouseup on the same element
                document.addEventListener('click', function suppressClick(e) {
                    e.stopImmediatePropagation();
                    document.removeEventListener('click', suppressClick, true);
                }, true);
                columnResizeState.didResize = false;
            }
        }
    }

    // Global event handlers (defined once)
    function handleMouseMove(e) {
        if (!columnResizeState.resizingColumn) return;

        columnResizeState.didResize = true;
        e.preventDefault();

        // Use requestAnimationFrame for smooth updates
        if (columnResizeState.rafId) {
            cancelAnimationFrame(columnResizeState.rafId);
        }

        columnResizeState.rafId = requestAnimationFrame(() => {
            const diff = e.pageX - columnResizeState.startX;
            const newWidth = Math.max(50, columnResizeState.startWidth + diff);

            columnResizeState.resizingColumn.style.width = newWidth + 'px';
            columnResizeState.resizingColumn.style.minWidth = newWidth + 'px';
        });
    }

    function handleMouseUp(e) {
        stopResize();
    }

    // Attach global event listeners only once
    if (!columnResizeState.listenersAttached) {
        document.addEventListener('mousemove', handleMouseMove);
        document.addEventListener('mouseup', handleMouseUp);
        window.addEventListener('mouseup', handleMouseUp); // Backup listener
        columnResizeState.listenersAttached = true;
    }

    // Expose to global scope so updateContent can call it
    window.initColumnResize = function() {
        const table = document.querySelector('.table-container table');
        if (!table) return;

        const headers = table.querySelectorAll('thead th');

        // Load saved column widths from localStorage (by column name, not index)
        function loadColumnWidths() {
            const savedWidths = localStorage.getItem('db_column_widths');
            if (savedWidths) {
                try {
                    const widths = JSON.parse(savedWidths);
                    headers.forEach((header) => {
                        const columnName = header.textContent.trim();
                        if (widths[columnName]) {
                            header.style.width = widths[columnName] + 'px';
                            header.style.minWidth = widths[columnName] + 'px';
                        }
                    });
                } catch (e) {
                    console.error('Error loading column widths:', e);
                }
            }
        }

        // Save column widths to localStorage (by column name for consistency across reloads)
        function saveColumnWidths() {
            const widths = {};
            headers.forEach((header) => {
                const columnName = header.textContent.trim();
                if (header.style.width) {
                    widths[columnName] = parseInt(header.style.width);
                }
            });
            localStorage.setItem('db_column_widths', JSON.stringify(widths));
        }

        // Create resize handle for each header
        headers.forEach((header, index) => {
            // Don't add resize to the last column
            if (index === headers.length - 1) return;

            // Remove existing resize handle if present
            const existingHandle = header.querySelector('.column-resize-handle');
            if (existingHandle) {
                existingHandle.remove();
            }

            const resizeHandle = document.createElement('div');
            resizeHandle.className = 'column-resize-handle';
            header.appendChild(resizeHandle);

            resizeHandle.addEventListener('mousedown', function(e) {
                columnResizeState.resizingColumn = header;
                columnResizeState.startX = e.pageX;
                columnResizeState.startWidth = header.offsetWidth;

                // Add visual feedback during resize
                const table = document.querySelector('.table-container table');
                if (table) {
                    table.classList.add('is-resizing');
                }
                header.classList.add('is-resizing');

                // Only disable text selection during resize and set cursor
                document.body.style.userSelect = 'none';
                document.body.style.cursor = 'col-resize';

                e.preventDefault();
                e.stopPropagation();
            });
        });

        // Load saved widths on init
        loadColumnWidths();
    }; // Changed to ; because it's now an assignment

    // Initialize column resize when table is present on page load
    if (document.querySelector('.table-container table')) {
        window.initColumnResize();
    }

    // NOTE: MutationObserver removed - updateContent() now handles re-initialization
    // The observer was causing infinite loops by triggering on resize handle additions

    // Bidirectional infinite scroll implementation with windowing
    let currentPage = window.pageData.database.current_page;
    let totalPages = window.pageData.database.total_pages;
    let isLoadingMore = false;
    let isLoadingPrevious = false;
    let loadedPages = [currentPage]; // Track which pages are currently in DOM
    let minLoadedPage = currentPage; // Track the lowest page number loaded
    let maxLoadedPage = currentPage; // Track the highest page number loaded

    // Reset function for when table is rebuilt (filters/sorts applied)
    window.resetInfiniteScroll = function(newTotalPages) {
        currentPage = 1;
        totalPages = newTotalPages || 1;
        isLoadingMore = false;
        isLoadingPrevious = false;
        loadedPages = [1];
        minLoadedPage = 1;
        maxLoadedPage = 1;
    };

    if (totalPages > 1) {
        window.addEventListener('scroll', function() {
            const scrollTop = window.pageYOffset || document.documentElement.scrollTop;
            const scrollHeight = document.documentElement.scrollHeight;
            const clientHeight = document.documentElement.clientHeight;

            // Check if scrolled near bottom (load next page) - trigger before end
            if (scrollHeight - scrollTop - clientHeight < CONFIG.SCROLL_TRIGGER_PX && !isLoadingMore && maxLoadedPage < totalPages) {
                loadMoreItems();
            }

            // Check if scrolled near top (load previous page) - trigger from top
            if (scrollTop < CONFIG.SCROLL_TRIGGER_PX && !isLoadingPrevious && minLoadedPage > 1) {
                loadPreviousItems();
            }
        });
    }

    function buildRowHTML(item, pageToLoad, index) {
        const rowNumber = (pageToLoad - 1) * CONFIG.ITEMS_PER_PAGE + index + 1;
        const deleteSVG = `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>`;
        const refreshSVG = `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>`;
        const historySVG = `<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>`;
        // Column order matches Jinja thead: checkbox | # | actions
        let html = '';
        html += `<td><input type="checkbox" class="item-select" data-item-id="${item.id}"></td>`;
        html += `<td class="db-row-num">${rowNumber}</td>`;
        html += `<td><span style="display:flex;gap:4px;align-items:center">` +
            `<button class="db-action-btn db-action-btn-delete delete-item" data-item-id="${item.id}" title="Delete">${deleteSVG}</button>` +
            `<button class="db-action-btn db-action-btn-refresh rescrape-item" data-item-id="${item.id}" title="Rescrape">${refreshSVG}</button>` +
            `<button class="db-action-btn db-action-btn-history history-item" data-item-id="${item.id}" title="State history">${historySVG}</button>` +
            `</span></td>`;

        const selectedColumns = window.pageData.database.selected_columns;
        selectedColumns.forEach(column => {
            let displayContent;
            if (column === 'size') {
                displayContent = item.size_gb !== undefined && item.size_gb !== null ? parseFloat(item.size_gb).toFixed(2) : 'N/A';
            } else {
                displayContent = item[column] !== undefined && item[column] !== null ? String(item[column]) : '';
            }
            html += renderCell(column, displayContent);
        });

        return html;
    }

    async function fetchPageData(pageToLoad) {
        const url = new URL(window.location.href);
        url.searchParams.set('page', pageToLoad);
        url.searchParams.set('ajax', '1');

        const response = await fetch(url.toString());
        return await response.json();
    }

    function createPageFragment(items, pageToLoad) {
        const fragment = document.createDocumentFragment();
        items.forEach((item, index) => {
            const row = document.createElement('tr');
            row.setAttribute('data-page', pageToLoad);
            row.innerHTML = buildRowHTML(item, pageToLoad, index);
            fragment.appendChild(row);
        });
        return fragment;
    }

    async function loadMoreItems() {
        if (isLoadingMore || maxLoadedPage >= totalPages) return;

        const perfStart = performance.now();
        isLoadingMore = true;
        const pageToLoad = maxLoadedPage + 1;

        try {
            const data = await fetchPageData(pageToLoad);

            if (data.items && data.items.length > 0) {
                const tbody = document.querySelector('.table-container tbody');
                const fragment = createPageFragment(data.items, pageToLoad);

                tbody.appendChild(fragment);
                loadedPages.push(pageToLoad);
                maxLoadedPage = pageToLoad;

                // Remove top pages if we exceed max pages in DOM
                if (loadedPages.length > CONFIG.MAX_PAGES_IN_DOM) {
                    const pageToRemove = loadedPages.shift();
                    minLoadedPage = loadedPages[0];
                    const rowsToRemove = tbody.querySelectorAll(`tr[data-page="${pageToRemove}"]`);
                    rowsToRemove.forEach(row => row.remove());
                }
            }

            // Log performance
            const duration = performance.now() - perfStart;
            performanceMetrics.scrollLoadTime = duration;
            if (duration > CONFIG.PERFORMANCE_WARNING_MS) {
                console.warn(`[PERFORMANCE] Slow scroll load (page ${pageToLoad}): ${duration.toFixed(2)}ms`);
            } else {
                console.log(`[PERFORMANCE] Scroll load (page ${pageToLoad}): ${duration.toFixed(2)}ms`);
            }
        } catch (error) {
            console.error('Error loading more items:', error);
        } finally {
            isLoadingMore = false;
        }
    }

    // Toolbar button wiring
    function dbToggleColumnSelector() {
        const colSel = document.getElementById('column-selector');
        const toggleBtn = document.getElementById('toggle-column-selector');
        if (colSel) {
            const isHidden = colSel.classList.contains('hidden');
            if (isHidden) {
                colSel.classList.remove('hidden');
                if (toggleBtn) toggleBtn.textContent = 'Hide Column Selector';
            } else {
                colSel.classList.add('hidden');
                if (toggleBtn) toggleBtn.textContent = 'Select Columns to Display';
            }
        }
    }

    function dbToggleFilterPanel() {
        const filterSort = document.getElementById('filter-sort');
        const presetsPanel = document.getElementById('db-presets-panel');
        if (filterSort) {
            const collapsed = filterSort.classList.toggle('db-filter-collapsed');
            if (presetsPanel) presetsPanel.classList.toggle('db-filter-collapsed', collapsed);
        }
    }

    const dbColumnsBtn = document.getElementById('db-columns-btn');
    const dbResetColsBtn = document.getElementById('db-reset-cols-btn');
    const dbFilterBtn = document.getElementById('db-filter-btn');
    const dbColSelHideBtn = document.getElementById('db-col-sel-hide-btn');
    const dbFilterCollapseBtn = document.getElementById('db-filter-collapse-btn');

    if (dbColumnsBtn) {
        dbColumnsBtn.addEventListener('click', dbToggleColumnSelector);
    }
    if (dbColSelHideBtn) {
        dbColSelHideBtn.addEventListener('click', dbToggleColumnSelector);
    }

    if (dbResetColsBtn) {
        dbResetColsBtn.addEventListener('click', function() {
            localStorage.removeItem('db_column_widths');
            document.querySelectorAll('.table-container th').forEach(th => {
                th.style.width = '';
                th.style.minWidth = '';
            });
        });
    }

    if (dbFilterBtn) {
        dbFilterBtn.addEventListener('click', dbToggleFilterPanel);
    }
    if (dbFilterCollapseBtn) {
        dbFilterCollapseBtn.addEventListener('click', dbToggleFilterPanel);
    }

    // Tangerine: mutually exclusive selection between the two column lists
    const availCols = document.getElementById('available-columns');
    const selCols = document.getElementById('selected-columns');
    if (availCols && selCols) {
        availCols.addEventListener('mousedown', function() {
            selCols.selectedIndex = -1;
        });
        selCols.addEventListener('mousedown', function() {
            availCols.selectedIndex = -1;
        });
    }

    async function loadPreviousItems() {
        if (isLoadingPrevious || minLoadedPage <= 1) return;

        isLoadingPrevious = true;
        const pageToLoad = minLoadedPage - 1;

        try {
            const data = await fetchPageData(pageToLoad);

            if (data.items && data.items.length > 0) {
                const tbody = document.querySelector('.table-container tbody');
                const fragment = createPageFragment(data.items, pageToLoad);

                // Preserve scroll position when prepending content
                const scrollBefore = window.pageYOffset || document.documentElement.scrollTop;
                const heightBefore = document.documentElement.scrollHeight;

                // Prepend to tbody (add at top)
                tbody.insertBefore(fragment, tbody.firstChild);
                loadedPages.unshift(pageToLoad);
                minLoadedPage = pageToLoad;

                // Adjust scroll position to maintain user's view
                const heightAfter = document.documentElement.scrollHeight;
                const heightDiff = heightAfter - heightBefore;
                window.scrollTo(0, scrollBefore + heightDiff);

                // Remove bottom pages if we exceed max pages in DOM
                if (loadedPages.length > CONFIG.MAX_PAGES_IN_DOM) {
                    const pageToRemove = loadedPages.pop();
                    maxLoadedPage = loadedPages[loadedPages.length - 1];
                    const rowsToRemove = tbody.querySelectorAll(`tr[data-page="${pageToRemove}"]`);
                    rowsToRemove.forEach(row => row.remove());
                }
            }
        } catch (error) {
            console.error('Error loading previous items:', error);
        } finally {
            isLoadingPrevious = false;
        }
    }

    // ── Floating sticky header ──
    const NAV_HEIGHT = 50;
    let fh = null;
    let fhRAF = null;

    function dbBuildFloatHeader() {
        const table = document.querySelector('.table-container table');
        if (!table) return;
        if (fh) { fh.remove(); fh = null; }
        const realThead = table.querySelector('thead');
        if (!realThead) return;

        fh = document.createElement('div');
        fh.id = 'db-float-header';
        fh.style.cssText = 'position:fixed;top:' + NAV_HEIGHT + 'px;left:0;z-index:999;overflow:hidden;pointer-events:none;display:none;background:#222;border-bottom:1px solid #333;box-shadow:0 2px 8px rgba(0,0,0,0.4);';
        const ct = document.createElement('table');
        ct.style.cssText = 'border-collapse:separate;border-spacing:0;table-layout:fixed;margin:0;';
        const ch = realThead.cloneNode(true);
        ch.querySelectorAll('.column-resize-handle').forEach(el => el.remove());
        ct.appendChild(ch);
        fh.appendChild(ct);
        document.body.appendChild(fh);
        dbSyncFloatHeader();

        // Mirror horizontal scroll
        const container = document.querySelector('.table-container');
        if (container) {
            container.addEventListener('scroll', function() { if (fh) fh.scrollLeft = container.scrollLeft; });
        }
    }

    function dbSyncFloatHeader() {
        if (!fh) return;
        const container = document.querySelector('.table-container');
        const table = document.querySelector('.table-container table');
        if (!container || !table) return;
        const cr = container.getBoundingClientRect();
        fh.style.width = cr.width + 'px';
        fh.style.left = cr.left + 'px';
        fh.scrollLeft = container.scrollLeft;
        fh.querySelector('table').style.width = table.offsetWidth + 'px';
        const realThs = table.querySelectorAll('thead th');
        const cloneThs = fh.querySelectorAll('thead th');
        realThs.forEach((th, i) => {
            if (cloneThs[i]) {
                cloneThs[i].style.width = th.offsetWidth + 'px';
                cloneThs[i].style.minWidth = th.offsetWidth + 'px';
            }
        });
    }

    window.addEventListener('scroll', function() {
        if (!fh) return;
        const container = document.querySelector('.table-container');
        const table = document.querySelector('.table-container table');
        if (!container || !table) return;
        const cr = container.getBoundingClientRect();
        const tr = table.getBoundingClientRect();
        const theadH = table.querySelector('thead') ? table.querySelector('thead').offsetHeight : 40;
        const show = cr.top < NAV_HEIGHT && tr.bottom > NAV_HEIGHT + theadH;
        fh.style.display = show ? 'block' : 'none';
        if (show) {
            if (fhRAF) cancelAnimationFrame(fhRAF);
            fhRAF = requestAnimationFrame(dbSyncFloatHeader);
        }
    }, { passive: true });

    window.addEventListener('resize', function() { dbSyncFloatHeader(); }, { passive: true });

    window.reinitFloatHeader = function() { dbBuildFloatHeader(); };

    dbBuildFloatHeader();

    // Back to row 1 button
    const dbBackTop = document.getElementById('db-back-top');
    if (dbBackTop) {
        // Show/hide based on scroll position
        window.addEventListener('scroll', function() {
            const scrollTop = window.pageYOffset || document.documentElement.scrollTop;
            dbBackTop.classList.toggle('db-back-top-visible', scrollTop > 300);
        }, { passive: true });

        dbBackTop.addEventListener('click', async function() {
            if (minLoadedPage === 1) {
                // Page 1 is in DOM — jump to top instantly
                window.scrollTo({ top: 0, behavior: 'instant' });
            } else {
                // Page 1 has been windowed out — reload it
                dbBackTop.disabled = true;
                try {
                    const data = await fetchPageData(1);
                    if (data && data.items) {
                        const tbody = document.querySelector('.table-container table tbody');
                        if (!tbody) return;

                        // Remove all existing rows
                        tbody.innerHTML = '';

                        // Reset windowing state
                        window.resetInfiniteScroll(totalPages);

                        // Build and insert page 1 rows
                        const fragment = createPageFragment(data.items, 1);
                        tbody.appendChild(fragment);

                        window.scrollTo({ top: 0, behavior: 'instant' });

                        if (typeof window.reinitFloatHeader === 'function') window.reinitFloatHeader();
                        if (typeof window.initColumnResize === 'function') window.initColumnResize();
                    }
                } finally {
                    dbBackTop.disabled = false;
                }
            }
        });
    }

// Per-page selector
const dbPerPageSel = document.getElementById('db-per-page-select');
if (dbPerPageSel) {
    dbPerPageSel.addEventListener('change', function() {
        const val = parseInt(this.value, 10);
        localStorage.setItem('db_per_page', val);
        const url = new URL(window.location.href);
        url.searchParams.set('per_page', val);
        url.searchParams.set('page', 1);
        window.location.href = url.toString();
    });
}

// Presets
(function() {
    const PRESETS_KEY = 'db_presets';
    const saveBtn = document.querySelector('.db-save-preset-btn');
    const listEl = document.getElementById('db-presets-list');
    if (!listEl) return;

    function loadPresets() {
        try { return JSON.parse(localStorage.getItem(PRESETS_KEY)) || []; }
        catch(e) { return []; }
    }

    function savePresets(presets) {
        localStorage.setItem(PRESETS_KEY, JSON.stringify(presets));
    }

    function captureState() {
        const filters = Array.from(document.querySelectorAll('.filter-row')).map(row => ({
            column: row.querySelector('.filter-column')?.value || '',
            operator: row.querySelector('.filter-operator')?.value || '',
            value: row.querySelector('.filter-value')?.value || ''
        })).filter(f => f.column);
        const sortColumn = document.getElementById('sort-column')?.value || '';
        const sortOrder = document.getElementById('sort-order')?.value || 'asc';
        const filterLogic = document.querySelector('input[name="filter_logic"]:checked')?.value || 'AND';
        const contentType = localStorage.getItem('db_content_type') || 'all';
        const perPage = parseInt(localStorage.getItem('db_per_page') || '250', 10);
        const letter = localStorage.getItem('db_current_letter') || '';
        let columns = [];
        try { columns = JSON.parse(localStorage.getItem('db_selected_columns')) || []; } catch(e) {}
        return { filters, sortColumn, sortOrder, filterLogic, contentType, perPage, columns, letter };
    }

    function applyPreset(preset) {
        // Write all preset values to localStorage
        const filters = preset.filters || [];
        const sortColumn = preset.sortColumn || 'id';
        const sortOrder = preset.sortOrder || 'asc';
        const filterLogic = preset.filterLogic || 'AND';
        const contentType = preset.contentType || 'all';
        const perPage = preset.perPage || 250;
        const letter = preset.letter || '';
        localStorage.setItem('db_filters', JSON.stringify(filters));
        localStorage.setItem('db_sort_column', sortColumn);
        localStorage.setItem('db_sort_order', sortOrder);
        localStorage.setItem('db_filter_logic', filterLogic);
        localStorage.setItem('db_content_type', contentType);
        localStorage.setItem('db_per_page', perPage);
        if (preset.columns && preset.columns.length > 0) {
            localStorage.setItem('db_selected_columns', JSON.stringify(preset.columns));
        }
        localStorage.setItem('db_current_letter', letter);

        // Clear the redirect flag so the early-redirect script runs fresh
        sessionStorage.removeItem('database_redirect_marker');

        // Build the full URL directly from preset data — don't rely on redirect script
        const baseUrl = window.pageData.database.database_index_url;
        const params = new URLSearchParams();
        params.set('sort_column', sortColumn);
        params.set('sort_order', sortOrder);
        params.set('content_type', contentType);
        params.set('filter_logic', filterLogic);
        params.set('letter', letter);
        params.set('per_page', perPage);
        params.set('page', '1');
        if (filters.length > 0) params.set('filters', JSON.stringify(filters));
        if (preset.columns && preset.columns.length > 0) {
            params.set('selected_columns', JSON.stringify(preset.columns));
        }
        window.location.href = `${baseUrl}?${params.toString()}`;
    }

    function renderPresets() {
        const presets = loadPresets();
        listEl.innerHTML = '';
        if (presets.length === 0) {
            listEl.innerHTML = '<div class="db-presets-empty">No presets saved yet</div>';
            return;
        }
        presets.forEach((preset, idx) => {
            const item = document.createElement('div');
            item.className = 'db-preset-item';
            item.innerHTML = `
                <span class="db-preset-name" title="${preset.name}">${preset.name}</span>
                <div class="db-preset-actions">
                    <button class="db-preset-load" data-idx="${idx}">Load</button>
                    <button class="db-preset-del" data-idx="${idx}">✕</button>
                </div>`;
            item.querySelector('.db-preset-load').addEventListener('click', () => applyPreset(preset));
            item.querySelector('.db-preset-del').addEventListener('click', () => {
                const all = loadPresets();
                all.splice(idx, 1);
                savePresets(all);
                renderPresets();
            });
            listEl.appendChild(item);
        });
    }

    if (saveBtn) {
        saveBtn.addEventListener('click', () => {
            showPopup({
                type: 'prompt',
                title: 'Save Preset',
                message: 'Preset name:',
                confirmText: 'Save',
                cancelText: 'Cancel',
                onConfirm: function(value) {
                    if (!value || !value.trim()) return;
                    const preset = captureState();
                    preset.name = value.trim();
                    const all = loadPresets();
                    all.push(preset);
                    savePresets(all);
                    renderPresets();
                }
            });
        });
    }

    renderPresets();
})();

}); // end DOMContentLoaded
