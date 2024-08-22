document.addEventListener('DOMContentLoaded', function() {
    const controlButton = document.getElementById('programControlButton');
    let currentStatus = 'Initialized';

    function updateButtonState(status) {
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

    controlButton.addEventListener('click', function() {
        const action = currentStatus === 'Running' ? 'reset' : 'start';
        fetch(`/api/${action}_program`, { method: 'POST' })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    updateStatus();
                } else {
                    alert(data.message);
                }
            })
            .catch(error => {
                console.error('Error controlling program:', error);
            });
    });

    // Set initial button text
    controlButton.textContent = 'Start Program';

    // Update status immediately and then every 5 seconds
    updateStatus();
    setInterval(updateStatus, 5000);
});