document.addEventListener('DOMContentLoaded', function() {
    loadScrapers();
    document.getElementById('add-scraper').addEventListener('click', addScraper);
});

function loadScrapers() {
    fetch('/api/scrapers')
        .then(response => response.json())
        .then(scrapers => {
            const scrapersList = document.getElementById('scrapers-list');
            scrapersList.innerHTML = '';
            for (const [id, config] of Object.entries(scrapers)) {
                scrapersList.appendChild(createScraperElement(id, config));
            }
        });
}

function createScraperElement(id, config) {
    const div = document.createElement('div');
    div.className = 'scraper';
    div.innerHTML = `
        <h3>${id}</h3>
        <label>
            Enabled:
            <input type="checkbox" ${config.enabled ? 'checked' : ''} onchange="updateScraper('${id}', 'enabled', this.checked)">
        </label>
        ${config.url !== undefined ? `
        <label>
            URL:
            <input type="text" value="${config.url}" onchange="updateScraper('${id}', 'url', this.value)">
        </label>
        ` : ''}
        ${config.api !== undefined ? `
        <label>
            API Key:
            <input type="text" value="${config.api}" onchange="updateScraper('${id}', 'api', this.value)">
        </label>
        ` : ''}
        ${config.enabled_indexers !== undefined ? `
        <label>
            Enabled Indexers:
            <input type="text" value="${config.enabled_indexers}" onchange="updateScraper('${id}', 'enabled_indexers', this.value)">
        </label>
        ` : ''}
        ${config.opts !== undefined ? `
        <label>
            Options:
            <input type="text" value="${config.opts}" onchange="updateScraper('${id}', 'opts', this.value)">
        </label>
        ` : ''}
        <button onclick="removeScraper('${id}')">Remove</button>
    `;
    return div;
}

function updateScraper(id, key, value) {
    fetch('/api/scrapers', {
        method: 'PUT',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ id, updates: { [key]: value } }),
    }).then(() => loadScrapers());
}

function removeScraper(id) {
    if (confirm(`Are you sure you want to remove the scraper ${id}?`)) {
        fetch('/api/scrapers', {
            method: 'DELETE',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ id }),
        }).then(() => loadScrapers());
    }
}

function addScraper() {
    const type = prompt('Enter the scraper type:');
    if (type) {
        fetch('/api/scrapers', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ type }),
        }).then(() => loadScrapers());
    }
}