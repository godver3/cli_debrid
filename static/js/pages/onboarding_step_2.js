/* Moved from templates/onboarding_step_2.html. */
import { showPopup, POPUP_TYPES } from '/static/js/notifications.js';

document.addEventListener('DOMContentLoaded', function() {
    // Show/hide Plex or Jellyfin fields based on media server selection
    function toggleSymlinkMediaServerFields(value) {
        const plexFields = document.getElementById('symlink-plex-fields');
        const jellyfinFields = document.getElementById('symlink-jellyfin-fields');
        if (plexFields) plexFields.style.display = value === 'plex' ? 'block' : 'none';
        if (jellyfinFields) jellyfinFields.style.display = value === 'jellyfin' ? 'block' : 'none';
    }

    const symlinkMediaServer = document.getElementById('symlink_media_server');
    if (symlinkMediaServer) {
        toggleSymlinkMediaServerFields(symlinkMediaServer.value);
        symlinkMediaServer.addEventListener('change', (e) => {
            toggleSymlinkMediaServerFields(e.target.value);
            document.querySelector('.plex-auth-section').style.display = e.target.value === 'plex' ? 'block' : 'none';
        });
    }
    const settingsForm = document.getElementById('settings-form');
    const nextStepBtn = document.querySelector('.next-step-btn');
    const fileManagementSelect = document.getElementById('file_collection_management');
    const plexFields = document.getElementById('plex-fields');
    const localFields = document.getElementById('local-fields');
    const plexDescription = document.getElementById('plex-description');
    const localDescription = document.getElementById('local-description');
    const overrideCheckbox = document.getElementById('windows_symlink_override');
    const symlinkWarningDiv = document.getElementById('windows-symlink-warning');
    const originalFilesPathLabel = document.querySelector('label[for="original_files_path"]');

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

    // Read values from the global config object
    const isWindows = window.ONBOARDING_CONFIG.isWindows;
    let allowWindowsSymlinks = window.ONBOARDING_CONFIG.allowWindowsSymlinks;

    // Change next step button text to "Save & Validate" and set next step URL
    if (nextStepBtn) {
        nextStepBtn.textContent = 'Save & Validate';
        nextStepBtn.classList.add('validate-btn');
        nextStepBtn.classList.add('onboarding-btn');
        nextStepBtn.disabled = false;
        nextStepBtn.setAttribute('data-next-step-url', window.pageData.onboarding_step_2.onboarding_onboarding_step_url);
    }

    // Function to update required fields based on management type
    function updateRequiredFields(managementType) {
        // Plex fields
        const plexInputs = plexFields.querySelectorAll('input:not([type="checkbox"])');
        plexInputs.forEach(input => {
            // Skip optional fields
            if (input.id === 'update_plex_on_file_discovery' || input.id === 'original_files_path') {
                return;
            }
            input.required = managementType === 'Plex';
        });

        // Local fields
        const localInputs = localFields.querySelectorAll('input');
        localInputs.forEach(input => {
            // Skip optional fields
            if (input.id === 'plex_url_for_symlink' || input.id === 'plex_token_for_symlink' || input.id === 'original_files_path') {
                return;
            }
            input.required = managementType === 'Local';
        });
    }

    // Function to toggle visibility of fields based on management type
    function toggleFields(managementType) {
        if (managementType === 'Plex') {
            plexFields.classList.add('active');
            localFields.classList.remove('active');
            plexDescription.style.display = 'block';
            localDescription.style.display = 'none';
            if (originalFilesPathLabel) {
                originalFilesPathLabel.textContent = 'Mount Path (optional in Plex mode):';
            }
        } else {
            plexFields.classList.remove('active');
            localFields.classList.add('active');
            plexDescription.style.display = 'none';
            localDescription.style.display = 'block';
            if (originalFilesPathLabel) {
                originalFilesPathLabel.textContent = 'Mount Path:';
            }
        }
        // The Plex sign-in is only useful when Plex is the media server.
        const mediaServer = document.getElementById('symlink_media_server');
        const usesPlex = managementType === 'Plex' || (mediaServer && mediaServer.value === 'plex');
        document.querySelector('.plex-auth-section').style.display = usesPlex ? 'block' : 'none';
        updateRequiredFields(managementType);
    }

    // Function to update the Symlinked/Local option availability and warning visibility
    function updateSymlinkOptionState() {
        const symlinkOption = fileManagementSelect.querySelector('option[value="Symlinked/Local"]');
        if (!symlinkOption) return; // Safety check

        const currentlyAllowed = allowWindowsSymlinks || (overrideCheckbox && overrideCheckbox.checked);

        symlinkOption.disabled = isWindows && !currentlyAllowed;

        if (isWindows) {
             if (symlinkWarningDiv) {
                symlinkWarningDiv.style.display = currentlyAllowed ? 'none' : 'block';
             }
            // If disallowed and selected, force back to Plex
            if (!currentlyAllowed && fileManagementSelect.value === 'Symlinked/Local') {
                fileManagementSelect.value = 'Plex';
                toggleFields('Plex'); // Update conditional fields display
                 showPopup({
                     type: POPUP_TYPES.WARNING,
                     title: 'Windows Limitation',
                     message: 'Symlinked/Local management is disabled on Windows unless explicitly allowed. Resetting to Plex.',
                     autoClose: 4000
                 });
            }
        }
    }

    // Initialize fields based on current selection and override state
    if (fileManagementSelect) {
        // Set initial state of override checkbox based on backend setting
        if (overrideCheckbox) {
            overrideCheckbox.checked = allowWindowsSymlinks;
            // Add listener to the override checkbox
            overrideCheckbox.addEventListener('change', updateSymlinkOptionState);
        }

        // Initial update of option state and UI visibility
        updateSymlinkOptionState();
        toggleFields(fileManagementSelect.value);

        // Change listener for the main select dropdown
        fileManagementSelect.addEventListener('change', function() {
            // Update the UI fields based on selection
            toggleFields(this.value);
            // Reset validation button state whenever selection changes
            if (nextStepBtn) {
                nextStepBtn.textContent = 'Validate Settings';
                nextStepBtn.classList.add('validate-btn');
            }
            // Re-validate symlink option state (in case it was forced back to Plex)
            updateSymlinkOptionState();
        });
    }

    // Modal control functions
    function resetModal() {
        currentStep = 1;
        step1.classList.add('active');
        step2.classList.remove('active');
        backBtn.style.display = 'none';
        nextBtn.textContent = 'Next';
        selectedServer = null;

        // Hide step 2 button if not using Plex management
        const managementType = document.getElementById('file_collection_management').value;
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
            const managementType = document.getElementById('file_collection_management').value;

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
                        document.getElementById('plex_url_for_symlink').value = connection.uri;
                        document.getElementById('plex_token_for_symlink').value = plexToken;
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
                document.getElementById('plex_url').value = connection.uri;
                document.getElementById('plex_token').value = plexToken;

                // Update library fields
                const movieLibrariesInput = document.querySelector('input[name="movie_libraries"]');
                const showsLibrariesInput = document.querySelector('input[name="shows_libraries"]');

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
                            } else if (pollData.error && pollData.error !== 'waiting_for_auth') {
                                // Only treat errors other than 'waiting_for_auth' as terminal
                                clearInterval(pollInterval);
                                throw new Error(pollData.error);
                            }
                            // If pollData.error is 'waiting_for_auth', we simply continue polling
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

    // Initialize Trakt authorization
    initializeTraktAuthorization();

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    }

    // Checkboxes are left out of FormData when unticked, so send both explicitly.
    function collectFormData() {
        const formData = new FormData(settingsForm);
        formData.set('use_debrid', document.getElementById('use_debrid').checked ? 'on' : 'off');
        formData.set('use_usenet', document.getElementById('use_usenet').checked ? 'on' : 'off');
        return formData;
    }

    async function validateSettings() {
        const formData = collectFormData();
        const managementType = formData.get('file_collection_management');
        const settings = Object.fromEntries(formData.entries());

        try {
            const response = await fetch('/settings_validation/onboarding-settings', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    management_type: managementType,
                    settings: settings
                })
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const result = await response.json();
            renderValidationResults(result);
            return result.valid;
        } catch (error) {
            console.error('Error validating settings:', error);
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error',
                message: 'Failed to validate settings: ' + escapeHtml(error.message),
            });
            return false;
        }
    }

    function renderValidationResults(results) {
        const validationMessage = results.checks.map(check => `
            <div class="validation-check ${check.valid && !check.warning ? 'success' : (check.warning ? 'warning' : 'error')}">
                <strong>${escapeHtml(check.name)}</strong>
                <div class="validation-message">${escapeHtml(check.message)}</div>
            </div>
        `).join('');

        showPopup({
            type: results.valid ? POPUP_TYPES.SUCCESS : POPUP_TYPES.ERROR,
            title: results.valid ? 'Everything checks out' : 'Some settings need fixing',
            message: validationMessage,
            autoClose: false
        });

        if (nextStepBtn) {
            if (results.valid) {
                nextStepBtn.textContent = 'Next Step';
                nextStepBtn.classList.remove('validate-btn');
            } else {
                nextStepBtn.textContent = 'Save & Validate';
                nextStepBtn.classList.add('validate-btn');
            }
            nextStepBtn.disabled = false;
        }
    }

    // Save first, then validate, so "Next Step" never moves on with unsaved changes.
    async function saveAndValidate() {
        try {
            const response = await fetch(window.pageData.onboarding_step_2.onboarding_onboarding_step_url_2, {
                method: 'POST',
                body: collectFormData()
            });
            const data = await response.json();
            if (!data.success) {
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error',
                    message: 'Error saving settings: ' + escapeHtml(data.error),
                });
                return false;
            }
        } catch (error) {
            console.error('Error:', error);
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error',
                message: 'An unexpected error occurred while saving.',
            });
            return false;
        }
        return validateSettings();
    }

    // Any edit after a successful validation has to be saved and validated again.
    settingsForm.addEventListener('input', () => {
        if (nextStepBtn && !nextStepBtn.classList.contains('validate-btn')) {
            nextStepBtn.textContent = 'Save & Validate';
            nextStepBtn.classList.add('validate-btn');
        }
    });

    if (nextStepBtn) {
        nextStepBtn.addEventListener('click', async function(event) {
            event.preventDefault();
            event.stopPropagation();

            if (nextStepBtn.classList.contains('validate-btn')) {
                await saveAndValidate();
            } else {
                window.location.href = nextStepBtn.getAttribute('data-next-step-url');
            }
        }, true);  // Use capture phase to ensure this handler runs first
    }

    settingsForm.addEventListener('submit', async function(event) {
        event.preventDefault();
        await saveAndValidate();
    });

    // --- Provider choice ---------------------------------------------------------
    const useDebrid = document.getElementById('use_debrid');
    const useUsenet = document.getElementById('use_usenet');
    const usenetBackend = document.getElementById('usenet_backend');
    const USENET_HINTS = {
        climount: {
            url: 'cli_mount\'s web UI address, e.g. http://192.168.1.50:8282 (not Phalanx DB on :8888).',
            token: 'Only needed if you turned on authentication in cli_mount.',
            placeholder: 'http://192.168.1.50:8282',
        },
        nzbdav: {
            url: 'NzbDAV\'s address, e.g. http://192.168.1.50:3000.',
            token: 'The SABnzbd API key from NzbDAV → Settings → SABnzbd.',
            placeholder: 'http://192.168.1.50:3000',
        },
        zurg: {
            url: 'Zurg 2.0\'s address, e.g. http://192.168.1.50:9999. cli_debrid uses its SABnzbd-compatible API.',
            token: 'The SABnzbd API key from your Zurg config.',
            placeholder: 'http://192.168.1.50:9999',
        },
    };

    function updateProviderPanels() {
        document.getElementById('panel-debrid').style.display = useDebrid.checked ? 'block' : 'none';
        document.getElementById('panel-usenet').style.display = useUsenet.checked ? 'block' : 'none';
        const backend = usenetBackend.value;
        const hints = USENET_HINTS[backend] || USENET_HINTS.climount;
        document.getElementById('usenet-url-hint').textContent = hints.url;
        document.getElementById('usenet-token-hint').textContent = hints.token;
        document.getElementById('usenet-token-label').textContent =
            backend === 'climount' ? 'API Token (optional):' : 'SABnzbd API Key:';
        document.getElementById('usenet_url').placeholder = hints.placeholder;
        document.getElementById('nzbdav-helper-wrap').style.display = backend === 'nzbdav' ? 'block' : 'none';
    }

    useDebrid.addEventListener('change', updateProviderPanels);
    useUsenet.addEventListener('change', () => {
        updateProviderPanels();
        if (useUsenet.checked) {
            // Point the mount picker at the matching tool.
            mountTool.value = usenetBackend.value === 'nzbdav' ? 'nzbdav'
                : usenetBackend.value === 'zurg' ? 'zurg' : 'climount';
            updateMountHint();
        }
    });
    usenetBackend.addEventListener('change', () => {
        updateProviderPanels();
        mountTool.value = usenetBackend.value === 'nzbdav' ? 'nzbdav'
            : usenetBackend.value === 'zurg' ? 'zurg' : 'climount';
        updateMountHint();
    });

    // --- Mount -------------------------------------------------------------------
    const mountTool = document.getElementById('mount_tool');
    const mountPath = document.getElementById('original_files_path');
    const MOUNT_HINTS = {
        climount: ['The folder you mapped to /mnt in the cli_mount container, plus /__all__. Debrid and usenet files both show up there.', '/mnt/climount/__all__'],
        decypharr: ['Decypharr\'s mount folder for your debrid service, ending in /__all__.', '/mnt/decypharr/__all__'],
        zurg: ['Your rclone mount of Zurg, ending in /__all__.', '/mnt/zurg/__all__'],
        nzbdav: ['Your rclone mount of NzbDAV\'s WebDAV share.', '/mnt/nzbdav'],
        other: ['The folder where your debrid/usenet files appear inside cli_debrid\'s container.', ''],
    };

    function guessMountTool() {
        const path = (mountPath.value || '').toLowerCase();
        for (const tool of ['zurg', 'decypharr', 'nzbdav', 'climount']) {
            if (path.includes(tool)) return tool;
        }
        if (useUsenet.checked && usenetBackend.value !== 'climount') return usenetBackend.value;
        return path ? 'other' : 'climount';
    }

    function updateMountHint() {
        const [hint, example] = MOUNT_HINTS[mountTool.value] || MOUNT_HINTS.other;
        document.getElementById('mount-path-hint').textContent = hint + (example ? ` Example: ${example}` : '');
        mountPath.placeholder = example;
    }

    mountTool.value = guessMountTool();
    mountTool.addEventListener('change', updateMountHint);
    updateMountHint();
    updateProviderPanels();

    // --- Test buttons --------------------------------------------------------------
    const TEST_PAYLOADS = {
        usenet: () => ({
            backend: usenetBackend.value,
            url: document.getElementById('usenet_url').value,
            token: document.getElementById('usenet_api_token').value,
        }),
        mount: () => ({ path: mountPath.value }),
        tvdb: () => ({ api_key: document.getElementById('tvdb_api_key').value }),
        tmdb: () => ({ api_key: document.getElementById('tmdb_api_key').value }),
    };

    document.querySelectorAll('.ob-test-btn[data-test]').forEach(button => {
        button.addEventListener('click', async () => {
            const what = button.dataset.test;
            const result = document.querySelector(`.ob-test-result[data-result="${what}"]`);
            const label = button.textContent;
            button.disabled = true;
            button.textContent = 'Testing…';
            result.className = 'ob-test-result';
            result.textContent = '';
            try {
                const response = await fetch(`/onboarding/test/${what}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(TEST_PAYLOADS[what]()),
                });
                const data = await response.json();
                result.classList.add(data.success ? (data.warning ? 'ob-warn' : 'ob-ok') : 'ob-err');
                result.textContent = (data.success ? (data.warning ? '⚠ ' : '✓ ') : '✗ ') + data.message;
            } catch (error) {
                result.classList.add('ob-err');
                result.textContent = '✗ Test failed: ' + error.message;
            } finally {
                button.disabled = false;
                button.textContent = label;
            }
        });
    });
});

// Persist just the Trakt client_id/client_secret fields before starting
// authorization. "Authorize Trakt" used to read whatever was last saved to
// disk, ignoring unsaved edits still sitting in these two inputs — clicking
// Authorize right after typing new credentials failed with "Trakt client ID
// or secret not set" even though the fields were filled in.
function saveTraktCredentials() {
    const clientIdInput = document.getElementById('trakt_client_id');
    const clientSecretInput = document.getElementById('trakt_client_secret');
    const clientId = clientIdInput ? clientIdInput.value.trim() : '';
    const clientSecret = clientSecretInput ? clientSecretInput.value.trim() : '';
    if (!clientId || !clientSecret) {
        return Promise.reject(new Error('Enter both a Trakt Client ID and Client Secret first.'));
    }
    return fetch('/settings/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ Trakt: { client_id: clientId, client_secret: clientSecret } })
    })
        .then(response => response.json())
        .then(data => {
            if (data.status !== 'success') {
                throw new Error(data.message || 'Failed to save Trakt credentials');
            }
        });
}

function initializeTraktAuthorization() {
    const traktAuthBtn = document.getElementById('trakt-auth-btn');
    const traktAuthStatus = document.getElementById('trakt-auth-status');
    const traktAuthCode = document.getElementById('trakt-auth-code');
    const traktCode = document.getElementById('trakt-code');
    const traktActivateLink = document.getElementById('trakt-activate-link');

    if (traktAuthBtn) {
        traktAuthBtn.addEventListener('click', function() {
            traktAuthBtn.disabled = true;
            traktAuthStatus.textContent = 'Saving Trakt credentials...';
            traktAuthCode.style.display = 'none';

            saveTraktCredentials()
                .then(() => {
                    traktAuthStatus.textContent = 'Initializing Trakt authorization...';
                    return fetch('/trakt/trakt_auth', { method: 'POST' });
                })
                .then(response => response.json())
                .then(data => {
                    console.log('Trakt auth response:', data);
                    if (data.user_code) {
                        traktCode.textContent = data.user_code;
                        traktActivateLink.href = data.verification_url;
                        traktAuthCode.style.display = 'block';
                        traktAuthStatus.textContent = 'Please enter the code on the Trakt website to complete authorization.';
                        pollTraktAuthStatus(data.device_code);
                    } else {
                        traktAuthStatus.textContent = 'Error: ' + (data.error || 'Unable to get authorization code');
                    }
                })
                .catch(error => {
                    console.error('Trakt auth error:', error);
                    traktAuthStatus.textContent = 'Error: ' + (error.message || 'Unable to start authorization process');
                })
                .finally(() => {
                    traktAuthBtn.disabled = false;
                });
        });
    }

    // Check initial Trakt authorization status
    checkTraktAuthStatus();
}

function pollTraktAuthStatus(device_code) {
    const traktAuthStatus = document.getElementById('trakt-auth-status');
    const traktAuthCode = document.getElementById('trakt-auth-code');
    const pollInterval = setInterval(() => {
        fetch('/trakt/trakt_auth_status', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ device_code: device_code }),
        })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'authorized') {
                    clearInterval(pollInterval);
                    traktAuthStatus.textContent = 'Trakt authorization successful!';
                    traktAuthStatus.classList.add('authorized');
                    traktAuthCode.style.display = 'none';
                    setTimeout(() => {
                        traktAuthStatus.textContent = 'Trakt is currently authorized.';
                    }, 5000);
                } else if (data.status === 'error') {
                    clearInterval(pollInterval);
                    traktAuthStatus.textContent = 'Error: ' + (data.message || 'Unknown error occurred');
                }
            })
            .catch(error => {
                console.error('Error:', error);
                traktAuthStatus.textContent = 'Error checking authorization status. Please try again.';
            });
    }, 5000); // Check every 5 seconds
}

function checkTraktAuthStatus() {
    const traktAuthStatus = document.getElementById('trakt-auth-status');
    fetch('/trakt/trakt_auth_status', { method: 'GET' })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'authorized') {
                traktAuthStatus.textContent = 'Trakt is currently authorized.';
                traktAuthStatus.classList.add('authorized');
            } else {
                traktAuthStatus.textContent = 'Trakt is not authorized.';
                traktAuthStatus.classList.remove('authorized');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            traktAuthStatus.textContent = 'Unable to check Trakt authorization status.';
        });
}
