document.addEventListener('DOMContentLoaded', function() {
    const controlButton = document.getElementById('programControlButton');
    let currentStatus = 'Initialized';

    function updateButtonState(status) {
        const canRun = checkRequiredConditions();
        if (status === 'Running') {
            controlButton.textContent = 'Stop Program';
            controlButton.setAttribute('data-status', 'Running');
            controlButton.classList.remove('start-program');
            controlButton.classList.add('stop-program');
        } else {
            controlButton.textContent = 'Start Program';
            controlButton.setAttribute('data-status', 'Initialized');
            controlButton.classList.remove('stop-program');
            controlButton.classList.add('start-program');
        }
        controlButton.disabled = !canRun && status !== 'Running';
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
        const scrapers = document.querySelectorAll('#scrapers .settings-section');
        scrapers.forEach(scraper => {
            const enabledCheckbox = scraper.querySelector('input[name$=".enabled"]');
            if (enabledCheckbox && enabledCheckbox.checked) {
                scrapersEnabled = true;
            }
        });

        // Check if at least one content source is enabled
        const contentSources = document.querySelectorAll('#content-sources .settings-section');
        contentSources.forEach(source => {
            const enabledCheckbox = source.querySelector('input[name$=".enabled"]');
            if (enabledCheckbox && enabledCheckbox.checked) {
                contentSourcesEnabled = true;
            }
        });

        // Explicitly check each required field
        const requiredFields = [
            'plex-url',
            'plex-token',
            'overseerr-url',
            'overseerr-api_key',
            'realdebrid-api_key'
        ];

        requiredFields.forEach(fieldId => {
            const field = document.getElementById(fieldId);
            if (!field || !field.value.trim()) {
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
        const popup = document.createElement('div');
        popup.className = 'error-popup';
        popup.innerHTML = `
            <div class="error-popup-content">
                <h3>Unable to Start Program</h3>
                <p>${message}</p>
                <button onclick="this.parentElement.parentElement.remove()">Close</button>
            </div>
        `;
        document.body.appendChild(popup);
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

    // Set initial button text
    controlButton.textContent = 'Start Program';

    // Update status immediately and then every 5 seconds
    updateStatus();
    setInterval(updateStatus, 5000);

    // Add event listeners to update button state when settings change
    document.addEventListener('change', function(event) {
        if (event.target.matches('#scrapers input[type="checkbox"], #content-sources input[type="checkbox"], #required input')) {
            updateButtonState(currentStatus);
        }
    });
});