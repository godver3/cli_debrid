// Page-wide modals and handlers for base.html (moved out of the template so browsers
// cache it). Loaded as a plain script at the same place in <head>.

document.addEventListener('DOMContentLoaded', function() {
    // Toast Manager is now loaded from external file

    // Notifications Modal Logic
    const notificationsModal = document.getElementById('notificationsModal');
    const notificationsBtn = document.getElementById('notifications_button');
    const notificationsCloseBtn = notificationsModal.querySelector('.close');
    const notificationsContainer = document.getElementById('notifications-container');
    let notifications = [];
    let lastNotificationCount = 0;
    let shownToastIds = new Set(); // Track which notifications have been shown as toasts
    let isFirstLoad = true; // Track if this is the first load
    let notificationEventSource = null; // SSE connection

    // Notification functions are handled in base.js to avoid conflicts

    // Modal controls for non-notification modals
    window.addEventListener('click', function(event) {
        // Close support modal if clicking outside
        if (event.target == supportModal) {
            console.log('Closing support modal via outside click');
            supportModal.style.display = 'none';
            document.body.classList.remove('modal-open'); // Ensure removed for support modal too
        }
    });

    // Notification toggle logic is handled in base.js to avoid conflicts

    // Support Modal Logic
    const supportModal = document.getElementById('supportModal');
    const supportModalClose = document.getElementById('supportModalClose');

    // Check phalanx status from API
    async function checkPhalanxStatus() {
        try {
            const response = await fetch('/settings/api/phalanx-disclaimer-status');
            const data = await response.json();
            return data.hasSeenDisclaimer;
        } catch (error) {
            console.error('Error checking phalanx status:', error);
            return false;
        }
    }

    // Check support modal status from API
    async function checkSupportModalStatus() {
        try {
            const response = await fetch('/settings/api/support-modal-status');
            const data = await response.json();
            return {
                hasSeenSupport: data.hasSeenSupport,
                pageViews: data.pageViews,
                neverShowAgain: data.neverShowAgain
            };
        } catch (error) {
            console.error('Error checking support modal status:', error);
            return {
                hasSeenSupport: false,
                pageViews: 0,
                neverShowAgain: false
            };
        }
    }

    // Increment page views via API
    async function incrementPageViews() {
        try {
            const response = await fetch('/settings/api/support-modal-pageview', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({})
            });
            const data = await response.json();
            if (!response.ok || !data.success) return null;
            return {
                pageViews: data.pageViews,
                hasSeenSupport: data.hasSeenSupport,
                neverShowAgain: data.neverShowAgain
            };
        } catch (error) {
            console.error('Error incrementing page views:', error);
            return null;
        }
    }

    // Mark support modal as seen via API
    async function markSupportModalSeen() {
        try {
            await fetch('/settings/api/support-modal-seen', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({})
            });
        } catch (error) {
            console.error('Error marking support modal as seen:', error);
        }
    }

    // Mark support modal as never show again via API
    async function markSupportModalNeverShow() {
        try {
            await fetch('/settings/api/support-modal-never-show', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({})
            });
        } catch (error) {
            console.error('Error marking support modal as never show:', error);
        }
    }

    // Show support modal if conditions are met
    async function handleSupportModal() {
        console.log('=== Support Modal Check Started ===');

        // Check if we're in a limited environment - if so, don't show support modal
        const isLimitedEnvironment = document.body.getAttribute('data-limited-environment') === 'true';
        if (isLimitedEnvironment) {
            console.log('❌ SUPPORT MODAL WILL NOT SHOW - Limited environment detected');
            console.log('=== Support Modal Check Completed ===');
            return;
        }

        // One request: the page-view POST returns the same fields as the
        // status GET (and doesn't count when "never show again" is set).
        // Non-admins can't POST, so they fall back to reading the status.
        const counted = await incrementPageViews();
        let { hasSeenSupport, pageViews, neverShowAgain } = counted || await checkSupportModalStatus();

        console.log('Final support modal conditions:', {
            hasSeenSupport,
            pageViews,
            neverShowAgain
        });

        // Calculate random chance (100% for testing, can be adjusted later)
        const baseChance = 0.01;
        const pageViewBonus = pageViews * 0.0005; // 0.05% increase per page view
        const totalChance = Math.min(baseChance + pageViewBonus, 1.0); // Cap at 100%

        console.log('Chance calculation:', {
            baseChance,
            pageViews,
            pageViewBonus,
            totalChance
        });

        // Generate random number and check if we should show the modal
        const randomValue = Math.random();
        const shouldShow = randomValue <= totalChance;

        console.log('Support modal random check:', {
            randomValue,
            totalChance,
            shouldShow
        });

        // Show support modal if conditions are met
        if (!neverShowAgain && shouldShow) {
            console.log('✅ SUPPORT MODAL WILL SHOW - Conditions met!');
            console.log('Showing support modal in 2 seconds...');
            setTimeout(() => {
                console.log('🎉 Attempting to show support modal now');
                supportModal.style.display = 'block';
                document.body.classList.add('modal-open'); // Add class for support modal
                markSupportModalSeen();
            }, 2000);
        } else {
            console.log('❌ SUPPORT MODAL WILL NOT SHOW');
            if (neverShowAgain) {
                console.log('Reason: neverShowAgain is true');
            } else if (!shouldShow) {
                console.log('Reason: Random chance check failed');
            }
        }

        console.log('=== Support Modal Check Completed ===');
    }

    // Initialize support modal handling
    handleSupportModal();

    // Support Modal Close Handlers
    if (supportModalClose) {
        supportModalClose.addEventListener('click', function() {
            console.log('Closing support modal via close button');
            supportModal.style.display = 'none';
            document.body.classList.remove('modal-open'); // Remove class
        });
    } else {
        console.warn('Support modal close button not found');
    }

    // Never Show Again Button Handler
    const neverShowAgainBtn = document.getElementById('neverShowAgainBtn');
    if (neverShowAgainBtn) {
        neverShowAgainBtn.addEventListener('click', async function() {
            console.log('Never show again button clicked');
            await markSupportModalNeverShow();
            supportModal.style.display = 'none';
            document.body.classList.remove('modal-open');
        });
    } else {
        console.warn('Never show again button not found');
    }

    // MDBList Popup Logic
    async function checkMDBListPopupStatus() {
        try {
            const response = await fetch('/settings/api/mdblist-popup-status');
            const data = await response.json();
            return data.shouldShow;
        } catch (error) {
            console.error('Error checking MDBList popup status:', error);
            return false;
        }
    }

    async function markMDBListPopupSeen() {
        try {
            await fetch('/settings/api/mdblist-popup-seen', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({})
            });
        } catch (error) {
            console.error('Error marking MDBList popup as seen:', error);
        }
    }

    async function addDefaultMDBLists() {
        try {
            const response = await fetch('/settings/api/add-default-mdblists', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({})
            });
            const data = await response.json();
            return data.success;
        } catch (error) {
            console.error('Error adding default MDBLists:', error);
            return false;
        }
    }

    async function handleMDBListPopup() {
        // Don't show popup on login page
        if (window.location.pathname === '/login' || window.location.pathname.includes('/auth/')) {
            return;
        }

        // The server only ever says yes in a limited environment, and <body> already knows.
        if (document.body.getAttribute('data-limited-environment') !== 'true') {
            return;
        }
        const shouldShow = await checkMDBListPopupStatus();

        if (shouldShow) {
            console.log('Showing MDBList popup in 2 seconds...');
            setTimeout(async () => {
                console.log('Attempting to show MDBList popup now');

                // Import showPopup from notifications.js
                const { showPopup, POPUP_TYPES } = await import('/static/js/notifications.js');

                // Check if user has dismissed the popup via localStorage
                const mdblistPopupDismissed = localStorage.getItem('mdblistPopupDismissed') === 'true';

                if (!mdblistPopupDismissed) {
                    showPopup({
                        type: POPUP_TYPES.CONFIRM,
                        title: 'Add Default MDBLists',
                        message: 'Would you like to add some default MDBList sources to get started? These will add popular movies and shows to your wanted list.<br><br>You can always add or remove content sources later in Settings.',
                        confirmText: 'Yes, Add Default Lists',
                        cancelText: 'No, Skip',
                        hiddenCloseButton: true,
                        onConfirm: async () => {
                            const success = await addDefaultMDBLists();
                            if (success) {
                                showPopup({
                                    type: POPUP_TYPES.SUCCESS,
                                    title: 'MDBLists Added',
                                    message: 'Default MDBList sources have been added successfully. You can view and manage them in Settings > Content Sources.',
                                    autoClose: 5000
                                });
                            } else {
                                showPopup({
                                    type: POPUP_TYPES.ERROR,
                                    title: 'Error',
                                    message: 'Failed to add default MDBList sources. You can add them manually in Settings > Content Sources.',
                                    autoClose: 5000
                                });
                            }
                            markMDBListPopupSeen();
                        },
                        onCancel: () => {
                            markMDBListPopupSeen();
                        },
                        onHiddenClose: () => {
                            // Save to localStorage so it doesn't show again for this user
                            localStorage.setItem('mdblistPopupDismissed', 'true');
                        }
                    });
                }
            }, 2000);
        }
    }

    // Initialize MDBList popup handling
    handleMDBListPopup();

    // Add ESC key support - Also remove modal-open class
    document.addEventListener('keydown', function(event) {
        if (event.key === 'Escape') {
             if (supportModal.style.display === 'block') {
                console.log('Closing support modal via ESC key');
                supportModal.style.display = 'none';
                document.body.classList.remove('modal-open');
             }
             if (notificationsModal.style.display === 'block') {
                console.log('Closing notifications modal via ESC key');
                notificationsModal.style.display = 'none';
                document.body.classList.remove('modal-open');
             }
             // Add for help modal if needed (assuming it's handled elsewhere)
             const helpModal = document.getElementById('help-modal-box');
             if (helpModal && helpModal.style.display === 'flex') { // Or however help modal is shown
                 console.log('Closing help modal via ESC key');
                 // Add code to hide help modal and remove 'modal-open'
                 // helpModal.style.display = 'none'; // Example
                 // document.getElementById('help-overlay').style.display = 'none'; // Example
                 // document.body.classList.remove('modal-open'); // Example
             }
        }
    });

    // ---- Add Mark All Read Button Logic ----
    if (markAllAsReadBtn) {
        markAllAsReadBtn.addEventListener('click', async () => {
            try {
                await fetch('/base/api/notifications/mark-all-read', { method: 'POST' });
                // Immediately refresh notifications to get updated read status
                await fetchNotifications(); 
            } catch (error) {
                console.error('Error marking all notifications as read:', error);
            }
        });
    }
    // ---- End Mark All Read Button Logic ----


    // --- "Hacked" Easter Egg ---
    const headerElement = document.querySelector('header');
    let headerClickCount = 0;
    let hackInProgress = false;

    if (headerElement) {
        headerElement.addEventListener('click', (e) => {
            // Only trigger if the click is on the header's background, not on interactive elements.
            if (e.target.closest('a, button, input, .hamburger-menu, .group-title')) {
                return;
            }

            if (hackInProgress) return;

            headerClickCount++;

            if (headerClickCount >= 15) {
                headerClickCount = 0; // Reset
                triggerHackEffect();
            }
        });
    }

    function sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    function nextFrame() {
        return new Promise(resolve => requestAnimationFrame(resolve));
    }

    async function triggerHackEffect() {
        const overlay = document.getElementById('hack-overlay');
        const textElement = document.getElementById('hack-text');
        const body = document.body;

        if (!overlay || !textElement || hackInProgress) return;

        hackInProgress = true;
        body.classList.add('modal-open'); // Disable scrolling
        overlay.style.display = 'flex';
        overlay.classList.add('screen-shake'); // Apply shake to the overlay

        const messages = [
            { text: "ACCESSING MAINFRAME...", delay: 1200, intensity: '1px' },
            { text: "BYPASSING FIREWALL...", delay: 1200, intensity: '2px' },
            { text: "COMPILING MALWARE...", delay: 1500, intensity: '2px' },
            { text: "!!! ACCESS GRANTED !!!", delay: 800, intensity: '2px' },
            { text: "YOU JUST GOT HACKED!!????", delay: 3000, intensity: '1px' },
            { text: "....kidding.", delay: 3500, intensity: '0px' }
        ];

        for (const message of messages) {
            overlay.style.setProperty('--shake-intensity', message.intensity);
            textElement.textContent = message.text;
            await nextFrame(); // Wait for the browser to render the change
            await sleep(message.delay);
        }

        // End of sequence
        overlay.style.display = 'none';
        overlay.classList.remove('screen-shake'); // Remove shake from the overlay
        body.classList.remove('modal-open'); // Re-enable scrolling
        hackInProgress = false; // Allow it to be triggered again
    }
    // --- End "Hacked" Easter Egg ---

    /* --- Version 666 Blood Drip Randomizer --- */
    (function() {
        const evilVersionEl = document.querySelector('.version-info.version-evil');
        if (!evilVersionEl) return;

        function randomizeDripDelays() {
            // Generate random delays between 4s and 10s
            const delay1 = (Math.random() * 6 + 4).toFixed(2) + 's';
            const delay2 = (Math.random() * 6 + 4).toFixed(2) + 's';
            // Each full drip cycle now lasts 4-10 seconds to ensure a pause between drips
            const duration = (Math.random() * 6 + 4).toFixed(2) + 's'; // 4-10 s cycle
            evilVersionEl.style.setProperty('--drip-delay1', delay1);
            evilVersionEl.style.setProperty('--drip-delay2', delay2);
            evilVersionEl.style.setProperty('--drip-duration', duration);
        }

        // Initial randomization
        randomizeDripDelays();

        // Re-randomize approximately every 12 seconds
        setInterval(randomizeDripDelays, 12000);
    })();
    /* --- End Version 666 Blood Drip Randomizer --- */

    /* --- Disable Evil Version Button Logic --- */
    (function() {
        const btn = document.getElementById('disableEvilButton');
        const versionEl = document.getElementById('versionInfo');
        if (!btn || !versionEl) return;

        // If previously disabled, immediately remove class & hide button
        if (localStorage.getItem('disableEvilVersion') === 'true') {
            versionEl.classList.remove('version-evil');
            btn.style.display = 'none';
            return;
        }

        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            localStorage.setItem('disableEvilVersion', 'true');
            versionEl.classList.remove('version-evil');
            btn.style.display = 'none';
        });
    })();
    /* --- End Disable Evil Version Button Logic --- */

    /* --- Support Icons Color Fade Effect --- */
    (function() {
        const supportIcons = document.querySelectorAll('.support-icon-link');
        if (supportIcons.length === 0) return;

        const brandColors = ['fade-github', 'fade-patreon', 'fade-kofi', 'fade-discord'];

        function triggerColorFade() {
            // Randomly select one or two icons to fade
            const iconsToFade = Math.random() < 0.5 ? 1 : 2;
            const shuffledIcons = Array.from(supportIcons).sort(() => Math.random() - 0.5);

            for (let i = 0; i < Math.min(iconsToFade, supportIcons.length); i++) {
                const icon = shuffledIcons[i];
                // Randomly select a brand color
                const randomColor = brandColors[Math.floor(Math.random() * brandColors.length)];

                // Add the fade class
                icon.classList.add(randomColor);

                // Remove fade class after animation completes (1.5 seconds total)
                setTimeout(() => {
                    icon.classList.remove(randomColor);
                }, 1500);
            }
        }

        // Trigger color fade every 10-20 seconds randomly
        function scheduleNextColorFade() {
            const delay = Math.random() * 10000 + 10000; // 10-20 seconds
            setTimeout(() => {
                triggerColorFade();
                scheduleNextColorFade();
            }, delay);
        }

        // Start the color fade cycle after a short initial delay
        setTimeout(scheduleNextColorFade, 4000);
    })();
    /* --- End Support Icons Color Fade Effect --- */
});
