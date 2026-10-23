/* Moved from templates/settings_tabs/scraping.html. */
// Reverse Parser Default Version functionality
document.addEventListener('DOMContentLoaded', function() {
    const defaultVersionSelect = document.getElementById('default-version');

    function updateDefaultVersionOptions(versions, currentConfiguredDefault) {
        if (!defaultVersionSelect) return;

        const currentValue = defaultVersionSelect.value || currentConfiguredDefault;
        defaultVersionSelect.innerHTML = versions.map(v => `<option value="${v}">${v}</option>`).join('');

        if (versions.includes(currentValue)) {
            defaultVersionSelect.value = currentValue;
        } else if (versions.length > 0) {
            defaultVersionSelect.value = versions[0];
        }
    }

    // Load existing versions and settings
    Promise.all([
        fetch('/settings/get_scraping_versions').then(response => response.json()),
        fetch('/settings/api/reverse_parser_settings').then(response => response.json())
    ]).then(([versionsData, reverseParserData]) => {
        const allVersions = versionsData.versions || [];
        const configuredDefaultVersion = reverseParserData.default_version || '';

        updateDefaultVersionOptions(allVersions, configuredDefaultVersion);

        if (allVersions.includes(configuredDefaultVersion)) {
            defaultVersionSelect.value = configuredDefaultVersion;
        } else if (allVersions.length > 0) {
            defaultVersionSelect.value = allVersions[0];
        }

    }).catch(error => {
        console.error("Error loading initial data for Reverse Parser settings:", error);
    });

    // Add event listener for saving settings
    document.addEventListener('saveSettings', function() {
        if (!defaultVersionSelect) return;

        const defaultVersion = defaultVersionSelect.value;

        const settings = {
            'Reverse Parser': {
                default_version: defaultVersion
            }
        };

        fetch('/settings/api/reverse_parser_settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(settings),
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                console.log('Reverse Parser settings saved successfully');
            } else {
                console.error('Failed to save Reverse Parser settings:', data.error);
            }
        })
        .catch((error) => {
            console.error('Error saving Reverse Parser settings:', error);
        });
    });
});

document.addEventListener('scrapingContentLoaded', function() {
    const defaultVersionSelect = document.getElementById('default-version');

    function updateDefaultVersionOptions(versions, currentConfiguredDefault) {
        if (!defaultVersionSelect) return;

        const currentValue = defaultVersionSelect.value || currentConfiguredDefault;
        defaultVersionSelect.innerHTML = versions.map(v => `<option value="${v}">${v}</option>`).join('');

        if (versions.includes(currentValue)) {
            defaultVersionSelect.value = currentValue;
        } else if (versions.length > 0) {
            defaultVersionSelect.value = versions[0];
        }
    }

    Promise.all([
        fetch('/settings/get_scraping_versions').then(response => response.json()),
        fetch('/settings/api/reverse_parser_settings').then(response => response.json())
    ]).then(([versionsData, reverseParserData]) => {
        const allVersions = versionsData.versions || [];
        const configuredDefaultVersion = reverseParserData.default_version || '';

        updateDefaultVersionOptions(allVersions, configuredDefaultVersion);

        if (allVersions.includes(configuredDefaultVersion)) {
            defaultVersionSelect.value = configuredDefaultVersion;
        } else if (allVersions.length > 0) {
            defaultVersionSelect.value = allVersions[0];
        }

    }).catch(error => {
        console.error("Error loading initial data for Reverse Parser settings:", error);
    });
});
