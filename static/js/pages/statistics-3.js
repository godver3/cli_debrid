/* Moved from templates/statistics.html. */
// Convert filenames to quality badges on statistics page
function applyQualityBadges() {
    // Check if functions are available
    if (typeof extractQualityTags !== 'function' || typeof createQualityBadge !== 'function') {
        console.warn('Quality badge functions not available yet, retrying...');
        setTimeout(applyQualityBadges, 100);
        return;
    }

    console.log('Applying quality badges...');

    // Find filename elements — file-details first so card dedup doesn't skip them
    const filenameElements = document.querySelectorAll('.file-details .filename');
    console.log('Found filename elements:', filenameElements.length);

    // Group by parent card to handle upgraded items with From/To
    const processedCards = new Set();

    filenameElements.forEach(filenameSpan => {
        // Find the parent stats-card
        const card = filenameSpan.closest('.stats-card');
        if (!card || processedCards.has(card)) {
            return;
        }

        // Check if this card has already been processed
        if (card.hasAttribute('data-badges-applied')) {
            return;
        }

        // Check if this is a recently-upgraded item (has both From and To)
        const fromSpan = card.querySelector('.filename[data-label="From:"]');
        const toSpan = card.querySelector('.filename[data-label="To:"]');

        if (fromSpan && toSpan) {
            // Recently Upgraded: Show both old >> new
            // Store original filenames if not already stored
            if (!fromSpan.hasAttribute('data-original-filename')) {
                fromSpan.setAttribute('data-original-filename', fromSpan.textContent.trim());
            }
            if (!toSpan.hasAttribute('data-original-filename')) {
                toSpan.setAttribute('data-original-filename', toSpan.textContent.trim());
            }

            const oldFilename = fromSpan.getAttribute('data-original-filename');
            const newFilename = toSpan.getAttribute('data-original-filename');

            if (oldFilename && newFilename) {
                console.log('Processing upgraded item:', oldFilename, '>>', newFilename);
                const oldTags = extractQualityTags(oldFilename);
                const newTags = extractQualityTags(newFilename);
                console.log('Old tags:', oldTags.map(t => `${t.type}:${t.value}`).join(', '));
                console.log('New tags:', newTags.map(t => `${t.type}:${t.value}`).join(', '));

                if (oldTags.length > 0 || newTags.length > 0) {
                    const oldBadgesHTML = oldTags.map(tag => createQualityBadge(tag)).join('');
                    const newBadgesHTML = newTags.map(tag => createQualityBadge(tag)).join('');

                    // Check if container already exists
                    let container = card.querySelector('.upgraded-tags-container');
                    if (!container) {
                        // Create container for both
                        container = document.createElement('div');
                        container.className = 'upgraded-tags-container';
                        container.innerHTML = `
                            <div class="release-tags">${oldBadgesHTML}</div>
                            <span class="upgrade-arrow">>></span>
                            <div class="release-tags">${newBadgesHTML}</div>
                        `;

                        // Insert container before the fromSpan
                        fromSpan.parentNode.insertBefore(container, fromSpan);
                    } else {
                        // Container exists, just update content and show it
                        container.innerHTML = `
                            <div class="release-tags">${oldBadgesHTML}</div>
                            <span class="upgrade-arrow">>></span>
                            <div class="release-tags">${newBadgesHTML}</div>
                        `;
                    }

                    // Show container and hide the original spans
                    container.style.display = '';
                    fromSpan.style.display = 'none';
                    toSpan.style.display = 'none';
                }
            }
            processedCards.add(card);
            card.setAttribute('data-badges-applied', 'true');
        } else {
            // Regular item: Just convert the single filename
            const filenameSpan = card.querySelector('.filename');
            if (filenameSpan) {
                // Store original filename if not already stored
                if (!filenameSpan.hasAttribute('data-original-filename')) {
                    filenameSpan.setAttribute('data-original-filename', filenameSpan.textContent.trim());
                }

                const filename = filenameSpan.getAttribute('data-original-filename');
                console.log('Processing regular item:', filename);

                if (filename) {
                    const qualityTags = extractQualityTags(filename);
                    console.log('Extracted tags:', qualityTags.map(t => `${t.type}:${t.value}`).join(', '));
                    if (qualityTags.length > 0) {
                        const badgesHTML = qualityTags.map(tag => createQualityBadge(tag)).join('');
                        filenameSpan.innerHTML = `<div class="release-tags">${badgesHTML}</div>`;
                        card.setAttribute('data-badges-applied', 'true');
                    }
                }
            }
            processedCards.add(card);
        }
    });

    console.log('Finished applying badges to', processedCards.size, 'cards');
}

// Apply badges on page load
document.addEventListener('DOMContentLoaded', function() {
    console.log('DOM loaded, waiting for scripts...');
    // Wait a bit to ensure all scripts are loaded
    setTimeout(applyQualityBadges, 200);
});
