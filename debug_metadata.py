from cli_battery.app.trakt_metadata import TraktMetadata
import logging

logging.basicConfig(level=logging.INFO)

# Initialize Trakt
trakt = TraktMetadata()

# Get fresh metadata
imdb_id = "tt9253284"
metadata = trakt.get_show_metadata(imdb_id)
print(f"\nFetched metadata:")
print(metadata)
