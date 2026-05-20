/**
 * Discover Settings Modal
 * Provides a quick-access modal for Discover page settings
 * Uses the same API endpoints as the main settings page
 */

(function() {
    'use strict';

    // Load theme-specific CSS dynamically
    function loadThemeCSS() {
        const getCurrentTheme = () => localStorage.getItem('theme') || 'default';
        const theme = getCurrentTheme();
        const cssFile = theme === 'tangerine' 
            ? 'css/tangerine/tangerine_discover_settings_modal.css' 
            : 'css/discover_settings_modal.css';
        
        // Check if the link already exists
        const existingLink = document.querySelector(`link[href*="discover_settings_modal.css"]`);
        if (!existingLink) {
            const link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = `/static/${cssFile}`;
            document.head.appendChild(link);
        } else {
            // Update existing link
            existingLink.href = `/static/${cssFile}`;
        }
    }

    // Load CSS immediately
    loadThemeCSS();

    let modal, overlay, closeBtn, cancelBtn, saveBtn;
    let hideNoRatingInput, hideNoPosterInput, onlyShowMissingInput, discoverEpisodeViewSelect, hideSpecialsInput;
    let currentSettings = null;

    // Initialize elements after DOM is ready
    function initializeElements() {
        modal = document.getElementById('discoverSettingsModal');
        if (!modal) {
            console.error('[Discover Settings Modal] Modal element not found in DOM');
            return false;
        }

        overlay = modal.querySelector('.discover-settings-modal-overlay');
        closeBtn = modal.querySelector('.discover-settings-modal-close');
        cancelBtn = modal.querySelector('.discover-settings-btn-cancel');
        saveBtn = modal.querySelector('.discover-settings-btn-save');

        hideNoRatingInput = document.getElementById('discoverHideNoRating');
        hideNoPosterInput = document.getElementById('discoverHideNoPoster');
        onlyShowMissingInput = document.getElementById('discoverOnlyShowMissing');
        discoverEpisodeViewSelect = document.getElementById('discoverEpisodeView');
        hideSpecialsInput = document.getElementById('discoverHideSpecials');

        return true;
    }

    /**
     * Open the discover settings modal
     */
    window.openDiscoverSettingsModal = async function() {
        // Initialize elements if not already done
        if (!modal && !initializeElements()) {
            console.error('[Discover Settings Modal] Failed to initialize modal elements');
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
            const discoverSettings = config['Discover Settings'] || {};

            if (hideNoRatingInput) {
                hideNoRatingInput.checked = discoverSettings.hide_no_rating || false;
            }

            if (hideNoPosterInput) {
                hideNoPosterInput.checked = discoverSettings.hide_no_poster || false;
            }

            if (onlyShowMissingInput) {
                onlyShowMissingInput.checked = discoverSettings.only_show_missing || false;
            }

            if (discoverEpisodeViewSelect) {
                discoverEpisodeViewSelect.value = discoverSettings.tv_show_episode_view || 'discover';
            }

            if (hideSpecialsInput) {
                // Default true — matches the previous hardcoded behaviour
                hideSpecialsInput.checked = discoverSettings.hide_specials !== false;
            }

            // Show modal
            modal.style.display = 'flex';
            document.body.style.overflow = 'hidden'; // Prevent background scrolling

        } catch (error) {
            console.error('[Discover Settings Modal] Error loading settings:', error);
            showNotification('Failed to load settings', 'error');
        }
    };

    /**
     * Close the discover settings modal
     */
    function closeModal() {
        if (modal) {
            modal.style.display = 'none';
            document.body.style.overflow = ''; // Restore scrolling
        }
    }

    /**
     * Save discover settings using the same API as settings page
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

            // Update Discover Settings in the config
            const updatedSettings = { ...currentSettings };

            if (!updatedSettings['Discover Settings']) {
                updatedSettings['Discover Settings'] = {};
            }

            updatedSettings['Discover Settings'].hide_no_rating = hideNoRatingInput.checked;
            updatedSettings['Discover Settings'].hide_no_poster = hideNoPosterInput.checked;
            updatedSettings['Discover Settings'].only_show_missing = onlyShowMissingInput.checked;
            updatedSettings['Discover Settings'].tv_show_episode_view = discoverEpisodeViewSelect.value;
            if (hideSpecialsInput) {
                updatedSettings['Discover Settings'].hide_specials = hideSpecialsInput.checked;
            }

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

                // Reload page immediately to reflect changes in discover results
                window.location.reload();
            } else {
                throw new Error(result.message || 'Failed to save settings');
            }

        } catch (error) {
            console.error('[Discover Settings Modal] Error saving settings:', error);
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
     * Show notification (uses global notification system if available)
     */
    function showNotification(message, type = 'info') {
        if (typeof window.showNotification === 'function') {
            window.showNotification(message, type);
        } else {
            console.log(`[Discover Settings Modal] ${type.toUpperCase()}: ${message}`);
            alert(message);
        }
    }

    /**
     * Initialize event listeners after DOM is ready
     */
    function initializeEventListeners() {
        document.addEventListener('DOMContentLoaded', function() {
            if (!initializeElements()) {
                return;
            }

            // Close button
            if (closeBtn) {
                closeBtn.addEventListener('click', closeModal);
            }

            // Cancel button
            if (cancelBtn) {
                cancelBtn.addEventListener('click', closeModal);
            }

            // Save button
            if (saveBtn) {
                saveBtn.addEventListener('click', saveSettings);
            }

            // Overlay click (close modal)
            if (overlay) {
                overlay.addEventListener('click', closeModal);
            }

            // ESC key to close
            document.addEventListener('keydown', function(e) {
                if (e.key === 'Escape' && modal && modal.style.display === 'flex') {
                    closeModal();
                }
            });

            // Settings button in discover page
            const settingsBtn = document.getElementById('discover-settings-btn');
            if (settingsBtn) {
                settingsBtn.addEventListener('click', window.openDiscoverSettingsModal);
            }
        });
    }

    // Initialize
    initializeEventListeners();

})();
