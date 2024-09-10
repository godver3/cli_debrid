// Use the server-side preference to set the initial state
let use24HourFormat = {{ 'true' if stats.use_24hour_format else 'false' }};
// Immediately set the checkbox state
document.getElementById('time-format-toggle').checked = use24HourFormat;

function updateAllTimes(data) {
    // Update recently aired
    const recentlyAiredList = document.querySelector('.recently-aired ul');
    if (recentlyAiredList) {
        recentlyAiredList.innerHTML = data.recently_aired.map(item => `
            <li>
                <span>${item.title} S${item.season.toString().padStart(2, '0')}E${item.episode.toString().padStart(2, '0')}</span>
                <span class="air-date">${item.formatted_time}</span>
            </li>
        `).join('');
    }

    // Update airing soon
    const airingSoonList = document.querySelector('.airing-soon ul');
    if (airingSoonList) {
        airingSoonList.innerHTML = data.airing_soon.map(item => `
            <li>
                <span>${item.title} S${item.season.toString().padStart(2, '0')}E${item.episode.toString().padStart(2, '0')}</span>
                <span class="air-date">${item.formatted_time}</span>
            </li>
        `).join('');
    }

    // Update upcoming releases
    const upcomingReleasesList = document.querySelector('.upcoming-releases ul');
    if (upcomingReleasesList) {
        upcomingReleasesList.innerHTML = data.upcoming_releases.map(item => `
            <li>
                <span>${item.titles.join(', ')}</span>
                <span class="release-date">
                    ${item.release_date ? new Date(item.release_date).toISOString().split('T')[0] : 'Date unknown'}
                </span>
            </li>
        `).join('');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const timeFormatToggle = document.getElementById('time-format-toggle');
    const compactToggle = document.getElementById('compact-toggle');
    const statisticsWrapper = document.querySelector('.statistics-wrapper');
    
    // Replace the existing time format toggle event listener with this:
    timeFormatToggle.addEventListener('change', function() {
        use24HourFormat = this.checked;
        
        fetch('/statistics/set_time_preference', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ use24HourFormat: use24HourFormat }),
            credentials: 'include',
        })
        .then(response => response.json())
        .then(data => {
            console.log('Server response:', data);
            updateAllTimes(data);
        })
        .catch(error => {
            console.error('Error saving preference:', error);
        });
    });

    // Replace the existing compact toggle event listener with this:
    compactToggle.addEventListener('change', function() {
        if (this.checked) {
            statisticsWrapper.classList.add('compact-view');
        } else {
            statisticsWrapper.classList.remove('compact-view');
        }
        
        // Save the compact view preference
        fetch('/statistics/set_compact_preference', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ compactView: this.checked }),
            credentials: 'include',
        })
        .then(response => response.json())
        .then(data => {
            console.log('Compact view preference saved:', data);
        })
        .catch(error => {
            console.error('Error saving compact view preference:', error);
        });
    });

    // Set initial compact view state
    if (compactToggle.checked) {
        statisticsWrapper.classList.add('compact-view');
    }

    function updateStatistics() {
        console.log('Attempting to fetch statistics...');
        const protocol = window.location.protocol;
        const host = window.location.host;
        fetch(`/statistics?ajax=1`, {
            method: 'GET',
            credentials: 'include'
        })
        .then(response => {
            console.log('Response received:', response);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            console.log('Data received:', data);
            // Update the statistics on the page
            document.getElementById('total-movies').textContent = data.total_movies;
            document.getElementById('total-shows').textContent = data.total_shows;
            document.getElementById('total-episodes').textContent = data.total_episodes;
            
            // Update the uptime
            const uptimeSeconds = data.uptime;
            const days = Math.floor(uptimeSeconds / 86400);
            const hours = Math.floor((uptimeSeconds % 86400) / 3600);
            const minutes = Math.floor((uptimeSeconds % 3600) / 60);
            document.getElementById('uptime').textContent = `${days} days, ${hours} hours, ${minutes} minutes`;
        
            // Update all sections, including on initial load
            updateRecentlyAired(data.recently_aired);
            updateAiringSoon(data.airing_soon);
            updateUpcomingReleases(data.upcoming_releases);
            updateRecentlyAdded(data.recently_added_movies, data.recently_added_shows);
            
            // Update timezone information
            updateTimezone(data.timezone);
            getActiveDownloads(data.active_downloads,data.limit_downloads);
        })
        .catch(error => {
            console.error('Error updating statistics:', error);
            console.error('Error details:', error.message);
            // Display an error message to the user
            const errorElement = document.createElement('div');
            errorElement.textContent = 'Failed to update statistics. Please refresh the page or try again later.';
            errorElement.style.color = 'red';
            document.querySelector('.statistics-wrapper').prepend(errorElement);
        });
    }

    function updateTimezone(timezone) {
        const timezoneElement = document.getElementById('current-timezone');
        if (timezoneElement) {
            timezoneElement.textContent = timezone;
        }
    }
    function getActiveDownloads(active_downloads, limit_downloads) {
    const rdActiveDownloads = document.getElementById('active-downloads');
    if (rdActiveDownloads) {
        console.log(`Updating RD active downloads: ${active_downloads}/${limit_downloads}`);
        rdActiveDownloads.textContent = `RD: ${active_downloads}/${limit_downloads}`;
    } else {
        console.error('Element with id "active-downloads" not found');
    }
}

    function updateRecentlyAired(recentlyAired) {
        const container = document.querySelector('.stats-box.recently-aired');
        if (container) {
            const ul = container.querySelector('ul') || document.createElement('ul');
            ul.innerHTML = recentlyAired.map(item => `
                <li>
                    <span>${item.title} S${String(item.season).padStart(2, '0')}E${String(item.episode).padStart(2, '0')}</span>
                    <span class="air-date">${item.formatted_time}</span>
                </li>
            `).join('');
            if (!container.contains(ul)) container.appendChild(ul);
        }
    }

    function updateAiringSoon(airingSoon) {
        const container = document.querySelector('.stats-box.airing-soon');
        if (container) {
            const ul = container.querySelector('ul') || document.createElement('ul');
            ul.innerHTML = airingSoon.map(item => `
                <li>
                    <span>${item.title} S${String(item.season).padStart(2, '0')}E${String(item.episode).padStart(2, '0')}</span>
                    <span class="air-date">${item.formatted_time}</span>
                </li>
            `).join('');
            if (!container.contains(ul)) container.appendChild(ul);
        }
    }

    function updateUpcomingReleases(upcomingReleases) {
        const container = document.querySelector('.stats-box.upcoming-releases');
        if (container) {
            const ul = container.querySelector('ul') || document.createElement('ul');
            ul.innerHTML = upcomingReleases.map(item => `
                <li>
                    <span>${item.titles.join(', ')}</span>
                    <span class="release-date">
                        ${item.release_date ? (typeof item.release_date === 'string' ? item.release_date : new Date(item.release_date).toISOString().split('T')[0]) : 'Date unknown'}
                    </span>
                </li>
            `).join('');
            if (!container.contains(ul)) container.appendChild(ul);
        }
    }

    function updateRecentlyAdded(movies, shows) {
        console.log('Recently added movies:', movies);
        console.log('Recently added shows:', shows);
        updateRecentlyAddedSection('Recently Added Movies', movies);
        updateRecentlyAddedSection('Recently Added Shows', shows);
    }

    function updateRecentlyAddedSection(type, items) {
        const sections = document.querySelectorAll('.stats-box.recently-added-section');
        let section = null;
        for (let s of sections) {
            if (s.querySelector('h4').textContent.trim() === type) {
                section = s;
                break;
            }
        }
        if (!section) {
            console.error(`Section for ${type} not found`);
            return;
        }
        const container = section.querySelector('.card-container') || document.createElement('div');
        container.className = 'card-container';
        if (Array.isArray(items) && items.length > 0) {
            container.innerHTML = items.map(item => {
                // Format the collected_at date
                const collectedDate = new Date(item.collected_at);
                const formattedDate = `${collectedDate.toLocaleDateString()} ${collectedDate.getHours().toString().padStart(2, '0')}:${collectedDate.getMinutes().toString().padStart(2, '0')}`;

                let additionalInfo = '';
                if (type === 'TV Shows' && item.seasons && item.latest_episode) {
                    additionalInfo = ` | Seasons: ${item.seasons.join(', ')} | Latest: S${item.latest_episode[0]}E${item.latest_episode[1]}`;
                }

                return `
                    <div class="card">
                        ${item.poster_url ? `<img src="${item.poster_url}" alt="${item.title} poster" class="card-img">` : ''}
                        <div class="card-content">
                            <h5 class="truncate">${item.title} ${item.year ? `(${item.year})` : ''}</h5>
                        </div>
                        <div class="card-hover-content">
                            <h5>${item.title} ${item.year ? `(${item.year})` : ''}</h5>
                            <p class="info-line"><span style="color: #f4f4f4;">Added:</span> ${formattedDate}</p>
                            <p class="info-line"><span style="color: #f4f4f4;">Versions:</span> ${item.versions}</p>
                            ${type === 'TV Shows' && item.seasons && item.latest_episode ? 
                                `<p class="info-line"><span style="color: #f4f4f4;">Seasons:</span> ${item.seasons.join(', ')}</p>
                                <p class="info-line"><span style="color: #f4f4f4;">Latest:</span> S${item.latest_episode[0]}E${item.latest_episode[1]}</p>` : ''}
                            <p class="info-line" style="display: inherit;"><span style="text-transform: uppercase;">${item.filled_by_title}</span></p>
                        </div>
                    </div>
                `;
            }).join('');
        } else {
            container.innerHTML = `<p>No recent additions</p>`;
        }
        if (!section.contains(container)) {
            section.appendChild(container);
        }
    }

    // Update statistics every 5 minutes
    setInterval(updateStatistics, 300000);

    console.log('Timezone from server:', '{{ stats.timezone }}');
    const timezoneElement = document.getElementById('current-timezone');
    if (timezoneElement) {
        console.log('Timezone displayed in HTML:', timezoneElement.textContent);
    } else {
        console.log('Timezone element not found in HTML');
    }

    // Initial updates
    updateStatistics();
});