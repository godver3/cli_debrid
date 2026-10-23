/* Moved from templates/statistics.html. */
document.addEventListener('DOMContentLoaded', function() {
    // Initialize external links modal
    initializeExternalLinksModal();
});

// Function to initialize the external links modal
function initializeExternalLinksModal() {
    console.log('Initializing external links modal');

    // Get modal elements
    const modal = document.getElementById('external-links-modal');
    const modalTitle = document.getElementById('modal-title');
    const traktLink = document.getElementById('trakt-link');
    const tmdbLink = document.getElementById('tmdb-link');
    const imdbLink = document.getElementById('imdb-link');
    const closeModal = document.querySelector('.close-modal');

    // Attach click event listeners to clickable items
    reattachExternalLinksListeners();

    // Close modal when clicking the close button
    closeModal.addEventListener('click', function() {
        modal.style.display = 'none';
    });

    // Close modal when clicking outside the modal content
    window.addEventListener('click', function(event) {
        if (event.target === modal) {
            modal.style.display = 'none';
        }
    });
}
