document.addEventListener('DOMContentLoaded', function() {
    initializeContentSourcesFunctionality();
});

function initializeContentSourcesFunctionality() {
    console.log('Initializing Content Sources Functionality');

    const addSourceBtn = document.getElementById('add-source-btn');
    const addSourcePopup = document.getElementById('add-source-popup');
    const cancelAddSourceBtn = document.getElementById('cancel-add-source');
    const addSourceForm = document.getElementById('add-source-form');
    const sourceTypeSelect = document.getElementById('source-type');

    if (addSourceBtn && addSourcePopup) {
        addSourceBtn.addEventListener('click', function(e) {
            e.preventDefault();
            addSourcePopup.style.display = 'block';
            updateDynamicFields();
        });
    }

    if (cancelAddSourceBtn && addSourcePopup) {
        cancelAddSourceBtn.addEventListener('click', function() {
            addSourcePopup.style.display = 'none';
        });
    }

    if (addSourceForm) {
        addSourceForm.addEventListener('submit', handleAddSourceSubmit);
    }

    if (sourceTypeSelect) {
        sourceTypeSelect.addEventListener('change', updateDynamicFields);
    }

    document.querySelectorAll('.delete-source-btn').forEach(button => {
        button.addEventListener('click', function() {
            const sourceId = this.getAttribute('data-source-id');
            if (confirm(`Are you sure you want to delete the source "${sourceId}"?`)) {
                deleteContentSource(sourceId);
            }
        });
    });

    console.log('Number of delete buttons:', document.querySelectorAll('.delete-source-btn').length);
}

function initializeContentSourcesExpandCollapse() {
    const contentSourcesTab = document.getElementById('content-sources');
    if (contentSourcesTab) {
        const sectionHeaders = contentSourcesTab.querySelectorAll('.settings-section-header');
        sectionHeaders.forEach(header => {
            header.addEventListener('click', function(event) {
                if (!event.target.classList.contains('delete-source-btn')) {
                    event.stopPropagation();
                    toggleSection(this);
                }
            });
        });
        const collapseAllBtn = contentSourcesTab.querySelector('.settings-collapse-all');
        const expandAllBtn = contentSourcesTab.querySelector('.settings-expand-all');
        if (collapseAllBtn) collapseAllBtn.addEventListener('click', collapseAllSections);
        if (expandAllBtn) expandAllBtn.addEventListener('click', expandAllSections);
    }
}

function toggleSection(sectionHeader) {
    console.log('Toggling section:', sectionHeader);

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

function expandAllSections() {
    toggleAllSections('block', '-');
}

function collapseAllSections() {
    toggleAllSections('none', '+');
}

function toggleAllSections(displayStyle, iconText) {
    const contentSourcesTab = document.getElementById('content-sources');
    const sections = contentSourcesTab.querySelectorAll('.settings-section');
    sections.forEach(section => {
        const header = section.querySelector('.settings-section-header');
        const content = section.querySelector('.settings-section-content');
        const toggleIcon = header.querySelector('.settings-toggle-icon');
        
        content.style.display = displayStyle;
        toggleIcon.textContent = iconText;
    });
}

function handleAddSourceSubmit(e) {
    e.preventDefault();
    const formData = new FormData(e.target);
    
    // Add display_name field if it doesn't exist
    if (!formData.has('display_name')) {
        const sourceType = formData.get('type');
        const displayName = `${sourceType} ${Date.now()}`;
        formData.append('display_name', displayName);
    }

    fetch('/content_sources/add', {
        method: 'POST',
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            document.getElementById('add-source-popup').style.display = 'none';
            e.target.reset();
            updateContentSourcesTab();
        } else {
            alert('An error occurred while adding the content source: ' + (data.error || 'Unknown error'));
        }
    })
    .catch(error => {
        console.error('Error:', error);
        alert('An error occurred while adding the content source.');
    });
}

function deleteContentSource(sourceId) {
    fetch('/content_sources/delete', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ source_id: sourceId }),
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Remove the deleted source from the DOM
            const sourceElement = document.querySelector(`.settings-section[data-source-id="${sourceId}"]`);
            if (sourceElement) {
                sourceElement.remove();
            }
            showNotification('Content source deleted successfully', 'success');
        } else {
            showNotification('Failed to delete content source: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('An error occurred while deleting the content source.', 'error');
    });
}

function showNotification(message, type) {
    const notification = document.createElement('div');
    notification.textContent = message;
    notification.className = `notification ${type}`;
    document.body.appendChild(notification);
    setTimeout(() => {
        notification.remove();
    }, 3000);
}

function updateContentSourcesTab() {
    fetch('/content_sources/content')
        .then(response => response.text())
        .then(html => {
            const contentSourcesTab = document.getElementById('content-sources');
            if (contentSourcesTab) {
                contentSourcesTab.innerHTML = html;
                initializeContentSourcesFunctionality();
                initializeContentSourcesExpandCollapse();
            }
        })
        .catch(error => {
            console.error('Error updating Content Sources tab:', error);
        });
}

function updateDynamicFields() {
    const sourceTypeSelect = document.getElementById('source-type');
    const dynamicFields = document.getElementById('dynamic-fields');
    if (!sourceTypeSelect || !dynamicFields) return;

    const selectedType = sourceTypeSelect.value;
    dynamicFields.innerHTML = '';
    if (window.contentSourceSettings && window.contentSourceSettings[selectedType]) {
        window.contentSourceSettings[selectedType].forEach(setting => {
            if (setting !== 'enabled' && setting !== 'versions') {
                const div = document.createElement('div');
                div.className = 'form-group';
                
                const label = document.createElement('label');
                label.htmlFor = setting;
                label.textContent = setting.charAt(0).toUpperCase() + setting.slice(1) + ':';
                
                const input = document.createElement('input');
                input.type = 'text';
                input.id = setting;
                input.name = setting;
                
                div.appendChild(label);
                div.appendChild(input);
                dynamicFields.appendChild(div);
            }
        });

        // Add enabled checkbox
        const enabledDiv = document.createElement('div');
        enabledDiv.className = 'form-group';
        
        const enabledLabel = document.createElement('label');
        enabledLabel.htmlFor = 'enabled';
        enabledLabel.textContent = 'Enabled:';
        
        const enabledInput = document.createElement('input');
        enabledInput.type = 'checkbox';
        enabledInput.id = 'enabled';
        enabledInput.name = 'enabled';
        
        enabledDiv.appendChild(enabledLabel);
        enabledDiv.appendChild(enabledInput);
        dynamicFields.appendChild(enabledDiv);
    }
}