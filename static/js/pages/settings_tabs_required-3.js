/* Moved from templates/settings_tabs/required.html. */
// Global function for updating media server settings
function updateMediaServerSettings() {
    const mediaServerType = document.getElementById('media-server-type');
    const plexSection = document.getElementById('plex-settings-in-file-management');
    const jellyfinSection = document.getElementById('jellyfin-settings-in-file-management');
    const plexUrlField = document.querySelector('input[name="File Management.plex_url_for_symlink"]')?.closest('.settings-form-group');
    const plexTokenField = document.querySelector('input[name="File Management.plex_token_for_symlink"]')?.closest('.settings-form-group');
    const movieLibsField = document.querySelector('input[name="Plex.movie_libraries"]')?.closest('.settings-form-group.symlink-plex-setting');
    const showsLibsField = document.querySelector('input[name="Plex.shows_libraries"]')?.closest('.settings-form-group.symlink-plex-setting');

    console.log('updateMediaServerSettings called');
    console.log('mediaServerType:', mediaServerType);
    console.log('mediaServerType.value:', mediaServerType ? mediaServerType.value : 'null');
    console.log('plexSection:', plexSection);
    console.log('jellyfinSection:', jellyfinSection);
    console.log('plexUrlField:', plexUrlField);
    console.log('plexTokenField:', plexTokenField);
    console.log('movieLibsField:', movieLibsField);
    console.log('showsLibsField:', showsLibsField);

    if (mediaServerType && plexSection && jellyfinSection) {
        if (mediaServerType.value === 'plex') {
            console.log('Setting to Plex');
            // For Symlinked/Local mode, only show the symlink-specific Plex fields, not the full Plex settings
            plexSection.style.display = 'none';
            jellyfinSection.style.display = 'none';
            if (plexUrlField) plexUrlField.style.display = 'block';
            if (plexTokenField) plexTokenField.style.display = 'block';
            if (movieLibsField) movieLibsField.style.display = 'block';
            if (showsLibsField) showsLibsField.style.display = 'block';
        } else if (mediaServerType.value === 'jellyfin') {
            console.log('Setting to Jellyfin');
            plexSection.style.display = 'none';
            jellyfinSection.style.display = 'block';
            if (plexUrlField) plexUrlField.style.display = 'none';
            if (plexTokenField) plexTokenField.style.display = 'none';
            if (movieLibsField) movieLibsField.style.display = 'none';
            if (showsLibsField) showsLibsField.style.display = 'none';
        } else {
            // Default case - hide both sections if no media server type is selected
            console.log('No media server type selected, hiding both sections');
            plexSection.style.display = 'none';
            jellyfinSection.style.display = 'none';
            if (plexUrlField) plexUrlField.style.display = 'none';
            if (plexTokenField) plexTokenField.style.display = 'none';
            if (movieLibsField) movieLibsField.style.display = 'none';
            if (showsLibsField) showsLibsField.style.display = 'none';
        }
    }
}

document.addEventListener('DOMContentLoaded', function() {
    // Get the elements
    const updatePlexOnDiscovery = document.getElementById('plex-update_plex_on_file_discovery');
    const disablePlexChecks = document.getElementById('plex-disable_plex_library_checks');

    // Only add event listeners if the elements exist
    if (updatePlexOnDiscovery && disablePlexChecks) {
        // Function to handle mutual exclusivity
        function handlePlexSettingsChange(event) {
            if (event.target === disablePlexChecks) {
                if (disablePlexChecks.checked) {
                    // If disable checks is enabled, disable the other one
                    updatePlexOnDiscovery.checked = false;
                }
            } else {
                // If either of the first two are modified
                if (updatePlexOnDiscovery.checked) {
                    disablePlexChecks.checked = false;
                }
            }
        }

        // Add event listeners
        updatePlexOnDiscovery.addEventListener('change', handlePlexSettingsChange);
        disablePlexChecks.addEventListener('change', handlePlexSettingsChange);
    }
});
