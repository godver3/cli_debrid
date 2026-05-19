/**
 * Shared Deletion Utilities
 *
 * Common JavaScript utilities for all deletion features:
 * - Library multi-select deletion
 * - Maintenance page duplicate cleanup
 * - Show page multi-level deletion
 *
 * This module provides:
 * - API communication for deletion operations
 * - Unified confirmation modal handling
 * - Toast notifications for success/error feedback
 * - Impact analysis before deletion
 * - Multi-select utilities (Ctrl+Click, Shift+Click, Ctrl+A)
 */

import { showPopup, POPUP_TYPES, showLoading, hideLoading } from './notifications.js';

// ============================================================================
// CONSTANTS & CONFIGURATION
// ============================================================================

export const DELETION_ENDPOINTS = {
    CHECK_IMPACT: '/library/check_deletion_impact',
    DELETE_ITEMS: '/library/delete_items',
    DELETE_RECOMMENDED: '/library/maintenance/delete_recommended'
};

// State priority for duplicate resolution (matches backend)
export const STATE_PRIORITY = {
    'Collected': 1,
    'Upgrading': 2,
    'Final_Check': 3,
    'Wanted': 4,
    'Unreleased': 5,
    'Error': 6,
    'Blacklisted': 7,
    'ghostlisted': 8,
    'all_blacklisted': 9
};

// Deletion layer configuration (6-layer deletion system)
export const DELETION_LAYERS = {
    DATABASE: 'database',
    MEDIA_SERVER: 'media_server',
    FILESYSTEM: 'filesystem',
    DEBRID: 'debrid',
    SYMLINKS: 'symlinks',
    CACHE: 'cache'
};

// ============================================================================
// DELETION COMMON CLASS
// ============================================================================

class DeletionCommonClass {
    constructor() {
        this.selectedItems = new Set();
        this.lastSelectedIndex = null;
        this.onSelectionChange = null;
    }

    // ========================================================================
    // MULTI-SELECT UTILITIES
    // ========================================================================

    /**
     * Initialize multi-select behavior on a container
     * @param {HTMLElement} container - Container element with selectable items
     * @param {Object} options - Configuration options
     * @param {string} options.itemSelector - CSS selector for selectable items
     * @param {string} options.checkboxSelector - CSS selector for checkboxes
     * @param {Function} options.onSelectionChange - Callback when selection changes
     */
    initMultiSelect(container, options = {}) {
        const {
            itemSelector = '.media-poster',
            checkboxSelector = 'input[type="checkbox"]',
            onSelectionChange = null
        } = options;

        this.onSelectionChange = onSelectionChange;

        // Handle click events on items
        container.addEventListener('click', (event) => {
            const item = event.target.closest(itemSelector);
            if (!item) return;

            const checkbox = item.querySelector(checkboxSelector);
            if (!checkbox) return;

            const itemId = parseInt(checkbox.value || checkbox.dataset.itemId);
            const allItems = Array.from(container.querySelectorAll(itemSelector));
            const currentIndex = allItems.indexOf(item);

            if (event.ctrlKey || event.metaKey) {
                // Ctrl+Click: Toggle individual item
                this.toggleItem(itemId, checkbox);
            } else if (event.shiftKey && this.lastSelectedIndex !== null) {
                // Shift+Click: Range selection
                this.selectRange(container, itemSelector, checkboxSelector, this.lastSelectedIndex, currentIndex);
            } else {
                // Regular click: Toggle single item
                this.toggleItem(itemId, checkbox);
            }

            this.lastSelectedIndex = currentIndex;
            this.notifySelectionChange();
        });

        // Handle Ctrl+A (Select All)
        document.addEventListener('keydown', (event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === 'a') {
                event.preventDefault();
                this.selectAll(container, itemSelector, checkboxSelector);
            } else if (event.key === 'Escape') {
                this.clearSelection(container, checkboxSelector);
            }
        });
    }

    /**
     * Toggle a single item selection
     */
    toggleItem(itemId, checkbox) {
        if (this.selectedItems.has(itemId)) {
            this.selectedItems.delete(itemId);
            checkbox.checked = false;
        } else {
            this.selectedItems.add(itemId);
            checkbox.checked = true;
        }
    }

    /**
     * Select a range of items (Shift+Click)
     */
    selectRange(container, itemSelector, checkboxSelector, startIndex, endIndex) {
        const allItems = Array.from(container.querySelectorAll(itemSelector));
        const start = Math.min(startIndex, endIndex);
        const end = Math.max(startIndex, endIndex);

        for (let i = start; i <= end; i++) {
            const checkbox = allItems[i].querySelector(checkboxSelector);
            if (checkbox) {
                const itemId = parseInt(checkbox.value || checkbox.dataset.itemId);
                this.selectedItems.add(itemId);
                checkbox.checked = true;
            }
        }
    }

    /**
     * Select all items (Ctrl+A)
     */
    selectAll(container, itemSelector, checkboxSelector) {
        const allItems = container.querySelectorAll(itemSelector);
        allItems.forEach(item => {
            const checkbox = item.querySelector(checkboxSelector);
            if (checkbox) {
                const itemId = parseInt(checkbox.value || checkbox.dataset.itemId);
                this.selectedItems.add(itemId);
                checkbox.checked = true;
            }
        });
        this.notifySelectionChange();
    }

    /**
     * Clear all selections
     */
    clearSelection(container, checkboxSelector) {
        this.selectedItems.clear();
        if (container) {
            const checkboxes = container.querySelectorAll(checkboxSelector);
            checkboxes.forEach(cb => cb.checked = false);
        }
        this.notifySelectionChange();
    }

    /**
     * Get current selection count
     */
    getSelectionCount() {
        return this.selectedItems.size;
    }

    /**
     * Get selected item IDs as array
     */
    getSelectedIds() {
        return Array.from(this.selectedItems);
    }

    /**
     * Notify callback of selection change
     */
    notifySelectionChange() {
        if (this.onSelectionChange) {
            this.onSelectionChange(this.selectedItems.size, this.getSelectedIds());
        }
    }

    // ========================================================================
    // API COMMUNICATION
    // ========================================================================

    /**
     * Check deletion impact before performing deletion
     * @param {Array<number>} itemIds - Array of item IDs to check
     * @param {Object} options - Deletion options
     * @returns {Promise<Object>} Impact analysis result
     */
    async checkImpact(itemIds, options = {}) {
        const {
            layers = [
                DELETION_LAYERS.DATABASE,
                DELETION_LAYERS.MEDIA_SERVER,
                DELETION_LAYERS.FILESYSTEM,
                DELETION_LAYERS.DEBRID,
                DELETION_LAYERS.SYMLINKS,
                DELETION_LAYERS.CACHE
            ],
            blacklist = false,
            blacklistSources = false
        } = options;

        // Skip impact check if no items specified (e.g., for show-level deletion with custom endpoint)
        if (!itemIds || itemIds.length === 0) {
            return {
                success: true,
                items_count: 0,
                items: [],
                layers: {}
            };
        }

        try {
            showLoading('Analyzing deletion impact...');

            const response = await fetch(DELETION_ENDPOINTS.CHECK_IMPACT, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    item_ids: itemIds,
                    layers: layers,
                    blacklist: blacklist,
                    blacklist_sources: blacklistSources
                })
            });

            hideLoading();

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            const data = await response.json();

            if (!data.success) {
                throw new Error(data.error || 'Failed to analyze deletion impact');
            }

            return data;
        } catch (error) {
            hideLoading();
            console.error('Error checking deletion impact:', error);
            throw error;
        }
    }

    /**
     * Execute deletion operation
     * @param {Array<number>} itemIds - Array of item IDs to delete
     * @param {Object} options - Deletion options
     * @returns {Promise<Object>} Deletion result
     */
    async executeDelete(itemIds, options = {}) {
        const {
            layers = [
                DELETION_LAYERS.DATABASE,
                DELETION_LAYERS.MEDIA_SERVER,
                DELETION_LAYERS.FILESYSTEM,
                DELETION_LAYERS.DEBRID,
                DELETION_LAYERS.SYMLINKS,
                DELETION_LAYERS.CACHE
            ],
            blacklist = false,
            blacklistSources = false,
            skipConfirmation = false,
            endpoint = null,  // Custom endpoint for special deletions (e.g., delete show)
            confirmTitle = null,
            confirmMessage = null
        } = options;

        try {
            // Show confirmation modal unless skipped
            if (!skipConfirmation) {
                // For custom endpoints with custom messages, show simple confirm
                if (endpoint && confirmMessage) {
                    let resolveConfirm;
                    const confirmPromise = new Promise(resolve => { resolveConfirm = resolve; });
                    showPopup({
                        type: 'confirm',
                        title: 'Confirm',
                        message: confirmMessage,
                        confirmText: 'Confirm',
                        cancelText: 'Cancel',
                        onConfirm: function() { resolveConfirm(true); },
                        onCancel: function() { resolveConfirm(false); }
                    });
                    const confirmed = await confirmPromise;
                    if (!confirmed) {
                        return { success: false, cancelled: true };
                    }
                } else {
                    const confirmed = await this.showConfirmationModal(itemIds, options);
                    if (!confirmed) {
                        return { success: false, cancelled: true };
                    }
                }
            }

            const loadingMessage = confirmTitle || `Deleting ${itemIds.length} item${itemIds.length > 1 ? 's' : ''}...`;
            showLoading(loadingMessage);

            // Use custom endpoint or default deletion endpoint
            const deleteUrl = endpoint || DELETION_ENDPOINTS.DELETE_ITEMS;

            // Build request body - custom endpoints might not need item_ids
            const requestBody = endpoint ? {
                layers: layers,
                blacklist: blacklist,
                blacklist_sources: blacklistSources
            } : {
                item_ids: itemIds,
                layers: layers,
                blacklist: blacklist,
                blacklist_sources: blacklistSources
            };

            const response = await fetch(deleteUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(requestBody)
            });

            hideLoading();

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            const data = await response.json();

            if (!data.success) {
                throw new Error(data.error || 'Deletion failed');
            }

            return data;
        } catch (error) {
            hideLoading();
            console.error('Error executing deletion:', error);
            throw error;
        }
    }

    /**
     * Delete all recommended items (for maintenance page)
     * @returns {Promise<Object>} Deletion result
     */
    async deleteRecommended() {
        try {
            showLoading('Deleting recommended items...');

            const response = await fetch(DELETION_ENDPOINTS.DELETE_RECOMMENDED, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                }
            });

            hideLoading();

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            const data = await response.json();

            if (!data.success) {
                throw new Error(data.error || 'Failed to delete recommended items');
            }

            return data;
        } catch (error) {
            hideLoading();
            console.error('Error deleting recommended items:', error);
            throw error;
        }
    }

    // ========================================================================
    // CONFIRMATION MODAL
    // ========================================================================

    /**
     * Show deletion confirmation modal with impact preview
     * @param {Array<number>} itemIds - Array of item IDs to delete
     * @param {Object} options - Deletion options
     * @returns {Promise<boolean>} True if confirmed, false if cancelled
     */
    async showConfirmationModal(itemIds, options = {}) {
        return new Promise(async (resolve) => {
            try {
                // Get impact analysis
                const impact = await this.checkImpact(itemIds, options);

                // Build impact summary message
                let message = this.buildImpactMessage(impact);

                // Build form for deletion options
                const formHtml = this.buildDeletionForm(impact, options);

                showPopup({
                    type: POPUP_TYPES.PROMPT,
                    title: `Delete ${itemIds.length} Item${itemIds.length > 1 ? 's' : ''}?`,
                    message: message,
                    formHtml: formHtml,
                    confirmText: 'Delete',
                    cancelText: 'Cancel',
                    onConfirm: (formData) => {
                        // Update options with user selections from form
                        if (formData.blacklist === 'on') options.blacklist = true;
                        if (formData.blacklistSources === 'on') options.blacklistSources = true;

                        // Update layers based on checkboxes
                        const selectedLayers = [];
                        for (const layer in DELETION_LAYERS) {
                            if (formData[`layer_${layer.toLowerCase()}`] === 'on') {
                                selectedLayers.push(DELETION_LAYERS[layer]);
                            }
                        }
                        if (selectedLayers.length > 0) {
                            options.layers = selectedLayers;
                        }

                        resolve(true);
                    },
                    onCancel: () => {
                        resolve(false);
                    }
                });
            } catch (error) {
                this.showError('Failed to analyze deletion impact: ' + error.message);
                resolve(false);
            }
        });
    }

    /**
     * Build impact summary message from impact analysis
     */
    buildImpactMessage(impact) {
        const parts = [];

        if (impact.items_count > 0) {
            parts.push(`${impact.items_count} database item${impact.items_count > 1 ? 's' : ''}`);
        }

        if (impact.layers) {
            const layerSummary = [];
            if (impact.layers.media_server && impact.layers.media_server.affected_count > 0) {
                layerSummary.push(`${impact.layers.media_server.affected_count} media server item${impact.layers.media_server.affected_count > 1 ? 's' : ''}`);
            }
            if (impact.layers.filesystem && impact.layers.filesystem.files_count > 0) {
                layerSummary.push(`${impact.layers.filesystem.files_count} file${impact.layers.filesystem.files_count > 1 ? 's' : ''}`);
            }
            if (impact.layers.debrid && impact.layers.debrid.torrents_count > 0) {
                const d = impact.layers.debrid;
                const parts = [];
                if (d.torrent_only_count > 0) parts.push(`${d.torrent_only_count} debrid torrent${d.torrent_only_count > 1 ? 's' : ''}`);
                if (d.nzb_count > 0) parts.push(`${d.nzb_count} usenet NZB${d.nzb_count > 1 ? 's' : ''}`);
                if (parts.length) layerSummary.push(parts.join(' + '));
            }
            if (impact.layers.symlinks && impact.layers.symlinks.symlinks_count > 0) {
                layerSummary.push(`${impact.layers.symlinks.symlinks_count} symlink${impact.layers.symlinks.symlinks_count > 1 ? 's' : ''}`);
            }

            if (layerSummary.length > 0) {
                parts.push(layerSummary.join(', '));
            }
        }

        if (parts.length === 0) {
            return 'No items will be deleted.';
        }

        return 'This will delete:\n' + parts.join('\n');
    }

    /**
     * Build deletion options form HTML
     */
    buildDeletionForm(impact, options) {
        const layers = options.layers || Object.values(DELETION_LAYERS);

        let html = '<form style="text-align: left;">';

        // Deletion layers checkboxes
        html += '<fieldset><legend>Deletion Layers</legend>';

        for (const layer in DELETION_LAYERS) {
            const layerKey = DELETION_LAYERS[layer];
            const isChecked = layers.includes(layerKey) ? 'checked' : '';
            const layerName = layer.charAt(0) + layer.slice(1).toLowerCase().replace('_', ' ');

            html += `
                <label>
                    <input type="checkbox" name="layer_${layer.toLowerCase()}" ${isChecked}>
                    ${layerName}
                </label><br>
            `;
        }

        html += '</fieldset>';

        // Additional options
        html += '<fieldset><legend>Additional Options</legend>';

        const blacklistChecked = options.blacklist ? 'checked' : '';
        html += `
            <label>
                <input type="checkbox" name="blacklist" ${blacklistChecked}>
                Blacklist items (prevent re-download)
            </label><br>
        `;

        const blacklistSourcesChecked = options.blacklistSources ? 'checked' : '';
        html += `
            <label>
                <input type="checkbox" name="blacklistSources" ${blacklistSourcesChecked}>
                Blacklist content sources (prevent re-addition)
            </label><br>
        `;

        html += '</fieldset>';
        html += '</form>';

        return html;
    }

    // ========================================================================
    // TOAST NOTIFICATIONS
    // ========================================================================

    /**
     * Show success notification
     */
    showSuccess(message, title = 'Success') {
        showPopup({
            type: POPUP_TYPES.SUCCESS,
            title: title,
            message: message,
            autoClose: 3000
        });
    }

    /**
     * Show error notification
     */
    showError(message, title = 'Error') {
        showPopup({
            type: POPUP_TYPES.ERROR,
            title: title,
            message: message,
            autoClose: 5000
        });
    }

    /**
     * Show info notification
     */
    showInfo(message, title = 'Info') {
        showPopup({
            type: POPUP_TYPES.INFO,
            title: title,
            message: message,
            autoClose: 4000
        });
    }

    /**
     * Show warning notification
     */
    showWarning(message, title = 'Warning') {
        showPopup({
            type: POPUP_TYPES.WARNING,
            title: title,
            message: message,
            autoClose: 4000
        });
    }

    // ========================================================================
    // UTILITY FUNCTIONS
    // ========================================================================

    /**
     * Format bytes to human-readable size
     */
    formatBytes(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
    }

    /**
     * Get state priority for duplicate resolution
     */
    getStatePriority(state) {
        return STATE_PRIORITY[state] || 10;
    }

    /**
     * Compare two items by state priority (for sorting)
     */
    compareByStatePriority(itemA, itemB) {
        const priorityA = this.getStatePriority(itemA.state);
        const priorityB = this.getStatePriority(itemB.state);
        return priorityA - priorityB;
    }

    /**
     * Find the best item in a duplicate group (highest priority state)
     */
    findBestItem(items) {
        if (!items || items.length === 0) return null;
        return items.reduce((best, current) => {
            return this.compareByStatePriority(current, best) < 0 ? current : best;
        });
    }

    /**
     * Group items for deletion recommendation
     */
    getRecommendedDeletions(items) {
        const best = this.findBestItem(items);
        if (!best) return [];
        return items.filter(item => item.id !== best.id);
    }
}

// ============================================================================
// DELETION REPORT FORMATTING UTILITY
// ============================================================================

/**
 * Build a formatted deletion report from API response
 * @param {Object} result - Deletion API response
 * @param {string} itemTitle - Title of deleted item (e.g., show name)
 * @param {string} mediaType - Type of media ('movie', 'show', 'season', 'episode') - defaults to 'show'
 * @returns {string} HTML-formatted report string
 */
export function buildDeletionReport(result, itemTitle = 'Item', mediaType = 'show') {
    const reportLines = [];

    // Check if item was ghostlisted instead of deleted
    const wasGhostlisted = result.auto_ghostlisted === true;

    // Header: item name and count
    // For movies, show file count; for shows/seasons/episodes, show episode count
    let countText;
    if (mediaType === 'movie') {
        countText = `${result.deleted_count} file${result.deleted_count !== 1 ? 's' : ''}`;
    } else {
        countText = `${result.deleted_count} episode${result.deleted_count !== 1 ? 's' : ''}`;
    }
    const action = wasGhostlisted ? 'Ghostlisted' : 'Removed';
    reportLines.push(`<strong>${action} "${itemTitle}"</strong> | ${countText}`);
    reportLines.push(''); // Empty line

    // Parse content source removal details
    const contentSourceResult = result.content_source_removal;
    let contentSourceSuccess = [];
    let contentSourceFailed = [];

    if (contentSourceResult) {
        contentSourceSuccess = contentSourceResult.sources_succeeded || [];
        contentSourceFailed = contentSourceResult.sources_failed || [];
    }

    // Report on each layer (only successful ones)
    if (result.layers_executed && result.layers_executed.length > 0) {
        for (const layer of result.layers_executed) {
            if (layer.startsWith('Database')) {
                // Show specific action based on layer type
                if (layer.includes('Ghostlisted')) {
                    reportLines.push('✓ Ghostlisted in database (not deleted, prevents re-addition)');
                } else if (layer.includes('Blacklisted')) {
                    reportLines.push('✓ Blacklisted in database');
                } else {
                    reportLines.push('✓ Removed from database');
                }
            } else if (layer === 'Media Server') {
                reportLines.push('✓ Removed from media server (Plex/Jellyfin)');
            } else if (layer === 'Filesystem') {
                reportLines.push('✓ Removed files from filesystem');
            } else if (layer && layer.startsWith('Debrid')) {
                reportLines.push('✓ Removed from debrid/usenet provider');
            } else if (layer === 'Symlinks') {
                reportLines.push('✓ Removed symlinks');
            } else if (layer === 'Cache') {
                reportLines.push('✓ Cleared cache');
            }
        }
    }

    // Report skipped layers
    if (result.layers_skipped && result.layers_skipped.length > 0) {
        for (const skip of result.layers_skipped) {
            const layerName = skip.layer || skip;
            const reason = skip.reason || 'Unknown reason';
            reportLines.push(`⊘ ${layerName} skipped (${reason})`);
        }
    }

    // Add content source removal details
    if (contentSourceSuccess.length > 0) {
        for (const source of contentSourceSuccess) {
            const detail = contentSourceResult.details?.[source];

            // Extract readable name from source key
            let sourceName = source;
            if (source.startsWith('Trakt_List_')) {
                const slug = source.replace('Trakt_List_', '');
                sourceName = slug.split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
            } else if (source.startsWith('Trakt_Collection_') || source.startsWith('Trakt Collection_')) {
                sourceName = 'Trakt Collection';
            } else if (source === 'Overseerr') {
                sourceName = 'Seerr';
            } else if (source === 'Plex_Watchlist') {
                sourceName = 'Plex Watchlist';
            // } else if (source.startsWith('MDBList_')) {
            //     sourceName = source.replace('MDBList_', '').replace(/_/g, ' ');
            }

            // Show both successful removals AND sources where item wasn't found
            if (detail) {
                // Handle Overseerr (has request_id instead of removed count)
                if (source === 'Overseerr' && detail.request_id) {
                    reportLines.push(`✓ Removed from ${sourceName} (request #${detail.request_id})`);
                }
                // Handle Plex Watchlist (has success flag, no removed count)
                else if (source === 'Plex_Watchlist' && detail.success) {
                    reportLines.push(`✓ Removed from ${sourceName}`);
                }
                // Handle MDBList sources (commented out for now)
                // else if (source.startsWith('MDBList_') && detail.success) {
                //     reportLines.push(`✓ Removed from ${sourceName}`);
                // }
                // Handle Trakt sources (has removed count)
                else if (detail.removed > 0) {
                    const itemType = sourceName === 'Trakt Collection' ? 'item' : 'show';
                    reportLines.push(`✓ Removed from ${sourceName} (deleted: ${detail.removed} ${itemType}${detail.removed !== 1 ? 's' : ''})`);
                }
                // Item wasn't found in source (Trakt-style with removed=0)
                else if (detail.success && detail.removed === 0) {
                    reportLines.push(`⊘ ${sourceName} checked (show not found in source)`);
                }
                // Generic success fallback
                else if (detail.success) {
                    reportLines.push(`✓ Removed from ${sourceName}`);
                }
            }
        }
    }

    // Report failures
    if (result.failed_count > 0) {
        reportLines.push('');
        reportLines.push(`⚠ ${result.failed_count} item(s) failed to delete`);
    }

    if (contentSourceFailed.length > 0) {
        for (const source of contentSourceFailed) {
            const detail = contentSourceResult.details?.[source];
            const message = detail?.message || 'Unknown error';
            reportLines.push(`✗ Failed to remove from ${source}: ${message}`);
        }
    }

    return reportLines.join('<br>');
}

// Make available globally for non-module scripts
window.buildDeletionReport = buildDeletionReport;

// ============================================================================
// EXPORT SINGLETON INSTANCE
// ============================================================================

export const DeletionCommon = new DeletionCommonClass();

// Make available globally for non-module scripts
window.DeletionCommon = DeletionCommon;
window.DELETION_LAYERS = DELETION_LAYERS;
window.STATE_PRIORITY = STATE_PRIORITY;
