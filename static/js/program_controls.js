import { showPopup, POPUP_TYPES } from './notifications.js';

let currentSettings = {};

export function initializeProgramControls() {
    if (window.isRateLimited) {
        console.log("Rate limit exceeded. Skipping program controls initialization.");
        return;
    }

    const programControlButton = document.getElementById('programControlButton');
    if (!programControlButton) return;

    // Use the initial state from the data attribute
    const initialStatus = document.body.dataset.programStatus;
    updateButtonState(initialStatus === 'Running');

    programControlButton.addEventListener('click', toggleProgram);

    function toggleProgram() {
        const currentStatus = programControlButton.dataset.status;
        const action = currentStatus === 'Running' ? 'stop' : 'start';

        if (action === 'start') {
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
                    errorMessage += "<li>Some required settings are missing. Please fill in all fields in the Required Settings tab (Plex, RealDebrid, and Metadata Battery settings).</li>";
                }
                errorMessage += "</ul>";
                showErrorPopup(errorMessage);
                return;
            }
        }

        fetch(`/program_operation/api/${action}_program`, { method: 'POST' })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    updateButtonState(action === 'start');
                } else {
                    showErrorPopup(data.message);
                }
            })
            .catch(error => {
                console.error('Error controlling program:', error);
                showErrorPopup('An error occurred while trying to control the program. Please check the console for more details.');
            });
    }

    function updateButtonState(isRunning) {
        if (isRunning) {
            programControlButton.innerHTML = '<i class="fas fa-stop"></i><span class="button-text">Stop Program</span>';
            programControlButton.classList.remove('start-program');
            programControlButton.classList.add('stop-program');
            programControlButton.dataset.status = 'Running';
        } else {
            programControlButton.innerHTML = '<i class="fas fa-play"></i><span class="button-text">Start Program</span>';
            programControlButton.classList.remove('stop-program');
            programControlButton.classList.add('start-program');
            programControlButton.dataset.status = 'Stopped';
        }
        updateSettingsManagement(isRunning);
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
            'RealDebrid.api_key',
            'Metadata Battery.url'
        ];

        console.log(currentSettings);
        console.log(requiredFields);

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
            title: 'Program Control Error',
            message: message
        });
    }

    function fetchSettings() {
        if (window.isRateLimited) {
            console.log("Rate limit exceeded. Skipping settings fetch.");
            return Promise.resolve({}); // Return an empty object or default settings
        }

        fetch('/settings/api/program_settings')
            .then(response => response.json())
            .then(data => {
                currentSettings = data;
                updateButtonState(programControlButton.dataset.status === 'Running');
            })
            .catch(error => {
                console.error('Error fetching program settings:', error);
            });
    }

    // Fetch settings initially and then every 30 seconds
    fetchSettings();
    setInterval(fetchSettings, 30000);

    // Fetch program status every 30 seconds
    setInterval(() => {
        fetch('/program_operation/api/program_status')
            .then(response => response.json())
            .then(data => {
                updateButtonState(data.running);
            })
            .catch(error => console.error('Error fetching program status:', error));
    }, 30000);

    // Add event listeners to update button state when settings change
    document.addEventListener('change', function(event) {
        if (event.target.matches('#scrapers input[type="checkbox"], #content-sources input[type="checkbox"], #required input')) {
            updateButtonState(programControlButton.dataset.status === 'Running');
        }
    });
}
