/* Moved from templates/onboarding_step_1.html. */
import { showPopup, POPUP_TYPES } from '/static/js/notifications.js';

document.addEventListener('DOMContentLoaded', function() {
    const welcomeMessage = document.getElementById('welcome-message');
    const step1Content = document.getElementById('step-1-content');
    const startOnboardingBtn = document.getElementById('start-onboarding');
    const adminForm = document.getElementById('admin-form');
    const nextStepBtn = document.querySelector('.next-step-btn');

    if (startOnboardingBtn) {
        startOnboardingBtn.addEventListener('click', function() {
            welcomeMessage.style.display = 'none';
            step1Content.style.display = 'block';
        });
    }

    if (adminForm) {
        adminForm.addEventListener('submit', function(event) {
            event.preventDefault();
            const formData = new FormData(adminForm);
            fetch(window.pageData.onboarding_step_1.onboarding_onboarding_step_url, {
                method: 'POST',
                body: formData
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    updateUIAfterAdminCreation();
                    showPopup({
                        type: POPUP_TYPES.SUCCESS,
                        title: 'Success',
                        message: 'Admin account created successfully!',
                        autoClose: 5000
                    });
                } else {
                    showPopup({
                        type: POPUP_TYPES.ERROR,
                        title: 'Error',
                        message: 'Error: ' + data.error,
                    });
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error',
                    message: 'An unexpected error occurred.',
                });
            });
        });
    }

    function updateUIAfterAdminCreation() {
        // Update the content of step-1-content
        step1Content.innerHTML = '<h2 class="onboarding-title">Step 1: Setup Admin Account</h2><p class="onboarding-text">The admin account has been successfully created. You can proceed to the next step.</p>';

        // Enable the Next Step button
        if (nextStepBtn) {
            nextStepBtn.disabled = false;
        }

        // Update the server-side can_proceed status
        fetch('/onboarding/update_can_proceed', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ step: 1, can_proceed: true })
        })
        .then(response => response.json())
        .then(data => {
            if (!data.success) {
                console.error('Failed to update can_proceed status');
            }
        })
        .catch(error => {
            console.error('Error updating can_proceed status:', error);
        });
    }

    // Initialize the Next Step button state
    if (nextStepBtn) {
        nextStepBtn.disabled = !window.pageData.onboarding_step_1.can_proceed;
    }
});
