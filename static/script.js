let deleteInProgress = false;
let isEventListenerAttached = false;

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

    console.log('DOMContentLoaded event fired');
    attachDeleteEventListener();

    // Make selectSeason function globally accessible    
    const hamburgerMenu = document.querySelector('.hamburger-menu');
    const navMenu = document.getElementById('navMenu');

    // Remove any existing event listener for delete buttons
    const databaseTable = document.getElementById('database-table');
    if (databaseTable) {
        databaseTable.removeEventListener('click', handleDeleteClick);
        // Add the event listener only once
        databaseTable.addEventListener('click', handleDeleteClick);
    }

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
            selectSeason(data.tmdb_id, data.title, data.year, 'tv', 'null', 'null', 'True', data.genre_ids, data.vote_average, data.backdrop_path, data.show_overview)
        };
        return movieElement;
    }
    
    function get_trendingMovies() {
        toggleResultsVisibility('get_trendingMovies');
        fetch('/movies_trending', {
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
        fetch('/shows_trending', {
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
        fetch('/trakt_auth_status')
            .then(response => response.json())
            .then(status => {
                if (status.status == 'authorized') {
                    get_trendingMovies();
                    get_trendingShows();
                }
                });
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

    // Initialize program controls for admin users
    if (typeof userRole !== 'undefined' && userRole === 'admin') {
        initializeProgramControls();
    }

    // Initial refresh
    refreshCurrentPage();
});

function handleDeleteClick(e) {
    console.log('Delete click handler called');
    if (e.target && e.target.classList.contains('delete-item')) {
        e.preventDefault();
        e.stopPropagation();
        const itemId = e.target.getAttribute('data-item-id');
        if (!deleteInProgress && confirm('Are you sure you want to delete this item?')) {
            deleteItem(itemId);
        }
    }
}

function attachDeleteEventListener() {
    console.log('Attaching delete event listener');
    if (!isEventListenerAttached) {
        const databaseTable = document.getElementById('database-table');
        if (databaseTable) {
            databaseTable.removeEventListener('click', handleDeleteClick);
            databaseTable.addEventListener('click', handleDeleteClick);
            isEventListenerAttached = true;
            console.log('Delete event listener attached');
        }
    }
}

// Move deleteItem function outside of DOMContentLoaded event
function deleteItem(itemId) {
    if (deleteInProgress) return;
    deleteInProgress = true;

    fetch('/delete_item', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ item_id: itemId })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            const row = document.querySelector(`button[data-item-id="${itemId}"]`);
            if (row) {
                const tableRow = row.closest('tr');
                if (tableRow) {
                    tableRow.remove();
                }
            }
            console.log('Item deleted successfully');
        } else {
            alert('Failed to delete item: ' + data.error);
        }
    })
    .catch(error => {
        console.error('Error:', error);
        alert('An error occurred while deleting the item.');
    })
    .finally(() => {
        deleteInProgress = false;
    });
}

console.log('Script ended');
