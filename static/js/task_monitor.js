function formatTime(seconds) {
    // Ensure seconds is a non-negative number
    if (typeof seconds !== 'number' || seconds < 0) {
        return 'soon'; // Or some other default like 'N/A'
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

function updateCurrentTaskDisplay(tasks) {
    const currentTaskDisplay = document.getElementById('currentTaskDisplay');
    if (!currentTaskDisplay) return;

    const taskNameElement = currentTaskDisplay.querySelector('.current-task-name');
    const taskTimeElement = currentTaskDisplay.querySelector('.current-task-time');

    // Clear previous state
    taskNameElement.textContent = '';
    taskTimeElement.textContent = '';

    if (tasks && tasks.length > 0) {
        let nextTask = null;
        let minNextRun = Infinity;

        // Find the task with the smallest non-null, non-negative next_run time
        for (const task of tasks) {
            // Ensure next_run is a valid number >= 0
            if (typeof task.next_run === 'number' && task.next_run >= 0 && task.next_run < minNextRun) {
                minNextRun = task.next_run;
                nextTask = task;
            }
        }

        if (nextTask) {
            // Found a task with a scheduled next run
            taskNameElement.textContent = nextTask.name;
            // Use the found minimum time for formatting
            taskTimeElement.textContent = `...in ${formatTime(minNextRun)}`; 
        } else {
            // No scheduled next run found, check for a running task
            const runningTask = tasks.find(task => task.running === true);
            if (runningTask) {
                taskNameElement.textContent = runningTask.name;
                taskTimeElement.textContent = '(Running)';
            } else {
                // No scheduled or running tasks, but the list is not empty
                taskNameElement.textContent = 'No active tasks'; // Or maybe 'Queue Idle'?
            }
        }
    } else {
        // tasks array is null or empty
        taskNameElement.textContent = 'No tasks';
    }
}

function updateTaskList(taskList, data) {
    if (data.success) {
        // Determine the list of tasks to potentially display and use for current task calculation
        const availableTasks = data.tasks || [];

        if (data.running) {
            if (data.paused && data.pause_reason) {
                // Show pause reason at the top of the task list
                taskList.innerHTML = `
                    <div class="task-item paused">
                        <div class="task-name">Queue Paused</div>
                        <div class="task-timing">
                            <span>${data.pause_reason}</span>
                        </div>
                    </div>`;
            } else {
                 // Clear task list if not paused (will be repopulated below if tasks exist)
                 taskList.innerHTML = '';
            }
            
            if (availableTasks.length > 0) {
                const tasksHtml = availableTasks
                    .sort((a, b) => (a.next_run ?? Infinity) - (b.next_run ?? Infinity)) // Sort by next_run, handling nulls
                    .map(task => `
                        <div class="task-item ${task.running ? 'running' : ''}">
                            <div class="task-name">${task.name}</div>
                            <div class="task-timing">
                                <span>${task.running ? 'Currently running' : (typeof task.next_run === 'number' ? `Next run: ${formatTime(task.next_run)}` : 'Scheduled')}</span>
                                <span>Interval: ${formatTime(task.interval)}</span>
                            </div>
                        </div>
                    `)
                    .join('');
                
                // Append tasks (after pause message if present)
                taskList.innerHTML += tasksHtml;

            } else if (!data.paused) { // Only show 'No tasks' if not paused and no tasks
                taskList.innerHTML = '<div class="no-tasks">No tasks scheduled</div>';
            }
            
            // Update current task display using the available tasks
            updateCurrentTaskDisplay(availableTasks);

        } else { // Program not running
            taskList.innerHTML = '<div class="no-tasks">Program not running</div>';
            updateCurrentTaskDisplay(null);
        }
    } else {
        taskList.innerHTML = '<div class="error">Failed to load tasks</div>';
        console.warn('Task fetch failed:', data.error);
        updateCurrentTaskDisplay(null);
    }
}

let eventSource = null;

function setupTaskStream(taskList) {
    if (eventSource) {
        eventSource.close();
    }

    eventSource = new EventSource('/base/api/task-stream');
    
    eventSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            updateTaskList(taskList, data);
        } catch (error) {
            console.error('Error parsing task data:', error);
            taskList.innerHTML = '<div class="error">Error parsing task data</div>';
            updateCurrentTaskDisplay(null);
        }
    };

    eventSource.onerror = (error) => {
        console.error('EventSource failed:', error);
        taskList.innerHTML = '<div class="error">Connection lost. Reconnecting...</div>';
        updateCurrentTaskDisplay(null);
        
        // EventSource will automatically try to reconnect
    };
}

export function initializeTaskMonitor() {
    // Check if we're on a page that should have the task monitor
    const body = document.querySelector('body');
    const isUserSystemEnabled = body.dataset.userSystemEnabled === 'true';
    const isOnboarding = body.dataset.isOnboarding === 'true';
    const taskMonitorContainer = document.getElementById('taskMonitorContainer');
    
    if (!taskMonitorContainer || isOnboarding) {
        return;
    }

    const currentTaskDisplay = document.getElementById('currentTaskDisplay');
    const taskMonitorDropdown = document.getElementById('taskMonitorDropdown');
    const refreshTasksButton = document.getElementById('refreshTasksButton');
    const taskList = document.getElementById('taskList');
    const taskMonitorToggle = document.getElementById('taskMonitorToggle');

    if (!currentTaskDisplay || !taskMonitorDropdown || !refreshTasksButton || !taskList || !taskMonitorToggle) {
        return;
    }

    // Initialize dropdown to be closed
    taskMonitorDropdown.style.display = 'none';

    // Load the visibility state from localStorage
    const isHidden = localStorage.getItem('taskMonitorHidden') === 'true';
    
    // Small delay to ensure smooth transition
    requestAnimationFrame(() => {
        if (!isHidden) {
            taskMonitorContainer.classList.add('visible');
        }
    });

    function toggleTaskMonitor() {
        const dropdown = document.getElementById('taskMonitorDropdown');
        if (dropdown) {
            dropdown.style.display = dropdown.style.display === 'none' ? 'block' : 'none';
        }
    }

    function toggleTaskMonitorVisibility() {
        taskMonitorContainer.classList.toggle('visible');
        localStorage.setItem('taskMonitorHidden', !taskMonitorContainer.classList.contains('visible'));
    }

    currentTaskDisplay.addEventListener('click', toggleTaskMonitor);
    refreshTasksButton.addEventListener('click', () => {
        // For SSE, we'll close and reopen the connection
        setupTaskStream(taskList);
    });
    taskMonitorToggle.addEventListener('click', toggleTaskMonitorVisibility);

    // Close dropdown when clicking outside
    document.addEventListener('click', function(e) {
        if (!taskMonitorDropdown.contains(e.target) && 
            !currentTaskDisplay.contains(e.target) && 
            taskMonitorDropdown.style.display === 'block') {
            toggleTaskMonitor();
        }
    });

    // Start the SSE connection
    setupTaskStream(taskList);
    
    // Clean up when the page is unloaded
    window.addEventListener('beforeunload', () => {
        if (eventSource) {
            eventSource.close();
        }
    });
} 