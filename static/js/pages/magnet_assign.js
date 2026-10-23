/* Moved from templates/magnet_assign.html. */
const notificationsUrl = window.pageData.magnet_assign.static_notifications_js;
let showPopup, POPUP_TYPES;

import(notificationsUrl).then(module => {
    showPopup = module.showPopup;
    POPUP_TYPES = module.POPUP_TYPES;
}).catch(error => console.error('Error loading notifications module:', error));

document.addEventListener('DOMContentLoaded', function() {
    // ── Apply backdrop image on mobile only ──
    if (window.matchMedia('(max-width: 768px)').matches) {
        document.querySelectorAll('.ma-card-poster-wrap[data-backdrop]').forEach(el => {
            el.style.backgroundImage = `url('${el.dataset.backdrop}')`;
            el.classList.add('has-backdrop');
        });
    }

    // ── Auto-fill and submit when prefill_title is in URL (search step only) ──
    const urlParams = new URLSearchParams(window.location.search);
    const prefillTitle = urlParams.get('prefill_title');
    const prefillType = urlParams.get('prefill_type');
    const prefillId = urlParams.get('prefill_id');
    const searchInput = document.getElementById('search_term');
    const contentTypeSelect = document.getElementById('content_type');
    // Only auto-submit if we have a title but no prefill_id (prefill_id means the route
    // already resolved the show and rendered results directly — don't re-submit)
    if (prefillTitle && !prefillId && searchInput && searchInput.form) {
        searchInput.value = prefillTitle;
        if (prefillType && contentTypeSelect) {
            const val = prefillType === 'tv' ? 'show' : prefillType;
            if ([...contentTypeSelect.options].some(o => o.value === val)) contentTypeSelect.value = val;
        }
        searchInput.form.submit();
        return;
    }

    // ── TV show selection handling ──
    document.querySelectorAll('.ma-card').forEach(card => {
        const mediaTypeInput = card.querySelector('input[name="media_type"]');
        const mediaType = mediaTypeInput?.value;
        const tvSection = card.querySelector('.ma-tv-show-selection');
        if (mediaType === 'movie' && tvSection) {
            tvSection.style.display = 'none';
        }
    });

    document.querySelectorAll('.ma-tv-show-selection').forEach(tvShowSelection => {
        const tmdbId = tvShowSelection.dataset.tmdbId;
        const selectionType = tvShowSelection.querySelector('.selection-type');
        const seasonSelection = tvShowSelection.querySelector('.season-selection');
        const episodeSelection = tvShowSelection.querySelector('.episode-selection');
        const seasonsSelect = tvShowSelection.querySelector('.seasons-select');
        const seasonSelect = tvShowSelection.querySelector('.season-select');
        const episodeSelect = tvShowSelection.querySelector('.episode-select');

        const form = tvShowSelection.closest('.ma-card-body').querySelector('.ma-magnet-form');
        const magnetInputWrap = form.querySelector('.ma-magnet-wrap');
        const versionInputWrap = form.querySelector('.ma-version-wrap');
        const submitBtnWrap = form.querySelector('.ma-submit-wrap');
        const selectionTypeInput = form.querySelector('.selection-type-input');
        const selectedSeasonsInput = form.querySelector('.selected-seasons-input');
        const seasonInput = form.querySelector('.season-input');
        const episodeInput = form.querySelector('.episode-input');

        const prefillDataElement = document.getElementById('prefill-data');
        const prefillSelection = (prefillDataElement?.dataset?.prefillSelection || '').trim();
        const prefillSeasons = (prefillDataElement?.dataset?.prefillSeasons || '').trim();
        const prefillEpisode = (prefillDataElement?.dataset?.prefillEpisode || '').trim();

        const orderSelect = tvShowSelection.querySelector('.episode-order-select');
        const orderHint = tvShowSelection.querySelector('.episode-order-hint');
        const orderInput = form.querySelector('.episode-order-input');
        let ordersLoaded = false;
        const orderHintText = orderHint.textContent;
        const orderPlexSupported = {};

        async function loadEpisodeOrders() {
            if (ordersLoaded) return;
            ordersLoaded = true;
            const loading = document.createElement('option');
            loading.disabled = true;
            loading.textContent = 'Loading orders…';
            orderSelect.appendChild(loading);
            try {
                const response = await fetch(`${window.pageData.magnet_assign.magnet_episode_orders_url}?tmdb_id=${tmdbId}`);
                const data = await response.json();
                loading.remove();
                if (!data.success) throw new Error(data.error || 'Could not list episode orders');
                (data.orders || []).forEach(order => {
                    orderPlexSupported[order.id] = order.plex_supported;
                    const option = document.createElement('option');
                    option.value = order.id;
                    option.textContent = order.id === data.current ? `${order.name} — current` : order.name;
                    orderSelect.appendChild(option);
                });
                if (data.library_episodes > 0) {
                    orderSelect.disabled = true;
                    orderHint.textContent = `Already in your library (${data.library_episodes} episodes): change its order from the show page.`;
                }
            } catch (error) {
                loading.remove();
                ordersLoaded = false;
                console.error('Error loading episode orders:', error);
                showErrorToast('Failed to load episode orders');
            }
        }

        orderSelect.addEventListener('focus', loadEpisodeOrders);
        orderSelect.addEventListener('mousedown', loadEpisodeOrders);
        orderSelect.addEventListener('change', function() {
            orderInput.value = this.value;
            // Same warning as Show options in the library
            orderHint.textContent = orderPlexSupported[this.value] === false
                ? "Plex has no matching episode ordering for this order. Once the show is in Plex, set its ordering there by hand if its episode numbers don't line up."
                : orderHintText;
            orderHint.style.color = orderPlexSupported[this.value] === false ? '#f0a14a' : '';
            hideMagnetFields();
            loadSeasonData(this.value).then(() => updateSelectionFields(selectionType.value));
        });

        async function loadSeasonData(order = '') {
            try {
                const orderParam = order ? `&order=${encodeURIComponent(order)}` : '';
                const response = await fetch(`${window.pageData.magnet_assign.magnet_get_season_data_url}?tmdb_id=${tmdbId}&allow_specials=true${orderParam}`);
                if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
                const data = await response.json();

                seasonSelect.seasonData = data;
                seasonsSelect.innerHTML = '';
                seasonSelect.innerHTML = '';
                episodeSelect.innerHTML = '';

                Object.entries(data).forEach(([seasonNum, episodeCount]) => {
                    const seasonOption = document.createElement('option');
                    seasonOption.value = seasonNum;
                    seasonOption.textContent = seasonNum === '0'
                        ? `Specials (${episodeCount} episodes)`
                        : `Season ${seasonNum} (${episodeCount} episodes)`;
                    seasonsSelect.appendChild(seasonOption);
                    seasonSelect.appendChild(seasonOption.cloneNode(true));
                });
                seasonSelect.disabled = false;

                if (prefillSelection === 'seasons' && prefillSeasons) {
                    const seasonValues = prefillSeasons.split(',').map(s => s.trim()).filter(Boolean);
                    const optionMap = new Map(Array.from(seasonsSelect.options).map(o => [o.value, o]));
                    seasonValues.forEach(val => { const opt = optionMap.get(val); if (opt) opt.selected = true; });
                    selectedSeasonsInput.value = Array.from(seasonsSelect.selectedOptions).map(o => o.value).join(',');
                    selectionType.value = 'seasons';
                    updateSelectionFields('seasons');
                }

                if (prefillSelection === 'episode' && prefillSeasons) {
                    const seasonValue = prefillSeasons.split(',')[0]?.trim();
                    if (seasonValue && data[seasonValue]) {
                        seasonSelect.value = seasonValue;
                        seasonInput.value = seasonValue;
                        populateEpisodes(data[seasonValue]);
                        if (prefillEpisode) {
                            episodeSelect.value = prefillEpisode;
                            episodeInput.value = prefillEpisode;
                        }
                        selectionType.value = 'episode';
                        updateSelectionFields('episode');
                        if (seasonInput.value && episodeInput.value) showMagnetFields();
                    }
                }
            } catch (error) {
                console.error('Error loading season data:', error);
                showErrorToast('Failed to load season data');
            }
        }

        selectionType.addEventListener('change', function() {
            updateSelectionFields(this.value);
            selectionTypeInput.value = this.value;
        });

        const initialSelectionType = prefillSelection || selectionType.value;
        if (prefillSelection) {
            const validValues = ['all', 'seasons', 'episode'];
            if (validValues.includes(prefillSelection)) selectionType.value = prefillSelection;
        }
        updateSelectionFields(initialSelectionType);
        selectionTypeInput.value = initialSelectionType;

        seasonSelect.addEventListener('change', function() {
            const selectedSeason = this.value;
            const seasonData = this.seasonData?.[selectedSeason];
            if (seasonData) {
                populateEpisodes(seasonData);
                seasonInput.value = selectedSeason;
                episodeSelect.value = '';
                episodeInput.value = '';
                hideMagnetFields();
            } else {
                episodeSelect.innerHTML = '';
                episodeSelect.disabled = true;
                seasonInput.value = '';
                episodeInput.value = '';
                hideMagnetFields();
            }
        });

        episodeSelect.addEventListener('change', function() {
            episodeInput.value = this.value;
            if (this.value) showMagnetFields(); else hideMagnetFields();
        });

        seasonsSelect.addEventListener('change', function() {
            const selected = Array.from(this.selectedOptions).map(opt => opt.value);
            selectedSeasonsInput.value = selected.join(',');
            if (selected.length > 0) showMagnetFields(); else hideMagnetFields();
        });

        if (prefillSelection === 'seasons' && prefillSeasons) {
            Promise.resolve().then(() => {
                const seasonValues = prefillSeasons.split(',').map(s => s.trim()).filter(Boolean);
                const optionMap = new Map(Array.from(seasonsSelect.options).map(o => [o.value, o]));
                seasonValues.forEach(val => { const opt = optionMap.get(val); if (opt) opt.selected = true; });
                selectedSeasonsInput.value = Array.from(seasonsSelect.selectedOptions).map(o => o.value).join(',');
                selectionType.value = 'seasons';
                updateSelectionFields('seasons');
            });
        }

        function updateSelectionFields(type) {
            if (type !== 'episode') { seasonInput.value = ''; episodeInput.value = ''; }
            if (type !== 'seasons') { selectedSeasonsInput.value = ''; }

            switch (type) {
                case 'all':
                    seasonSelection.style.display = 'none';
                    episodeSelection.style.display = 'none';
                    showMagnetFields();
                    break;
                case 'seasons':
                    seasonSelection.style.display = 'block';
                    episodeSelection.style.display = 'none';
                    const selectedSeasons = Array.from(seasonsSelect.selectedOptions).map(o => o.value);
                    if (selectedSeasons.length > 0) {
                        selectedSeasonsInput.value = selectedSeasons.join(',');
                        showMagnetFields();
                    } else {
                        selectedSeasonsInput.value = '';
                        hideMagnetFields();
                    }
                    break;
                case 'episode':
                    seasonSelection.style.display = 'none';
                    episodeSelection.style.display = 'block';
                    hideMagnetFields();
                    if (seasonSelect.seasonData && !episodeSelect.value) {
                        if (!seasonSelect.value && seasonSelect.options.length > 0) {
                            seasonSelect.value = seasonSelect.options[0].value;
                            seasonInput.value = seasonSelect.value;
                        }
                        const seasonData = seasonSelect.seasonData[seasonSelect.value];
                        if (seasonData) {
                            populateEpisodes(seasonData);
                            if (!episodeSelect.value && episodeSelect.options.length > 0) {
                                episodeSelect.value = episodeSelect.options[0].value;
                                episodeInput.value = episodeSelect.value;
                            }
                        }
                    }
                    seasonInput.value = seasonSelect.value || '';
                    episodeInput.value = episodeSelect.value || '';
                    if (seasonInput.value && episodeInput.value) showMagnetFields(); else hideMagnetFields();
                    break;
            }
            selectionTypeInput.value = type;
        }

        function populateEpisodes(episodeCount) {
            episodeSelect.innerHTML = '';
            episodeSelect.disabled = false;
            for (let i = 1; i <= episodeCount; i++) {
                const option = document.createElement('option');
                option.value = i;
                option.textContent = `Episode ${i}`;
                episodeSelect.appendChild(option);
            }
        }

        function showMagnetFields() {
            if (magnetInputWrap) { magnetInputWrap.style.display = 'flex'; magnetInputWrap.style.flexDirection = 'column'; }
            if (submitBtnWrap) { submitBtnWrap.style.display = 'flex'; }
        }

        function hideMagnetFields() {
            if (magnetInputWrap) magnetInputWrap.style.display = 'none';
            if (submitBtnWrap) submitBtnWrap.style.display = 'none';
        }

        loadSeasonData();
    });

    // ── Version prefill ──
    function prefillVersionIfNeeded() {
        const prefillDataElement = document.getElementById('prefill-data');
        if (!prefillDataElement) return;
        const isPrefilled = JSON.parse(prefillDataElement.dataset.isPrefilled || 'false');
        const prefillVersion = prefillDataElement.dataset.prefillVersion;
        if (isPrefilled && prefillVersion) {
            const versionSelect = document.querySelector('.ma-card .ma-version-select');
            if (versionSelect) {
                setTimeout(() => {
                    if (Array.from(versionSelect.options).some(opt => opt.value === prefillVersion)) {
                        versionSelect.value = prefillVersion;
                    }
                }, 100);
            }
        }
    }

    // ── Load versions ──
    const versionsUrl = window.pageData.magnet_assign.magnet_get_versions_url;
    fetch(versionsUrl)
        .then(r => r.json())
        .then(versions => {
            document.querySelectorAll('.ma-version-select').forEach(select => {
                select.innerHTML = '<option value="">Select Version</option>';
                versions.forEach(version => {
                    const option = document.createElement('option');
                    option.value = version;
                    option.textContent = version;
                    select.appendChild(option);
                });
            });
            prefillVersionIfNeeded();
        })
        .catch(error => console.error('Error loading versions:', error));

    // ── File input UI ──
    document.querySelectorAll('.ma-file-input').forEach(fileInput => {
        fileInput.addEventListener('change', function() {
            const label = this.closest('.ma-file-btn');
            const magnetInput = this.closest('form').querySelector('.ma-magnet-input');
            if (this.files.length > 0) {
                const fileName = this.files[0].name;
                label.classList.add('has-file');
                label.querySelector('.ma-file-label').textContent = fileName;
                if (magnetInput) magnetInput.value = '';
            } else {
                label.classList.remove('has-file');
                label.querySelector('.ma-file-label').textContent = 'Choose torrent file';
            }
        });
    });

    // ── Magnet input live state ──
    document.querySelectorAll('.ma-magnet-input').forEach(input => {
        input.addEventListener('input', function() {
            this.classList.toggle('has-value', !!this.value.trim());
            if (this.value.trim()) {
                const form = this.closest('form');
                const fileInput = form?.querySelector('.ma-file-input');
                if (fileInput) {
                    fileInput.value = '';
                    const label = fileInput.closest('.ma-file-btn');
                    if (label) {
                        label.classList.remove('has-file');
                        label.querySelector('.ma-file-label').textContent = 'Choose torrent file';
                    }
                }
            }
        });
    });

    // ── Async form submission ──
    document.querySelectorAll('.ma-magnet-form').forEach(form => {
        form.addEventListener('submit', async function(e) {
            e.preventDefault();
            const submitButton = form.querySelector('.ma-btn-orange-solid');
            if (submitButton) submitButton.disabled = true;
            Loading.show();

            try {
                const magnetLink = form.querySelector('.ma-magnet-input')?.value || '';
                const torrentFile = form.querySelector('.ma-file-input')?.files?.[0];
                const version = form.querySelector('.ma-version-select')?.value;

                if (!magnetLink.trim() && !torrentFile) throw new Error('Please enter a magnet link or upload a torrent file.');
                if (magnetLink.trim() && torrentFile) throw new Error('Please provide either a magnet link OR a torrent file, not both.');
                if (!version) throw new Error('Please select a version.');

                const response = await fetch(form.getAttribute('action'), {
                    method: 'POST',
                    body: new FormData(form)
                });

                if (response.ok) {
                    const data = await response.json();
                    if (data.success && data.redirect_url) {
                        window.location.href = data.redirect_url;
                    } else {
                        throw new Error(data.error || 'Backend indicated success but provided no redirect URL.');
                    }
                } else {
                    const errorData = await response.json();
                    throw new Error(errorData.error || `Server responded with status ${response.status}`);
                }
            } catch (error) {
                console.error('Error preparing assignment:', error);
                showErrorToast(error.message || 'An unexpected error occurred.');
                setTimeout(() => { window.location.reload(); }, 10000);
            } finally {
                Loading.hide();
                if (submitButton && document.contains(submitButton)) submitButton.disabled = false;
            }
        });
    });

    // ── URL state management (debounced) ──
    function debounce(func, wait) {
        let timeout;
        return function(...args) {
            clearTimeout(timeout);
            timeout = setTimeout(() => func(...args), wait);
        };
    }

    function updateURLState(tmdbId, selectionType, season, episode, seasons) {
        const url = new URL(window.location);
        url.searchParams.set('tmdb_id', tmdbId);
        url.searchParams.set('selection_type', selectionType);
        if (selectionType === 'episode' && season && episode) {
            url.searchParams.set('season', season);
            url.searchParams.set('episode', episode);
        } else if (selectionType === 'seasons' && seasons) {
            url.searchParams.set('seasons', seasons);
        } else {
            url.searchParams.delete('season');
            url.searchParams.delete('episode');
            url.searchParams.delete('seasons');
        }
        window.history.pushState({}, '', url);
    }

    document.querySelectorAll('.ma-tv-show-selection').forEach(tvShowSelection => {
        const tmdbId = tvShowSelection.dataset.tmdbId;
        const selectionType = tvShowSelection.querySelector('.selection-type');
        const seasonSelect = tvShowSelection.querySelector('.season-select');
        const episodeSelect = tvShowSelection.querySelector('.episode-select');
        const seasonsSelect = tvShowSelection.querySelector('.seasons-select');

        if (selectionType) selectionType.addEventListener('change', debounce(function() {
            updateURLState(tmdbId, this.value, null, null, null);
        }, 300));
        if (seasonSelect) seasonSelect.addEventListener('change', debounce(function() {
            updateURLState(tmdbId, 'episode', this.value, episodeSelect?.value, null);
        }, 300));
        if (episodeSelect) episodeSelect.addEventListener('change', debounce(function() {
            updateURLState(tmdbId, 'episode', seasonSelect?.value, this.value, null);
        }, 300));
        if (seasonsSelect) seasonsSelect.addEventListener('change', debounce(function() {
            updateURLState(tmdbId, 'seasons', null, null, Array.from(this.selectedOptions).map(o => o.value).join(','));
        }, 300));
    });
});

// ── Toast ──
let maToastTimer;
function maShowToast(msg, color = 'blue') {
    const t = document.getElementById('ma-toast');
    t.className = 'ma-toast ma-toast-' + color;
    document.getElementById('ma-toast-msg').textContent = msg;
    void t.offsetWidth;
    t.classList.add('show');
    clearTimeout(maToastTimer);
    maToastTimer = setTimeout(() => t.classList.remove('show'), 3000);
}
function showErrorToast(msg) { maShowToast(msg, 'red'); }
