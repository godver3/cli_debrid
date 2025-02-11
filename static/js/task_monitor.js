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

function updateTaskList(taskList, data) {
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