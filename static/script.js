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
    document.body.classList.add('dark-mode');
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

setInterval(refreshCurrentPage, 1000);  // Refresh every 5 seconds

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
                
                let hasContent = false;
                queueOrder.forEach(queueName => {
                    if (data[queueName] && data[queueName].length > 0) {
                        hasContent = true;
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

                if (!hasContent) {
                    queueContents.innerHTML = '<p>All queues are currently empty.</p>';
                }
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
    if (window.location.pathname === '/queues') {
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

function displaySeasonInfo(title, season_num, air_date, season_overview, poster_path, genre_ids, vote_average, backdrop_path, show_overview) {
    const seasonInfo = document.getElementById('season-info');
    seasonInfo.innerHTML = `
        <div class="season-info-container">
            <span class="show-rating">${(vote_average).toFixed(1)}</span>
            <img src="https://image.tmdb.org/t/p/w300${poster_path}" alt="${title} Season ${season_num}" class="season-poster">
            <div class="season-details">
                <h2>${title} - Season ${season_num}</h2>
                <p>${genre_ids}</p>
                <div class="season-overview">
                    <p>${season_overview ? season_overview : show_overview}</p>
                </div>
            </div>
        </div>
        <div class="season-bg-image" style="background-image: url('https://image.tmdb.org/t/p/w1920_and_h800_multi_faces${backdrop_path}');"></div>
    `;

}



function selectSeason(mediaId, title, year, mediaType, season, episode, multi, genre_ids, vote_average, backdrop_path, show_overview) {
    const resultsDiv = document.getElementById('seasonResults');
    const dropdown = document.getElementById('seasonDropdown');
    const seasonPackButton = document.getElementById('seasonPackButton');
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

    fetch('/select_season', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            const seasonResults = data.results;

            dropdown.innerHTML = '';
            seasonResults.forEach(item => {
                const option = document.createElement('option');
                option.value = JSON.stringify(item);
                option.textContent = `Season: ${item.season_num}`;
                dropdown.appendChild(option);
            });

            dropdown.addEventListener('change', function() {
                const selectedItem = JSON.parse(this.value);
                displaySeasonInfo(selectedItem.title, selectedItem.season_num, selectedItem.air_date, selectedItem.season_overview, selectedItem.poster_path, genre_ids, vote_average, backdrop_path, show_overview);
                selectEpisode(selectedItem.id, selectedItem.title, selectedItem.year, selectedItem.media_type, selectedItem.season_num, null, selectedItem.multi);
            });

            seasonPackButton.onclick = function() {
                const selectedItem = JSON.parse(dropdown.value);
                selectMedia(selectedItem.id, selectedItem.title, selectedItem.year, selectedItem.media_type, selectedItem.season_num, null, selectedItem.multi);
            };

            resultsDiv.style.display = 'block';

            // Trigger initial selection
            if (dropdown.options.length > 0) {
                dropdown.selectedIndex = 0;
                dropdown.dispatchEvent(new Event('change'));
            }
        }
    })
    .catch(error => {
        console.error('Error:', error);
        displayError('An error occurred while selecting media.');
    });
}

function selectEpisode(mediaId, title, year, mediaType, season, episode, multi) {
    const version = document.getElementById('version-select').value;
    let formData = new FormData();
    formData.append('media_id', mediaId);
    formData.append('title', title);
    formData.append('year', year);
    formData.append('media_type', mediaType);
    formData.append('season', season);
    if (episode !== null) formData.append('episode', episode);
    formData.append('multi', multi);
    formData.append('version', version);

    fetch('/select_episode', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            displayError(data.error);
        } else {
            displayEpisodeResults(data.episodeResults, title, year, version);
        }
    })
    .catch(error => {
        console.error('Error:', error);
        displayError('An error occurred while selecting media.');
    });
}
async function selectMedia(mediaId, title, year, mediaType, season, episode, multi) {
    showLoadingState(); // Show loading state before fetching results
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
        displayTorrentResults(data.torrent_results, title, year, version);
    })
    .catch(error => {
        console.error('Error:', error);
    });
}

function addToRealDebrid(magnetLink) {
    showLoadingState();
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
    hideLoadingState();
    const overlayContent = document.getElementById('overlayStatus');
    overlayContent.innerHTML = `<p style="color: red;">${message}</p>`;
}

function displaySuccess(message) {
    hideLoadingState();
    const overlayContent = document.getElementById('overlayStatus');
    overlayContent.innerHTML = `<p style="color: green;">${message}</p>`;
}

function showLoadingState() {
    // Create and display loading indicator
    const loadingIndicator = document.createElement('div');
    loadingIndicator.id = 'loadingIndicator';
    loadingIndicator.style.position = 'fixed';
    loadingIndicator.style.top = '50%';
    loadingIndicator.style.left = '50%';
    loadingIndicator.style.transform = 'translate(-50%, -50%)';
    loadingIndicator.style.zIndex = '1000';
    loadingIndicator.innerHTML = '<img src="/static/loadingimage.gif" alt="Loading..." style="width: 100px; height: 100px;">';
    document.body.appendChild(loadingIndicator);

    // Disable all buttons
    const buttons = document.getElementsByTagName('button');
    for (let button of buttons) {
        button.disabled = true;
        button.style.opacity = '0.5';
    }
    
    const selecter = document.getElementsByTagName('select');
    for (let select of selecter) {
        select.disabled = true;
        select.style.opacity = '0.5';
    }

    const episodeDiv = document.getElementsByClassName('episode');
    for (let episode of episodeDiv) {
        episode.style.opacity = '0.5';
        //episode.onclick = false;
    }
}

// Function to hide loading state and re-enable buttons
function hideLoadingState() {
    // Remove loading indicator
    const loadingIndicator = document.getElementById('loadingIndicator');
    if (loadingIndicator) {
        loadingIndicator.remove();
    }

    // Re-enable all buttons
    const buttons = document.getElementsByTagName('button');
    for (let button of buttons) {
        button.disabled = false;
        button.style.opacity = '1';
    }
    
    const selecter = document.getElementsByTagName('select');
    for (let select of selecter) {
        select.disabled = false;
        select.style.opacity = '1';
    }

    const episodeDiv = document.getElementsByClassName('episode');
    for (let episode of episodeDiv) {
        //episode.onclick = true;
        episode.style.opacity = '1';
    }
}


function displayEpisodeResults(episodeResults, title, year) {
    toggleResultsVisibility(true);
    const episodeResultsDiv = document.getElementById('episodeResults');
    episodeResultsDiv.innerHTML = '';
    
    // Create a container for the grid layout
    const gridContainer = document.createElement('div');
    gridContainer.style.display = 'flex';
    gridContainer.style.flexWrap = 'wrap';
    gridContainer.style.gap = '20px';
    const mediaQuery = window.matchMedia('(max-width: 1024px)');
    function handleScreenChange(e) {
        if (e.matches) {
            gridContainer.style.justifyContent = 'center';
        } else {
            gridContainer.style.justifyContent = 'flex-start';
        }
    }
    mediaQuery.addListener(handleScreenChange);
    handleScreenChange(mediaQuery);

    episodeResults.forEach(item => {
        const episodeDiv = document.createElement('div');
        episodeDiv.className = 'episode';
        var options = {year: 'numeric', month: 'long', day: 'numeric' };
        var date  = new Date(item.air_date);
        episodeDiv.innerHTML = `        
            <button><span class="episode-rating">${(item.vote_average).toFixed(1)}</span>
            <img src="${item.still_path ? `https://image.tmdb.org/t/p/w300${item.still_path}` : `static/noimage-cli.png`}" alt="${item.episode_title}" style="width: 100%; height: auto;">
            <div class="episode-info">
                <h2 class="episode-title">${item.episode_num}. ${item.episode_title}</h2>
                <p class="episode-sub">${date.toLocaleDateString("en-US", options)}</p>
            </div></button>
        `;
        episodeDiv.onclick = function() {
            selectMedia(item.id, item.title, item.year, item.media_type, item.season_num, item.episode_num, item.multi);
        };
        gridContainer.appendChild(episodeDiv);
    });

    episodeResultsDiv.appendChild(gridContainer);
}




function toggleResultsVisibility(showDetailed) {
    const searchResult = document.getElementById('searchResult');
    const seasonResults = document.getElementById('seasonResults');
    const dropdown = document.getElementById('seasonDropdown');
    const seasonPackButton = document.getElementById('seasonPackButton');
    const episodeResultsDiv = document.getElementById('episodeResults');
    if (showDetailed) {
        searchResult.style.display = 'none';
        seasonResults.style.display = 'block';
        dropdown.style.display = 'block';
        seasonPackButton.style.display = 'block';
        episodeResultsDiv.style.display = 'block';
    } else {
        searchResult.style.display = 'block';
        seasonResults.style.display = 'none';
        episodeResultsDiv.style.display = 'none';
    }
}

function displaySearchResults(searchResult) {
    toggleResultsVisibility(false);
    const searchResultsDiv = document.getElementById('searchResult');
    searchResultsDiv.innerHTML = '';
    
    // Create a container for the grid layout
    const gridContainer = document.createElement('div');
    gridContainer.style.display = 'flex';
    gridContainer.style.flexWrap = 'wrap';
    gridContainer.style.gap = '20px';
    const mediaQuery = window.matchMedia('(max-width: 1024px)');
    function handleScreenChange(e) {
        if (e.matches) {
            gridContainer.style.justifyContent = 'center';
        } else {
            gridContainer.style.justifyContent = 'flex-start';
        }
    }
    mediaQuery.addListener(handleScreenChange);
    handleScreenChange(mediaQuery);

    searchResult.forEach(item => {
        if (item.year) {
            const searchResDiv = document.createElement('div');
            searchResDiv.className = 'sresult';
            searchResDiv.innerHTML = `
                <button>${item.media_type === 'tv' ? '<span class="mediatype-tv">TV</span>' : '<span class="mediatype-mv">MOVIE</span>'}
                <img src="https://image.tmdb.org/t/p/w600_and_h900_bestv2${item.poster_path}" alt="${item.episode_title}" style="width: 100%; height: auto;">
                <div class="searchresult-info">
                    <h2 class="searchresult-item">${item.title} (${item.year})</h2>
                </div></button>                
            `;        
            searchResDiv.onclick = function() {
                //selectMedia(item.id, item.title, item.year, item.media_type, item.season_num, item.episode_num, item.multi);
                if (item.media_type === 'movie') {
                    selectMedia(item.id, item.title, item.year, item.media_type, item.season || 'null', item.episode || 'null', item.multi);
                } else {
                    selectSeason(item.id, item.title, item.year, item.media_type, item.season || 'null', item.episode || 'null', item.multi, item.genre_ids, item.vote_average, item.backdrop_path, item.show_overview);
                }
            };
            gridContainer.appendChild(searchResDiv);
        }
    });

    searchResultsDiv.appendChild(gridContainer);
}

function displayTorrentResults(data, title, year) {
    hideLoadingState();
    const overlay = document.getElementById('overlay');

    const mediaQuery = window.matchMedia('(max-width: 1024px)');
    function handleScreenChange(e) {
        if (e.matches) {
            const overlayContentRes = document.getElementById('overlayContentRes');
            overlayContentRes.innerHTML = `<h3>Torrent Results for ${title} (${year})</h3>`;
            const gridContainer = document.createElement('div');
            gridContainer.style.display = 'flex';
            gridContainer.style.flexWrap = 'wrap';
            gridContainer.style.gap = '15px';
            gridContainer.style.justifyContent = 'center';

            data.forEach(torrent => {
                const torResDiv = document.createElement('div');
                torResDiv.className = 'torresult';
                torResDiv.style.border = '1px solid white';
                torResDiv.innerHTML = `
                    <button>
                    <div class="torresult-info">
                        <p class="torresult-title">${torrent.title}</p>
                        <p class="torresult-item">${(torrent.size).toFixed(1)} GB |${torrent.cached ? ` ${torrent.cached} |` : ''} ${torrent.score_breakdown.total_score}</p>
                        <p class="torresult-item">${torrent.source}</p>
                    </div>
                    </button>                
                `;        
                torResDiv.onclick = function() {
                    addToRealDebrid(torrent.magnet)
                };
                gridContainer.appendChild(torResDiv);
            });

            overlayContentRes.appendChild(gridContainer);
        } else {
            const overlayContent = document.getElementById('overlayContent');
            overlayContent.innerHTML = `<h3>Torrent Results for ${title} (${year})</h3>`;
            // Create table element
            const table = document.createElement('table');
            table.style.width = '100%';
            table.style.borderCollapse = 'collapse';

            // Create table header
            const thead = document.createElement('thead');
            thead.innerHTML = `
                <tr>
                    <th style="border: 1px solid #ddd; padding: 8px; color: rgb(191 191 190);">Name</th>
                    <th style="border: 1px solid #ddd; padding: 8px; color: rgb(191 191 190);">Size</th>
                    <th style="border: 1px solid #ddd; padding: 8px; color: rgb(191 191 190);">Source</th>
                    <th style="border: 1px solid #ddd; padding: 8px; color: rgb(191 191 190);">Cached</th>
                    <th style="border: 1px solid #ddd; padding: 8px; color: rgb(191 191 190);">Score</th>
                    <th style="border: 1px solid #ddd; padding: 8px; color: rgb(191 191 190);">Action</th>
                </tr>
            `;
            table.appendChild(thead);

            // Create table body
            const tbody = document.createElement('tbody');
            data.forEach(torrent => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td style="border: 1px solid #ddd; padding: 8px; font-weight: 600; text-transform: uppercase; color: rgb(191 191 190);">${torrent.title}</td>
                    <td style="border: 1px solid #ddd; padding: 8px; color: rgb(191 191 190);">${(torrent.size).toFixed(1)} GB</td>
                    <td style="border: 1px solid #ddd; padding: 8px; color: rgb(191 191 190);">${torrent.source}</td>
                    <td style="border: 1px solid #ddd; padding: 8px; color: rgb(191 191 190);">${torrent.cached}</td>
                    <td style="border: 1px solid #ddd; padding: 8px; color: rgb(191 191 190);">${torrent.score_breakdown.total_score}</td>
                    <td style="border: 1px solid #ddd; padding: 8px; color: rgb(191 191 190);"><button onclick="addToRealDebrid('${torrent.magnet}')">Add to Real-Debrid</button></td>
                `;
                tbody.appendChild(row);
            });
            table.appendChild(tbody);

            overlayContent.appendChild(table);
        }
    }
    mediaQuery.addListener(handleScreenChange);
    handleScreenChange(mediaQuery);

    // Close the overlay when the close button is clicked
    document.querySelector('.close-btn').onclick = function() {
        document.getElementById('overlay').style.display = 'none';
    };
    
    overlay.style.display = 'block';
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
    // Make selectSeason function globally accessible    
    const hamburgerMenu = document.querySelector('.hamburger-menu');
    const navMenu = document.getElementById('navMenu');

    hamburgerMenu.addEventListener('click', () => {
        hamburgerMenu.classList.toggle('active');
        navMenu.classList.toggle('active');
        if (navMenu.classList == 'active') {
            navMenu.style.display = 'flex';
        }
        else{
            navMenu.style.display = 'none';
        }
    });

    // Responsive layout
    function responsiveLayout() {
        if (window.innerWidth <= 768) {
        navMenu.classList.remove('active');
        hamburgerMenu.classList.remove('active');
        navMenu.style.display = 'none';
        hamburgerMenu.style.display = 'flex';
        } else {
        navMenu.style.display = 'flex';
        hamburgerMenu.style.display = 'none';
        }
    }

    window.addEventListener('resize', responsiveLayout);
    responsiveLayout();

    // Close the overlay when the close button is clicked
    const closeButton = document.querySelector('.close-btn');
    if (closeButton) {
        closeButton.onclick = function() {
            document.getElementById('overlay').style.display = 'none';
        };
    }

    loadDarkModePreference();
    
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