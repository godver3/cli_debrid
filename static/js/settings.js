document.addEventListener('DOMContentLoaded', function() {
    const saveSettingsButton = document.getElementById('saveSettingsButton');
    if (saveSettingsButton) {
        saveSettingsButton.addEventListener('click', handleSettingsFormSubmit);
    }
    
    initializeAllFunctionalities();
    
    const lastActiveTab = localStorage.getItem('currentTab') || 'required';
    openTab(lastActiveTab);
});

function initializeAllFunctionalities() {
    initializeTabSwitching();
    initializeExpandCollapse();
    initializeContentSourcesFunctionality();
    initializeScrapersFunctionality();
    initializeScrapingFunctionality();
}

function initializeTabSwitching() {
    const tabButtons = document.querySelectorAll('.settings-tab-button');
    tabButtons.forEach(button => {
        button.addEventListener('click', function() {
            const tabName = this.getAttribute('data-tab');
            openTab(tabName);
        });
    });
}

function openTab(tabName) {
    const tabContents = document.querySelectorAll('.settings-tab-content');
    const tabButtons = document.querySelectorAll('.settings-tab-button');
    
    tabContents.forEach(content => content.style.display = 'none');
    tabButtons.forEach(button => button.classList.remove('active'));
    
    const activeTab = document.getElementById(tabName);
    activeTab.style.display = 'block';
    document.querySelector(`[data-tab="${tabName}"]`).classList.add('active');

    localStorage.setItem('currentTab', tabName);
}

function initializeExpandCollapseForSection(section) {
    const header = section.querySelector('.settings-section-header');
    const content = section.querySelector('.settings-section-content');
    const toggleIcon = header.querySelector('.settings-toggle-icon');

    header.addEventListener('click', function(event) {
        if (!event.target.classList.contains('delete-source-btn') &&
            !event.target.classList.contains('delete-scraper-btn') &&
            !event.target.classList.contains('delete-version-btn')) {
            event.stopPropagation();
            if (content.style.display === 'none' || content.style.display === '') {
                content.style.display = 'block';
                toggleIcon.textContent = '-';
            } else {
                content.style.display = 'none';
                toggleIcon.textContent = '+';
            }
        }
    });
}

function initializeExpandCollapse() {
    const allTabContents = document.querySelectorAll('.settings-tab-content');
    
    allTabContents.forEach(tabContent => {
        const expandAllButton = tabContent.querySelector('.settings-expand-all');
        const collapseAllButton = tabContent.querySelector('.settings-collapse-all');
        const sections = tabContent.querySelectorAll('.settings-section');

        sections.forEach(section => initializeExpandCollapseForSection(section));

        if (expandAllButton) {
            expandAllButton.addEventListener('click', () => expandAll(tabContent));
        }

        if (collapseAllButton) {
            collapseAllButton.addEventListener('click', () => collapseAll(tabContent));
        }
    });
}

function toggleSection(sectionHeader) {
    const sectionContent = sectionHeader.nextElementSibling;
    const toggleIcon = sectionHeader.querySelector('.settings-toggle-icon');
    
    if (sectionContent.style.display === 'none' || sectionContent.style.display === '') {
        sectionContent.style.display = 'block';
        toggleIcon.textContent = '-';
    } else {
        sectionContent.style.display = 'none';
        toggleIcon.textContent = '+';
    }
}

function expandAll(tabContent) {
    const sections = tabContent.querySelectorAll('.settings-section-content');
    const toggleIcons = tabContent.querySelectorAll('.settings-toggle-icon');
    sections.forEach(section => section.style.display = 'block');
    toggleIcons.forEach(icon => icon.textContent = '-');
}

function collapseAll(tabContent) {
    const sections = tabContent.querySelectorAll('.settings-section-content');
    const toggleIcons = tabContent.querySelectorAll('.settings-toggle-icon');
    sections.forEach(section => section.style.display = 'none');
    toggleIcons.forEach(icon => icon.textContent = '+');
}

function handleSettingsFormSubmit(event) {
    event.preventDefault();
    updateSettings();
}

function updateSettings() {
    const settingsData = {};
    const inputs = document.querySelectorAll('#settingsForm input, #settingsForm select, #settingsForm textarea');
    
    inputs.forEach(input => {
        const name = input.name;
        let value = input.value;
        
        if (input.type === 'checkbox') {
            value = input.checked;
        } else if (value.toLowerCase() === 'true') {
            value = true;
        } else if (value.toLowerCase() === 'false') {
            value = false;
        }

        const nameParts = name.split('.');
        let current = settingsData;
        
        for (let i = 0; i < nameParts.length - 1; i++) {
            if (!current[nameParts[i]]) {
                current[nameParts[i]] = {};
            }
            current = current[nameParts[i]];
        }
        
        current[nameParts[nameParts.length - 1]] = value;
    });

    // Ensure 'Content Sources' and 'Scrapers' sections exist
    if (!settingsData['Content Sources']) {
        settingsData['Content Sources'] = {};
    }
    if (!settingsData['Scrapers']) {
        settingsData['Scrapers'] = {};
    }

    // Correctly structure MDBList content source
    if (settingsData['Content Sources']['MDBList_1']) {
        const mdbList = settingsData['Content Sources']['MDBList_1'];
        if (typeof mdbList === 'object') {
            mdbList.enabled = mdbList.enabled || false;
            mdbList.versions = mdbList.versions || '';
            mdbList.display_name = mdbList.display_name || '';
            mdbList.urls = mdbList.urls || '';
        }
    }

    // Remove any 'Unknown' content sources
    Object.keys(settingsData['Content Sources']).forEach(key => {
        if (key.startsWith('Unknown_')) {
            delete settingsData['Content Sources'][key];
        }
    });

    // Preserve existing scrapers
    const existingScrapers = document.querySelectorAll('[data-scraper-id]');
    console.log(`Found ${existingScrapers.length} existing scrapers`);

    existingScrapers.forEach(scraper => {
        try {
            const scraperId = scraper.getAttribute('data-scraper-id');
            console.log(`Processing scraper: ${scraperId}`);
            
            const scraperData = {
                type: scraperId.split('_')[0] // Extract type from the scraper ID
            };

            // Collect all input fields for this scraper
            scraper.querySelectorAll('input, select, textarea').forEach(input => {
                const fieldName = input.name.split('.').pop();
                if (input.type === 'checkbox') {
                    scraperData[fieldName] = input.checked;
                } else {
                    scraperData[fieldName] = input.value;
                }
            });

            console.log(`Collected data for scraper ${scraperId}:`, scraperData);
            settingsData['Scrapers'][scraperId] = scraperData;
        } catch (error) {
            console.error(`Error processing scraper:`, error);
        }
    });

    // Remove any top-level fields that should be nested
    const topLevelFields = ['Plex', 'Overseerr', 'RealDebrid', 'Torrentio', 'Scraping', 'Queue', 'Trakt', 'Debug', 'Content Sources', 'Scrapers'];
    Object.keys(settingsData).forEach(key => {
        if (!topLevelFields.includes(key)) {
            delete settingsData[key];
        }
    });

    console.log("Final settings data to be sent:", JSON.stringify(settingsData, null, 2));

    fetch('/api/settings', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(settingsData)
    })
    .then(response => response.json())
    .then(data => {
        console.log("Server response:", data);
        if (data.status === 'success') {
            showNotification('Settings saved successfully', 'success');
        } else {
            showNotification('Error saving settings', 'error');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error saving settings', 'error');
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
            updateDynamicFields('source');
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
        sourceTypeSelect.addEventListener('change', () => updateDynamicFields('source'));
    }

    document.querySelectorAll('.delete-source-btn').forEach(button => {
        button.addEventListener('click', function() {
            const sourceId = this.getAttribute('data-source-id');
            if (confirm(`Are you sure you want to delete the source "${sourceId}"?`)) {
                deleteContentSource(sourceId);
            }
        });
    });
}

function initializeScrapersFunctionality() {
    const addScraperBtn = document.getElementById('add-scraper-btn');
    const addScraperPopup = document.getElementById('add-scraper-popup');
    const cancelAddScraperBtn = document.getElementById('cancel-add-scraper');
    const addScraperForm = document.getElementById('add-scraper-form');
    const scraperTypeSelect = document.getElementById('scraper-type');

    if (addScraperBtn && addScraperPopup) {
        addScraperBtn.addEventListener('click', function(e) {
            e.preventDefault();
            addScraperPopup.style.display = 'block';
            updateDynamicFields('scraper');
        });
    }

    if (cancelAddScraperBtn && addScraperPopup) {
        cancelAddScraperBtn.addEventListener('click', function() {
            addScraperPopup.style.display = 'none';
        });
    }

    if (addScraperForm) {
        addScraperForm.addEventListener('submit', handleAddScraperSubmit);
    }

    if (scraperTypeSelect) {
        scraperTypeSelect.addEventListener('change', () => updateDynamicFields('scraper'));
    }

    document.querySelectorAll('.delete-scraper-btn').forEach(button => {
        button.addEventListener('click', function() {
            const scraperId = this.getAttribute('data-scraper-id');
            if (confirm(`Are you sure you want to delete the scraper "${scraperId}"?`)) {
                deleteScraper(scraperId);
            }
        });
    });
}

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
}

function updateDynamicFields(type) {
    const typeSelect = document.getElementById(`${type}-type`);
    const dynamicFields = document.getElementById('dynamic-fields');
    if (!typeSelect || !dynamicFields) return;

    const selectedType = typeSelect.value;
    dynamicFields.innerHTML = '';

    const settings = type === 'source' ? window.contentSourceSettings : window.scraperSettings;

    if (settings && settings[selectedType]) {
        Object.entries(settings[selectedType]).forEach(([setting, config]) => {
            if (setting !== 'enabled') {
                const div = document.createElement('div');
                div.className = 'form-group';
                
                const label = document.createElement('label');
                label.htmlFor = setting;
                label.textContent = setting.charAt(0).toUpperCase() + setting.slice(1) + ':';
                
                const input = document.createElement('input');
                input.type = config.type === 'boolean' ? 'checkbox' : 'text';
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

function handleAddSourceSubmit(e) {
    e.preventDefault();
    const form = e.target;
    const formData = new FormData(form);
    const jsonData = {};
    
    formData.forEach((value, key) => {
        if (value === 'true') value = true;
        if (value === 'false') value = false;
        if (form.elements[key].type === 'checkbox') value = form.elements[key].checked;
        jsonData[key] = value;
    });

    // Ensure the correct structure for the new content source
    const sourceData = {
        type: jsonData.type,
        enabled: jsonData.enabled || false,
        versions: jsonData.versions || "",
        display_name: jsonData.display_name || "",
        urls: jsonData.urls || ""
    };

    // Add any additional fields that might be specific to certain source types
    if (jsonData.url) sourceData.url = jsonData.url;
    if (jsonData.api_key) sourceData.api_key = jsonData.api_key;

    fetch('/content_sources/add', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(sourceData)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            document.getElementById('add-source-popup').style.display = 'none';
            form.reset();
            updateContentSourcesTab().then(() => {
                const newSection = document.querySelector(`.settings-section[data-source-id="${data.source_id}"]`);
                if (newSection) {
                    initializeExpandCollapseForSection(newSection);
                }
            });
            showNotification('Content source added successfully', 'success');
        } else {
            showNotification('Error adding content source: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error adding content source', 'error');
    });
}

function handleAddScraperSubmit(e) {
    e.preventDefault();
    const form = e.target;
    const formData = new FormData(form);
    const jsonData = {};
    
    formData.forEach((value, key) => {
        if (value === 'true') value = true;
        if (value === 'false') value = false;
        if (form.elements[key].type === 'checkbox') value = form.elements[key].checked;
        jsonData[key] = value;
    });

    // Ensure the scraper type is included
    if (!jsonData.type) {
        console.error('No scraper type provided');
        showNotification('Error: No scraper type provided', 'error');
        return;
    }

    fetch('/scrapers/add', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(jsonData)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            document.getElementById('add-scraper-popup').style.display = 'none';
            form.reset();
            updateScrapersTab().then(() => {
                const newSection = document.querySelector(`.settings-section[data-scraper-id="${data.scraper_id}"]`);
                if (newSection) {
                    initializeExpandCollapseForSection(newSection);
                }
            });
            showNotification('Scraper added successfully', 'success');
        } else {
            showNotification('Error adding scraper: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error adding scraper', 'error');
    });
}

function handleAddVersionSubmit(e) {
    e.preventDefault();
    const form = e.target;
    const formData = new FormData(form);
    const jsonData = {};
    
    formData.forEach((value, key) => {
        if (value === 'true') value = true;
        if (value === 'false') value = false;
        if (form.elements[key].type === 'checkbox') value = form.elements[key].checked;
        jsonData[key] = value;
    });

    fetch('/versions/add', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(jsonData)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            document.getElementById('add-version-popup').style.display = 'none';
            form.reset();
            updateScrapingTab().then(() => {
                const newSection = document.querySelector(`.settings-section[data-version-id="${data.version_id}"]`);
                if (newSection) {
                    initializeExpandCollapseForSection(newSection);
                }
            });
            showNotification('Version added successfully', 'success');
        } else {
            showNotification('Error adding version: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error adding version', 'error');
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
        showNotification('Error deleting content source', 'error');
    });
}

function deleteScraper(scraperId) {
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
            const scraperElement = document.querySelector(`.settings-section[data-scraper-id="${scraperId}"]`);
            if (scraperElement) {
                scraperElement.remove();
            }
            showNotification('Scraper deleted successfully', 'success');
        } else {
            showNotification('Failed to delete scraper: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error deleting scraper', 'error');
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
            const versionElement = document.querySelector(`.settings-section[data-version-id="${versionId}"]`);
            if (versionElement) {
                versionElement.remove();
            }
            showNotification('Version deleted successfully', 'success');
        } else {
            showNotification('Failed to delete version: ' + (data.error || 'Unknown error'), 'error');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error deleting version', 'error');
    });
}

function updateContentSourcesTab() {
    return fetch('/content_sources/content')
        .then(response => response.text())
        .then(html => {
            const contentSourcesTab = document.getElementById('content-sources');
            if (contentSourcesTab) {
                contentSourcesTab.innerHTML = html;
                initializeContentSourcesFunctionality();
                initializeExpandCollapse();
            }
        })
        .catch(error => {
            console.error('Error updating Content Sources tab:', error);
            showNotification('Error updating Content Sources tab', 'error');
        });
}

function updateScrapersTab() {
    return fetch('/scrapers/content')
        .then(response => response.text())
        .then(html => {
            const scrapersTab = document.getElementById('scrapers');
            if (scrapersTab) {
                scrapersTab.innerHTML = html;
                initializeScrapersFunctionality();
                initializeExpandCollapse();
            }
        })
        .catch(error => {
            console.error('Error updating Scrapers tab:', error);
            showNotification('Error updating Scrapers tab', 'error');
        });
}

function updateScrapingTab() {
    return fetch('/scraping/content')
        .then(response => response.text())
        .then(html => {
            const scrapingTab = document.getElementById('scraping');
            if (scrapingTab) {
                scrapingTab.innerHTML = html;
                initializeScrapingFunctionality();
                initializeExpandCollapse();
            }
        })
        .catch(error => {
            console.error('Error updating Scraping tab:', error);
            showNotification('Error updating Scraping tab', 'error');
        });
}

// Utility function to create form fields dynamically
function createFormField(setting, value, type) {
    const div = document.createElement('div');
    div.className = 'form-group';
    
    const label = document.createElement('label');
    label.htmlFor = setting;
    label.textContent = setting.charAt(0).toUpperCase() + setting.slice(1) + ':';
    
    let input;
    if (type === 'boolean') {
        input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = value;
    } else if (Array.isArray(value)) {
        input = document.createElement('select');
        input.multiple = true;
        value.forEach(option => {
            const optionElement = document.createElement('option');
            optionElement.value = option;
            optionElement.textContent = option;
            optionElement.selected = true;
            input.appendChild(optionElement);
        });
    } else {
        input = document.createElement('input');
        input.type = 'text';
        input.value = value;
    }
    
    input.id = setting;
    input.name = setting;
    input.className = 'settings-input';
    
    div.appendChild(label);
    div.appendChild(input);
    return div;
}
