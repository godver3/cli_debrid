/* Moved from templates/onboarding_step_5.html. */
// Define pollTaskStatus function
function pollTaskStatus(taskId) {
    fetch(`/debug/api/task_status/${taskId}`)
        .then(response => response.json())
        .then(data => {
            console.log('Task status:', data);  // Debug logging

            if (data.status === 'PENDING' || data.status === 'RUNNING') {
                // Update loading message if provided
                if (data.message) {
                    Loading.show(data.message);
                }
                setTimeout(() => pollTaskStatus(taskId), 2000);  // Poll every 2 seconds
            } else if (data.status === 'SUCCESS') {
                Loading.hide();
                let message = 'Library scan completed successfully';
                if (data.result) {
                    // Handle if result is an object
                    if (typeof data.result === 'object') {
                        message = data.result.message || message;
                    } else {
                        message = data.result;
                    }
                }
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    message: message,
                    title: 'Success'
                });
            } else {
                Loading.hide();
                let errorMessage = 'Library scan failed';
                if (data.result) {
                    if (typeof data.result === 'object') {
                        errorMessage = data.result.error || data.result.message || errorMessage;
                    } else {
                        errorMessage = data.result;
                    }
                }
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: errorMessage,
                    title: 'Error'
                });
            }
        })
        .catch(error => {
            console.error('Error:', error);
            Loading.hide();
            showPopup({
                type: POPUP_TYPES.ERROR,
                message: 'An error occurred while checking task status',
                title: 'Error'
            });
        });
}

document.addEventListener('DOMContentLoaded', () => {
    setupOptionSelection();
    setupConversionHandlers();
    setupPlexLibrarySelection(); // Add call to new setup function

    // Add handler for ALL scan Plex library forms using their IDs
    const scanPlexLibraryForms = document.querySelectorAll('[id^="scan-plex-library-form"]');
    scanPlexLibraryForms.forEach(form => {
        setupScanHandler(form); // Pass the form element to the handler
    });
});

// Define the actual submit logic as a separate function
function handleScanSubmit(event, formElement) {
    event.preventDefault();

    // Get result element IDs from data attributes (moved inside)
    const resultsId = formElement.dataset.resultsId;
    const showsStatId = formElement.dataset.showsStatId;
    const moviesStatId = formElement.dataset.moviesStatId;
    const episodesStatId = formElement.dataset.episodesStatId;
    const scanTimestampId = formElement.dataset.scanTimestampId;

    showPopup({
        type: POPUP_TYPES.CONFIRM,
        message: 'This will scan your entire Plex library. This process may take some time depending on your library size. Do you want to continue?',
        title: 'Confirmation',
        onConfirm: () => {
            Loading.show('Initializing Plex library scan...', 'Starting scan process...');
            fetch('/debug/api/direct_plex_scan', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success && data.scan_id) {
                    // Set up SSE for progress updates
                    const eventSource = new EventSource(`/debug/api/plex_scan_progress/${data.scan_id}`);
                    console.log(`[Client SSE] EventSource connection initiated to /debug/api/plex_scan_progress/${data.scan_id}`);
                    let progressTimeout;
                    let taskCompletedSuccessfully = false; // Flag to track successful completion

                    // Set a timeout to close the connection if no updates for 30 seconds
                    const resetTimeout = () => {
                        if (progressTimeout) clearTimeout(progressTimeout);
                        progressTimeout = setTimeout(() => {
                            console.log('Progress updates timed out - treating as complete');
                            eventSource.close();
                            Loading.hide();

                            showPopup({
                                type: POPUP_TYPES.SUCCESS,
                                message: 'Library scan completed. If any items appear to be missing, you can try scanning again.',
                                title: 'Scan Complete'
                            });
                        }, 30000); // 30 second timeout
                    };

                    // Reset timeout on each message
                    eventSource.onmessage = function(event) {
                        console.log('[Client SSE] onmessage received raw data:', event.data);
                        resetTimeout(); // Reset timeout on each message
                        const progress = JSON.parse(event.data);
                        console.log('[Client SSE] Parsed progress:', progress);

                        // Update loading message with detailed progress
                        if (progress.message) {
                            let mainMessage = progress.message;
                            let details = [];

                            // Add phase-specific progress details
                            if (progress.phase === 'collection') {
                                if (progress.shows_processed !== undefined && progress.total_shows !== undefined) {
                                    details.push(`Shows: ${progress.shows_processed}/${progress.total_shows}`);
                                }
                                if (progress.movies_processed !== undefined && progress.total_movies !== undefined) {
                                    details.push(`Movies: ${progress.movies_processed}/${progress.total_movies}`);
                                }
                                if (progress.episodes_found !== undefined) {
                                    details.push(`Total Episodes Found: ${progress.episodes_found}`);
                                }
                            } else if (progress.phase === 'database' || progress.status === 'adding') {
                                details.push(`Adding items to database...`);
                                if (progress.total_items) {
                                    details.push(`Total items to process: ${progress.total_items}`);
                                }
                            }

                            Loading.show(mainMessage, details.join('\n'));
                        }

                        console.log('[Client SSE] Checking progress.complete...', progress.complete);
                        // Handle completion or error
                        if (progress.complete) {
                            console.log('[Client SSE] progress.complete is true. Setting flag.');
                            taskCompletedSuccessfully = true; // Set flag on success

                            // Use setTimeout to defer success handling slightly
                            setTimeout(() => {
                                console.log('[Client SSE] Executing deferred success handling (setTimeout callback).');
                                if (progressTimeout) {
                                    clearTimeout(progressTimeout);
                                }
                                // eventSource.close(); // <-- Keep this commented out
                                Loading.hide();

                                // Update and show scan results using IDs from data attributes
                                try {
                                    document.getElementById(showsStatId).textContent = `${progress.shows_processed}/${progress.total_shows}`;
                                    document.getElementById(moviesStatId).textContent = `${progress.movies_processed}/${progress.total_movies}`;
                                    document.getElementById(episodesStatId).textContent = progress.episodes_found;
                                    document.getElementById(scanTimestampId).textContent = new Date().toLocaleString();
                                    document.getElementById(resultsId).classList.add('active');

                                // Mark scan as complete (generic function)
                                markScanComplete();

                                // Enable save config button if we're in conversion mode
                                const selectedOption = document.querySelector('.setup-option.selected');
                                if (selectedOption && selectedOption.dataset.option === 'convert-to-symlink') {
                                    const saveConfigBtn = document.getElementById('save-config-btn');
                                    if (saveConfigBtn) {
                                        saveConfigBtn.disabled = false;
                                    }
                                }
                                } catch (error) {
                                    console.error('Error updating scan results:', error);
                                }

                                showPopup({
                                    type: POPUP_TYPES.SUCCESS,
                                    message: `${progress.message}\n\nStats:\n` +
                                            `Shows: ${progress.shows_processed}/${progress.total_shows}\n` +
                                            `Movies: ${progress.movies_processed}/${progress.total_movies}\n` +
                                            `Episodes Found: ${progress.episodes_found}`,
                                    title: 'Success'
                                });
                            }, 0); // Delay of 0 milliseconds (yields execution)

                        } else if (progress.status === 'adding') {
                            Loading.show(progress.message);
                        }
                    };

                    eventSource.onerror = function(error) {
                        console.log('[Client SSE] onerror triggered. Checking flag...', taskCompletedSuccessfully);
                        if (!taskCompletedSuccessfully) { // Only handle error if task didn't complete successfully
                            console.error('[Client SSE] EventSource error (task did not complete): ', error);
                            if (eventSource.readyState !== EventSource.CLOSED) { eventSource.close(); }
                        } else {
                            console.log('[Client SSE] EventSource error occurred after successful completion, ignoring.');
                            // Ensure connection is closed if browser didn't handle it
                            if (eventSource.readyState !== EventSource.CLOSED) {
                                console.log('[Client SSE] Closing eventSource from onerror (after success).');
                                eventSource.close();
                            }
                        }
                    };
                } else {
                    Loading.hide();
                    showPopup({
                        type: POPUP_TYPES.ERROR,
                        message: data.error || 'Failed to start library scan',
                        title: 'Error'
                    });
                }
            })
            .catch(error => {
                Loading.hide();
                console.error('Error:', error);
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'An error occurred while starting the library scan',
                    title: 'Error'
                });
            });
        }
    });
}

function setupScanHandler(formElement) {
    // Check if a handler is already attached and remove it
    if (formElement._scanSubmitHandler) {
        console.log(`[setupScanHandler] Removing existing listener for form ${formElement.id || 'no-id'}`);
        formElement.removeEventListener('submit', formElement._scanSubmitHandler);
    }

    // Create the new handler function for this specific form
    const newHandler = (event) => handleScanSubmit(event, formElement);

    // Store the new handler reference on the element
    formElement._scanSubmitHandler = newHandler;

    // Add the new listener
    console.log(`[setupScanHandler] Adding new listener for form ${formElement.id || 'no-id'}`);
    formElement.addEventListener('submit', newHandler);
}

function setupOptionSelection() {
    const options = document.querySelectorAll('.setup-option');
    const nextStepBtn = document.querySelector('.next-step-btn');
    const optionsContainer = document.querySelector('.setup-options-container');

    options.forEach(option => {
        option.addEventListener('click', () => {
            console.log('Option clicked:', option.dataset.option);

            // Remove selection from other options
            options.forEach(opt => opt.classList.remove('selected'));
            // Select clicked option
            option.classList.add('selected');

            const setupType = option.dataset.option;

            // Hide all info sections first
            document.querySelectorAll('.setup-info').forEach(info => {
                info.classList.remove('active');
            });

            // Show the relevant info section
            const infoSection = document.getElementById(`${setupType}-setup-info`);
            console.log('Looking for info section:', `${setupType}-setup-info`);

            if (infoSection) {
                console.log('Found info section');
                // First add condensed to hide other options
                optionsContainer.classList.add('condensed');
                // Then show the info section
                setTimeout(() => {
                    infoSection.classList.add('active');

                    // Simplified logic: Ensure scan handler is attached ONLY when the button is visible and ready
                    // Remove handler attachment from here for plex-symlink initially.
                    // It will be attached either if pre-configured OR after saving.

                    // Attach handler immediately if the scan section is visible from the start (pre-configured)
                    if (setupType === 'plex-symlink') {
                        const scanSection = document.getElementById('plex-scan-section-symlink');
                        const scanForm = scanSection?.querySelector('#scan-plex-library-form'); // Use scanSection context
                        if (scanSection && scanSection.style.display !== 'none' && scanForm && !scanForm.dataset.scanHandlerAttached) {
                            console.log('[setupOptionSelection] Attaching handler for pre-configured symlink scan');
                            setupScanHandler(scanForm);
                            scanForm.dataset.scanHandlerAttached = 'true';
                        }
                    } else if (setupType === 'plex-direct') {
                         // Attach handler for direct mount scan form if present
                         const scanFormDirect = infoSection.querySelector('#scan-plex-library-form-direct');
                         if (scanFormDirect && !scanFormDirect.dataset.scanHandlerAttached) {
                             console.log('[setupOptionSelection] Attaching handler for direct scan');
                             setupScanHandler(scanFormDirect);
                             scanFormDirect.dataset.scanHandlerAttached = 'true';
                         }
                    } else if (setupType === 'convert-to-symlink') {
                        // Attach handler for convert scan form if present
                        const scanFormConvert = infoSection.querySelector('#scan-plex-library-form-convert');
                        if (scanFormConvert && !scanFormConvert.dataset.scanHandlerAttached) {
                            console.log('[setupOptionSelection] Attaching handler for convert scan');
                            setupScanHandler(scanFormConvert);
                            scanFormConvert.dataset.scanHandlerAttached = 'true';
                        }
                    }

                }, 50);

                // Control next step button state based on setup type
                if (setupType === 'convert-to-symlink') {
                    // For conversion, disable until conversion is complete
                    window.conversionComplete = false;
                    if (nextStepBtn) nextStepBtn.disabled = true;
                } else if (setupType === 'plex-symlink' || setupType === 'plex-direct') { // Updated condition
                    // For existing symlink or direct library, disable until scan is complete
                    window.scanComplete = false;
                    if (nextStepBtn) nextStepBtn.disabled = true;
                } else {
                    // For all other options, enable immediately
                    if (nextStepBtn) nextStepBtn.disabled = false;
                    // Update can_proceed in the session
                    fetch('/onboarding/update_can_proceed', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({
                            step: '5',
                            can_proceed: true
                        })
                    });
                }
            } else {
                console.error('Info section not found for:', setupType);
            }
        });
    });
}

function setupConversionHandlers() {
    const saveConfigBtn = document.getElementById('save-config-btn');
    const convertLibraryBtn = document.getElementById('convert-library-btn');

    if (saveConfigBtn) {
        saveConfigBtn.addEventListener('click', function() {
            const config = {
                symlinked_files_path: document.getElementById('symlinked_files_path').value,
                movie_template: document.getElementById('movie_template').value,
                episode_template: document.getElementById('episode_template').value
            };

            Loading.show('Saving configuration...');
            fetch('/onboarding/settings/api/update', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    'File Management.symlinked_files_path': config.symlinked_files_path,
                    'Debug.symlink_movie_template': config.movie_template,
                    'Debug.symlink_episode_template': config.episode_template
                })
            })
            .then(response => response.json())
            .then(data => {
                Loading.hide();
                if (data.success) {
                    showPopup({
                        type: POPUP_TYPES.SUCCESS,
                        message: 'Configuration saved successfully',
                        title: 'Success'
                    });
                    convertLibraryBtn.disabled = false;
                } else {
                    showPopup({
                        type: POPUP_TYPES.ERROR,
                        message: data.error || 'Failed to save configuration',
                        title: 'Error'
                    });
                }
            })
            .catch(error => {
                Loading.hide();
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'An error occurred while saving configuration',
                    title: 'Error'
                });
            });
        });
    }

    if (convertLibraryBtn) {
        convertLibraryBtn.addEventListener('click', function() {
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: 'This will convert your entire library to use symlinks. This process may take some time. Do you want to continue?',
                title: 'Confirmation',
                onConfirm: () => {
                    Loading.show('Converting library to symlinks...', 'Starting conversion process...');
                    fetch('/debug/convert_to_symlinks', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        }
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (data.success && data.task_id) {
                            // Set up SSE for progress updates
                            const eventSource = new EventSource(`/debug/api/conversion_progress/${data.task_id}`);

                            eventSource.onmessage = function(event) {
                                const progress = JSON.parse(event.data);

                                // Update loading message with detailed progress
                                if (progress.message) {
                                    let mainMessage = progress.message;
                                    let details = [];

                                    // Add detailed progress if available
                                    if (progress.total_items !== undefined) {
                                        details.push(`Total items: ${progress.total_items}`);
                                        details.push(`Processed: ${progress.processed_items}/${progress.total_items}`);
                                        details.push(`Symlinks created: ${progress.symlinks_created}`);

                                        if (progress.items_to_wanted > 0) {
                                            details.push(`Items moved to Wanted: ${progress.items_to_wanted}`);
                                        }
                                        if (progress.items_deleted > 0) {
                                            details.push(`Duplicates removed: ${progress.items_deleted}`);
                                        }
                                        if (progress.items_skipped > 0) {
                                            details.push(`Items skipped: ${progress.items_skipped}`);
                                        }
                                    }

                                    Loading.show(mainMessage, details.join('\n'));
                                }

                                // Handle completion or error
                                if (progress.complete) {
                                    eventSource.close();
                                    Loading.hide();

                                    if (progress.status === 'complete') {
                                        let message = 'Library conversion completed successfully\n\n';
                                        message += `Stats:\n`;
                                        message += `Total items processed: ${progress.processed_items}\n`;
                                        message += `Symlinks created: ${progress.symlinks_created}\n`;

                                        if (progress.items_to_wanted > 0) {
                                            message += `Items moved to Wanted: ${progress.items_to_wanted}\n`;
                                        }
                                        if (progress.items_deleted > 0) {
                                            message += `Duplicates removed: ${progress.items_deleted}\n`;
                                        }
                                        if (progress.items_skipped > 0) {
                                            message += `Items skipped: ${progress.items_skipped}`;
                                        }

                                        showPopup({
                                            type: POPUP_TYPES.SUCCESS,
                                            message: message,
                                            title: 'Success'
                                        });

                                        // Show next steps directly in the UI
                                        const nextStepsHtml = `
                                            <div class="conversion-steps" style="margin-top: 20px;">
                                                <h5>Next Steps</h5>
                                                <ul class="info-list">
                                                    <li>Update your Plex library paths to point to the new symlinked locations in addition to the current location</li>
                                                    <li>Run a full library scan - this will take up to a few days to complete</li>
                                                    <li>Once scanned and verified working you can remove the old library paths from Plex</li>
                                                </ul>
                                            </div>
                                        `;

                                        // Add next steps after scan results
                                        const scanResults = document.getElementById('scan-results-convert');
                                        if (scanResults) {
                                            scanResults.insertAdjacentHTML('afterend', nextStepsHtml);
                                        }

                                        // Mark conversion as complete
                                        window.conversionComplete = true;

                                        // Update can_proceed in the session
                                        fetch('/onboarding/update_can_proceed', {
                                            method: 'POST',
                                            headers: {
                                                'Content-Type': 'application/json',
                                            },
                                            body: JSON.stringify({
                                                step: '5',
                                                can_proceed: true
                                            })
                                        });
                                    } else if (progress.status === 'error') {
                                        showPopup({
                                            type: POPUP_TYPES.ERROR,
                                            message: progress.message || 'An error occurred during conversion',
                                            title: 'Error'
                                        });
                                    }
                                }
                            };

                            eventSource.onerror = function() {
                                eventSource.close();
                                Loading.hide();
                                showPopup({
                                    type: POPUP_TYPES.ERROR,
                                    message: 'Lost connection to server while monitoring progress',
                                    title: 'Error'
                                });
                            };
                        } else {
                            Loading.hide();
                            showPopup({
                                type: POPUP_TYPES.ERROR,
                                message: data.error || 'Failed to start library conversion',
                                title: 'Error'
                            });
                        }
                    })
                    .catch(error => {
                        Loading.hide();
                        console.error('Error:', error);
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: 'An error occurred while converting library',
                            title: 'Error'
                        });
                    });
                }
            });
        });
    }
}

// Add these functions to handle completion states
function markScanComplete() {
    window.scanComplete = true;
    const nextStepBtn = document.querySelector('.next-step-btn');
    if (nextStepBtn) {
        nextStepBtn.disabled = false;
        // Update can_proceed in the session
        fetch('/onboarding/update_can_proceed', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                step: '5',
                can_proceed: true
            })
        });
    }
}

// --- UPDATED Plex Library Selection Functions ---
function setupPlexLibrarySelection() {
    const form = document.getElementById('plex-library-selection-form');
    if (form) {
        form.addEventListener('submit', handleSavePlexLibraries);
    }
    // Removed fetchPlexLibraries call
}

function handleSavePlexLibraries(event) {
    event.preventDefault();
    // Get input elements instead of select
    const movieInput = document.getElementById('plex-movie-libraries');
    const showInput = document.getElementById('plex-show-libraries');

    // Get trimmed values directly from input fields
    const movieLibrariesStr = movieInput.value.trim();
    const showLibrariesStr = showInput.value.trim();

    // Updated validation for non-empty strings
    if (!movieLibrariesStr || !showLibrariesStr) {
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: 'Please enter names for both movie and show libraries.',
            title: 'Input Required'
        });
        return;
    }

    // Settings object remains the same
    const settingsToUpdate = {
        'Plex.movie_libraries': movieLibrariesStr,
        'Plex.shows_libraries': showLibrariesStr
    };

    console.log('Saving Plex library settings:', settingsToUpdate);
    Loading.show('Saving Plex library configuration...');

    // Fetch call remains the same
    fetch('/onboarding/settings/api/update', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(settingsToUpdate)
    })
    .then(response => response.json())
    .then(data => {
        Loading.hide();
        if (data.success) {
            showPopup({
                type: POPUP_TYPES.SUCCESS,
                message: 'Plex libraries saved successfully.',
                title: 'Success'
            });
            // Hide the selection prompt and show the scan section
            const promptDiv = document.getElementById('plex-library-selection-prompt');
            const scanSection = document.getElementById('plex-scan-section-symlink'); // Target specific scan section
            if (promptDiv) promptDiv.style.display = 'none';
            if (scanSection) scanSection.style.display = 'block';

             // Update displayed libraries in the list using standard DOM methods
             const listItems = document.querySelectorAll('#plex-symlink-setup-info .info-list li');
             let movieLiFound = false;
             let showLiFound = false;

             listItems.forEach(li => {
                 const labelSpan = li.querySelector('span.var-label');
                 if (labelSpan) {
                     if (labelSpan.textContent.trim() === 'Movie Libraries:') {
                         const valueSpan = labelSpan.nextElementSibling;
                         if (valueSpan && valueSpan.classList.contains('var-value')) {
                             valueSpan.textContent = movieLibrariesStr;
                             movieLiFound = true;
                         }
                     } else if (labelSpan.textContent.trim() === 'Show Libraries:') {
                         const valueSpan = labelSpan.nextElementSibling;
                         if (valueSpan && valueSpan.classList.contains('var-value')) {
                             valueSpan.textContent = showLibrariesStr;
                             showLiFound = true;
                         }
                     }
                 }
             });

             // Add the list items if they weren't found
             const list = document.querySelector('#plex-symlink-setup-info .info-list');
             if (list) {
                 if (!movieLiFound) {
                     list.insertAdjacentHTML('beforeend', `<li><span class="var-label">Movie Libraries:</span> <span class="var-value">${movieLibrariesStr}</span></li>`);
                 }
                 if (!showLiFound) {
                     list.insertAdjacentHTML('beforeend', `<li><span class="var-label">Show Libraries:</span> <span class="var-value">${showLibrariesStr}</span></li>`);
                 }
             }

             // Attach handler ONLY here after save, ensuring it wasn't attached before
             const scanForm = document.getElementById('scan-plex-library-form'); // Get scan form within the symlink section
             if (scanForm && !scanForm.dataset.scanHandlerAttached) {
                console.log('[handleSavePlexLibraries] Attaching handler for symlink scan after save');
                setupScanHandler(scanForm);
                scanForm.dataset.scanHandlerAttached = 'true';
             }

        } else {
            showPopup({
                type: POPUP_TYPES.ERROR,
                message: `Failed to save libraries: ${data.error || 'Unknown error'}`,
                title: 'Error'
            });
        }
    })
    .catch(error => {
        Loading.hide();
        showPopup({
            type: POPUP_TYPES.ERROR,
            message: `An error occurred: ${error.message}`,
            title: 'Error'
        });
    });
}
// --- END UPDATED Plex Library Selection Functions ---
