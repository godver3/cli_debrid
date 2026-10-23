/* Moved from templates/database.html. */
// Early redirect script to load page with localStorage settings
(function() {
    const STORAGE_KEYS = {
        FILTERS: 'db_filters',
        SORT_COLUMN: 'db_sort_column',
        SORT_ORDER: 'db_sort_order',
        SELECTED_COLUMNS: 'db_selected_columns',
        CONTENT_TYPE: 'db_content_type',
        FILTER_LOGIC: 'db_filter_logic',
        LETTER: 'db_current_letter',
        PER_PAGE: 'db_per_page'
    };
    const VALID_PER_PAGE = [100, 250, 500, 1000, 2000, 5000];
    const DEFAULTS = {
        SORT_COLUMN: 'id',
        SORT_ORDER: 'asc',
        CONTENT_TYPE: 'all',
        FILTER_LOGIC: 'AND',
        LETTER: '',
        PER_PAGE: 250
    };

    function getQueryParam(param) {
        const urlParams = new URLSearchParams(window.location.search);
        return urlParams.get(param);
    }

    const savedFiltersJSON = localStorage.getItem(STORAGE_KEYS.FILTERS);
    const savedSortColumn = localStorage.getItem(STORAGE_KEYS.SORT_COLUMN);
    const savedSortOrder = localStorage.getItem(STORAGE_KEYS.SORT_ORDER);
    const savedColumnsJSON = localStorage.getItem(STORAGE_KEYS.SELECTED_COLUMNS);
    const savedContentType = localStorage.getItem(STORAGE_KEYS.CONTENT_TYPE);
    const savedFilterLogic = localStorage.getItem(STORAGE_KEYS.FILTER_LOGIC);
    const savedLetter = localStorage.getItem(STORAGE_KEYS.LETTER);
    const savedPerPage = localStorage.getItem(STORAGE_KEYS.PER_PAGE);

    const queryParams = new URLSearchParams();
    let needsRedirect = false;

    // Selected Columns
    let columnsToUse = null;
    if (savedColumnsJSON) {
        try {
            const parsed = JSON.parse(savedColumnsJSON);
            if (Array.isArray(parsed) && parsed.length > 0) columnsToUse = parsed;
        } catch (e) { /* ignore */ }
    }
    if (columnsToUse) {
        const columnsToUseJSON = JSON.stringify(columnsToUse);
        if (getQueryParam('selected_columns') !== columnsToUseJSON) needsRedirect = true;
        queryParams.set('selected_columns', columnsToUseJSON);
    } else if (getQueryParam('selected_columns')) {
        // If LS is empty but param exists, it implies manual URL or shared link.
        // To strictly enforce LS (i.e. clear columns if not in LS), set needsRedirect = true
        // and DON'T set the queryParam. For now, we'll let existing params pass if LS is empty.
    }

    // Filters
    if (savedFiltersJSON) {
        try {
            JSON.parse(savedFiltersJSON); // Validate JSON
            if (getQueryParam('filters') !== savedFiltersJSON) needsRedirect = true;
            queryParams.set('filters', savedFiltersJSON);
        } catch(e) { /* ignore */ }
    } else if (getQueryParam('filters')) {
        // similar to columns: let existing param pass if LS is empty for filters.
    }

    // Sort Column
    const sortColumn = savedSortColumn || DEFAULTS.SORT_COLUMN;
    if (getQueryParam('sort_column') !== sortColumn) needsRedirect = true;
    queryParams.set('sort_column', sortColumn);

    // Sort Order
    const sortOrder = savedSortOrder || DEFAULTS.SORT_ORDER;
    if (getQueryParam('sort_order') !== sortOrder) needsRedirect = true;
    queryParams.set('sort_order', sortOrder);

    // Content Type
    const contentType = (savedContentType === 'movie' || savedContentType === 'episode' || savedContentType === 'all') ? savedContentType : DEFAULTS.CONTENT_TYPE;
    if (getQueryParam('content_type') !== contentType) needsRedirect = true;
    queryParams.set('content_type', contentType);

    // Filter Logic
    const filterLogic = (savedFilterLogic === 'AND' || savedFilterLogic === 'OR') ? savedFilterLogic : DEFAULTS.FILTER_LOGIC;
    if (getQueryParam('filter_logic') !== filterLogic) needsRedirect = true;
    queryParams.set('filter_logic', filterLogic);

    // Letter (use !== null check to allow empty string)
    const letter = (savedLetter !== null) ? savedLetter : DEFAULTS.LETTER;
    if (getQueryParam('letter') !== letter) needsRedirect = true;
    queryParams.set('letter', letter);

    // Per Page
    const perPageRaw = parseInt(savedPerPage, 10);
    const perPage = VALID_PER_PAGE.includes(perPageRaw) ? perPageRaw : DEFAULTS.PER_PAGE;
    if (getQueryParam('per_page') !== String(perPage)) needsRedirect = true;
    queryParams.set('per_page', perPage);

    const redirectFlag = 'database_redirect_marker';
    const flagIsSet = sessionStorage.getItem(redirectFlag) === 'true';

    if (getQueryParam('ajax') === '1' || flagIsSet) {
        needsRedirect = false;
    }

    if (needsRedirect) {
        sessionStorage.setItem(redirectFlag, 'true');
        const baseUrl = window.pageData.database.database_index_url;
        const newUrl = queryParams.toString() ? `${baseUrl}?${queryParams.toString()}` : baseUrl;
        window.location.replace(newUrl);
    } else {
        if (flagIsSet) {
            sessionStorage.removeItem(redirectFlag);
        }
    }
})();
