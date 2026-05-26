# cli_debrid

cli_debrid is a successor to, and pays homage to plex_debrid. cli_debrid is designed to automatically manage and upgrade your media collection, leveraging various sources and services to ensure you always have the best quality content available.

## How can you support the project?

[![](https://img.shields.io/static/v1?label=Sponsor&message=%E2%9D%A4&logo=GitHub&color=%23fe8e86)](https://github.com/sponsors/godver3)
[![Support me on Patreon](https://img.shields.io/endpoint.svg?url=https%3A%2F%2Fshieldsio-patreon.vercel.app%2Fapi%3Fusername%3Dgodver3%26type%3Dpatrons&style=flat)](https://patreon.com/godver3)
[![Support me on Ko-fi](https://img.shields.io/badge/Ko--fi-Support-29ABE0?style=flat&logo=ko-fi)](https://ko-fi.com/godver3)

cli_debrid will always be free.

## Version Information

*Main Branch*

![Main Branch Version](https://img.shields.io/endpoint?url=https://version.godver3.xyz/version/main)

*Dev Branch*

![Dev Branch Version](https://img.shields.io/endpoint?url=https://version.godver3.xyz/version/dev&color=orange&logoColor=orange)

## Community

- [Discord](https://discord.gg/ynqnXGJ4hU)
- Feel free to join and ask questions or to share ideas.

## Screenshots

![image](https://github.com/user-attachments/assets/084c3685-8ba7-481a-8dae-e4c45304e489)
![image](https://github.com/user-attachments/assets/a11fde0a-52a7-47da-8a95-120e977d6f8c)
![image](https://github.com/user-attachments/assets/1715c872-d508-4d54-845e-13de096feadf)
![image](https://github.com/user-attachments/assets/335b739d-99f3-4cb9-ac97-a8ae6e887a4f)
![image](https://github.com/user-attachments/assets/59d049cc-e17c-49d3-9afb-9b24ea7f0606)

## Key Features

- **Automated Media Management**: Continuously scans for new content and upgrades existing media.
- **Multiple Content Sources**: Supports MDBList, Trakt, Seerr, Plex Watchlist, Plex RSS, and Adaptive Lists.
- **Intelligent Scraping**: Supports Zilean, Jackett, Prowlarr, Torrentio, Nyaa, MediaFusion, AIOStreams, and KnightCrawler.
- **Multiple Debrid Providers**: Full support for Real-Debrid, TorBox, AllDebrid, Premiumize, and DebridLink.
- **Upgrading Function**: Automatically seeks and applies quality upgrades for collected content.
- **Web Interface**: Feature-rich web interface including Library, Discover, Overlays, Upgrade Hub, Debrid Manager, and more.
- **Metadata Battery**: Metadata is stored locally to minimise external API calls.
- **Notifications**: Discord, Email, Telegram, and NTFY support.

## Firefox Extension

cli_debrid offers a Firefox extension called **cli_debrid magnet assign** that integrates with [debridmediamanager.com](https://debridmediamanager.com) to pre-populate the magnet assignment tool with information from DMM.com.

**Download**: [Firefox Add-on](https://addons.mozilla.org/firefox/downloads/file/4567721/baa7b13ad11b4c308b3e-1.2.xpi)

**Requirements**: Minimum cli_debrid version 0.7.12

This extension streamlines the process of assigning magnets to your media by automatically populating the magnet assignment tool with relevant information from DebridMediaManager - simply add your cli_debrid instance URL, username and password.

## Overall Program

For full details on how cli_debrid works, see the **[Documentation](https://godver3.github.io/cli_docs/)**.

### dev vs main

dev is the latest version of cli_debrid. It is generally recommended for day to day use as issues are most quickly identified in dev.

main is the stable version of cli_debrid. main tends to fall behind dev and is not highly recommended.

Development generally works on a 6-8 week cycle, with dev being moved to main at the end of each cycle.

### Library Management

Supports either a Plex or Symlinked library — see [Plex](https://godver3.github.io/cli_docs/integrations/plex/) and [Jellyfin](https://godver3.github.io/cli_docs/integrations/jellyfin/) integration guides.

- Plex: Uses Plex's API to get your library and track what you have.
- Symlinked: Uses a local folder structure to track your library.
- *Important - if running on Windows, Developer Mode must be enabled to allow symlinking! Additionally Plex does not support symlinks on Windows, meaning Jellyfin is the best option on Windows when using symlinks*

### Settings

A settings menu allows you to configure all program settings. Full configuration docs:

- [Required settings](https://godver3.github.io/cli_docs/configuration/required/) (Plex, Debrid Provider, Trakt)
- [Scrapers](https://godver3.github.io/cli_docs/configuration/scrapers/) (Zilean, Jackett, Torrentio, Nyaa)
- [Versions](https://godver3.github.io/cli_docs/configuration/versions/) (scraping quality preferences, filters)
- [Content sources](https://godver3.github.io/cli_docs/configuration/content-sources/) (MDBList, Collected content, Trakt watchlists/lists, Seerr)
- [Additional settings](https://godver3.github.io/cli_docs/configuration/additional/) (UI settings, TMDB key, Metadata age threshold, deletions syncing, queue management)
- [Advanced settings](https://godver3.github.io/cli_docs/configuration/advanced/)
- [Notifications](https://godver3.github.io/cli_docs/configuration/notifications/) (Discord, Email, Telegram, NTFY)

### Manual/Testing Scraper

Allows you to manually initiate scraping for specific content. The [Testing Scraper](https://godver3.github.io/cli_docs/features/scraper-tester/) allows you to fine tune your scraping settings and weights to ensure your preferred releases are grabbed.

### Debug Functions

Provides various debugging tools for advanced users — see [Debug Functions](https://godver3.github.io/cli_docs/features/debug-functions/).


## Queue Operations

For detailed information on queue processing intervals, upgrading criteria, sleep/wake mechanics, blacklisting, multi-pack processing and webhook support, see the **[Queue Operations](https://godver3.github.io/cli_docs/features/queues/)** documentation.

## Philosophy

### Database and "I Know What I Got"

cli_debrid maintains a local database of your media collection, keeping track of what you have and what quality it's in. This "I Know What I Got" approach allows cli_debrid to maintain a list of what you have, and what you want. Other philosophies include minimized API calls, high specificity in scraping, and an easy to use interface, with a fulsome backend.

## Getting Started

New to cli_debrid? Start with the **[Getting Started guide](https://godver3.github.io/cli_docs/getting-started/)** in the documentation.

- **New here:** [Getting Started](https://godver3.github.io/cli_docs/getting-started/), [Prerequisites](https://godver3.github.io/cli_docs/getting-started/prerequisites/), [What's Next](https://godver3.github.io/cli_docs/getting-started/whats-next/)
- **Installing:** [Docker](https://godver3.github.io/cli_docs/installation/docker/), [Unraid](https://godver3.github.io/cli_docs/installation/unraid/), [Windows](https://godver3.github.io/cli_docs/installation/windows/), [TrueNAS](https://godver3.github.io/cli_docs/installation/truenas/), [Updating](https://godver3.github.io/cli_docs/installation/updating/)
- **Configuring:** [Required](https://godver3.github.io/cli_docs/configuration/required/), [Content Sources](https://godver3.github.io/cli_docs/configuration/content-sources/), [Scrapers](https://godver3.github.io/cli_docs/configuration/scrapers/), [Versions](https://godver3.github.io/cli_docs/configuration/versions/), [Notifications](https://godver3.github.io/cli_docs/configuration/notifications/)
- **Scrapers:** [Zilean](https://godver3.github.io/cli_docs/scrapers/zilean/), [Jackett](https://godver3.github.io/cli_docs/scrapers/jackett/), [Torrentio](https://godver3.github.io/cli_docs/scrapers/torrentio/), [Nyaa](https://godver3.github.io/cli_docs/scrapers/nyaa/), [AIOStreams](https://godver3.github.io/cli_docs/scrapers/aiostreams/), [MediaFusion](https://godver3.github.io/cli_docs/scrapers/mediafusion/)
- **Integrations:** [Plex](https://godver3.github.io/cli_docs/integrations/plex/), [Jellyfin](https://godver3.github.io/cli_docs/integrations/jellyfin/), [Seerr](https://godver3.github.io/cli_docs/integrations/seerr/), [Zurg](https://godver3.github.io/cli_docs/integrations/zurg/), [OpenClaw](https://godver3.github.io/cli_docs/integrations/openclaw/)
- **Features:** [Library](https://godver3.github.io/cli_docs/features/library/), [Queues](https://godver3.github.io/cli_docs/features/queues/), [Upgrade Hub](https://godver3.github.io/cli_docs/features/upgrade-hub/), [Overlays](https://godver3.github.io/cli_docs/features/overlays/), [Discover](https://godver3.github.io/cli_docs/features/discover/), [Debrid Manager](https://godver3.github.io/cli_docs/features/debrid-manager/)
- **Troubleshooting:** [FAQ](https://godver3.github.io/cli_docs/faq/), [Debug Functions](https://godver3.github.io/cli_docs/features/debug-functions/)

### Quick Start (Docker)

```bash
mkdir -p ${HOME}/cli_debrid && cd ${HOME}/cli_debrid
curl -O https://raw.githubusercontent.com/godver3/cli_debrid/main/docker-compose.yml
docker compose up -d
```

Then open `http://your-server-ip:5000` and follow the onboarding wizard.

cli_debrid is built for both AMD64 and ARM64:

- **Dev**: `godver3/cli_debrid:dev` / `godver3/cli_debrid:dev-arm64`
- **Stable**: `godver3/cli_debrid:main` / `godver3/cli_debrid:main-arm64`

## Issues

Submit issues through Discord or GitHub issues. Try to include relevant logging, or at minimum error Tracebacks where possible. Preference is for Discord submission.

## Contributing

Please contribute through either Issues or by submitting code.

## License

cli_debrid will always be free for anyone to use.

## Acknowledgements

Thanks to:

- Various other projects that have come before this one, and likely do things better in many ways
- Specific thanks to the NyaaPy, PTT/Parsett, and downsub libraries (https://github.com/JuanjoSalvador/NyaaPy, https://github.com/dreulavelle/PTT, and https://github.com/ericvlog/Downsub)
- The original creator of plex_debrid.
- Helpful communities of content creators

## Caveat

I'll include a caveat that this project was built almost entirely using AI (though I have a bit of experience working with code in the past). I would say I learned a fair bit through the process and overall enjoyed getting to this point. That said, I'll do what I can to fix things, but cli_debrid is built almost entirely on spaghetti and probably has lots of brow-raising content. Apologies in advance real devs who decide to look under the hood.
