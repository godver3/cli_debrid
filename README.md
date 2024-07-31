# cli_debrid

cli_debrid is a successor to, and pays homage to plex_debrid. cli_debrid is designed to automatically manage and upgrade your media collection, leveraging various sources and services to ensure you always have the best quality content available.

## Key Features

- **Automated Media Management**: Continuously scans for new content and upgrades existing media.
- **Multiple Content Sources**: Integrates with Plex (required) for collection management, Overseerr (required) for content requests, and MDBList (optional) for additional content discovery.
- **Intelligent Scraping**: Uses multiple scrapers to find the best quality content available.
- **Real-Debrid Integration**: Uses Real-Debrid for cached content.
- **Upgrading Functionality**: Automatically seeks and applies upgrades for newly added content.
- **Web Interface**: Provides a user-friendly web interface for monitoring.

## Main Functions

### Run Program

The core functionality of the software. When started, it:

1. Scans your Plex library for existing content.
2. Checks Overseerr for requested content.
3. Scrapes various sources for the best quality versions of wanted content.
4. Manages downloads through Real-Debrid.
5. Seeks upgrades for your media.

### Settings

A comprehensive settings menu allows you to configure:

- Required settings (Plex, Overseerr, Real-Debrid)
- Additional settings (Optional scrapers, MDBList, etc.)
- Scraping settings (Quality preferences, filters)
- Debug settings

### Manual/Testing Scraper

Allows you to manually initiate scraping for specific content, useful for testing and troubleshooting. The Testing Scraper allows you to fine tune your scraping settings and weights to ensure your preferred releases are grabbed.

### Debug Functions

Provides various debugging tools and logs to help diagnose issues and monitor the software's performance.

## Detailed Queue Operations
<details>
<summary>Queue Processing Intervals</summary>
CLI Debrid processes different queues at various intervals to optimize performance and resource usage. Here are the default processing intervals for each queue:

Wanted Queue: Every 5 seconds
Scraping Queue: Every 5 seconds
Adding Queue: Every 5 seconds
Checking Queue: Every 5 minutes (300 seconds)
Sleeping Queue: Every 15 minutes (900 seconds)
Upgrading Queue: Every 5 minutes (300 seconds)

## Philosophy

### Database and "I Know What I Got"

cli_debrid maintains a local database of your media collection, keeping track of what you have and what quality it's in. This "I Know What I Got" approach allows cli_debrid to maintain a list of what you have, and what you want.

### Upgrading Functionality

cli_debrid will automatically search for and apply upgrades to newly added content, ensuring you always have the best quality available. Upgrading is applied to content with a release date of less than a week ago (or in other words, cli_debrid does not try to upgrade old content.

## Required Components

- **Plex**: Used as the primary source of information about your current media collection.
- **Overseerr**: Used to manage and track content requests, and to provide full metadata for content.

## Optional Content Sources/Other Settings

- **MDBList**: Can be used as an additional source for content discovery. Add URLs separated by commas.
- **TMDB API**: Can be used for detailed episode content like runtimes for enhanced bitrate estimation.
- **Collected**: Can be used as an additional source. Essentially this is a way to take your current library and flag all items for metadata processing. If you have a season of a show, this will then mark any other seasons/episodes as wanted.

## Getting Started

### Prerequisites

- Docker and Docker Compose installed on your system
- A Plex server
- An Overseerr instance
- A Real-Debrid account

### Setup Instructions

1. Create a directory for cli_debrid:
   ```
   mkdir -p ${HOME}/cli_debrid
   ```

2. Create a `docker-compose.yml` file in the cli_debrid directory with the following content:
   ```yaml
   services:
     cli_debrid:
       image: godver3/cli_debrid:latest
       pull_policy: always
       container_name: cli_debrid
       ports:
         - "5000:5000"
       volumes:
         - ${HOME}/cli_debrid/db_content:/app/db_content
         - ${HOME}/cli_debrid/config.ini:/app/config.ini
         - ${HOME}/cli_debrid/logs:/app/logs
       environment:
         - TZ=America/Edmonton
       restart: unless-stopped
       tty: true
       stdin_open: true
   ```

3. Start the container:
   ```
   cd ${HOME}/cli_debrid
   docker-compose up -d
   ```

4. Access the web interface (note - only accessible when the program has been started):
   Open a web browser and navigate to `http://your-server-ip:5000`

5. Configure additional settings:
   Edit the `config.ini` file to configure additional settings such as scrapers, MDBList integration, and scraping preferences.

### Post-Setup

- Monitor the logs at `${HOME}/cli_debrid/logs` to ensure everything is running smoothly.
- Use the web interface to monitor queue status and statistics.
- Adjust settings as needed to optimize performance and results for your specific setup.

### Updating

To update to the latest version of cli_debrid:

```
cd ${HOME}/cli_debrid
docker-compose pull
docker-compose up -d
```

This will pull the latest image and restart the container with the updated version.

## Contributing

Please contribute through either Issues or by submitting code.

## License

cli_debrid will always be free for anyone to use.

## Caveat

I'll include a caveat that this project was built almost entirely using AI (though I have a bit of experience working with code in the past). I would say I learned a fair bit through the process and overall enjoyed getting to this point. That said, I'll do what I can to fix things, but cli_debrid is built almost entirely on spaghetti and probably has lots of brow-raising content. Apologies in advance real devs who decide to look under the hood.

## To Do

- Add uncached management options
- Allow settings to be edited through the webUI
- Allow program control through the webUI
