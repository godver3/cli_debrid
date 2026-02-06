/**
 * Trailer Modal Functionality
 * Shared across library_show, library_movie, and discover_details pages
 */

(function() {
    'use strict';

    /**
     * Initialize trailer button functionality
     * @param {string} tmdbId - The TMDB ID
     * @param {string} mediaType - Either 'show' or 'movie'
     */
    window.initializeTrailerButton = async function(tmdbId, mediaType) {
        const trailerBtn = document.getElementById('btn-trailer');

        if (!trailerBtn || !tmdbId) {
            if (trailerBtn) {
                trailerBtn.style.display = 'none';
            }
            return;
        }

        // Check if already initialized to prevent duplicate listeners
        if (trailerBtn.dataset.initialized === 'true') {
            return;
        }

        // Check if trailer exists before showing button
        try {
            const response = await fetch(`/library/api/trailer/${mediaType}/${tmdbId}`);
            const data = await response.json();

            if (data.success && data.trailer) {
                // Trailer exists, show button and add click handler
                trailerBtn.style.display = 'inline-flex';
                trailerBtn.dataset.initialized = 'true';

                // Store trailer data to avoid re-fetching
                trailerBtn.dataset.trailerKey = data.trailer.key;
                trailerBtn.dataset.trailerName = data.trailer.name;

                // Add click handler
                trailerBtn.addEventListener('click', function() {
                    showTrailerModal(data.trailer.key, data.trailer.name);
                });
            } else {
                // No trailer available, keep button hidden
                trailerBtn.style.display = 'none';
            }
        } catch (error) {
            console.error('Error checking trailer availability:', error);
            trailerBtn.style.display = 'none';
        }
    };

    /**
     * Fetch trailer from API and display in modal
     */
    async function fetchAndShowTrailer(tmdbId, mediaType) {
        try {
            const response = await fetch(`/library/api/trailer/${mediaType}/${tmdbId}`);
            const data = await response.json();

            if (data.success && data.trailer) {
                showTrailerModal(data.trailer.key, data.trailer.name);
            } else {
                showNotification(data.error || 'No trailer available', 'error');
                // Hide trailer button if no trailer found
                const trailerBtn = document.getElementById('btn-trailer');
                if (trailerBtn) {
                    trailerBtn.style.display = 'none';
                }
            }
        } catch (error) {
            console.error('Error fetching trailer:', error);
            showNotification('Failed to load trailer', 'error');
        }
    }

    /**
     * Display trailer in a modal window
     */
    function showTrailerModal(youtubeKey, trailerName) {
        // Create modal HTML
        const modal = document.createElement('div');
        modal.className = 'trailer-modal';
        modal.innerHTML = `
            <div class="trailer-modal-backdrop"></div>
            <div class="trailer-modal-content">
                <div class="trailer-modal-header">
                    <h3>${escapeHtml(trailerName)}</h3>
                    <button class="trailer-close-btn" aria-label="Close trailer">&times;</button>
                </div>
                <div class="trailer-video-container">
                    <iframe
                        src="https://www.youtube.com/embed/${escapeHtml(youtubeKey)}?autoplay=1&rel=0"
                        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                        allowfullscreen>
                    </iframe>
                </div>
            </div>
        `;

        document.body.appendChild(modal);

        // Trigger animation
        requestAnimationFrame(() => {
            modal.classList.add('show');
        });

        // Close handlers
        const closeBtn = modal.querySelector('.trailer-close-btn');
        const backdrop = modal.querySelector('.trailer-modal-backdrop');

        function closeModal() {
            modal.classList.remove('show');
            setTimeout(() => {
                modal.remove();
            }, 200);
        }

        closeBtn.addEventListener('click', closeModal);
        backdrop.addEventListener('click', closeModal);

        // Close on ESC key
        function handleEscape(e) {
            if (e.key === 'Escape') {
                closeModal();
                document.removeEventListener('keydown', handleEscape);
            }
        }
        document.addEventListener('keydown', handleEscape);
    }

    /**
     * Show notification message
     */
    function showNotification(message, type = 'info') {
        // Check if there's a global notification function
        if (typeof window.showNotification === 'function') {
            window.showNotification(message, type);
            return;
        }

        // Fallback: simple alert-style notification
        const notification = document.createElement('div');
        notification.className = `notification notification-${type}`;
        notification.textContent = message;
        notification.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 1rem 1.5rem;
            background: ${type === 'error' ? '#dc3545' : '#28a745'};
            color: white;
            border-radius: 4px;
            z-index: 10001;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
            animation: slideInRight 0.3s ease;
        `;

        document.body.appendChild(notification);

        setTimeout(() => {
            notification.style.animation = 'slideOutRight 0.3s ease';
            setTimeout(() => notification.remove(), 300);
        }, 3000);
    }

    /**
     * Escape HTML to prevent XSS
     */
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

})();
