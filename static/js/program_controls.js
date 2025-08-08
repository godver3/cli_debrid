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
    updateButtonState(initialStatus);

    programControlButton.addEventListener('click', toggleProgram);
    // Add touch event handling for mobile
    programControlButton.addEventListener('touchstart', function(e) {
        e.preventDefault();  // Prevent double-firing on mobile
        toggleProgram();
    });

    function toggleProgram() {
        const currentStatus = programControlButton.dataset.status;
        const action = currentStatus === 'Running' ? 'stop' : 'start';

        if (currentStatus === 'Starting' || currentStatus === 'Stopping') {
            return;
        }

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
                    errorMessage += "<li>Some required settings are missing. Please fill in all fields in the Required Settings tab (Plex, Debrid Provider, and Metadata Battery settings).</li>";
                }
                errorMessage += "</ul>";
                showErrorPopup(errorMessage);
                return;
            }
        }

        fetch(`/program_operation/api/${action}_program`, { 
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({}) // Send empty JSON object
        })
            .then(response => {
                // Check if the response is actually JSON
                const contentType = response.headers.get('content-type');
                if (!contentType || !contentType.includes('application/json')) {
                    throw new Error(`Expected JSON response but got ${contentType || 'unknown content type'}`);
                }
                return response.json();
            })
            .then(data => {
                if (data.status === 'success') {
                    updateButtonState(action === 'start' ? 'Starting' : 'Stopping');
                } else {
                    showErrorPopup(data.message);
                    fetchProgramStatus();
                }
            })
            .catch(error => {
                console.error('Error controlling program:', error);
                showErrorPopup('An error occurred while trying to control the program. Please check the console for more details.');
                fetchProgramStatus();
            });
    }

    function updateButtonState(status) {
        programControlButton.disabled = false;
        
        let iconClass = '';
        let buttonText = '';
        let buttonClasses = ['icon-button']; // Base class

        // Remove all state classes first to avoid conflicts
        programControlButton.classList.remove('start-program', 'stop-program', 'starting-program', 'stopping-program');

        switch(status) {
            case 'Running':
                iconClass = 'fa-stop';
                buttonText = 'Stop Program';
                buttonClasses.push('stop-program');
                break;
            case 'Starting':
                iconClass = 'fa-spinner fa-spin';
                buttonText = 'Starting...';
                buttonClasses.push('starting-program');
                programControlButton.disabled = true;
                break;
            case 'Stopping':
                iconClass = 'fa-spinner fa-spin';
                buttonText = 'Stopping...';
                buttonClasses.push('stopping-program');
                programControlButton.disabled = true;
                break;
            case 'Stopped':
            default:
                if (status !== 'Stopped' && status) { // Log if status is unknown but not empty
                    console.warn(`Unknown program status: '${status}'. Defaulting to 'Stopped'.`);
                }
                iconClass = 'fa-play';
                buttonText = 'Start Program';
                buttonClasses.push('start-program');
                status = 'Stopped'; // Normalize status for dataset
                break;
        }

        programControlButton.className = buttonClasses.join(' ');
        programControlButton.innerHTML = `<i class="fas ${iconClass}"></i><span class="button-text">${buttonText}</span>`;
        programControlButton.dataset.status = status;

        const isBusy = (status === 'Running' || status === 'Starting' || status === 'Stopping');
        updateSettingsManagement(isBusy);
    }

    function updateSettingsManagement(isBusy) {
        // DISABLED: Allow settings changes while program is running for testing
        // const buttons = document.querySelectorAll('#saveSettingsButton, .add-scraper-link, .add-version-link, .add-source-link, .delete-scraper-btn, .delete-version-btn, .duplicate-version-btn, .delete-source-btn, .import-versions-link');
        // buttons.forEach(button => {
        //     button.disabled = isBusy;
        //     button.style.opacity = isBusy ? '0.5' : '1';
        //     button.style.cursor = isBusy ? 'not-allowed' : 'pointer';
        // });
    
        // const runningMessage = document.getElementById('programRunningMessage');
        // const settingsContainer = document.querySelector('.settings-container');
    
        // if (isBusy) {
        //     if (!runningMessage && settingsContainer) {
        //         const message = document.createElement('div');
        //         message.id = 'programRunningMessage';
        //         message.textContent = 'Program is running. Settings management is disabled.';
        //         message.style.color = 'red';
        //         message.style.marginBottom = '10px';
        //         settingsContainer.prepend(message);
        //     }
        // } else if (runningMessage) {
        //     runningMessage.remove();
        // }
    }

    function checkRequiredConditions() {
        let scrapersEnabled = false;
        let contentSourcesEnabled = false;
        let requiredSettingsComplete = true;

        // Check if at least one scraper is enabled
        if (currentSettings.Scrapers) {
            scrapersEnabled = Object.values(currentSettings.Scrapers).some(scraper => scraper.enabled);
        }

        // Consider presence of any content source as enabled; runtime toggles control actual task execution
        if (currentSettings['Content Sources']) {
            contentSourcesEnabled = Object.values(currentSettings['Content Sources']).length > 0;
        }

        // Check required settings
        const requiredFields = [
            //'Plex.url',
            //'Plex.token',
            'Debrid Provider.provider',
            'Debrid Provider.api_key',
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
            return;
        }

        fetch('/settings/api/program_settings')
            .then(response => {
                // Check if the response is actually JSON
                const contentType = response.headers.get('content-type');
                if (!contentType || !contentType.includes('application/json')) {
                    throw new Error(`Expected JSON response but got ${contentType || 'unknown content type'}`);
                }
                return response.json();
            })
            .then(data => {
                currentSettings = data;
            })
            .catch(error => {
                console.error('Error fetching program settings:', error);
            });
    }

    function fetchProgramStatus() {
        fetch('/program_operation/api/program_status')
            .then(response => {
                // Check if the response is actually JSON
                const contentType = response.headers.get('content-type');
                if (!contentType || !contentType.includes('application/json')) {
                    throw new Error(`Expected JSON response but got ${contentType || 'unknown content type'}`);
                }
                return response.json();
            })
            .then(data => {
                updateButtonState(data.status);
            })
            .catch(error => {
                console.error('Error fetching program status:', error);
                // Don't update button state on error to avoid UI issues
            });
    }

    // Fetch settings initially and then every 30 seconds
    fetchSettings();
    setInterval(fetchSettings, 30000);

    // Fetch program status every 5 seconds for responsiveness
    setInterval(fetchProgramStatus, 5000);

    // Add event listeners to update button state when settings change
    document.addEventListener('change', function(event) {
        if (event.target.matches('#scrapers input[type="checkbox"], #content-sources input[type="checkbox"], #required input')) {
            updateButtonState(programControlButton.dataset.status);
        }
    });
}
