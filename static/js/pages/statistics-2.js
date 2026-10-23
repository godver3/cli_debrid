/* Moved from templates/statistics.html. */
// Auto-refresh functionality
(function() {
    console.log('Auto-refresh script initialized');

    // Configuration
    const refreshInterval = 300000; // 5 minutes
    let refreshTimer = null;

    // Function to refresh stats without full page reload
    window.refreshStats = refreshStats;
    function refreshStats() {
        console.log('Attempting to refresh stats...');

        fetch('/statistics/api/index')
            .then(response => {
                console.log('Response received:', response.status);
                return response.json();
            })
            .then(data => {
                // Log the entire data object for debugging
                console.log('Raw data received:', data);

                // Now we can check the structure and update accordingly
                if (data) {
                    // Use the existing function to update UI if available
                    console.log('Updating UI with new data');

                    // Extract the pieces we need whether or not they're nested
                    const activeDownloadsData = data.active_downloads_data || 
                                              (data.stats && data.stats.active_downloads_data) || 
                                              {};

                    const usageStatsData = data.usage_stats_data || 
                                         (data.stats && data.stats.usage_stats_data) || 
                                         {};

                    // Update active downloads (only if not in limited environment).
                    // Two mutually exclusive layouts share #active_downloads:
                    // debrid mode pairs it with #download_limit (active/limit),
                    // usenet/combined mode pairs it with #idle_downloads
                    // (active/idle connections) — only one of the two sibling
                    // elements exists in the DOM at a time, per which the
                    // server rendered.
                    const activeDownloadsElement = document.getElementById('active_downloads');
                    const downloadLimitElement = document.getElementById('download_limit');
                    const idleDownloadsElement = document.getElementById('idle_downloads');
                    const subDaysElement = document.getElementById('subscription_days');
                    if (activeDownloadsElement && activeDownloadsData) {
                        activeDownloadsElement.textContent = activeDownloadsData.count || "0";
                        if (downloadLimitElement) {
                            downloadLimitElement.textContent = activeDownloadsData.limit || "25";
                        }
                        if (idleDownloadsElement) {
                            idleDownloadsElement.textContent = activeDownloadsData.idle || "0";
                        }

                        // Also update class if status is provided
                        if (activeDownloadsData.status) {
                            const statusElement = activeDownloadsElement.parentElement;
                            statusElement.classList.remove('warning', 'critical');
                            if (activeDownloadsData.status === 'critical') {
                                statusElement.classList.add('critical');
                            } else if (activeDownloadsData.status === 'warning') {
                                statusElement.classList.add('warning');
                            }
                        }
                    }
                    // Update subscription days if present
                    const subscriptionData = data.subscription_status || (data.stats && data.stats.subscription_status) || null;
                    if (subDaysElement) {
                        if (subscriptionData && subscriptionData.days_remaining !== null && subscriptionData.days_remaining !== undefined) {
                            subDaysElement.textContent = ` (${subscriptionData.days_remaining} days)`;
                        } else {
                            subDaysElement.textContent = '';
                        }
                    }

                    // Update the toggle-bar Broken/Sub pill on auto-refresh, mode-aware
                    try {
                        updateStatsToggleBarPill(data);
                    } catch (e) {}

                    // Update usage stats (only if not in limited environment)
                    const dailyUsageElement = document.getElementById('daily_usage');
                    const dailyLimitElement = document.getElementById('daily_limit');
                    if (dailyUsageElement && dailyLimitElement && usageStatsData) {
                        dailyUsageElement.textContent = usageStatsData.used || "0";
                        dailyLimitElement.textContent = usageStatsData.limit || "0";

                        // Update styling based on percentage
                        const usageElement = dailyUsageElement.parentElement;
                        usageElement.classList.remove('warning', 'critical');
                        if (usageStatsData.percentage >= 90) {
                            usageElement.classList.add('critical');
                        } else if (usageStatsData.percentage >= 75) {
                            usageElement.classList.add('warning');
                        }
                    }

                    // Try to update other elements if the data is suitable format
                    // Only call updateUIWithTimePreference if it exists and data seems right
                    if (typeof updateUIWithTimePreference === 'function' && 
                        (data.status === 'OK' || data.recently_aired || data.airing_soon)) {
                        try {
                            updateUIWithTimePreference(data);
                            console.log('Full UI update successful');
                        } catch (err) {
                            console.error('Error in updateUIWithTimePreference:', err);
                        }
                    } else {
                        // If we can't use the existing update function, at least make sure
                        // click event listeners are reattached
                        if (typeof reattachExternalLinksListeners === 'function') {
                            try {
                                reattachExternalLinksListeners();
                                console.log('Reattached external links listeners');
                            } catch (err) {
                                console.error('Error reattaching external links listeners:', err);
                            }
                        }
                        // Reattach recently aired action button listeners after refresh
                        if (typeof attachRecentlyAiredListeners === 'function') {
                            try {
                                attachRecentlyAiredListeners();
                                console.log('Reattached recently aired listeners after auto-refresh');
                            } catch (err) {
                                console.error('Error reattaching recently aired listeners:', err);
                            }
                        }
                    }

                    console.log('Stats refreshed successfully');
                } else {
                    console.warn('No data received from API');
                }
            })
            .catch(error => {
                console.error('Error refreshing stats:', error);
            })
            .finally(() => {
                console.log('Scheduling next refresh in', refreshInterval, 'ms');
                scheduleNextRefresh();
            });
    }

    // Schedule the next refresh
    function scheduleNextRefresh() {
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(refreshStats, refreshInterval);
        console.log('Next refresh scheduled at:', new Date(Date.now() + refreshInterval).toLocaleTimeString());
    }

    // Start the refresh cycle - with a slight delay to ensure page is fully loaded
    setTimeout(() => {
        console.log('Starting initial refresh cycle');
        scheduleNextRefresh();
    }, 5000);

    // Log that the script is ready
    console.log('Auto-refresh setup complete');
})(); // Self-executing function
