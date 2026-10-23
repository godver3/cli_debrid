/* Moved from templates/settings_tabs/additional.html. */
document.addEventListener('DOMContentLoaded', function() {
    const sourceList = document.getElementById('content-source-list');
    let draggingElement = null;

    // Function to load enabled content sources
    function loadEnabledContentSources() {
        fetch('/settings/api/enabled_content_sources')
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    // Get current priority order
                    const priorityInput = document.querySelector('input[name="Queue.content_source_priority"]');
                    const currentOrder = priorityInput.value.split(',').map(s => s.trim());

                    // Sort sources based on current order
                    const sources = data.sources.sort((a, b) => {
                        const aIndex = currentOrder.indexOf(a.id);
                        const bIndex = currentOrder.indexOf(b.id);
                        // If neither is in the current order, maintain their original order
                        if (aIndex === -1 && bIndex === -1) return 0;
                        // If only one is in the current order, put the one that is first
                        if (aIndex === -1) return 1;
                        if (bIndex === -1) return -1;
                        // Otherwise sort by their position in the current order
                        return aIndex - bIndex;
                    });

                    // Clear existing list
                    sourceList.innerHTML = '';

                    // Add sources to list
                    sources.forEach(source => {
                        const li = document.createElement('li');
                        li.className = 'content-source-item';
                        // Store both ID and display name as data attributes
                        li.setAttribute('data-source-id', source.id);
                        li.setAttribute('data-display-name', source.display_name);

                        // Show the display name in the UI
                        li.textContent = source.display_name;

                        // Create a tooltip with the source ID for reference
                        li.title = `Source ID: ${source.id}`;
                        sourceList.appendChild(li);
                    });

                    // Initialize drag and drop
                    initDragAndDrop();
                }
            })
            .catch(error => console.error('Error loading content sources:', error));
    }

    function initDragAndDrop() {
        const items = sourceList.querySelectorAll('.content-source-item');

        items.forEach(item => {
            item.draggable = true;

            item.addEventListener('dragstart', function(e) {
                draggingElement = item;
                item.classList.add('dragging');
            });

            item.addEventListener('dragend', function(e) {
                draggingElement = null;
                item.classList.remove('dragging');

                // Update the priority input using source IDs
                const newOrder = Array.from(sourceList.children)
                    .map(item => item.getAttribute('data-source-id'));

                // Update the hidden input
                const priorityInput = document.querySelector('input[name="Queue.content_source_priority"]');
                priorityInput.value = newOrder.join(',');

                // Save the new order
                fetch('/settings/api/save_content_source_order', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ order: newOrder }),
                })
                .catch(error => console.error('Error saving order:', error));
            });

            item.addEventListener('dragover', function(e) {
                e.preventDefault();
                if (!draggingElement || draggingElement === item) return;

                const rect = item.getBoundingClientRect();
                const midpoint = rect.top + rect.height / 2;

                if (e.clientY < midpoint) {
                    sourceList.insertBefore(draggingElement, item);
                } else {
                    sourceList.insertBefore(draggingElement, item.nextSibling);
                }
            });
        });
    }

    // Load content sources when the page loads
    loadEnabledContentSources();
});
