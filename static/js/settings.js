document.addEventListener('DOMContentLoaded', function() {
    const tabButtons = document.querySelectorAll('.settings-tab-button');
    const sections = document.querySelectorAll('.settings-section-header');
    const expandAllButtons = document.querySelectorAll('.settings-expand-all');
    const collapseAllButtons = document.querySelectorAll('.settings-collapse-all');
    
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
    
    // Add New Scraper button
    const addScraperBtn = document.getElementById('add-scraper-btn');
    const addScraperPopup = document.getElementById('add-scraper-popup');
    const cancelAddScraperBtn = document.getElementById('cancel-add-scraper');
    const addScraperForm = document.getElementById('add-scraper-form');
    const scraperTypeSelect = document.getElementById('scraper-type');
    const dynamicFields = document.getElementById('dynamic-fields');

    if (addScraperBtn) {
        addScraperBtn.addEventListener('click', function(e) {
            e.preventDefault();
            addScraperPopup.style.display = 'block';
            updateDynamicFields();
        });
    }

    if (cancelAddScraperBtn) {
        cancelAddScraperBtn.addEventListener('click', function() {
            addScraperPopup.style.display = 'none';
        });
    }

    if (scraperTypeSelect) {
        scraperTypeSelect.addEventListener('change', updateDynamicFields);
    }

    if (addScraperForm) {
        addScraperForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const formData = new FormData(addScraperForm);
            fetch('/scrapers/add', {
                method: 'POST',
                body: formData
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    addScraperPopup.style.display = 'none'; // Close the popup
                    addScraperForm.reset(); // Reset the form
                    location.reload(); // Reload the page to show the new scraper
                } else {
                    alert('Failed to add scraper: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(error => {
                console.error('Error:', error);
                alert('An error occurred while adding the scraper.');
            });
        });
    }

    function updateDynamicFields() {
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
        }
    }
    
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

// Keep the updateSettings function as it was before
function updateSettings(event) {
    event.preventDefault();
    
    let formData = new FormData(event.target);
    let settings = {};
    
    for (let [key, value] of formData.entries()) {
        let keys = key.split('.');
        let current = settings;
        
        for (let i = 0; i < keys.length - 1; i++) {
            if (!(keys[i] in current)) {
                current[keys[i]] = {};
            }
            current = current[keys[i]];
        }
        
        if (value === 'true') {
            value = true;
        } else if (value === 'false') {
            value = false;
        } else if (!isNaN(value) && value !== '') {
            value = Number(value);
        }
        
        current[keys[keys.length - 1]] = value;
    }
    
    fetch('/api/settings', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(settings)
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            displaySuccess('Settings saved successfully!');
        } else {
            displayError('Error saving settings.');
        }
    })
    .catch((error) => {
        console.error('Error:', error);
        displayError('Error saving settings.');
    });
}

function displaySuccess(message) {
    const saveStatus = document.getElementById('saveStatus');
    saveStatus.textContent = message;
    saveStatus.style.color = 'green';
}

function displayError(message) {
    const saveStatus = document.getElementById('saveStatus');
    saveStatus.textContent = message;
    saveStatus.style.color = 'red';
}