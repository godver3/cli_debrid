/* Moved from templates/settings_tabs/scraping.html. */
// Initialize scraper priority toggles
function initializeScraperPriorityToggles() {
    // Find all enable scraper priorities checkboxes
    const toggles = document.querySelectorAll('[id^="scraping-"][id$="-enable_scraper_priorities"]');

    toggles.forEach(toggle => {
        // Get version from the checkbox ID (e.g., "scraping-1080p-enable_scraper_priorities" -> "1080p")
        const version = toggle.id.replace('scraping-', '').replace('-enable_scraper_priorities', '');
        const prioritiesSection = document.getElementById(`scraper-priorities-${version}`);

        if (!prioritiesSection) return;

        // Function to update the disabled state
        function updateDisabledState() {
            if (toggle.checked) {
                prioritiesSection.style.opacity = '1';
                prioritiesSection.style.pointerEvents = 'auto';
            } else {
                prioritiesSection.style.opacity = '0.5';
                prioritiesSection.style.pointerEvents = 'none';
            }
        }

        // Set initial state
        updateDisabledState();

        // Add event listener for changes
        toggle.addEventListener('change', updateDisabledState);
    });
}

document.addEventListener('DOMContentLoaded', initializeScraperPriorityToggles);
document.addEventListener('scrapingContentLoaded', initializeScraperPriorityToggles);
