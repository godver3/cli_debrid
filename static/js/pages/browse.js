/* Moved from templates/browse.html. */
class Modal {
    constructor(modalId) {
        this.modal = document.getElementById(modalId);
        this.isOpen = false;

        // Close when clicking outside the modal
        this.modal.addEventListener('click', (e) => {
            if (e.target === this.modal) {
                this.hide();
            }
        });

        // Close when clicking close button
        const closeBtn = this.modal.querySelector('.btn-close');
        if (closeBtn) {
            closeBtn.addEventListener('click', () => this.hide());
        }
    }

    show() {
        this.modal.classList.add('show');
        document.body.style.overflow = 'hidden';
        this.isOpen = true;
    }

    hide() {
        this.modal.classList.remove('show');
        document.body.style.overflow = '';
        this.isOpen = false;
    }
}

document.addEventListener('DOMContentLoaded', function() {
    const modal = new Modal('versionsModal');
    const modalTitle = document.querySelector('.modal-title');
    const modalBody = document.querySelector('.modal-body');

    // Add click handlers to all buttons that open the modal
    document.querySelectorAll('[data-modal-target]').forEach(button => {
        button.addEventListener('click', (e) => {
            const title = button.dataset.title;
            const mediaType = button.dataset.mediaType;
            const content = JSON.parse(button.dataset.content);

            modalTitle.textContent = title;
            modalBody.innerHTML = '';

            if (mediaType === 'movies') {
                modalBody.innerHTML = '<h6 class="mb-3">Available Versions</h6>';
                content.forEach(version => {
                    const versionItem = document.createElement('div');
                    versionItem.className = 'version-item';
                    versionItem.onclick = function() {
                        modal.hide();
                        window.location.href = `/video/${version.id}`;
                    };

                    const fileInfo = document.createElement('small');
                    fileInfo.textContent = version.filled_by_file || version.location_on_disk;
                    versionItem.appendChild(fileInfo);

                    if (version.version) {
                        const badge = document.createElement('span');
                        badge.className = 'badge bg-secondary ms-2';
                        badge.textContent = version.version;
                        versionItem.appendChild(badge);
                    }

                    modalBody.appendChild(versionItem);
                });
            } else {
                // TV show content handling
                const seasons = content.seasons;
                Object.entries(seasons)
                    .sort(([a], [b]) => Number(a) - Number(b))
                    .forEach(([seasonNum, episodes]) => {
                        const seasonHeader = document.createElement('div');
                        seasonHeader.className = 'season-header mb-2';
                        seasonHeader.innerHTML = `<h6>Season ${seasonNum}</h6>`;
                        modalBody.appendChild(seasonHeader);

                        episodes.sort((a, b) => a.episode_number - b.episode_number)
                            .forEach(episode => {
                                const episodeItem = document.createElement('div');
                                episodeItem.className = 'version-item';
                                episodeItem.onclick = function() {
                                    modal.hide();
                                    window.location.href = `/video/${episode.id}`;
                                };

                                const header = document.createElement('div');
                                header.className = 'd-flex justify-content-between align-items-center mb-1';

                                const episodeNum = document.createElement('span');
                                episodeNum.className = 'episode-number';
                                episodeNum.textContent = `E${episode.episode_number}`;
                                header.appendChild(episodeNum);

                                if (episode.version) {
                                    const badge = document.createElement('span');
                                    badge.className = 'badge bg-secondary';
                                    badge.textContent = episode.version;
                                    header.appendChild(badge);
                                }
                                episodeItem.appendChild(header);

                                if (episode.episode_title) {
                                    const title = document.createElement('div');
                                    title.className = 'episode-title mb-1';
                                    title.textContent = episode.episode_title;
                                    episodeItem.appendChild(title);
                                }

                                const fileInfo = document.createElement('small');
                                fileInfo.textContent = episode.filled_by_file || episode.location_on_disk;
                                episodeItem.appendChild(fileInfo);

                                modalBody.appendChild(episodeItem);
                            });
                    });
            }

            modal.show();
        });
    });
});
