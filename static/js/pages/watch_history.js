/* Moved from templates/watch_history.html. */
function confirmClearHistory() {
    showPopup({
        type: 'confirm',
        title: 'Clear Watch History',
        message: 'Are you sure you want to clear all watch history? This action cannot be undone.',
        confirmText: 'Clear',
        cancelText: 'Cancel',
        onConfirm: function() {
            fetch('/database/watch_history/clear', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showPopup({ type: 'success', title: 'Success', message: 'Watch history cleared successfully', autoClose: 4000 });
                    window.location.reload();
                } else {
                    showPopup({ type: 'error', title: 'Error', message: 'Error clearing watch history: ' + data.error, autoClose: 5000 });
                }
            })
            .catch(error => {
                showPopup({ type: 'error', title: 'Error', message: 'Error clearing watch history: ' + error, autoClose: 5000 });
            });
        }
    });
}
