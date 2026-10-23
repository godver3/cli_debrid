/* Moved from templates/manual_assignment.html. */
const notificationsUrl = window.pageData.manual_assignment.static_notifications_js;
let showPopup, POPUP_TYPES;

// Dynamic import of notifications module
import(notificationsUrl).then(module => {
    showPopup = module.showPopup;
    POPUP_TYPES = module.POPUP_TYPES;
}).catch(error => console.error('Error loading notifications module:', error));

document.addEventListener('DOMContentLoaded', function() {
    const assignmentForm = document.getElementById('manual-assignment-form');
    const submitButton = document.getElementById('confirm-assignment-btn');

    assignmentForm.addEventListener('submit', async function(e) {
        e.preventDefault();
        submitButton.disabled = true;
        Loading.show();

        try {
            const formData = new FormData(assignmentForm);
            const response = await fetch(assignmentForm.action, {
                method: 'POST',
                body: formData,
                headers: {
                    'Accept': 'application/json'
                }
            });

            const data = await response.json();
            Loading.hide();

            if (response.ok && data.success) {
                showPopup({
                    type: POPUP_TYPES.SUCCESS,
                    title: 'Assignment Successful',
                    message: data.message || `Successfully assigned ${data.added_count || 0} item(s).`,
                    autoClose: 4000,
                    onConfirm: () => { window.location.href = window.pageData.manual_assignment.discover_index_url; }
                });
                setTimeout(() => { window.location.href = window.pageData.manual_assignment.discover_index_url; }, 4000);
            } else {
                showPopup({
                    type: POPUP_TYPES.ERROR,
                    title: 'Assignment Failed',
                    message: data.error || 'An error occurred during assignment.',
                    autoClose: 5000
                });
                submitButton.disabled = false; // Re-enable button on failure
            }
        } catch (error) {
            Loading.hide();
            console.error('Error submitting assignment:', error);
            showPopup({
                type: POPUP_TYPES.ERROR,
                title: 'Submission Error',
                message: 'A network or unexpected error occurred.',
                autoClose: 5000
            });
            submitButton.disabled = false; // Re-enable button on error
        }
    });
});
