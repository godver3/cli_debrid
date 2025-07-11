# cli_debrid

cli_debrid is a successor to, and pays homage to plex_debrid. cli_debrid is designed to automatically manage and upgrade your media collection, leveraging various sources and services to ensure you always have the best quality content available.

## How can you support the project?

[![](https://img.shields.io/static/v1?label=Sponsor&message=%E2%9D%A4&logo=GitHub&color=%23fe8e86)](https://github.com/sponsors/godver3)
[![Support me on Patreon](https://img.shields.io/endpoint.svg?url=https%3A%2F%2Fshieldsio-patreon.vercel.app%2Fapi%3Fusername%3Dgodver3%26type%3Dpatrons&style=flat)](https://patreon.com/godver3)
[![Support me on Ko-fi](https://img.shields.io/badge/Ko--fi-Support-29ABE0?style=flat&logo=ko-fi)](https://ko-fi.com/godver3)

cli_debrid will always be free.

## Need Help? Hitting a Brick Wall?

If you're struggling with setup or configuration, consider using [Debridify](https://debridify.xyz) - a hosted service that offers:

- **Monthly Hosted Service**: Ready-to-use cli_debrid/Jellyfin/Jellyseerr instance with full support
- **One-Time Setup**: Custom deployment with your own equipment

Both options provide a hassle-free way to get cli_debrid running without the complexity of manual setup. This is a service provided directly by the creator of cli_debrid.

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
- **Multiple Content Sources**: Supports MDBList, Trakt, Overseerr and more.
- **Intelligent Scraping**: Uses multiple scrapers to find the best quality content available.
- **Real-Debrid Integration**: Uses Real-Debrid for cached/uncached content. More providers to come in the future.
- **Upgrading Function**: Automatically seeks and applies upgrades for newly added content.
- **Web Interface**: Provides a user-friendly web interface for monitoring.
- **Metadata Battery**: Metadata is stored locally in a battery to avoid over-usage of APIs.

## Overall Program

### Run Program

The core functionality of the software. When started, it:

1. Determines your current existing content.
2. Checks content sources for any wanted content that isn't already collected.
3. Scrapes various sources for the best quality versions of wanted content.
4. Manages downloads through your Debrid provider.
5. Seeks upgrades for your media if available.

### dev vs main

dev is the latest version of cli_debrid. It is generally recommended for day to day use as issues are most quickly identified in dev.

main is the stable version of cli_debrid. main tends to fall behind dev and is not highly recommended.

Development generally works on a 6-8 week cycle, with dev being moved to main at the end of each cycle.

### Library Management

Supports either a Plex or Symlinked library:

- Plex: Uses Plex's API to get your library and track what you have.
- Symlinked: Uses a local folder structure to track your library.
- *Important - if running on Windows, Developer Mode must be enabled to allow symlinking! Additionally Plex does not support symlinks on Windows, meaning Jellyfin is the best option on Windows when using symlinks*

### Settings

A settings menu allows you to configure all program settings:

- Required settings (Plex, Debrid Provider, Trakt)
- Scrapers (Zilean, Jackett, Torrentio, Nyaa)
- Scraping settings (Quality preferences, filters)
- Content sources (MDBList, Collected content, Trakt watchlists/lists, Overseerr)
- Additional settings (UI settings, TMDB key, Metadata age threshold, deletions syncing, queue management)
- Advanced settings
- Notifications (Discord, Email, Telegram, NTFY)
- Reverse Parser (used to assign versions to existing content through regex terms)
- Debug settings

### Manual/Testing Scraper

Allows you to manually initiate scraping for specific content. The Testing Scraper allows you to fine tune your scraping settings and weights to ensure your preferred releases are grabbed.

### Debug Functions

Provides various debugging tools for advanced users.

## Detailed Queue Operations
<details>
<summary>Queue Processing Intervals</summary>
<br>
cli_debrid processes different queues at various intervals to optimize performance and resource usage. Here are the default processing intervals for each queue:

- Wanted Queue: Every 5 seconds - Moves items to either Scraping or Unreleased queues
- Scraping Queue: Every 5 seconds - Searches for items and moves into Adding or Sleeping (if not found)
- Adding Queue: Every 5 seconds - Adds items to Real Debrid or moves into Sleeping (if failed)
- Checking Queue: Every 5 minutes (300 seconds) - Runs a Plex Recently Added scan and marks items as Collected if found. If an item isn't found for 6 hours move the item back into Wanted and mark the magnet as unwanted
- Sleeping Queue: Every 15 minutes (900 seconds) - Details below, used for items that have not yet been scraped successfully
- Upgrading Queue: Every 5 minutes (300 seconds) - Checks for items eligible for upgrades every 5 minutes

</details>
<details>
<summary>Additional Tasks and Their Intervals</summary>
<br>
Additional task information:

- Full Plex Scan: Every 1 hour (3600 seconds)
- Overseerr Wanted Content Check: Every 15 minutes (900 seconds)
- MDBList Wanted Content Check: Every 15 minutes (900 seconds)
- Debug Log: Every 1 minute (60 seconds)
- Refresh Release Dates: Every 1 hour (3600 seconds)
- Collected Wanted Content Check: Every 24 hours (86400 seconds)
- Tasks can be managed/enabled through the Task Manager page

</details>
<details>
<summary>Upgrading Queue Criteria</summary>
<br>
Items are added to the Upgrading Queue when:

- They are successfully added to Real-Debrid and moved to the Checking Queue.
- They were released within the past week

Items in the Upgrading Queue are processed every 60 minutes to check for potential quality upgrades for recently added content.
</details>
<details>
<summary>Sleep and Wake Mechanism</summary>
<br>
Items in the Sleeping Queue use a wake count system:

- Initial sleep duration: 30 minutes
- After each sleep cycle, the wake count for the item is incremented
- Default wake limit: 3 attempts (configurable in settings)
- If an item reaches the wake limit, it's moved to the Blacklisted state
- Items with a release date older than one week are also moved to the Blacklisted state

</details>
<details>
<summary>Blacklisting</summary>
<br>
Items are blacklisted (moved to the Blacklisted state) when:

- They exceed the wake limit in the Sleeping Queue
- Their release date is more than one week old and weren't found on first scrape

Blacklisted items are no longer processed by the queue system. Blacklisted items are woken per your Blacklist Duration if enabled.
</details>
<details>
<summary>Multi-pack Processing</summary>
<br>
When a multi-pack result (e.g., a full season) is found:

- The original item is moved to the Checking Queue
- All matching episodes in the Wanted, Scraping, and Sleeping queues are also moved to the Checking Queue
- All moved items are added to the Upgrading Queue for potential future upgrades

</details>
<details>
<summary>Webhook Support</summary>
<br>
cli_debrid supports webhooks from Overseerr:

- Receives notifications for new content requests
- Processes the webhook data and adds new items to the Wanted Queue
- To use, enable the Webhook agent in Overseerr, set the URL to https://localhost:5000/webhook (or wherever Overseerr can see your cli_debrid instance at) and enable Notifications for "Request Pending Approval" and "Request Automatically Approved"

cli_debrid can also receive webhooks from Zurg to process non-cli_debrid added items (i.e. through DebridMediaManager)

See the [Wiki](https://github.com/godver3/cli_debrid/wiki/Webhooks) for more details

</details>

## Philosophy

### Database and "I Know What I Got"

cli_debrid maintains a local database of your media collection, keeping track of what you have and what quality it's in. This "I Know What I Got" approach allows cli_debrid to maintain a list of what you have, and what you want. Other philosophies include minimized API calls, high specificity in scraping, and an easy to use interface, with a fulsome backend.

## Required Components

- **Plex or Jellyfin**: Used as the primary source of information about your current media collection.
- **Trakt Account**: Used by our Metadata Battery to retrieve all needed Metadata.
- **Debrid Provider**: A Real-Debrid API key.
- **Method to Mount Media from Debrid Provider**: While we don't require Zurg, we highly recommend this as a very effective way to locally mount your Debrid Provider's content locally for Plex to see.

## Other Settings

- **TMDB API Key**: Used to retrieve Home Screen posters.

## Getting Started

### Prerequisites

- Docker and Docker Compose installed on your system
- A Plex server
- An Overseerr instance
- A Real-Debrid or account

### Setup Instructions

1. Create a directory for cli_debrid:
   ```
   mkdir -p ${HOME}/cli_debrid
   ```

2. Download the `docker-compose.yml` file from the repository:
   ```
   cd ${HOME}/cli_debrid
   curl -O https://raw.githubusercontent.com/godver3/cli_debrid/main/docker-compose.yml
   ```

3. Edit the `docker-compose.yml` file to match your local folder structure.
  
4. Start the container:
   ```
   cd ${HOME}/cli_debrid
   docker-compose up -d
   ```

5. Access the web interface:
   Open a web browser and navigate to `http://your-server-ip:5000`

### Other Notes

cli_debrid is built for both AMD64 and ARM64 using tags:

- dev:
  - godver3/cli_debrid:dev-arm64 (arm64)
  - godver3/cli_debrid:dev (amd64)
- stable:
  - godver3/cli_debrid:main-arm64 (arm64)
  - godver3/cli_debrid:main (amd64)

latest can also be used which is pinned to the newest dev build. Alternatively cli_debrid is built for Windows as a frozen Python application

### Post-Setup

- Monitor the logs at `/host/location/logs` or wherever you have configured for log storage
- Check the content of your queues in the webUI
- Adjust settings as needed to scrape for exactly the results you want

### Updating

To update to the latest version of cli_debrid:

```
cd ${HOME}/cli_debrid
docker-compose pull
docker-compose up -d
```

This will pull the latest image and restart the container with the updated version.

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
- The original creator of plex_debrid
- Helpful communities of content creators

## Caveat

I'll include a caveat that this project was built almost entirely using AI (though I have a bit of experience working with code in the past). I would say I learned a fair bit through the process and overall enjoyed getting to this point. That said, I'll do what I can to fix things, but cli_debrid is built almost entirely on spaghetti and probably has lots of brow-raising content. Apologies in advance real devs who decide to look under the hood.
