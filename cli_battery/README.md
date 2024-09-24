![Python Tests](https://github.com/godver3/cli_battery/actions/workflows/python-tests.yml/badge.svg)

# CLI Battery

CLI Battery is a Flask-based web application for managing metadata for movies and TV shows. It integrates with Trakt for fetching and updating metadata.

## Features

- Dashboard with statistics about items and metadata
- Debug view for all items in the database
- Metadata management for movies and TV shows
- Integration with Trakt API for fetching metadata
- Provider management (enable/disable metadata providers)
- Settings management
- Poster image retrieval

## API Endpoints

- `/`: Home page with dashboard statistics
- `/debug`: Debug view of all items
- `/metadata`: View all metadata
- `/providers`: Manage metadata providers
- `/settings`: Application settings
- `/api/metadata/<imdb_id>`: Fetch metadata for a specific item
- `/api/seasons/<imdb_id>`: Fetch seasons data for a TV show
- `/authorize_trakt`: Initiate Trakt authorization
- `/trakt_callback`: Handle Trakt authorization callback

## Setup

1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Set up your Trakt API credentials in the settings
4. Run the application: `python app.py`

## Testing

Run the tests using:
```python -m unittest discover tests```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
