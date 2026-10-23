/* Moved from templates/library_management.html. */
// Existing library management functionality
document.addEventListener('DOMContentLoaded', () => {
    loadLibraries();
    setupEventListeners();
});

function setupEventListeners() {
    const addLibraryBtn = document.getElementById('addLibraryBtn');
    const closeModal = document.getElementById('closeModal');
    const libraryForm = document.getElementById('libraryForm');
    const libraryPath = document.getElementById('libraryPath');
    const scanSymlinksBtn = document.getElementById('scanSymlinksBtn');

    addLibraryBtn.addEventListener('click', () => openModal());
    closeModal.addEventListener('click', () => closeModal());
    libraryForm.addEventListener('submit', handleLibrarySubmit);
    libraryPath.addEventListener('input', debounce(validatePath, 500));
    scanSymlinksBtn.addEventListener('click', scanForBrokenSymlinks);
}

async function loadLibraries() {
    try {
        const response = await fetch('/library_management/api/libraries');
        const libraries = await response.json();
        renderLibraries(libraries);
    } catch (error) {
        console.error('Error loading libraries:', error);
    }
}

function renderLibraries(libraries) {
    const grid = document.getElementById('libraryGrid');
    grid.innerHTML = libraries.map(library => `
        <div class="library-card">
            <div class="library-card-header">
                <h3 class="library-card-title">${library.name}</h3>
                <div class="library-card-actions">
                    <button class="btn btn-sm btn-secondary" onclick="editLibrary(${library.id})">
                        <i class="fas fa-edit"></i>
                    </button>
                    <button class="btn btn-sm btn-danger" onclick="deleteLibrary(${library.id})">
                        <i class="fas fa-trash"></i>
                    </button>
                </div>
            </div>
            <div class="library-path">${library.path}</div>
            <div class="library-status">
                <span class="status-indicator ${library.active ? 'status-active' : 'status-inactive'}"></span>
                ${library.active ? 'Active' : 'Inactive'}
            </div>
        </div>
    `).join('');
}

// New symlink scanning functionality
async function scanForBrokenSymlinks() {
    const scanStatus = document.getElementById('scanStatus');
    const symlinkStats = document.getElementById('symlinkStats');
    const brokenSymlinksList = document.getElementById('brokenSymlinksList');

    try {
        // Show scanning status
        scanStatus.classList.add('active');
        symlinkStats.style.display = 'none';
        brokenSymlinksList.style.display = 'none';

        const response = await fetch('/library_management/api/libraries/scan-broken', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({})
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const results = await response.json();

        // Update stats
        document.getElementById('totalSymlinks').textContent = results.total_symlinks;
        document.getElementById('brokenSymlinks').textContent = results.broken_count;

        // Calculate and update health score
        const healthScore = results.total_symlinks > 0 
            ? Math.round(((results.total_symlinks - results.broken_count) / results.total_symlinks) * 100)
            : 100;
        document.getElementById('healthScore').textContent = `${healthScore}%`;

        // Show stats
        symlinkStats.style.display = 'flex';

        // Render broken symlinks list
        if (results.broken_count > 0) {
            brokenSymlinksList.innerHTML = results.broken_symlinks.map(symlink => `
                <div class="broken-symlink-item">
                    <div class="broken-symlink-path">
                        <strong>Symlink:</strong> ${symlink.relative_path}
                    </div>
                    <div class="broken-symlink-target">
                        <strong>Target:</strong> ${symlink.target_path}
                    </div>
                </div>
            `).join('');
            brokenSymlinksList.style.display = 'block';
        } else {
            brokenSymlinksList.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-check-circle"></i>
                    <p>No broken symlinks found!</p>
                </div>
            `;
            brokenSymlinksList.style.display = 'block';
        }

    } catch (error) {
        console.error('Error scanning for broken symlinks:', error);
        showPopup({ type: 'error', title: 'Error', message: 'Error scanning for broken symlinks: ' + error.message, autoClose: 5000 });
        symlinkStats.style.display = 'none';
        brokenSymlinksList.innerHTML = `
            <div class="empty-state">
                <i class="fas fa-exclamation-circle"></i>
                <p>Error scanning for broken symlinks</p>
                <small>${error.message}</small>
            </div>
        `;
        brokenSymlinksList.style.display = 'block';
    } finally {
        scanStatus.classList.remove('active');
    }
}

// Existing utility functions
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

async function validatePath() {
    const pathInput = document.getElementById('libraryPath');
    const validation = document.getElementById('pathValidation');

    try {
        const response = await fetch('/library_management/api/libraries/verify', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ path: pathInput.value })
        });

        const result = await response.json();

        if (result.valid) {
            validation.className = 'path-validation validation-success';
            validation.textContent = 'Path is valid';
        } else {
            validation.className = 'path-validation validation-error';
            validation.textContent = 'Invalid path';
        }
    } catch (error) {
        validation.className = 'path-validation validation-error';
        validation.textContent = 'Error validating path';
    }
}

async function handleLibrarySubmit(event) {
    event.preventDefault();
    const formData = new FormData(event.target);
    const data = Object.fromEntries(formData);

    try {
        const response = await fetch('/library_management/api/libraries', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            closeModal();
            loadLibraries();
        } else {
            const error = await response.json();
            showPopup({ type: 'error', title: 'Error', message: error.message || 'Error saving library', autoClose: 5000 });
        }
    } catch (error) {
        console.error('Error saving library:', error);
        showPopup({ type: 'error', title: 'Error', message: 'Error saving library', autoClose: 5000 });
    }
}

function openModal(library = null) {
    const modal = document.getElementById('libraryModal');
    const form = document.getElementById('libraryForm');
    const title = document.getElementById('modalTitle');

    title.textContent = library ? 'Edit Library' : 'Add Library';
    form.reset();

    if (library) {
        form.name.value = library.name;
        form.path.value = library.path;
    }

    modal.style.display = 'block';
}

function closeModal() {
    const modal = document.getElementById('libraryModal');
    modal.style.display = 'none';
}

async function deleteLibrary(id) {
    showPopup({
        type: 'confirm',
        title: 'Delete Library',
        message: 'Are you sure you want to delete this library?',
        confirmText: 'Delete',
        cancelText: 'Cancel',
        onConfirm: async function() {
            try {
                const response = await fetch(`/library_management/api/libraries/${id}`, {
                    method: 'DELETE'
                });

                if (response.ok) {
                    loadLibraries();
                } else {
                    const error = await response.json();
                    showPopup({ type: 'error', title: 'Error', message: error.message || 'Error deleting library', autoClose: 5000 });
                }
            } catch (error) {
                console.error('Error deleting library:', error);
                showPopup({ type: 'error', title: 'Error', message: 'Error deleting library', autoClose: 5000 });
            }
        }
    });
}

window.editLibrary = editLibrary;
window.deleteLibrary = deleteLibrary;
window.closeModal = closeModal;
