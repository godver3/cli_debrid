/* Moved from templates/debug_functions.html. */
// showPopup and POPUP_TYPES are set on window by notifications.js, which base.html loads earlier
const { showPopup, POPUP_TYPES } = window;
// The 'Loading' object will be accessed from the global scope,
// assuming loading.js makes it available there (e.g., window.Loading = Loading)
// and is loaded before this module script.

// Restore the active tab early
if (typeof restoreActiveTab === 'function') {
    restoreActiveTab();
}

// Define showToast function for notifications
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;

    document.body.appendChild(toast);

    setTimeout(() => {
        toast.classList.add('show');
    }, 10);

    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => {
            toast.remove();
        }, 300);
    }, 3000);
}

document.addEventListener('DOMContentLoaded', function() {
    // --- Generic Form Handler (with Confirmation Popup) ---
    // Select only forms intended for the generic handler
    const genericFormIds = [
        'bulk-delete-form',
        'download-logs-form', 
        'delete-database-form',
        'refresh-release-dates-form',
        'move-to-upgrading-form',
        'version-propagator-form',
        'direct-emby-scan-form',
        'resync-symlinks-form'
        // Add other form IDs here if they follow the simple submitForm pattern
    ];

    // --- New Bulk Subtitle Scan Form Handler ---
    const bulkSubtitleScanForm = document.getElementById('bulk-subtitle-scan-form');
    if (bulkSubtitleScanForm) {
        bulkSubtitleScanForm.addEventListener('submit', function(e) {
            e.preventDefault();
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: 'Are you sure you want to start the bulk subtitle scan? This may take a while and consume resources.',
                title: 'Confirmation',
                onConfirm: () => {
                    Loading.show();
                    fetch('/debug/run_bulk_subtitle_scan', {
                        method: 'POST'
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (data.task_id) {
                            pollTaskStatus(data.task_id);
                        } else {
                            Loading.hide();
                            showPopup({
                                type: POPUP_TYPES.ERROR,
                                message: data.error || 'Failed to start task',
                                title: 'Error'
                            });
                        }
                    })
                    .catch(error => {
                        Loading.hide();
                        console.error('Error:', error);
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: 'An error occurred while starting the scan.',
                            title: 'Error'
                        });
                    });
                }
            });
        });
    }
    // --- End New Bulk Subtitle Scan Form Handler ---

    genericFormIds.forEach(formId => {
        const form = document.getElementById(formId);
        if (form) {
            form.addEventListener('submit', function(e) {
                e.preventDefault();
                const url = form.action;
                const method = form.method;

                if (form.id === 'download-logs-form') {
                    downloadLogs();
                    return;
                }

                let confirmMessage = 'Are you sure you want to perform this action?';
                if (form.id === 'delete-database-form') {
                    const confirmDelete = document.getElementById('confirm_delete').value;
                    if (confirmDelete !== 'DELETE') {
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: 'Please type DELETE to confirm database deletion',
                            title: 'Error'
                        });
                        return;
                    }
                    confirmMessage = 'Are you sure you want to delete the entire database? This action cannot be undone.';
                } else if (form.id === 'get-collected-form') {
                    confirmMessage = 'Are you sure you want to get collected items from Plex?';
                } else if (form.id === 'get-wanted-form') {
                    confirmMessage = 'Are you sure you want to get wanted items from the selected source?';
                } else if (form.id === 'propagate-version-form') {
                    confirmMessage = 'Are you sure you want to add a propagated version for every original version found?';
                }

                showPopup({
                    type: POPUP_TYPES.CONFIRM,
                    message: confirmMessage,
                    title: 'Confirmation',
                    onConfirm: () => {
                        Loading.show();
                        submitForm(url, method, new FormData(form));
                    }
                });
            });
        }
    });

    function submitForm(url, method, formData) {
        fetch(url, {
            method: method,
            body: formData
        })
        .then(response => response.json())
        .then(data => {
            Loading.hide();
            if (data.success) {
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    message: data.message || 'Action completed successfully',
                    title: 'Success'
                });
                if (method === 'POST' && url.includes('delete_database')) {
                    setTimeout(() => {
                        window.location.reload();
                    }, 3000);
                } else {
                    const form = document.querySelector(`form[action="${url}"]`);
                    if (form) {
                        form.reset();
                    }
                }
            } else {
                throw new Error(data.error || 'An error occurred');
            }
        })
        .catch(error => {
            Loading.hide();
            console.error('Error:', error);
            showPopup({
                type: POPUP_TYPES.ERROR,
                message: error.message || 'An error occurred',
                title: 'Error'
            });
        });
    }

    function downloadLogs() {
        const lines = document.getElementById('log_lines').value;
        const url = `/logs/api/logs?lines=${lines}&download=true`;

        fetch(url)
            .then(response => {
                if (!response.ok) {
                    throw new Error('Network response was not ok');
                }
                return response.blob();
            })
            .then(blob => {
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.style.display = 'none';
                a.href = url;
                a.download = 'debug.log';
                document.body.appendChild(a);
                a.click();
                window.URL.revokeObjectURL(url);
            })
            .catch(error => {
                console.error('Error:', error);
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'Failed to download logs',
                    title: 'Error'
                });
            });
    }

    function checkProgramStatus() {
        fetch('/program_operation/api/program_status')
            .then(response => response.json())
            .then(data => {
                // DISABLED: Allow debug functions while program is running for testing
                const isRunning = false; // Hardcoded to false to allow testing
                // const isRunning = data.status === 'Running';
                const buttons = document.querySelectorAll('button[type="submit"]');
                buttons.forEach(button => {
                    button.disabled = isRunning;
                    button.style.opacity = isRunning ? '0.5' : '1';
                    button.style.cursor = isRunning ? 'not-allowed' : 'pointer';
                });

                const runningMessage = document.getElementById('programRunningMessage');
                if (isRunning) {
                    if (!runningMessage) {
                        const message = document.createElement('div');
                        message.id = 'programRunningMessage';
                        message.textContent = 'Program is running. Debug functions are disabled.';
                        message.style.color = 'red';
                        message.style.marginBottom = '10px';
                        document.querySelector('.debug-container').prepend(message);
                    }
                } else if (runningMessage) {
                    runningMessage.remove();
                }
            })
            .catch(error => console.error('Error checking program status:', error));
    }

    checkProgramStatus();
    setInterval(checkProgramStatus, 5000);

    const queueSelect = document.getElementById('queue_select');
    const queueItemsContainer = document.getElementById('queue_items_container');
    const queueItems = document.getElementById('queue_items');
    const actionSelect = document.getElementById('action_select');
    const targetQueueContainer = document.getElementById('target_queue_container');
    const selectAllBtn = document.getElementById('select_all_btn');
    const unselectAllBtn = document.getElementById('unselect_all_btn');

    let lastChecked = null;

    queueSelect.addEventListener('change', function() {
        console.log('Queue selected:', this.value);
        fetchQueueItems(this.value);
    });

    actionSelect.addEventListener('change', function() {
        targetQueueContainer.style.display = this.value === 'move' ? 'block' : 'none';
    });

    selectAllBtn.addEventListener('click', function() {
        toggleAllCheckboxes(true);
    });

    unselectAllBtn.addEventListener('click', function() {
        toggleAllCheckboxes(false);
    });

    function toggleAllCheckboxes(checked) {
        const checkboxes = queueItems.querySelectorAll('input[type="checkbox"]');
        checkboxes.forEach(checkbox => {
            checkbox.checked = checked;
        });
    }

    function fetchQueueItems(queue) {
        console.log('Fetching queue items for:', queue);
        queueItemsContainer.style.display = 'block';
        queueItems.innerHTML = 'Loading...';

        fetch(`/debug/api/bulk_queue_contents`)
            .then(response => response.json())
            .then(data => {
                console.log('Received data:', data);
                const items = data[queue] || [];
                console.log('Items for queue:', items);

                if (items.length === 0) {
                    queueItems.innerHTML = '<p>No items in this queue.</p>';
                } else {
                    queueItems.innerHTML = items.map(item => `
                        <div class="queue-item">
                            <input type="checkbox" name="selected_items" value="${item.id}" id="item_${item.id}">
                            <label for="item_${item.id}">${item.title} (ID: ${item.id})</label>
                        </div>
                    `).join('');
                    addCheckboxListeners();
                }
            })
            .catch(error => {
                console.error('Error fetching queue items:', error);
                queueItems.innerHTML = '<p>Error loading queue items. Please try again.</p>';
            });
    }

    function addCheckboxListeners() {
        const checkboxes = queueItems.querySelectorAll('input[type="checkbox"]');
        checkboxes.forEach(checkbox => {
            checkbox.addEventListener('click', handleCheckboxClick);
        });
    }

    function handleCheckboxClick(e) {
        if (!lastChecked) {
            lastChecked = e.target;
            return;
        }

        if (e.shiftKey) {
            const start = Array.from(queueItems.querySelectorAll('input[type="checkbox"]')).indexOf(e.target);
            const end = Array.from(queueItems.querySelectorAll('input[type="checkbox"]')).indexOf(lastChecked);
            const checkboxes = queueItems.querySelectorAll('input[type="checkbox"]');

            checkboxes.forEach((checkbox, i) => {
                if (i >= Math.min(start, end) && i <= Math.max(start, end)) {
                    checkbox.checked = lastChecked.checked;
                }
            });
        }

        if (e.ctrlKey || e.metaKey) {
            e.preventDefault();
            e.target.checked = !e.target.checked;
        }

        lastChecked = e.target;
    }

    const bulkActionForm = document.getElementById('bulk-queue-action-form');
    if (bulkActionForm) {
        bulkActionForm.addEventListener('submit', function(e) {
            e.preventDefault();
            performBulkAction();
        });
    }

    async function processBatchedItems(items, action, targetQueue = null) {
        const FRONTEND_BATCH_SIZE = 100; // Process 100 items at a time
        let totalProcessed = 0;
        let errors = [];

        for (let i = 0; i < items.length; i += FRONTEND_BATCH_SIZE) {
            const batch = items.slice(i, i + FRONTEND_BATCH_SIZE);
            const formData = new FormData();
            formData.append('action', action);
            if (targetQueue) {
                formData.append('target_queue', targetQueue);
            }

            batch.forEach(id => {
                formData.append('selected_items', id);
            });

            try {
                const response = await fetch('/database/bulk_queue_action', {
                    method: 'POST',
                    body: formData,
                });
                const data = await response.json();

                if (data.success) {
                    totalProcessed += batch.length;
                } else {
                    errors.push(`Batch ${Math.floor(i/FRONTEND_BATCH_SIZE) + 1}: ${data.error}`);
                }
            } catch (error) {
                errors.push(`Batch ${Math.floor(i/FRONTEND_BATCH_SIZE) + 1}: ${error.message}`);
            }
        }

        return {
            success: errors.length === 0,
            warning: errors.length > 0 && totalProcessed > 0,
            message: errors.length > 0 
                ? `Processed ${totalProcessed} items with ${errors.length} batch errors. Errors: ${errors.join('; ')}`
                : `Successfully processed ${totalProcessed} items`,
        };
    }

    function performBulkAction() {
        const selectedItems = document.querySelectorAll('input[name="selected_items"]:checked');
        if (selectedItems.length === 0) {
            showPopup({
                type: POPUP_TYPES.ERROR,
                message: 'Please select at least one item to perform the action.',
                title: 'No Items Selected'
            });
            return;
        }

        const action = document.getElementById('action_select').value;
        const actionText = action === 'delete' ? 'delete' : 'move';
        const targetQueue = action === 'move' ? document.getElementById('target_queue').value : null;

        if (action === 'move' && !targetQueue) {
            showPopup({
                type: POPUP_TYPES.ERROR,
                message: 'Please select a target queue.',
                title: 'No Target Queue Selected'
            });
            return;
        }

        const selectedItemIds = Array.from(selectedItems).map(checkbox => checkbox.value);
        const confirmMessage = `Are you sure you want to ${actionText} ${selectedItems.length} item(s)${targetQueue ? ` to ${targetQueue} queue` : ''}?`;

        showPopup({
            type: POPUP_TYPES.CONFIRM,
            message: confirmMessage,
            title: 'Confirm Bulk Action',
            onConfirm: async () => {
                Loading.show();
                try {
                    const result = await processBatchedItems(selectedItemIds, action, targetQueue);
                    showPopup({
                        type: result.warning ? POPUP_TYPES.WARNING : POPUP_TYPES.SUCCESS,
                        message: result.message,
                        title: result.warning ? 'Partial Success' : 'Success',
                    });
                    setTimeout(() => {
                        fetchQueueItems(queueSelect.value);
                    }, 2000);
                } catch (error) {
                    console.error('Error performing bulk action:', error);
                    showPopup({
                        type: POPUP_TYPES.ERROR,
                        message: 'An unexpected error occurred while performing the bulk action.',
                        title: 'Error',
                    });
                } finally {
                    Loading.hide();
                }
            },
        });
    }

    // Initial fetch of queue items
    console.log('Initial queue fetch');
    fetchQueueItems(queueSelect.value);

    // Add this new function to fetch and display rate limit information
    function fetchRateLimitInfo() {
        console.log('Fetching rate limit info...');

        // Get the rate-limit-info element
        const rateLimitInfoElement = document.getElementById('rate-limit-info');
        if (!rateLimitInfoElement) {
            console.error('rate-limit-info element not found!');
            return;
        }

        // Show loading message
        document.getElementById('rate-limit-loading').style.display = 'block';
        document.getElementById('rate-limit-content').style.display = 'none';

        // Fetch the rate limit data
        fetch('/debug/api/rate_limit_info')
            .then(response => {
                console.log('Rate limit response status:', response.status);
                if (!response.ok) {
                    throw new Error(`HTTP error! Status: ${response.status}`);
                }
                return response.json();
            })
            .then(data => {
                console.log('Rate limit data received:', data);

                // Build HTML string for the rate limit info
                let html = '<h4>Rate Limits by Domain:</h4>';

                if (!data || Object.keys(data).length === 0) {
                    html += '<p>No rate limit data available.</p>';
                } else {
                    for (const [domain, limits] of Object.entries(data)) {
                        const fiveMinClass = limits.five_minute.count > limits.five_minute.limit ? 'rate-limit-warning' : 'rate-limit-normal';
                        const hourlyClass = limits.hourly.count > limits.hourly.limit ? 'rate-limit-warning' : 'rate-limit-normal';

                        html += `
                            <div class="domain-rate-limit">
                                <h5>${domain}</h5>
                                <p class="${fiveMinClass}">5-minute: ${limits.five_minute.count} / ${limits.five_minute.limit} requests</p>
                                <p class="${hourlyClass}">Hourly: ${limits.hourly.count} / ${limits.hourly.limit} requests</p>
                            </div>
                        `;
                    }
                }

                // Update the rate limit info element with the new HTML
                document.getElementById('rate-limit-loading').style.display = 'none';
                document.getElementById('rate-limit-content').style.display = 'block';
                document.getElementById('rate-limit-content').innerHTML = html;
            })
            .catch(error => {
                console.error('Error fetching rate limit info:', error);
                document.getElementById('rate-limit-loading').style.display = 'none';
                document.getElementById('rate-limit-content').style.display = 'block';
                document.getElementById('rate-limit-content').innerHTML = '<p>Error loading rate limit information: ' + error.message + '</p>';
            });
    }

    // Ensure rate limit info is displayed on page load
    document.addEventListener('DOMContentLoaded', function() {
        console.log('DOM fully loaded, checking rate limit info');

        // Check if rate limit info is displayed after a short delay
        setTimeout(function() {
            const rateLimitInfo = document.getElementById('rate-limit-info');
            if (rateLimitInfo && document.getElementById('rate-limit-loading').style.display === 'block') {
                console.log('Rate limit info still loading, trying again');
                fetchRateLimitInfo();
            }
        }, 2000);
    });

    // Fetch rate limit info on page load
    fetchRateLimitInfo();

    // Add event listener for the refresh button
    const refreshRateLimitsButton = document.getElementById('refresh-rate-limits');
    if (refreshRateLimitsButton) {
        refreshRateLimitsButton.addEventListener('click', function() {
            // Add a visual indicator that refresh is happening
            this.textContent = 'Refreshing...';
            this.disabled = true;

            // Call the fetch function
            fetchRateLimitInfo();

            // Reset button after a short delay
            setTimeout(() => {
                this.textContent = 'Refresh Rate Limits';
                this.disabled = false;
            }, 1000);
        });
    } else {
        console.error('Refresh rate limits button not found!');
    }

    const sendTestNotificationForm = document.getElementById('send-test-notification-form');
    if (sendTestNotificationForm) {
        sendTestNotificationForm.addEventListener('submit', function(e) {
            e.preventDefault();
            sendTestNotification();
        });
    }

    function sendTestNotification() {
        console.log('sendTestNotification function called');
        const url = '/debug/send_test_notification';
        console.log('Sending request to:', url);
        fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({})  // Send an empty object as body
        })
        .then(response => {
            console.log('Response status:', response.status);
            console.log('Response headers:', response.headers);
            return response.text().then(text => {
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}, body: ${text}`);
                }
                try {
                    return JSON.parse(text);
                } catch (e) {
                    console.error('Error parsing JSON:', e);
                    throw new Error(`Invalid JSON response: ${text}`);
                }
            });
        })
        .then(data => {
            if (data.success) {
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    message: data.message || 'Test notification sent successfully',
                    title: 'Success'
                });
            } else {
                throw new Error(data.error || 'An error occurred while sending the test notification');
            }
        })
        .catch(error => {
            console.error('Error details:', error);
            console.error('Error name:', error.name);
            console.error('Error message:', error.message);
            console.error('Error stack:', error.stack);
            showPopup({
                type: POPUP_TYPES.ERROR,
                message: `An error occurred: ${error.message}. Please check the server logs for more details.`,
                title: 'Error'
            });
        });
    }

    // Define pollTaskStatus function once
    function pollTaskStatus(taskId) {
        fetch(`/debug/api/task_status/${taskId}`)
            .then(response => response.json())
            .then(data => {
                if (data.status === 'PENDING' || data.status === 'RUNNING') {
                    setTimeout(() => pollTaskStatus(taskId), 2000);  // Poll every 2 seconds
                } else {
                    Loading.hide();
                    if (data.status === 'SUCCESS') {
                        // Handle data.result appropriately
                        let message = 'Task completed successfully';
                        if (typeof data.result === 'string') {
                            message = data.result;
                        } else if (typeof data.result === 'object') {
                            // You can adjust this based on the actual structure of data.result
                            message = data.result.message || JSON.stringify(data.result);
                        }
                        showPopup({
                            type: POPUP_TYPES.SUCCESS,
                            message: message,
                            title: 'Success'
                        });
                    } else {
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: data.result || 'Task failed',
                            title: 'Error'
                        });
                    }
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

    // Handler for getCollectedForm
    const getCollectedForm = document.getElementById('get-collected-form');
    getCollectedForm.addEventListener('submit', function(e) {
        e.preventDefault();
        const formData = new FormData(this);
        const collectionType = formData.get('collection_type');

        showPopup({
            type: POPUP_TYPES.CONFIRM,
            message: 'Are you sure you want to get collected items from Plex?',
            title: 'Confirmation',
            onConfirm: () => {
                Loading.show();
                fetch(this.action, { // this.action is /debug/api/get_collected_from_plex
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ collection_type: collectionType })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.task_id) {
                        pollTaskStatus(data.task_id);
                    } else {
                        Loading.hide();
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: 'Failed to start task',
                            title: 'Error'
                        });
                    }
                })
                .catch(error => {
                    Loading.hide();
                    console.error('Error:', error);
                    showPopup({
                        type: POPUP_TYPES.ERROR,
                        message: 'An error occurred',
                        title: 'Error'
                    });
                });
            }
        });
    });

    // Handler for getWantedForm
    const getWantedForm = document.getElementById('get-wanted-form');
    getWantedForm.addEventListener('submit', function(e) {
        e.preventDefault();
        const formData = new FormData(this);
        const sourceId = formData.get('source');

        // Get the selected source text (display name) and value
        const sourceSelect = document.getElementById('wanted_source');
        const sourceValue = sourceSelect.value;
        const sourceDisplayName = sourceSelect.options[sourceSelect.selectedIndex].textContent;

        let confirmMessage = 'Are you sure you want to get wanted items?';
        if (sourceValue !== 'all') {
            confirmMessage = `Are you sure you want to get wanted items from "${sourceDisplayName}"?`;
        }

        // Adding a confirmation popup
        showPopup({
            type: POPUP_TYPES.CONFIRM,
            message: confirmMessage,
            title: 'Confirmation',
            onConfirm: () => {
                Loading.show();
                fetch(this.action, { // this.action is /debug/api/get_wanted_content
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ source_id: sourceId })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.task_id) {
                        pollTaskStatus(data.task_id);
                    } else {
                        Loading.hide();
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: 'Failed to start task',
                            title: 'Error'
                        });
                    }
                })
                .catch(error => {
                    Loading.hide();
                    console.error('Error:', error);
                    showPopup({
                        type: POPUP_TYPES.ERROR,
                        message: 'An error occurred',
                        title: 'Error'
                    });
                });
            }
        });
    });

    // Fetch available tasks and populate the select element
    fetch('/debug/get_available_tasks')
        .then(response => response.json())
        .then(data => {
            const taskSelect = document.getElementById('task_select');

            // Clear any existing options
            taskSelect.innerHTML = '';

            // Add special Full cli_mount Sync option at top
            const fullSyncOpt = document.createElement('option');
            fullSyncOpt.value = '__full_climount_sync__';
            fullSyncOpt.textContent = 'Full cli_mount Sync (reset + full)';
            taskSelect.appendChild(fullSyncOpt);

            // Check if we have the new structured format
            if (data.task_map) {
                // Sort tasks alphabetically by display name
                data.task_map.sort((a, b) => a.display_name.localeCompare(b.display_name));

                // Add options with display names
                data.task_map.forEach(task => {
                    const option = document.createElement('option');
                    option.value = task.id;
                    option.textContent = task.display_name;
                    taskSelect.appendChild(option);
                });
            } else {
                // Fallback to old format if task_map is not available
                data.tasks.forEach(task => {
                    const option = document.createElement('option');
                    option.value = task;

                    // Format the task name
                    let displayName = task;
                    if (task.startsWith('task_')) {
                        displayName = task.replace('task_', '').replace(/_/g, ' ');
                        // Capitalize first letter of each word
                        displayName = displayName.replace(/\b\w/g, c => c.toUpperCase());
                    }

                    option.textContent = displayName;
                    taskSelect.appendChild(option);
                });
            }
        })
        .then(() => {
            const filter = document.getElementById('task_select_filter');
            const sel = document.getElementById('task_select');
            if (!filter || !sel) return;
            const allOptions = Array.from(sel.options).map(o => ({ value: o.value, text: o.textContent }));
            filter.addEventListener('input', function() {
                const q = this.value.trim().toLowerCase();
                sel.innerHTML = '';
                const matches = q ? allOptions.filter(o => o.text.toLowerCase().includes(q)) : allOptions;
                matches.forEach(o => {
                    const opt = document.createElement('option');
                    opt.value = o.value;
                    opt.textContent = o.text;
                    sel.appendChild(opt);
                });
                const newSize = q && matches.length > 0 ? matches.length : 1;
                sel.size = newSize;
                sel.style.backgroundImage = newSize > 1 ? 'none' : '';
                sel.style.paddingRight = newSize > 1 ? '10px' : '';
                sel.style.overflowY = newSize > 1 ? 'hidden' : '';
            });
            function collapseSelect() {
                sel.size = 1;
                sel.style.backgroundImage = '';
                sel.style.paddingRight = '';
                sel.style.overflowY = '';
            }
            filter.addEventListener('blur', function() {
                setTimeout(collapseSelect, 150);
            });
            sel.addEventListener('change', function() {
                collapseSelect();
                filter.value = '';
            });
        })
        .catch(error => console.error('Error fetching available tasks:', error));

    // Add event listener for the run task form
    const runTaskForm = document.getElementById('run-task-form');
    runTaskForm.addEventListener('submit', function(e) {
        e.preventDefault();
        const taskSelect = document.getElementById('task_select');
        const taskName = taskSelect.value;
        const taskDisplayName = taskSelect.options[taskSelect.selectedIndex].textContent;

        // Handle special Full cli_mount Sync option
        if (taskName === '__full_climount_sync__') {
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: 'Reset sync timestamp and run a full cli_mount sync?',
                title: 'Full cli_mount Sync',
                onConfirm: () => {
                    Loading.show();
                    fetch('/debug/run_full_climount_sync', { method: 'POST' })
                        .then(r => r.json())
                        .then(d => {
                            Loading.hide();
                            if (d.success) showPopup({ type: POPUP_TYPES.SUCCESS, title: 'Success', message: d.message });
                            else showPopup({ type: POPUP_TYPES.ERROR, title: 'Error', message: d.error || 'Failed' });
                        }).catch(() => { Loading.hide(); });
                }
            });
            return;
        }

        showPopup({
            type: POPUP_TYPES.CONFIRM,
            message: `Are you sure you want to run the task "${taskDisplayName}"?`,
            title: 'Confirmation',
            onConfirm: () => {
                Loading.show();
                fetch('/debug/run_task', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        task_name: taskName
                    })
                })
                .then(response => response.json())
                .then(data => {
                    Loading.hide();
                    if (data.success) {
                        // --- START EDIT: Update success message for "Run Task Manually" ---
                        let successMessage = data.message; // Default to message from backend (e.g., "Task 'X' queued...")
                        if (data.job_id) {
                            successMessage += ` (Job ID: ${data.job_id})`;
                        }
                        showPopup({
                            type: POPUP_TYPES.SUCCESS,
                            message: successMessage, // Display the "queued" message
                            title: 'Task Action' // Changed title from 'Success' to 'Task Action'
                        });
                        // We might not want to poll here by default for all manually run tasks,
                        // as some are quick. Polling could be added selectively if needed.
                        // If polling is desired, here's where you could initiate it:
                        // if (data.job_id) {
                        //     pollTaskStatus(data.job_id); // Assuming pollTaskStatus handles Loading.hide()
                        // }
                        // --- END EDIT ---
                    } else {
                        throw new Error(data.error || 'Failed to run task');
                    }
                })
                .catch(error => {
                    Loading.hide();
                    console.error('Error:', error);
                    showPopup({
                        type: POPUP_TYPES.ERROR,
                        message: error.message || 'An error occurred while running the task',
                        title: 'Error'
                    });
                });
            }
        });
    });

    // Reset battery show cache button
    document.getElementById('reset-battery-show-cache-btn').addEventListener('click', function() {
        const btn = this;
        const result = document.getElementById('reset-battery-show-cache-result');
        btn.disabled = true;
        btn.textContent = 'Resetting...';
        result.textContent = '';
        fetch('/debug/reset_battery_show_cache', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                btn.disabled = false;
                btn.textContent = 'Reset Show Cache';
                result.style.color = data.success ? 'green' : 'red';
                result.textContent = data.success ? data.message : ('Error: ' + data.error);
            })
            .catch(err => {
                btn.disabled = false;
                btn.textContent = 'Reset Show Cache';
                result.style.color = 'red';
                result.textContent = 'Error: ' + err.message;
            });
    });

    // Load versions for the version propagator dropdowns
    function loadVersions() {
        fetch('/debug/get_versions')
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    const originalSelect = document.getElementById('original_version');
                    const propagatedSelect = document.getElementById('propagated_version');

                    // Clear existing options except the first one
                    while (originalSelect.options.length > 1) originalSelect.remove(1);
                    while (propagatedSelect.options.length > 1) propagatedSelect.remove(1);

                    // Add new options
                    data.versions.forEach(version => {
                        const originalOption = new Option(version, version);
                        const propagatedOption = new Option(version, version);
                        originalSelect.add(originalOption);
                        propagatedSelect.add(propagatedOption);
                    });
                } else {
                    showPopup({
                        type: POPUP_TYPES.ERROR,
                        message: data.error,
                        title: 'Error Loading Versions'
                    });
                }
            })
            .catch(error => {
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'Error loading versions: ' + error,
                    title: 'Error'
                });
            });
    }

    // Load versions when the page loads
    loadVersions();

    // Add event listener for convert to symlinks form
    const convertToSymlinksForm = document.getElementById('convert-to-symlinks-form');
    const convertProgressDiv = document.getElementById('convert-to-symlinks-progress');

    if (convertToSymlinksForm && convertProgressDiv) {
        convertToSymlinksForm.addEventListener('submit', function(e) {
            e.preventDefault();
            convertProgressDiv.innerHTML = ''; // Clear previous progress

            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: 'Are you sure you want to convert library items to use symlinks? This is ONLY intended for converting existing Plex libraries to a Symlinked library, and should only be done once. This action is not reversible.',
                title: 'Confirmation',
                onConfirm: () => {
                    Loading.show();
                    fetch('/debug/convert_to_symlinks', {
                        method: 'POST',
                    })
                    .then(response => {
                        if (!response.ok) {
                            // Try to get error text, then throw
                            return response.text().then(text => {
                                 throw new Error(text || `Server responded with status ${response.status}`);
                            });
                        }
                        // Check content type BEFORE parsing
                        const contentType = response.headers.get('content-type');
                         if (contentType && contentType.includes('application/json')) {
                            return response.json();
                        }
                         throw new Error('Server response was not JSON'); // Or handle non-JSON if expected
                    })
                    .then(data => {
                        Loading.hide();
                        // --- Start Task Monitoring ---
                        if (data.success && data.task_id) {
                            convertProgressDiv.innerHTML = `<p>Conversion task started (ID: ${data.task_id}). Monitoring progress...</p>`;
                            monitorConversionProgress(data.task_id, convertProgressDiv); // Call the monitoring function
                        } else {
                            throw new Error(data.error || 'Failed to start conversion task.');
                        }
                        // --- End Task Monitoring ---
                    })
                    .catch(error => {
                        Loading.hide();
                        console.error('Error starting conversion:', error);
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: error.message || 'An error occurred while starting the conversion.',
                            title: 'Error'
                        });
                         convertProgressDiv.innerHTML = `<p class="error">Error: ${error.message}</p>`;
                    });
                }
            });
        });
    }

    // --- Add New Monitoring Function ---
    function monitorConversionProgress(taskId, targetDiv) {
        targetDiv.innerHTML = `<p>Connecting to progress stream for task ${taskId}...</p>`;
        const eventSource = new EventSource(`/debug/api/conversion_progress/${taskId}`); // Use the correct endpoint

        eventSource.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);
                let progressHtml = `<p>Status: ${data.status || 'N/A'}</p><p>Message: ${data.message || 'N/A'}</p>`;

                // Display specific counts if available
                progressHtml += `<p>Files Scanned: ${data.total_files_scanned || 0}</p>`;
                progressHtml += `<p>Media Files Found: ${data.media_files_found || 0}</p>`;
                progressHtml += `<p>Items Processed: ${data.items_processed || 0}</p>`;
                progressHtml += `<p>DB Entries Added: ${data.items_added_to_db || 0}</p>`;
                progressHtml += `<p>Symlinks Created: ${data.symlinks_created || 0}</p>`;
                progressHtml += `<p>Skipped (Duplicates): ${data.skipped_duplicates || 0}</p>`;

                let errorsHtml = '';
                if (data.parser_errors > 0) errorsHtml += `<li>Parser Errors: ${data.parser_errors}</li>`;
                if (data.metadata_errors > 0) errorsHtml += `<li>Metadata Errors: ${data.metadata_errors}</li>`;
                if (data.db_errors > 0) errorsHtml += `<li>Database Errors: ${data.db_errors}</li>`;
                if (data.symlink_errors > 0) errorsHtml += `<li>Symlink Errors: ${data.symlink_errors}</li>`;
                if (data.errors && data.errors.length > 0) {
                     errorsHtml += `<li>General Errors: ${data.errors.length}</li>`;
                     // Optionally list first few general errors
                     // data.errors.slice(0, 3).forEach(err => { errorsHtml += `<li style="margin-left: 15px;">- ${err}</li>`; });
                }

                if (errorsHtml) {
                    progressHtml += `<p class="error">Errors Encountered:</p><ul>${errorsHtml}</ul>`;
                }

                // Display Dry Run Preview
                if (data.preview && data.preview.length > 0) {
                    progressHtml += '<h4>Dry Run Preview (First 5):</h4><ul>';
                    data.preview.forEach(item => {
                        progressHtml += `<li>${item.original_file} -> ${item.symlink_path || 'Error'} (Title: ${item.fetched_title || 'N/A'})</li>`;
                    });
                    progressHtml += '</ul>';
                }


                targetDiv.innerHTML = progressHtml;

                if (data.complete) {
                    eventSource.close();
                    console.log(`Progress stream closed for conversion task ${taskId}`);
                    showPopup({
                         type: data.success ? POPUP_TYPES.SUCCESS : POPUP_TYPES.ERROR,
                         message: data.message || (data.success ? 'Conversion complete.' : 'Conversion failed or finished with errors.'),
                         title: data.success ? 'Conversion Complete' : 'Conversion Finished'
                    });
                }
            } catch (e) {
                console.error("Error parsing progress update:", e, "Raw data:", event.data);
                targetDiv.innerHTML = `<p class="error">Error receiving progress update.</p>`;
                eventSource.close(); // Close on parsing error
            }
        };

        eventSource.onerror = function(error) {
            console.error("EventSource failed:", error);
            targetDiv.innerHTML = `<p class="error">Connection to progress stream lost.</p>`;
            eventSource.close();
             showPopup({
                type: POPUP_TYPES.ERROR,
                message: 'Lost connection to the progress stream. Please check server logs.',
                title: 'Connection Error'
            });
        };
    }
    // --- End New Monitoring Function ---

    function updatePlexTokenStatus(data) {
        const container = document.querySelector('.token-status-container');
        container.innerHTML = '';

        if (!data.success) {
            container.innerHTML = `<div class="error">Error: ${data.error}</div>`;
            return;
        }

        Object.entries(data.token_status).forEach(([username, status]) => {
            const tokenDiv = document.createElement('div');
            tokenDiv.className = `token-status ${status.valid ? 'valid' : 'invalid'}`;

            let expiresText = 'No expiration date';
            if (status.expires_at) {
                const expiresDate = new Date(status.expires_at);
                if (!isNaN(expiresDate)) {
                    expiresText = `Expires: ${expiresDate.toLocaleString()}`;
                }
            }

            let lastCheckedText = '';
            if (status.last_checked) {
                const lastCheckedDate = new Date(status.last_checked);
                if (!isNaN(lastCheckedDate)) {
                    lastCheckedText = `Last checked: ${lastCheckedDate.toLocaleString()}`;
                }
            }

            tokenDiv.innerHTML = `
                <div class="token-user">${username}</div>
                <div class="token-details">
                    <span class="token-validity ${status.valid ? 'valid' : 'invalid'}">
                        ${status.valid ? 'Valid' : 'Invalid'}
                    </span>
                    <hr class="token-sep">
                    ${status.username ? `<span class="token-username"><span class="token-detail-label">Plex Username:</span> <strong>${status.username}</strong></span>` : ''}
                    <span class="token-expiry">${expiresText}</span>
                    <hr class="token-sep">
                    <span class="token-last-checked">${lastCheckedText}</span>
                </div>
            `;
            container.appendChild(tokenDiv);
        });
    }

    function refreshPlexTokens() {
        const button = document.getElementById('refresh-plex-tokens');
        button.disabled = true;
        button.textContent = 'Refreshing...';

        fetch('/debug/validate_plex_tokens', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        })
        .then(response => response.json())
        .then(data => {
            updatePlexTokenStatus(data);
        })
        .catch(error => {
            console.error('Error:', error);
            const container = document.querySelector('.token-status-container');
            container.innerHTML = `<div class="error">Error refreshing tokens: ${error}</div>`;
        })
        .finally(() => {
            button.disabled = false;
            button.textContent = 'Refresh Tokens';
        });
    }

    // Initial fetch to show current status without refreshing
    fetch('/debug/validate_plex_tokens', {
        method: 'GET'
    })
    .then(response => response.json())
    .then(data => {
        updatePlexTokenStatus(data);
    })
    .catch(error => {
        console.error('Error:', error);
        const container = document.querySelector('.token-status-container');
        container.innerHTML = `<div class="error">Error loading token status: ${error}</div>`;
    });

    // Add click handler for refresh button
    document.getElementById('refresh-plex-tokens').addEventListener('click', () => {
        refreshPlexTokens();
    });

    function updateTraktTokenStatus(data) {
        const container = document.querySelector('#trakt-token-status .token-status-container');
        if (!data.success) {
            container.innerHTML = `<div class="error">Error: ${data.error}</div>`;
            return;
        }

        const status = data.status;
        const tokenData = status.token_data || {};
        const expiresAt = status.expires_at ? new Date(status.expires_at) : null;
        // Try to get last_refresh from both possible locations
        const lastRefresh = status.last_refresh ? new Date(status.last_refresh) : 
                          (tokenData.last_refresh ? new Date(tokenData.last_refresh) : null);
        const now = new Date();

        console.log('Token Status Data:', {
            status,
            tokenData,
            expiresAt,
            lastRefresh,
            rawLastRefresh: status.last_refresh,
            tokenDataLastRefresh: tokenData.last_refresh
        });

        let html = `
            <div class="token-details">
                <div class="token-validity ${status.is_authenticated ? 'valid' : 'invalid'}">
                    Status: ${status.is_authenticated ? 'Valid' : 'Invalid'}
                </div>`;

        if (expiresAt) {
            const timeUntilExpiry = Math.floor((expiresAt - now) / (1000 * 60 * 60));
            html += `
                <div class="token-expiry">
                    Expires: <span style="color:var(--text)">${expiresAt.toLocaleString()}</span> <span style="color:var(--text3)">(in ${timeUntilExpiry} hours)</span>
                </div>`;
        }

        if (lastRefresh) {
            const timeSinceRefresh = Math.floor((now - lastRefresh) / (1000 * 60));
            const hoursAgo = Math.floor(timeSinceRefresh / 60);
            const minutesAgo = timeSinceRefresh % 60;
            const timeAgoText = hoursAgo > 0 ?
                `${hoursAgo} hours ${minutesAgo} minutes ago` :
                `${minutesAgo} minutes ago`;
            html += `
                <div class="token-last-refresh">
                    Last Refreshed: <span style="color:var(--text)">${lastRefresh.toLocaleString()}</span> <span style="color:var(--text3)">(${timeAgoText})</span>
                </div>`;
        }

        if (tokenData.access_token) {
            html += `
                <div class="token-access">
                    Access Token: ${tokenData.access_token.substring(0, 10)}...
                </div>`;
        }

        if (tokenData.refresh_token) {
            html += `
                <div class="token-refresh">
                    Refresh Token: ${tokenData.refresh_token.substring(0, 10)}...
                </div>`;
        }

        html += '</div>';
        container.innerHTML = html;
    }

    function refreshTraktTokenStatus() {
        const button = document.getElementById('refresh-trakt-tokens');
        button.disabled = true;
        button.textContent = 'Refreshing...';

        fetch('/debug/api/trakt_token_status')
            .then(response => response.json())
            .then(data => {
                updateTraktTokenStatus(data);
            })
            .catch(error => {
                console.error('Error:', error);
                const container = document.querySelector('#trakt-token-status .token-status-container');
                container.innerHTML = `<div class="error">Error loading token status: ${error}</div>`;
            })
            .finally(() => {
                button.disabled = false;
                button.textContent = 'Refresh Status';
            });
    }

    // Initial fetch of token status
    refreshTraktTokenStatus();

    // Add click handler for refresh button
    document.getElementById('refresh-trakt-tokens').addEventListener('click', refreshTraktTokenStatus);

    // Verification Queue functionality
    function fetchVerificationQueue() {
        const statsContainer = document.getElementById('verification-stats');
        const queueContainer = document.getElementById('verification-queue-container');

        statsContainer.innerHTML = '<div class="stats-loading">Loading verification stats...</div>';
        queueContainer.innerHTML = '<div class="queue-loading">Loading verification queue...</div>';

        fetch('/debug/get_verification_queue')
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    // Display stats
                    const stats = data.stats;
                    statsContainer.innerHTML = `
                        <div class="stats-item">
                            <span class="stats-label">Total Files:</span>
                            <span class="stats-value">${stats.total}</span>
                        </div>
                        <div class="stats-item">
                            <span class="stats-label">Verified:</span>
                            <span class="stats-value">${stats.verified} (${stats.percent_verified}%)</span>
                        </div>
                        <div class="stats-item">
                            <span class="stats-label">Unverified:</span>
                            <span class="stats-value">${stats.unverified}</span>
                        </div>
                    `;

                    // Display queue items
                    if (data.files.length === 0) {
                        queueContainer.innerHTML = '<div class="no-items">No files in verification queue</div>';
                    } else {
                        let html = '<table class="verification-table">';
                        html += '<thead><tr>';
                        html += '<th>Title</th>';
                        html += '<th>Filename</th>';
                        html += '<th>Added</th>';
                        html += '<th>Attempts</th>';
                        html += '</tr></thead>';
                        html += '<tbody>';

                        data.files.forEach(file => {
                            const addedDate = new Date(file.added_at).toLocaleString();
                            const lastAttempt = file.last_attempt ? new Date(file.last_attempt).toLocaleString() : 'Never';

                            html += '<tr>';
                            html += `<td title="${file.title}">${file.title}</td>`;
                            html += `<td title="${file.full_path}">${file.filename}</td>`;
                            html += `<td title="Added: ${addedDate}\nLast Attempt: ${lastAttempt}">${addedDate}</td>`;
                            html += `<td>${file.attempts}</td>`;
                            html += '</tr>';
                        });

                        html += '</tbody></table>';
                        queueContainer.innerHTML = html;
                    }
                } else {
                    statsContainer.innerHTML = '<div class="error">Error loading stats: ' + data.error + '</div>';
                    queueContainer.innerHTML = '<div class="error">Error loading queue: ' + data.error + '</div>';
                }
            })
            .catch(error => {
                statsContainer.innerHTML = '<div class="error">Error loading stats: ' + error.message + '</div>';
                queueContainer.innerHTML = '<div class="error">Error loading queue: ' + error.message + '</div>';
            });
    }

    // Run verification scan
    function runVerificationScan() {
        const button = document.getElementById('run-verification-scan');
        button.disabled = true;

        // Show loading indicator
        Loading.show('Running verification scan...', 'This may take a moment depending on your library size.');

        fetch('/program_operation/trigger_task', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            body: `task_name=${encodeURIComponent('task_verify_symlinked_files')}`
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                Loading.hide();
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    title: 'Verification Scan Started',
                    message: 'Verification scan started successfully. The queue will refresh shortly.',
                    autoClose: 3000
                });
                setTimeout(() => {
                    fetchVerificationQueue();
                }, 3000); // Refresh after 3 seconds
            } else {
                Loading.hide();
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error',
                    message: `Error starting verification scan: ${data.error}`,
                    autoClose: 5000
                });
            }
            button.disabled = false;
            button.textContent = 'Verification Scan';
        })
        .catch(error => {
            Loading.hide();
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error',
                message: `Error: ${error.message}`,
                autoClose: 5000
            });
            button.disabled = false;
            button.textContent = 'Verification Scan';
        });
    }

    // Add event listeners
    document.getElementById('refresh-verification-queue').addEventListener('click', fetchVerificationQueue);
    document.getElementById('run-verification-scan').addEventListener('click', runVerificationScan);

    // Initial fetch of verification queue
    fetchVerificationQueue();

    // --- Cache File Management Logic ---
    const selectAllCacheBtn = document.getElementById('select_all_cache_btn');
    const unselectAllCacheBtn = document.getElementById('unselect_all_cache_btn');
    const manageCacheFilesForm = document.getElementById('manage-cache-files-form');

    if (selectAllCacheBtn && unselectAllCacheBtn) {
        selectAllCacheBtn.addEventListener('click', function() {
            toggleAllCacheCheckboxes(true);
        });

        unselectAllCacheBtn.addEventListener('click', function() {
            toggleAllCacheCheckboxes(false);
        });
    }

    function toggleAllCacheCheckboxes(checked) {
        if (!manageCacheFilesForm) return;
        const checkboxes = manageCacheFilesForm.querySelectorAll('input[type="checkbox"]');
        checkboxes.forEach(checkbox => {
            checkbox.checked = checked;
        });
    }

    if (manageCacheFilesForm) {
        manageCacheFilesForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const selectedCheckboxes = manageCacheFilesForm.querySelectorAll('input[name="selected_files"]:checked');

            if (selectedCheckboxes.length === 0) {
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'Please select at least one cache file to delete.',
                    title: 'No Files Selected'
                });
                return;
            }

            const fileCount = selectedCheckboxes.length;
            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: `Are you sure you want to delete ${fileCount} selected cache file(s)? This action cannot be undone.`,
                title: 'Confirm Deletion',
                onConfirm: () => {
                    Loading.show();
                    const formData = new FormData(manageCacheFilesForm);
                    fetch('/debug/api/delete_cache_files', {
                        method: 'POST',
                        body: formData
                    })
                    .then(response => response.json())
                    .then(data => {
                        Loading.hide();
                        if (data.success) {
                            showPopup({
                                type: data.errors ? POPUP_TYPES.WARNING : POPUP_TYPES.SUCCESS,
                                message: data.message,
                                title: data.errors ? 'Partial Success' : 'Success'
                            });
                            // Refresh the page or just the list after deletion
                            setTimeout(() => { 
                                window.location.reload(); // Simple page reload
                            }, 2000); 
                        } else {
                            throw new Error(data.error || 'Failed to delete cache files');
                        }
                    })
                    .catch(error => {
                        Loading.hide();
                        console.error('Error:', error);
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: error.message || 'An error occurred while deleting cache files.',
                            title: 'Error'
                        });
                    });
                }
            });
        });
    }
    // --- End Cache File Management Logic ---

    // --- Symlink Path Modification Logic ---
    const modifySymlinkForm = document.getElementById('modify-symlink-paths-form');
    const modifySymlinkResultsDiv = document.getElementById('modify-symlink-results');

    if (modifySymlinkForm) {
        modifySymlinkForm.addEventListener('submit', function(e) {
            e.preventDefault();
            modifySymlinkResultsDiv.innerHTML = ''; // Clear previous results
            Loading.show();

            const formData = new FormData(modifySymlinkForm);
            const isDryRun = formData.get('dry_run') === 'on';
            const currentSymlink = formData.get('current_symlink_base').trim();
            const newSymlink = formData.get('new_symlink_base').trim();
            const currentOriginal = formData.get('current_original_base').trim();
            const newOriginal = formData.get('new_original_base').trim();

            // Basic validation
            if (!(currentSymlink && newSymlink) && !(currentOriginal && newOriginal)) {
                Loading.hide();
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'Please provide at least one complete pair (current and new) of base paths to modify.',
                    title: 'Input Error'
                });
                return;
            }
            if ((currentSymlink && !newSymlink) || (!currentSymlink && newSymlink) || (currentOriginal && !newOriginal) || (!currentOriginal && newOriginal)){
                 Loading.hide();
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: 'If modifying a path type (symlink or original), both current and new paths must be provided.',
                    title: 'Input Error'
                });
                return;
            }

            fetch('/debug/api/modify_symlink_paths', {
                method: 'POST',
                body: formData
            })
            .then(response => response.json())
            .then(data => {
                Loading.hide();
                if (data.success) {
                    showPopup({
                        type: data.dry_run ? POPUP_TYPES.INFO : POPUP_TYPES.SUCCESS,
                        message: data.message,
                        title: data.dry_run ? 'Dry Run Results' : 'Update Complete',
                        autoClose: false
                    });

                    if (data.dry_run) {
                        let previewHtml = `<h4>Preview of Changes (${data.items_to_update_count} items affected):</h4>`;
                        if (data.preview && data.preview.length > 0) {
                            // Use a more descriptive class for modal tables
                            previewHtml += '<table class="preview-results-table"><thead><tr><th>ID</th><th>Old Location</th><th>New Location</th><th>Old Original</th><th>New Original</th></tr></thead><tbody>';
                            data.preview.forEach(item => {
                                previewHtml += `<tr>
                                    <td>${item.id}</td>
                                    <td class="path-cell">${item.old_location || 'N/A'}</td>
                                    <td class="path-cell">${item.new_location || 'N/A'}</td>
                                    <td class="path-cell">${item.old_original || 'N/A'}</td>
                                    <td class="path-cell">${item.new_original || 'N/A'}</td>
                                </tr>`;
                            });
                            previewHtml += '</tbody></table>';
                            if (data.items_to_update_count > data.preview.length) {
                                previewHtml += `<p><i>(Showing first ${data.preview.length} changes)</i></p>`;
                            }
                        } else {
                            previewHtml += '<p>No items would be changed with these inputs.</p>';
                        }
                        // Show the preview in a popup instead of the results div
                        showPopup({
                            type: POPUP_TYPES.INFO,
                            title: 'Dry Run Preview',
                            message: previewHtml,
                            large: true,
                            autoClose: false
                        });
                        modifySymlinkResultsDiv.innerHTML = '<p>Dry run preview displayed in popup.</p>'; // Update results div
                    } else {
                        // Optionally clear form or results after successful update
                        // modifySymlinkForm.reset(); // Uncomment to clear form
                        modifySymlinkResultsDiv.innerHTML = `<p>${data.message}</p>`;
                    }
                } else {
                    throw new Error(data.error || 'An unknown error occurred.');
                }
            })
            .catch(error => {
                Loading.hide();
                console.error('Error modifying symlink paths:', error);
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    message: error.message,
                    title: 'Error',
                    autoClose: false
                });
                modifySymlinkResultsDiv.innerHTML = `<p class="error">Error: ${error.message}</p>`;
            });
        });
    }
    // --- End Symlink Path Modification Logic ---

    // --- Rclone Mount to Symlinks Logic ---
    const rcloneScanForm = document.getElementById('rclone-to-symlinks-form');
    const rcloneProgressDiv = document.getElementById('rclone-scan-progress');

    if (rcloneScanForm) {
        rcloneScanForm.addEventListener('submit', function(e) {
            e.preventDefault();
            rcloneProgressDiv.innerHTML = ''; // Clear previous progress
            const isDryRun = document.getElementById('rclone_dry_run').checked;
            const confirmMessage = isDryRun
                ? 'Start Rclone mount scan (Dry Run)? This will analyze files but make no changes.'
                : 'Start Rclone mount scan and create symlinks? This will add items to the database and create symlinks on your filesystem.';

            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: confirmMessage,
                title: 'Confirm Rclone Scan',
                onConfirm: () => {
                    Loading.show();
                    const formData = new FormData(rcloneScanForm);

                    fetch('/debug/api/rclone_to_symlinks', {
                        method: 'POST',
                        body: formData
                    })
                    .then(response => response.json())
                    .then(data => {
                        Loading.hide();
                        if (data.success && data.task_id) {
                            rcloneProgressDiv.innerHTML = `<p>Scan task started (ID: ${data.task_id}). Monitoring progress...</p>`;
                            monitorRcloneScanProgress(data.task_id, rcloneProgressDiv);
                        } else {
                            throw new Error(data.error || 'Failed to start Rclone scan task.');
                        }
                    })
                    .catch(error => {
                        Loading.hide();
                        console.error('Error starting Rclone scan:', error);
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: error.message,
                            title: 'Error'
                        });
                        rcloneProgressDiv.innerHTML = `<p class="error">Error: ${error.message}</p>`;
                    });
                }
            });
        });
    }

    function monitorRcloneScanProgress(taskId, targetDiv) {
        targetDiv.innerHTML = `<p>Connecting to progress stream for task ${taskId}...</p>`;
        const eventSource = new EventSource(`/debug/api/rclone_scan_progress/${taskId}`); // Use the correct endpoint

        eventSource.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);
                let progressHtml = `<p>Status: ${data.status || 'N/A'}</p><p>Message: ${data.message || 'N/A'}</p>`;

                // Display specific counts if available
                progressHtml += `<p>Files Scanned: ${data.total_files_scanned || 0}</p>`;
                progressHtml += `<p>Media Files Found: ${data.media_files_found || 0}</p>`;
                progressHtml += `<p>Items Processed: ${data.items_processed || 0}</p>`;
                progressHtml += `<p>DB Entries Added: ${data.items_added_to_db || 0}</p>`;
                progressHtml += `<p>Symlinks Created: ${data.symlinks_created || 0}</p>`;
                progressHtml += `<p>Skipped (Duplicates): ${data.skipped_duplicates || 0}</p>`;

                let errorsHtml = '';
                if (data.parser_errors > 0) errorsHtml += `<li>Parser Errors: ${data.parser_errors}</li>`;
                if (data.metadata_errors > 0) errorsHtml += `<li>Metadata Errors: ${data.metadata_errors}</li>`;
                if (data.db_errors > 0) errorsHtml += `<li>Database Errors: ${data.db_errors}</li>`;
                if (data.symlink_errors > 0) errorsHtml += `<li>Symlink Errors: ${data.symlink_errors}</li>`;
                if (data.errors && data.errors.length > 0) {
                     errorsHtml += `<li>General Errors: ${data.errors.length}</li>`;
                     // Optionally list first few general errors
                     // data.errors.slice(0, 3).forEach(err => { errorsHtml += `<li style="margin-left: 15px;">- ${err}</li>`; });
                }

                if (errorsHtml) {
                    progressHtml += `<p class="error">Errors Encountered:</p><ul>${errorsHtml}</ul>`;
                }

                // Display Dry Run Preview
                if (data.preview && data.preview.length > 0) {
                    progressHtml += '<h4>Dry Run Preview (First 5):</h4><ul>';
                    data.preview.forEach(item => {
                        progressHtml += `<li>${item.original_file} -> ${item.symlink_path || 'Error'} (Title: ${item.fetched_title || 'N/A'})</li>`;
                    });
                    progressHtml += '</ul>';
                }


                targetDiv.innerHTML = progressHtml;

                if (data.complete) {
                    eventSource.close();
                    console.log(`Progress stream closed for Rclone scan task ${taskId}`);
                    showPopup({
                         type: data.success ? POPUP_TYPES.SUCCESS : POPUP_TYPES.ERROR,
                         message: data.message || (data.success ? 'Scan complete.' : 'Scan failed or finished with errors.'),
                         title: data.success ? 'Scan Complete' : 'Scan Finished'
                    });
                }
            } catch (e) {
                console.error("Error parsing progress update:", e, "Raw data:", event.data);
                targetDiv.innerHTML = `<p class="error">Error receiving progress update.</p>`;
                eventSource.close(); // Close on parsing error
            }
        };

        eventSource.onerror = function(error) {
            console.error("EventSource failed:", error);
            targetDiv.innerHTML = `<p class="error">Connection to progress stream lost.</p>`;
            eventSource.close();
             showPopup({
                type: POPUP_TYPES.ERROR,
                message: 'Lost connection to the progress stream. Please check server logs.',
                title: 'Connection Error'
            });
        };
    }
    // --- End Rclone Mount to Symlinks Logic ---

    // --- Fix Zurg Symlinks Logic ---
    const scanDupeBtn = document.getElementById('scan-duplicate-symlinks-btn');
    const dupeResultsDiv = document.getElementById('duplicate-symlinks-results');
    let dupeGroupsData = [];

    function rescrapeDupeItems(itemIds) {
        if (!itemIds.length) return;
        showPopup({
            type: POPUP_TYPES.CONFIRM,
            message: `Delete ${itemIds.length} episode(s) - symlink, source file/debrid download, media server entry - and move them back to Wanted for rescraping?`,
            title: 'Confirm Delete & Rescrape',
            onConfirm: () => {
                Loading.show();
                fetch('/debug/api/rescrape_duplicate_symlinks', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ item_ids: itemIds })
                })
                .then(response => response.json())
                .then(data => {
                    Loading.hide();
                    if (data.success) {
                        showPopup({ type: POPUP_TYPES.SUCCESS, message: `Deleted and moved ${data.moved} item(s) back to Wanted.`, title: 'Rescrape Queued', autoClose: true });
                        if (scanDupeBtn) scanDupeBtn.click();
                    } else {
                        throw new Error(data.error || 'An unknown error occurred.');
                    }
                })
                .catch(error => {
                    Loading.hide();
                    showPopup({ type: POPUP_TYPES.ERROR, message: error.message, title: 'Error', autoClose: false });
                });
            }
        });
    }

    function renderDupeGroups(data) {
        dupeGroupsData = data.groups;
        let html = `<h4>Results:</h4>`;
        html += `<p>Symlinks scanned: ${data.scanned} &middot; Duplicate groups: ${data.group_count} &middot; Episodes affected: ${data.affected_count}</p>`;
        if (data.group_count === 0) {
            html += `<p>No duplicate targets found - every episode points to its own distinct file.</p>`;
            dupeResultsDiv.innerHTML = html;
            return;
        }
        html += `<button type="button" id="rescrape-all-dupe-groups-btn" style="margin-bottom:10px;">Rescrape All (${data.affected_count})</button>`;
        data.groups.forEach((group, gIdx) => {
            html += `<div class="dupe-group" style="margin-bottom:16px;border:1px solid #444;padding:10px;border-radius:6px;">`;
            if (group.is_multi_episode_release) {
                html += `<div style="display:inline-block;background:#2d5f3f;color:#9fe6b8;font-size:0.75em;padding:2px 8px;border-radius:10px;margin-bottom:6px;">Possible multi-episode release &mdash; filename appears to cover all episodes below</div>`;
            }
            html += `<div class="path-cell" style="word-break:break-all;color:#aaa;font-size:0.85em;margin-bottom:6px;">Target: ${group.target}</div>`;
            html += `<button type="button" class="rescrape-dupe-group-btn" data-group="${gIdx}" style="margin-bottom:8px;">Rescrape This Group (${group.items.length})</button>`;
            html += `<table class="preview-results-table"><thead><tr><th>Show</th><th>Season/Episode</th><th>Path</th></tr></thead><tbody>`;
            group.items.forEach(item => {
                const showLabel = `${item.title || 'Unknown'} (${item.year || ''})`;
                const se = `S${String(item.season_number).padStart(2, '0')}E${String(item.episode_number).padStart(2, '0')}`;
                html += `<tr>
                    <td class="title-cell">${showLabel}</td>
                    <td>${se}</td>
                    <td class="path-cell">${item.path}</td>
                </tr>`;
            });
            html += `</tbody></table></div>`;
        });
        dupeResultsDiv.innerHTML = html;

        document.querySelectorAll('.rescrape-dupe-group-btn').forEach(btn => {
            btn.addEventListener('click', function () {
                const gIdx = parseInt(this.dataset.group, 10);
                const ids = dupeGroupsData[gIdx].items.filter(i => i.item_id).map(i => i.item_id);
                rescrapeDupeItems(ids);
            });
        });
        const rescrapeAllBtn = document.getElementById('rescrape-all-dupe-groups-btn');
        if (rescrapeAllBtn) {
            rescrapeAllBtn.addEventListener('click', function () {
                const ids = [];
                dupeGroupsData.forEach(g => g.items.forEach(i => { if (i.item_id) ids.push(i.item_id); }));
                rescrapeDupeItems(ids);
            });
        }
    }

    if (scanDupeBtn) {
        scanDupeBtn.addEventListener('click', function () {
            Loading.show();
            dupeResultsDiv.innerHTML = '';
            fetch('/debug/api/scan_duplicate_symlinks', { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    Loading.hide();
                    if (data.success) {
                        renderDupeGroups(data);
                    } else {
                        throw new Error(data.error || 'An unknown error occurred.');
                    }
                })
                .catch(error => {
                    Loading.hide();
                    showPopup({ type: POPUP_TYPES.ERROR, message: error.message, title: 'Error', autoClose: false });
                    dupeResultsDiv.innerHTML = `<p class="error">Error: ${error.message}</p>`;
                });
        });
    }

    const fixZurgForm = document.getElementById('fix-zurg-symlinks-form');
    const fixZurgResultsDiv = document.getElementById('fix-zurg-results');

    if (fixZurgForm) {
        fixZurgForm.addEventListener('submit', function(e) {
            e.preventDefault();
            fixZurgResultsDiv.innerHTML = ''; // Clear previous results

            const isDryRun = document.getElementById('zurg_dry_run').checked;
            const confirmMessage = isDryRun
                ? 'Start Zurg symlink analysis (Dry Run)? This will check for issues but make no changes.'
                : 'Fix Zurg symlinks? This will update database entries and recreate symlinks where folder extensions have changed.';

            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: confirmMessage,
                title: 'Confirm Zurg Symlink Fix',
                onConfirm: () => {
                    Loading.show();
                    const formData = new FormData(fixZurgForm);

                    fetch('/debug/api/fix_zurg_symlinks', {
                        method: 'POST',
                        body: formData
                    })
                    .then(response => response.json())
                    .then(data => {
                        Loading.hide();
                        if (data.success) {
                            showPopup({
                                type: data.dry_run ? POPUP_TYPES.INFO : POPUP_TYPES.SUCCESS,
                                message: data.message,
                                title: data.dry_run ? 'Dry Run Results' : 'Fix Complete',
                                autoClose: false
                            });

                            let resultsHtml = `<h4>Results:</h4>`;
                            resultsHtml += `<p>Items Checked: ${data.items_checked}</p>`;
                            resultsHtml += `<p>Items Needing Fix: ${data.items_needing_fix}</p>`;
                            if (!data.dry_run) {
                                resultsHtml += `<p>Items Fixed: ${data.items_fixed}</p>`;
                            }

                            if (data.preview && data.preview.length > 0) {
                                resultsHtml += '<h4>Preview of Changes:</h4>';
                                resultsHtml += '<table class="preview-results-table"><thead><tr><th>ID</th><th>Title</th><th>Old Path</th><th>New Path</th></tr></thead><tbody>';
                                data.preview.forEach(item => {
                                    resultsHtml += `<tr>
                                        <td>${item.id}</td>
                                        <td class="title-cell">${item.title}</td>
                                        <td class="path-cell">${item.old_original_path}</td>
                                        <td class="path-cell">${item.new_original_path}</td>
                                    </tr>`;
                                });
                                resultsHtml += '</tbody></table>';
                                if (data.items_needing_fix > data.preview.length) {
                                    resultsHtml += `<p><i>(Showing first ${data.preview.length} changes)</i></p>`;
                                }
                            }

                            if (data.errors && data.errors.length > 0) {
                                resultsHtml += `<h4>Errors (${data.errors.length}):</h4><ul>`;
                                data.errors.slice(0, 10).forEach(error => {
                                    resultsHtml += `<li class="error">${error}</li>`;
                                });
                                if (data.errors.length > 10) {
                                    resultsHtml += `<li><i>... and ${data.errors.length - 10} more errors</i></li>`;
                                }
                                resultsHtml += '</ul>';
                            }

                            fixZurgResultsDiv.innerHTML = resultsHtml;
                        } else {
                            throw new Error(data.error || 'An unknown error occurred.');
                        }
                    })
                    .catch(error => {
                        Loading.hide();
                        console.error('Error fixing Zurg symlinks:', error);
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: error.message,
                            title: 'Error',
                            autoClose: false
                        });
                        fixZurgResultsDiv.innerHTML = `<p class="error">Error: ${error.message}</p>`;
                    });
                }
            });
        });
    }
    // --- End Fix Zurg Symlinks Logic ---

    // --- Episode Results Modal Functions ---
    let lastEpisodeResults = null;

    function showEpisodeResultsModal(resultsHtml, title = 'Episode Fix Results') {
        const modal = document.getElementById('episode-results-modal');
        const modalBody = document.getElementById('episode-modal-body');
        const modalTitle = document.getElementById('episode-modal-title');

        modalTitle.textContent = title;
        modalBody.innerHTML = resultsHtml;
        modal.style.display = 'flex';
        document.body.style.overflow = 'hidden'; // Prevent background scrolling
    }

    function closeEpisodeResultsModal() {
        const modal = document.getElementById('episode-results-modal');
        modal.style.display = 'none';
        document.body.style.overflow = ''; // Restore scrolling
    }

    // Close modal when clicking overlay
    document.addEventListener('click', function(e) {
        const modal = document.getElementById('episode-results-modal');
        if (e.target.classList.contains('episode-modal-overlay')) {
            closeEpisodeResultsModal();
        }
    });

    // Close modal on Escape key
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            const modal = document.getElementById('episode-results-modal');
            if (modal.style.display === 'flex') {
                closeEpisodeResultsModal();
            }
        }
    });

    // View Last Results button
    const viewResultsBtn = document.getElementById('view-episode-results-btn');
    if (viewResultsBtn) {
        viewResultsBtn.addEventListener('click', function() {
            if (lastEpisodeResults) {
                showEpisodeResultsModal(lastEpisodeResults.html, lastEpisodeResults.title);
            }
        });
    }

    // Close button event listener
    const closeBtn = document.getElementById('episode-modal-close-btn');
    if (closeBtn) {
        closeBtn.addEventListener('click', closeEpisodeResultsModal);
    }
    // --- End Episode Results Modal Functions ---

    // --- Cleanup Results Modal Functions ---
    let lastCleanupResults = null;

    window.showCleanupResultsModal = function showCleanupResultsModal(resultsHtml, title = 'Cleanup Results') {
        const modal = document.getElementById('cleanup-results-modal');
        const modalBody = document.getElementById('cleanup-modal-body');
        const modalTitle = document.getElementById('cleanup-modal-title');

        modalTitle.textContent = title;
        modalBody.innerHTML = resultsHtml;
        modal.style.display = 'flex';
        document.body.style.overflow = 'hidden'; // Prevent background scrolling
    }

    window.closeCleanupResultsModal = function closeCleanupResultsModal() {
        const modal = document.getElementById('cleanup-results-modal');
        modal.style.display = 'none';
        document.body.style.overflow = ''; // Restore scrolling
    }

    // Close modal when clicking overlay
    document.addEventListener('click', function(e) {
        const modal = document.getElementById('cleanup-results-modal');
        if (e.target.classList.contains('episode-modal-overlay') && modal.style.display === 'flex') {
            closeCleanupResultsModal();
        }
    });

    // Close modal on Escape key
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            const modal = document.getElementById('cleanup-results-modal');
            if (modal.style.display === 'flex') {
                closeCleanupResultsModal();
            }
        }
    });

    // Close button event listener
    const cleanupCloseBtn = document.getElementById('cleanup-modal-close-btn');
    if (cleanupCloseBtn) {
        cleanupCloseBtn.addEventListener('click', closeCleanupResultsModal);
    }
    // --- End Cleanup Results Modal Functions ---

    // --- Fix Episode Numbers Logic ---
    const fixEpisodeForm = document.getElementById('fix-episode-numbers-form');

    if (fixEpisodeForm) {
        fixEpisodeForm.addEventListener('submit', function(e) {
            e.preventDefault();

            const isDryRun = document.getElementById('episode_dry_run').checked;
            const showDuplicates = document.getElementById('show_duplicates').checked;
            const removeDuplicates = document.getElementById('remove_duplicates').checked;

            let confirmMessage = isDryRun
                ? 'Start episode number analysis (Dry Run)? This will scan for incorrect episode numbers but make no changes.'
                : 'Fix episode numbers? This will update database entries where episode numbers do not match filenames.';

            if (removeDuplicates && !isDryRun) {
                confirmMessage += '\n\nWARNING: This will also DELETE bug-created duplicate entries (same torrent, wrong episode numbers).';
            }

            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: confirmMessage,
                title: 'Confirm Episode Number Fix',
                onConfirm: () => {
                    // Show progress bar and start timer
                    const progressDiv = document.getElementById('episode-fix-progress');
                    const elapsedTimeSpan = document.getElementById('episode-elapsed-time');
                    const progressMessage = document.getElementById('episode-progress-message');

                    progressDiv.style.display = 'block';
                    progressMessage.textContent = isDryRun ? 'Analyzing episodes...' : 'Fixing episodes...';

                    let startTime = Date.now();
                    let timerInterval = setInterval(() => {
                        const elapsedSeconds = Math.floor((Date.now() - startTime) / 1000);
                        const elapsedMinutes = Math.floor(elapsedSeconds / 60);
                        const remainingSeconds = elapsedSeconds % 60;
                        elapsedTimeSpan.textContent = `Elapsed: ${elapsedMinutes}m ${remainingSeconds}s`;
                    }, 1000);

                    const formData = new FormData(fixEpisodeForm);

                    fetch('/debug/api/fix_episode_numbers', {
                        method: 'POST',
                        body: formData
                    })
                    .then(response => response.json())
                    .then(data => {
                        clearInterval(timerInterval);
                        progressDiv.style.display = 'none';
                        if (data.success) {
                            let popupMessage = '';
                            if (data.dry_run) {
                                popupMessage = `Found ${data.mismatches_found} episodes with incorrect episode numbers`;
                                if (data.torrent_duplicates_found > 0) {
                                    popupMessage += ` and ${data.torrent_duplicates_found} torrent ID duplicates`;
                                }
                            } else {
                                popupMessage = `Fixed ${data.fixed_count} episodes`;
                                if (data.deleted_count > 0) {
                                    popupMessage += ` and deleted ${data.deleted_count} bug-created duplicates`;
                                }
                            }

                            // Build results HTML for modal
                            let resultsHtml = '<div class="episode-results-summary">';
                            resultsHtml += '<h4>Summary</h4>';
                            resultsHtml += `<p><strong>Episodes with Incorrect Numbers:</strong> ${data.mismatches_found}</p>`;
                            if (!data.dry_run) {
                                resultsHtml += `<p><strong>Episodes Fixed:</strong> ${data.fixed_count}</p>`;
                            }
                            if (data.deleted_count && data.deleted_count > 0) {
                                resultsHtml += `<p><strong>Bug-Created Duplicates Deleted:</strong> ${data.deleted_count}</p>`;
                            }
                            if (data.torrent_duplicates_found) {
                                resultsHtml += `<p><strong>Torrent ID Duplicates Found:</strong> ${data.torrent_duplicates_found}</p>`;
                            }
                            resultsHtml += '</div>';

                            if (data.mismatches && data.mismatches.length > 0) {
                                resultsHtml += '<h4>Mismatched Episodes:</h4>';
                                resultsHtml += '<table class="preview-results-table"><thead><tr><th>ID</th><th>Title</th><th>Filename</th><th>DB</th><th>Filename Says</th><th>Torrent ID</th></tr></thead><tbody>';
                                data.mismatches.forEach(item => {
                                    resultsHtml += `<tr>
                                        <td>${item.id}</td>
                                        <td class="title-cell">${item.title || 'N/A'}</td>
                                        <td class="path-cell">${item.filename}</td>
                                        <td>S${item.db_season}E${item.db_episode}</td>
                                        <td>S${item.parsed_season}E${item.parsed_episode}</td>
                                        <td>${item.filled_by_torrent_id || 'N/A'}</td>
                                    </tr>`;
                                });
                                resultsHtml += '</tbody></table>';
                                if (data.mismatches_found > data.mismatches.length) {
                                    resultsHtml += `<p><i>(Showing first ${data.mismatches.length} mismatches)</i></p>`;
                                }
                            }

                            if (showDuplicates && data.duplicates && data.duplicates.length > 0) {
                                resultsHtml += '<h4>Duplicate Episodes:</h4>';
                                resultsHtml += '<table class="preview-results-table"><thead><tr><th>TMDB ID</th><th>Season/Episode</th><th>Count</th><th>IDs</th><th>Torrent IDs</th><th>Filenames</th></tr></thead><tbody>';
                                data.duplicates.forEach(dup => {
                                    resultsHtml += `<tr>
                                        <td>${dup.tmdb_id}</td>
                                        <td>S${dup.season_number}E${dup.episode_number}</td>
                                        <td>${dup.count}</td>
                                        <td class="path-cell">${dup.ids}</td>
                                        <td class="path-cell">${dup.torrent_ids || 'N/A'}</td>
                                        <td class="path-cell">${dup.files || 'N/A'}</td>
                                    </tr>`;
                                });
                                resultsHtml += '</tbody></table>';
                                if (data.duplicates_found > data.duplicates.length) {
                                    resultsHtml += `<p><i>(Showing first ${data.duplicates.length} duplicates)</i></p>`;
                                }
                            } else if (showDuplicates) {
                                resultsHtml += '<p>No duplicate episodes found.</p>';
                            }

                            // Display torrent ID duplicates
                            if (data.torrent_duplicates && data.torrent_duplicates.length > 0) {
                                resultsHtml += '<h4>Bug-Created Duplicates (Same Torrent ID):</h4>';
                                resultsHtml += '<p class="info-text">These episodes have the same torrent ID but wrong episode numbers due to the bug.</p>';
                                resultsHtml += '<table class="preview-results-table"><thead><tr><th>Torrent ID</th><th>TMDB</th><th>Show/Season</th><th>Correct Episodes</th><th>Wrong Episodes</th><th>Status</th></tr></thead><tbody>';
                                data.torrent_duplicates.forEach(dup => {
                                    const correctEps = dup.correct_ids.length > 0 ? `${dup.correct_ids.length} episodes (IDs: ${dup.correct_ids.join(', ')})` : 'None found';
                                    const wrongEps = dup.wrong_ids.length > 0 ? `${dup.wrong_ids.length} episodes (IDs: ${dup.wrong_ids.join(', ')})` : 'None found';
                                    const status = data.deleted_count > 0 ? '<span style="color:#22c55e;font-weight:600;">✓ Deleted</span>' : '<span style="color:#f59e0b;font-weight:600;">Found</span>';

                                    resultsHtml += `<tr>
                                        <td>${dup.torrent_id}</td>
                                        <td>${dup.tmdb_id}</td>
                                        <td>${dup.episodes_info}</td>
                                        <td class="path-cell">${correctEps}</td>
                                        <td class="path-cell">${wrongEps}</td>
                                        <td>${status}</td>
                                    </tr>`;
                                });
                                resultsHtml += '</tbody></table>';
                            } else if (data.torrent_duplicates_found === 0) {
                                resultsHtml += '<p>No torrent ID duplicates found.</p>';
                            }

                            // Store results for "View Last Results" button
                            const modalTitle = data.dry_run ? 'Episode Analysis Results (Dry Run)' : 'Episode Fix Results';
                            lastEpisodeResults = {
                                html: resultsHtml,
                                title: modalTitle
                            };

                            // Show modal with results
                            showEpisodeResultsModal(resultsHtml, modalTitle);

                            // Show "View Last Results" button
                            document.getElementById('view-episode-results-btn').style.display = 'inline-block';

                            // Also show success popup
                            showPopup({
                                type: data.dry_run ? POPUP_TYPES.INFO : POPUP_TYPES.SUCCESS,
                                message: popupMessage,
                                title: data.dry_run ? 'Analysis Complete' : 'Fix Complete',
                                autoClose: true
                            });
                        } else {
                            showPopup({
                                type: POPUP_TYPES.ERROR,
                                message: data.error || 'An unknown error occurred',
                                title: 'Error'
                            });
                        }
                    })
                    .catch(error => {
                        clearInterval(timerInterval);
                        progressDiv.style.display = 'none';
                        console.error('Error:', error);
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: 'Network error occurred. Check console for details.',
                            title: 'Error'
                        });
                    });
                }
            });
        });
    }
    // --- End Fix Episode Numbers Logic ---

    // --- Clean Up Failed Upgrade Attempts Logic ---
    {
        const upgradesForm = document.getElementById('cleanup-upgrades-form');
        const upgradesProgress = document.getElementById('upgrades-progress');
        const upgradesProgressBar = document.getElementById('upgrades-progress-bar');
        const upgradesProgressMessage = document.getElementById('upgrades-progress-message');
        const upgradesElapsedTime = document.getElementById('upgrades-elapsed-time');
        const viewUpgradesResultsBtn = document.getElementById('view-upgrades-results-btn');
        let upgradesResults = {};

        if (upgradesForm) {
            upgradesForm.addEventListener('submit', function(e) {
                e.preventDefault();

                const dryRunChecked = document.getElementById('upgrades_dry_run').checked;
                // Get values from dropdown selects
                const versionMatch = document.getElementById('version_match').value;
                const keepAction = document.getElementById('keep_action').value;

                const mediaType = document.getElementById('upgrades_media_type').value;

                // Show progress UI
                upgradesProgress.style.display = 'block';
                upgradesProgressBar.style.width = '0%';
                upgradesProgressMessage.textContent = 'Scanning for duplicates...';

                // Helper function to format seconds to human-readable time
                function formatTime(seconds) {
                    const hours = Math.floor(seconds / 3600);
                    const minutes = Math.floor((seconds % 3600) / 60);
                    const secs = seconds % 60;

                    if (hours > 0) {
                        return `${hours}h ${minutes}m ${secs}s`;
                    } else if (minutes > 0) {
                        return `${minutes}m ${secs}s`;
                    } else {
                        return `${secs}s`;
                    }
                }

                // Start the timer (will be cleared when SSE starts)
                const startTime = Date.now();
                let timerInterval = setInterval(() => {
                    const elapsed = Math.floor((Date.now() - startTime) / 1000);
                    upgradesElapsedTime.textContent = `Elapsed: ${formatTime(elapsed)}`;
                }, 1000);

                // Animate progress bar to 30% immediately
                setTimeout(() => {
                    upgradesProgressBar.style.width = '30%';
                }, 100);

                // Prepare form data
                const formData = new FormData();
                if (dryRunChecked) {
                    formData.append('dry_run', 'on');
                }
                formData.append('version_match', versionMatch);
                formData.append('keep_action', keepAction);
                formData.append('media_type', mediaType);

                // Add exclude patterns if provided
                const excludePatterns = document.getElementById('exclude_patterns').value.trim();
                if (excludePatterns) {
                    formData.append('exclude_patterns', excludePatterns);
                }

                // Add NAS filter if the element exists (only rendered when nas_paths configured)
                const nasFilterEl = document.getElementById('upgrades_nas_filter');
                if (nasFilterEl) {
                    formData.append('nas_filter', nasFilterEl.value);
                }

                // Make the request
                let eventSource = null;

                fetch('/debug/api/cleanup_failed_upgrades', {
                    method: 'POST',
                    body: formData
                })
                .then(response => response.json())
                .then(data => {
                    // If we got a session_id, connect to SSE for real-time progress
                    if (data.session_id && !dryRunChecked) {
                        // Clear the local timer since SSE will provide better updates
                        clearInterval(timerInterval);
                        timerInterval = null;

                        eventSource = new EventSource(`/debug/api/cleanup_progress/${data.session_id}`);

                        eventSource.onmessage = (event) => {
                            const progress = JSON.parse(event.data);

                            if (progress.status === 'running') {
                                // Update progress bar
                                const percentage = (progress.items_processed / progress.total_items) * 100;
                                upgradesProgressBar.style.width = `${percentage}%`;

                                // Update message with phase
                                const phaseText = progress.phase === 'plex_deletion' ? 'Deleting from Plex' : 'Processing';
                                upgradesProgressMessage.textContent = `${phaseText}: ${progress.items_processed}/${progress.total_items} items`;

                                // Update elapsed and remaining time (human-readable)
                                const elapsedText = formatTime(progress.elapsed);
                                const remainingText = formatTime(progress.eta);
                                upgradesElapsedTime.textContent = `Elapsed: ${elapsedText} | Remaining: ~${remainingText}`;
                            }

                            if (progress.status === 'complete' || progress.status === 'error') {
                                eventSource.close();
                            }
                        };

                        eventSource.onerror = () => {
                            eventSource.close();
                        };
                    } else {
                        // No SSE (dry run or error), keep local timer running
                        // Timer will be cleared on completion
                    }

                    if (data.success) {
                        // Update progress to 100%
                        upgradesProgressBar.style.width = '100%';
                        upgradesProgressMessage.textContent = data.message;

                        // Stop timer if still running
                        if (timerInterval) {
                            clearInterval(timerInterval);
                            timerInterval = null;
                        }
                        const finalTime = Math.floor((Date.now() - startTime) / 1000);
                        upgradesElapsedTime.textContent = `Completed in ${formatTime(finalTime)}`;

                        // Build results HTML for modal with pagination
                        const groupsPerPage = 20;
                        let currentPage = 1;
                        let showExcluded = true; // Track toggle state

                        function renderFailedUpgradesPage(page, forceShowExcluded) {
                            // Update showExcluded if explicitly passed
                            if (forceShowExcluded !== undefined) {
                                showExcluded = forceShowExcluded;
                            }

                            // Filter groups based on showExcluded setting
                            let displayGroups = data.groups || [];
                            if (!showExcluded) {
                                // Only show groups that have at least one non-excluded item
                                displayGroups = displayGroups.filter(group => {
                                    return group.delete_items && group.delete_items.some(item => item.action !== 'excluded');
                                });
                            }

                            const totalGroups = displayGroups.length;
                            const totalPages = Math.ceil(totalGroups / groupsPerPage);

                            // Adjust page if out of bounds
                            if (page > totalPages && totalPages > 0) {
                                page = totalPages;
                            }

                            let resultsHtml = '<div class="episode-results-summary">';
                            const mediaLabel = mediaType === 'show' ? 'shows' : 'movies';

                            // Compact horizontal summary section
                            const statLabel = `color:#aaa;font-size:12px;`;
                            const statVal = `color:#e0e0e0;font-weight:600;font-size:12px;`;
                            const divider = `<div style="width:1px;height:16px;background:#333;"></div>`;
                            resultsHtml += `<div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;padding:10px 14px;background:#222;border:1px solid #333;border-radius:6px;margin-bottom:14px;">`;

                            resultsHtml += `<span style="${statVal}">${data.total_movies} Groups Found</span>${divider}`;
                            resultsHtml += `<span style="${statLabel}">${data.dry_run ? 'Would Delete' : 'Deleted'}:</span><span style="${statVal}">${data.total_deleted}</span>`;

                            if (data.total_excluded && data.total_excluded > 0) {
                                resultsHtml += `${divider}<span style="${statLabel}">Excluded:</span><span style="color:#22c55e;font-weight:600;font-size:12px;">${data.total_excluded}</span>`;
                            }

                            resultsHtml += `${divider}<span style="${statLabel}">Keep:</span><span style="${statVal}">${data.total_movies}</span>${divider}`;

                            let actionText = 'Keep collected, delete blacklisted';
                            if (data.keep_action === 'keep_blacklisted') {
                                actionText = 'Keep blacklisted, delete collected';
                            } else if (data.keep_action === 'keep_collected_delete_ghostlisted') {
                                actionText = 'Keep collected, delete ghostlisted';
                            } else if (data.keep_action === 'keep_ghostlisted_delete_collected') {
                                actionText = 'Keep ghostlisted, delete collected';
                            } else if (data.keep_action === 'keep_best_quality') {
                                actionText = 'Keep best quality';
                            } else if (data.keep_action === 'keep_collected_delete_stale') {
                                actionText = 'Keep collected, delete stale states';
                            }
                            resultsHtml += `<span style="${statLabel}">Action:</span><span style="${statVal}">${actionText}</span>${divider}`;
                            resultsHtml += `<span style="${statLabel}">Version:</span><span style="${statVal}">${data.version_match === 'all' ? 'Any' : 'Same only'}</span>${divider}`;
                            resultsHtml += `<span style="${statLabel}">Time:</span><span style="${statVal}">${data.elapsed_time}s</span>`;

                            resultsHtml += `</div>`; // Close horizontal stats

                            // Function to generate pagination controls HTML
                            function getPaginationHTML(page, totalPages) {
                                if (totalPages <= 1) return '';

                                const btnStyle = `padding:6px 14px;background:#2a2a2a;border:1px solid #3d3d3d;color:#e0e0e0;border-radius:5px;cursor:pointer;font-size:12px;font-family:inherit`;
                                const btnDisabledStyle = `padding:6px 14px;background:#1a1a1a;border:1px solid #2a2a2a;color:#555;border-radius:5px;cursor:not-allowed;font-size:12px;font-family:inherit`;
                                let paginationHtml = `<div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:10px;margin:15px 0;padding:10px;background:#222;border:1px solid #2a2a2a;border-radius:6px;">`;

                                // Previous button
                                paginationHtml += `<button onclick="renderFailedUpgradesPage(${Math.max(1, page - 1)})" style="${page === 1 ? btnDisabledStyle : btnStyle}" ${page === 1 ? 'disabled' : ''}>Previous</button>`;

                                // Page info and go to page input
                                paginationHtml += `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:center;">`;
                                paginationHtml += `<span style="color:#aaa;font-size:12px;">Page ${page} of ${totalPages}</span>`;
                                paginationHtml += `<span style="color:#444;">|</span>`;
                                paginationHtml += `<label style="display:flex;align-items:center;gap:5px;">`;
                                paginationHtml += `<span style="white-space:nowrap;font-size:12px;color:#aaa;">Go to:</span>`;
                                paginationHtml += `<input type="number" id="gotoPageInput_${page}" min="1" max="${totalPages}" placeholder="${page}" style="width:54px;padding:5px 8px;text-align:center;background:#2a2a2a;border:1px solid #3d3d3d;color:#e0e0e0;border-radius:5px;font-size:12px;">`;
                                paginationHtml += `<button onclick="const input = document.getElementById('gotoPageInput_${page}'); const pageNum = parseInt(input.value); if (pageNum >= 1 && pageNum <= ${totalPages}) renderFailedUpgradesPage(pageNum);" style="${btnStyle}">Go</button>`;
                                paginationHtml += `</label>`;
                                paginationHtml += `</div>`;

                                // Next button
                                paginationHtml += `<button onclick="renderFailedUpgradesPage(${Math.min(totalPages, page + 1)})" style="${page === totalPages ? btnDisabledStyle : btnStyle}" ${page === totalPages ? 'disabled' : ''}>Next</button>`;

                                paginationHtml += `</div>`;
                                return paginationHtml;
                            }

                            if (displayGroups && displayGroups.length > 0) {
                                const startIdx = (page - 1) * groupsPerPage;
                                const endIdx = Math.min(startIdx + groupsPerPage, totalGroups);
                                const pageGroups = displayGroups.slice(startIdx, endIdx);

                                const displaySuffix = !showExcluded ? ' (excluding fully-excluded groups)' : '';

                                // Container for heading and checkbox
                                resultsHtml += `<div style="display: flex; align-items: flex-start; justify-content: space-between; gap: 15px; flex-wrap: wrap; margin-bottom: 10px;">`;

                                // Left side: Heading and description
                                resultsHtml += `<div style="flex:1 1 auto;min-width:260px;">`;
                                resultsHtml += `<div style="font-size:12px;font-weight:700;color:#e8601c;text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px;">Duplicate Groups (showing ${startIdx + 1}-${endIdx} of ${totalGroups}${displaySuffix}):</div>`;
                                resultsHtml += `<div style="font-size:11px;color:#666;font-style:italic;">Each row shows one duplicate group with items to keep (✓) and items to delete (✗).</div>`;
                                resultsHtml += `</div>`;

                                if (data.total_excluded && data.total_excluded > 0) {
                                    resultsHtml += `<label style="flex:0 0 auto;display:flex;align-items:center;gap:7px;cursor:pointer;user-select:none;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.2);padding:5px 11px;border-radius:5px;align-self:center;">`;
                                    const checkedAttr = showExcluded ? 'checked' : '';
                                    resultsHtml += `<input type="checkbox" id="toggleExcludedItems" ${checkedAttr} style="cursor:pointer;">`;
                                    resultsHtml += `<span style="font-size:12px;color:#22c55e;font-weight:500;white-space:nowrap;">Show Excluded (${data.total_excluded})</span>`;
                                    resultsHtml += `</label>`;
                                }

                                resultsHtml += `</div>`; // Close heading container

                                // Add pagination at top
                                resultsHtml += getPaginationHTML(page, totalPages);

                                resultsHtml += `<table style="width:100%;margin-top:10px;border-collapse:collapse;font-size:12px;">`;
                                resultsHtml += `<thead><tr><th style="padding:8px 10px;text-align:left;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:#aaa;border-bottom:1px solid #333;background:#222;">Group #</th><th style="padding:8px 10px;text-align:left;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:#22c55e;border-bottom:1px solid #333;background:#222;">Keep (✓)</th><th style="padding:8px 10px;text-align:left;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:#ef4444;border-bottom:1px solid #333;background:#222;">Delete (✗)</th></tr></thead><tbody>`;

                                pageGroups.forEach((group, idx) => {
                                    const groupNum = startIdx + idx + 1;

                                    resultsHtml += `<tr style="border-bottom:1px solid #2a2a2a;">`;
                                    resultsHtml += `<td style="padding:8px 10px;vertical-align:top;color:#aaa;font-weight:600;">${groupNum}</td>`;

                                    // Keep items column
                                    resultsHtml += `<td style="padding:8px 10px;border-left:2px solid rgba(34,197,94,.4);background:rgba(34,197,94,.04);vertical-align:top;">`;
                                    group.keep_items.forEach(item => {
                                        const episode = item.season_number ? ` S${item.season_number}E${item.episode_number}` : '';
                                        const scoreDisplay = item.current_score !== undefined && item.current_score !== 'N/A' ? ` | Score: ${typeof item.current_score === 'number' ? item.current_score.toFixed(1) : item.current_score}` : '';
                                        const ghostlistedStatus = item.ghostlisted !== undefined ? (item.ghostlisted ? 'True' : 'False') : 'N/A';
                                        const fileLocation = item.location && item.location !== 'N/A' ? item.location : 'No file location';
                                        resultsHtml += `<div style="margin-bottom:8px;"><span style="font-weight:600;color:#e0e0e0;">${item.title}${episode}</span><br>`;
                                        resultsHtml += `<span style="color:#aaa;font-size:11px;">ID: ${item.id} | State: ${item.state} | Version: ${item.version} | Ghostlisted: ${ghostlistedStatus}${scoreDisplay}</span><br>`;
                                        resultsHtml += `<span style="color:#666;font-size:11px;font-family:monospace;word-break:break-all;">File: ${fileLocation}</span></div>`;
                                    });
                                    resultsHtml += `</td>`;

                                    // Delete items column
                                    resultsHtml += `<td style="padding:8px 10px;border-left:2px solid rgba(239,68,68,.4);background:rgba(239,68,68,.04);vertical-align:top;">`;
                                    group.delete_items.forEach(item => {
                                        const episode = item.season_number ? ` S${item.season_number}E${item.episode_number}` : '';
                                        const scoreDisplay = item.current_score !== undefined && item.current_score !== 'N/A' ? ` | Score: ${typeof item.current_score === 'number' ? item.current_score.toFixed(1) : item.current_score}` : '';
                                        const ghostlistedStatus = item.ghostlisted !== undefined ? (item.ghostlisted ? 'True' : 'False') : 'N/A';
                                        const fileLocation = item.location && item.location !== 'N/A' ? item.location : 'No file location';

                                        const isExcluded = item.action === 'excluded';
                                        const itemStyle = isExcluded ? 'background:rgba(34,197,94,.08);padding:7px 9px;border-radius:4px;border-left:2px solid rgba(34,197,94,.4);' : '';
                                        const statusText = isExcluded ? `🛡️ Excluded from deletion (pattern: ${item.excluded_pattern})` : '';
                                        const excludedClass = isExcluded ? ' excluded-item' : '';

                                        resultsHtml += `<div class="duplicate-item${excludedClass}" style="margin-bottom:8px;${itemStyle}">`;
                                        if (isExcluded) {
                                            resultsHtml += `<div style="color:#22c55e;font-weight:600;margin-bottom:4px;font-size:11px;">${statusText}</div>`;
                                        }
                                        resultsHtml += `<span style="font-weight:600;color:#e0e0e0;">${item.title}${episode}</span><br>`;
                                        resultsHtml += `<span style="color:#aaa;font-size:11px;">ID: ${item.id} | State: ${item.state} | Version: ${item.version} | Ghostlisted: ${ghostlistedStatus}${scoreDisplay}</span><br>`;
                                        resultsHtml += `<span style="color:#666;font-size:11px;font-family:monospace;word-break:break-all;">File: ${fileLocation}</span></div>`;
                                    });
                                    resultsHtml += `</td>`;
                                    resultsHtml += `</tr>`;
                                });
                                resultsHtml += `</tbody></table>`;

                                // Add pagination at bottom
                                resultsHtml += getPaginationHTML(page, totalPages);
                            }
                            resultsHtml += '</div>';

                            const modalBody = document.getElementById('cleanup-modal-body');
                            modalBody.innerHTML = resultsHtml;

                            // Add event listener for excluded items toggle
                            const toggleCheckbox = document.getElementById('toggleExcludedItems');
                            if (toggleCheckbox) {
                                toggleCheckbox.addEventListener('change', function() {
                                    // Re-render the page with new filter setting (go to page 1)
                                    renderFailedUpgradesPage(1, this.checked);
                                });
                            }

                            // Apply initial visibility based on showExcluded state
                            if (!showExcluded) {
                                // Hide individual excluded items in mixed rows
                                const excludedItems = document.querySelectorAll('.excluded-item');
                                excludedItems.forEach(item => {
                                    item.style.display = 'none';
                                });
                            }
                        }

                        // Make function global for pagination buttons
                        window.renderFailedUpgradesPage = renderFailedUpgradesPage;

                        const dryRunChecked = document.getElementById('upgrades_dry_run').checked;
                        const modalTitle = dryRunChecked ? 'Duplicate Version Cleanup Analysis (Dry Run)' : 'Duplicate Version Cleanup Results';

                        // Show modal first
                        showCleanupResultsModal('', modalTitle);

                        // Then render the content with pagination
                        renderFailedUpgradesPage(currentPage);

                        // Save data and render function for view results button
                        window.lastFailedUpgradesResults = {
                            data: data,
                            mediaType: mediaType,
                            title: modalTitle,
                            renderFunction: renderFailedUpgradesPage
                        };

                        // Show view results button for later viewing
                        if (viewUpgradesResultsBtn) {
                            viewUpgradesResultsBtn.style.display = 'inline-block';
                        }

                        // Hide progress after 3 seconds
                        setTimeout(() => {
                            upgradesProgress.style.display = 'none';
                        }, 3000);
                    } else {
                        clearInterval(timerInterval);
                        upgradesProgressBar.style.width = '0%';
                        upgradesProgress.style.display = 'none';

                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: data.error || 'An unknown error occurred',
                            title: 'Cleanup Failed'
                        });
                    }
                })
                .catch(error => {
                    clearInterval(timerInterval);
                    upgradesProgressBar.style.width = '0%';
                    upgradesProgress.style.display = 'none';

                    showPopup({
                        type: POPUP_TYPES.ERROR,
                        message: 'Failed to cleanup failed upgrades: ' + error.message,
                        title: 'Error'
                    });
                });
            });
        }

        // View results button handler
        if (viewUpgradesResultsBtn) {
            viewUpgradesResultsBtn.addEventListener('click', function() {
                if (window.lastFailedUpgradesResults && window.lastFailedUpgradesResults.renderFunction) {
                    showCleanupResultsModal('', window.lastFailedUpgradesResults.title);
                    window.lastFailedUpgradesResults.renderFunction(1);  // Re-render from page 1
                }
            });
        }
    }
    // --- End Clean Up Failed Upgrade Attempts Logic ---

    // --- Handler for Delete Battery DB Form ---
    const deleteBatteryDbForm = document.getElementById('delete-battery-db-form');
    if (deleteBatteryDbForm) {
        deleteBatteryDbForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const url = this.action;
            const method = this.method;

            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: 'Are you sure you want to delete the battery database files (cli_battery.db, -shm, -wal)? This might resolve certain issues but will reset some internal states.',
                title: 'Confirm Deletion',
                onConfirm: () => {
                    Loading.show();
                    submitForm(url, method, new FormData(this)); // Use the generic submitForm
                }
            });
        });
    }
    // --- End Handler for Delete Battery DB Form ---

    // --- Resync Symlinks with New Settings Logic ---
    const resyncSymlinksForm = document.getElementById('resync-symlinks-form');
    if (resyncSymlinksForm) {
        resyncSymlinksForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const oldOriginalPath = this.elements['old_original_path'].value;
            const newOriginalPath = this.elements['new_original_path'].value;

            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: `Are you sure you want to resync symlinks with new settings? This will update the stored original file paths in the database for matching items. Then, it regenerates symlinks for relevant collected items based on current symlink settings and these (potentially updated) original paths. If no paths are provided, it attempts to resync all collected items.`,
                title: 'Confirm Resync',
                onConfirm: () => {
                    Loading.show();
                    submitForm(this.action, 'POST', new FormData(this));
                }
            });
        });
    }
    // --- End Resync Symlinks with New Settings Logic ---

    // --- Remove Duplicate Database Items Logic ---
    const removeDuplicatesForm = document.getElementById('remove-duplicates-form');
    const removeDuplicatesResultsDiv = document.getElementById('remove-duplicates-results');

    if (removeDuplicatesForm) {
        removeDuplicatesForm.addEventListener('submit', function(e) {
            e.preventDefault();
            removeDuplicatesResultsDiv.innerHTML = ''; // Clear previous results

            const isDryRun = document.getElementById('dry_run_duplicates').checked;
            const confirmMessage = isDryRun
                ? 'Start duplicate analysis (Dry Run)? This will identify items with the same filename but won\'t delete anything.'
                : 'Remove duplicate database items based on the filled_by_file field? This will identify items with the same filename and remove the duplicates, keeping only one entry per unique file.';

            showPopup({
                type: POPUP_TYPES.CONFIRM,
                message: confirmMessage,
                title: 'Confirm Duplicate Removal',
                onConfirm: () => {
                    Loading.show();
                    const formData = new FormData(removeDuplicatesForm);

                    fetch('/debug/api/remove_duplicate_items', {
                        method: 'POST',
                        body: formData
                    })
                    .then(response => response.json())
                    .then(data => {
                        Loading.hide();
                        if (data.success) {
                            // Build results HTML for modal with pagination
                            const itemsPerPage = 50;
                            let currentPage = 1;
                            const totalGroups = data.preview ? data.preview.length : 0;
                            const totalPages = Math.ceil(totalGroups / itemsPerPage);

                            function renderDuplicatesPage(page) {
                                let resultsHtml = '<div class="episode-results-summary">';

                                // Improved summary
                                resultsHtml += `<p><strong>${data.duplicate_groups} Duplicate Groups Found</strong> (files with multiple database entries)</p>`;
                                resultsHtml += `<p><strong>Total Duplicate Items:</strong> ${data.total_duplicates}</p>`;
                                resultsHtml += `<p><strong>${data.dry_run ? 'Would Keep' : 'Kept'}:</strong> ${data.duplicate_groups} items (oldest per file)</p>`;
                                if (!data.dry_run) {
                                    resultsHtml += `<p><strong>Items Deleted:</strong> ${data.items_deleted}</p>`;
                                }

                                if (data.preview && data.preview.length > 0) {
                                    const startIdx = (page - 1) * itemsPerPage;
                                    const endIdx = Math.min(startIdx + itemsPerPage, totalGroups);
                                    const pageGroups = data.preview.slice(startIdx, endIdx);

                                    resultsHtml += `<h4>Duplicate Groups (showing ${startIdx + 1}-${endIdx} of ${totalGroups}):</h4>`;
                                    const thS = `padding:8px 10px;text-align:left;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid #333;background:#222;`;
                                    resultsHtml += `<table style="width:100%;border-collapse:collapse;margin-top:10px;font-size:12px;"><thead><tr><th style="${thS}color:#aaa;">File</th><th style="${thS}color:#aaa;text-align:center;">Count</th><th style="${thS}color:#22c55e;">Keep Item</th><th style="${thS}color:#ef4444;">Delete Items</th></tr></thead><tbody>`;
                                    pageGroups.forEach(group => {
                                        const keepGhostlisted = group.keep_item.ghostlisted ? 'True' : 'False';
                                        const keepTorrentId = group.keep_item.torrent_id || 'N/A';
                                        resultsHtml += `<tr>
                                            <td style="border-bottom:1px solid #2a2a2a;padding:8px 10px;word-break:break-all;max-width:200px;font-family:monospace;font-size:11px;color:#888;">${group.filled_by_file}</td>
                                            <td style="border-bottom:1px solid #2a2a2a;padding:8px 10px;text-align:center;color:#e8601c;font-weight:700;">${group.count}</td>
                                            <td style="border-bottom:1px solid #2a2a2a;padding:8px 10px;border-left:2px solid rgba(34,197,94,.4);background:rgba(34,197,94,.04);"><span style="font-weight:600;color:#e0e0e0;">${group.keep_item.title}</span><br><span style="color:#aaa;font-size:11px;">ID: ${group.keep_item.id} | State: ${group.keep_item.state} | Ghostlisted: ${keepGhostlisted}<br>Torrent ID: ${keepTorrentId}</span></td>
                                            <td style="border-bottom:1px solid #2a2a2a;padding:8px 10px;border-left:2px solid rgba(239,68,68,.4);background:rgba(239,68,68,.04);">${group.delete_items.map(item => {
                                                const ghostlisted = item.ghostlisted ? 'True' : 'False';
                                                const torrentId = item.torrent_id || 'N/A';
                                                return `<div style="margin-bottom:6px;"><span style="font-weight:600;color:#e0e0e0;">${item.title}</span><br><span style="color:#aaa;font-size:11px;">ID: ${item.id} | State: ${item.state} | Ghostlisted: ${ghostlisted}<br>Torrent ID: ${torrentId}</span></div>`;
                                            }).join('')}</td>
                                        </tr>`;
                                    });
                                    resultsHtml += '</tbody></table>';

                                    // Pagination controls
                                    if (totalPages > 1) {
                                        const pb = `padding:6px 14px;background:#2a2a2a;border:1px solid #3d3d3d;color:#e0e0e0;border-radius:5px;cursor:pointer;font-size:12px;font-family:inherit`;
                                        const pbd = `padding:6px 14px;background:#1a1a1a;border:1px solid #2a2a2a;color:#555;border-radius:5px;cursor:not-allowed;font-size:12px;font-family:inherit`;
                                        resultsHtml += `<div style="display:flex;align-items:center;justify-content:center;gap:10px;margin-top:14px;padding:10px;background:#222;border:1px solid #2a2a2a;border-radius:6px;">`;
                                        resultsHtml += `<button onclick="renderDuplicatesPage(${Math.max(1, page - 1)})" style="${page === 1 ? pbd : pb}" ${page === 1 ? 'disabled' : ''}>Previous</button>`;
                                        resultsHtml += `<span style="color:#aaa;font-size:12px;">Page ${page} of ${totalPages}</span>`;
                                        resultsHtml += `<button onclick="renderDuplicatesPage(${Math.min(totalPages, page + 1)})" style="${page === totalPages ? pbd : pb}" ${page === totalPages ? 'disabled' : ''}>Next</button>`;
                                        resultsHtml += `</div>`;
                                    }
                                } else if (data.duplicate_groups === 0) {
                                    resultsHtml += '<p><em>No duplicate items found in the database.</em></p>';
                                }

                                resultsHtml += '</div>';

                                const modalBody = document.getElementById('cleanup-modal-body');
                                modalBody.innerHTML = resultsHtml;
                            }

                            // Make function global for pagination buttons
                            window.renderDuplicatesPage = renderDuplicatesPage;

                            const modalTitle = data.dry_run ? 'Duplicate Removal Analysis (Dry Run)' : 'Duplicate Removal Results';

                            // Show modal first
                            showCleanupResultsModal('', modalTitle);

                            // Then render the content with pagination
                            renderDuplicatesPage(currentPage);

                            // Clear and hide the results div since we're using modal now
                            removeDuplicatesResultsDiv.innerHTML = '';
                        } else {
                            throw new Error(data.error || 'An unknown error occurred.');
                        }
                    })
                    .catch(error => {
                        Loading.hide();
                        console.error('Error removing duplicates:', error);
                        showPopup({
                            type: POPUP_TYPES.ERROR,
                            message: error.message,
                            title: 'Error',
                            autoClose: false
                        });
                        removeDuplicatesResultsDiv.innerHTML = `<p class="error" style="color: #dc3545; padding: 10px; background-color: #f8d7da; border: 1px solid #f5c6cb; border-radius: 4px;">Error: ${error.message}</p>`;
                    });
                }
            });
        });
    }
    // --- End Remove Duplicate Database Items Logic ---

    // --- Plex Labels Management Functions ---
    loadContentSourcesForLabels();
});

window.loadContentSourcesForLabels = function() {
    fetch('/debug/plex-labels/sources-list')
        .then(response => response.json())
        .then(data => {
            const select = document.getElementById('bulk-apply-source');
            if (!select) return;

            // Clear existing options except the first placeholder
            while (select.options.length > 1) {
                select.remove(1);
            }

            if (data.success && data.sources) {
                // Add content sources that have plex_labels enabled
                data.sources.forEach(source => {
                    const option = document.createElement('option');
                    option.value = source.id;
                    option.textContent = `${source.name} (${source.type})`;
                    select.appendChild(option);
                });
            }
        })
        .catch(error => console.error('Error loading content sources:', error));
}

window.searchItemsByLabel = function() {
    const label = document.getElementById('label-search-input').value.trim();
    const resultsDiv = document.getElementById('label-search-results');

    if (!label) {
        resultsDiv.innerHTML = '<p style="color: #ff6b6b;">Please enter a label name</p>';
        return;
    }

    resultsDiv.innerHTML = '<p>Searching...</p>';

    fetch(`/debug/plex-labels/search?label=${encodeURIComponent(label)}`)
        .then(response => response.json())
        .then(data => {
            resultsDiv.innerHTML = '';
            if (data.success && data.items && data.items.length > 0) {
                let html = `<p style="color: #4ecdc4; margin-bottom: 10px;">Found ${data.items.length} item(s) with label "<strong>${label}</strong>":</p><ul style="margin:0;padding-left:20px;">`;
                data.items.forEach(item => {
                    html += `<li style="padding: 4px 0;">${item.title} <span style="color:var(--text3)">(${item.type}, ID: ${item.id})</span></li>`;
                });
                html += '</ul>';
                showCleanupResultsModal(html, `Label Search: "${label}"`);
            } else {
                resultsDiv.innerHTML = `<p style="color: #ffd93d;">No items found with label "${label}"</p>`;
            }
        })
        .catch(error => {
            resultsDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${error.message}</p>`;
        });
}

window.previewBulkApply = function() {
    const sourceId = document.getElementById('bulk-apply-source').value;
    const previewDiv = document.getElementById('bulk-apply-preview');
    const executeBtn = document.getElementById('bulk-apply-execute-btn');

    if (!sourceId) {
        previewDiv.innerHTML = '<p style="color: #ff6b6b;">Please select a content source</p>';
        executeBtn.disabled = true;
        return;
    }

    previewDiv.innerHTML = '<p>Loading preview...</p>';

    fetch(`/debug/plex-labels/bulk-apply-preview?source_id=${encodeURIComponent(sourceId)}`)
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                let html = `<p style="color: #4ecdc4;">Preview: ${data.count} item(s) will have labels applied</p>`;
                if (data.labels && data.labels.length > 0) {
                    html += `<p>Labels to apply: <strong>${data.labels.join(', ')}</strong></p>`;
                }
                previewDiv.innerHTML = html;
                executeBtn.disabled = false;
            } else {
                previewDiv.innerHTML = `<p style="color: #ff6b6b;">${data.message || 'Error loading preview'}</p>`;
                executeBtn.disabled = true;
            }
        })
        .catch(error => {
            previewDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${error.message}</p>`;
            executeBtn.disabled = true;
        });
}

// Helper function for streaming progress updates
window.handleStreamingProgress = function(url, body, previewDiv, executeBtn, config) {
    const {
        actionVerb = 'Processing', // e.g., "Removing", "Syncing", "Cleaning"
        countKey = 'applied',      // e.g., "removed", "synced", "cleaned"
        itemKey = 'item_title'
    } = config;

    fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    .then(response => {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        function processText({ done, value }) {
            if (done) {
                executeBtn.disabled = false;
                return;
            }

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    try {
                        const data = JSON.parse(line.slice(6));

                        if (data.error) {
                            previewDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${data.error}</p>`;
                            executeBtn.disabled = false;
                            return;
                        }

                        if (data.status === 'started') {
                            previewDiv.innerHTML = `
                                <div class="progress-message">
                                    <span class="operation-spinner"></span>
                                    <span>${actionVerb} ${data.total} item(s)...</span>
                                </div>
                                <div style="margin-top: 10px;">
                                    <div style="color: #95a5a6; font-size: 12px;">Progress: 0/${data.total}</div>
                                </div>
                            `;
                        }

                        if (data.status === 'progress') {
                            const percent = Math.round((data.current / data.total) * 100);
                            const estMinutes = Math.floor(data.estimated_remaining_seconds / 60);
                            const estSeconds = data.estimated_remaining_seconds % 60;
                            const elapsedMinutes = Math.floor(data.elapsed_seconds / 60);
                            const elapsedSeconds = data.elapsed_seconds % 60;

                            previewDiv.innerHTML = `
                                <div class="progress-message">
                                    <span class="operation-spinner"></span>
                                    <span>${actionVerb}: ${data[itemKey] || 'Unknown'}</span>
                                </div>
                                <div style="margin-top: 10px;">
                                    <div style="background: #2a2a2a; border-radius: 4px; overflow: hidden; margin-bottom: 8px;">
                                        <div style="background: #4ecdc4; height: 6px; width: ${percent}%; transition: width 0.3s;"></div>
                                    </div>
                                    <div style="color: #95a5a6; font-size: 12px;">Progress: ${data.current}/${data.total} (${percent}%) - ${countKey}: ${data[countKey]}</div>
                                    <div style="color: #95a5a6; font-size: 12px;">Elapsed: ${elapsedMinutes}m ${elapsedSeconds}s | Remaining: ~${estMinutes}m ${estSeconds}s</div>
                                </div>
                            `;
                        }

                        if (data.status === 'complete') {
                            let message = `<p style="color: #4ecdc4;">Success! ${actionVerb} ${data[countKey]} out of ${data.total} item(s)</p>`;
                            message += `<p style="color: #95a5a6; font-size: 12px;">Total time: ${Math.floor(data.elapsed_seconds / 60)}m ${data.elapsed_seconds % 60}s</p>`;

                            if (data.failed_items && data.failed_items.length > 0) {
                                message += `<p style="color: #ffa500;">Warning: ${data.failed_count} item(s) failed</p>`;
                            }

                            previewDiv.innerHTML = message;
                            executeBtn.disabled = false;
                        }

                        if (data.status === 'error') {
                            previewDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${data.error}</p>`;
                            executeBtn.disabled = false;
                        }
                    } catch (e) {
                        console.error('Error parsing SSE data:', e);
                    }
                }
            }

            return reader.read().then(processText);
        }

        return reader.read().then(processText);
    })
    .catch(error => {
        previewDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${error.message}</p>`;
        executeBtn.disabled = false;
    });
};

window.executeBulkApply = function() {
    const sourceId = document.getElementById('bulk-apply-source').value;
    const previewDiv = document.getElementById('bulk-apply-preview');
    const executeBtn = document.getElementById('bulk-apply-execute-btn');

    showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Confirm Bulk Apply',
        message: 'Are you sure you want to apply labels to all items from this source?',
        confirmText: 'Apply Labels',
        cancelText: 'Cancel',
        onConfirm: () => {
            executeBtn.disabled = true;
            previewDiv.innerHTML = '<div class="progress-message"><span class="operation-spinner"></span><span>Starting...</span></div>';

            handleStreamingProgress(
                '/debug/plex-labels/bulk-apply-stream',
                { source_id: sourceId },
                previewDiv,
                executeBtn,
                { actionVerb: 'Applying labels to', countKey: 'applied', itemKey: 'item_title' }
            );
        }
    });
}

window.previewBulkRemove = function() {
    const label = document.getElementById('bulk-remove-label-input').value.trim();
    const previewDiv = document.getElementById('bulk-remove-preview');
    const executeBtn = document.getElementById('bulk-remove-execute-btn');

    if (!label) {
        previewDiv.innerHTML = '<p style="color: #ff6b6b;">Please enter a label name</p>';
        executeBtn.disabled = true;
        return;
    }

    previewDiv.innerHTML = '<p>Loading preview...</p>';

    fetch(`/debug/plex-labels/bulk-remove-preview?label=${encodeURIComponent(label)}`)
        .then(response => response.json())
        .then(data => {
            if (data.success && data.count > 0) {
                previewDiv.innerHTML = `<p style="color: #ffd93d;">Warning: ${data.count} item(s) will have label "${label}" removed</p>`;
                executeBtn.disabled = false;
            } else if (data.count === 0) {
                previewDiv.innerHTML = `<p style="color: #4ecdc4;">No items found with label "${label}"</p>`;
                executeBtn.disabled = true;
            } else {
                previewDiv.innerHTML = `<p style="color: #ff6b6b;">${data.message || 'Error loading preview'}</p>`;
                executeBtn.disabled = true;
            }
        })
        .catch(error => {
            previewDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${error.message}</p>`;
            executeBtn.disabled = true;
        });
}

window.executeBulkRemove = function() {
    const label = document.getElementById('bulk-remove-label-input').value.trim();
    const previewDiv = document.getElementById('bulk-remove-preview');
    const executeBtn = document.getElementById('bulk-remove-execute-btn');

    showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Confirm Bulk Remove',
        message: `Are you sure you want to remove label "${label}" from all items? This cannot be undone.`,
        confirmText: 'Remove Label',
        cancelText: 'Cancel',
        onConfirm: () => {
            executeBtn.disabled = true;
            previewDiv.innerHTML = '<div class="progress-message"><span class="operation-spinner"></span><span>Starting...</span></div>';

            handleStreamingProgress(
                '/debug/plex-labels/bulk-remove-stream',
                { label: label },
                previewDiv,
                executeBtn,
                { actionVerb: 'Removing label from', countKey: 'removed', itemKey: 'item_title' }
            );
        }
    });
}

window.findOrphanedLabels = function() {
    const resultsDiv = document.getElementById('orphaned-labels-results');
    const cleanupBtn = document.getElementById('orphaned-cleanup-btn');

    resultsDiv.innerHTML = '<p>Searching...</p>';

    fetch('/debug/plex-labels/find-orphaned')
        .then(response => response.json())
        .then(data => {
            resultsDiv.innerHTML = '';
            if (data.success) {
                const hasOrphanedLabels = data.orphaned_labels && data.orphaned_labels.length > 0;
                const hasOrphanedSources = data.orphaned_sources && data.orphaned_sources.length > 0;

                if (hasOrphanedLabels || hasOrphanedSources) {
                    let html = '';

                    if (hasOrphanedLabels) {
                        html += `<p style="color:#ffd93d;font-weight:bold;margin:0 0 6px;">Orphaned Labels (${data.labels_count})</p>`;
                        html += `<p style="font-size:0.9em;color:var(--text3);margin:0 0 8px;">Labels from sources no longer in config</p>`;
                        html += '<ul style="margin:0 0 16px;padding-left:20px;">';
                        data.orphaned_labels.forEach(label => {
                            html += `<li style="padding:3px 0;">${label.label} <span style="color:var(--text3)">(${label.item_count} items)</span></li>`;
                        });
                        html += '</ul>';
                    }

                    if (hasOrphanedSources) {
                        html += `<p style="color:#ff6b6b;font-weight:bold;margin:0 0 6px;">Unconfigured Sources (${data.sources_count})</p>`;
                        html += `<p style="font-size:0.9em;color:var(--text3);margin:0 0 8px;">Items from sources not in config</p>`;
                        html += '<ul style="margin:0;padding-left:20px;">';
                        data.orphaned_sources.forEach(source => {
                            html += `<li style="padding:3px 0;">${source.source} <span style="color:var(--text3)">(${source.item_count} items)</span></li>`;
                        });
                        html += '</ul>';
                    }

                    html += `<div style="margin-top:16px;border-top:1px solid var(--border2);padding-top:12px;"><button onclick="closeCleanupResultsModal();cleanupOrphanedLabels();" style="width:100%;padding:7px 14px;background:#dc3545;color:#fff;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;">Cleanup</button></div>`;
                    showCleanupResultsModal(html, 'Orphaned Label Cleanup');
                    cleanupBtn.disabled = false;
                } else {
                    resultsDiv.innerHTML = '<p style="color:#4ecdc4;">No orphaned labels or unconfigured sources found!</p>';
                    cleanupBtn.disabled = true;
                }
            } else {
                resultsDiv.innerHTML = `<p style="color:#ff6b6b;">${data.message || 'Error finding orphaned items'}</p>`;
                cleanupBtn.disabled = true;
            }
        })
        .catch(error => {
            resultsDiv.innerHTML = `<p style="color:#ff6b6b;">Error: ${error.message}</p>`;
            cleanupBtn.disabled = true;
        });
}

window.cleanupOrphanedLabels = function() {
    const resultsDiv = document.getElementById('orphaned-labels-results');
    const cleanupBtn = document.getElementById('orphaned-cleanup-btn');

    showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Confirm Cleanup',
        message: 'Are you sure you want to cleanup orphaned data? This will:\n1. Remove labels from inactive sources\n2. Clear source metadata from items with unconfigured sources\n\nThis cannot be undone.',
        confirmText: 'Cleanup',
        cancelText: 'Cancel',
        onConfirm: () => {
            resultsDiv.innerHTML = '<div class="progress-message"><span class="operation-spinner"></span><span>Starting cleanup...</span></div>';
            cleanupBtn.disabled = true;

            // Handle custom two-phase streaming progress
            fetch('/debug/plex-labels/cleanup-orphaned-stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            })
            .then(response => {
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';

                function processText({ done, value }) {
                    if (done) {
                        cleanupBtn.disabled = false;
                        return;
                    }

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop();

                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            try {
                                const data = JSON.parse(line.slice(6));

                                if (data.error) {
                                    resultsDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${data.error}</p>`;
                                    cleanupBtn.disabled = false;
                                    return;
                                }

                                if (data.status === 'started') {
                                    const phaseLabel = data.phase === 1 ? 'Phase 1: Cleaning orphaned labels' : 'Phase 2: Clearing orphaned source metadata';
                                    resultsDiv.innerHTML = `
                                        <div class="progress-message">
                                            <span class="operation-spinner"></span>
                                            <span>${phaseLabel}</span>
                                        </div>
                                        <div style="margin-top: 10px;">
                                            <div style="color: #95a5a6; font-size: 12px;">Progress: 0/${data.total}</div>
                                        </div>
                                    `;
                                }

                                if (data.status === 'progress') {
                                    const percent = Math.round((data.current / data.total) * 100);
                                    const estMinutes = Math.floor(data.estimated_remaining_seconds / 60);
                                    const estSeconds = data.estimated_remaining_seconds % 60;
                                    const elapsedMinutes = Math.floor(data.elapsed_seconds / 60);
                                    const elapsedSeconds = data.elapsed_seconds % 60;

                                    let itemInfo = '';
                                    let countInfo = '';
                                    let phaseLabel = '';

                                    if (data.phase === 1) {
                                        phaseLabel = 'Phase 1: Cleaning orphaned labels';
                                        itemInfo = data.item_title || 'Unknown';
                                        countInfo = `Labels cleaned: ${data.labels_cleaned}`;
                                    } else {
                                        phaseLabel = 'Phase 2: Clearing source metadata';
                                        itemInfo = `${data.item_title || 'Unknown'} (${data.source})`;
                                        countInfo = `Sources cleaned: ${data.sources_cleaned}`;
                                    }

                                    resultsDiv.innerHTML = `
                                        <div class="progress-message">
                                            <span class="operation-spinner"></span>
                                            <span>${phaseLabel}: ${itemInfo}</span>
                                        </div>
                                        <div style="margin-top: 10px;">
                                            <div style="background: #2a2a2a; border-radius: 4px; overflow: hidden; margin-bottom: 8px;">
                                                <div style="background: #4ecdc4; height: 6px; width: ${percent}%; transition: width 0.3s;"></div>
                                            </div>
                                            <div style="color: #95a5a6; font-size: 12px;">Progress: ${data.current}/${data.total} (${percent}%) - ${countInfo}</div>
                                            <div style="color: #95a5a6; font-size: 12px;">Elapsed: ${elapsedMinutes}m ${elapsedSeconds}s | Remaining: ~${estMinutes}m ${estSeconds}s</div>
                                        </div>
                                    `;
                                }

                                if (data.status === 'complete') {
                                    let message = '<p style="color: #4ecdc4;">Cleanup Complete!</p>';
                                    message += `<div style="margin-top: 10px;">`;
                                    message += `<div>• Labels cleaned: ${data.labels_cleaned} (from ${data.total_phase1} items checked)</div>`;
                                    message += `<div>• Sources cleaned: ${data.sources_cleaned} (from ${data.total_phase2} items checked)</div>`;
                                    message += `<div style="color: #95a5a6; font-size: 12px; margin-top: 8px;">Total time: ${Math.floor(data.elapsed_seconds / 60)}m ${data.elapsed_seconds % 60}s</div>`;
                                    message += `</div>`;

                                    resultsDiv.innerHTML = message;
                                    cleanupBtn.disabled = false;
                                }

                                if (data.status === 'error') {
                                    resultsDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${data.error}</p>`;
                                    cleanupBtn.disabled = false;
                                }
                            } catch (e) {
                                console.error('Error parsing SSE data:', e);
                            }
                        }
                    }

                    return reader.read().then(processText);
                }

                return reader.read().then(processText);
            })
            .catch(error => {
                resultsDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${error.message}</p>`;
                cleanupBtn.disabled = false;
            });
        }
    });
}

window.syncAllLabels = function() {
    const statusDiv = document.getElementById('sync-labels-status');

    showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Confirm Sync',
        message: 'This will re-sync all labels based on current content source configurations. Continue?',
        confirmText: 'Sync Labels',
        cancelText: 'Cancel',
        onConfirm: () => {
            statusDiv.innerHTML = '<div class="progress-message"><span class="operation-spinner"></span><span>Starting...</span></div>';

            // Create a dummy button for the helper (sync doesn't have a button)
            const dummyBtn = { disabled: false };

            handleStreamingProgress(
                '/debug/plex-labels/sync-all-stream',
                {},
                statusDiv,
                dummyBtn,
                { actionVerb: 'Syncing labels for', countKey: 'synced', itemKey: 'item_title' }
            );
        }
    });
}

// Backfill Resolution Function
window.backfillResolution = function() {
    const statusDiv = document.getElementById('backfill-resolution-status');

    showConfirmationModal({
        title: 'Backfill Resolution Data',
        message: 'This will extract resolution data (2160p, 1080p, etc.) from stored file paths for all Collected items with NULL resolution. This is safe to run multiple times and will not overwrite existing resolution data.',
        confirmText: 'Start Backfill',
        cancelText: 'Cancel',
        onConfirm: () => {
            statusDiv.innerHTML = '<div class="progress-message"><span class="operation-spinner"></span><span>Backfilling resolution data...</span></div>';

            fetch('/debug/plex-labels/backfill-resolution', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    let resultHtml = `<div class="success-message">${data.message}</div>`;
                    resultHtml += `<div style="margin-top: 10px;"><strong>Summary:</strong></div>`;
                    resultHtml += `<div>Total items processed: ${data.total_items}</div>`;
                    resultHtml += `<div>Successfully updated: ${data.updated}</div>`;
                    if (data.failed > 0) {
                        resultHtml += `<div>Failed: ${data.failed}</div>`;
                    }

                    if (data.by_resolution && Object.keys(data.by_resolution).length > 0) {
                        resultHtml += `<div style="margin-top: 10px;"><strong>By Resolution:</strong></div>`;
                        for (const [resolution, count] of Object.entries(data.by_resolution)) {
                            resultHtml += `<div>&nbsp;&nbsp;${resolution}: ${count} items</div>`;
                        }
                    }

                    statusDiv.innerHTML = resultHtml;
                } else {
                    statusDiv.innerHTML = `<div class="error-message">${data.message}</div>`;
                }
            })
            .catch(error => {
                console.error('Error:', error);
                statusDiv.innerHTML = `<div class="error-message">Error: ${error.message}</div>`;
            });
        }
    });
}
// --- End Plex Labels Management Functions ---

// --- Test Plex Item Lookup Function ---
window.testItemLookup = function() {
    const itemId = document.getElementById('test_lookup_item_id').value.trim();
    const resultsDiv = document.getElementById('test-lookup-results');

    if (!itemId) {
        resultsDiv.innerHTML = '<p style="color: #ff6b6b;">Please enter an Item ID</p>';
        return;
    }

    resultsDiv.innerHTML = '<p>Testing Plex lookup...</p>';

    fetch(`/debug/plex-labels/test-item-lookup/${itemId}`)
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            let html = '';

            // Database info
            const dbInfo = data.database_info || {};
            html += `<div style="margin-bottom: 15px; padding: 10px; background: #2a2a2a; border-radius: 4px;">`;
            html += `<div style="margin-bottom: 8px;"><strong>Database Info:</strong></div>`;
            html += `<div><strong>ID:</strong> ${data.item_id}</div>`;
            html += `<div><strong>Title:</strong> ${dbInfo.title || 'N/A'}</div>`;
            html += `<div><strong>Type:</strong> ${dbInfo.type || 'N/A'}</div>`;
            html += `<div><strong>Year:</strong> ${dbInfo.year || 'N/A'}</div>`;
            html += `<div><strong>IMDb ID:</strong> ${dbInfo.imdb_id || 'None'}</div>`;
            html += `<div><strong>TMDb ID:</strong> ${dbInfo.tmdb_id || 'None'}</div>`;
            html += `</div>`;

            // Plex match result
            if (data.plex_found) {
                const plexInfo = data.plex_info || {};
                const plexLabels = plexInfo.labels || [];
                html += `<p style="color: #4ecdc4;">✓ Item found in Plex!</p>`;
                html += `<div style="padding: 10px; background: #2a2a2a; border-radius: 4px;">`;
                html += `<div><strong>Plex Title:</strong> ${plexInfo.title || 'N/A'}</div>`;
                html += `<div><strong>Plex Year:</strong> ${plexInfo.year || 'N/A'}</div>`;
                html += `<div><strong>Plex GUID:</strong> ${plexInfo.guid || 'N/A'}</div>`;
                html += `<div><strong>Plex Labels:</strong> ${plexLabels.length > 0 ? plexLabels.join(', ') : 'None'}</div>`;
                html += `</div>`;
            } else {
                html += `<p style="color: #ffa500;">✗ Item NOT found in Plex</p>`;
                html += `<p style="color: #95a5a6; font-size: 12px;">This item exists in the database but couldn't be matched to Plex. Possible reasons:</p>`;
                html += `<ul style="color: #95a5a6; font-size: 12px; margin-left: 20px;">`;
                html += `<li>Item not in Plex library yet</li>`;
                html += `<li>Missing or incorrect IMDb/TMDb ID</li>`;
                html += `<li>Title mismatch (check for apostrophes, special characters)</li>`;
                html += `</ul>`;
            }

            resultsDiv.innerHTML = html;
        } else {
            resultsDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${data.message}</p>`;
        }
    })
    .catch(error => {
        resultsDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${error.message}</p>`;
    });
};
// --- End Test Plex Item Lookup Function ---

// --- Fix Missing IMDb ID Function ---
document.getElementById('fix_imdb_search_by')?.addEventListener('change', function() {
    document.getElementById('fix_imdb_lookup_label').textContent = this.value === 'tmdb_id' ? 'TMDB ID:' : 'Item ID:';
    document.getElementById('fix_imdb_item_id').placeholder = this.value === 'tmdb_id' ? 'e.g., 12345' : 'e.g., 98080';
});

window.fixImdbId = function() {
    const lookupVal = document.getElementById('fix_imdb_item_id').value.trim();
    const imdbId = document.getElementById('fix_imdb_id').value.trim();
    const searchBy = document.getElementById('fix_imdb_search_by').value;
    const resultsDiv = document.getElementById('fix-imdb-results');

    if (!lookupVal || !imdbId) {
        resultsDiv.innerHTML = '<p style="color: #ff6b6b;">Please fill in both fields</p>';
        return;
    }
    if (!imdbId.match(/^tt\d+$/)) {
        resultsDiv.innerHTML = '<p style="color: #ff6b6b;">IMDb ID must start with "tt" followed by numbers (e.g., tt37212066)</p>';
        return;
    }

    resultsDiv.innerHTML = '<p>Updating IMDb ID...</p>';

    const url = searchBy === 'tmdb_id'
        ? `/debug/plex-labels/fix-imdb-by-tmdb/${lookupVal}/${imdbId}`
        : `/debug/plex-labels/fix-imdb/${lookupVal}/${imdbId}`;

    fetch(url, { method: 'POST' })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            let html = `<p style="color: #4ecdc4;">✓ Success!</p>`;
            html += `<div style="margin-top: 10px; padding: 10px; background: #2a2a2a; border-radius: 4px;">`;
            if (searchBy === 'tmdb_id') {
                html += `<div><strong>Updated ${data.updated_count} item(s)</strong></div>`;
                html += `<div><strong>IMDb ID set to:</strong> ${data.imdb_id_after}</div>`;
                (data.items || []).forEach(i => {
                    html += `<div style="margin-top:4px; font-size:12px;">ID ${i.id} — ${i.title} (was: ${i.imdb_id_before || 'None'})</div>`;
                });
            } else {
                html += `<div><strong>Item ID:</strong> ${data.item_id}</div>`;
                html += `<div><strong>Title:</strong> ${data.title}</div>`;
                html += `<div><strong>IMDb ID Before:</strong> ${data.imdb_id_before || 'None'}</div>`;
                html += `<div><strong>IMDb ID After:</strong> ${data.imdb_id_after}</div>`;
            }
            html += `</div>`;
            resultsDiv.innerHTML = html;
        } else {
            resultsDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${data.message}</p>`;
        }
    })
    .catch(error => {
        resultsDiv.innerHTML = `<p style="color: #ff6b6b;">Error: ${error.message}</p>`;
    });
};
// --- End Fix Missing IMDb ID Function ---

// --- Plex Labels Help Function ---
window.showPlexLabelsHelp = function() {
    const helpOverlay = document.getElementById('help-overlay');
    const helpModalBox = document.getElementById('help-modal-box');
    const helpModalBody = document.getElementById('help-modal-body');

    if (!helpOverlay || !helpModalBox || !helpModalBody) {
        console.error('Help modal elements not found');
        return;
    }

    // Show the modal
    document.body.classList.add('modal-open');
    helpOverlay.style.display = 'block';
    helpModalBox.style.display = 'flex';
    helpOverlay.classList.add('visible');
    helpModalBox.classList.add('visible');

    // Load the Plex Labels help content
    helpModalBody.innerHTML = `<p>Loading help...</p>`;

    fetch('/base/api/help-content?page_path=plex_labels')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.html) {
                helpModalBody.innerHTML = data.html;
            } else {
                helpModalBody.innerHTML = `<p>Help content not available.</p>`;
            }
        })
        .catch(error => {
            console.error('Error loading help content:', error);
            helpModalBody.innerHTML = `<p>Error loading help content.</p>`;
        });
};
// --- End Plex Labels Help Function ---

// --- Backup Restoration Functions ---
let selectedBackupPath = null;
let selectedBackupFilename = null;

async function loadBackupsList() {
    const resultsDiv = document.getElementById('restoreResults');
    resultsDiv.innerHTML = '<div class="spinner-border text-primary" role="status"><span class="sr-only">Loading...</span></div>';

    try {
        const response = await fetch('/debug/api/list_database_backups');
        const data = await response.json();

        if (!data.success) {
            resultsDiv.innerHTML = `<div class="alert alert-danger">Error: ${data.error}</div>`;
            return;
        }

        if (data.backups.length === 0) {
            resultsDiv.innerHTML = '<div class="alert alert-info">No backup files found.</div>';
            return;
        }

        let html = `
            <table class="table table-sm table-bordered">
                <tbody>
        `;

        data.backups.forEach(backup => {
            const categoryColors = {
                'corrupted': 'danger',
                'manual_copy': 'warning',
                'old_version': 'info',
                'safety_backup': 'secondary',
                'recent_backup': 'success',
                'orphaned_temp': 'danger',
                'unknown': 'dark'
            };
            const inlineColors = {
                'danger': 'background-color: rgba(239, 68, 68, 0.5); color: #fff; border-radius: 0.25rem;',
                'warning': 'background-color: rgba(234, 179, 8, 0.5); color: #fff; border-radius: 0.25rem;',
                'success': 'background-color: rgba(16, 185, 129, 0.5); color: #fff; border-radius: 0.25rem;',
                'info': 'background-color: rgba(59, 130, 246, 0.5); color: #fff; border-radius: 0.25rem;',
                'secondary': 'background-color: rgba(107, 114, 128, 0.5); color: #fff; border-radius: 0.25rem;',
                'dark': 'background-color: rgba(30, 30, 30, 0.5); color: #fff; border-radius: 0.25rem;'
            };
            const badgeColor = categoryColors[backup.category] || 'secondary';
            const badgeStyle = inlineColors[badgeColor] || inlineColors['secondary'];
            const rowClass = backup.is_valid ? '' : 'table-danger';
            const validBadge = backup.is_valid
                ? `<span class="badge badge-success" style="${inlineColors['success']} font-size: 0.75em; padding: 0.4em 0.6em;">✓ Valid</span>`
                : `<span class="badge badge-danger" style="${inlineColors['danger']} font-size: 0.75em; padding: 0.4em 0.6em;">✗ Invalid</span>`;

            html += `
                <tr class="${rowClass}">
                    <td style="padding: 8px;">
                        <div style="display: flex; align-items: flex-start; gap: 8px;">
                            <input type="radio" name="backupSelection" style="margin-top: 3px; flex-shrink: 0;"
                                   ${backup.is_valid ? '' : 'disabled'}
                                   onclick="selectBackup('${backup.path}', '${backup.filename}')">
                            <div style="flex: 1; min-width: 0; overflow: hidden;">
                                <div style="font-weight: 500; margin-bottom: 4px; font-size: 0.9em; word-wrap: break-word;">${backup.filename}</div>
                                <div style="font-size: 0.8em; opacity: 0.8; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
                                    <span>Size: ${backup.size_mb} MB</span>
                                    <span>Age: ${backup.age_display}</span>
                                    <span class="badge badge-${badgeColor}" style="${badgeStyle} font-size: 0.75em; padding: 0.4em 0.6em;">${backup.category}</span>
                                    ${validBadge}
                                </div>
                            </div>
                        </div>
                    </td>
                </tr>
            `;
        });

        html += `
                </tbody>
            </table>
            <button class="btn btn-warning btn-lg" id="restoreBtn" onclick="confirmRestore()" disabled>
                <i class="fas fa-undo"></i> Restore Selected Backup
            </button>
        `;

        resultsDiv.innerHTML = html;

    } catch (error) {
        resultsDiv.innerHTML = `<div class="alert alert-danger">Error loading backups: ${error.message}</div>`;
    }
}

function selectBackup(path, filename) {
    selectedBackupPath = path;
    selectedBackupFilename = filename;
    document.getElementById('restoreBtn').disabled = false;
}

async function confirmRestore() {
    if (!selectedBackupPath) {
        showPopup({ type: 'warning', title: 'Required', message: 'Please select a backup to restore.', autoClose: 4000 });
        return;
    }

    const createBackup = document.getElementById('backupBeforeRestore').checked;

    const backupLine = createBackup
        ? `<li>Your current database will be backed up as a safety copy</li>`
        : `<li><strong>Your current database will NOT be backed up</strong></li>`;

    showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: '⚠️ Database Restoration',
        message: `
            <p>You are about to restore from:</p>
            <p><code>${selectedBackupFilename}</code></p>
            <p><strong>Important:</strong></p>
            <ul style="list-style:disc;padding-left:1.2em;margin:8px 0;">
                <li>The container will <strong>RESTART</strong> automatically</li>
                ${backupLine}
                <li>All current data will be replaced with the backup</li>
                <li>This action cannot be undone</li>
            </ul>
            <p>Are you absolutely sure you want to proceed?</p>
        `,
        confirmText: 'Restore',
        cancelText: 'Cancel',
        onConfirm: async () => {
            const resultsDiv = document.getElementById('restoreResults');
            resultsDiv.innerHTML = '<div class="alert alert-warning"><strong>Requesting restore...</strong></div>';

            try {
                const response = await fetch('/debug/api/request_database_restore', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        backup_path: selectedBackupPath,
                        create_safety_backup: createBackup
                    })
                });

                const data = await response.json();

                if (data.success) {
                    // Show countdown and auto-refresh
                    let countdown = 60;
                    resultsDiv.innerHTML = `
                        <div class="alert alert-success">
                            <h5><i class="fas fa-check-circle"></i> Restore Request Created</h5>
                            <p><strong>The container is restarting...</strong></p>
                            <p>Database will be restored from: <code>${selectedBackupFilename}</code></p>
                            <p style="font-size: 1.2em; margin-top: 15px;">
                                <i class="fas fa-sync fa-spin"></i> Auto-refreshing in <strong id="countdown">${countdown}</strong> seconds...
                            </p>
                            <p style="font-size: 0.9em; opacity: 0.8; margin-top: 10px;">
                                <em>You can also refresh manually if the app is ready sooner.</em>
                            </p>
                        </div>
                    `;

                    // Countdown timer with auto-refresh
                    const countdownInterval = setInterval(() => {
                        countdown--;
                        const countdownEl = document.getElementById('countdown');
                        if (countdownEl) countdownEl.textContent = countdown;
                        if (countdown <= 0) {
                            clearInterval(countdownInterval);
                            location.reload();
                        }
                    }, 1000);
                } else {
                    resultsDiv.innerHTML = `<div class="alert alert-danger"><strong>Error:</strong> ${data.error}</div>`;
                }
            } catch (error) {
                resultsDiv.innerHTML = `<div class="alert alert-danger"><strong>Error:</strong> ${error.message}</div>`;
            }
        }
    });
}
// --- End Backup Restoration Functions ---

// --- Database Cleanup Functions ---
let scanResults = null;

async function scanOldDatabases() {
    const resultsDiv = document.getElementById('cleanupResults');
    resultsDiv.innerHTML = '<div class="spinner-border text-primary" role="status"><span class="sr-only">Scanning...</span></div>';

    try {
        const response = await fetch('/debug/api/scan_old_databases');
        const data = await response.json();

        if (!data.success) {
            resultsDiv.innerHTML = `<div class="alert alert-danger">Error: ${data.error}</div>`;
            return;
        }

        scanResults = data;
        displayScanResults(data);

    } catch (error) {
        resultsDiv.innerHTML = `<div class="alert alert-danger">Error scanning: ${error.message}</div>`;
    }
}

function displayScanResults(data) {
    const resultsDiv = document.getElementById('cleanupResults');

    if (data.total_files === 0) {
        resultsDiv.innerHTML = '<div class="alert alert-success">✓ No old database files found. Your system is clean!</div>';
        return;
    }

    let html = `
        <div class="alert alert-info">
            <strong>Scan Results:</strong> Found ${data.total_files} old database files
            (${data.total_size_mb.toFixed(2)} MB total)
        </div>
    `;

    // Backups folder
    if (data.backups_folder.files.length > 0) {
        html += `
            <h5><i class="fas fa-folder"></i> Backups Folder (${data.backups_folder.files.length} files, ${data.backups_folder.total_size_mb.toFixed(2)} MB)</h5>
            <div style="margin-bottom: 10px;">
                <label><input type="checkbox" id="selectAllBackups" onclick="toggleAllBackups(this.checked)"> Select All</label>
            </div>
            <table class="table table-sm table-bordered">
                <tbody>
        `;

        data.backups_folder.files.forEach((file, idx) => {
            const categoryColors = {
                'corrupted': 'danger',
                'manual_copy': 'warning',
                'old_version': 'info',
                'safety_backup': 'secondary',
                'recent_backup': 'success',
                'orphaned_temp': 'danger',
                'unknown': 'dark'
            };
            const inlineColors = {
                'danger': 'background-color: rgba(239, 68, 68, 0.5); color: #fff; border-radius: 0.25rem;',
                'warning': 'background-color: rgba(234, 179, 8, 0.5); color: #fff; border-radius: 0.25rem;',
                'success': 'background-color: rgba(16, 185, 129, 0.5); color: #fff; border-radius: 0.25rem;',
                'info': 'background-color: rgba(59, 130, 246, 0.5); color: #fff; border-radius: 0.25rem;',
                'secondary': 'background-color: rgba(107, 114, 128, 0.5); color: #fff; border-radius: 0.25rem;',
                'dark': 'background-color: rgba(30, 30, 30, 0.5); color: #fff; border-radius: 0.25rem;'
            };
            const badgeColor = categoryColors[file.category] || 'secondary';
            const badgeStyle = inlineColors[badgeColor] || inlineColors['secondary'];

            html += `
                <tr>
                    <td style="padding: 8px;">
                        <div style="display: flex; align-items: flex-start; gap: 8px;">
                            <input type="checkbox" class="backup-checkbox" value="${file.path}" style="margin-top: 3px; flex-shrink: 0;">
                            <div style="flex: 1; min-width: 0; overflow: hidden;">
                                <div style="font-weight: 500; margin-bottom: 4px; font-size: 0.9em; word-wrap: break-word;">${file.filename}</div>
                                <div style="font-size: 0.8em; opacity: 0.8; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
                                    <span>Size: ${file.size_mb} MB</span>
                                    <span>Age: ${file.age_display}</span>
                                    <span class="badge badge-${badgeColor}" style="${badgeStyle} font-size: 0.75em; padding: 0.4em 0.6em;">${file.category}</span>
                                </div>
                            </div>
                        </div>
                    </td>
                </tr>
            `;
        });

        html += `
                </tbody>
            </table>
        `;
    }

    // DB Content folder
    if (data.db_content_folder.files.length > 0) {
        html += `
            <h5><i class="fas fa-folder"></i> DB Content Folder (${data.db_content_folder.files.length} files, ${data.db_content_folder.total_size_mb.toFixed(2)} MB)</h5>
            <div style="margin-bottom: 10px;">
                <label><input type="checkbox" id="selectAllContent" onclick="toggleAllContent(this.checked)"> Select All</label>
            </div>
            <table class="table table-sm table-bordered">
                <tbody>
        `;

        data.db_content_folder.files.forEach((file, idx) => {
            const categoryColors = {
                'corrupted': 'danger',
                'manual_copy': 'warning',
                'old_version': 'info',
                'safety_backup': 'secondary',
                'recent_backup': 'success',
                'orphaned_temp': 'danger',
                'unknown': 'dark'
            };
            const inlineColors = {
                'danger': 'background-color: rgba(239, 68, 68, 0.5); color: #fff; border-radius: 0.25rem;',
                'warning': 'background-color: rgba(234, 179, 8, 0.5); color: #fff; border-radius: 0.25rem;',
                'success': 'background-color: rgba(16, 185, 129, 0.5); color: #fff; border-radius: 0.25rem;',
                'info': 'background-color: rgba(59, 130, 246, 0.5); color: #fff; border-radius: 0.25rem;',
                'secondary': 'background-color: rgba(107, 114, 128, 0.5); color: #fff; border-radius: 0.25rem;',
                'dark': 'background-color: rgba(30, 30, 30, 0.5); color: #fff; border-radius: 0.25rem;'
            };
            const badgeColor = categoryColors[file.category] || 'secondary';
            const badgeStyle = inlineColors[badgeColor] || inlineColors['secondary'];

            html += `
                <tr>
                    <td style="padding: 8px;">
                        <div style="display: flex; align-items: flex-start; gap: 8px;">
                            <input type="checkbox" class="content-checkbox" value="${file.path}" style="margin-top: 3px; flex-shrink: 0;">
                            <div style="flex: 1; min-width: 0; overflow: hidden;">
                                <div style="font-weight: 500; margin-bottom: 4px; font-size: 0.9em; word-wrap: break-word;">${file.filename}</div>
                                <div style="font-size: 0.8em; opacity: 0.8; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
                                    <span>Size: ${file.size_mb} MB</span>
                                    <span>Age: ${file.age_display}</span>
                                    <span class="badge badge-${badgeColor}" style="${badgeStyle} font-size: 0.75em; padding: 0.4em 0.6em;">${file.category}</span>
                                </div>
                            </div>
                        </div>
                    </td>
                </tr>
            `;
        });

        html += `
                </tbody>
            </table>
        `;
    }

    html += `
        <div class="mt-3">
            <button class="btn btn-info btn-lg mr-2" onclick="deleteOldDatabases(true)">
                <i class="fas fa-eye"></i> Dry Run (Preview)
            </button>
            <button class="btn btn-danger btn-lg" onclick="deleteOldDatabases(false)">
                <i class="fas fa-trash"></i> Delete Selected Files
            </button>
        </div>
    `;

    resultsDiv.innerHTML = html;
}

function toggleAllBackups(checked) {
    document.querySelectorAll('.backup-checkbox').forEach(cb => cb.checked = checked);
}

function toggleAllContent(checked) {
    document.querySelectorAll('.content-checkbox').forEach(cb => cb.checked = checked);
}

async function deleteOldDatabases(dryRun) {
    const backupPaths = Array.from(document.querySelectorAll('.backup-checkbox:checked')).map(cb => cb.value);
    const contentPaths = Array.from(document.querySelectorAll('.content-checkbox:checked')).map(cb => cb.value);
    const allPaths = [...backupPaths, ...contentPaths];

    if (allPaths.length === 0) {
        showPopup({ type: 'warning', title: 'Required', message: 'Please select at least one file to delete.', autoClose: 4000 });
        return;
    }

    const doDelete = async () => {
        const resultsDiv = document.getElementById('cleanupResults');
        resultsDiv.innerHTML = `<div class="alert alert-warning"><strong>${dryRun ? 'Running dry run...' : 'Deleting files...'}</strong></div>`;

        try {
            const response = await fetch('/debug/api/delete_old_databases', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    file_paths: allPaths,
                    dry_run: dryRun
                })
            });

            const data = await response.json();

            if (data.success) {
                let html = `
                    <div class="alert alert-${dryRun ? 'info' : 'success'}">
                        <h5><i class="fas fa-check-circle"></i> ${dryRun ? 'Dry Run Complete' : 'Deletion Complete'}</h5>
                        <p><strong>Deleted:</strong> ${data.deleted_count} files (${data.total_size_freed_mb.toFixed(2)} MB freed)</p>
                        <p><strong>Skipped:</strong> ${data.skipped_count} files</p>
                    </div>
                `;

                if (data.deleted.length > 0) {
                    html += '<h6>Deleted Files:</h6><ul>';
                    data.deleted.forEach(file => { html += `<li><code>${file}</code></li>`; });
                    html += '</ul>';
                }

                if (data.skipped.length > 0) {
                    html += '<h6>Skipped Files:</h6><ul>';
                    data.skipped.forEach(item => { html += `<li><code>${item.file}</code> - ${item.reason}</li>`; });
                    html += '</ul>';
                }

                if (!dryRun) {
                    html += '<button class="btn btn-primary mt-3" onclick="scanOldDatabases()">Scan Again</button>';
                }

                resultsDiv.innerHTML = html;
            } else {
                resultsDiv.innerHTML = `<div class="alert alert-danger"><strong>Error:</strong> ${data.error}</div>`;
            }
        } catch (error) {
            resultsDiv.innerHTML = `<div class="alert alert-danger"><strong>Error:</strong> ${error.message}</div>`;
        }
    };

    if (!dryRun) {
        showPopup({
            type: POPUP_TYPES.CONFIRM,
            title: '⚠️ File Deletion',
            message: `
                <p>You are about to <strong>permanently delete ${allPaths.length} database file${allPaths.length !== 1 ? 's' : ''}</strong>.</p>
                <p>This action <strong>cannot be undone</strong>.</p>
                <p>Are you sure you want to proceed?</p>
            `,
            confirmText: 'Delete',
            cancelText: 'Cancel',
            onConfirm: doDelete
        });
    } else {
        doDelete();
    }
}
// --- End Database Cleanup Functions ---

// Make functions globally accessible for onclick handlers
window.loadBackupsList = loadBackupsList;
window.selectBackup = selectBackup;
window.confirmRestore = confirmRestore;
window.scanOldDatabases = scanOldDatabases;
window.toggleAllBackups = toggleAllBackups;
window.toggleAllContent = toggleAllContent;
window.deleteOldDatabases = deleteOldDatabases;

// ── cli_mount Cleanup ──────────────────────────────────────────────────
let _dcyPollTimer = null;

async function dcyLoadProviders() {
    const sel = document.getElementById('dcy-provider');
    const dbInput = document.getElementById('dcy-db-path');
    sel.innerHTML = '<option value="">Loading...</option>';
    try {
        const r = await fetch('/debug/api/climount_providers');
        const d = await r.json();
        if (d.success && d.providers && d.providers.length) {
            sel.innerHTML = d.providers.map(p => `<option value="${p}">${p}</option>`).join('');
            // Pre-fill db path based on data_path setting
            if (d.data_path && !dbInput.value) {
                dbInput.value = d.data_path.replace(/\/?$/, '/db');
            }
        } else {
            sel.innerHTML = '<option value="">No providers found — check cli_mount Data Path setting</option>';
        }
    } catch(e) {
        sel.innerHTML = '<option value="">Error loading providers</option>';
    }
}

async function dcyRunCleanup() {
    const provider = document.getElementById('dcy-provider').value;
    const dbPath = document.getElementById('dcy-db-path').value.trim();
    const dryRun = document.getElementById('dcy-dry-run').checked;
    const btn = document.getElementById('dcy-run-btn');
    const status = document.getElementById('dcy-status');

    if (!provider) { alert('Select a provider first'); return; }
    if (!dbPath) { alert('Enter the DB path'); return; }

    btn.disabled = true;
    btn.textContent = 'Running...';
    status.style.display = 'block';
    status.style.color = '#aaa';
    status.textContent = 'Starting...';
    clearTimeout(_dcyPollTimer);

    try {
        const r = await fetch('/debug/api/climount_cleanup', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({provider, dry_run: dryRun, db_path: dbPath})
        });
        const d = await r.json();
        if (!d.success) {
            status.style.color = '#ff6b6b';
            status.textContent = 'Error: ' + (d.error || 'Unknown error');
            btn.disabled = false; btn.textContent = 'Run Cleanup';
            return;
        }
        _dcyPollCleanup(d.job_id, btn, status);
    } catch(e) {
        status.style.color = '#ff6b6b';
        status.textContent = 'Error: ' + e.message;
        btn.disabled = false; btn.textContent = 'Run Cleanup';
    }
}

function _dcyPollCleanup(jobId, btn, status) {
    _dcyPollTimer = setTimeout(async () => {
        try {
            const r = await fetch(`/debug/api/climount_cleanup_status/${jobId}`);
            const d = await r.json();
            if (d.status === 'running') {
                status.textContent = 'Running...';
                _dcyPollCleanup(jobId, btn, status);
                return;
            }
            btn.disabled = false;
            btn.textContent = 'Run Cleanup';
            const result = d.result || {};
            if (result.success) {
                const isDry = result.dry_run;
                const summary = isDry
                    ? `Dry run complete — ${result.entries_to_delete} entries, ${result.items_full_delete} full deletes, ${result.items_partial} partial, ${result.items_untouched} untouched`
                    : `Done — ${result.entries_deleted} entries deleted, ${result.items_deleted} folders deleted, ${result.items_updated} folders updated`;
                status.style.color = '#4ade80';
                status.textContent = summary;
                // Show full output in modal
                const lines = result.lines || [];
                document.getElementById('dcy-modal-body').textContent = lines.join('\n');
                document.getElementById('dcy-results-modal').style.display = 'flex';
            } else {
                status.style.color = '#ff6b6b';
                status.textContent = 'Error: ' + (result.error || 'Unknown error');
            }
        } catch(e) {
            btn.disabled = false; btn.textContent = 'Run Cleanup';
            status.style.color = '#ff6b6b';
            status.textContent = 'Poll error: ' + e.message;
        }
    }, 1500);
}

window.dcyLoadProviders = dcyLoadProviders;
window.dcyRunCleanup = dcyRunCleanup;

// ── cli_mount DB Backup / Restore / Cleanup ───────────────────────────

// Show cli_mount DB sections only if data_path is configured
(async function dcyCheckDataPath() {
    try {
        const r = await fetch('/debug/api/climount_providers');
        const d = await r.json();
        if (d.success && d.data_path) {
            ['dcy-backup-section','dcy-restore-section','dcy-cleanup-db-section'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.style.display = '';
            });
        }
    } catch(e) {}
})();

async function dcyBackupNow() {
    const btn = document.getElementById('dcy-backup-now-btn');
    const result = document.getElementById('dcy-backup-result');
    btn.disabled = true; btn.textContent = 'Backing up...';
    result.textContent = '';
    try {
        const r = await fetch('/debug/api/climount_backup_now', {method:'POST'});
        const d = await r.json();
        result.style.color = d.success ? '#4ade80' : '#ff6b6b';
        result.textContent = d.success ? d.message : ('Error: ' + d.error);
    } catch(e) {
        result.style.color = '#ff6b6b';
        result.textContent = 'Error: ' + e.message;
    }
    btn.disabled = false; btn.textContent = 'Backup Now';
}

let _dcySelectedBackupPath = null;

async function dcyLoadBackupsList() {
    const resultsDiv = document.getElementById('dcyRestoreResults');
    resultsDiv.innerHTML = '<div class="spinner-border text-primary" role="status"><span class="sr-only">Loading...</span></div>';
    try {
        const r = await fetch('/debug/api/list_climount_backups');
        const d = await r.json();
        if (!d.success) { resultsDiv.innerHTML = `<div class="alert alert-danger">Error: ${d.error}</div>`; return; }
        if (!d.backups.length) { resultsDiv.innerHTML = '<div class="alert alert-info">No backup files found.</div>'; return; }

        const categoryColors = {
            'corrupted': 'danger', 'manual_copy': 'warning', 'old_version': 'info',
            'safety_backup': 'secondary', 'recent_backup': 'success', 'orphaned_temp': 'danger', 'unknown': 'dark'
        };
        const inlineColors = {
            'danger': 'background-color: rgba(239, 68, 68, 0.5); color: #fff; border-radius: 0.25rem;',
            'warning': 'background-color: rgba(234, 179, 8, 0.5); color: #fff; border-radius: 0.25rem;',
            'success': 'background-color: rgba(16, 185, 129, 0.5); color: #fff; border-radius: 0.25rem;',
            'info': 'background-color: rgba(59, 130, 246, 0.5); color: #fff; border-radius: 0.25rem;',
            'secondary': 'background-color: rgba(107, 114, 128, 0.5); color: #fff; border-radius: 0.25rem;',
            'dark': 'background-color: rgba(30, 30, 30, 0.5); color: #fff; border-radius: 0.25rem;'
        };

        let html = `<table class="table table-sm table-bordered"><tbody>`;
        d.backups.forEach(b => {
            const badgeColor = categoryColors[b.category] || 'secondary';
            const badgeStyle = inlineColors[badgeColor] || inlineColors['secondary'];
            const rowClass = b.is_valid ? '' : 'table-danger';
            const validBadge = b.is_valid
                ? `<span class="badge badge-success" style="${inlineColors['success']} font-size: 0.75em; padding: 0.4em 0.6em;">&#10003; Valid</span>`
                : `<span class="badge badge-danger" style="${inlineColors['danger']} font-size: 0.75em; padding: 0.4em 0.6em;">&#10007; Invalid</span>`;
            html += `
                <tr class="${rowClass}">
                    <td style="padding: 8px;">
                        <div style="display: flex; align-items: flex-start; gap: 8px;">
                            <input type="radio" name="dcyBackupSel" style="margin-top: 3px; flex-shrink: 0;"
                                   ${!b.is_valid ? 'disabled' : ''}
                                   onchange="dcySelectBackup('${b.path}','${b.filename}')">
                            <div style="flex: 1; min-width: 0; overflow: hidden;">
                                <div style="font-weight: 500; margin-bottom: 4px; font-size: 0.9em; word-wrap: break-word;">${b.filename}</div>
                                <div style="font-size: 0.8em; opacity: 0.8; display: flex; flex-wrap: wrap; gap: 6px; align-items: center;">
                                    <span>Size: ${b.size_mb} MB</span>
                                    <span>Age: ${b.age_display}</span>
                                    <span class="badge" style="${badgeStyle} font-size: 0.75em; padding: 0.4em 0.6em;">${b.category.replace(/_/g,' ')}</span>
                                    ${validBadge}
                                </div>
                            </div>
                        </div>
                    </td>
                </tr>`;
        });
        html += `</tbody></table>
            <button class="btn btn-warning btn-lg" id="dcyRestoreBtn" onclick="dcyConfirmRestore()" disabled>
                <i class="fas fa-undo"></i> Restore Selected
            </button>`;
        resultsDiv.innerHTML = html;
    } catch(e) {
        resultsDiv.innerHTML = `<div class="alert alert-danger">Error loading backups: ${e.message}</div>`;
    }
}

function dcySelectBackup(path, filename) {
    _dcySelectedBackupPath = path;
    document.getElementById('dcyRestoreBtn').disabled = false;
}

async function dcyConfirmRestore() {
    if (!_dcySelectedBackupPath) return;
    const fname = _dcySelectedBackupPath.split('/').pop();
    const safety = document.getElementById('dcyBackupBeforeRestore').checked;
    if (!confirm(`Restore "${fname}"?\n\nMake sure cli_mount is stopped before proceeding.\n${safety ? 'A safety backup will be created.' : 'No safety backup will be created.'}`)) return;
    const btn = document.getElementById('dcyRestoreBtn');
    btn.disabled = true; btn.textContent = 'Restoring...';
    try {
        const r = await fetch('/debug/api/request_climount_restore', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({backup_path: _dcySelectedBackupPath, create_safety_backup: safety})
        });
        const d = await r.json();
        const resultsDiv = document.getElementById('dcyRestoreResults');
        if (d.success) {
            resultsDiv.innerHTML = `<div style="color:#4ade80;margin-top:8px">&#10003; ${d.message}</div>`;
        } else {
            resultsDiv.innerHTML = `<div style="color:#ff6b6b;margin-top:8px">Error: ${d.error}</div>`;
            btn.disabled = false; btn.textContent = 'Restore Selected';
        }
    } catch(e) {
        document.getElementById('dcyRestoreResults').innerHTML = `<span style="color:#ff6b6b">Error: ${e.message}</span>`;
        btn.disabled = false; btn.textContent = 'Restore Selected';
    }
}

async function dcyScanOldDatabases() {
    const resultsDiv = document.getElementById('dcyCleanupResults');
    resultsDiv.innerHTML = '<em>Scanning...</em>';
    try {
        const r = await fetch('/debug/api/scan_climount_old_databases');
        const d = await r.json();
        if (!d.success) { resultsDiv.innerHTML = `<span style="color:#ff6b6b">Error: ${d.error}</span>`; return; }
        if (!d.files.length) { resultsDiv.innerHTML = '<em>No old backup files found.</em>'; return; }

        const catColors = {recent_backup:'#4ade80', old_version:'#fbbf24', safety_backup:'#60a5fa', corrupted:'#ff6b6b'};
        let html = `<p style="margin-bottom:8px">Found <strong>${d.files.length}</strong> file(s) (${d.total_size_mb} MB total)</p>`;
        html += '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr>'
            + '<th style="padding:4px 8px"><input type="checkbox" onchange="dcyToggleAll(this.checked)"> All</th>'
            + '<th style="text-align:left;padding:4px 8px">File</th>'
            + '<th style="padding:4px 8px">Size</th>'
            + '<th style="padding:4px 8px">Age</th>'
            + '<th style="padding:4px 8px">Type</th>'
            + '</tr></thead><tbody>';
        d.files.forEach(f => {
            const c = catColors[f.category] || '#888';
            html += `<tr style="border-bottom:1px solid #333">
                <td style="padding:4px 8px;text-align:center"><input type="checkbox" class="dcy-cleanup-cb" value="${f.path}"></td>
                <td style="padding:4px 8px;font-family:monospace">${f.filename}</td>
                <td style="padding:4px 8px;text-align:right">${f.size_mb} MB</td>
                <td style="padding:4px 8px;text-align:center">${f.age_display}</td>
                <td style="padding:4px 8px;text-align:center"><span style="color:${c};font-size:11px">${f.category.replace('_',' ')}</span></td>
            </tr>`;
        });
        html += '</tbody></table>';
        html += '<div style="margin-top:10px;display:flex;gap:8px">'
            + '<button type="button" class="btn" onclick="dcyDeleteSelected(true)">Dry Run (Preview)</button>'
            + '<button type="button" class="btn" onclick="dcyDeleteSelected(false)" style="background:#c62828">Delete Selected</button>'
            + '</div><div id="dcyDeleteResult" style="margin-top:8px"></div>';
        resultsDiv.innerHTML = html;
    } catch(e) {
        resultsDiv.innerHTML = `<span style="color:#ff6b6b">Error: ${e.message}</span>`;
    }
}

function dcyToggleAll(checked) {
    document.querySelectorAll('.dcy-cleanup-cb').forEach(cb => cb.checked = checked);
}

async function dcyDeleteSelected(dryRun) {
    const paths = Array.from(document.querySelectorAll('.dcy-cleanup-cb:checked')).map(cb => cb.value);
    if (!paths.length) { alert('Select files to delete first'); return; }
    if (!dryRun && !confirm(`Delete ${paths.length} file(s)? This cannot be undone.`)) return;
    const result = document.getElementById('dcyDeleteResult');
    result.textContent = dryRun ? 'Previewing...' : 'Deleting...';
    try {
        const r = await fetch('/debug/api/delete_climount_old_databases', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({file_paths: paths, dry_run: dryRun})
        });
        const d = await r.json();
        if (d.success) {
            const prefix = dryRun ? '[DRY RUN] Would delete' : 'Deleted';
            result.style.color = '#4ade80';
            result.textContent = `${prefix} ${d.deleted_count} file(s) (${d.total_size_freed_mb} MB freed). Skipped: ${d.skipped_count}.`;
            if (!dryRun) dcyScanOldDatabases();
        } else {
            result.style.color = '#ff6b6b';
            result.textContent = 'Error: ' + d.error;
        }
    } catch(e) {
        result.style.color = '#ff6b6b';
        result.textContent = 'Error: ' + e.message;
    }
}

window.dcyBackupNow = dcyBackupNow;
window.dcyLoadBackupsList = dcyLoadBackupsList;
window.dcySelectBackup = dcySelectBackup;
window.dcyConfirmRestore = dcyConfirmRestore;
window.dcyScanOldDatabases = dcyScanOldDatabases;
window.dcyToggleAll = dcyToggleAll;
window.dcyDeleteSelected = dcyDeleteSelected;

// Auto-load providers when library tab is shown
document.querySelector('[onclick="switchDebugTab(\'library\')"]')?.addEventListener('click', () => {
    if (!document.getElementById('dcy-provider').value) dcyLoadProviders();
});
// Also load on page ready if library tab is active
if (document.getElementById('library-tab')?.classList.contains('active')) dcyLoadProviders();

async function backfillNzbNamesRun() {
    const btn = document.getElementById('backfill-nzb-run-btn');
    const status = document.getElementById('backfill-nzb-status');
    const dryRun = document.getElementById('backfill-nzb-dry-run').checked;
    const force = document.getElementById('backfill-nzb-force').checked;
    btn.disabled = true;
    status.style.display = 'block';
    status.textContent = 'Starting...';
    try {
        const r = await fetch('/debug/api/backfill_nzb_names', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({dry_run: dryRun, force: force})
        });
        const data = await r.json();
        if (!data.success) {
            status.textContent = 'Error: ' + (data.error || 'Unknown error');
            btn.disabled = false;
            return;
        }
        const taskId = data.task_id;
        status.textContent = 'Running... (task ' + taskId + ')';
        // Poll for completion
        const poll = setInterval(async () => {
            const sr = await fetch('/debug/api/backfill_nzb_names/status/' + taskId);
            const sd = await sr.json();
            status.textContent = sd.message || sd.status || 'Running...';
            if (sd.complete) {
                clearInterval(poll);
                btn.disabled = false;
            }
        }, 3000);
    } catch(e) {
        status.textContent = 'Error: ' + e.message;
        btn.disabled = false;
    }
}

window.backfillNzbNamesRun = backfillNzbNamesRun;

function runWatchlistCleanup() {
    const btn = document.getElementById('cleanup-watchlist-btn');
    const result = document.getElementById('cleanup-watchlist-result');
    btn.disabled = true;
    result.textContent = 'Running... check logs for progress.';
    fetch('/debug/api/cleanup_collected_watchlist', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            result.textContent = data.message || (data.success ? 'Started.' : 'Failed.');
        })
        .catch(e => { result.textContent = 'Error: ' + e; })
        .finally(() => { btn.disabled = false; });
}
window.runWatchlistCleanup = runWatchlistCleanup;
