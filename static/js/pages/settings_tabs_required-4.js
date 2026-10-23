/* Moved from templates/settings_tabs/required.html. */
import { showPopup, POPUP_TYPES } from '/static/js/notifications.js';

document.addEventListener('DOMContentLoaded', function() {
    const collectionTypeSelect = document.querySelector('select[name="File Management.file_collection_management"]');
    // Read values from data attributes, parsing the JSON string representation
    const isWindows = collectionTypeSelect ? JSON.parse(collectionTypeSelect.dataset.isWindows || 'false') : false;
    const allowWindowsSymlinks = collectionTypeSelect ? JSON.parse(collectionTypeSelect.dataset.allowWindowsSymlinks || 'false') : false;

    if (collectionTypeSelect) {
        updateUIVisibility(collectionTypeSelect.value);

        collectionTypeSelect.addEventListener('change', async function(event) {
            const previousValue = this.getAttribute('data-previous-value');
            const newValue = this.value;

            // If the value didn't actually change (e.g., clicked disabled option or re-selected same), do nothing
            if (newValue === previousValue) {
                return;
            }

            // Prevent immediate change & temporarily revert for confirmation dialog
            event.preventDefault();
            event.stopPropagation();
            this.value = previousValue; // Revert visually for confirmation

            // Confirmation logic
            let confirmMessage = 'Changing the collection management type may affect your existing library structure. Are you sure you want to proceed?';
            if (previousValue === 'Symlinked/Local' && newValue === 'Plex') {
                confirmMessage = 'Changing from Symlinked/Local to Plex management is an advanced process that may affect your existing library structure. Are you sure you want to proceed?';
            } else if (previousValue === 'Plex' && newValue === 'Symlinked/Local') {
                confirmMessage = 'Changing from Plex to Symlinked/Local management is an advanced process that may affect your existing library structure. Are you sure you want to proceed?';
            } // (Add other conditions if needed)

            showPopup({
                type: POPUP_TYPES.CONFIRM,
                title: '⚠️ Change Collection Management Type',
                message: confirmMessage,
                confirmText: 'Yes, Change',
                cancelText: 'No, Keep Current',
                onConfirm: () => {
                    // Apply the change
                    this.value = newValue;
                    this.setAttribute('data-previous-value', newValue); // Update previous value reference

                    // Trigger settings change event & UI update
                    const settingsEvent = new CustomEvent('settings-changed', {
                        detail: {
                            section: 'File Management',
                            key: 'file_collection_management',
                            value: newValue
                        }
                    });
                    document.dispatchEvent(settingsEvent);
                    updateUIVisibility(newValue);
                },
                onCancel: () => {
                    // Value is already visually reverted, do nothing further
                }
            });
        });

        function updateUIVisibility(value) {
            const plexSection = document.getElementById('plex-settings-in-file-management');
            const jellyfinSection = document.getElementById('jellyfin-settings-in-file-management');
            const mediaServerSelection = document.getElementById('media-server-selection');
            const symlinkFields = [
                document.querySelector('div.settings-form-group:has(#file\\ management-original_files_path)'),
                document.querySelector('div.settings-form-group:has(#file\\ management-symlinked_files_path)')
            ];
            const plexSpecificFields = document.querySelectorAll('.symlink-plex-setting');

            if (value === 'Plex') {
                if (plexSection) plexSection.style.display = 'block';
                if (jellyfinSection) jellyfinSection.style.display = 'none';
                if (mediaServerSelection) mediaServerSelection.style.display = 'none';
                symlinkFields.forEach(field => { if (field) field.style.display = 'none'; });
                plexSpecificFields.forEach(field => { if (field) field.style.display = 'none'; });
            } else if (value === 'Symlinked/Local') {
                if (plexSection) plexSection.style.display = 'none';
                if (jellyfinSection) jellyfinSection.style.display = 'none';
                if (mediaServerSelection) mediaServerSelection.style.display = 'block';
                symlinkFields.forEach(field => { if (field) field.style.display = 'block'; });
                // Don't show Plex-specific fields here, let updateMediaServerSettings handle them
                plexSpecificFields.forEach(field => { if (field) field.style.display = 'none'; });
                // Show the appropriate media server settings based on current selection
                // Use setTimeout to ensure the media server type is properly initialized
                setTimeout(() => {
                    updateMediaServerSettings();
                }, 0);
            } else {
                if (plexSection) plexSection.style.display = 'none';
                if (jellyfinSection) jellyfinSection.style.display = 'none';
                if (mediaServerSelection) mediaServerSelection.style.display = 'none';
                symlinkFields.forEach(field => { if (field) field.style.display = 'none'; });
                plexSpecificFields.forEach(field => { if (field) field.style.display = 'none'; });
            }
        }


    }

    // Plex-related variables
    const plexAuthBtn = document.getElementById('plex-auth-btn');
    const modal = document.getElementById('plex-modal');
    const closeBtn = document.querySelector('.plex-modal-close');
    const nextBtn = document.getElementById('plex-modal-next');
    const backBtn = document.getElementById('plex-modal-back');
    const step1 = document.getElementById('plex-modal-step-1');
    const step2 = document.getElementById('plex-modal-step-2');

    let plexToken = null;
    let selectedServer = null;
    let currentStep = 1;

    // Modal control functions
    function resetModal() {
        currentStep = 1;
        step1.classList.add('active');
        step2.classList.remove('active');
        backBtn.style.display = 'none';
        nextBtn.textContent = 'Next';
        selectedServer = null;

        // Hide step 2 button if not using Plex management
        const managementType = document.querySelector('select[name="File Management.file_collection_management"]').value;
        if (managementType !== 'Plex') {
            nextBtn.textContent = 'Finish';
        }
    }

    if (closeBtn) {
        closeBtn.onclick = function() {
            modal.style.display = 'none';
            resetModal();
        }
    }

    /*
    // Commenting out the window.onclick listener to prevent closing the modal by clicking the backdrop
    window.onclick = function(event) {
        if (event.target == modal) {
            modal.style.display = 'none';
            resetModal();
        }
    }
    */

    if (nextBtn) {
        nextBtn.onclick = function() {
            const managementType = document.querySelector('select[name="File Management.file_collection_management"]').value;

            if (currentStep === 1) {
                if (!selectedServer) {
                    showPopup({
                        type: POPUP_TYPES.WARNING,
                        title: 'Warning',
                        message: 'Please select a server first.',
                        autoClose: 3000
                    });
                    return;
                }

                // If not using Plex management, skip library selection
                if (managementType !== 'Plex') {
                    const connection = selectedServer.connections.find(c => c.local) || selectedServer.connections[0];

                    // Update only the Plex connection details
                    if (managementType === 'Symlinked/Local') {
                        document.querySelector('input[name="File Management.plex_url_for_symlink"]').value = connection.uri;
                        document.querySelector('input[name="File Management.plex_token_for_symlink"]').value = plexToken;
                    }

                    // Close the modal
                    modal.style.display = 'none';
                    resetModal();

                    showPopup({
                        type: POPUP_TYPES.SUCCESS,
                        title: 'Success',
                        message: 'Plex configuration completed successfully!',
                        autoClose: 3000
                    });
                    return;
                }

                // Continue with library selection for Plex management
                step1.classList.remove('active');
                step2.classList.add('active');
                backBtn.style.display = 'block';
                nextBtn.textContent = 'Finish';
                currentStep = 2;
            } else {
                // Save the selected libraries and update the main form
                const modalMovieLibraries = document.getElementById('modal-movie-libraries');
                const modalShowsLibraries = document.getElementById('modal-shows-libraries');
                const connection = selectedServer.connections.find(c => c.local) || selectedServer.connections[0];

                // Update form fields for Plex management
                document.querySelector('input[name="Plex.url"]').value = connection.uri;
                document.querySelector('input[name="Plex.token"]').value = plexToken;

                // Update library fields
                const movieLibrariesInput = document.querySelector('input[name="Plex.movie_libraries"]');
                const showsLibrariesInput = document.querySelector('input[name="Plex.shows_libraries"]');

                if (movieLibrariesInput && showsLibrariesInput) {
                    const selectedMovies = Array.from(modalMovieLibraries.selectedOptions).map(opt => opt.value);
                    const selectedShows = Array.from(modalShowsLibraries.selectedOptions).map(opt => opt.value);

                    movieLibrariesInput.value = selectedMovies.join(',');
                    showsLibrariesInput.value = selectedShows.join(',');
                }

                // Close the modal
                modal.style.display = 'none';
                resetModal();

                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    title: 'Success',
                    message: 'Plex configuration completed successfully!',
                    autoClose: 3000
                });
            }
        }
    }

    // Server selection function
    async function selectServer(server, event) {
        // Remove selection from other cards
        document.querySelectorAll('.plex-server-card').forEach(card => {
            card.classList.remove('selected');
        });

        // Select this card
        if (event && event.currentTarget) {
            event.currentTarget.classList.add('selected');
        }
        selectedServer = server;

        // Prefer local connection if available
        const connection = server.connections.find(c => c.local) || server.connections[0];

        try {
            const librariesResponse = await fetch(`/onboarding/plex/libraries?url=${encodeURIComponent(connection.uri)}&token=${plexToken}`);
            const librariesData = await librariesResponse.json();

            if (librariesData.success) {
                // Clear existing options
                const modalMovieLibraries = document.getElementById('modal-movie-libraries');
                const modalShowsLibraries = document.getElementById('modal-shows-libraries');
                modalMovieLibraries.innerHTML = '';
                modalShowsLibraries.innerHTML = '';

                // Add libraries to appropriate selects
                librariesData.libraries.forEach(library => {
                    const option = document.createElement('option');
                    option.value = library.title;
                    option.textContent = library.title;

                    if (library.type === 'movie') {
                        modalMovieLibraries.appendChild(option);
                    } else if (library.type === 'show') {
                        modalShowsLibraries.appendChild(option);
                    }
                });

                // Enable the Next button
                nextBtn.disabled = false;
            } else {
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error',
                    message: librariesData.error || 'Failed to fetch libraries'
                });
            }
        } catch (error) {
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error',
                message: 'Failed to fetch libraries: ' + error.message
            });
        }
    }

    // Plex authentication
    if (plexAuthBtn) {
        plexAuthBtn.addEventListener('click', async function() {
            try {
                // Add loading state to button
                plexAuthBtn.disabled = true;
                plexAuthBtn.innerHTML = `
                    <img src="https://www.plex.tv/wp-content/themes/plex/assets/img/plex-logo.svg" alt="Plex Logo">
                    <span>Connecting to Plex...</span>
                `;

                // First create a pin
                const pinResponse = await fetch('/onboarding/plex/auth/pin', {
                    method: 'POST'
                });
                const pinData = await pinResponse.json();

                if (!pinData.success) {
                    throw new Error(pinData.error || 'Failed to create pin');
                }

                console.log('Pin created successfully:', pinData);

                const width = 500;
                const height = 600;
                const left = window.screenX + (window.outerWidth - width) / 2;
                const top = window.screenY + (window.outerHeight - height) / 2;

                // Start polling for pin status
                let pollInterval;
                const startPolling = async () => {
                    pollInterval = setInterval(async () => {
                        try {
                            const pollResponse = await fetch('/onboarding/plex/auth/pin/check', {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json'
                                },
                                body: JSON.stringify({
                                    pin_id: pinData.pin.id
                                })
                            });
                            const pollData = await pollResponse.json();
                            console.log('Poll response:', pollData);

                            if (pollData.success && pollData.token) {
                                clearInterval(pollInterval);
                                console.log('Authentication successful, proceeding with token');

                                // Use the token to verify and proceed
                                const verifyResponse = await fetch('/onboarding/plex/verify_token', {
                                    method: 'POST',
                                    headers: {
                                        'Content-Type': 'application/json'
                                    },
                                    body: JSON.stringify({
                                        token: pollData.token
                                    })
                                });
                                const verifyData = await verifyResponse.json();
                                console.log('Verify response:', verifyData);

                                if (verifyData.success && verifyData.valid) {
                                    plexToken = pollData.token;
                                    showPopup({
                                        type: POPUP_TYPES.SUCCESS,
                                        title: 'Success',
                                        message: `Successfully connected as ${verifyData.username}`,
                                        autoClose: 3000
                                    });

                                    console.log('Fetching Plex servers...');
                                    // Continue with server discovery
                                    const response = await fetch(`/onboarding/plex/discover?token=${plexToken}`);
                                    const data = await response.json();
                                    console.log('Server discovery response:', data);

                                    if (data.success) {
                                        // First prepare the server list
                                        const plexServerslist = document.getElementById('plex-modal-step-1').querySelector('#plex-servers-list');
                                        plexServerslist.innerHTML = '';

                                        if (data.servers && data.servers.length > 0) {
                                            data.servers.forEach(server => {
                                                console.log('Adding server:', server.name);
                                                const serverCard = document.createElement('div');
                                                serverCard.className = 'plex-server-card';
                                                serverCard.innerHTML = `
                                                    <div class="plex-server-name">${server.name}</div>
                                                    <div class="plex-connection-type">
                                                        ${server.owned ? 'Owned Server' : 'Shared Server'}
                                                        ${server.connections.some(c => c.local) ? ' (Local Access Available)' : ''}
                                                    </div>
                                                `;

                                                serverCard.addEventListener('click', (event) => selectServer(server, event));
                                                plexServerslist.appendChild(serverCard);
                                            });
                                        } else {
                                            console.log('No servers found');
                                            plexServerslist.innerHTML = '<div class="no-servers">No Plex servers found. Make sure you have access to at least one Plex server.</div>';
                                        }

                                        // Show the modal after everything is ready
                                        modal.style.display = 'block';
                                    } else {
                                        throw new Error(data.error || 'Failed to connect to Plex');
                                    }
                                } else {
                                    throw new Error(verifyData.error || 'Invalid Plex token');
                                }
                            } else if (pollData.error && pollData.error !== "waiting_for_auth") {
                                clearInterval(pollInterval);
                                throw new Error(pollData.error);
                            }
                        } catch (error) {
                            clearInterval(pollInterval);
                            console.error('Error during polling:', error);
                            showPopup({
                                type: POPUP_TYPES.ERROR,
                                title: 'Error',
                                message: error.message
                            });
                        }
                    }, 2000); // Poll every 2 seconds
                };

                // Open the Plex auth window
                const authWindow = window.open(
                    pinData.pin.authUrl,
                    'PlexAuth',
                    `width=${width},height=${height},left=${left},top=${top}`
                );

                if (!authWindow) {
                    throw new Error('Popup blocked! Please allow popups for this site and try again.');
                }

                // Start polling
                startPolling();

                // Clean up interval if the window is closed
                const checkWindow = setInterval(() => {
                    if (authWindow.closed) {
                        clearInterval(checkWindow);
                        clearInterval(pollInterval);
                    }
                }, 1000);

            } catch (error) {
                console.error('Error:', error);
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error',
                    message: 'Failed to start Plex authentication: ' + error.message
                });
            } finally {
                // Reset button state
                plexAuthBtn.disabled = false;
                plexAuthBtn.innerHTML = `
                    <img src="https://www.plex.tv/wp-content/themes/plex/assets/img/plex-logo.svg" alt="Plex Logo">
                    <span>Sign in to Plex</span>
                `;
            }
        });
    }

            // Handle file management type changes
        const fileManagementSelect = document.querySelector('select[name="File Management.file_collection_management"]');
        if (fileManagementSelect) {
            fileManagementSelect.addEventListener('change', function() {
                const managementType = this.value;
                const plexSection = document.getElementById('plex-settings-in-file-management');
                const jellyfinSection = document.getElementById('jellyfin-settings-in-file-management');
                const mediaServerSelection = document.getElementById('media-server-selection');
                const symlinkFields = document.querySelectorAll('.symlink-plex-setting');

                if (managementType === 'Plex') {
                    if (plexSection) plexSection.style.display = 'block';
                    if (jellyfinSection) jellyfinSection.style.display = 'none';
                    if (mediaServerSelection) mediaServerSelection.style.display = 'none';
                    symlinkFields.forEach(field => {
                        if (field) field.style.display = 'none';
                    });
                } else if (managementType === 'Symlinked/Local') {
                    if (plexSection) plexSection.style.display = 'none';
                    if (jellyfinSection) jellyfinSection.style.display = 'none';
                    if (mediaServerSelection) mediaServerSelection.style.display = 'block';
                    symlinkFields.forEach(field => {
                        if (field) field.style.display = 'block';
                    });
                    // Show the appropriate media server settings based on current selection
                    updateMediaServerSettings();
                } else {
                    if (plexSection) plexSection.style.display = 'none';
                    if (jellyfinSection) jellyfinSection.style.display = 'none';
                    if (mediaServerSelection) mediaServerSelection.style.display = 'none';
                    symlinkFields.forEach(field => {
                        if (field) field.style.display = 'none';
                    });
                }
            });

            // Trigger initial state
            fileManagementSelect.dispatchEvent(new Event('change'));
        }

        // Handle media server type changes
        const mediaServerTypeSelect = document.getElementById('media-server-type');
        if (mediaServerTypeSelect) {
            console.log('Found media server type select:', mediaServerTypeSelect);
            mediaServerTypeSelect.addEventListener('change', function() {
                console.log('Media server type changed to:', this.value);
                updateMediaServerSettings();
            });

            // Set initial state for media server settings
            const fileManagementType = document.querySelector('select[name="File Management.file_collection_management"]').value;
            console.log('Initial file management type:', fileManagementType);
            if (fileManagementType === 'Symlinked/Local') {
                console.log('Setting initial media server settings');
                updateMediaServerSettings();
            }
        } else {
            console.log('Media server type select not found');
        }
});
