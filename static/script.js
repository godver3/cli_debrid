function toggleDarkMode() {
    document.body.classList.toggle('dark-mode');
    localStorage.setItem('darkMode', document.body.classList.contains('dark-mode'));
    updateDarkModeIcon();
}

function updateDarkModeIcon() {
    const icon = document.getElementById('darkModeIcon');
    if (document.body.classList.contains('dark-mode')) {
        icon.textContent = '☀️';
        icon.title = 'Switch to light mode';
    } else {
        icon.textContent = '🌙';
        icon.title = 'Switch to dark mode';
    }
}

function loadDarkModePreference() {
    if (localStorage.getItem('darkMode') === 'true') {
        document.body.classList.add('dark-mode');
    }
    updateDarkModeIcon();
}

function updateStats() {
    fetch('/api/stats')
        .then(response => response.json())
        .then(data => {
            document.getElementById('total-processed').textContent = data.total_processed;
            document.getElementById('successful-additions').textContent = data.successful_additions;
            document.getElementById('failed-additions').textContent = data.failed_additions;
            let uptime = Math.floor(data.uptime);
            let days = Math.floor(uptime / 86400);
            let hours = Math.floor((uptime % 86400) / 3600);
            let minutes = Math.floor((uptime % 3600) / 60);
            document.getElementById('uptime').textContent = `${days} days, ${hours} hours, ${minutes} minutes`;
        });
}

function updateLogs() {
    fetch('/api/logs')
        .then(response => response.json())
        .then(data => {
            let logEntries = document.getElementById('log-entries');
            if (logEntries) {
                logEntries.innerHTML = data.map(log => `<div class="log-entry">${log}</div>`).join('');
                logEntries.scrollTop = logEntries.scrollHeight;
            }
        });
}

function refreshCurrentPage() {
    if (window.location.pathname === '/statistics' || window.location.pathname === '/') {
        updateStats();
    } else if (window.location.pathname === '/queues') {
        updateQueueContents();
    } else if (window.location.pathname === '/logs') {
        updateLogs();
    }
}

function toggleQueue(element) {
    var items = element.nextElementSibling;
    var queueName = element.querySelector('span').textContent;
    if (items.style.display === "none" || items.style.display === "") {
        items.style.display = "block";
        localStorage.setItem('queue_' + queueName, 'expanded');
    } else {
        items.style.display = "none";
        localStorage.setItem('queue_' + queueName, 'collapsed');
    }
}

function updateQueueContents() {
    fetch('/api/queue_contents')
        .then(response => response.json())
        .then(data => {
            console.log("Fetched queue contents:", data);  // Debug log
            let queueContents = document.getElementById('queue-contents');
            if (queueContents) {
                queueContents.innerHTML = '';
                for (let [queueName, items] of Object.entries(data)) {
                    let queueDiv = document.createElement('div');
                    queueDiv.className = 'queue';
                    let isExpanded = localStorage.getItem('queue_' + queueName) === 'expanded';
                    queueDiv.innerHTML = `
                        <div class="queue-title" onclick="toggleQueue(this)">
                            <span>${queueName}</span>
                            <span class="queue-count">${items.length} items</span>
                        </div>
                        <div class="queue-items" style="display: ${isExpanded ? 'block' : 'none'};">
                            ${items.map(item => `
                                <div class="item">
                                    ${item.title} (${item.year})
                                    ${item.type === 'episode' ? ` S${item.season_number}E${item.episode_number}` : ''}
                                    ${queueName === 'Unreleased' ? ` - Release Date: ${item.release_date}` : ''}
                                </div>
                            `).join('')}
                        </div>
                    `;
                    queueContents.appendChild(queueDiv);
                }
            }
        })
        .catch(error => console.error('Error fetching queue contents:', error));
}

function refreshCurrentPage() {
    if (window.location.pathname === '/statistics' || window.location.pathname === '/') {
        updateStats();
    } else if (window.location.pathname === '/queues') {
        updateQueueContents();
    } else if (window.location.pathname === '/logs') {
        updateLogs();
    }
}

document.addEventListener('DOMContentLoaded', function() {
    loadDarkModePreference();
    
    // Set up auto-refresh
    setInterval(refreshCurrentPage, 5000);  // Refresh every 5 seconds

    // Initial refresh
    refreshCurrentPage();
});