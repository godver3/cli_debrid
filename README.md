# cli_debrid

cli_debrid is a successor to, and pays homage to plex_debrid. cli_debrid is designed to automatically manage and upgrade your media collection, leveraging various sources and services to ensure you always have the best quality content available.

## Key Features

- **Automated Media Management**: Continuously scans for new content and upgrades existing media.
- **Multiple Content Sources**: Integrates with Plex (required) for collection management, Overseerr (required) for content requests, and MDBList (optional) for additional content discovery.
- **Intelligent Scraping**: Uses multiple scrapers to find the best quality content available.
- **Real-Debrid/Torbox Integration**: Uses Real-Debrid or Torbox for cached content. Uncached content handling for Real-Debrid.
- **Upgrading Functionality**: Automatically seeks and applies upgrades for newly added content.
- **Web Interface**: Provides a user-friendly web interface for monitoring.
- **Metadata Battery**: Metadata is stored locally in a battery to avoid over-usage of APIs.

## Main Functions

### Run Program

The core functionality of the software. When started, it:

1. Scans your Plex library for existing content.
2. Checks content sources for any wanted content that isn't already collected.
3. Scrapes various sources for the best quality versions of wanted content.
4. Manages downloads through your Debrid provider.
5. Seeks upgrades for your media if available.

### Settings

A settings menu allows you to configure:

- Required settings (Plex, Debrid Provider, Trakt)
- Scrapers (Zilean, Jackett, Torrentio, Nyaa)
- Scraping settings (Quality preferences, filters)
- Content sources (MDBList, Collected content, Trakt watchlists/lists, Overseerr)
- Additional settings (UI settings, TMDB key, Metadata age threshold, deletions syncing, queue management)
- Advanced settings
- Notifications (Discord, Email, Telegram, NTFY)
- Reverse Parser (used to assign versions to existing content through regex terms)

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

Blacklisted items are no longer processed by the queue system. Blacklisted items are woken per your Blacklist Duration.
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

</details>

## Philosophy

### Database and "I Know What I Got"

cli_debrid maintains a local database of your media collection, keeping track of what you have and what quality it's in. This "I Know What I Got" approach allows cli_debrid to maintain a list of what you have, and what you want.

### Upgrading Functionality

cli_debrid will automatically search for and apply upgrades to newly added content, ensuring you always have the best quality available. 

## Required Components

- **Plex**: Used as the primary source of information about your current media collection.
- **Overseerr**: Used to manage and track content requests, and to provide full metadata for content.
- **Trakt Account**: Used by our Metadata Battery to retrieve all needed Metadata.
- **Debrid Provider**: Either a Real-Debrid or Torbox API key.
- **Method to Mount Media from Debrid Provider**: While we don't require Zurg, we highly recommend this as a very effective way to locally mount your Debrid Provider's content locally for Plex to see.

## Optional Content Sources/Other Settings

- **TMDB API Key**: Can be used for detailed episode content like runtimes for enhanced bitrate estimation, as well as to retrieve Home screen posters
- **Collected**: Can be used as an additional source. Essentially this is a way to take your current library and flag all items for metadata processing. If you have a season of a show, this will then mark any other seasons/episodes as wanted.

## Getting Started

### Prerequisites

- Docker and Docker Compose installed on your system
- A Plex server
- An Overseerr instance
- A Real-Debrid or Torbox account

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

5. Connect to the container to view logs (or view through Portainer/your log viewer of choice):

   ```
   docker attach cli_debrid
   ```

5. Access the web interface:
   Open a web browser and navigate to `http://your-server-ip:5000`

### Other Notes

cli_debrid is built for both AMD64 and ARM64 using tags:

godver3/dev-arm64:latest (arm64)
godver3/dev:latest (amd64)

Alternatively cli_debrid is built for Windows.

### Post-Setup

- Monitor the logs at `${HOME}/cli_debrid/user/logs`
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

Submit issues through GitHub issues. Try to include relevant logging, or at minimum error Tracebacks where possible.

## Contributing

Please contribute through either Issues or by submitting code.

## License

cli_debrid will always be free for anyone to use. 

## Acknowledgements

Thanks to:

- Various other projects that have come before this one, and likely do things better in many ways
- The original creator of plex_debrid
- Helpful communities of content creators

## Caveat

I'll include a caveat that this project was built almost entirely using AI (though I have a bit of experience working with code in the past). I would say I learned a fair bit through the process and overall enjoyed getting to this point. That said, I'll do what I can to fix things, but cli_debrid is built almost entirely on spaghetti and probably has lots of brow-raising content. Apologies in advance real devs who decide to look under the hood.
