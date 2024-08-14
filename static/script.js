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

function updateSettings(event) {
    event.preventDefault();
    
    let formData = new FormData(event.target);
    let settings = {};

    for (let [key, value] of formData.entries()) {
        let keys = key.split('.');
        let current = settings;
        for (let i = 0; i < keys.length - 1; i++) {
            if (!(keys[i] in current)) {
                current[keys[i]] = {};
            }
            current = current[keys[i]];
        }
        if (value === 'true') {
            value = true;
        } else if (value === 'false') {
            value = false;
        } else if (!isNaN(value) && value !== '') {
            value = Number(value);
        }
        current[keys[keys.length - 1]] = value;
    }

    fetch('/settings', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(settings)
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            displaySuccess('Settings saved successfully!');
        } else {
            displayError('Error saving settings.');
        }
    })
    .catch((error) => {
        console.error('Error:', error);
        displayError('Error saving settings.');
    });
}

function displaySuccess(message) {
    const saveStatus = document.getElementById('saveStatus');
    saveStatus.textContent = message;
    saveStatus.style.color = 'green';
}

function displayError(message) {
    const saveStatus = document.getElementById('saveStatus');
    saveStatus.textContent = message;
    saveStatus.style.color = 'red';
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
    // We've removed the else if for '/scraper' as it's now handled in the HTML file
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
                const queueOrder = ['Upgrading', 'Wanted', 'Scraping', 'Adding', 'Checking', 'Sleeping', 'Unreleased', 'Blacklisted'];
                
                queueOrder.forEach(queueName => {
                    if (data[queueName] && data[queueName].length > 0) {
                        let queueDiv = document.createElement('div');
                        queueDiv.className = 'queue';
                        let isExpanded = localStorage.getItem('queue_' + queueName) === 'expanded';
                        
                        queueDiv.innerHTML = `
                            <div class="queue-title" onclick="toggleQueue(this)">
                                <span>${queueName}</span>
                                <span class="queue-count">${data[queueName].length} items</span>
                            </div>
                            <div class="queue-items" style="display: ${isExpanded ? 'block' : 'none'};">
                                ${queueName === 'Blacklisted' ? generateConsolidatedItems(data[queueName], 'Blacklisted') :
                                  queueName === 'Unreleased' ? generateConsolidatedItems(data[queueName], 'Unreleased') :
                                  generateRegularItems(queueName, data[queueName])}
                            </div>
                        `;
                        queueContents.appendChild(queueDiv);
                    }
                });
            }
        })
        .catch(error => console.error('Error fetching queue contents:', error));
}

function generateConsolidatedItems(items, queueName) {
    let consolidatedItems = {};
    items.forEach(item => {
        let key = `${item.title}_${item.year}`;
        if (!consolidatedItems[key]) {
            consolidatedItems[key] = {
                title: item.title,
                year: item.year,
                versions: new Set(),
                seasons: new Set(),
                release_date: item.release_date
            };
        }
        consolidatedItems[key].versions.add(item.version);
        if (item.type === 'episode') {
            consolidatedItems[key].seasons.add(item.season_number);
        }
    });

    return Object.values(consolidatedItems).map(item => `
        <div class="item">
            ${item.title} (${item.year}) - Version(s): ${Array.from(item.versions).join(', ')}
            ${item.seasons.size > 0 ? ` - Season(s): ${Array.from(item.seasons).join(', ')}` : ''}
            ${queueName === 'Unreleased' ? ` - Release Date: ${item.release_date}` : ''}
        </div>
    `).join('');
}

function generateRegularItems(queueName, items) {
    return items.map(item => `
        <div class="item">
            ${item.title} (${item.year}) - Version: ${item.version || 'Unknown'}
            ${item.type === 'episode' ? ` S${item.season_number}E${item.episode_number}` : ''}
            ${queueName === 'Upgrading' ? `
                - Current Quality: ${item.current_quality || 'N/A'}
                - Target Quality: ${item.target_quality || 'N/A'}
                - Time Added: ${item.time_added ? new Date(item.time_added).toLocaleString() : 'N/A'}
                - Upgrades Found: ${item.upgrades_found || 0}
            ` : ''}
            ${queueName === 'Sleeping' ? `
                - Wake Count: ${item.wake_count !== undefined ? item.wake_count : 'N/A'}
            ` : ''}
        </div>
    `).join('');
}

function generateBlacklistedItems(items) {
    let consolidatedItems = {};
    items.forEach(item => {
        let key = `${item.title}_${item.year}`;
        if (!consolidatedItems[key]) {
            consolidatedItems[key] = {
                title: item.title,
                year: item.year,
                versions: new Set(),
                seasons: new Set()
            };
        }
        consolidatedItems[key].versions.add(item.version);
        if (item.type === 'episode') {
            consolidatedItems[key].seasons.add(item.season_number);
        }
    });

    return Object.values(consolidatedItems).map(item => `
        <div class="item">
            ${item.title} (${item.year}) - Version(s): ${Array.from(item.versions).join(', ')}
            ${item.seasons.size > 0 ? ` - Season(s): ${Array.from(item.seasons).join(', ')}` : ''}
        </div>
    `).join('');
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

function searchMedia(event) {
    event.preventDefault();
    const searchTerm = document.querySelector('input[name="search_term"]').value;
    const version = document.querySelector('select[name="version"]').value;
    fetch('/scraper', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: `search_term=${encodeURIComponent(searchTerm)}&version=${encodeURIComponent(version)}`
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            displaySearchResults(data.results, version);
        }
    })
    .catch(error => {
        console.error('Error:', error);
        displayError('An error occurred while searching.');
    });
}

function selectMedia(mediaId, title, year, mediaType, season, episode, multi) {
    const version = document.getElementById('version-select').value;
    let formData = new FormData();
    formData.append('media_id', mediaId);
    formData.append('title', title);
    formData.append('year', year);
    formData.append('media_type', mediaType);
    if (season !== null) formData.append('season', season);
    if (episode !== null) formData.append('episode', episode);
    formData.append('multi', multi);
    formData.append('version', version);

    fetch('/select_media', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            displayTorrentResults(data.torrent_results, title, year, version);
        }
    })
    .catch(error => {
        console.error('Error:', error);
        displayError('An error occurred while selecting media.');
    });
}

function addToRealDebrid(magnetLink) {
    fetch('/add_to_real_debrid', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: `magnet_link=${encodeURIComponent(magnetLink)}`
    })
    .then(response => {
        if (!response.ok) {
            throw new Error('Network response was not ok');
        }
        return response.json();
    })
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            displaySuccess(data.message);
        }
    })
    .catch(error => {
        console.error('Error:', error);
        displayError('An error occurred while adding to Real-Debrid.');
    });
}

function displayError(message) {
    const resultsDiv = document.getElementById('results');
    resultsDiv.innerHTML = `<p style="color: red;">${message}</p>`;
}

function displaySuccess(message) {
    const resultsDiv = document.getElementById('results');
    resultsDiv.innerHTML = `<p style="color: green;">${message}</p>`;
}

function displaySearchResults(results) {
    const resultsDiv = document.getElementById('results');
    let html = '<h3>Search Results</h3><table border="1"><thead><tr><th>Title</th><th>Year</th><th>Type</th><th>Action</th></tr></thead><tbody>';
    for (let item of results) {
        html += `
            <tr>
                <td>${item.title}</td>
                <td>${item.year}</td>
                <td>${item.media_type}</td>
                <td>
                    <button onclick="selectMedia('${item.id}', '${item.title}', '${item.year}', '${item.media_type}', ${item.season || 'null'}, ${item.episode || 'null'}, ${item.multi})">Select</button>
                </td>
            </tr>
        `;
    }
    html += '</tbody></table>';
    resultsDiv.innerHTML = html;
}

function displayTorrentResults(results, title, year) {
    const resultsDiv = document.getElementById('results');
    let html = `<h3>Torrent Results for ${title} (${year})</h3><table border="1"><thead><tr><th>Name</th><th>Size</th><th>Source</th><th>Cached</th><th>Score</th><th>Action</th></tr></thead><tbody>`;
    for (let torrent of results) {
        html += `
            <tr>
                <td>${torrent.title}</td>
                <td>${(torrent.size).toFixed(1)} GB</td>
                <td>${torrent.source}</td>
                <td>${torrent.cached}</td>
                <td>${torrent.score_breakdown.total_score}</td>
                <td>
                    <button onclick="addToRealDebrid('${torrent.magnet}')">Add to Real-Debrid</button>
                </td>
            </tr>
        `;
    }
    html += '</tbody></table>';
    resultsDiv.innerHTML = html;
}

function openTab(event, tabName) {
    // Hide all tab contents
    const tabContents = document.querySelectorAll('.tab-content');
    tabContents.forEach(content => {
        content.style.display = 'none';
    });

    // Remove 'active' class from all tab buttons
    const tabButtons = document.querySelectorAll('.tab-button');
    tabButtons.forEach(button => {
        button.classList.remove('active');
    });

    // Show the selected tab content
    document.getElementById(tabName).style.display = 'block';

    // Add 'active' class to the clicked button
    event.currentTarget.classList.add('active');
}

document.addEventListener('DOMContentLoaded', function() {
    loadDarkModePreference();
    
    // Set up auto-refresh
    setInterval(refreshCurrentPage, 5000);  // Refresh every 5 seconds

    // Add event listener for search form
    const searchForm = document.getElementById('search-form');
    if (searchForm) {
        searchForm.addEventListener('submit', searchMedia);
    }

    // Database-specific functionality
    const columnForm = document.getElementById('column-form');
    const filterForm = document.getElementById('filter-form');

    if (columnForm) {
        columnForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const formData = new FormData(columnForm);
            fetch('/database', {
                method: 'POST',
                body: formData
            }).then(() => {
                window.location.reload();
            });
        });
    }

    if (filterForm) {
        filterForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const formData = new FormData(filterForm);
            const params = new URLSearchParams(formData);
            window.location.href = '/database?' + params.toString();
        });
    }

    // Handle alphabetical pagination
    const paginationLinks = document.querySelectorAll('.pagination a');
    paginationLinks.forEach(link => {
        link.addEventListener('click', function(e) {
            e.preventDefault();
            const letter = this.textContent;
            const currentUrl = new URL(window.location.href);
            currentUrl.searchParams.set('letter', letter);
            window.location.href = currentUrl.toString();
        });
    });

    // Add event listener for settings form
    const settingsForm = document.getElementById('settingsForm');
    if (settingsForm) {
        settingsForm.addEventListener('submit', updateSettings);
    }
    
    // Add event listeners for tab buttons
    const tabButtons = document.querySelectorAll('.tab-button');
    tabButtons.forEach(button => {
        button.addEventListener('click', function(event) {
            openTab(event, this.getAttribute('data-tab'));
        });
    });

    // Initial refresh
    refreshCurrentPage();
});