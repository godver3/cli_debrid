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
    if (data.success) {
        const availableTasks = data.tasks || []; // All scheduled tasks with details
        const runningTaskNamesSet = new Set(data.running_tasks_list || []); // Set of names for running tasks

        if (data.running) { // Check if the overall program runner is active
             let runningTasksHtml = '';
             let scheduledTasksHtml = '';
             let finalHtml = '';

             // Add pause message if paused - this takes priority
             if (data.paused && data.pause_reason) {
                 finalHtml = `
                     <div class="task-item paused">
                         <div class="task-name">Queue Paused</div>
                         <div class="task-timing">
                             <span>${data.pause_reason}</span>
                         </div>
                     </div>`;
                 taskList.innerHTML = finalHtml;
                 return; // Don't show task lists if paused
             }

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
            taskList.innerHTML = '<div class="no-tasks">Program not running</div>';
        }
    } else { // data.success is false
        taskList.innerHTML = '<div class="error">Failed to load tasks</div>';
        console.warn('Task fetch failed:', data.error);
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

            if (data.success) {
                const tasks = data.tasks || [];
                const isPaused = data.paused;
                const pauseReason = data.pause_reason;
                // --- START EDIT: Get list and pass to updateTaskDisplay ---
                const runningTasksList = data.running_tasks_list || [];
                console.log('[Task Stream] Received running_tasks_list:', runningTasksList);

                // Pass the list to updateTaskDisplay
                updateTaskDisplay(tasks, isPaused, pauseReason, runningTasksList);
                // Also pass the correct data to updateTaskList
                updateTaskList(taskListElement, data);
                // --- END EDIT ---

                // --- START: Update Pause Banner ---
                if (pauseBannerElement && pauseReasonTextElement) {
                    if (isPaused) {
                        pauseReasonTextElement.textContent = `Queue Paused: ${pauseReason || 'Unknown reason'}`;
                        pauseBannerElement.classList.remove('hidden');
                        // No need to manage body class here, that's for top overlays
                    } else {
                        pauseBannerElement.classList.add('hidden');
                    }
                }
                // --- END: Update Pause Banner ---

            } else {
                console.error('Task stream reported an error:', data.error);
                displayError('Error fetching task status.');
                // --- START EDIT: Pass empty list on error ---
                updateTaskDisplay([], false, null, []);
                // --- END EDIT ---
                if (pauseBannerElement) pauseBannerElement.classList.add('hidden');
            }
        } catch (error) {
            console.error('Error parsing task stream data:', error);
            displayError('Error parsing task data.');
            // --- START EDIT: Pass empty list on error ---
            updateTaskDisplay([], false, null, []);
            // --- END EDIT ---
             if (pauseBannerElement) pauseBannerElement.classList.add('hidden');
        }
    };

    eventSource.onerror = (error) => {
        console.error('EventSource failed:', error);
        if (taskListElement) {
             taskListElement.innerHTML = '<div class="error">Connection lost. Reconnecting...</div>';
        }
        updateTaskDisplay([], false, null, []);
         if (pauseBannerElement) pauseBannerElement.classList.add('hidden');
        // EventSource will automatically try to reconnect
    };
}

// --- START: Move toggleTaskMonitor outside initializeTaskMonitor ---
function toggleTaskMonitor() {
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
        icon.classList.toggle('fa-chevron-down', !isTaskMonitorVisible); 
        icon.classList.toggle('fa-chevron-up', isTaskMonitorVisible); 
    }
    
    // Call the global body padding update function
    if (typeof window.updateBodyPaddingForTopOverlays === 'function') {
        window.updateBodyPaddingForTopOverlays();
    } else {
        console.error('updateBodyPaddingForTopOverlays function not found.');
    }
    
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
    // Update icon based on initial state
    const initialIcon = taskMonitorToggle.querySelector('i');
    if(initialIcon){
        initialIcon.classList.toggle('fa-chevron-down', !isTaskMonitorVisible); 
        initialIcon.classList.toggle('fa-chevron-up', isTaskMonitorVisible); 
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
function updateTaskDisplay(scheduledTasks, isPaused, pauseReason, runningTasksList) {
    if (!currentTaskDisplay || !currentTaskNameElement || !currentTaskTimeElement) {
        console.warn("Task display elements not found during update.");
        return;
    }

    // --- START EDIT: Clear tooltip initially ---
    currentTaskDisplay.removeAttribute('title');
    // --- END EDIT ---

    // Priority 1: Check for Paused State
    if (isPaused) {
        currentTaskNameElement.textContent = `Queue Paused`;
        currentTaskTimeElement.textContent = pauseReason || 'No reason specified';
        currentTaskDisplay.classList.add('paused');
        currentTaskDisplay.classList.remove('running');
        return;
    } else {
        currentTaskDisplay.classList.remove('paused');
    }

    // Priority 2: Check for Currently Running Tasks
    if (runningTasksList && runningTasksList.length > 0) {
        let taskToShow = null;
        let isPriorityTask = false;

        // Find the highest priority task running
        for (const priority of TASK_DISPLAY_PRIORITY) {
            if (runningTasksList.includes(priority)) {
                taskToShow = priority;
                isPriorityTask = true;
                break;
            }
        }
        if (!taskToShow) {
            for (const runningTask of runningTasksList) {
                if (runningTask.endsWith('_wanted')) {
                    taskToShow = runningTask;
                    isPriorityTask = true; // Treat content sources as priority
                    break;
                }
            }
        }
        if (!taskToShow) {
            taskToShow = runningTasksList[0];
        }

        // Construct display name
        let displayName = `Running: ${taskToShow}`;
        const otherTasksCount = runningTasksList.length - 1;
        if (otherTasksCount > 0) {
            displayName += ` (+${otherTasksCount} other)`;
            // --- START EDIT: Add tooltip when multiple tasks run ---
            const allRunningTasksString = runningTasksList.join(', ');
            currentTaskDisplay.setAttribute('title', `Running Tasks: ${allRunningTasksString}`);
            // --- END EDIT ---
        }

        currentTaskNameElement.textContent = displayName;
        currentTaskTimeElement.textContent = 'In Progress...';
        currentTaskDisplay.classList.add('running');
        currentTaskDisplay.classList.remove('paused');
        return;
    } else {
        currentTaskDisplay.classList.remove('running');
        // Ensure tooltip is removed if no tasks are running
        currentTaskDisplay.removeAttribute('title');
    }

    // Priority 3: Show Next Scheduled Task
    const upcomingTasks = scheduledTasks
        .filter(task => task.enabled && task.next_run_timestamp)
        .sort((a, b) => a.next_run_timestamp - b.next_run_timestamp);

    if (upcomingTasks.length > 0) {
        const nextTask = upcomingTasks[0];
        currentTaskNameElement.textContent = `Next: ${nextTask.name}`;

        if (nextTask.next_run !== null) {
            const timeUntilNext = nextTask.next_run;
            if (timeUntilNext <= 1) {
                 currentTaskTimeElement.textContent = 'Due now';
            } else if (timeUntilNext < 60) {
                currentTaskTimeElement.textContent = `in ${Math.round(timeUntilNext)}s`;
            } else {
                currentTaskTimeElement.textContent = `in ${Math.round(timeUntilNext / 60)}m`;
            }
        } else {
             currentTaskTimeElement.textContent = 'Scheduled';
        }
    } else {
        currentTaskNameElement.textContent = 'No upcoming tasks';
        currentTaskTimeElement.textContent = '';
    }
    // Ensure tooltip is removed when showing next task
    currentTaskDisplay.removeAttribute('title');
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

// --- START: Modify DOMContentLoaded Listener ---
document.addEventListener('DOMContentLoaded', () => {
    console.log("DOMContentLoaded event fired."); // Log when event fires
    initializeTaskMonitor(); 
}); 
// --- END: Modify DOMContentLoaded Listener --- 