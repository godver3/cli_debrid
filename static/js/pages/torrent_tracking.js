/* Moved from templates/torrent_tracking.html. */
function showModal(modalId) {
    const modal = document.getElementById(modalId);
    const overlay = document.getElementById('overlay' + modalId.replace('detailsModal', ''));
    modal.style.display = 'block';
    overlay.style.display = 'block';
    document.body.style.overflow = 'hidden';
}

function hideModal(modalId) {
    const modal = document.getElementById(modalId);
    const overlay = document.getElementById('overlay' + modalId.replace('detailsModal', ''));
    modal.style.display = 'none';
    overlay.style.display = 'none';
    document.body.style.overflow = '';
}

// Close modal when clicking outside
window.onclick = function(event) {
    if (event.target.classList.contains('modal-overlay')) {
        const modalId = event.target.id.replace('overlay', 'detailsModal');
        hideModal(modalId);
    }
}

document.addEventListener('DOMContentLoaded', function() {
    // Keep track of active polling intervals
    const activePolls = new Map();
    const POLL_INTERVAL = 10000;  // Poll every 10 seconds after initial load
    const INITIAL_STAGGER_DELAY = 1000;  // Delay between each initial status check

    // Terminal states that don't need further polling
    const TERMINAL_STATES = ['downloaded', 'magnet_error', 'error', 'virus', 'dead'];

    // Format JSON in pre elements
    document.querySelectorAll('pre.json-formatter').forEach(function(el) {
        try {
            const content = el.textContent;
            const obj = JSON.parse(content);
            el.textContent = JSON.stringify(obj, null, 2);
        } catch (e) {
            console.error('Error formatting JSON:', e);
        }
    });

    // Function to stop polling for a hash
    function stopPolling(hash) {
        if (activePolls.has(hash)) {
            clearInterval(activePolls.get(hash));
            activePolls.delete(hash);
            console.log(`Stopped polling for hash: ${hash}`);
        }
    }

    // Set up intersection observer for lazy loading
    const observer = new IntersectionObserver((entries, observer) => {
        entries.forEach((entry, index) => {
            const row = entry.target;
            const hash = row.dataset.hash;

            if (entry.isIntersecting) {
                // Start polling when element becomes visible
                if (!activePolls.has(hash)) {
                    // Stagger the initial check based on the index
                    setTimeout(() => {
                        checkTorrentStatus(hash);
                        // Set up regular polling after initial staggered load
                        const intervalId = setInterval(() => checkTorrentStatus(hash), POLL_INTERVAL);
                        activePolls.set(hash, intervalId);
                    }, index * INITIAL_STAGGER_DELAY);
                }
            } else {
                // Stop polling when element is no longer visible
                stopPolling(hash);
            }
        });
    }, {
        root: null,
        rootMargin: '50px',
        threshold: 0.1
    });

    // Observe all torrent entries
    document.querySelectorAll('.torrent-entry').forEach(row => {
        observer.observe(row);
    });

    function checkTorrentStatus(hash) {
        const statusCells = document.querySelectorAll(`.status-cell[data-hash="${hash}"]`);

        if (statusCells.length === 0) {
            console.warn(`No status cells found for hash: ${hash}`);
            return;
        }

        fetch(`/debug/verify_torrent/${hash}`)
            .then(response => {
                if (!response.ok) {
                    if (response.status === 429) {
                        return new Promise((resolve) => {
                            setTimeout(() => {
                                resolve(checkTorrentStatus(hash));
                            }, 2000);
                        });
                    }
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                return response.json();
            })
            .then(data => {
                console.log(`Raw response for ${hash}:`, data);

                // Handle undefined or null data
                if (!data) {
                    throw new Error('No data received from server');
                }

                // Update all matching status cells
                statusCells.forEach(cell => {
                    // Create the badge HTML
                    let badge;
                    if (data.error) {
                        badge = `<span class="status-badge error" title="Error: ${data.error}">Error</span>`;
                    } else if (data.is_present === true) {
                        const status = data.status ? data.status.charAt(0).toUpperCase() + data.status.slice(1) : 'Present';
                        const badgeClass = status.toLowerCase() === 'downloading' ? 'downloading' : 'present';
                        badge = `<span class="status-badge ${badgeClass}" title="${status}">${status}</span>`;
                    } else if (data.is_present === false) {
                        const reason = data.removal_reason ? data.removal_reason : '';
                        badge = `<span class="status-badge removed" title="Removed: ${reason}">Removed</span>`;
                    } else {
                        badge = '<span class="status-badge error">Unknown</span>';
                    }

                    // Force a DOM reflow by accessing offsetHeight
                    cell.innerHTML = '';
                    void cell.offsetHeight;
                    cell.innerHTML = badge;
                    console.log(`Updated status cell for ${hash}:`, cell.innerHTML);
                });

                // Stop polling for terminal states
                if (data.is_present === false || (data.status && TERMINAL_STATES.includes(data.status.toLowerCase()))) {
                    console.log(`Reached terminal state for hash ${hash}: ${data.status}`);
                    stopPolling(hash);
                }
            })
            .catch(error => {
                console.error('Error checking torrent status:', error);
                const errorBadge = `<span class="status-badge error" title="${error.message}">Error</span>`;
                statusCells.forEach(cell => {
                    cell.innerHTML = errorBadge;
                });
            });
    }

    // Add CSS for warning badge
    const style = document.createElement('style');
    style.textContent = `
        .status-badge.warning {
            background-color: #ffc107;
            color: #000;
        }
    `;
    document.head.appendChild(style);
});
