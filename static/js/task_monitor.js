function formatTime(seconds) {
    // Ensure seconds is a non-negative number
    if (typeof seconds !== 'number' || seconds < 0) {
        return 'now'; // Or some other default like 'N/A'
    }
    if (seconds < 1) {
        return '<1s'; // Handle very small times like 0
    }
    if (seconds < 60) {
        return `${Math.round(seconds)}s`;
    } else if (seconds < 3600) {
        return `${Math.round(seconds / 60)}m`;
    } else {
        return `${Math.round(seconds / 3600)}h`;
    }
}

// Define these variables at a higher scope, initialized to null
let currentTaskDisplay = null;
let currentTaskNameElement = null;
let currentTaskTimeElement = null;
let taskListElement = null;
let isTaskMonitorVisible = false; // Track visibility state
let isTaskMonitorInitialized = false; // Add this flag

// Define pause banner elements globally or pass them around
let pauseBannerElement = null;
let pauseReasonTextElement = null;

// NEW: Track if we have auto-hidden the task monitor due to a pause state on mobile
let autoHiddenDueToPause = false;

// NEW: Track if task monitor is disabled due to connectivity failure
let disabledDueToConnectivityFailure = false;

// --- START EDIT: Define Task Priority ---
const TASK_DISPLAY_PRIORITY = [
    'Checking',
    'Scraping',
    'Adding',
    'task_plex_full_scan',
    'task_run_library_maintenance',
    'task_reconcile_queues',
    // Add other important/long tasks if needed
];
// We will also check if a task name ends with '_wanted' for content sources
// --- END EDIT ---

// Assign dropdown element *later*, just before checks/use
let taskMonitorDropdownElement = null;

function updateTaskList(taskList, data) {
    if (!taskList) {
        console.error("Task list element not provided to updateTaskList");
        return;
    }
    // --- START EDIT: Add logging ---
    console.log('[TaskMonitor LOG] updateTaskList received data:', JSON.parse(JSON.stringify(data)));
    // --- END EDIT ---
    if (data.success) {
        const availableTasks = data.tasks || [];
        const runningTaskNamesSet = new Set(data.running_tasks_list || []);

        if (data.running) {
             let runningTasksHtml = '';
             let scheduledTasksHtml = '';
             let finalHtml = '';

             // --- START EDIT: Use pause_info for task list pause message ---
             if (data.paused && data.pause_info && data.pause_info.reason_string) {
                 // --- START EDIT: Add logging ---
                 console.log('[TaskMonitor LOG] updateTaskList: Queue is PAUSED.', data.pause_info.reason_string);
                 // --- END EDIT ---
                 finalHtml = `
                     <div class="task-item paused">
                         <div class="task-name">Queue Paused</div>
                         <div class="task-timing">
                             <span>${data.pause_info.reason_string}</span>
                         </div>
                     </div>`;
                 taskList.innerHTML = finalHtml;
                 // Update the main current task display as well if paused
                 if (currentTaskNameElement) currentTaskNameElement.textContent = 'Queue Paused';
                 if (currentTaskTimeElement) currentTaskTimeElement.textContent = data.pause_info.reason_string.substring(0, 50) + (data.pause_info.reason_string.length > 50 ? '...' : ''); // Show truncated reason
                 if (currentTaskDisplay) currentTaskDisplay.classList.add('paused');
                 return; 
             } else if (currentTaskDisplay && currentTaskDisplay.classList.contains('paused') && !data.paused) {
                 // If it was paused but no longer is, clear the current task display pause state
                 if (currentTaskDisplay) currentTaskDisplay.classList.remove('paused');
             }
             // --- END EDIT ---

             // --- START EDIT: Separate running and scheduled tasks ---
             const runningTaskObjects = [];
             const scheduledTaskObjects = [];
             const availableTaskMap = new Map(availableTasks.map(task => [task.name, task]));

             // Populate runningTaskObjects
             runningTaskNamesSet.forEach(taskName => {
                 const taskDetails = availableTaskMap.get(taskName);
                 if (taskDetails) {
                     runningTaskObjects.push(taskDetails);
                 } else {
                     // Task is running but not in the scheduled list (e.g., manually triggered?)
                     // Add a basic representation
                     runningTaskObjects.push({
                         name: taskName,
                         next_run: null,
                         next_run_timestamp: null,
                         interval: 'N/A',
                         enabled: true // Assume enabled if running
                     });
                 }
             });
             // --- START EDIT: Add logging ---
             console.log('[TaskMonitor LOG] updateTaskList: Running tasks identified:', JSON.parse(JSON.stringify(runningTaskObjects.map(t => t.name))));
             // --- END EDIT ---

             // Populate scheduledTaskObjects (those not currently running)
             availableTasks.forEach(task => {
                 if (!runningTaskNamesSet.has(task.name)) {
                     scheduledTaskObjects.push(task);
                 }
             });

             // Sort scheduled tasks by next run time
             scheduledTaskObjects.sort((a, b) => (a.next_run_timestamp ?? Infinity) - (b.next_run_timestamp ?? Infinity));
             // --- END EDIT ---


             // --- START EDIT: Generate HTML for Running Tasks ---
             if (runningTaskObjects.length > 0) {
                 runningTasksHtml = '<div class="task-list-header">Currently Running</div>';
                 runningTasksHtml += runningTaskObjects.map(task => {
                     // Always show 'Running' status for these
                     const statusText = 'Currently running';
                     const itemClass = 'task-item running'; // Always add 'running' class

                     return `
                         <div class="${itemClass}">
                             <div class="task-name">${task.name}</div>
                             <div class="task-timing">
                                 <span>${statusText}</span>
                                 <span>Interval: ${task.interval ? formatTime(task.interval) : 'N/A'}</span>
                             </div>
                         </div>`;
                 }).join('');
             }
             // --- END EDIT ---


             // --- START EDIT: Generate HTML for Scheduled Tasks ---
             if (scheduledTaskObjects.length > 0) {
                 // Add header only if there were also running tasks, or always? Let's add always for clarity.
                 scheduledTasksHtml = '<div class="task-list-header">Scheduled Tasks</div>';
                 scheduledTasksHtml += scheduledTaskObjects.map(task => {
                     const isEnabled = task.enabled;
                     let statusText = '';
                     let itemClass = 'task-item'; // Never add 'running' class here

                     if (isEnabled && typeof task.next_run === 'number') {
                         statusText = `Next run: ${formatTime(task.next_run)}`;
                     } else if (!isEnabled) {
                         statusText = 'Disabled';
                         itemClass += ' disabled';
                     } else {
                          statusText = 'Scheduled';
                     }

                     return `
                         <div class="${itemClass}">
                             <div class="task-name">${task.name}</div>
                             <div class="task-timing">
                                 <span>${statusText}</span>
                                 <span>Interval: ${task.interval ? formatTime(task.interval) : 'N/A'}</span>
                             </div>
                         </div>`;
                 }).join('');
             }
             // --- END EDIT ---


             // Combine HTML parts
             finalHtml = runningTasksHtml + scheduledTasksHtml;

             // Handle case where no tasks exist at all
             if (finalHtml === '' && !data.paused) {
                 finalHtml = '<div class="no-tasks">No tasks found</div>';
             }

             taskList.innerHTML = finalHtml;

        } else { // Program runner not active
            // --- START EDIT: Add logging ---
            console.log('[TaskMonitor LOG] updateTaskList: Program not running.');
            // --- END EDIT ---
            taskList.innerHTML = '<div class="no-tasks">Program not running</div>';
            if (currentTaskNameElement) currentTaskNameElement.textContent = 'Program Stopped';
            if (currentTaskTimeElement) currentTaskTimeElement.textContent = '';
            if (currentTaskDisplay) currentTaskDisplay.classList.remove('running', 'paused');
        }
    } else { // data.success is false
        taskList.innerHTML = '<div class="error">Failed to load tasks</div>';
        console.warn('Task fetch failed:', data.error);
        if (currentTaskNameElement) currentTaskNameElement.textContent = 'Error Loading Tasks';
        if (currentTaskTimeElement) currentTaskTimeElement.textContent = '';
         if (currentTaskDisplay) currentTaskDisplay.classList.remove('running', 'paused');
    }
}

let eventSource = null;

function setupTaskStream() {
    // --- START: Find pause banner elements ---
    pauseBannerElement = document.getElementById('pauseStatusBanner');
    pauseReasonTextElement = document.getElementById('pauseReasonText');
    if (!pauseBannerElement || !pauseReasonTextElement) {
         console.warn("Pause status banner elements not found. Pause status will not be displayed.");
    }
    // --- END: Find pause banner elements ---

    if (!taskListElement) {
        console.error("Task list element not initialized before setting up stream.");
        return;
    }
    if (eventSource) {
        console.log("setupTaskStream: Closing existing EventSource.");
        eventSource.close();
    }

    console.log("setupTaskStream: Creating new EventSource.");
    eventSource = new EventSource('/base/api/task-stream');

    eventSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            // --- START EDIT: Add logging ---
            console.log('[TaskMonitor LOG] SSE onmessage data received:', JSON.parse(JSON.stringify(data)));
            // --- END EDIT ---

            if (data.success) {
                const tasks = data.tasks || [];
                const isPaused = data.paused;
                const pauseInfo = data.pause_info || { reason_string: null, error_type: null, status_code: null }; // Default if undefined
                const runningTasksList = data.running_tasks_list || [];
                
                // --- START EDIT: Pass pauseInfo to updateTaskDisplay ---
                updateTaskDisplay(tasks, isPaused, pauseInfo, runningTasksList);
                // --- END EDIT ---
                updateTaskList(taskListElement, data); // updateTaskList will also use data.pause_info

                // --- START EDIT: Update Pause Banner with new logic ---
                if (pauseBannerElement && pauseReasonTextElement) {
                    if (isPaused && pauseInfo && pauseInfo.reason_string) {
                        let displayMessage = `Queue Paused: ${pauseInfo.reason_string}`; // Default detailed message

                        if (pauseInfo.error_type === 'UNAUTHORIZED' || pauseInfo.status_code === 401) {
                            const serviceName = pauseInfo.service_name || 'Debrid service';
                            displayMessage = `Queue Paused: ${serviceName} API Key is invalid or unauthorized. Please check your settings.`;
                        } else if (pauseInfo.error_type === 'FORBIDDEN' || pauseInfo.status_code === 403) {
                            const serviceName = pauseInfo.service_name || 'Debrid service';
                            displayMessage = `Queue Paused: ${serviceName} API access forbidden. Check API key, IP, or account status.`;
                        } else if (pauseInfo.error_type === 'CONNECTION_ERROR') {
                             // Keep the detailed reason_string for general connection errors as it includes retry counts.
                            displayMessage = `Queue Paused: ${pauseInfo.reason_string}. This will likely resolve on its own.`;
                        }
                        // Add more conditions for other error_types (RATE_LIMIT, DB_HEALTH, SYSTEM_SCHEDULED) if needed for banner.
                        // For now, they will use the default pauseInfo.reason_string.

                        pauseReasonTextElement.textContent = displayMessage;
                        pauseBannerElement.classList.remove('hidden');
                    } else {
                        pauseBannerElement.classList.add('hidden');
                    }
                }
                // --- END EDIT ---

            } else {
                console.error('Task stream reported an error:', data.error);
                displayError('Error fetching task status.');
                // NEW: Trigger connectivity failure for stream errors
                updateTaskDisplay([], true, { reason_string: 'Failed to fetch task status from server', error_type: 'CONNECTION_ERROR', status_code: 500 }, []);
                if (pauseBannerElement) pauseBannerElement.classList.add('hidden');
            }
        } catch (error) {
            console.error('Error parsing task stream data:', error);
            displayError('Error parsing task data.');
            // NEW: Trigger connectivity failure for parsing errors
            updateTaskDisplay([], true, { reason_string: 'Failed to parse task data from server', error_type: 'CONNECTION_ERROR', status_code: 500 }, []);
             if (pauseBannerElement) pauseBannerElement.classList.add('hidden');
        }
    };

    eventSource.onerror = (error) => {
        console.error('Task stream connection error:', error);
        displayError('Connection error with task stream.');
        // NEW: Trigger connectivity failure for SSE connection errors
        updateTaskDisplay([], true, { reason_string: 'Lost connection to task stream server', error_type: 'CONNECTION_ERROR', status_code: 500 }, []);
        if (pauseBannerElement) pauseBannerElement.classList.add('hidden');
        // Optionally, you might want to attempt to re-establish the EventSource connection here
        // For simplicity, this example doesn't include reconnection logic.
        if (eventSource) {
            eventSource.close();
            // Consider a delay before retrying setupTaskStream()
            // setTimeout(setupTaskStream, 5000); // e.g., retry after 5 seconds
        }
    };
}

// --- START: Move toggleTaskMonitor outside initializeTaskMonitor ---
function toggleTaskMonitor() {
    // NEW: Prevent toggling if disabled due to connectivity failure
    if (disabledDueToConnectivityFailure) {
        console.log('[TaskMonitor LOG] Toggle ignored: Task monitor disabled due to connectivity failure');
        return;
    }

    const container = document.getElementById('taskMonitorContainer');
    // Ensure container exists before proceeding
    if (!container) {
        console.error("Task monitor container not found in toggleTaskMonitor.");
        return;
    }
    
    const toggleButton = document.getElementById('taskMonitorToggle');
    const icon = toggleButton ? toggleButton.querySelector('i') : null;

    // Toggle visibility state variable
    isTaskMonitorVisible = !isTaskMonitorVisible; 
    container.classList.toggle('visible', isTaskMonitorVisible);

    // Update toggle icon if it exists
    if (icon) {
        const isMobile = window.innerWidth <= 1045;
        if (isMobile) {
            // For bottom bar: UP to show, DOWN to hide
            icon.classList.toggle('fa-chevron-up', !isTaskMonitorVisible);
            icon.classList.toggle('fa-chevron-down', isTaskMonitorVisible);
        } else {
            // For top bar: DOWN to show, UP to hide
            icon.classList.toggle('fa-chevron-down', !isTaskMonitorVisible);
            icon.classList.toggle('fa-chevron-up', isTaskMonitorVisible);
        }
    }
    
    // Call the global body padding update function
    if (typeof window.updateBodyPaddingForTopOverlays === 'function') {
        window.updateBodyPaddingForTopOverlays();
    } else {
        console.error('updateBodyPaddingForTopOverlays function not found.');
    }
    
    // --- START EDIT: Add bottom padding adjustment for mobile ---
    if (window.innerWidth <= 1045) {
        if (isTaskMonitorVisible) {
            document.body.classList.add('has-bottom-overlay');
        } else {
            document.body.classList.remove('has-bottom-overlay');
        }
    }
    // --- END EDIT ---
    
    // Optional: Persist visibility state if needed (handled by toggleTaskMonitorVisibility?)
    // localStorage.setItem('taskMonitorHidden', !isTaskMonitorVisible);
}
// --- END: Move toggleTaskMonitor outside initializeTaskMonitor ---

export function initializeTaskMonitor() {
    // --- START: Add Initialization Guard ---
    if (isTaskMonitorInitialized) {
        console.warn("initializeTaskMonitor called again after already initialized. Skipping.");
        return; 
    }
    // --- END: Add Initialization Guard ---

    // Check if we're on a page that should have the task monitor
    const body = document.querySelector('body');
    const isUserSystemEnabled = body.dataset.userSystemEnabled === 'true';
    const isOnboarding = body.dataset.isOnboarding === 'true';
    const taskMonitorContainer = document.getElementById('taskMonitorContainer');
    
    if (!taskMonitorContainer || isOnboarding) {
        console.log("Task monitor container not found or on onboarding page. Skipping initialization.");
        return; // Skip if monitor shouldn't be present
    }
    
    console.log("Attempting to initialize Task Monitor..."); // Log start

    // Assign the globally scoped variables here
    currentTaskDisplay = document.getElementById('currentTaskDisplay');
    taskListElement = document.getElementById('taskList');
    const taskMonitorToggle = document.getElementById('taskMonitorToggle');

    // Assign dropdown element *later*, just before checks/use
    taskMonitorDropdownElement = document.getElementById('taskMonitorDropdown'); 

    // Perform the combined check for essential elements
    if (!currentTaskDisplay || !taskListElement || !taskMonitorToggle || !taskMonitorDropdownElement) {
         // Log which element is missing specifically
         if (!currentTaskDisplay) console.error("Task Monitor Init Failed: #currentTaskDisplay not found.");
         if (!taskListElement) console.error("Task Monitor Init Failed: #taskList not found.");
         if (!taskMonitorToggle) console.error("Task Monitor Init Failed: #taskMonitorToggle not found.");
         if (!taskMonitorDropdownElement) console.error("Task Monitor Init Failed: #taskMonitorDropdown not found."); // This might log now
         return; // Stop if any essential element is missing
    }

    // Get the child elements *after* confirming currentTaskDisplay exists
    currentTaskNameElement = currentTaskDisplay.querySelector('.current-task-name');
    currentTaskTimeElement = currentTaskDisplay.querySelector('.current-task-time');

    // Also check if these child elements were found
    if (!currentTaskNameElement || !currentTaskTimeElement) {
        console.error("Task name or time element within current task display not found.");
    }

    // --- START: Set Initialization Flag ---
    // Set the flag *after* basic checks but before adding listeners/starting stream
    isTaskMonitorInitialized = true;
    console.log("Task Monitor Initializing Now...");
    // --- END: Set Initialization Flag ---

    // --- START: Load initial visibility state ---
    // Determine initial visibility (e.g., based on localStorage or default to hidden)
    const startVisible = localStorage.getItem('taskMonitorHidden') !== 'true'; // Default to visible if not set or false
    isTaskMonitorVisible = startVisible; // Set the module-scope variable
    if (startVisible) {
         taskMonitorContainer.classList.add('visible');
    } else {
         taskMonitorContainer.classList.remove('visible');
    }
    // --- START EDIT: Adjust body bottom padding for initial visibility on mobile ---
    if (window.innerWidth <= 1045) {
        if (startVisible) {
            document.body.classList.add('has-bottom-overlay');
        } else {
            document.body.classList.remove('has-bottom-overlay');
        }
    }
    // --- END EDIT ---
    // Update icon based on initial state
    const initialIcon = taskMonitorToggle.querySelector('i');
    if(initialIcon){
        const isMobile = window.innerWidth <= 1045;
        if (isMobile) {
            // For bottom bar: UP to show, DOWN to hide
            initialIcon.classList.toggle('fa-chevron-up', !isTaskMonitorVisible);
            initialIcon.classList.toggle('fa-chevron-down', isTaskMonitorVisible);
        } else {
            // For top bar: DOWN to show, UP to hide
            initialIcon.classList.toggle('fa-chevron-down', !isTaskMonitorVisible); 
            initialIcon.classList.toggle('fa-chevron-up', isTaskMonitorVisible); 
        }
    }
    // --- END: Load initial visibility state ---

    // Set initial dropdown state - safe because we checked taskMonitorDropdownElement above
    taskMonitorDropdownElement.classList.remove('dropdown-visible'); 

    // --- START: Event Listeners ---
    if (currentTaskDisplay) { // Check currentTaskDisplay separately is fine
        currentTaskDisplay.addEventListener('click', toggleDropdownVisibility); 
        currentTaskDisplay.setAttribute('aria-haspopup', 'true'); 
        // Now we are sure taskMonitorDropdownElement exists due to the check above
        currentTaskDisplay.setAttribute('aria-expanded', taskMonitorDropdownElement.classList.contains('dropdown-visible')); 
    } else {
        // This branch shouldn't be reached if the earlier check worked, but kept for safety
        console.warn("Current task display area not found during init (post-check).");
    }
    
    if (taskMonitorToggle) { // Check taskMonitorToggle separately is fine
        taskMonitorToggle.addEventListener('click', () => {
            toggleTaskMonitor(); // Toggle the main container
            
            const container = document.getElementById('taskMonitorContainer'); // Re-fetch container needed here
            
            // Persist state
            if (container) {
                localStorage.setItem('taskMonitorHidden', !container.classList.contains('visible'));
            }
            
            // If hiding the container, ensure the dropdown is also marked as not visible
            // Add checks here as well for robustness
            if (taskMonitorDropdownElement && container && !container.classList.contains('visible')) { 
                taskMonitorDropdownElement.classList.remove('dropdown-visible');
                if (currentTaskDisplay) { // Check currentTaskDisplay again before use
                    currentTaskDisplay.setAttribute('aria-expanded', 'false');
                }
            }
        }); 
    } else {
        // This branch shouldn't be reached if the earlier check worked
         console.warn("Task monitor toggle button not found during init (post-check).");
    }
    // --- END: Event Listeners ---

    // --- START: Add Guard and Logging for setupTaskStream ---
    // Clean up existing source just in case initialize is somehow called multiple times
    if (eventSource) { 
        console.warn("initializeTaskMonitor: Found existing EventSource (should not happen with guard). Closing it.");
        eventSource.close();
        eventSource = null;
    }
    console.log("initializeTaskMonitor: Calling setupTaskStream().");
    setupTaskStream(); // Start the SSE connection
    // --- END: Add Guard and Logging for setupTaskStream ---
    
    // Clean up when the page is unloaded
    window.addEventListener('beforeunload', () => {
        // --- START: Add Logging to beforeunload ---
        console.log("beforeunload: Event triggered.");
        if (eventSource) {
            console.log(`beforeunload: Closing EventSource (readyState: ${eventSource.readyState})`);
            eventSource.close();
            console.log("beforeunload: EventSource closed.");
        } else {
            console.log("beforeunload: No active EventSource to close.");
        }
        // --- END: Add Logging to beforeunload ---
    });

    // Initial body padding check after setup
    if (typeof window.updateBodyPaddingForTopOverlays === 'function') {
        // isTaskMonitorVisible is already set based on initial load
        window.updateBodyPaddingForTopOverlays(); 
    }

    console.log("Task Monitor Initialization Complete."); // Log end
}

// --- START EDIT: Rewrite updateTaskDisplay to handle list ---
function updateTaskDisplay(scheduledTasks, isPaused, pauseInfo, runningTasksList) {
    // --- START EDIT: Add logging ---
    console.log('[TaskMonitor LOG] updateTaskDisplay called. Paused:', isPaused, 'PauseInfo:', JSON.parse(JSON.stringify(pauseInfo)), 'RunningTasksList:', JSON.parse(JSON.stringify(runningTasksList)), 'ScheduledTasks:', JSON.parse(JSON.stringify(scheduledTasks.map(t => t.name))));
    // --- END EDIT ---
    if (!currentTaskDisplay || !currentTaskNameElement || !currentTaskTimeElement) {
        console.warn("Task display elements not found during update.");
        return;
    }

    const effectivePauseInfo = pauseInfo || { reason_string: null, error_type: null, status_code: null };

    // NEW: Check for severe connectivity failures that should disable the task monitor
    const isConnectivityFailure = effectivePauseInfo.error_type === 'CONNECTION_ERROR' || 
                                 effectivePauseInfo.error_type === 'UNAUTHORIZED' || 
                                 effectivePauseInfo.error_type === 'FORBIDDEN' ||
                                 (effectivePauseInfo.status_code && (effectivePauseInfo.status_code >= 500 || effectivePauseInfo.status_code === 401 || effectivePauseInfo.status_code === 403));

    // NEW: Handle connectivity failure - disable task monitor
    if (isPaused && isConnectivityFailure && !disabledDueToConnectivityFailure) {
        console.log('[TaskMonitor LOG] Connectivity failure detected, disabling task monitor');
        disableTaskMonitor();
        disabledDueToConnectivityFailure = true;
        return;
    }

    // NEW: Re-enable task monitor if it was disabled due to connectivity and connectivity is restored
    if (disabledDueToConnectivityFailure && (!isPaused || !isConnectivityFailure)) {
        console.log('[TaskMonitor LOG] Connectivity restored, re-enabling task monitor');
        enableTaskMonitor();
        disabledDueToConnectivityFailure = false;
    }

    // NEW: If task monitor is disabled due to connectivity, don't process further updates
    if (disabledDueToConnectivityFailure) {
        return;
    }

    if (isPaused) {
        // --- START EDIT: Add logging ---
        console.log('[TaskMonitor LOG] updateTaskDisplay: Displaying PAUSED state.');
        // --- END EDIT ---
        currentTaskNameElement.textContent = 'Queue Paused';
        let reasonForDisplay = effectivePauseInfo.reason_string || 'Processing is currently paused.';
        if (effectivePauseInfo.error_type === 'UNAUTHORIZED') {
            reasonForDisplay = 'API Key Unauthorized. Check Settings.';
        } else if (effectivePauseInfo.error_type === 'FORBIDDEN') {
            reasonForDisplay = 'API Access Forbidden. Check Settings/IP.';
        } else if (effectivePauseInfo.error_type === 'CONNECTION_ERROR' && effectivePauseInfo.reason_string) {
            reasonForDisplay = effectivePauseInfo.reason_string;
        }
        currentTaskTimeElement.textContent = reasonForDisplay.substring(0, 50) + (reasonForDisplay.length > 50 ? '...' : '');
        currentTaskDisplay.classList.add('paused');
        currentTaskDisplay.classList.remove('running');

        // NEW: On mobile, automatically hide the task monitor container so it does not overlap the paused banner
        if (window.innerWidth <= 1045 && !autoHiddenDueToPause) {
            const container = document.getElementById('taskMonitorContainer');
            if (container && container.classList.contains('visible')) {
                toggleTaskMonitor(); // reuse existing toggle logic
                autoHiddenDueToPause = true;
            }
        }
        return;
    } else {
        currentTaskDisplay.classList.remove('paused');

        // NEW: If the monitor was auto-hidden while paused, restore it when unpaused
        if (autoHiddenDueToPause && window.innerWidth <= 1045) {
            const container = document.getElementById('taskMonitorContainer');
            if (container && !container.classList.contains('visible')) {
                toggleTaskMonitor();
            }
            autoHiddenDueToPause = false;
        }
    }

    if (runningTasksList && runningTasksList.length > 0) {
        // Since only one task can be running, we can just use the first one
        const runningTask = runningTasksList[0];
        const taskDetails = scheduledTasks.find(st => st.name === runningTask);
        
        currentTaskNameElement.textContent = runningTask;
        if (taskDetails && taskDetails.interval) {
            currentTaskTimeElement.textContent = `Running (Interval: ${formatTime(taskDetails.interval)})`;
        } else {
            currentTaskTimeElement.textContent = 'Running';
        }
        currentTaskDisplay.classList.add('running');
    } else if (scheduledTasks && scheduledTasks.length > 0) {
        let nextTask = null;
        let minNextRun = Infinity;

        scheduledTasks.forEach(task => {
            if (task.enabled && typeof task.next_run === 'number' && task.next_run < minNextRun) {
                minNextRun = task.next_run;
                nextTask = task;
            }
        });

        if (nextTask) {
            // --- START EDIT: Add logging ---
            console.log('[TaskMonitor LOG] updateTaskDisplay: Displaying next scheduled task:', nextTask.name, 'Time:', formatTime(nextTask.next_run));
            // --- END EDIT ---
            currentTaskNameElement.textContent = nextTask.name;
            currentTaskTimeElement.textContent = `in ${formatTime(nextTask.next_run)}`;
        } else {
            // --- START EDIT: Add logging ---
            console.log('[TaskMonitor LOG] updateTaskDisplay: No enabled upcoming tasks.');
            // --- END EDIT ---
            currentTaskNameElement.textContent = 'No upcoming tasks';
            currentTaskTimeElement.textContent = '';
        }
        currentTaskDisplay.classList.remove('running');
    } else {
        // --- START EDIT: Add logging ---
        console.log('[TaskMonitor LOG] updateTaskDisplay: No tasks scheduled.');
        // --- END EDIT ---
        currentTaskNameElement.textContent = 'No tasks scheduled';
        currentTaskTimeElement.textContent = '';
        currentTaskDisplay.classList.remove('running');
    }
}
// --- END EDIT ---

// Add helper function for displaying errors in the task monitor if it doesn't exist
function displayError(message) {
    if (taskListElement) {
        taskListElement.innerHTML = `<div class="error">${message}</div>`;
    }
    // Also clear the main display
    if (currentTaskNameElement) currentTaskNameElement.textContent = 'Error';
    if (currentTaskTimeElement) currentTaskTimeElement.textContent = '';
    if (currentTaskDisplay) {
        currentTaskDisplay.classList.remove('running', 'paused');
    }
}

// Ensure toggleDropdownVisibility function is defined correctly:
function toggleDropdownVisibility() {
    // NEW: Prevent dropdown interaction if disabled due to connectivity failure
    if (disabledDueToConnectivityFailure) {
        console.log('[TaskMonitor LOG] Dropdown toggle ignored: Task monitor disabled due to connectivity failure');
        return;
    }

    if (!taskMonitorDropdownElement || !currentTaskDisplay) {
        console.error("Task monitor dropdown or display element not found in toggleDropdownVisibility.");
        return;
    }
    // Only toggle the dropdown if the main container is currently visible
    const container = document.getElementById('taskMonitorContainer');
    if (container && container.classList.contains('visible')) {
        taskMonitorDropdownElement.classList.toggle('dropdown-visible');
        // Update the aria attribute for accessibility
        const isExpanded = taskMonitorDropdownElement.classList.contains('dropdown-visible');
        currentTaskDisplay.setAttribute('aria-expanded', isExpanded);
    } else {
        console.log("Dropdown toggle ignored: Main container is hidden.");
    }
}

// NEW: Helper functions to disable/enable task monitor components
function disableTaskMonitor() {
    const container = document.getElementById('taskMonitorContainer');
    const toggleButton = document.getElementById('taskMonitorToggle');
    const rateLimitsToggle = document.getElementById('rateLimitsSectionToggle');
    
    if (container) {
        container.style.display = 'none';
        container.classList.remove('visible');
        // Also remove any dropdown visibility
        if (taskMonitorDropdownElement) {
            taskMonitorDropdownElement.classList.remove('dropdown-visible');
        }
    }
    
    // NEW: Only hide toggle buttons on mobile with !important
    const isMobile = window.innerWidth <= 1045;
    
    if (toggleButton && isMobile) {
        toggleButton.style.setProperty('display', 'none', 'important');
    }
    
    // NEW: Also hide the rate limits toggle button during connectivity failure (mobile only)
    if (rateLimitsToggle && isMobile) {
        rateLimitsToggle.style.setProperty('display', 'none', 'important');
    }
    
    // Remove any bottom padding that might be applied
    if (window.innerWidth <= 1045) {
        document.body.classList.remove('has-bottom-overlay');
    }
    
    // Reset auto-hide flag since we're force-disabling
    autoHiddenDueToPause = false;
    
    console.log('[TaskMonitor LOG] Task monitor and toggle buttons disabled due to connectivity failure');
}

function enableTaskMonitor() {
    const container = document.getElementById('taskMonitorContainer');
    const toggleButton = document.getElementById('taskMonitorToggle');
    const rateLimitsToggle = document.getElementById('rateLimitsSectionToggle');
    
    if (container) {
        container.style.display = '';
        // Restore previous visibility state from localStorage
        const wasHidden = localStorage.getItem('taskMonitorHidden') === 'true';
        if (!wasHidden) {
            container.classList.add('visible');
            isTaskMonitorVisible = true;
            if (window.innerWidth <= 1045) {
                document.body.classList.add('has-bottom-overlay');
            }
        } else {
            isTaskMonitorVisible = false;
        }
    }
    
    // NEW: Only restore toggle buttons on mobile
    const isMobile = window.innerWidth <= 1045;
    
    if (toggleButton && isMobile) {
        toggleButton.style.removeProperty('display');
        // Restore proper icon state
        const icon = toggleButton.querySelector('i');
        if (icon) {
            // Clear existing classes first
            icon.classList.remove('fa-chevron-up', 'fa-chevron-down');
            
            // For bottom bar: UP to show, DOWN to hide
            icon.classList.add(isTaskMonitorVisible ? 'fa-chevron-down' : 'fa-chevron-up');
        }
    } else if (toggleButton && !isMobile) {
        // Desktop: restore normally without mobile-specific logic
        toggleButton.style.display = '';
        const icon = toggleButton.querySelector('i');
        if (icon) {
            // Clear existing classes first
            icon.classList.remove('fa-chevron-up', 'fa-chevron-down');
            
            // For top bar: DOWN to show, UP to hide
            icon.classList.add(isTaskMonitorVisible ? 'fa-chevron-up' : 'fa-chevron-down');
        }
    }
    
    // NEW: Also restore the rate limits toggle button (mobile only)
    if (rateLimitsToggle && isMobile) {
        rateLimitsToggle.style.removeProperty('display');
    } else if (rateLimitsToggle && !isMobile) {
        // Desktop: restore normally
        rateLimitsToggle.style.display = '';
    }
    
    console.log('[TaskMonitor LOG] Task monitor and toggle buttons re-enabled after connectivity restored');
}

// --- START: Modify DOMContentLoaded Listener ---
document.addEventListener('DOMContentLoaded', () => {
    console.log("DOMContentLoaded event fired."); // Log when event fires
    initializeTaskMonitor(); 
}); 
// --- END: Modify DOMContentLoaded Listener --- 