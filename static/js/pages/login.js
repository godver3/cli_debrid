/* Moved from templates/login.html. */
document.addEventListener('DOMContentLoaded', function() {
    var flashMessages = document.querySelectorAll('.flash-message');
    if (flashMessages.length > 0) {
        flashMessages.forEach(function(flashMessage) {
            setTimeout(function() {
                flashMessage.classList.add('show');
            }, 100);

            // Don't auto-hide login failure messages
            var messageText = flashMessage.textContent.toLowerCase();
            if (!messageText.includes('please check your login details')) {
                setTimeout(function() {
                    flashMessage.classList.remove('show');
                }, 5000);
            }
        });
    }

    var rememberMeCheckbox = document.getElementById('remember_me');
    if (rememberMeCheckbox) {
        if (localStorage.getItem('rememberMe') === 'true') {
            rememberMeCheckbox.checked = true;
        }

        rememberMeCheckbox.addEventListener('change', function() {
            localStorage.setItem('rememberMe', this.checked);
        });
    }
});
