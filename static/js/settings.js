document.addEventListener('DOMContentLoaded', function() {
    const tabButtons = document.querySelectorAll('.tab-button');
    const sections = document.querySelectorAll('.section-header');

    tabButtons.forEach(button => {
        button.addEventListener('click', () => {
            const tabName = button.getAttribute('data-tab');
            openTab(tabName);
        });
    });

    sections.forEach(section => {
        section.addEventListener('click', () => {
            toggleSection(section.parentElement.querySelector('.section-content'));
        });
    });
});

function openTab(tabName) {
    const tabContents = document.querySelectorAll('.tab-content');
    const tabButtons = document.querySelectorAll('.tab-button');

    tabContents.forEach(content => content.style.display = 'none');
    tabButtons.forEach(button => button.classList.remove('active'));

    document.getElementById(tabName).style.display = 'block';
    document.querySelector(`[data-tab="${tabName}"]`).classList.add('active');
}

function toggleSection(sectionContent) {
    const isActive = sectionContent.classList.contains('active');
    const toggleIcon = sectionContent.previousElementSibling.querySelector('.toggle-icon');
    
    sectionContent.classList.toggle('active');
    toggleIcon.textContent = isActive ? '+' : '-';
}

function updateSettings(event) {
    event.preventDefault();
    
    let formData = new FormData(event.target);
    let settings = {};

    for (let [key, value] of formData.entries()) {
        let keys = key.split('.');
        let current = settings;
        for (let i = 0; i < keys.length - 1; i++) {
            if (!(keys[i] in current)) {
                current[keys[i]] = {};
            }
            current = current[keys[i]];
        }
        if (value === 'true') {
            value = true;
        } else if (value === 'false') {
            value = false;
        } else if (!isNaN(value) && value !== '') {
            value = Number(value);
        }
        current[keys[keys.length - 1]] = value;
    }

    fetch('/api/settings', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(settings)
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            displaySuccess('Settings saved successfully!');
        } else {
            displayError('Error saving settings.');
        }
    })
    .catch((error) => {
        console.error('Error:', error);
        displayError('Error saving settings.');
    });
}

function displaySuccess(message) {
    const saveStatus = document.getElementById('saveStatus');
    saveStatus.textContent = message;
    saveStatus.style.color = 'green';
}

function displayError(message) {
    const saveStatus = document.getElementById('saveStatus');
    saveStatus.textContent = message;
    saveStatus.style.color = 'red';
}