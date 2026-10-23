/* Moved from templates/reverse_parser.html. */
import { initializeTooltips, setUpdatingContent } from '/static/js/tooltips.js';
import { splitRespectingParentheses } from '/static/js/settings.js';

document.addEventListener('DOMContentLoaded', function() {
    console.log("DOMContentLoaded event fired");

    // DOM Elements
    const defaultVersionSelect = document.getElementById('default-version');
    const filterDefaultButton = document.getElementById('filterDefaultButton');
    const applyParsedVersionsButton = document.getElementById('applyParsedVersionsButton');

    let currentDefaultVersion;
    let isFilteringDefault = window.pageData.reverse_parser.filter_default;
    let currentItems = []; // Store current items for apply operation

    // Initialization
    initializeTooltips();
    setupEventListeners();
    loadVersionData();

    // Event Listeners Setup
    function setupEventListeners() {
        if (filterDefaultButton) {
            filterDefaultButton.addEventListener('click', handleFilterDefault);
        }

        if (applyParsedVersionsButton) {
            applyParsedVersionsButton.addEventListener('click', handleApplyParsedVersions);
        }
        if (defaultVersionSelect) {
            defaultVersionSelect.addEventListener('change', function() {
                currentDefaultVersion = this.value;
                updateVersionSettings();
                updateContent(window.pageData.reverse_parser.database_reverse_parser_url, { filter_default: isFilteringDefault, page: 1 });
            });
        }
    }

    // Event Handlers
    function handleFilterDefault() {
        isFilteringDefault = !isFilteringDefault;
        console.log("Filtering default:", isFilteringDefault);
        updateContent(window.pageData.reverse_parser.database_reverse_parser_url, { filter_default: isFilteringDefault, page: 1 });
    }

    // Version Management
    function loadVersionData() {
        Promise.all([
            fetch('/settings/get_scraping_versions').then(response => response.json()),
            fetch('/settings/api/reverse_parser_settings').then(response => response.json())
        ]).then(([versionsData, reverseParserData]) => {
            // Check if versionsData has an error or versions is missing
            if (versionsData.error || !versionsData.versions) {
                console.error("Error loading scraping versions:", versionsData.error || "No versions found");
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error',
                    message: 'Failed to load scraping versions: ' + (versionsData.error || 'No versions configured')
                });
                return;
            }
            setupDefaultVersion(versionsData.versions, reverseParserData);
        }).catch(error => {
            console.error("Error loading version data:", error);
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error',
                message: 'Failed to load version data: ' + error.message
            });
        });
    }

    function setupDefaultVersion(allScrapingVersions, reverseParserData) {
        // Safety check: ensure allScrapingVersions is an array
        const versions = Array.isArray(allScrapingVersions) ? allScrapingVersions : [];

        updateDefaultVersionOptions(versions, reverseParserData.default_version);
        currentDefaultVersion = reverseParserData.default_version || (versions.length > 0 ? versions[0] : '');
        defaultVersionSelect.value = currentDefaultVersion;

        assignVersions();
    }

    function updateDefaultVersionOptions(versions, currentConfiguredDefault) {
        // Safety check: ensure versions is an array
        if (!Array.isArray(versions) || versions.length === 0) {
            console.warn("No versions available to populate dropdown");
            defaultVersionSelect.innerHTML = '<option value="">No versions configured</option>';
            return;
        }

        const currentSelection = defaultVersionSelect.value || currentConfiguredDefault;
        defaultVersionSelect.innerHTML = versions.map(v => `<option value="${v}">${v}</option>`).join('');

        if (versions.includes(currentSelection)) {
            defaultVersionSelect.value = currentSelection;
        } else if (currentConfiguredDefault && versions.includes(currentConfiguredDefault)) {
            defaultVersionSelect.value = currentConfiguredDefault;
        } else if (versions.length > 0) {
            defaultVersionSelect.value = versions[0];
        }
    }

    function updateVersionSettings() {
        const defaultVersion = document.getElementById('default-version').value;

        const settingsToUpdate = {
            'Reverse Parser': {
                default_version: defaultVersion
            }
        };

        fetch(window.pageData.reverse_parser.settings_update_settings_url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(settingsToUpdate)
        }).then(response => response.json())
        .then(data => {
            if (data.status === 'success') {
                console.log('Reverse Parser settings updated via dedicated updateVersionSettings:', data);
                // Optionally, show a success message to the user
            } else {
                console.error('Error updating Reverse Parser settings:', data.message);
                // Optionally, show an error message to the user
            }
        }).catch(error => {
            console.error('Error updating Reverse Parser settings:', error);
            // Optionally, show an error message to the user
        });
    }

    function assignVersions() {
        const defaultVersionSelectElem = document.getElementById('default-version');
        const defaultVersion = defaultVersionSelectElem ? defaultVersionSelectElem.value : null;

        const rows = document.querySelectorAll('#items-table tbody tr');

        rows.forEach((row) => {
            const assignedVersionCell = row.querySelector('.assigned-version');
            if (assignedVersionCell) {
                const parsedVersion = assignedVersionCell.getAttribute('data-parsed-version');
                let version = parsedVersion;
                let isDefault = false;

                if (parsedVersion.endsWith('**')) {
                    version = parsedVersion.slice(0, -2);
                    isDefault = true;
                } else if (parsedVersion.endsWith('*')) {
                    version = parsedVersion.slice(0, -1);
                }

                assignedVersionCell.textContent = version;

                if (isDefault) {
                    row.classList.remove('matched');
                    row.classList.add('default-version');
                } else {
                    row.classList.add('matched');
                    row.classList.remove('default-version');
                }
            }
        });
    }

    // Content Update
    function updateContent(url, params = {}) {
        // Add default sort parameters if not present
        if (!params.sort_column) params.sort_column = window.pageData.reverse_parser.sort_column;
        if (!params.sort_order) params.sort_order = window.pageData.reverse_parser.sort_order;
        if (!params.page) params.page = 1;

        // Add ajax parameter
        params.ajax = '1';

        const queryString = new URLSearchParams(params).toString();
        url += '?' + queryString;

        setUpdatingContent(true);

        fetch(url)
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    throw new Error(data.error);
                }

                const tableContainer = document.querySelector('.table-container');
                if (tableContainer) {
                    if (data.items && data.items.length > 0) {
                        currentItems = data.items; // Store items for later use
                        tableContainer.innerHTML = renderTable(data.items, data.selected_columns);
                        assignVersions();  // Call this after rendering the table
                    } else {
                        currentItems = []; // Clear items if none found
                        tableContainer.innerHTML = '<p class="rp-empty">No collected items found in the database.</p>';
                    }
                } else {
                    console.error('Table container not found');
                }

                updateUIForCurrentState(data);
                initializeTooltips();
            })
            .catch(error => {
                console.error('Error updating content:', error);
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error',
                    message: 'Failed to update database content: ' + error.message
                });
            })
            .finally(() => {
                setUpdatingContent(false);
            });
    }

    function renderTable(items, selectedColumns) {
        let tableHTML = '<table id="items-table"><thead><tr>';
        selectedColumns.forEach(column => {
            if (column !== 'version') {
                tableHTML += `<th>${column.charAt(0).toUpperCase() + column.slice(1)}</th>`;
            }
        });
        tableHTML += '<th>Assigned Version</th></tr></thead><tbody>';
        tableHTML += renderTableRows(items, selectedColumns);
        tableHTML += '</tbody></table>';
        return tableHTML;
    }

    function renderTableRows(items, selectedColumns) {
        let rowsHTML = '';
        items.forEach(item => {
            rowsHTML += '<tr>';
            selectedColumns.forEach(column => {
                if (column !== 'version') {
                    let cellContent = item[column];
                    if (cellContent === null || cellContent === undefined || cellContent === '') {
                        cellContent = 'None';
                    }
                    rowsHTML += `<td class="truncate" data-full-content="${cellContent}">${cellContent}</td>`;
                }
            });
            rowsHTML += `<td class="assigned-version" data-parsed-version="${item.parsed_version || ''}"></td></tr>`;
        });
        return rowsHTML;
    }

    function handleApplyParsedVersions() {
        if (!currentItems || currentItems.length === 0) {
            showPopup({
                type: POPUP_TYPES.WARNING,
                title: 'No Items',
                message: 'No items available to apply parsed versions to.'
            });
            return;
        }

        // Prepare items with required fields for backend
        const itemsToUpdate = currentItems.map(item => ({
            id: item.id,
            filled_by_file: item.filled_by_file,
            version: item.version,
            state: 'Collected' // All items from reverse parser are in Collected state
        }));

        fetch(window.pageData.reverse_parser.database_apply_parsed_versions_url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ items_to_update: itemsToUpdate })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    title: 'Success',
                    message: data.message || 'Parsed versions have been applied to all items.'
                });
                // Refresh the content to show updated versions
                updateContent(window.pageData.reverse_parser.database_reverse_parser_url, { filter_default: isFilteringDefault, page: 1 });
            } else {
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error',
                    message: 'Failed to apply parsed versions: ' + (data.error || 'Unknown error')
                });
            }
        })
        .catch(error => {
            console.error('Error applying parsed versions:', error);
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error',
                message: 'An error occurred while applying parsed versions: ' + error.message
            });
        });
    }

    function updateUIForCurrentState(data) {
        isFilteringDefault = data.filter_default;
        if (filterDefaultButton) {
            filterDefaultButton.textContent = isFilteringDefault ? 'Show All' : 'Show Only Default Version';
        }
    }
});
