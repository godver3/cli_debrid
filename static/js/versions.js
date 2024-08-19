document.addEventListener('DOMContentLoaded', function() {
    initializeScrapingFunctionality();
});

function initializeScrapingFunctionality() {
    const addVersionBtn = document.getElementById('add-version-btn');
    const addVersionPopup = document.getElementById('add-version-popup');
    const cancelAddVersionBtn = document.getElementById('cancel-add-version');
    const addVersionForm = document.getElementById('add-version-form');

    if (addVersionBtn && addVersionPopup) {
        addVersionBtn.addEventListener('click', function(e) {
            e.preventDefault();
            addVersionPopup.style.display = 'block';
        });
    }

    if (cancelAddVersionBtn && addVersionPopup) {
        cancelAddVersionBtn.addEventListener('click', function() {
            addVersionPopup.style.display = 'none';
        });
    }

    if (addVersionForm) {
        addVersionForm.addEventListener('submit', handleAddVersionSubmit);
    }

    document.querySelectorAll('.delete-version-btn').forEach(button => {
        button.addEventListener('click', function() {
            const versionId = this.getAttribute('data-version-id');
            if (confirm(`Are you sure you want to delete the version "${versionId}"?`)) {
                deleteVersion(versionId);
            }
        });
    });

    initializeScrapingExpandCollapse();
}

function initializeScrapingExpandCollapse() {
    const scrapingTab = document.getElementById('scraping');
    if (scrapingTab) {
        const sectionHeaders = scrapingTab.querySelectorAll('.settings-section-header');
        sectionHeaders.forEach(header => {
            header.addEventListener('click', function(event) {
                event.stopPropagation();
                toggleSection(this);
            });
        });
    }
}

function toggleSection(sectionHeader) {
    const content = sectionHeader.nextElementSibling;
    const toggleIcon = sectionHeader.querySelector('.settings-toggle-icon');
    if (content.style.display === 'none' || content.style.display === '') {
        content.style.display = 'block';
        toggleIcon.textContent = '-';
    } else {
        content.style.display = 'none';
        toggleIcon.textContent = '+';
    }
}

function handleAddVersionSubmit(e) {
    e.preventDefault();
    const formData = new FormData(e.target);
    
    fetch('/versions/add', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            document.getElementById('add-version-popup').style.display = 'none';
            e.target.reset();
            updateScrapingTab();
        } else {
            alert('An error occurred while adding the version: ' + (data.error || 'Unknown error'));
        }
    })
    .catch(error => {
        console.error('Error:', error);
        alert('An error occurred while adding the version.');
    });
}

function deleteVersion(versionId) {
    fetch('/versions/delete', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ version_id: versionId }),
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            updateScrapingTab();
        } else {
            alert('Failed to delete version: ' + (data.error || 'Unknown error'));
        }
    })
    .catch(error => {
        console.error('Error:', error);
        alert('An error occurred while deleting the version.');
    });
}

function updateScrapingTab() {
    fetch('/scraping/content')
        .then(response => response.text())
        .then(html => {
            const scrapingTab = document.getElementById('scraping');
            if (scrapingTab) {
                scrapingTab.innerHTML = html;
                initializeScrapingFunctionality();
            }
        })
        .catch(error => {
            console.error('Error updating Scraping tab:', error);
        });
}