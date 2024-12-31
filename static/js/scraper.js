function searchMedia(event) {
    event.preventDefault();
    showLoadingState();
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
        hideLoadingState();
        console.log('Received data:', JSON.stringify(data, null, 2));  // Pretty print the entire response
        if (data.error) {
            displayError(data.error);
        } else if (data.results && Array.isArray(data.results)) {
            displaySearchResults(data.results, version);
        } else {
            displayError('Invalid response from server');
        }
    })
    .catch(error => {
        hideLoadingState();
        console.error('Error:', error);
        displayError('An error occurred while searching.');
    });
}

function displaySearchResults(results, version) {
    console.log('Displaying results:', results);  // Debugging line
    toggleResultsVisibility('displaySearchResults');
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

    results.forEach(item => {
        const searchResDiv = document.createElement('div');
        searchResDiv.className = 'sresult';
        searchResDiv.innerHTML = `
            <button>${item.media_type === 'show' ? '<span class="mediatype-tv">TV</span>' : '<span class="mediatype-mv">MOVIE</span>'}
            <img src="${item.poster_path ? 'https://image.tmdb.org/t/p/w600_and_h900_bestv2' + item.poster_path : '/static/image/placeholder-horizontal.png'}" alt="${item.title}" style="width: 100%; height: auto;">
            <div class="searchresult-info">
                <h2 class="searchresult-item">${item.title} (${item.year || 'N/A'})</h2>
            </div></button>                
        `;        
        searchResDiv.onclick = function() {
            if (item.media_type === 'movie') {
                selectMedia(item.id, item.title, item.year, item.media_type, null, null, false, version);
            } else {
                selectSeason(item.id, item.title, item.year, item.media_type, null, null, true, item.genre_ids, item.vote_average, item.backdrop_path, item.show_overview);
            }
        };
        gridContainer.appendChild(searchResDiv);
    });

    searchResultsDiv.appendChild(gridContainer);
}

function displaySeasonInfo(title, season_num, air_date, season_overview, poster_path, genre_ids, vote_average, backdrop_path, show_overview) {
    console.log('Received genre_ids:', genre_ids);
    const seasonInfo = document.getElementById('season-info');

    // Format genre_ids into a string of genre names
    let genreString = '';
    if (Array.isArray(genre_ids)) {
        genreString = genre_ids
            .filter(genre => genre) // Filter out null or undefined genres
            .map(genre => {
                if (typeof genre === 'string') {
                    return genre;
                } else if (typeof genre === 'object' && genre.name) {
                    return genre.name.split(' ').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
                }
                return '';
            })
            .filter(genre => genre) // Filter out any empty strings
            .slice(0, 3) // Truncate to 3 genres
            .join(', ');
    } else if (typeof genre_ids === 'string') {
        genreString = genre_ids;
    }

    // If genreString is empty after processing, set a default message
    if (!genreString) {
        genreString = 'Genres not available';
    }

    seasonInfo.innerHTML = `
        <div class="season-info-container">
            <span class="show-rating">${(vote_average).toFixed(1)}</span>
            <img src="https://image.tmdb.org/t/p/w300${poster_path}" alt="${title} Season ${season_num}" class="season-poster">
            <div class="season-details">
                <h2>${title} - Season ${season_num}</h2>
                <p>${genreString}</p>
                <div class="season-overview">
                    <p>${season_overview ? season_overview : show_overview}</p>
                </div>
            </div>
        </div>
        <div class="season-bg-image" style="background-image: url('https://image.tmdb.org/t/p/w1920_and_h800_multi_faces${backdrop_path}');"></div>
    `;
}

function selectSeason(mediaId, title, year, mediaType, season, episode, multi, genre_ids, vote_average, backdrop_path, show_overview, tmdb_api_key_set) {
    showLoadingState();
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

    fetch('/scraper/select_season', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        hideLoadingState();
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
                if (tmdb_api_key_set) {
                    displaySeasonInfo(selectedItem.title, selectedItem.season_num, selectedItem.air_date, selectedItem.season_overview, selectedItem.poster_path, genre_ids, vote_average, backdrop_path, show_overview);
                } else {
                    displaySeasonInfoTextOnly(selectedItem.title, selectedItem.season_num);
                }
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
        hideLoadingState();
        console.error('Error:', error);
        displayError('An error occurred while selecting media.');
    });
}

// New function to display season info as text-only
function displaySeasonInfoTextOnly(title, season_num) {
    const seasonInfo = document.getElementById('season-info');
    seasonInfo.innerHTML = `
        <div class="season-info-container text-only">
            <h2>${title} - Season ${season_num}</h2>
        </div>
    `;
}

function selectEpisode(mediaId, title, year, mediaType, season, episode, multi) {
    showLoadingState();
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

    fetch('/scraper/select_episode', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        hideLoadingState();
        if (data.error) {
            displayError(data.error);
        } else {
            displayEpisodeResults(data.episodeResults, title, year, version);
        }
    })
    .catch(error => {
        hideLoadingState();
        console.error('Error:', error);
        displayError('An error occurred while selecting media.');
    });
}

async function selectMedia(mediaId, title, year, mediaType, season, episode, multi) {
    showLoadingState();
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
    fetch('/scraper/select_media', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        hideLoadingState();
        displayTorrentResults(data.torrent_results, title, year, version);
    })
    .catch(error => {
        hideLoadingState();
        console.error('Error:', error);
        displayError('An error occurred while selecting media.');
    });
}

function addToRealDebrid(magnetLink) {
    
    showPopup({
        type: POPUP_TYPES.CONFIRM,
        title: 'Confirm Action',
        message: 'Are you sure you want to add this torrent to your Debrid Provider?',
        confirmText: 'Add',
        cancelText: 'Cancel',
        onConfirm: () => {
            showLoadingState();

            fetch('/scraper/add_to_real_debrid', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                },
                body: `magnet_link=${encodeURIComponent(magnetLink)}`
            })
            .then(response => {
                if (!response.ok) {
                    return response.json().then(errorData => {
                        throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
                    });
                }
                return response.json();
            })

            .then(data => {
                hideLoadingState();

                if (data.error) {
                    throw new Error(data.error);
                } else {
                    showPopup({
                        type: POPUP_TYPES.SUCCESS,
                        title: 'Success',
                        message: data.message,
                        autoClose: 15000  // 15 seconds
                    });
                }
            })
            .catch(error => {
                console.error('Error:', error);
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Error',
                    message: `Error adding to Real-Debrid: ${error.message}`,
                });
            })
        },

    });
}

function displayError(message) {
    showPopup({
        type: POPUP_TYPES.ERROR,
        title: 'Error',
        message: message
    });
}

function displaySuccess(message) {
    showPopup({
        type: POPUP_TYPES.SUCCESS,
        title: 'Success',
        message: message
    });
}

function showLoadingState() {
    Loading.show();
    
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
        episode.style.pointerEvents = 'none';
        episode.style.opacity = '0.5';
    }
}

// Function to hide loading state and re-enable buttons
function hideLoadingState() {
    Loading.hide();

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
        episode.style.pointerEvents = 'auto';
        episode.style.opacity = '1';
    }
}

function displayEpisodeResults(episodeResults, title, year, version) {
    toggleResultsVisibility('displayEpisodeResults');
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
            <img src="${item.still_path ? `https://image.tmdb.org/t/p/w300${item.still_path}` : `/static/image/placeholder-horizontal.png`}" alt="${item.episode_title}" style="width: 100%; height: auto;">
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

function toggleResultsVisibility(section) {
    const trendingContainer = document.getElementById('trendingContainer');
    const searchResult = document.getElementById('searchResult');
    const seasonResults = document.getElementById('seasonResults');
    const dropdown = document.getElementById('seasonDropdown');
    const seasonPackButton = document.getElementById('seasonPackButton');
    const episodeResultsDiv = document.getElementById('episodeResults');
    if (section === 'displayEpisodeResults') {
        trendingContainer.style.display = 'none';
        searchResult.style.display = 'none';
        seasonResults.style.display = 'block';
        dropdown.style.display = 'block';
        seasonPackButton.style.display = 'block';
        episodeResultsDiv.style.display = 'block';
    }
    if (section === 'displaySearchResults') {
        trendingContainer.style.display = 'none';
        searchResult.style.display = 'block';
        seasonResults.style.display = 'none';
        episodeResultsDiv.style.display = 'none';
    }
    if (section === 'get_trendingMovies') {
        trendingContainer.style.display = 'block';
        searchResult.style.display = 'none';
        seasonResults.style.display = 'none';
        dropdown.style.display = 'none';
        seasonPackButton.style.display = 'none';
        episodeResultsDiv.style.display = 'none';

    }
}

function displayTorrentResults(data, title, year, version) {
    hideLoadingState();
    const overlay = document.getElementById('overlay');

    const mediaQuery = window.matchMedia('(max-width: 1024px)');
    function handleScreenChange(e) {
        if (e.matches) {
            const overlayContentRes = document.getElementById('overlayContent');
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

                // Create cache status badge
                const cacheStatus = torrent.cached || 'Unknown';
                const cacheStatusClass = cacheStatus === 'Yes' ? 'cached' : 
                                      cacheStatus === 'No' ? 'not-cached' : 
                                      cacheStatus === 'Not Checked' ? 'not-checked' :
                                      cacheStatus === 'N/A' ? 'check-unavailable' : 'unknown';

                torResDiv.innerHTML = `
                    <button>
                    <div class="torresult-info">
                        <p class="torresult-title">${torrent.title}</p>
                        <p class="torresult-item">${(torrent.size).toFixed(1)} GB | ${torrent.score_breakdown.total_score}</p>
                        <p class="torresult-item">${torrent.source}</p>
                        <span class="cache-status ${cacheStatusClass}">${cacheStatus}</span>
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
                    <th style="color: rgb(191 191 190); width: 80%;">Name</th>
                    <th style="color: rgb(191 191 190); width: 10%;">Size</th>
                    <th style="color: rgb(191 191 190); width: 15%;">Source</th>
                    <th style="color: rgb(191 191 190); width: 10%;">Score</th>
                    <th style="color: rgb(191 191 190); width: 15%; text-align: center;">Cache</th>
                    <th style="color: rgb(191 191 190); width: 10%; text-align: center;">Action</th>
                </tr>
            `;
            table.appendChild(thead);

            // Create table body
            const tbody = document.createElement('tbody');
            data.forEach(torrent => {
                const cacheStatus = torrent.cached || 'Unknown';
                const cacheStatusClass = cacheStatus === 'Yes' ? 'cached' : 
                                      cacheStatus === 'No' ? 'not-cached' : 
                                      cacheStatus === 'Not Checked' ? 'not-checked' :
                                      cacheStatus === 'N/A' ? 'check-unavailable' : 'unknown';

                const row = document.createElement('tr');
                row.innerHTML = `
                    <td style="font-weight: 600; text-transform: uppercase; color: rgb(191 191 190); max-width: 80%; word-wrap: break-word; white-space: normal; padding: 10px;">
                        <div style="display: block; line-height: 1.4; min-height: fit-content;">
                            ${torrent.title}
                        </div>
                    </td>
                    <td style="color: rgb(191 191 190);">${(torrent.size).toFixed(1)} GB</td>
                    <td style="color: rgb(191 191 190);">${torrent.source}</td>
                    <td style="color: rgb(191 191 190);">${torrent.score_breakdown.total_score}</td>
                    <td style="color: rgb(191 191 190); text-align: center;">
                        <span class="cache-status ${cacheStatusClass}">${cacheStatus}</span>
                    </td>
                    <td style="color: rgb(191 191 190); text-align: center;">
                        <button onclick="addToRealDebrid('${torrent.magnet}')">Add to Debrid</button>
                    </td>
                `;
                tbody.appendChild(row);
            });
            table.appendChild(tbody);

            overlayContent.appendChild(table);
        }
    }
    mediaQuery.addListener(handleScreenChange);
    handleScreenChange(mediaQuery);

    overlay.style.display = 'block';
}

document.addEventListener('DOMContentLoaded', function() {

    // Close the overlay when the close button is clicked
    const closeButton = document.querySelector('.close-btn');
    if (closeButton) {
        closeButton.onclick = function() {
            document.getElementById('overlay').style.display = 'none';
        };
    }
  
    const container_mv = document.getElementById('movieContainer');
    const scrollLeftBtn_mv = document.getElementById('scrollLeft_mv');
    const scrollRightBtn_mv = document.getElementById('scrollRight_mv');
    if (scrollLeftBtn_mv) {
        scrollLeftBtn_mv.disabled = container_mv.scrollLeft === 0;
    }
    function updateButtonStates_mv() {
        scrollLeftBtn_mv.disabled = container_mv.scrollLeft === 0;
        scrollRightBtn_mv.disabled = container_mv.scrollLeft >= container_mv.scrollWidth - container_mv.offsetWidth;
    }

    function scroll_mv(direction) {
        const scrollAmount = container_mv.offsetWidth;
        const newPosition = direction === 'left'
            ? Math.max(container_mv.scrollLeft - scrollAmount, 0)
            : Math.min(container_mv.scrollLeft + scrollAmount, container_mv.scrollWidth - container_mv.offsetWidth);
        
        container_mv.scrollTo({ left: newPosition, behavior: 'smooth' });
    }

    if (container_mv) {
        scrollLeftBtn_mv.addEventListener('click', () => scroll_mv('left'));
        scrollRightBtn_mv.addEventListener('click', () => scroll_mv('right'));
        container_mv.addEventListener('scroll', updateButtonStates_mv);
    }

    const container_tv = document.getElementById('showContainer');
    const scrollLeftBtn_tv = document.getElementById('scrollLeft_tv');
    const scrollRightBtn_tv = document.getElementById('scrollRight_tv');
    if (scrollLeftBtn_tv) {
        scrollLeftBtn_tv.disabled = container_tv.scrollLeft === 0;
    }
    function updateButtonStates_tv() {
        scrollLeftBtn_tv.disabled = container_tv.scrollLeft === 0;
        scrollRightBtn_tv.disabled = container_tv.scrollLeft >= container_tv.scrollWidth - container_tv.offsetWidth;
    }

    function scroll_tv(direction) {
        const scrollAmount = container_tv.offsetWidth;
        const newPosition = direction === 'left'
            ? Math.max(container_tv.scrollLeft - scrollAmount, 0)
            : Math.min(container_tv.scrollLeft+ scrollAmount, container_tv.scrollWidth - container_tv.offsetWidth);
        
        container_tv.scrollTo({ left: newPosition, behavior: 'smooth' });
    }
    if (container_tv) {
        scrollLeftBtn_tv.addEventListener('click', () => scroll_tv('left'));
        scrollRightBtn_tv.addEventListener('click', () => scroll_tv('right'));
        container_tv.addEventListener('scroll', updateButtonStates_tv);
    }

    function createMovieElement(data) {
        const movieElement = document.createElement('div');
        movieElement.className = 'media-card';
        movieElement.innerHTML = `
            <div class="media-poster">
                <span id="trending-rating">${(data.rating).toFixed(1)}</span>
                <span id="trending-watchers">👁 ${data.watcher_count}</span>
                <span class="media-title">${data.title}</br><span style="font-size: 14px; opacity: 0.8;">${data.year}</span></span>
                <img src="${data.poster_path}" alt="${data.title}" class="media-poster-img">
            </div>
        `;
        movieElement.onclick = function() {
            selectMedia(data.tmdb_id, data.title, data.year, 'movie', 'null', 'null', 'False');
        };
        return movieElement;
    }
    
    function createShowElement(data) {
        const movieElement = document.createElement('div');
        movieElement.className = 'media-card';
        movieElement.innerHTML = `
            <div class="media-poster">
                <span id="trending-rating">${(data.rating).toFixed(1)}</span>
                <span id="trending-watchers">👁 ${data.watcher_count}</span>
                <span class="media-title">${data.title}</br><span style="font-size: 14px; opacity: 0.8;">${data.year}</span></span>
                <img src="${data.poster_path}" alt="${data.title}" class="media-poster-img">
            </div>
        `;
        movieElement.onclick = function() {
            selectSeason(data.tmdb_id, data.title, data.year, 'tv', 'null', 'null', 'True', data.genre_ids, data.vote_average, data.backdrop_path, data.show_overview, data.tmdb_api_key_set)
        };
        return movieElement;
    }
    
    function get_trendingMovies() {
        toggleResultsVisibility('get_trendingMovies');
        fetch('/scraper/movies_trending', {
            method: 'GET'
        })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                displayError(data.error);
            } else {
                const trendingMovies = data.trendingMovies;
                trendingMovies.forEach(item => {
                    const movieElement = createMovieElement(item);
                    container_mv.appendChild(movieElement);
                });

            }
        })
        .catch(error => {
            console.error('Error:', error);
            displayError('An error occurred.');
        });
    }

    function get_trendingShows() {
        toggleResultsVisibility('get_trendingMovies');
        fetch('/scraper/shows_trending', {
            method: 'GET'
        })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                displayError(data.error);
            } else {
                const trendingShows = data.trendingShows;
                trendingShows.forEach(item => {
                    const showElement = createShowElement(item);
                    container_tv.appendChild(showElement);
                });

            }
        })
        .catch(error => {
            console.error('Error:', error);
            displayError('An error occurred.');
        });
    }

    // Add event listener for search form
    const searchForm = document.getElementById('search-form');
    if (searchForm) {
        fetch('/trakt/trakt_auth_status')
            .then(response => response.json())
            .then(status => {
                if (status.status == 'authorized') {
                    get_trendingMovies();
                    get_trendingShows();
                } else {
                    displayTraktAuthMessage();
                }
            });
        searchForm.addEventListener('submit', searchMedia);
    }

    // Initialize the Loading object
    Loading.init();
});

function displayTraktAuthMessage() {
    const trendingContainer = document.getElementById('trendingContainer');
    trendingContainer.innerHTML = '<p>Please authenticate with Trakt to see trending movies and shows.</p>';
}