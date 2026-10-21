/**
 * Library Settings Modal
 * Provides a quick-access modal for Library Manager settings
 * Uses the same API endpoints as the main settings page
 */

(function() {
    'use strict';

    let modal, overlay, closeBtn, cancelBtn, saveBtn, clearCacheBtn;
    let autoGhostlistInput;
    let removeFromContentSourcesInput;
    let hideSeasonZeroInput;
    let currentSettings = null;

    // Initialize elements after DOM is ready
    function initializeElements() {
        modal = document.getElementById('librarySettingsModal');
        if (!modal) {
            console.error('[Library Settings Modal] Modal element not found in DOM');
            return false;
        }

        overlay = modal.querySelector('.library-settings-modal-overlay');
        closeBtn = modal.querySelector('.library-settings-modal-close');
        cancelBtn = modal.querySelector('.library-settings-btn-cancel');
        saveBtn = modal.querySelector('.library-settings-btn-save');
        clearCacheBtn = document.getElementById('clearLibraryCacheBtn');

        autoGhostlistInput = document.getElementById('autoGhostlistDeleted');
        removeFromContentSourcesInput = document.getElementById('removeFromContentSources');
        hideSeasonZeroInput = document.getElementById('hideSeasonZero');

        return true;
    }

    /**
     * Open the library settings modal
     */
    window.openLibrarySettingsModal = async function() {
        // Initialize elements if not already done
        if (!modal && !initializeElements()) {
            console.error('[Library Settings Modal] Failed to initialize modal elements');
            return;
        }

        try {
            // Fetch current settings
            const response = await fetch('/settings/api/config');
            if (!response.ok) {
                throw new Error('Failed to fetch settings');
            }

            const config = await response.json();
            currentSettings = config;

            // Populate modal with current values
            const libraryManager = config['Library Manager'] || {};

            if (autoGhostlistInput) {
                autoGhostlistInput.checked = libraryManager.ghostlist_mode || false;
            }

            if (removeFromContentSourcesInput) {
                removeFromContentSourcesInput.checked = libraryManager.remove_from_content_sources !== undefined
                    ? libraryManager.remove_from_content_sources
                    : true; // Default to true
            }

            if (hideSeasonZeroInput) {
                hideSeasonZeroInput.checked = libraryManager.hide_season_zero !== undefined
                    ? libraryManager.hide_season_zero
                    : true; // Default to true (hidden)
            }

            // Show modal
            modal.style.display = 'flex';
            document.body.style.overflow = 'hidden'; // Prevent background scrolling

        } catch (error) {
            console.error('[Library Settings Modal] Error loading settings:', error);
            showNotification('Failed to load settings', 'error');
        }
    };

    /**
     * Close the library settings modal
     */
    function closeModal() {
        if (modal) {
            modal.style.display = 'none';
            document.body.style.overflow = ''; // Restore scrolling
        }
    }

    /**
     * Save library settings using the same API as settings page
     */
    async function saveSettings() {
        if (!currentSettings) {
            showNotification('No settings loaded', 'error');
            return;
        }

        try {
            // Disable save button during save
            if (saveBtn) {
                saveBtn.disabled = true;
                saveBtn.textContent = 'Saving...';
            }

            // Update Library Manager settings in the config
            const updatedSettings = { ...currentSettings };

            if (!updatedSettings['Library Manager']) {
                updatedSettings['Library Manager'] = {};
            }

            updatedSettings['Library Manager'].ghostlist_mode = autoGhostlistInput.checked;
            updatedSettings['Library Manager'].remove_from_content_sources = removeFromContentSourcesInput.checked;
            updatedSettings['Library Manager'].hide_season_zero = hideSeasonZeroInput ? hideSeasonZeroInput.checked : true;

            // Send to same API endpoint as settings page
            const response = await fetch('/settings/api/settings', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(updatedSettings)
            });

            const result = await response.json();

            if (response.ok && result.status === 'success') {
                closeModal();

                // Reload page immediately to reflect changes (e.g., ghost icons, artwork source changes)
                window.location.reload();
            } else {
                throw new Error(result.message || 'Failed to save settings');
            }

        } catch (error) {
            console.error('[Library Settings Modal] Error saving settings:', error);
            showNotification(error.message || 'Failed to save settings', 'error');
        } finally {
            // Re-enable save button
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save Settings';
            }
        }
    }

    /**
     * Clear artwork cache
     */
    async function clearArtworkCache() {
        if (!confirm('This will clear all cached posters and backdrops. Artwork will be re-downloaded on next library load. Continue?')) {
            return;
        }

        try {
            // Disable button during operation
            if (clearCacheBtn) {
                clearCacheBtn.disabled = true;
                clearCacheBtn.textContent = 'Clearing...';
            }

            const response = await fetch('/library/clear_cache', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                }
            });

            const result = await response.json();

            if (response.ok && result.success) {
                showNotification('Artwork cache cleared successfully', 'success');
            } else {
                throw new Error(result.message || 'Failed to clear cache');
            }

        } catch (error) {
            console.error('[Library Settings Modal] Error clearing cache:', error);
            showNotification(error.message || 'Failed to clear cache', 'error');
        } finally {
            // Re-enable button
            if (clearCacheBtn) {
                clearCacheBtn.disabled = false;
                clearCacheBtn.textContent = 'Clear Cache';
            }
        }
    }

    /**
     * Show notification using existing notification system
     */
    function showNotification(message, type = 'info') {
        // Try to use existing notification system from base.js
        if (typeof window.showToast === 'function') {
            window.showToast(message, type);
        } else if (typeof window.showNotification === 'function') {
            window.showNotification(message, type);
        } else {
            // Fallback to alert
            alert(message);
        }
    }

    // Initialize when DOM is ready
    function setupEventListeners() {
        if (!initializeElements()) {
            console.warn('[Library Settings Modal] Elements not ready, event listeners not attached');
            return;
        }

        // Event Listeners
        if (closeBtn) {
            closeBtn.addEventListener('click', closeModal);
        }

        if (cancelBtn) {
            cancelBtn.addEventListener('click', closeModal);
        }

        if (overlay) {
            overlay.addEventListener('click', closeModal);
        }

        if (saveBtn) {
            saveBtn.addEventListener('click', saveSettings);
        }

        if (clearCacheBtn) {
            clearCacheBtn.addEventListener('click', clearArtworkCache);
        }

        // Close modal on Escape key
        document.addEventListener('keydown', function(event) {
            if (event.key === 'Escape' && modal && modal.style.display === 'flex') {
                closeModal();
            }
        });

        console.log('[Library Settings Modal] Initialized successfully');
    }

    // Setup when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', setupEventListeners);
    } else {
        // DOM already loaded
        setupEventListeners();
    }
})();
