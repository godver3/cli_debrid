/* Moved from templates/settings_tabs/additional.html. */
    // Persist just the Trakt client_id/client_secret fields before starting
    // authorization. "Authorize Trakt" used to read whatever was last saved
    // to disk, ignoring unsaved edits still sitting in these two inputs —
    // clicking Authorize right after typing new credentials (without first
    // clicking the page's separate "Save Settings" button) failed with
    // "Trakt client ID or secret not set" even though the fields were filled in.
    function saveTraktCredentials() {
        const clientIdInput = document.getElementById('trakt-client_id');
        const clientSecretInput = document.getElementById('trakt-client_secret');
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

function initializeSymlinkFolderOrder() {
    const orderListElement = document.getElementById('symlink-folder-order-list');
    const orderInput = document.getElementById('symlink_folder_order_input');
    // Assuming the display paragraph is the next sibling of the input field
    const savedOrderDisplayP = orderInput.nextElementSibling;

    if (!orderListElement || !orderInput) {
        console.warn('Symlink folder order elements not found.');
        return;
    }

    const availableComponents = [
        { id: 'type', displayName: 'Content Type' },
        { id: 'version', displayName: 'Version' },
        { id: 'resolution', displayName: 'Resolution' }
    ];

    let draggingElement = null;

    function renderOrderList() {
        const currentOrderString = orderInput.value || 'type,version,resolution';
        let currentOrderIds = currentOrderString.split(',').map(s => s.trim()).filter(s => s);

        // Ensure all available components are present in the order, adding missing ones to the end.
        const finalOrderedComponents = [];
        const presentIds = new Set();

        // Add components based on the currentOrderIds if they are valid
        currentOrderIds.forEach(id => {
            const component = availableComponents.find(c => c.id === id);
            if (component) {
                finalOrderedComponents.push(component);
                presentIds.add(id);
            }
        });

        // Add any missing available components to the end
        availableComponents.forEach(component => {
            if (!presentIds.has(component.id)) {
                finalOrderedComponents.push(component);
            }
        });

        // Update the input field's value if it changed (e.g. due to adding missing components)
        const newOrderStringForInput = finalOrderedComponents.map(c => c.id).join(',');
        if (orderInput.value !== newOrderStringForInput) {
            orderInput.value = newOrderStringForInput;
            // No need to save here, as this is initial setup/correction of stored value
        }
        if (savedOrderDisplayP) {
             savedOrderDisplayP.textContent = `Current order (saved): ${newOrderStringForInput}`;
        }


        orderListElement.innerHTML = ''; // Clear existing items
        finalOrderedComponents.forEach(component => {
            const li = document.createElement('li');
            // Using 'content-source-item' for styling, can be changed to a specific class like 'symlink-order-item'
            li.className = 'symlink-order-item'; 
            li.setAttribute('data-id', component.id);
            li.textContent = component.displayName;
            li.draggable = true;
            orderListElement.appendChild(li);
        });

        addDragAndDropListeners();
    }

    function addDragAndDropListeners() {
        const items = orderListElement.querySelectorAll('.symlink-order-item');
        items.forEach(item => {
            item.addEventListener('dragstart', function(e) {
                draggingElement = this;
                this.classList.add('dragging');
            });

            item.addEventListener('dragend', function() {
                if (draggingElement) {
                    draggingElement.classList.remove('dragging');
                }
                draggingElement = null;
                updateOrderAndSave();
            });

            item.addEventListener('dragover', function(e) {
                e.preventDefault();
                if (!draggingElement || draggingElement === this) return;

                const rect = this.getBoundingClientRect();
                const midpoint = rect.top + rect.height / 2;

                if (e.clientY < midpoint) {
                    orderListElement.insertBefore(draggingElement, this);
                } else {
                    orderListElement.insertBefore(draggingElement, this.nextSibling);
                }
            });

            item.addEventListener('drop', function(e) {
                e.preventDefault(); // Prevent default to allow drop
                // Reordering is handled by dragover and finalization by dragend
            });
        });
    }

    function updateOrderAndSave() {
        const newOrderIds = Array.from(orderListElement.children)
                              .map(item => item.getAttribute('data-id'));
        const newOrderString = newOrderIds.join(',');

        orderInput.value = newOrderString;

        if (savedOrderDisplayP) {
            savedOrderDisplayP.textContent = `Current order (saved): ${newOrderString}`;
        }

        // Call the global saveSetting function
        if (typeof saveSetting === 'function') {
            saveSetting(orderInput.dataset.section, orderInput.dataset.key, newOrderString);
        } else {
            console.warn('Global saveSetting function not found. Order will be saved on main form submission.');
        }
    }

    // Initial population of the list
    renderOrderList();
}

function initializeSymlinkPathExamples() {
    const exampleElements = {
        movie: document.getElementById('example-movie-path'),
        animeMovie: document.getElementById('example-anime-movie-path'),
        tvEpisode: document.getElementById('example-tv-episode-path'),
        animeTvEpisode: document.getElementById('example-anime-tv-episode-path'),
        documentaryMovie: document.getElementById('example-documentary-movie-path'),
        documentaryTvEpisode: document.getElementById('example-documentary-tv-episode-path')
    };

    // Helper to get checkbox value
    const getCheckboxValue = (name) => {
        const el = document.querySelector(`input[name="${name}"]`);
        return el ? el.checked : false;
    };

    // Helper to get input value
    const getInputValue = (name, defaultValue = '') => {
        const el = document.querySelector(`input[name="${name}"], select[name="${name}"]`);
        if (el) {
            const value = el.value;
            if (value !== undefined && value !== null && value.trim() !== '') {
                return value;
            } else {
                return defaultValue;
            }
        }
        return defaultValue;
    };

    function getCurrentSymlinkSettings() {
        return {
            // File Management
            folderOrder: getInputValue('File Management.symlink_folder_order', 'type,version,resolution'),
            organizeByType: getCheckboxValue('File Management.symlink_organize_by_type'),
            organizeByType: true,
            organizeByResolution: getCheckboxValue('File Management.symlink_organize_by_resolution'),
            organizeByVersion: getCheckboxValue('File Management.symlink_organize_by_version'),
            baseSymlinkedPath: getInputValue('File Management.symlinked_files_path', '/mnt/symlinked'), // For completeness, though we'll show relative

            // Debug (Templates and Folder Names)
            movieTemplate: getInputValue('Debug.symlink_movie_template', '{title} ({year})/{title} ({year}) - {imdb_id} - {version} - ({original_filename})'),
            episodeTemplate: getInputValue('Debug.symlink_episode_template', '{title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {imdb_id} - {version} - ({original_filename})'),
            enableSeparateAnimeFolders: getCheckboxValue('Debug.enable_separate_anime_folders'),
            enableSeparateDocumentaryFolders: getCheckboxValue('Debug.enable_separate_documentary_folders'),
            moviesFolderName: getInputValue('Debug.movies_folder_name', 'Movies'),
            tvShowsFolderName: getInputValue('Debug.tv_shows_folder_name', 'TV Shows'),
            animeMoviesFolderName: getInputValue('Debug.anime_movies_folder_name', 'Anime Movies'),
            animeTvShowsFolderName: getInputValue('Debug.anime_tv_shows_folder_name', 'Anime TV Shows'),
            documentaryMoviesFolderName: getInputValue('Debug.documentary_movies_folder_name', 'Documentary Movies'),
            documentaryTvShowsFolderName: getInputValue('Debug.documentary_tv_shows_folder_name', 'Documentary TV Shows')
        };
    }

    const mockItems = {
        movie: {
            title: 'Awesome Movie', year: '2023', type: 'movie', imdb_id: 'tt1234567', version: 'Remux', resolution: '2160p', 
            genres: 'Action,Adventure', original_filename: 'Awesome.Movie.2023.Remux.mkv', filled_by_file: 'dummy.mkv'
        },
        animeMovie: {
            title: 'Epic Anime Film', year: '2024', type: 'movie', imdb_id: 'tt7654321', version: 'BluRay', resolution: '1080p',
            genres: 'Animation,Anime,Fantasy', original_filename: 'Epic.Anime.Film.2024.BluRay.mkv', filled_by_file: 'dummy.mkv'
        },
        tvEpisode: {
            title: 'Great TV Show', year: '2022', type: 'episode', imdb_id: 'tt9876543', version: 'WEB-DL', resolution: '1080p',
            season_number: 1, episode_number: 2, episode_title: 'The Second Chapter', season_year: '2022',
            genres: 'Drama,Sci-Fi', original_filename: 'Great.TV.Show.S01E02.WEB-DL.mkv', filled_by_file: 'dummy.mkv'
        },
        animeTvEpisode: {
            title: 'Cool Anime Series', year: '2021', type: 'episode', imdb_id: 'tt1231231', version: 'HDTV', resolution: '720p',
            season_number: 2, episode_number: 10, episode_title: 'A New Foe Appears', season_year: '2022',
            genres: 'Animation,Anime,Action', original_filename: 'Cool.Anime.Series.S02E10.HDTV.mkv', filled_by_file: 'dummy.mkv'
        },
        documentaryMovie: {
            title: 'Amazing Earth', year: '2023', type: 'movie', imdb_id: 'tt2345678', version: 'WEB-DL', resolution: '1080p',
            genres: 'Documentary', original_filename: 'Amazing.Earth.2023.WEB-DL.mkv', filled_by_file: 'dummy.mkv'
        },
        documentaryTvEpisode: {
            title: 'Planet Life', year: '2022', type: 'episode', imdb_id: 'tt8765432', version: 'BluRay', resolution: '2160p',
            season_number: 1, episode_number: 3, episode_title: 'The Deep Oceans', season_year: '2022',
            genres: 'Documentary', original_filename: 'Planet.Life.S01E03.BluRay.mkv', filled_by_file: 'dummy.mkv'
        }
    };

    function jsSanitizeFilename(filename) {
        if (!filename) return '';
        // Basic sanitization similar to Python's re.sub(r'[<>|?*:"\'\&/\\]', '_', filename)
        // and NFKD normalization. JS lacks direct NFKD, so this is a simplification.
        // For display purposes, a more basic replacement should suffice.
        filename = filename.normalize('NFD').replace(/[\u0300-\u036f]/g, ""); // Remove diacritics
        return filename.replace(/[<>|?*:"'&/\\]/g, '_').trim();
    }

    function formatNumber(num, digits) {
        return String(num).padStart(digits, '0');
    }

    function generateExamplePath(item, settings) {
        const extension = item.original_filename.includes('.') ? '.' + item.original_filename.split('.').pop() : '.mkv';
        let orderedPrefixParts = [];
        const mediaType = item.type; // 'movie' or 'episode'

        const isAnime = item.genres && item.genres.toLowerCase().includes('anime');
        const isDocumentary = item.genres && item.genres.toLowerCase().includes('documentary');

        const folderOrderList = settings.folderOrder.split(',').map(s => s.trim().toLowerCase()).filter(s => s);

        for (const component of folderOrderList) {
            if (component === "version" && settings.organizeByVersion) {
                const versionStr = (item.version || '').replace(/\*/g, ''); // Strip asterisks
                if (versionStr) {
                    const sanitizedVersionFolder = jsSanitizeFilename(versionStr);
                    if (sanitizedVersionFolder) orderedPrefixParts.push(sanitizedVersionFolder);
                }
            } else if (component === "resolution" && settings.organizeByResolution) {
                // Simplified: Use item.resolution directly if available.
                // Python logic uses version_settings['max_resolution'].
                if (item.resolution) {
                     // Assuming item.resolution is already like "1080p", "2160p"
                    const sanitizedResFolder = jsSanitizeFilename(item.resolution);
                    if (sanitizedResFolder) orderedPrefixParts.push(sanitizedResFolder);
                }
            } else if (component === "type" && settings.organizeByType) {
                let folderNameForType = "";
                if (isAnime && settings.enableSeparateAnimeFolders) {
                    folderNameForType = mediaType === 'movie' ? settings.animeMoviesFolderName : settings.animeTvShowsFolderName;
                } else if (isDocumentary && settings.enableSeparateDocumentaryFolders) {
                    folderNameForType = mediaType === 'movie' ? settings.documentaryMoviesFolderName : settings.documentaryTvShowsFolderName;
                } else {
                    folderNameForType = mediaType === 'movie' ? settings.moviesFolderName : settings.tvShowsFolderName;
                }
                folderNameForType = (folderNameForType || '').trim();
                if (folderNameForType) orderedPrefixParts.push(folderNameForType); // Assume settings values are already clean
            }
        }

        let pathParts = [...orderedPrefixParts];
        let template = mediaType === 'movie' ? settings.movieTemplate : settings.episodeTemplate;

        // Template variable preparation
        const templateVars = {
            title: item.title || 'Unknown Title',
            year: item.year || '',
            imdb_id: item.imdb_id || '',
            tmdb_id: item.tmdb_id || '', // Add if needed in template
            version: (item.version || '').replace(/\*/g, ''),
            quality: item.quality || '', // Add if needed
            original_filename: item.original_filename.substring(0, item.original_filename.lastIndexOf('.')) || item.original_filename,
            resolution: item.resolution || '',
            season_number: item.season_number !== undefined ? formatNumber(item.season_number, 2) : '',
            episode_number: item.episode_number !== undefined ? formatNumber(item.episode_number, 2) : '',
            episode_title: item.episode_title || '',
            season_year: item.season_year || item.year || ''
        };

        // Process template string: first format season/episode numbers if present in placeholders
        template = template.replace(/\{season_number:(\d+)d\}/g, (match, p1) => formatNumber(item.season_number || 0, parseInt(p1)));
        template = template.replace(/\{episode_number:(\d+)d\}/g, (match, p1) => formatNumber(item.episode_number || 0, parseInt(p1)));

        // Generic placeholder replacement
        let finalPathSegment = template.replace(/\{([^}]+)\}/g, (match, key) => templateVars[key.trim()] !== undefined ? templateVars[key.trim()] : match);

        const templatePathSegments = finalPathSegment.split('/');
        let finalFilename = "";

        templatePathSegments.forEach((segment, index) => {
            const sanitizedSegment = jsSanitizeFilename(segment);
            if (index === templatePathSegments.length - 1) { // Filename part
                finalFilename = sanitizedSegment.endsWith(extension) ? sanitizedSegment : sanitizedSegment + extension;
            } else { // Directory part
                if (sanitizedSegment) pathParts.push(sanitizedSegment);
            }
        });

        // We show path relative to the main symlinked_files_path
        return pathParts.join('/') + (pathParts.length > 0 ? '/' : '') + finalFilename;
    }

    window.displaySymlinkPathExamples = function() { // Make it global for easy calling
        const settings = getCurrentSymlinkSettings();
        console.log(settings)
        if (exampleElements.movie) exampleElements.movie.textContent = generateExamplePath(mockItems.movie, settings);
        if (exampleElements.animeMovie) exampleElements.animeMovie.textContent = generateExamplePath(mockItems.animeMovie, settings);
        if (exampleElements.tvEpisode) exampleElements.tvEpisode.textContent = generateExamplePath(mockItems.tvEpisode, settings);
        if (exampleElements.animeTvEpisode) exampleElements.animeTvEpisode.textContent = generateExamplePath(mockItems.animeTvEpisode, settings);
        if (exampleElements.documentaryMovie) exampleElements.documentaryMovie.textContent = generateExamplePath(mockItems.documentaryMovie, settings);
        if (exampleElements.documentaryTvEpisode) exampleElements.documentaryTvEpisode.textContent = generateExamplePath(mockItems.documentaryTvEpisode, settings);
    }

    // --- Event Listeners ---
    const settingInputsToWatch = [
        'File Management.symlink_folder_order', 'File Management.symlink_organize_by_type',
        'File Management.symlink_organize_by_resolution', 'File Management.symlink_organize_by_version',
        'Debug.symlink_movie_template', 'Debug.symlink_episode_template',
        'Debug.enable_separate_anime_folders', 'Debug.movies_folder_name',
        'Debug.tv_shows_folder_name', 'Debug.anime_movies_folder_name', 'Debug.anime_tv_shows_folder_name',
        'Debug.enable_separate_documentary_folders', 'Debug.documentary_movies_folder_name', 'Debug.documentary_tv_shows_folder_name'
    ];

    settingInputsToWatch.forEach(name => {
        const inputElement = document.querySelector(`input[name="${name}"], select[name="${name}"]`);
        if (inputElement) {
            inputElement.addEventListener('input', displaySymlinkPathExamples); // 'input' for text fields, 'change' for checkboxes/selects
            if (inputElement.type === 'checkbox' || inputElement.tagName === 'SELECT') {
                 inputElement.addEventListener('change', displaySymlinkPathExamples);
            }
        }
    });

    // Initial display
    displaySymlinkPathExamples();
}

document.addEventListener('DOMContentLoaded', function() {  
    initializeTraktAuthorization();
    initializeSymlinkFolderOrder();
    initializeSymlinkPathExamples();

    function initializeImdbIdWarningLogic() {
        const movieTemplateInput = document.getElementById('additional-symlink_movie_template');
        const movieWarningDiv = document.getElementById('symlink_movie_template_imdb_warning');
        const episodeTemplateInput = document.getElementById('additional-symlink_episode_template');
        const episodeWarningDiv = document.getElementById('symlink_episode_template_imdb_warning');
        const fileManagementSelect = document.querySelector('select[name="File Management.file_collection_management"]');

        function validateSingleTemplate(templateInput, warningDiv, fcmType) {
            if (!templateInput || !warningDiv) return;
            warningDiv.textContent = ''; // Clear previous warning
            warningDiv.style.display = 'none';

            if (fcmType === 'Symlinked/Local') {
                if (templateInput.value.trim() !== '' && !templateInput.value.includes('{imdb_id}')) {
                    warningDiv.textContent = 'Warning: {imdb_id} is required in the template for symlink recovery.';
                    warningDiv.style.display = 'block';
                }
            }
        }

        function checkAllSymlinkTemplates() {
            // Use current value from select if available, otherwise fallback to value at page load (passed by Jinja)
            // This handles cases where the select element might not be immediately found or if tabs are isolated.
            const fcmTypeFromJson = window.pageData.settings_tabs_additional.File_Management_file_collection_manageme;
            const fcmType = fileManagementSelect ? fileManagementSelect.value : fcmTypeFromJson;

            validateSingleTemplate(movieTemplateInput, movieWarningDiv, fcmType);
            validateSingleTemplate(episodeTemplateInput, episodeWarningDiv, fcmType);
        }

        if (movieTemplateInput && episodeTemplateInput && movieWarningDiv && episodeWarningDiv) {
            movieTemplateInput.addEventListener('input', checkAllSymlinkTemplates);
            episodeTemplateInput.addEventListener('input', checkAllSymlinkTemplates);

            if (fileManagementSelect) {
                fileManagementSelect.addEventListener('change', checkAllSymlinkTemplates);
            } else {
                // This might happen if required.html's content is not yet in DOM when this script part runs,
                // or if they are in truly separate DOMs (e.g. iframes, though not typical for simple tabs).
                console.warn('File Management select ("File Management.file_collection_management") not found by additional.html for IMDB ID warning listener. Warnings will be based on page load settings for File Collection Management or until an input event on templates triggers a check.');
            }

            // Initial check
            checkAllSymlinkTemplates();
        } else {
            console.warn('Symlink template input fields or warning divs not found. IMDB ID warning logic will not run.');
        }
    }

    initializeImdbIdWarningLogic(); // Initialize the new logic

    // Initialize logo selection
    const logoRadioButtons = document.querySelectorAll('.logo-option input[type="radio"]');
    logoRadioButtons.forEach(radio => {
        radio.addEventListener('change', function() {
            const section = this.dataset.section;
            const key = this.dataset.key;
            const value = this.value;

            // Save the setting when radio button changes
            saveSetting(section, key, value);
        });
    });

    // Function to save a setting to the server
    function saveSetting(section, key, value) {
        const formData = new FormData();
        formData.append('section', section);
        formData.append('key', key);
        formData.append('value', value);

        fetch('/settings/save_single_setting', {
            method: 'POST',
            body: formData
        })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'success') {
                // Show a small notification that setting was saved
                const notification = document.createElement('div');
                notification.className = 'setting-saved-notification';
                notification.textContent = 'Setting saved!';
                document.body.appendChild(notification);

                // Remove the notification after a short delay
                setTimeout(() => {
                    notification.style.opacity = '0';
                    setTimeout(() => {
                        notification.remove();
                    }, 500);
                }, 2000);

                // If it's the logo setting, reload the page to show the new logo
                if (section === 'UI Settings' && key === 'program_logo') {
                    setTimeout(() => {
                        window.location.reload();
                    }, 1000);
                }
            }
        })
        .catch(error => {
            console.error('Error saving setting:', error);
        });
    }

    // Clear Library Cache Button Handler
    const clearCacheBtn = document.getElementById('clear-library-cache-btn');
    if (clearCacheBtn) {
        clearCacheBtn.addEventListener('click', function() {
            showPopup({
                type: 'confirm',
                title: 'Clear Artwork Cache',
                message: 'Are you sure you want to clear the artwork cache? All posters and backdrops will be re-downloaded from the primary source on next library load.',
                confirmText: 'Clear Cache',
                cancelText: 'Cancel',
                onConfirm: function() {
                    fetch('/library/clear_cache', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        }
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            showPopup({ type: 'success', title: 'Success', message: 'Cache cleared successfully! Artwork will be refreshed on next library load.', autoClose: 4000 });
                        } else {
                            showPopup({ type: 'error', title: 'Error', message: 'Failed to clear cache: ' + (data.error || 'Unknown error'), autoClose: 5000 });
                        }
                    })
                    .catch(error => {
                        console.error('Error clearing cache:', error);
                        showPopup({ type: 'error', title: 'Error', message: 'Error clearing cache. Check console for details.', autoClose: 5000 });
                    });
                }
            });
        });
    }
});
