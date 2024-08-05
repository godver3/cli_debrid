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

function searchMedia(event) {
    event.preventDefault();
    const searchTerm = document.querySelector('input[name="search_term"]').value;
    fetch('/scraper', {  // Changed from '/search' to '/scraper'
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: `search_term=${encodeURIComponent(searchTerm)}`
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            displaySearchResults(data.results);
        }
    })
    .catch(error => {
        console.error('Error:', error);
        displayError('An error occurred while searching.');
    });
}

function selectMedia(mediaId, title, year, mediaType, season, episode, multi) {
    let formData = new FormData();
    formData.append('media_id', mediaId);
    formData.append('title', title);
    formData.append('year', year);
    formData.append('media_type', mediaType);
    if (season !== null) formData.append('season', season);
    if (episode !== null) formData.append('episode', episode);
    formData.append('multi', multi);

    fetch('/select_media', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            displayTorrentResults(data.torrent_results, title, year);
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
    let html = `<h3>Torrent Results for ${title} (${year})</h3><table border="1"><thead><tr><th>Name</th><th>Size</th><th>Source</th><th>Action</th></tr></thead><tbody>`;
    for (let torrent of results) {
        html += `
            <tr>
                <td>${torrent.title}</td>
                <td>${torrent.size}</td>
                <td>${torrent.source}</td>
                <td>
                    <button onclick="addToRealDebrid('${torrent.magnet}')">Add to Real-Debrid</button>
                </td>
            </tr>
        `;
    }
    html += '</tbody></table>';
    resultsDiv.innerHTML = html;
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

    // Initial refresh
    refreshCurrentPage();
});