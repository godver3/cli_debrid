/**
 * Overlay Settings Modal
 * Provides a quick-access modal for Overlay settings
 * Uses the same API endpoints as the main settings page
 */

(function() {
    'use strict';

    // Load theme-specific CSS dynamically
    // Always loads the base CSS; for tangerine also loads the theme override on top
    function loadThemeCSS() {
        const getCurrentTheme = () => localStorage.getItem('theme') || 'default';
        const theme = getCurrentTheme();

        // Ensure base CSS is always present
        if (!document.querySelector('link[href*="overlay_settings_modal.css"]:not([href*="tangerine"])')) {
            const link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = '/static/css/overlay_settings_modal.css';
            document.head.appendChild(link);
        }

        // Add tangerine override on top if needed
        const tangerineId = 'tangerine-overlay-settings-css';
        if (theme === 'tangerine') {
            if (!document.getElementById(tangerineId)) {
                const link = document.createElement('link');
                link.id = tangerineId;
                link.rel = 'stylesheet';
                link.href = '/static/css/tangerine/tangerine_overlay_settings_modal.css';
                document.head.appendChild(link);
            }
        } else {
            const existing = document.getElementById(tangerineId);
            if (existing) existing.remove();
        }
    }

    // Load CSS immediately
    loadThemeCSS();

    let modal, overlay, closeBtn, cancelBtn, saveBtn;
    let overlaysEnabledInput, mediaDataPathInput, contentCheckDaysInput, syncItemsPerRunInput;
    let textlessPosterInput;
    let currentSettings = null;

    function initializeElements() {
        modal = document.getElementById('overlaySettingsModal');
        if (!modal) {
            console.error('[Overlay Settings Modal] Modal element not found in DOM');
            return false;
        }

        overlay = modal.querySelector('.overlay-settings-modal-overlay');
        closeBtn = modal.querySelector('.overlay-settings-modal-close');
        cancelBtn = modal.querySelector('.overlay-settings-btn-cancel');
        saveBtn = modal.querySelector('.overlay-settings-btn-save');

        overlaysEnabledInput = document.getElementById('overlaySettingsEnabled');
        mediaDataPathInput = document.getElementById('overlaySettingsMediaDataPath');
        contentCheckDaysInput = document.getElementById('overlaySettingsContentCheckDays');
        syncItemsPerRunInput = document.getElementById('overlaySettingsSyncItemsPerRun');
        textlessPosterInput = document.getElementById('overlaySettingsTextlessPoster');
        textlessPosterInput = document.getElementById('overlaySettingsTextlessPoster');
        textlessPosterInput = document.getElementById('overlaySettingsTextlessPoster');

        return true;
    }

    /**
     * Open the overlay settings modal
     */
    window.openOverlaySettingsModal = async function() {
        if (!modal && !initializeElements()) {
            console.error('[Overlay Settings Modal] Failed to initialize modal elements');
            return;
        }

        try {
            const response = await fetch('/settings/api/config');
            if (!response.ok) {
                throw new Error('Failed to fetch settings');
            }

            const config = await response.json();
            currentSettings = config;

            const overlaySettings = config['Overlay Settings'] || {};

            if (overlaysEnabledInput) {
                overlaysEnabledInput.checked = overlaySettings.overlays_enabled || false;
            }

            if (textlessPosterInput) {
                textlessPosterInput.checked = overlaySettings.textless_posters || false;
            }

            if (textlessPosterInput) {
                textlessPosterInput.checked = overlaySettings.textless_posters || false;
            }

            if (textlessPosterInput) {
                textlessPosterInput.checked = overlaySettings.textless_posters || false;
            }

            if (mediaDataPathInput) {
                mediaDataPathInput.value = overlaySettings.plex_data_path || '';
            }

            if (contentCheckDaysInput) {
                contentCheckDaysInput.value = overlaySettings.overlay_content_check_interval_days ?? 7;
            }

            if (syncItemsPerRunInput) {
                syncItemsPerRunInput.value = overlaySettings.sync_items_per_run ?? 200;
            }

            modal.style.display = 'flex';
            document.body.style.overflow = 'hidden';

        } catch (error) {
            console.error('[Overlay Settings Modal] Error loading settings:', error);
            showNotification('Failed to load settings', 'error');
        }
    };

    /**
     * Close the overlay settings modal
     */
    function closeModal() {
        if (modal) {
            modal.style.display = 'none';
            document.body.style.overflow = '';
        }
    }

    /**
     * Save overlay settings using the same API as the settings page
     */
    async function saveSettings() {
        if (!currentSettings) {
            showNotification('No settings loaded', 'error');
            return;
        }

        try {
            if (saveBtn) {
                saveBtn.disabled = true;
                saveBtn.textContent = 'Saving...';
            }

            const updatedSettings = { ...currentSettings };

            if (!updatedSettings['Overlay Settings']) {
                updatedSettings['Overlay Settings'] = {};
            }

            updatedSettings['Overlay Settings'].overlays_enabled = overlaysEnabledInput.checked;
            if (textlessPosterInput) {
                updatedSettings['Overlay Settings'].textless_posters = textlessPosterInput.checked;
            }
            if (textlessPosterInput) {
                updatedSettings['Overlay Settings'].textless_posters = textlessPosterInput.checked;
            }
            if (textlessPosterInput) {
                updatedSettings['Overlay Settings'].textless_posters = textlessPosterInput.checked;
            }
            updatedSettings['Overlay Settings'].plex_data_path = mediaDataPathInput.value.trim();
            if (contentCheckDaysInput) {
                updatedSettings['Overlay Settings'].overlay_content_check_interval_days = parseInt(contentCheckDaysInput.value) || 7;
            }
            if (syncItemsPerRunInput) {
                updatedSettings['Overlay Settings'].sync_items_per_run = parseInt(syncItemsPerRunInput.value) || 200;
            }

            const response = await fetch('/settings/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(updatedSettings)
            });

            const result = await response.json();

            if (response.ok && result.status === 'success') {
                closeModal();
                showNotification('Overlay settings saved', 'success');
            } else {
                throw new Error(result.message || 'Failed to save settings');
            }

        } catch (error) {
            console.error('[Overlay Settings Modal] Error saving settings:', error);
            showNotification(error.message || 'Failed to save settings', 'error');
        } finally {
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save Settings';
            }
        }
    }

    function showNotification(message, type = 'info') {
        if (typeof window.showToast === 'function') {
            window.showToast(message, type);
        } else if (typeof window.showNotification === 'function') {
            window.showNotification(message, type);
        } else {
            showPopup({ type: 'info', title: 'Notice', message: message, autoClose: 4000 });
        }
    }

    // ── Poster Reset ──────────────────────────────────────────────────────────

    let _resetPollTimer = null;

    window.openPosterReviewGrid = function() {
        // Close the settings modal first so it doesn't sit on top of the review grid
        closeModal();
        // If already on the overlays page the function is defined there — call it.
        // Otherwise navigate to the overlays page with a hash that auto-opens the grid.
        if (typeof window._openPosterReviewGridLocal === 'function') {
            window._openPosterReviewGridLocal();
        } else {
            window.location.href = '/overlays#open-review-grid';
        }
    };

    window.openPosterResetConfirm = function() {
        const msg =
            'This will replace ALL posters in your library with clean originals from TMDB.\n\n' +
            '• Any foreign overlays (Kometa, PMM, etc.) will be removed\n' +
            '• Custom posters you have set manually will also be overwritten\n' +
            '• cli_debrid overlays will NOT be re-applied automatically\n' +
            '  — use Generate All on the overlay page when ready\n\n' +
            'Continue?';
        showPopup({
            type: 'confirm',
            title: 'Reset All Posters',
            message: msg,
            confirmText: 'Confirm',
            cancelText: 'Cancel',
            onConfirm: function() { _startResetJob(null); }
        });
        return;
    };

    window.cancelPosterReset = async function() {
        try {
            await fetch('/api/overlays/reset/cancel', { method: 'POST' });
        } catch (_) {}
    };

    async function _startResetJob(plex_rating_keys) {
        const endpoint = plex_rating_keys ? '/api/overlays/reset/selective' : '/api/overlays/reset/start';
        const body = plex_rating_keys
            ? JSON.stringify({ plex_rating_keys, reset_seasons: true })
            : JSON.stringify({ reset_seasons: true });

        try {
            const r = await fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body,
            });
            const d = await r.json();
            if (!d.success) {
                showNotification(d.error || 'Failed to start reset', 'error');
                return;
            }
            _showResetProgress();
            _pollResetStatus();
        } catch (e) {
            showNotification('Failed to start reset job', 'error');
        }
    }

    function _showResetProgress() {
        const el = document.getElementById('posterResetProgress');
        if (el) el.style.display = 'block';
    }

    function _hideResetProgress() {
        const el = document.getElementById('posterResetProgress');
        if (el) el.style.display = 'none';
    }

    function _pollResetStatus() {
        if (_resetPollTimer) clearInterval(_resetPollTimer);
        _resetPollTimer = setInterval(async () => {
            try {
                const r = await fetch('/api/overlays/reset/status');
                const d = await r.json();
                if (!d.success) return;

                const txt = document.getElementById('posterResetProgressText');
                const pct = document.getElementById('posterResetProgressPct');
                const bar = document.getElementById('posterResetProgressBar');

                const label = d.current
                    ? `Resetting: ${d.current}`
                    : `${d.done} reset · ${d.failed} failed · ${d.skipped} skipped`;

                if (txt) txt.textContent = label;
                if (pct) pct.textContent = `${d.percent}%`;
                if (bar) bar.style.width = `${d.percent}%`;

                if (!d.running) {
                    clearInterval(_resetPollTimer);
                    _resetPollTimer = null;
                    const summary = `Poster reset complete: ${d.done} reset, ${d.failed} failed, ${d.skipped} skipped`;
                    if (txt) txt.textContent = summary;
                    if (pct) pct.textContent = '100%';
                    if (bar) bar.style.width = '100%';
                    showNotification(summary, d.failed > 0 ? 'warning' : 'success');
                    // Refresh overlay stats on the main page if visible
                    if (typeof window.loadStats === 'function') window.loadStats();
                }
            } catch (_) {}
        }, 1500);
    }

    // Expose _startResetJob for Phase 2 review grid
    window._startSelectiveResetJob = function(keys) {
        _startResetJob(keys);
    };

    function setupEventListeners() {
        if (!initializeElements()) {
            console.warn('[Overlay Settings Modal] Elements not ready, event listeners not attached');
            return;
        }

        if (closeBtn) closeBtn.addEventListener('click', closeModal);
        if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
        if (overlay) overlay.addEventListener('click', closeModal);
        if (saveBtn) saveBtn.addEventListener('click', saveSettings);

        document.addEventListener('keydown', function(event) {
            if (event.key === 'Escape' && modal && modal.style.display === 'flex') {
                closeModal();
            }
        });

        console.log('[Overlay Settings Modal] Initialized successfully');
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', setupEventListeners);
    } else {
        setupEventListeners();
    }
})();
