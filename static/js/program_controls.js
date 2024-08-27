import { showPopup, POPUP_TYPES } from './notifications.js';

export function initializeProgramControls() {
    const controlButton = document.getElementById('programControlButton');
    if (!controlButton) return; // Exit if the button doesn't exist

    let currentStatus = 'Initialized';
    let currentSettings = {};

    function updateButtonState(status) {
        const buttonText = status === 'Running' ? 'Stop Program' : 'Start Program';
        const iconClass = status === 'Running' ? 'fa-stop' : 'fa-play';
        
        // Update button content while preserving the icon
        controlButton.innerHTML = `<i class="fas ${iconClass}"></i> <span class="button-text">${buttonText}</span>`;
        
        controlButton.setAttribute('data-status', status === 'Running' ? 'Running' : 'Initialized');
        controlButton.classList.toggle('stop-program', status === 'Running');
        controlButton.classList.toggle('start-program', status !== 'Running');
        
        controlButton.disabled = false; // Always enable the button
        currentStatus = status;
        updateSettingsManagement(status === 'Running');
    }

    function updateSettingsManagement(isRunning) {
        const buttons = document.querySelectorAll('#saveSettingsButton, .add-scraper-link, .add-version-link, .add-source-link, .delete-scraper-btn, .delete-version-btn, .duplicate-version-btn, .delete-source-btn');
        buttons.forEach(button => {
            button.disabled = isRunning;
            button.style.opacity = isRunning ? '0.5' : '1';
            button.style.cursor = isRunning ? 'not-allowed' : 'pointer';
        });
    
        const runningMessage = document.getElementById('programRunningMessage');
        const settingsContainer = document.querySelector('.settings-container');
    
        if (isRunning) {
            if (!runningMessage && settingsContainer) {
                const message = document.createElement('div');
                message.id = 'programRunningMessage';
                message.textContent = 'Program is running. Settings management is disabled.';
                message.style.color = 'red';
                message.style.marginBottom = '10px';
                settingsContainer.prepend(message);
            }
        } else if (runningMessage) {
            runningMessage.remove();
        }
    }

    function updateStatus() {
        fetch('/api/program_status')
            .then(response => response.json())
            .then(data => {
                updateButtonState(data.running ? 'Running' : 'Initialized');
            })
            .catch(error => {
                console.error('Error fetching program status:', error);
            });
    }

    function checkRequiredConditions() {
        let scrapersEnabled = false;
        let contentSourcesEnabled = false;
        let requiredSettingsComplete = true;

        // Check if at least one scraper is enabled
        if (currentSettings.Scrapers) {
            scrapersEnabled = Object.values(currentSettings.Scrapers).some(scraper => scraper.enabled);
        }

        // Check if at least one content source is enabled
        if (currentSettings['Content Sources']) {
            contentSourcesEnabled = Object.values(currentSettings['Content Sources']).some(source => source.enabled);
        }

        // Check required settings
        const requiredFields = [
            'Plex.url',
            'Plex.token',
            'Overseerr.url',
            'Overseerr.api_key',
            'RealDebrid.api_key'
        ];

        requiredFields.forEach(field => {
            const [section, key] = field.split('.');
            if (!currentSettings[section] || !currentSettings[section][key]) {
                requiredSettingsComplete = false;
            }
        });

        return {
            canRun: scrapersEnabled && contentSourcesEnabled && requiredSettingsComplete,
            scrapersEnabled,
            contentSourcesEnabled,
            requiredSettingsComplete
        };
    }

    function showErrorPopup(message) {
        showPopup({
            type: POPUP_TYPES.ERROR,
            title: 'Unable to Start Program',
            message: message
        });
    }

    controlButton.addEventListener('click', function() {
        if (currentStatus !== 'Running') {
            const conditions = checkRequiredConditions();
            if (!conditions.canRun) {
                let errorMessage = "The program cannot start due to the following reasons:<ul>";
                if (!conditions.scrapersEnabled) {
                    errorMessage += "<li>No scrapers are enabled. Please enable at least one scraper.</li>";
                }
                if (!conditions.contentSourcesEnabled) {
                    errorMessage += "<li>No content sources are enabled. Please enable at least one content source.</li>";
                }
                if (!conditions.requiredSettingsComplete) {
                    errorMessage += "<li>Some required settings are missing. Please fill in all fields in the Required Settings tab (Plex, Overseerr, and RealDebrid settings).</li>";
                }
                errorMessage += "</ul>";
                showErrorPopup(errorMessage);
                return;
            }
        }

        const action = currentStatus === 'Running' ? 'reset' : 'start';
        fetch(`/api/${action}_program`, { method: 'POST' })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    updateStatus();
                } else {
                    showErrorPopup(data.message);
                }
            })
            .catch(error => {
                console.error('Error controlling program:', error);
                showErrorPopup('An error occurred while trying to control the program. Please check the console for more details.');
            });
    });

    // Fetch current settings
    function fetchSettings() {
        fetch('/api/program_settings')
            .then(response => response.json())
            .then(data => {
                currentSettings = data;
                updateStatus(); // Update status after fetching settings
            })
            .catch(error => {
                console.error('Error fetching program settings:', error);
            });
    }

    // Fetch settings initially and then every 30 seconds
    fetchSettings();
    setInterval(fetchSettings, 30000);

    // Update status every 5 seconds
    setInterval(updateStatus, 5000);

    // Add event listeners to update button state when settings change
    document.addEventListener('change', function(event) {
        if (event.target.matches('#scrapers input[type="checkbox"], #content-sources input[type="checkbox"], #required input')) {
            updateButtonState(currentStatus);
        }
    });
}