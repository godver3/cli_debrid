let selectedContent = null;
let availableVersions = [];

// Initialize everything when the DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    // Initialize Loading object
    window.Loading.init();
    
    // Initialize event listeners
    initializeEventListeners();
    
    // Fetch versions
    fetchVersions();
});

function initializeEventListeners() {
    // Add form submit event listener
    const searchForm = document.getElementById('search-form');
    if (searchForm) {
        searchForm.addEventListener('submit', searchMedia);
    }

    // Event listeners for modal buttons
    const confirmVersionsBtn = document.getElementById('confirmVersions');
    if (confirmVersionsBtn) {
        confirmVersionsBtn.addEventListener('click', () => {
            const selectedVersions = Array.from(document.querySelectorAll('input[name="versions"]:checked'))
                .map(checkbox => checkbox.value);
            
            if (selectedVersions.length === 0) {
                window.showPopup({
                    type: window.POPUP_TYPES.WARNING,
                    title: 'Warning',
                    message: 'Please select at least one version'
                });
                return;
            }
            
            requestContent(selectedContent, selectedVersions);
            document.getElementById('versionModal').style.display = 'none';
        });
    }

    const cancelVersionsBtn = document.getElementById('cancelVersions');
    if (cancelVersionsBtn) {
        cancelVersionsBtn.addEventListener('click', () => {
            document.getElementById('versionModal').style.display = 'none';
        });
    }

    // Close modal when clicking outside
    window.addEventListener('click', (event) => {
        const modal = document.getElementById('versionModal');
        if (event.target === modal) {
            modal.style.display = 'none';
        }
    });
}

async function fetchVersions() {
    try {
        const response = await fetch('/content/versions');
        const data = await response.json();
        if (data.versions) {
            availableVersions = data.versions;
        }
    } catch (error) {
        console.error('Error fetching versions:', error);
        window.showPopup({
            type: window.POPUP_TYPES.ERROR,
            title: 'Error',
            message: 'Error fetching versions'
        });
    }
}

async function searchMedia(event) {
    event.preventDefault();
    const searchTerm = document.querySelector('input[name="search_term"]').value;
    
    if (!searchTerm) return;

    window.Loading.show();
    
    try {
        const response = await fetch('/content/search', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ search_term: searchTerm })
        });

        const results = await response.json();
        displayResults(results);
    } catch (error) {
        console.error('Error searching:', error);
        window.showPopup({
            type: window.POPUP_TYPES.ERROR,
            title: 'Error',
            message: 'Error searching for content'
        });
    } finally {
        window.Loading.hide();
    }
}

function displayResults(results) {
    const resultsContainer = document.getElementById('searchResults');
    resultsContainer.innerHTML = '';

    results.forEach(result => {
        const card = document.createElement('div');
        card.className = 'media-card';
        
        const posterUrl = result.posterPath || '/static/images/no-poster.jpg';
        const title = result.title || 'Untitled';
        
        card.innerHTML = `
            <div class="media-poster">
                <img src="${posterUrl}" alt="${title}" loading="lazy">
                <span class="media-type-badge">
                    ${result.mediaType === 'movie' ? 'MOVIE' : 'TV'}
                </span>
                <div class="media-overlay">
                    <div class="media-year">${result.year || 'N/A'}</div>
                    <h3 class="media-title">${title}</h3>
                </div>
            </div>
        `;
        
        // Add click event to show version modal
        card.addEventListener('click', () => showVersionModal(result));
        
        resultsContainer.appendChild(card);
    });
}

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
            <input type="checkbox" id="${version}" name="versions" value="${version}">
            <label for="${version}">${version}</label>
        `;
        versionCheckboxes.appendChild(div);
    });
    
    modal.style.display = 'block';
}

async function requestContent(content, selectedVersions) {
    window.Loading.show();
    try {
        const response = await fetch('/content/request', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                ...content,
                versions: selectedVersions
            })
        });

        const result = await response.json();
        if (result.success) {
            window.showPopup({
                type: window.POPUP_TYPES.SUCCESS,
                title: 'Success',
                message: `Successfully requested ${content.title}`,
                autoClose: 3000
            });
        } else {
            window.showPopup({
                type: window.POPUP_TYPES.ERROR,
                title: 'Error',
                message: result.error || 'Error requesting content'
            });
        }
    } catch (error) {
        console.error('Error requesting content:', error);
        window.showPopup({
            type: window.POPUP_TYPES.ERROR,
            title: 'Error',
            message: 'Error requesting content'
        });
    } finally {
        window.Loading.hide();
    }
} 