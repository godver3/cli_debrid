/* Moved from templates/browse.html. */
let currentTVShow = null;
let currentContentContainer = null;

function showSeasons() {
    currentContentContainer.innerHTML = '<h6 class="mb-3">Select a Season</h6>';
    const seasonsList = document.createElement('div');
    seasonsList.className = 'list-group';

    Object.entries(currentTVShow.seasons)
        .sort(([a], [b]) => parseInt(a) - parseInt(b))
        .forEach(([season, episodes]) => {
            const seasonItem = document.createElement('button');
            seasonItem.className = 'list-group-item list-group-item-action bg-dark text-light';
            seasonItem.innerHTML = `Season ${season} <span class="badge bg-secondary float-end">${Object.keys(groupEpisodesByNumber(episodes)).length} episodes</span>`;
            seasonItem.onclick = () => showEpisodes(season, episodes);
            seasonsList.appendChild(seasonItem);
        });

    currentContentContainer.appendChild(seasonsList);
}

function groupEpisodesByNumber(episodes) {
    return episodes.reduce((acc, episode) => {
        const key = episode.episode_number;
        if (!acc[key]) {
            acc[key] = {
                episode_number: episode.episode_number,
                episode_title: episode.episode_title,
                versions: []
            };
        }
        acc[key].versions.push(episode);
        return acc;
    }, {});
}

function showEpisodes(season, episodes) {
    currentContentContainer.innerHTML = `
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h6 class="mb-0">Season ${season}</h6>
            <button class="btn btn-outline-light btn-sm" onclick="showSeasons()">Back to Seasons</button>
        </div>
    `;

    const episodesList = document.createElement('div');
    episodesList.className = 'list-group';

    const groupedEpisodes = groupEpisodesByNumber(episodes);

    Object.values(groupedEpisodes)
        .sort((a, b) => a.episode_number - b.episode_number)
        .forEach(episode => {
            const episodeItem = document.createElement('button');
            episodeItem.className = 'list-group-item list-group-item-action bg-dark text-light';
            episodeItem.innerHTML = `
                <div class="d-flex justify-content-between align-items-center">
                    <span>Episode ${episode.episode_number}: ${episode.episode_title}</span>
                    <span class="badge bg-primary">${episode.versions.length} version${episode.versions.length !== 1 ? 's' : ''}</span>
                </div>
            `;
            episodeItem.onclick = () => showVersions(season, episode);
            episodesList.appendChild(episodeItem);
        });

    currentContentContainer.appendChild(episodesList);
}

function showVersions(season, episode) {
    currentContentContainer.innerHTML = `
        <div class="d-flex justify-content-between align-items-center mb-3">
            <h6 class="mb-0">Season ${season}, Episode ${episode.episode_number}</h6>
            <button class="btn btn-outline-light btn-sm" onclick="showEpisodes(${season}, currentTVShow.seasons[${season}])">Back to Episodes</button>
        </div>
        <h6 class="mb-3">Available Versions</h6>
    `;

    episode.versions.forEach(version => {
        const versionItem = document.createElement('button');
        versionItem.className = 'version-item w-100 text-start mb-2';
        versionItem.onclick = () => {
            const versionsModal = bootstrap.Modal.getInstance(document.getElementById('versionsModal'));
            versionsModal.hide();
            window.location.href = `/video/${version.id}`;
        };

        versionItem.innerHTML = `
            <div>Version ${version.version}</div>
            <small class="d-block text-muted">${version.filled_by_file || version.location_on_disk}</small>
        `;

        currentContentContainer.appendChild(versionItem);
    });
}

document.addEventListener('DOMContentLoaded', function() {
    const versionsModal = document.getElementById('versionsModal');
    versionsModal.addEventListener('show.bs.modal', function(event) {
        const button = event.relatedTarget;
        const title = button.getAttribute('data-title');
        const mediaType = button.getAttribute('data-media-type');
        const content = JSON.parse(button.getAttribute('data-content'));

        const modalTitle = versionsModal.querySelector('.modal-title');
        const modalBody = versionsModal.querySelector('.modal-body');
        modalTitle.textContent = title;
        modalBody.innerHTML = '';

        if (mediaType === 'movies') {
            modalBody.innerHTML = '<h6 class="mb-3">Available Versions</h6>';
            content.forEach(version => {
                const versionItem = document.createElement('button');
                versionItem.className = 'version-item w-100 text-start mb-2';
                versionItem.onclick = () => {
                    const versionsModal = bootstrap.Modal.getInstance(document.getElementById('versionsModal'));
                    versionsModal.hide();
                    window.location.href = `/video/${version.id}`;
                };

                versionItem.innerHTML = `
                    <div>Version ${version.version}</div>
                    <small class="d-block text-muted">${version.filled_by_file || version.location_on_disk}</small>
                `;

                modalBody.appendChild(versionItem);
            });
        } else {
            currentTVShow = content;
            const contentContainer = document.createElement('div');
            currentContentContainer = contentContainer;
            modalBody.appendChild(contentContainer);
            showSeasons();
        }
    });
});
