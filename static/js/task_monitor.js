function formatTime(seconds) {
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

    if (tasks && tasks.length > 0) {
        const nextTask = tasks.reduce((a, b) => a.next_run < b.next_run ? a : b);
        taskNameElement.textContent = nextTask.name;
        taskTimeElement.textContent = `in ${formatTime(nextTask.next_run)}`;
    } else {
        taskNameElement.textContent = 'No active tasks';
        taskTimeElement.textContent = '';
    }
}

let consecutiveErrors = 0;
const MAX_CONSECUTIVE_ERRORS = 3;
let updateInterval = null;

async function updateTasks(taskList, silent = false) {
    try {
        const response = await fetch('/base/api/current-task');
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();
        consecutiveErrors = 0; // Reset error counter on success

        if (data.success) {
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
                }
                
                if (data.tasks && data.tasks.length > 0) {
                    const tasksHtml = data.tasks
                        .sort((a, b) => a.next_run - b.next_run)
                        .map(task => `
                            <div class="task-item ${task.running ? 'running' : ''}">
                                <div class="task-name">${task.name}</div>
                                <div class="task-timing">
                                    <span>${task.running ? 'Currently running' : `Next run: ${formatTime(task.next_run)}`}</span>
                                    <span>Interval: ${formatTime(task.interval)}</span>
                                </div>
                            </div>
                        `)
                        .join('');
                    
                    // If paused, append tasks after the pause message
                    if (data.paused && data.pause_reason) {
                        taskList.innerHTML += tasksHtml;
                    } else {
                        taskList.innerHTML = tasksHtml;
                    }
                    
                    // Update current task display with either running task or next scheduled task
                    const runningTask = data.tasks.find(t => t.running);
                    const nextTask = runningTask || data.tasks.reduce((a, b) => a.next_run < b.next_run ? a : b);
                    updateCurrentTaskDisplay([nextTask]);
                    
                    // Restore normal polling interval if we were in error state
                    if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS && updateInterval) {
                        clearInterval(updateInterval);
                        updateInterval = setInterval(() => updateTasks(taskList), 2000);
                    }
                } else {
                    const status = 'Program not running';
                    if (data.paused && data.pause_reason) {
                        taskList.innerHTML = `
                            <div class="task-item paused">
                                <div class="task-name">Queue Paused</div>
                                <div class="task-timing">
                                    <span>${data.pause_reason}</span>
                                </div>
                            </div>
                            <div class="no-tasks">${status}</div>`;
                    } else {
                        taskList.innerHTML = `<div class="no-tasks">${status}</div>`;
                    }
                    updateCurrentTaskDisplay(null);
                }
            } else {
                taskList.innerHTML = '<div class="no-tasks">Program not running</div>';
                updateCurrentTaskDisplay(null);
            }
        } else {
            if (!silent) {
                taskList.innerHTML = '<div class="error">Failed to load tasks</div>';
                console.warn('Task fetch failed:', data.error);
            }
            updateCurrentTaskDisplay(null);
        }
    } catch (error) {
        consecutiveErrors++;
        
        if (!silent) {
            console.warn('Error fetching tasks:', error.message);
            taskList.innerHTML = '<div class="error">Connection lost. Retrying...</div>';
        }
        
        updateCurrentTaskDisplay(null);

        // If we've had too many consecutive errors, slow down the polling
        if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS && updateInterval) {
            clearInterval(updateInterval);
            updateInterval = setInterval(() => updateTasks(taskList, true), 1000); // Retry every 1 second
        }
    }
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
        const isVisible = taskMonitorDropdown.style.display === 'block';
        taskMonitorDropdown.style.display = isVisible ? 'none' : 'block';
        currentTaskDisplay.classList.toggle('active', !isVisible);
    }

    function toggleTaskMonitorVisibility() {
        taskMonitorContainer.classList.toggle('visible');
        localStorage.setItem('taskMonitorHidden', !taskMonitorContainer.classList.contains('visible'));
    }

    currentTaskDisplay.addEventListener('click', toggleTaskMonitor);
    refreshTasksButton.addEventListener('click', () => {
        consecutiveErrors = 0; // Reset error counter on manual refresh
        clearInterval(updateInterval);
        updateTasks(taskList);
        updateInterval = setInterval(() => updateTasks(taskList), 1000);
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

    // Start updating tasks
    updateTasks(taskList);
    updateInterval = setInterval(() => updateTasks(taskList), 1000);
} 