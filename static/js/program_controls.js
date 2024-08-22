document.addEventListener('DOMContentLoaded', function() {
    const controlButton = document.getElementById('programControlButton');
    let currentStatus = 'Initialized';

    function updateButtonState(status) {
        if (status === 'Running') {
            controlButton.textContent = 'Stop Program';
            controlButton.setAttribute('data-status', 'Running');
        } else {
            controlButton.textContent = 'Start Program';
            controlButton.setAttribute('data-status', 'Initialized');
        }
        currentStatus = status;
    }

    function updateStatus() {
        fetch('/api/program_status')
            .then(response => response.json())
            .then(data => {
                updateButtonState(data.status);
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