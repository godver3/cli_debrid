"""Auth headers for Jellyfin / Emby API calls."""


def jellyfin_auth_headers(token: str) -> dict:
    """Headers that authenticate an API key against Jellyfin and Emby.

    Jellyfin has deprecated the legacy X-Emby-Token header (along with
    X-MediaBrowser-Token and the api_key query parameter): newer servers turn
    it off by default and answer 401 unless EnableLegacyAuthorization is set in
    system.xml. The supported form is the standard Authorization header.
    Emby still reads X-Emby-Token, so both are sent; Jellyfin uses the
    Authorization header when present and ignores the legacy one.
    """
    token = (token or '').strip().replace('"', '')
    return {
        'Authorization': f'MediaBrowser Token="{token}"',
        'X-Emby-Token': token,
    }
