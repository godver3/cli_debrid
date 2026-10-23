/* Moved from templates/settings_tabs/debug.html. */
document.addEventListener('DOMContentLoaded', function() {
    const uiCheckbox = document.getElementById('ui_enable_unblacklisting');
    const actualCheckbox = document.getElementById('debug-disable_unblacklisting');

    // Set initial state of the visible UI checkbox based on the actual (hidden) checkbox's state
    // If actual 'disable_unblacklisting' is checked (true), then 'enable_unblacklisting' is unchecked (false).
    if (actualCheckbox) { // Ensure the element exists
         uiCheckbox.checked = !actualCheckbox.checked;

        uiCheckbox.addEventListener('change', function() {
            // When the visible UI checkbox changes, update the actual (hidden) checkbox inversely
            // If 'ui_enable_unblacklisting' is now checked, then 'disable_unblacklisting' should be false (unchecked).
            actualCheckbox.checked = !this.checked;

            // Dispatch a change event on the actual checkbox so that any existing save mechanisms
            // that listen for changes on data-key="disable_unblacklisting" will pick it up.
            const event = new Event('change', { bubbles: true });
            actualCheckbox.dispatchEvent(event);
        });
    }
});
