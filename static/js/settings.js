document.addEventListener('DOMContentLoaded', function() {
    const tabButtons = document.querySelectorAll('.settings-tab-button');
    const sections = document.querySelectorAll('.settings-section-header');
    const expandAllButtons = document.querySelectorAll('.settings-expand-all');
    const collapseAllButtons = document.querySelectorAll('.settings-collapse-all');
    const settingsForm = document.getElementById('settingsForm');
    const saveButton = document.querySelector('.settings-submit-button');
    
    tabButtons.forEach(button => {
        button.addEventListener('click', () => {
            const tabName = button.getAttribute('data-tab');
            openTab(tabName);
        });
    });
    
    sections.forEach(section => {
        section.addEventListener('click', () => {
            toggleSection(section);
        });
    });

    expandAllButtons.forEach(button => {
        button.addEventListener('click', () => {
            const tabContent = button.closest('.settings-tab-content');
            expandAll(tabContent);
        });
    });

    collapseAllButtons.forEach(button => {
        button.addEventListener('click', () => {
            const tabContent = button.closest('.settings-tab-content');
            collapseAll(tabContent);
        });
    });

    if (settingsForm) {
        settingsForm.addEventListener('submit', handleSettingsFormSubmit);
    }

    if (saveButton) {
        saveButton.addEventListener('click', function(e) {
            e.preventDefault();
            console.log('Save button clicked');
            handleSettingsFormSubmit(e);
        });
    }
    
    // Add New Scraper functionality
    const addScraperBtn = document.getElementById('add-scraper-btn');
    const addScraperPopup = document.getElementById('add-scraper-popup');
    const cancelAddScraperBtn = document.getElementById('cancel-add-scraper');
    const addScraperForm = document.getElementById('add-scraper-form');
    const scraperTypeSelect = document.getElementById('scraper-type');

    if (addScraperBtn && addScraperPopup) {
        addScraperBtn.addEventListener('click', function(e) {
            e.preventDefault();
            addScraperPopup.style.display = 'block';
            updateDynamicFields();
        });
    }

    if (cancelAddScraperBtn && addScraperPopup) {
        cancelAddScraperBtn.addEventListener('click', function() {
            addScraperPopup.style.display = 'none';
        });
    }

    if (scraperTypeSelect) {
        scraperTypeSelect.addEventListener('change', updateDynamicFields);
    }

    if (addScraperForm) {
        addScraperForm.addEventListener('submit', handleAddScraperSubmit);
    }

    // Initialize scraper functionality
    initializeScrapersFunctionality();
});

function openTab(tabName) {
    const tabContents = document.querySelectorAll('.settings-tab-content');
    const tabButtons = document.querySelectorAll('.settings-tab-button');
    
    tabContents.forEach(content => content.style.display = 'none');
    tabButtons.forEach(button => button.classList.remove('active'));
    
    document.getElementById(tabName).style.display = 'block';
    document.querySelector(`[data-tab="${tabName}"]`).classList.add('active');
}

function toggleSection(sectionHeader) {
    const sectionContent = sectionHeader.nextElementSibling;
    const toggleIcon = sectionHeader.querySelector('.settings-toggle-icon');
    
    sectionContent.classList.toggle('active');
    toggleIcon.textContent = sectionContent.classList.contains('active') ? '-' : '+';
}

function expandAll(tabContent) {
    const sections = tabContent.querySelectorAll('.settings-section-content');
    sections.forEach(section => {
        section.classList.add('active');
        section.previousElementSibling.querySelector('.settings-toggle-icon').textContent = '-';
    });
}

function collapseAll(tabContent) {
    const sections = tabContent.querySelectorAll('.settings-section-content');
    sections.forEach(section => {
        section.classList.remove('active');
        section.previousElementSibling.querySelector('.settings-toggle-icon').textContent = '+';
    });
}

function handleSettingsFormSubmit(event) {
    event.preventDefault();
    console.log('Settings form submitted');
    const settingsForm = document.getElementById('settingsForm');
    console.log('settingsForm element:', settingsForm); // Debug log
    updateSettings(settingsForm);
}

function updateSettings(settingsDiv) {
    console.log('updateSettings called');
    
    let settings = {};

    function traverseElement(element, currentObject) {
        Array.from(element.children).forEach(child => {
            if (child.name) {
                let value = child.value;
                if (child.type === 'checkbox') {
                    value = child.checked;
                }
                
                // Convert to appropriate type
                if (value === 'true') {
                    value = true;
                } else if (value === 'false') {
                    value = false;
                } else if (!isNaN(value) && value !== '') {
                    value = Number(value);
                }

                let keys = child.name.split('.');
                let current = currentObject;
                for (let i = 0; i < keys.length - 1; i++) {
                    if (!(keys[i] in current)) {
                        current[keys[i]] = {};
                    }
                    current = current[keys[i]];
                }
                current[keys[keys.length - 1]] = value;

                console.log(`Processing form data: ${child.name} = ${value}`);
            } else if (child.children.length > 0) {
                traverseElement(child, currentObject);
            }
        });
    }

    traverseElement(settingsDiv, settings);

    console.log('Collected settings:', settings);

    fetch('/api/settings', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(settings)
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        console.log('Server response:', data);
        if (data.status === 'success') {
            displaySuccess('Settings saved successfully!');
        } else {
            displayError('Error saving settings: ' + (data.error || 'Unknown error'));
        }
    })
    .catch((error) => {
        console.error('Error:', error);
        displayError('Error saving settings: ' + error.message);
    });
}

function handleAddScraperSubmit(e) {
    e.preventDefault();
    console.log('Add Scraper form submitted');
    const formData = new FormData(e.target);
    
    fetch('/scrapers/add', {
        method: 'POST',
        body: formData
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        if (data.success) {
            const addScraperPopup = document.getElementById('add-scraper-popup');
            if (addScraperPopup) {
                addScraperPopup.style.display = 'none';
            }
            e.target.reset();
            location.reload();
        } else {
            throw new Error(data.error || 'Unknown error');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        alert('An error occurred while adding the scraper: ' + error.message);
    });
}

function updateDynamicFields() {
    const scraperTypeSelect = document.getElementById('scraper-type');
    const dynamicFields = document.getElementById('dynamic-fields');
    if (!scraperTypeSelect || !dynamicFields) return;

    const selectedType = scraperTypeSelect.value;
    dynamicFields.innerHTML = '';
    if (window.scraperSettings && window.scraperSettings[selectedType]) {
        window.scraperSettings[selectedType].forEach(setting => {
            if (setting !== 'enabled') {
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
    } else {
        console.error('Scraper settings not found for type:', selectedType);
    }
}

function initializeScrapersFunctionality() {
    console.log("Initializing scraper functionality");
    
    // Existing delete scraper functionality
    document.querySelectorAll('.delete-scraper-btn').forEach(button => {
        button.addEventListener('click', function() {
            const scraperId = this.getAttribute('data-scraper-id');
            if (confirm(`Are you sure you want to delete the scraper "${scraperId}"?`)) {
                fetch('/scrapers/delete', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ scraper_id: scraperId }),
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        location.reload();
                    } else {
                        alert('Failed to delete scraper: ' + (data.error || 'Unknown error'));
                    }
                })
                .catch(error => {
                    console.error('Error:', error);
                    alert('An error occurred while deleting the scraper.');
                });
            }
        });
    });
}

function displaySuccess(message) {
    console.log('Success:', message);
    const saveStatus = document.getElementById('saveStatus');
    if (saveStatus) {
        saveStatus.textContent = message;
        saveStatus.style.color = 'green';
    } else {
        console.error('saveStatus element not found');
    }
}

function displayError(message) {
    console.error('Error:', message);
    const saveStatus = document.getElementById('saveStatus');
    if (saveStatus) {
        saveStatus.textContent = message;
        saveStatus.style.color = 'red';
    } else {
        console.error('saveStatus element not found');
    }
}