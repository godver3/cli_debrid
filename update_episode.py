#!/usr/bin/env python3

# Script to manually update episode 4 of "Stick" (tt31710249) and set updated_at to yesterday

import sys
import os
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add the CLI battery path to import the models
sys.path.insert(0, '/root/dev_cli_debrid')

from cli_battery.app.database import Item, Episode, Season, Metadata, init_db, get_timezone_aware_now
from cli_battery.app.direct_api import DirectAPI

def update_episode_data():
    """Update episode 4 of tt31710249 to have first_aired as June 9, 2025 and set item updated_at to yesterday"""
    
    # Initialize database
    engine = init_db()
    if not engine:
        print("ERROR: Failed to initialize database")
        return False
    
    Session = sessionmaker(bind=engine)
    
    with Session() as session:
        try:
            # Find the item
            item = session.query(Item).filter_by(imdb_id='tt31710249').first()
            if not item:
                print("ERROR: Item tt31710249 not found")
                return False
            
            print(f"Found item: {item.title} (ID: {item.id})")
            
            # Find season 1
            season = session.query(Season).filter_by(item_id=item.id, season_number=1).first()
            if not season:
                print("ERROR: Season 1 not found")
                return False
            
            print(f"Found season 1 (ID: {season.id})")
            
            # Find episode 4
            episode = session.query(Episode).filter_by(season_id=season.id, episode_number=4).first()
            if not episode:
                print("ERROR: Episode 4 not found")
                return False
            
            print(f"Found episode 4: {episode.title}")
            print(f"Current first_aired: {episode.first_aired}")
            
            # Update episode 4's first_aired to June 9, 2025 (keeping the same time)
            new_first_aired = datetime(2025, 6, 9, 2, 0, 0, tzinfo=timezone.utc)
            episode.first_aired = new_first_aired
            
            print(f"Updated first_aired to: {episode.first_aired}")
            
            # Set item's updated_at to yesterday to force staleness
            yesterday = datetime.now(get_timezone_aware_now().tzinfo) - timedelta(days=15)
            item.updated_at = yesterday
            
            print(f"Set item updated_at to: {item.updated_at}")
            
            # Also check and update the seasons metadata JSON if it exists
            seasons_metadata = session.query(Metadata).filter_by(
                item_id=item.id, 
                key='seasons'
            ).first()
            
            if seasons_metadata:
                try:
                    seasons_data = json.loads(seasons_metadata.value)
                    if '1' in seasons_data and 'episodes' in seasons_data['1'] and '4' in seasons_data['1']['episodes']:
                        # Update the JSON metadata too
                        seasons_data['1']['episodes']['4']['first_aired'] = '2025-06-09T02:00:00'
                        seasons_metadata.value = json.dumps(seasons_data)
                        seasons_metadata.last_updated = yesterday
                        print("Also updated seasons metadata JSON")
                    else:
                        print("Episode 4 not found in seasons metadata JSON")
                except json.JSONDecodeError:
                    print("WARNING: Could not parse seasons metadata JSON")
            else:
                print("No seasons metadata found")
            
            # Commit changes
            session.commit()
            print("SUCCESS: Changes committed to database")
            return True
            
        except Exception as e:
            session.rollback()
            print(f"ERROR: {e}")
            return False

def verify_changes():
    """Verify the changes were applied"""
    print("\n=== Verifying Changes ===")
    
    try:
        metadata_result, source = DirectAPI.get_show_metadata("tt31710249")
        if metadata_result:
            episode_4 = metadata_result.get('seasons', {}).get(1, {}).get('episodes', {}).get(4, {})
            if episode_4:
                print(f"Episode 4 first_aired: {episode_4.get('first_aired')}")
                print(f"Data source: {source}")
                
                if '2025-06-09' in str(episode_4.get('first_aired', '')):
                    print("✓ Episode 4 first_aired successfully updated to June 9")
                else:
                    print("✗ Episode 4 first_aired not updated correctly")
            else:
                print("✗ Episode 4 not found in returned data")
        else:
            print("✗ Failed to retrieve show metadata")
    except Exception as e:
        print(f"ERROR during verification: {e}")

if __name__ == "__main__":
    print("=== Updating Episode Data ===")
    
    success = update_episode_data()
    if success:
        verify_changes()
    else:
        print("Failed to update episode data") 