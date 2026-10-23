/* Moved from templates/onboarding_navigation.html. */
document.addEventListener('DOMContentLoaded', function() {
    const prevStepBtn = document.querySelector('.prev-step-btn');
    const nextStepBtn = document.querySelector('.next-step-btn');
    const finishSetupBtn = document.querySelector('.finish-setup-btn');
    const skipBtn = document.querySelector('.skip-onboarding-btn');

    if (skipBtn) {
        skipBtn.addEventListener('click', function() {
            window.location.href = window.pageData.onboarding_navigation.onboarding_skip_onboarding_url;
        });
    }

    if (prevStepBtn) {
        prevStepBtn.addEventListener('click', function() {
            const currentStep = window.pageData.onboarding_navigation.current_step;
            if (currentStep === '4') {
                window.location.href = window.pageData.onboarding_navigation.onboarding_onboarding_step_url;
            } else if (currentStep === '3a') {
                window.location.href = window.pageData.onboarding_navigation.onboarding_onboarding_step_url_2;
            } else {
                const prevStep = parseInt(currentStep) - 1;
                window.location.href = window.pageData.onboarding_navigation.onboarding_onboarding_step_url_3 + prevStep;
            }
        });
    }

    if (nextStepBtn) {
        const currentStep = window.pageData.onboarding_navigation.current_step;
        if (currentStep === '3') {
            nextStepBtn.addEventListener('click', function() {
                if (!this.disabled) {
                    window.location.href = window.pageData.onboarding_navigation.onboarding_onboarding_step_url;
                }
            });
        } else if (currentStep === '3a') {
            nextStepBtn.addEventListener('click', function() {
                if (!this.disabled) {
                    window.location.href = window.pageData.onboarding_navigation.onboarding_onboarding_step_url_4;
                }
            });
        } else if (currentStep !== '2') {
            nextStepBtn.addEventListener('click', function() {
                if (!this.disabled) {
                    const nextStep = parseInt(currentStep) + 1;
                    window.location.href = window.pageData.onboarding_navigation.onboarding_onboarding_step_url_3 + nextStep;
                }
            });
        }
    }

    if (finishSetupBtn) {
        finishSetupBtn.addEventListener('click', function() {
            import('/static/js/notifications.js').then(({ showPopup, POPUP_TYPES }) => {
                showPopup({
                    type: POPUP_TYPES.CONFIRM,
                    title: 'Complete Setup',
                    message: `
                        <p>Your cli_debrid instance is now configured and ready to use!</p>
                        <p><strong>Here's what happens next:</strong></p>
                        <ul style="list-style-type: none; padding-left: 0; margin: 15px 0;">
                            <li style="margin-bottom: 8px;">• The start button and the top right controls program operations</li>
                            <li style="margin-bottom: 8px;">• Consider setting up webhooks for <a href="https://github.com/godver3/cli_debrid/wiki/Webhooks#overseerr" target="_blank" style="color: #3498db;">Seerr</a> and <a href="https://github.com/godver3/cli_debrid/wiki/Webhooks#zurg" target="_blank" style="color: #3498db;">Zurg</a> (if using Symlinks)</li>
                            <li style="margin-bottom: 8px;">• Check the <a href="https://github.com/godver3/cli_debrid" target="_blank" style="color: #3498db;">GitHub page</a> or the <a href="https://godver3.github.io/cli_docs/" target="_blank" style="color: #3498db;">Docs</a> for more information</li>
                            <li style="margin-bottom: 8px;">• Look through the settings menu - there are lots of options to customize cli_debrid</li>
                            <li style="margin-bottom: 8px;">• Set up your Versions to customize the content you scrape</li>
                            <li style="margin-bottom: 8px;">• If you have any questions - feel free to join the <a href="https://discord.gg/ynqnXGJ4hU" target="_blank" style="color: #3498db;">Discord server</a> and ask away!</li>
                        </ul>
                        <div class="advice-section">
                            <div class="advice-header">
                                💡 Want my advice?
                                <i class="fas fa-chevron-down toggle-icon"></i>
                            </div>
                            <div class="advice-content">
                                I'd recommend that you don't try to replicate entire streaming libraries. Collecting is fun, but don't spam service providers. Above all - make sure you watch your content! It's more fun - I swear.
                            </div>
                        </div>
                        <p>Would you like to proceed to the dashboard?</p>
                    `,
                    confirmText: 'Go to Dashboard',
                    cancelText: 'Stay Here',
                    onConfirm: () => {
                        // Complete onboarding and redirect to dashboard
                        fetch(window.pageData.onboarding_navigation.onboarding_complete_onboarding_url, {
                            method: 'POST',
                        })
                        .then(response => response.json())
                        .then(result => {
                            if (result.success) {
                                window.location.href = window.pageData.onboarding_navigation.root_root_url;
                            } else {
                                showPopup({
                                    type: POPUP_TYPES.ERROR,
                                    title: 'Error',
                                    message: 'Failed to complete setup: ' + result.error
                                });
                            }
                        });
                    }
                });

                // Add click handler for advice section toggle
                setTimeout(() => {
                    const adviceHeader = document.querySelector('.popup-content .advice-header');
                    if (adviceHeader) {
                        adviceHeader.addEventListener('click', () => {
                            const section = adviceHeader.closest('.advice-section');
                            section.classList.toggle('expanded');
                        });
                    }
                }, 100);
            });
        });
    }
});
