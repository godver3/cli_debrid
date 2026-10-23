/* Moved from templates/statistics.html. */
// Calculate color based on percentage (0% = green, 50% = yellow, 100% = red)
function getGradientColor(percentage) {
    // Clamp percentage between 0 and 100
    const percent = Math.max(0, Math.min(100, percentage));

    let r, g, b;

    if (percent <= 50) {
        // Green to Yellow (0% to 50%)
        // Green: rgb(26, 40, 34) to Yellow: rgb(40, 40, 26)
        const ratio = percent / 50;
        r = Math.round(26 + (40 - 26) * ratio);
        g = Math.round(40 + (40 - 40) * ratio);
        b = Math.round(34 + (26 - 34) * ratio);
    } else {
        // Yellow to Red (50% to 100%)
        // Yellow: rgb(40, 40, 26) to Red: rgb(40, 26, 26)
        const ratio = (percent - 50) / 50;
        r = Math.round(40 + (40 - 40) * ratio);
        g = Math.round(40 + (26 - 40) * ratio);
        b = Math.round(26 + (26 - 26) * ratio);
    }

    return `rgb(${r} ${g} ${b} / 65%)`;
}

// Apply color gradient to stat boxes
function applyPercentageColorGradient() {
    // Active downloads percentage
    const activeDownloads = parseInt(document.getElementById('active_downloads')?.textContent || 0);
    const downloadLimit = parseInt(document.getElementById('download_limit')?.textContent || 75);
    const activePercentage = downloadLimit > 0 ? (activeDownloads / downloadLimit) * 100 : 0;

    // Daily usage percentage
    const dailyUsageText = document.getElementById('daily_usage')?.textContent || '0%';
    const dailyPercentage = parseFloat(dailyUsageText.replace('%', '')) || 0;

    // Find the stat-item boxes
    const statItems = document.querySelectorAll('.stat-item');

    statItems.forEach((item) => {
        const label = item.querySelector('.stat-label')?.textContent?.trim();

        if (label === 'Active') {
            const color = getGradientColor(activePercentage);
            item.style.backgroundColor = color;
            item.style.transition = 'background-color 0.3s ease';
        } else if (label === 'Daily Usage') {
            const color = getGradientColor(dailyPercentage);
            item.style.backgroundColor = color;
            item.style.transition = 'background-color 0.3s ease';
        }
    });
}

// Update active downloads display based on data from server
function updateActiveDownloadsDisplay() {
    const data = window.pageData.statistics.active_downloads_data;
    const downloadsElement = document.getElementById('active_downloads');
    // Only one of these two siblings exists at a time — debrid mode
    // renders #download_limit (active/limit), usenet/combined mode
    // renders #idle_downloads (active/idle) instead.
    const limitElement = document.getElementById('download_limit');
    const idleElement = document.getElementById('idle_downloads');
    const subDaysElement = document.getElementById('subscription_days');

    // Skip if elements don't exist (limited environment)
    if (!downloadsElement || !data) {
        return;
    }

    const statusElement = downloadsElement.parentElement;

    // Update the display values with fallbacks
    downloadsElement.textContent = data.count || 0;
    if (limitElement) {
        limitElement.textContent = data.limit || 25;  // Fallback to default max downloads
    }
    if (idleElement) {
        idleElement.textContent = data.idle || 0;
    }

    // Update styling based on status
    statusElement.classList.remove('warning', 'critical');
    if (data.status === 'critical') {
        statusElement.classList.add('critical');
    } else if (data.status === 'warning') {
        statusElement.classList.add('warning');
    }

    // Update color gradient for tangerine theme
    applyPercentageColorGradient();

    // Initialize subscription days from server-rendered stats on first paint
    try {
        const initialStats = window.pageData.statistics.value;
        if (subDaysElement && initialStats && initialStats.subscription_status) {
            const days = initialStats.subscription_status.days_remaining;
            if (days !== null && days !== undefined) {
                subDaysElement.textContent = ` (${days} days)`;
            }
        }
    } catch (e) {}
}

// Update usage stats display based on data from server
function updateUsageStatsDisplay() {
    const data = window.pageData.statistics.usage_stats_data;
    const dailyUsageElement = document.getElementById('daily_usage');
    const dailyLimitElement = document.getElementById('daily_limit');

    // Skip if elements don't exist (limited environment)
    if (!dailyUsageElement || !dailyLimitElement || !data) {
        return;
    }

    const usageElement = dailyUsageElement.parentElement;

    // Update with pre-formatted values from backend
    dailyUsageElement.textContent = data.used;
    dailyLimitElement.textContent = data.limit;

    // Update styling based on percentage
    usageElement.classList.remove('warning', 'critical');
    if (data.percentage >= 90) {
        usageElement.classList.add('critical');
    } else if (data.percentage >= 75) {
        usageElement.classList.add('warning');
    }

    // Update color gradient for tangerine theme
    applyPercentageColorGradient();
}

document.getElementById('time-format-toggle').addEventListener('change', function() {
    const isChecked = this.checked;

    // Disable the toggle while processing
    this.disabled = true;

    fetch('/set_time_preference', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        },
        body: JSON.stringify({
            use24HourFormat: isChecked
        })
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        if (data.status === 'OK') {
            // Log the data structure for debugging
            console.log('Time preference data:', data);

            // Update the UI with the new data without page reload
            updateUIWithTimePreference(data);
        } else {
            throw new Error('Failed to update time preference');
        }
    })
    .catch(error => {
        console.error('Error updating time preference:', error);
        // Revert the toggle state
        this.checked = !isChecked;
    })
    .finally(() => {
        // Re-enable the toggle
        this.disabled = false;
    });
});

document.getElementById('compact-toggle').addEventListener('change', function() {
    const isChecked = this.checked;

    // Disable the toggle while processing
    this.disabled = true;

    fetch('/set_compact_preference', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        },
        body: JSON.stringify({
            compactView: isChecked
        })
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        if (data.success) {
            // Instead of immediate reload, use a small timeout
            setTimeout(() => {
                window.location.reload();
            }, 100);
        } else {
            throw new Error('Failed to update compact preference');
        }
    })
    .catch(error => {
        // Only log and revert if it's not an AbortError (caused by navigation)
        if (error.name !== 'AbortError') {
            console.error('Error updating compact preference:', error);
            // Revert the toggle state
            this.checked = !isChecked;
        }
    })
    .finally(() => {
        // Re-enable the toggle
        this.disabled = false;
    });
});
