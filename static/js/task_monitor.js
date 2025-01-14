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
    const taskNameElement = currentTaskDisplay.querySelector('.current-task-name');
    const taskTimeElement = currentTaskDisplay.querySelector('.current-task-time');

    if (tasks && tasks.length > 0) {
        // Find the task with the lowest next_run time
        const nextTask = tasks.reduce((a, b) => a.next_run < b.next_run ? a : b);
        taskNameElement.textContent = nextTask.name;
        taskTimeElement.textContent = `in ${formatTime(nextTask.next_run)}`;
    } else {
        taskNameElement.textContent = 'No active tasks';
        taskTimeElement.textContent = '';
    }
}

async function updateTasks(taskList) {
    try {
        const response = await fetch('/base/api/current-task');
        const data = await response.json();

        if (data.success) {
            if (data.running && data.tasks && data.tasks.length > 0) {
                const tasksHtml = data.tasks
                    .sort((a, b) => a.next_run - b.next_run)
                    .map(task => `
                        <div class="task-item">
                            <div class="task-name">${task.name}</div>
                            <div class="task-timing">
                                <span>Next run: ${formatTime(task.next_run)}</span>
                                <span>Interval: ${formatTime(task.interval)}</span>
                            </div>
                        </div>
                    `)
                    .join('');
                taskList.innerHTML = tasksHtml;
                
                // Update the current task display
                updateCurrentTaskDisplay(data.tasks);
            } else {
                const status = data.running ? 'No active tasks' : 'Program not running';
                taskList.innerHTML = `<div class="no-tasks">${status}</div>`;
                
                // Update current task display for no tasks
                updateCurrentTaskDisplay(null);
            }
        } else {
            taskList.innerHTML = '<div class="error">Failed to load tasks</div>';
            console.error('Task fetch failed:', data.error);
            updateCurrentTaskDisplay(null);
        }
    } catch (error) {
        console.error('Error fetching tasks:', error);
        taskList.innerHTML = '<div class="error">Failed to load tasks</div>';
        updateCurrentTaskDisplay(null);
    }
}

export function initializeTaskMonitor() {
    const taskMonitorContainer = document.getElementById('taskMonitorContainer');
    const currentTaskDisplay = document.getElementById('currentTaskDisplay');
    const taskMonitorDropdown = document.getElementById('taskMonitorDropdown');
    const refreshTasksButton = document.getElementById('refreshTasksButton');
    const taskList = document.getElementById('taskList');
    const taskMonitorToggle = document.getElementById('taskMonitorToggle');
    let updateInterval;

    if (!taskMonitorContainer || !currentTaskDisplay || !taskMonitorDropdown || !refreshTasksButton || !taskList || !taskMonitorToggle) {
        console.error('Required task monitor elements not found');
        return;
    }

    // Load the visibility state from localStorage
    const isHidden = localStorage.getItem('taskMonitorHidden') === 'true';
    if (isHidden) {
        taskMonitorContainer.classList.add('hidden');
    }

    function toggleTaskMonitor() {
        const isVisible = taskMonitorDropdown.style.display === 'block';
        taskMonitorDropdown.style.display = isVisible ? 'none' : 'block';
        currentTaskDisplay.classList.toggle('active', !isVisible);
    }

    function toggleTaskMonitorVisibility() {
        taskMonitorContainer.classList.toggle('hidden');
        // Save the state to localStorage
        localStorage.setItem('taskMonitorHidden', taskMonitorContainer.classList.contains('hidden'));
    }

    currentTaskDisplay.addEventListener('click', toggleTaskMonitor);
    refreshTasksButton.addEventListener('click', () => {
        updateTasks(taskList);
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