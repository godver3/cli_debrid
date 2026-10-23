/* Moved from templates/settings_tabs/additional.html. */
document.addEventListener('DOMContentLoaded', function() {
    // Bazarr API Key Generation
    const generateKeyBtn = document.getElementById('bazarr-generate-key');
    const apiKeyInput = document.getElementById('bazarr-api_key');

    if (generateKeyBtn && apiKeyInput) {
        generateKeyBtn.addEventListener('click', function() {
            fetch('/settings/api/bazarr/generate_key', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success && data.api_key) {
                    apiKeyInput.value = data.api_key;
                    apiKeyInput.type = 'text';
                    document.getElementById('bazarr-show-key').textContent = 'Hide';
                    showPopup({ type: 'success', title: 'Success', message: 'New API key generated! Remember to save settings and update your Bazarr configuration.', autoClose: 4000 });
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Failed to generate API key', autoClose: 5000 });
                }
            })
            .catch(error => {
                console.error('Error generating API key:', error);
                showPopup({ type: 'error', title: 'Error', message: 'Error generating API key. Check console for details.', autoClose: 5000 });
            });
        });
    }

    // Copy API Key
    const copyKeyBtn = document.getElementById('bazarr-copy-key');
    if (copyKeyBtn && apiKeyInput) {
        copyKeyBtn.addEventListener('click', function() {
            const key = apiKeyInput.value;
            if (key) {
                navigator.clipboard.writeText(key).then(() => {
                    const originalText = copyKeyBtn.textContent;
                    copyKeyBtn.textContent = 'Copied!';
                    setTimeout(() => { copyKeyBtn.textContent = originalText; }, 2000);
                }).catch(err => {
                    console.error('Failed to copy:', err);
                    showPopup({ type: 'error', title: 'Error', message: 'Failed to copy to clipboard', autoClose: 5000 });
                });
            } else {
                showPopup({ type: 'warning', title: 'Required', message: 'No API key to copy. Generate one first.', autoClose: 4000 });
            }
        });
    }

    // Show/Hide API Key
    const showKeyBtn = document.getElementById('bazarr-show-key');
    if (showKeyBtn && apiKeyInput) {
        showKeyBtn.addEventListener('click', function() {
            if (apiKeyInput.type === 'password') {
                apiKeyInput.type = 'text';
                showKeyBtn.textContent = 'Hide';
            } else {
                apiKeyInput.type = 'password';
                showKeyBtn.textContent = 'Show';
            }
        });
    }

    // Update URL display with actual host
    const urlSpan = document.getElementById('bazarr-url');
    if (urlSpan) {
        urlSpan.textContent = window.location.protocol + '//' + window.location.host;
    }
});
