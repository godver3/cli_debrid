/**
 * Discover Add Media Page
 * Simplified episode selection for TV shows from discover page
 * No search functionality - only season/episode selection
 */

(function() {
    'use strict';

    // Initialize on DOM load
    document.addEventListener('DOMContentLoaded', function() {
        console.log('🎬 Discover Add Media page loaded');

        // Get media data from meta tags
        const mediaData = {
            media_id: document.querySelector('meta[name="media_id"]')?.content,
            title: document.querySelector('meta[name="title"]')?.content,
            year: document.querySelector('meta[name="year"]')?.content,
            media_type: document.querySelector('meta[name="media_type"]')?.content || 'tv',
            rating: parseFloat(document.querySelector('meta[name="rating"]')?.content) || 0,
            vote_average: parseFloat(document.querySelector('meta[name="vote_average"]')?.content) || 0,
            genre_ids: JSON.parse(document.querySelector('meta[name="genre_ids"]')?.content || '[]'),
            backdrop_path: document.querySelector('meta[name="backdrop_path"]')?.content || '',
            overview: document.querySelector('meta[name="overview"]')?.content || 'No overview available'
        };

        console.log('Media data:', mediaData);

        // Get allow_specials from localStorage
        const allow_specials = localStorage.getItem('allowSpecials') === 'true';

        // Show loading indicator
        const seasonResults = document.getElementById('seasonResults');
        if (seasonResults) {
            seasonResults.innerHTML = '<div class="loading">Loading seasons...</div>';
        }

        // Fetch seasons
        const formData = new FormData();
        formData.append('media_id', mediaData.media_id);
        formData.append('title', mediaData.title);
        formData.append('year', mediaData.year);
        formData.append('media_type', mediaData.media_type);
        formData.append('multi', 'True');
        formData.append('version', 'Any');
        formData.append('allow_specials', allow_specials);
        if (mediaData.rating) formData.append('rating', mediaData.rating);
        if (mediaData.vote_average) formData.append('vote_average', mediaData.vote_average);
        if (mediaData.genre_ids && mediaData.genre_ids.length > 0) {
            // Convert array to comma-separated string
            formData.append('genre_ids', mediaData.genre_ids.join(','));
        }

        fetch('/scraper/select_season', {
            method: 'POST',
            body: formData
        })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                console.error('Error loading season data:', data.error);
                if (seasonResults) {
                    seasonResults.innerHTML = `<div class="error">Error: ${data.error}</div>`;
                }
                return;
            }

            console.log('Season data loaded successfully');

            // Get season results
            const seasonData = data.episode_results || data.results;
            if (!seasonData || seasonData.length === 0) {
                if (seasonResults) {
                    seasonResults.innerHTML = '<div class="error">No season results found</div>';
                }
                return;
            }

            // Clear loading indicator
            if (seasonResults) {
                // Check permissions for Season Pack button visibility
                const hasUserPermissionsEl = document.getElementById('has_user_permissions');
                const hasUserPermissions = hasUserPermissionsEl && hasUserPermissionsEl.value === 'True';

                const seasonPackButtonHtml = hasUserPermissions
                    ? '<button id="seasonPackButton">Season Pack</button>'
                    : '';

                seasonResults.style.display = 'block';
                seasonResults.innerHTML = `
                    <div id="season-info"></div>
                    <div class="season-controls">
                        <select id="seasonDropdown"></select>
                        ${seasonPackButtonHtml}
                        <button id="requestSeasonButton">Request Season</button>
                    </div>
                    <div id="episodeResults"></div>
                `;
            }

            // Populate the season dropdown
            const dropdown = document.getElementById('seasonDropdown');
            if (!dropdown) {
                console.error('Season dropdown not found');
                return;
            }

            dropdown.innerHTML = '';
            seasonData.forEach(item => {
                const option = document.createElement('option');
                option.value = JSON.stringify(item);
                option.textContent = item.season_num === 0 ? 'Specials' : `Season: ${item.season_num}`;
                dropdown.appendChild(option);
            });

            // Add containment to season-controls
            const seasonControls = document.querySelector('.season-controls');
            if (seasonControls) {
                seasonControls.style.maxWidth = '1600px';
                seasonControls.style.margin = '0 auto';
                seasonControls.style.display = 'flex';
                seasonControls.style.width = '100%';
            }

            // Setup event handlers
            setupSeasonDropdown(dropdown, mediaData, seasonData, allow_specials);
            setupSeasonPackButton(dropdown, mediaData.genre_ids);
            setupRequestSeasonButton(dropdown);

            // Auto-select first season
            if (dropdown.options.length > 0) {
                dropdown.dispatchEvent(new Event('change'));
            }
        })
        .catch(error => {
            console.error('Error loading media:', error);
            if (seasonResults) {
                seasonResults.innerHTML = '<div class="error">Failed to load media data</div>';
            }
        });
    });

    // Setup season dropdown change handler
    function setupSeasonDropdown(dropdown, mediaData, seasonData, allow_specials) {
        const tmdb_api_key_set = document.getElementById('tmdb_api_key_set')?.value === 'True';

        dropdown.addEventListener('change', function() {
            const selectedItem = JSON.parse(this.value);
            const optionText = this.options[this.selectedIndex].textContent;
            let displayedSeasonNum = selectedItem.season_num;

            if (optionText.startsWith('Season: ')) {
                const extractedSeason = parseInt(optionText.replace('Season: ', ''));
                if (!isNaN(extractedSeason)) {
                    displayedSeasonNum = extractedSeason;
                }
            }

            // Display season info
            if (tmdb_api_key_set) {
                displaySeasonInfo(
                    selectedItem.title,
                    displayedSeasonNum,
                    selectedItem.air_date,
                    selectedItem.season_overview,
                    selectedItem.poster_path,
                    mediaData.genre_ids,
                    mediaData.vote_average,
                    mediaData.backdrop_path,
                    mediaData.overview
                );
            } else {
                displaySeasonInfoTextOnly(selectedItem.title, displayedSeasonNum);
            }

            // Fetch and display episodes
            selectEpisode(
                selectedItem.id,
                selectedItem.title,
                selectedItem.year,
                selectedItem.media_type,
                displayedSeasonNum,
                null,
                selectedItem.multi,
                mediaData.genre_ids,
                allow_specials
            );
        });
    }

    // Setup season pack button
    function setupSeasonPackButton(dropdown, genre_ids) {
        const seasonPackButton = document.getElementById('seasonPackButton');

        // Button may not exist for requesters due to permission restrictions
        if (!seasonPackButton) {
            return;
        }

        if (seasonPackButton) {
            seasonPackButton.onclick = function() {

                const selectedItem = JSON.parse(dropdown.value);
                const content = {
                    mediaId: selectedItem.id,
                    title: selectedItem.title,
                    year: selectedItem.year,
                    mediaType: selectedItem.media_type,
                    season: selectedItem.season_num,
                    episode: null,
                    multi: true,
                    genre_ids: genre_ids
                };
                showScrapeVersionModal(content);
            };
        }
    }

    // Setup request season button
    function setupRequestSeasonButton(dropdown) {
        const requestSeasonButton = document.getElementById('requestSeasonButton');
        if (requestSeasonButton) {
            requestSeasonButton.onclick = function() {
                const selectedItem = JSON.parse(dropdown.value);
                const content = {
                    id: selectedItem.id,
                    mediaType: selectedItem.media_type,
                    title: selectedItem.title,
                    seasons: [selectedItem.season_num]
                };
                showVersionModal(content);
            };
        }
    }

    // Display season info with images
    function displaySeasonInfo(title, season_num, air_date, season_overview, poster_path, genre_ids, vote_average, backdrop_path, show_overview) {
        const seasonInfo = document.getElementById('season-info');
        if (!seasonInfo) return;

        // Add containment styling to season-info
        seasonInfo.style.maxWidth = '1600px';
        seasonInfo.style.margin = '0 auto';
        seasonInfo.style.width = '100%';

        // Format genre_ids into a string
        let genreString = '';
        if (Array.isArray(genre_ids) && genre_ids.length > 0) {
            genreString = genre_ids
                .filter(genre => genre)
                .slice(0, 3)
                .join(', ');
        }
        if (!genreString) {
            genreString = 'Genres not available';
        }

        // Create background style
        let backgroundImageStyle = '';
        if (backdrop_path) {
            const backdropUrl = backdrop_path.startsWith('http') ? backdrop_path : `/scraper/tmdb_image/w1920_and_h800_multi_faces${backdrop_path}`;
            backgroundImageStyle = `background-image: url('${backdropUrl}');`;
        } else {
            backgroundImageStyle = 'background: linear-gradient(to bottom, #333333, #121212);';
        }

        const seasonLabel = season_num === 0 ? 'Specials' : `Season ${season_num}`;

        seasonInfo.innerHTML = `
            <div class="season-info-container">
                <img src="/scraper/tmdb_image/w300${poster_path}" alt="${title} ${seasonLabel}" class="season-poster">
                <div class="season-details">
                    <span class="show-rating">${(vote_average || 0).toFixed(1)}</span>
                    <h2>${title} - ${seasonLabel}</h2>
                    <p>${genreString}</p>
                    <div class="season-overview">
                        <p>${season_overview || show_overview}</p>
                    </div>
                </div>
            </div>
            <div class="season-bg-image" style="${backgroundImageStyle}"></div>
        `;
    }

    // Display season info without images
    function displaySeasonInfoTextOnly(title, season_num) {
        const seasonInfo = document.getElementById('season-info');
        if (!seasonInfo) return;

        const seasonLabel = season_num === 0 ? 'Specials' : `Season ${season_num}`;
        seasonInfo.innerHTML = `
            <div class="season-info-text-only">
                <h2>${title} - ${seasonLabel}</h2>
            </div>
        `;
    }

    // Fetch and display episodes for a season
    function selectEpisode(mediaId, title, year, mediaType, season, episode, multi, genre_ids, allow_specials) {
        const episodeResults = document.getElementById('episodeResults');
        if (episodeResults) {
            episodeResults.innerHTML = '<div class="loading">Loading episodes...</div>';
        }

        const formData = new FormData();
        formData.append('media_id', mediaId);
        formData.append('title', title);
        formData.append('year', year);
        formData.append('media_type', mediaType);
        formData.append('season', season);
        formData.append('multi', multi || 'True');
        formData.append('version', 'Any');
        formData.append('allow_specials', allow_specials);
        if (genre_ids && Array.isArray(genre_ids)) {
            formData.append('genre_ids', genre_ids.join(','));
        }

        fetch('/scraper/select_episode', {
            method: 'POST',
            body: formData
        })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                console.error('Error loading episodes:', data.error);
                if (episodeResults) {
                    episodeResults.innerHTML = `<div class="error">Error: ${data.error}</div>`;
                }
                return;
            }

            const episodes = data.episode_results || [];
            displayEpisodeResults(episodes, title, year, 'Any', mediaId, mediaType, season, null, genre_ids);
        })
        .catch(error => {
            console.error('Error fetching episodes:', error);
            if (episodeResults) {
                episodeResults.innerHTML = '<div class="error">Failed to load episodes</div>';
            }
        });
    }

    // Display episode results
    function displayEpisodeResults(episodes, title, year, version, mediaId, mediaType, season, episode, genre_ids) {
        console.log(`Rendered episodes 1-${episodes.length} of ${episodes.length}`);

        const episodeResults = document.getElementById('episodeResults');
        if (!episodeResults) return;

        if (!episodes || episodes.length === 0) {
            episodeResults.innerHTML = '<div class="empty-state">No episodes found</div>';
            return;
        }

        // Check if user is a requester
        const isRequesterEl = document.getElementById('is_requester');
        const isRequester = isRequesterEl && isRequesterEl.value === 'True';

        // Clear and create grid container (let CSS handle the styling)
        episodeResults.innerHTML = '';
        const gridContainer = document.createElement('div');
        // Don't set inline styles - let scraper.css handle it with #episodeResults > div

        episodes.forEach((ep, index) => {
            const episodeDiv = document.createElement('div');
            episodeDiv.className = 'episode';

            // Format air date
            var options = {year: 'numeric', month: 'long', day: 'numeric'};
            var date = ep.air_date ? new Date(ep.air_date) : null;

            // Use still_path if available, otherwise poster_path, otherwise placeholder
            const imagePath = ep.still_path || ep.poster_path;
            const imageSrc = imagePath
                ? `/scraper/tmdb_image/w300${imagePath}`
                : '/static/image/placeholder-horizontal.png';
            const imageClass = imagePath ? '' : 'placeholder-episode';

            // Determine loading attribute (eager for first 12)
            const loadingAttr = index < 12 ? 'eager' : 'lazy';

            // Episode rating (use vote_average from episode data)
            const rating = ep.vote_average || 0;

            // Map API field names: episode_num, episode_title (not episode_number, name)
            const episodeNumber = ep.episode_num || ep.episode_number || 0;
            const episodeTitle = ep.episode_title || ep.name || 'Untitled';

            episodeDiv.innerHTML = `
                <button>
                    <span class="episode-rating">${rating.toFixed(1)}</span>
                    <img src="${imageSrc}"
                        alt="${episodeTitle}"
                        loading="${loadingAttr}"
                        class="${imageClass}">
                    <div class="episode-info">
                        <h2 class="episode-title">${episodeNumber}. ${episodeTitle}</h2>
                        <p class="episode-sub">${date ? date.toLocaleDateString("en-US", options) : 'Air date unknown'}</p>
                    </div>
                </button>
            `;

            // Set cursor to pointer for all users
            episodeDiv.style.cursor = 'pointer';

            // Add click handler for both requester and non-requester users
            episodeDiv.onclick = function() {
                const content = {
                    mediaId: ep.id,
                    id: ep.id,
                    title: ep.title,
                    year: ep.year,
                    mediaType: ep.media_type,
                    season: ep.season_num,
                    episode: episodeNumber,
                    multi: ep.multi,
                    genre_ids: genre_ids,
                    episodes: [[ep.season_num, episodeNumber]]
                };

                // Requesters use the request modal, others use scrape modal
                if (isRequester) {
                    showVersionModal(content);
                } else {
                    showScrapeVersionModal(content);
                }
            };

            gridContainer.appendChild(episodeDiv);
        });

        episodeResults.appendChild(gridContainer);
    }

    // ============================================
    // Modal and Version Management
    // ============================================

    let availableVersions = [];
    let selectedContent = null;
    let scrapeContent = null;

    // Fetch available versions from backend
    async function fetchVersions() {
        try {
            const response = await fetch('/content/versions');
            const data = await response.json();
            if (data.versions) {
                availableVersions = data.versions;
            }
        } catch (error) {
            console.error('Error fetching versions:', error);
            displayError('Error fetching versions');
        }
    }

    // Show scrape version modal
    function showScrapeVersionModal(content) {
        scrapeContent = content;
        const modal = document.getElementById('scrapeVersionModal');
        const versionRadios = document.getElementById('scrapeVersionRadios');

        versionRadios.innerHTML = '';

        availableVersions.forEach((version, index) => {
            const div = document.createElement('div');
            div.className = 'version-checkbox';
            div.innerHTML = `
                <input type="radio" id="scrape-version-${version}" name="scrape-versions" value="${version}" ${index === 0 ? 'checked' : ''}>
                <label for="scrape-version-${version}">${version}</label>
            `;
            versionRadios.appendChild(div);
        });

        // Add a 'No Version' option
        const noVersionDiv = document.createElement('div');
        noVersionDiv.className = 'version-checkbox';
        noVersionDiv.innerHTML = `
            <input type="radio" id="scrape-version-No Version" name="scrape-versions" value="No Version">
            <label for="scrape-version-No Version">No Version</label>
        `;
        versionRadios.appendChild(noVersionDiv);

        document.body.classList.add('modal-open');
        modal.style.display = 'flex';
    }

    // Handle scrape version confirmation
    async function handleScrapeVersionConfirm() {
        const selectedVersion = document.querySelector('#scrapeVersionRadios input[name="scrape-versions"]:checked')?.value;
        if (selectedVersion === undefined) {
            displayError('Please select a version.');
            return;
        }

        closeScrapeVersionModal();

        const c = scrapeContent;
        await selectMedia(c.mediaId, c.title, c.year, c.mediaType, c.season, c.episode, c.multi, c.genre_ids, selectedVersion);
    }

    // Close scrape version modal
    function closeScrapeVersionModal() {
        document.getElementById('scrapeVersionModal').style.display = 'none';
        document.body.classList.remove('modal-open');
    }

    // Show version selection modal for requests
    function showVersionModal(content) {
        selectedContent = content;
        const modal = document.getElementById('versionModal');
        const versionCheckboxes = document.getElementById('versionCheckboxes');

        // Clear existing checkboxes
        versionCheckboxes.innerHTML = '';

        // Create checkboxes for each version
        availableVersions.forEach(version => {
            const div = document.createElement('div');
            div.className = 'version-checkbox';
            div.innerHTML = `
                <input type="checkbox" id="request-version-${version}" name="versions" value="${version}">
                <label for="request-version-${version}">${version}</label>
            `;
            versionCheckboxes.appendChild(div);
        });

        document.body.classList.add('modal-open');
        modal.style.display = 'flex';
    }

    // Handle version confirm for requests
    async function handleVersionConfirm() {
        const versionCheckboxes = document.querySelectorAll('#versionCheckboxes input[name="versions"]:checked');
        const selectedVersions = Array.from(versionCheckboxes).map(cb => cb.value);

        if (selectedVersions.length === 0) {
            displayError('Please select at least one version');
            return;
        }

        closeVersionModal();
        await requestContent(selectedContent, selectedVersions);
    }

    // Close version modal
    function closeVersionModal() {
        document.getElementById('versionModal').style.display = 'none';
        document.body.classList.remove('modal-open');
    }

    // Request content from backend
    async function requestContent(content, selectedVersions) {
        showLoadingState('Requesting content, please wait...');
        try {
            const requestData = {
                id: content.id,
                mediaType: content.mediaType,
                title: content.title,
                versions: selectedVersions
            };

            // Add seasons if specified for TV shows
            if (content.mediaType === 'tv' && content.seasons) {
                requestData.seasons = content.seasons;
            }

            // Add episodes if specified
            if (content.episodes) {
                requestData.episodes = content.episodes;
            }

            const response = await fetch('/content/request', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(requestData)
            });

            const result = await response.json();
            if (result.success) {
                displaySuccess(`Successfully requested ${content.title}`);
            } else {
                displayError(result.error || 'Failed to request content');
            }
        } catch (error) {
            console.error('Error requesting content:', error);
            displayError('Error requesting content');
        } finally {
            hideLoadingState();
        }
    }

    // Select media and start scraping
    async function selectMedia(mediaId, title, year, mediaType, season, episode, multi, genre_ids, version) {
        // Check if user is a requester
        const isRequesterEl = document.getElementById('is_requester');
        if (isRequesterEl && isRequesterEl.value === 'True') {
            return;
        }

        if (!mediaId || mediaId === 'undefined') {
            console.error("selectMedia called with invalid mediaId:", mediaId);
            displayError("An internal error occurred: media ID is missing.");
            return;
        }

        showLoadingState('Loading, please wait...');

        let formData = new FormData();
        formData.append('media_id', mediaId);
        formData.append('title', title);
        formData.append('year', year);
        formData.append('media_type', mediaType);
        if (season != null) formData.append('season', season);
        if (episode != null) formData.append('episode', episode);
        formData.append('multi', multi);
        formData.append('version', version);
        formData.append('skip_cache_check', 'true');

        // Convert genre_ids to comma-separated string if it's an array
        if (genre_ids) {
            const genreString = Array.isArray(genre_ids) ? genre_ids.join(',') : genre_ids;
            formData.append('genre_ids', genreString);
        }

        try {
            const response = await fetch('/scraper/select_media', {
                method: 'POST',
                body: formData
            });

            if (response.status === 403) {
                hideLoadingState();
                displayError("Access forbidden. You don't have permission to perform this action.");
                return;
            }

            const data = await response.json();

            if (data.error) {
                hideLoadingState();
                displayError(data.error);
                return;
            }

            // Display torrent results on current page instead of redirecting
            displayTorrentResults(data, title, year, version, mediaId, mediaType, season, episode, genre_ids);

        } catch (error) {
            hideLoadingState();
            console.error('Error:', error);
            displayError('An error occurred while processing your request.');
        }
    }

    // Display error message
    function displayError(message) {
        if (typeof showPopup === 'function') {
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Error',
                message: message
            });
        } else {
            alert('Error: ' + message);
        }
    }

    // Display success message
    function displaySuccess(message) {
        if (typeof showPopup === 'function') {
            showPopup({
                type: POPUP_TYPES.SUCCESS,
                title: 'Success',
                message: message
            });
        } else {
            alert('Success: ' + message);
        }
    }

    // Show loading state
    function showLoadingState(message = 'Scraping torrents, please wait...') {
        if (typeof Loading !== 'undefined' && Loading.show) {
            Loading.show(message);
        }

        // Disable all buttons
        const buttons = document.getElementsByTagName('button');
        for (let button of buttons) {
            if (!button.classList.contains('close-loading')) {
                button.disabled = true;
                button.style.opacity = '0.5';
            }
        }

        const selecter = document.getElementsByTagName('select');
        for (let select of selecter) {
            select.disabled = true;
            select.style.opacity = '0.5';
        }
    }

    // Hide loading state
    function hideLoadingState() {
        if (typeof Loading !== 'undefined' && Loading.hide) {
            Loading.hide();
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
    }

    // Initialize modal event listeners
    function initializeModalListeners() {
        // Scrape version modal confirm button
        const confirmScrapeVersion = document.getElementById('confirmScrapeVersion');
        if (confirmScrapeVersion) {
            confirmScrapeVersion.addEventListener('click', handleScrapeVersionConfirm);
        }

        // Scrape version modal cancel button
        const cancelScrapeVersion = document.getElementById('cancelScrapeVersion');
        if (cancelScrapeVersion) {
            cancelScrapeVersion.addEventListener('click', closeScrapeVersionModal);
        }

        // Request version modal confirm button
        const confirmVersions = document.getElementById('confirmVersions');
        if (confirmVersions) {
            confirmVersions.addEventListener('click', handleVersionConfirm);
        }

        // Request version modal cancel button
        const cancelVersions = document.getElementById('cancelVersions');
        if (cancelVersions) {
            cancelVersions.addEventListener('click', closeVersionModal);
        }

        // ESC key to close modals
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                const scrapeModal = document.getElementById('scrapeVersionModal');
                const versionModal = document.getElementById('versionModal');

                if (scrapeModal && scrapeModal.style.display === 'flex') {
                    closeScrapeVersionModal();
                }
                if (versionModal && versionModal.style.display === 'flex') {
                    closeVersionModal();
                }
            }
        });
    }

    // Display torrent results in overlay
    function displayTorrentResults(data, title, year, version, mediaId, mediaType, season, episode, genre_ids, searchDuration = 0) {
        hideLoadingState();
        const overlay = document.getElementById('overlay');
        const overlayContent = document.getElementById('overlayContent');

        if (!overlay || !overlayContent) {
            console.error('Overlay elements not found');
            return;
        }

        // Data structure: { torrent_results: [...], filtered_out_torrent_results: [...] }
        const passedTorrents = data.torrent_results || [];
        const filteredOutTorrents = data.filtered_out_torrent_results || [];

        const allDisplayItems = passedTorrents.map(t => ({ ...t, __isActuallyFilteredOut: false }))
                                         .concat(filteredOutTorrents.map(t => ({ ...t, __isActuallyFilteredOut: true })));

        // Clear overlay content
        overlayContent.innerHTML = '';

        // Check if mobile
        const mediaQuery = window.matchMedia('(max-width: 1024px)');
        const isMobile = mediaQuery.matches;

        // Check current theme
        const currentTheme = localStorage.getItem('selectedTheme') || 'classic';

        if (isMobile) {
            // MOBILE VIEW - Simple cards
            const header = document.createElement('h3');
            header.textContent = `Torrent Results for ${title} (${year})`;
            overlayContent.appendChild(header);

            if (allDisplayItems.length === 0) {
                const noResults = document.createElement('p');
                noResults.textContent = 'No torrents found.';
                noResults.style.textAlign = 'center';
                noResults.style.padding = '2rem';
                overlayContent.appendChild(noResults);
            } else {
                const gridContainer = document.createElement('div');
                gridContainer.style.display = 'flex';
                gridContainer.style.flexWrap = 'wrap';
                gridContainer.style.gap = '20px';
                gridContainer.style.justifyContent = 'center';
                gridContainer.style.padding = '1rem 0';

                allDisplayItems.forEach((torrent) => {
                    const isFilteredOut = torrent.__isActuallyFilteredOut;
                    const torResDiv = document.createElement('div');
                    torResDiv.className = 'torresult' + (isFilteredOut ? ' filtered-out-item' : '');

                    const bitrateDisplay = torrent.bitrate ? ` | ${(torrent.bitrate / 1000).toFixed(1)} Mbps` : '';
                    const scoreDisplay = isFilteredOut ? (torrent.filter_reason || 'Filtered') : (torrent.score_breakdown?.total_score || 'N/A');

                    torResDiv.innerHTML = `
                        <button ${isFilteredOut ? 'style="cursor:pointer; opacity:0.7;"' : ''}>
                            <div class="torresult-info">
                                <p class="torresult-title">${torrent.title || torrent.original_title || 'N/A'}</p>
                                <p class="torresult-item">${(torrent.size || 0).toFixed(1)} GB${bitrateDisplay} | Score: ${scoreDisplay}</p>
                                <p class="torresult-item">${torrent.source || 'N/A'}</p>
                                <span class="cache-status ${torrent.cached === 'Yes' ? 'cached' :
                                              torrent.cached === 'No' ? 'not-cached' :
                                              torrent.cached === 'Not Checked' ? 'not-checked' :
                                              torrent.cached === 'N/A' ? 'check-unavailable' : 'unknown'}">${torrent.cached || 'N/A'}</span>
                            </div>
                        </button>
                    `;

                    torResDiv.onclick = function() {
                        const torrentData = {
                            title: title, year: year, version: version, media_type: mediaType,
                            season: season || null, episode: episode || null, tmdb_id: mediaId,
                            genres: genre_ids, original_title: torrent.original_title
                        };

                        if (isFilteredOut) {
                            const confirmationMessage = `This item was filtered for the following reason:\n\n'${torrent.filter_reason || 'No specific reason provided'}'.\n\nDo you want to add it anyway?`;
                            if (typeof showPopup === 'function') {
                                showPopup({
                                    type: POPUP_TYPES.CONFIRM,
                                    title: 'Add Filtered Item?',
                                    message: confirmationMessage,
                                    confirmText: 'Add Anyway',
                                    onConfirm: () => {
                                        addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                                    }
                                });
                            } else if (confirm(confirmationMessage)) {
                                addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                            }
                        } else {
                            addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                        }
                    };

                    gridContainer.appendChild(torResDiv);
                });
                overlayContent.appendChild(gridContainer);
            }
        } else if (currentTheme === 'tangerine') {
            // TANGERINE THEME - Table with filters (copied from scraper.js)
            // This uses the existing displayTorrentResults logic from scraper.js
            // Call the scraper.js function if it exists
            if (typeof window.displayTorrentResults === 'function') {
                window.displayTorrentResults(data, title, year, version, mediaId, mediaType, season, episode, genre_ids, searchDuration);
                return;
            }
            // Fallback to simple display if scraper.js function not available
            const header = document.createElement('h3');
            header.textContent = `Torrent Results for ${title} (${year})`;
            overlayContent.appendChild(header);

            const gridContainer = document.createElement('div');
            gridContainer.style.display = 'flex';
            gridContainer.style.flexWrap = 'wrap';
            gridContainer.style.gap = '20px';
            gridContainer.style.justifyContent = 'center';
            gridContainer.style.padding = '1rem 0';

            allDisplayItems.forEach((torrent) => {
                const isFilteredOut = torrent.__isActuallyFilteredOut;
                const torResDiv = document.createElement('div');
                torResDiv.className = 'torresult' + (isFilteredOut ? ' filtered-out-item' : '');

                const scoreDisplay = isFilteredOut ? (torrent.filter_reason || 'Filtered') : (torrent.score_breakdown?.total_score || 'N/A');

                torResDiv.innerHTML = `
                    <button ${isFilteredOut ? 'style="cursor:pointer; opacity:0.7;"' : ''}>
                        <div class="torresult-info">
                            <p class="torresult-title">${torrent.title || torrent.original_title || 'N/A'}</p>
                            <p class="torresult-item">${(torrent.size || 0).toFixed(1)} GB | Score: ${scoreDisplay}</p>
                            <p class="torresult-item">${torrent.source || 'N/A'}</p>
                            <span class="cache-status ${torrent.cached === 'Yes' ? 'cached' :
                                          torrent.cached === 'No' ? 'not-cached' :
                                          torrent.cached === 'Not Checked' ? 'not-checked' :
                                          torrent.cached === 'N/A' ? 'check-unavailable' : 'unknown'}">${torrent.cached || 'N/A'}</span>
                        </div>
                    </button>
                `;

                torResDiv.onclick = function() {
                    const torrentData = {
                        title: title, year: year, version: version, media_type: mediaType,
                        season: season || null, episode: episode || null, tmdb_id: mediaId,
                        genres: genre_ids, original_title: torrent.original_title
                    };

                    if (isFilteredOut) {
                        const confirmationMessage = `This item was filtered for the following reason:\n\n'${torrent.filter_reason || 'No specific reason provided'}'.\n\nDo you want to add it anyway?`;
                        if (typeof showPopup === 'function') {
                            showPopup({
                                type: POPUP_TYPES.CONFIRM,
                                title: 'Add Filtered Item?',
                                message: confirmationMessage,
                                confirmText: 'Add Anyway',
                                onConfirm: () => {
                                    addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                                }
                            });
                        } else if (confirm(confirmationMessage)) {
                            addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                        }
                    } else {
                        addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                    }
                };

                gridContainer.appendChild(torResDiv);
            });
            overlayContent.appendChild(gridContainer);
        } else {
            // CLASSIC THEME - Simple grid
            const header = document.createElement('h3');
            header.textContent = `Torrent Results for ${title} (${year})`;
            overlayContent.appendChild(header);

            if (allDisplayItems.length === 0) {
                const noResults = document.createElement('p');
                noResults.textContent = 'No torrents found.';
                noResults.style.textAlign = 'center';
                noResults.style.padding = '2rem';
                overlayContent.appendChild(noResults);
            } else {
                const gridContainer = document.createElement('div');
                gridContainer.style.display = 'flex';
                gridContainer.style.flexWrap = 'wrap';
                gridContainer.style.gap = '20px';
                gridContainer.style.justifyContent = 'center';
                gridContainer.style.padding = '1rem 0';

                allDisplayItems.forEach((torrent) => {
                    const isFilteredOut = torrent.__isActuallyFilteredOut;
                    const torResDiv = document.createElement('div');
                    torResDiv.className = 'torresult' + (isFilteredOut ? ' filtered-out-item' : '');

                    const bitrateDisplay = torrent.bitrate ? ` | ${(torrent.bitrate / 1000).toFixed(1)} Mbps` : '';
                    const scoreDisplay = isFilteredOut ? (torrent.filter_reason || 'Filtered') : (torrent.score_breakdown?.total_score || 'N/A');

                    torResDiv.innerHTML = `
                        <button ${isFilteredOut ? 'style="cursor:pointer; opacity:0.7;"' : ''}>
                            <div class="torresult-info">
                                <p class="torresult-title">${torrent.title || torrent.original_title || 'N/A'}</p>
                                <p class="torresult-item">${(torrent.size || 0).toFixed(1)} GB${bitrateDisplay} | Score: ${scoreDisplay}</p>
                                <p class="torresult-item">${torrent.source || 'N/A'}</p>
                                <span class="cache-status ${torrent.cached === 'Yes' ? 'cached' :
                                              torrent.cached === 'No' ? 'not-cached' :
                                              torrent.cached === 'Not Checked' ? 'not-checked' :
                                              torrent.cached === 'N/A' ? 'check-unavailable' : 'unknown'}">${torrent.cached || 'N/A'}</span>
                            </div>
                        </button>
                    `;

                    torResDiv.onclick = function() {
                        const torrentData = {
                            title: title, year: year, version: version, media_type: mediaType,
                            season: season || null, episode: episode || null, tmdb_id: mediaId,
                            genres: genre_ids, original_title: torrent.original_title
                        };

                        if (isFilteredOut) {
                            const confirmationMessage = `This item was filtered for the following reason:\n\n'${torrent.filter_reason || 'No specific reason provided'}'.\n\nDo you want to add it anyway?`;
                            if (typeof showPopup === 'function') {
                                showPopup({
                                    type: POPUP_TYPES.CONFIRM,
                                    title: 'Add Filtered Item?',
                                    message: confirmationMessage,
                                    confirmText: 'Add Anyway',
                                    onConfirm: () => {
                                        addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                                    }
                                });
                            } else if (confirm(confirmationMessage)) {
                                addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                            }
                        } else {
                            addToRealDebrid(torrent.magnet, {...torrent, ...torrentData});
                        }
                    };

                    gridContainer.appendChild(torResDiv);
                });
                overlayContent.appendChild(gridContainer);
            }
        }

        // Show overlay
        document.body.classList.add('modal-open');
        overlay.style.display = 'flex';

        // Setup close button
        const closeButton = overlay.querySelector('.close-btn');
        if (closeButton) {
            closeButton.onclick = function() { closeOverlay(); };
        }

        // Click outside to close
        overlay.onclick = function(event) {
            if (event.target === overlay) {
                closeOverlay();
            }
        };
    }

    // Close overlay
    function closeOverlay() {
        const overlay = document.getElementById('overlay');
        if (overlay) {
            overlay.style.display = 'none';
            document.body.classList.remove('modal-open');
        }
    }

    // Add torrent to Real-Debrid
    async function addToRealDebrid(magnet, torrentData) {
        if (!magnet) {
            displayError('No magnet link available');
            return;
        }

        showLoadingState('Adding torrent, please wait...');

        try {
            const response = await fetch('/add_item', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    magnet: magnet,
                    title: torrentData.title,
                    year: torrentData.year,
                    version: torrentData.version,
                    media_type: torrentData.media_type,
                    season: torrentData.season,
                    episode: torrentData.episode,
                    tmdb_id: torrentData.tmdb_id,
                    genres: torrentData.genres,
                    original_title: torrentData.original_title
                })
            });

            const result = await response.json();
            hideLoadingState();

            if (result.success) {
                displaySuccess(result.message || 'Successfully added to Real-Debrid');
                closeOverlay();
            } else {
                displayError(result.error || 'Failed to add to Real-Debrid');
            }
        } catch (error) {
            hideLoadingState();
            console.error('Error adding to Real-Debrid:', error);
            displayError('An error occurred while adding to Real-Debrid');
        }
    }

    // Fetch versions on page load
    fetchVersions();

    // Initialize modal listeners
    initializeModalListeners();

})();
