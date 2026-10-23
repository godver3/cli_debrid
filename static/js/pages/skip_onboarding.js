/* Moved from templates/skip_onboarding.html. */
import { showPopup, POPUP_TYPES } from '/static/js/notifications.js';

document.addEventListener('DOMContentLoaded', function() {
    const skipButton = document.getElementById('skip-onboarding-btn');

    skipButton.addEventListener('click', function() {
        fetch(window.pageData.skip_onboarding.onboarding_skip_onboarding_url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    title: 'Success',
                    message: 'Onboarding skipped successfully!',
                    onClose: () => {
                        window.location.href = window.pageData.skip_onboarding.root_root_url;
                    }
                });
            } else {
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error',
                    message: data.error || 'Failed to skip onboarding'
                });
            }
        })
        .catch(error => {
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error',
                message: 'An unexpected error occurred'
            });
        });
    });
});
