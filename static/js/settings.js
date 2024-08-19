document.addEventListener('DOMContentLoaded', function() {
    const saveSettingsButton = document.getElementById('saveSettingsButton');
    if (saveSettingsButton) {
        saveSettingsButton.addEventListener('click', handleSettingsFormSubmit);
    }

    window.contentSourceSettings = window.contentSourceSettings || {};
    window.scraperSettings = window.scraperSettings || {};
    window.scrapingVersions = window.scrapingVersions || {};
    
    initializeAllFunctionalities();
    
    const lastActiveTab = localStorage.getItem('currentTab') || 'required';
    openTab(lastActiveTab);

    debugDOMStructure();
});

function debugDOMStructure() {
    console.log('Debugging DOM structure:');
    const settingsForm = document.getElementById('settingsForm');
    const scrapersTab = document.getElementById('scrapers');
    
    if (settingsForm) {
        console.log('Settings form found');
        const tabContents = settingsForm.querySelectorAll('.settings-tab-content');
        console.log(`Found ${tabContents.length} tab contents inside settingsForm`);
        tabContents.forEach((tab, index) => {
            console.log(`Tab ${index + 1}: id="${tab.id}", class="${tab.className}"`);
        });
    } else {
        console.log('Settings form not found');
    }

    if (scrapersTab) {
        console.log('Scrapers tab found');
        const scraperSections = scrapersTab.querySelectorAll('.settings-section');
        console.log(`Found ${scraperSections.length} scraper sections`);
    } else {
        console.log('Scrapers tab not found');
    }
}

function initializeAllFunctionalities() {
    initializeTabSwitching();
    initializeExpandCollapse();
    initializeContentSourcesFunctionality();
    initializeScrapersFunctionality();
    initializeScrapingFunctionality();
    initializeTraktAuthorization();
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

function reinitializeExpandCollapse() {
    const allSections = document.querySelectorAll('.settings-section');
    allSections.forEach(section => {
        const header = section.querySelector('.settings-section-header');
        const content = section.querySelector('.settings-section-content');
        const toggleIcon = header.querySelector('.settings-toggle-icon');

        if (header && content && toggleIcon) {
            // Remove existing event listeners
            header.removeEventListener('click', toggleSection);
            
            // Add new event listener
            header.addEventListener('click', toggleSection);
        }
    });

    console.log(`Reinitialized expand/collapse for ${allSections.length} sections`);
}

function toggleSection(event) {
    if (!event.target.classList.contains('delete-scraper-btn')) {
        event.stopPropagation();
        const content = this.nextElementSibling;
        const toggleIcon = this.querySelector('.settings-toggle-icon');
        if (content.style.display === 'none' || content.style.display === '') {
            content.style.display = 'block';
            toggleIcon.textContent = '-';
        } else {
            content.style.display = 'none';
            toggleIcon.textContent = '+';
        }
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

const debouncedUpdateSettings = debounce(updateSettings, 300);

function handleSettingsFormSubmit(event) {
    event.preventDefault();
    try {
        updateSettings()
            .then(() => {
                console.log("Settings updated successfully");
            })
            .catch(error => {
                console.error("Error updating settings:", error);
            });
    } catch (error) {
        console.error("Error in handleSettingsFormSubmit:", error);
    }
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

    // Ensure 'Content Sources' section exists
    if (!settingsData['Content Sources']) {
        settingsData['Content Sources'] = {};
    }

    // Safely process Content Sources
    if (settingsData['Content Sources'] && typeof settingsData['Content Sources'] === 'object') {
        Object.keys(settingsData['Content Sources']).forEach(sourceId => {
            const sourceData = settingsData['Content Sources'][sourceId];
            if (sourceData && typeof sourceData === 'object') {
                // Handle versions as a list
                if (sourceData.versions) {
                    if (typeof sourceData.versions === 'string') {
                        sourceData.versions = sourceData.versions.split(',').map(v => v.trim()).filter(v => v);
                    } else if (typeof sourceData.versions === 'boolean') {
                        // If it's a boolean, we need to determine which versions to include
                        // This assumes you have a list of available versions somewhere
                        const availableVersions = ['1080p', '2160p']; // Adjust this list as needed
                        sourceData.versions = sourceData.versions ? availableVersions : [];
                    }
                } else {
                    sourceData.versions = [];
                }
            }
        });
    }

    // Remove any 'Unknown' content sources
    if (settingsData['Content Sources'] && typeof settingsData['Content Sources'] === 'object') {
        Object.keys(settingsData['Content Sources']).forEach(key => {
            if (key.startsWith('Unknown_')) {
                delete settingsData['Content Sources'][key];
            }
        });
    }

    // Debug: Log all tabs
    const allTabs = document.querySelectorAll('.tab-content > div');
    console.log(`Found ${allTabs.length} tabs:`);
    allTabs.forEach((tab, index) => {
        console.log(`Tab ${index + 1} id: ${tab.id}`);
    });

    // Process scraper sections
    const scrapersTab = document.getElementById('scrapers');
    console.log(`Scrapers tab found: ${scrapersTab !== null}`);

    if (scrapersTab) {
        const scraperSections = scrapersTab.querySelectorAll('.settings-section');
        console.log(`Found ${scraperSections.length} scraper sections`);

        if (!settingsData['Scrapers']) {
            settingsData['Scrapers'] = {};
        }

        scraperSections.forEach(section => {
            const header = section.querySelector('.settings-section-header h4');
            if (!header) {
                console.log('Skipping section: No header found');
                return;
            }

            const scraperId = header.textContent.trim();
            console.log(`Processing scraper: ${scraperId}`);
            
            const scraperData = {
                type: scraperId.split('_')[0] // Extract type from the scraper ID
            };

            // Collect all input fields for this scraper
            section.querySelectorAll('input, select, textarea').forEach(input => {
                const fieldName = input.name.split('.').pop();
                if (input.type === 'checkbox') {
                    scraperData[fieldName] = input.checked;
                } else {
                    scraperData[fieldName] = input.value;
                }
            });

            console.log(`Collected data for scraper ${scraperId}:`, scraperData);
            settingsData['Scrapers'][scraperId] = scraperData;
        });
    } else {
        console.error('Scrapers tab not found!');
    }

    // Remove any scrapers that are not actual scrapers
    const validScraperTypes = ['Zilean', 'Comet', 'Jackett', 'Torrentio'];
    if (settingsData['Scrapers'] && typeof settingsData['Scrapers'] === 'object') {
        Object.keys(settingsData['Scrapers']).forEach(key => {
            if (settingsData['Scrapers'][key] && !validScraperTypes.includes(settingsData['Scrapers'][key].type)) {
                delete settingsData['Scrapers'][key];
            }
        });
    }

    // Remove any top-level fields that should be nested
    const topLevelFields = ['Plex', 'Overseerr', 'RealDebrid', 'Torrentio', 'Scraping', 'Queue', 'Trakt', 'Debug', 'Content Sources', 'Scrapers'];
    Object.keys(settingsData).forEach(key => {
        if (!topLevelFields.includes(key)) {
            delete settingsData[key];
        }
    });

    console.log("Final settings data to be sent:", JSON.stringify(settingsData, null, 2));

    return fetch('/api/settings', {
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
            // Update tabs sequentially
            return updateContentSourcesTab()
                .then(() => updateScrapersTab())
                .then(() => updateScrapingTab())
                .then(() => {
                    reinitializeExpandCollapse();
                    console.log("Expand/collapse functionality reinitialized");
                });
        } else {
            throw new Error('Error saving settings');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error saving settings', 'error');
        throw error;
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

    if (!settings) {
        console.error(`Settings for ${type} are not defined`);
        return;
    }

    if (!settings[selectedType]) {
        console.error(`Settings for ${selectedType} are not defined`);
        return;
    }

    if (settings && settings[selectedType]) {
        Object.entries(settings[selectedType]).forEach(([setting, config]) => {
            if (setting !== 'enabled' && setting !== 'versions') {
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

        // Add version checkboxes
        if (type === 'source' && window.scrapingVersions) {
            const versionsDiv = document.createElement('div');
            versionsDiv.className = 'form-group';
            const versionsLabel = document.createElement('label');
            versionsLabel.textContent = 'Versions:';
            versionsDiv.appendChild(versionsLabel);

            const versionCheckboxes = document.createElement('div');
            versionCheckboxes.className = 'version-checkboxes';

            Object.keys(window.scrapingVersions).forEach(version => {
                const label = document.createElement('label');
                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox';
                checkbox.name = 'versions';
                checkbox.value = version;
                label.appendChild(checkbox);
                label.appendChild(document.createTextNode(` ${version}`));
                versionCheckboxes.appendChild(label);
            });

            versionsDiv.appendChild(versionCheckboxes);
            dynamicFields.appendChild(versionsDiv);
        }
    }
}

function handleAddSourceSubmit(e) {
    e.preventDefault();
    const form = e.target;
    const formData = new FormData(form);
    const sourceData = {};
    
    formData.forEach((value, key) => {
        if (key === 'versions') {
            if (!sourceData.versions) {
                sourceData.versions = [];
            }
            if (form.elements[key].checked) {
                sourceData.versions.push(value);
            }
        } else if (form.elements[key].type === 'checkbox') {
            sourceData[key] = form.elements[key].checked;
        } else {
            sourceData[key] = value === 'true' ? true : (value === 'false' ? false : value);
        }
    });

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
        body: JSON.stringify({ source_id: sourceId })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            console.log(`Content source ${sourceId} deleted successfully`);
            showNotification('Content source deleted successfully', 'success');
            
            // Update the Content Sources tab content
            return updateContentSourcesTab();
        } else {
            throw new Error(data.error || 'Unknown error');
        }
    })
    .then(() => {
        console.log('Content Sources tab updated successfully');
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error deleting content source: ' + error.message, 'error');
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
            // Remove the scraper element from the UI
            const scraperElement = document.querySelector(`.settings-section[data-scraper-id="${scraperId}"]`);
            if (scraperElement) {
                scraperElement.remove();
            }
            showNotification('Scraper deleted successfully', 'success');
            
            // Update the Scrapers tab content
            return updateScrapersTab();
        } else {
            throw new Error(data.error || 'Unknown error');
        }
    })
    .then(() => {
        console.log('Scrapers tab updated successfully');
    })
    .catch(error => {
        console.error('Error:', error);
        showNotification('Error deleting scraper: ' + error.message, 'error');
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
        .then(response => {
            if (!response.ok) {
                throw new Error('Network response was not ok');
            }
            return response.text();
        })
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

function initializeTraktAuthorization() {
    const traktAuthBtn = document.getElementById('trakt-auth-btn');
    const traktAuthStatus = document.getElementById('trakt-auth-status');
    const traktAuthCode = document.getElementById('trakt-auth-code');
    const traktCode = document.getElementById('trakt-code');
    const traktActivateLink = document.getElementById('trakt-activate-link');

    if (traktAuthBtn) {
        traktAuthBtn.addEventListener('click', function() {
            traktAuthBtn.disabled = true;
            traktAuthStatus.textContent = 'Initializing Trakt authorization...';
            traktAuthCode.style.display = 'none';

            fetch('/trakt_auth', { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    console.log('Trakt auth response:', data);
                    if (data.user_code) {
                        traktCode.textContent = data.user_code;
                        traktActivateLink.href = data.verification_url;
                        traktAuthCode.style.display = 'block';
                        traktAuthStatus.textContent = 'Please enter the code on the Trakt website to complete authorization.';
                        pollTraktAuthStatus(data.device_code);
                    } else {
                        traktAuthStatus.textContent = 'Error: ' + (data.error || 'Unable to get authorization code');
                    }
                })
                .catch(error => {
                    console.error('Trakt auth error:', error);
                    traktAuthStatus.textContent = 'Error: Unable to start authorization process';
                })
                .finally(() => {
                    traktAuthBtn.disabled = false;
                });
        });
    }

    // Check initial Trakt authorization status
    checkTraktAuthStatus();
}

function pollTraktAuthStatus(device_code) {
    const traktAuthStatus = document.getElementById('trakt-auth-status');
    const traktAuthCode = document.getElementById('trakt-auth-code');
    const pollInterval = setInterval(() => {
        fetch('/trakt_auth_status', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ device_code: device_code }),
        })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'authorized') {
                    clearInterval(pollInterval);
                    traktAuthStatus.textContent = 'Trakt authorization successful!';
                    traktAuthStatus.classList.add('authorized');
                    traktAuthCode.style.display = 'none';
                    setTimeout(() => {
                        traktAuthStatus.textContent = 'Trakt is currently authorized.';
                    }, 5000);
                } else if (data.status === 'error') {
                    clearInterval(pollInterval);
                    traktAuthStatus.textContent = 'Error: ' + (data.message || 'Unknown error occurred');
                }
            })
            .catch(error => {
                console.error('Error:', error);
                traktAuthStatus.textContent = 'Error checking authorization status. Please try again.';
            });
    }, 5000); // Check every 5 seconds
}

function checkTraktAuthStatus() {
    const traktAuthStatus = document.getElementById('trakt-auth-status');
    fetch('/trakt_auth_status', { method: 'GET' })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'authorized') {
                traktAuthStatus.textContent = 'Trakt is currently authorized.';
                traktAuthStatus.classList.add('authorized');
            } else {
                traktAuthStatus.textContent = 'Trakt is not authorized.';
                traktAuthStatus.classList.remove('authorized');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            traktAuthStatus.textContent = 'Unable to check Trakt authorization status.';
        });
}
