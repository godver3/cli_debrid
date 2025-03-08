import logging
from typing import Dict, Any, List
from datetime import datetime, date, timedelta
import json

from database import get_all_media_items, get_media_item_by_id
from settings import get_setting
from scraper.scraper import scrape
from not_wanted_magnets import is_magnet_not_wanted, is_url_not_wanted
from wake_count_manager import wake_count_manager
from cli_battery.app.direct_api import DirectAPI

class ScrapingQueue:
    def __init__(self):
        self.items = []

    def update(self):
        self.items = [dict(row) for row in get_all_media_items(state="Scraping")]
        
        # Get the queue sort order setting
        sort_order = get_setting("Queue", "queue_sort_order", "None")
        
        # Apply sorting based on the setting
        if sort_order == "Movies First":
            self.items.sort(key=lambda x: 0 if x['type'] == 'movie' else 1)
        elif sort_order == "Episodes First":
            self.items.sort(key=lambda x: 0 if x['type'] == 'episode' else 1)
        # For "None", we keep the default order

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def reset_not_wanted_check(self, item_id):
        """Reset the disable_not_wanted_check flag after scraping is complete"""
        from database import get_db_connection
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE media_items 
                SET disable_not_wanted_check = FALSE
                WHERE id = ?
            """, (item_id,))
            conn.commit()
        except Exception as e:
            logging.error(f"Error resetting disable_not_wanted_check flag: {str(e)}")
        finally:
            conn.close()

    def process(self, queue_manager):
        processed_count = 0
        had_error = False
        today = date.today()
        
        if self.items:
            item = self.items.pop(0)
            item_identifier = queue_manager.generate_identifier(item)
            try:
                logging.info(f"Starting to process scraping results for {item_identifier}")
                
                # Check release date logic - skip for early release items
                if not item.get('early_release', False):
                    if item['release_date'] == 'Unknown':
                        logging.info(f"Item {item_identifier} has an unknown release date. Moving back to Wanted queue.")
                        queue_manager.move_to_wanted(item, "Scraping")
                        processed_count += 1
                        return True  # Continue processing other items
                else:
                    logging.info(f"Processing early release item {item_identifier} regardless of release date")
                    
                try:
                    # Only check release date for non-early release items
                    if not item.get('early_release', False) and item['release_date'] != 'Unknown':
                        release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()

                        # Check if version requires physical release
                        scraping_versions = get_setting('Scraping', 'versions', {})
                        version_settings = scraping_versions.get(item.get('version', ''), {})
                        require_physical = version_settings.get('require_physical_release', False)
                        physical_release_date = item.get('physical_release_date')
                        
                        # If physical release is required, use that date instead
                        if require_physical and physical_release_date:
                            try:
                                physical_date = datetime.strptime(physical_release_date, '%Y-%m-%d').date()
                                if physical_date > today:
                                    logging.info(f"Item {item_identifier} has a future physical release date ({physical_date}). Moving back to Wanted queue.")
                                    queue_manager.move_to_wanted(item, "Scraping")
                                    processed_count += 1
                                    return True
                            except ValueError:
                                logging.warning(f"Invalid physical release date format for item {item_identifier}: {physical_release_date}")
                        # If physical release is required but no date available, move back to Wanted
                        elif require_physical and not physical_release_date:
                            logging.info(f"Item {item_identifier} requires physical release but no date available. Moving back to Wanted queue.")
                            queue_manager.move_to_wanted(item, "Scraping")
                            processed_count += 1
                            return True
                        # Otherwise check normal release timing with early_release flag
                        elif not item.get('early_release', False) and release_date > today:
                            logging.info(f"Item {item_identifier} has a future release date ({release_date}). Moving back to Wanted queue.")
                            queue_manager.move_to_wanted(item, "Scraping")
                            processed_count += 1
                            return True
                except ValueError:
                    logging.warning(f"Item {item_identifier} has an invalid release date format: {item['release_date']}. Moving back to Wanted queue.")
                    queue_manager.move_to_wanted(item, "Scraping")
                    processed_count += 1
                    return True

                # Multi-pack check logic
                is_multi_pack = False
                if item['type'] == 'episode':
                    logging.info(f"Checking multi-pack eligibility for {item['title']} S{item['season_number']:02d}E{item['episode_number']:02d}")
                    
                    show_metadata, _ = DirectAPI.get_show_metadata(item['imdb_id'])
                    
                    if show_metadata and 'seasons' in show_metadata:
                        season_str = str(item['season_number'])
                        if season_str in show_metadata['seasons']:
                            season_data = show_metadata['seasons'][season_str]
                            if 'episodes' in season_data:
                                # Check if this is the season finale
                                total_episodes = len(season_data['episodes'])
                                is_finale = item['episode_number'] == total_episodes
                                if is_finale:
                                    logging.info(f"Episode {item['episode_number']} is the season finale - skipping multi-pack search")
                                    is_multi_pack = False
                                else:
                                    # First pass - log all episode dates
                                    logging.info(f"Checking air dates for {total_episodes} episodes in season {season_str}:")
                                    sorted_episodes = sorted(season_data['episodes'].items(), key=lambda x: int(x[0]))
                                    for ep_num, ep_data in sorted_episodes:
                                        first_aired = ep_data.get('first_aired', 'unknown')
                                        if first_aired and first_aired != 'unknown':
                                            try:
                                                # Parse ISO 8601 datetime and convert to date
                                                air_date = datetime.strptime(first_aired.split('T')[0], '%Y-%m-%d').date()
                                                status = "future" if air_date > today else "aired"
                                                logging.info(f"  Episode {ep_num}: {air_date} ({status})")
                                            except (ValueError, TypeError):
                                                logging.info(f"  Episode {ep_num}: {first_aired} (invalid format)")
                                        else:
                                            logging.info(f"  Episode {ep_num}: unknown air date")

                                    # Check for any unaired episodes
                                    has_unaired_episodes = False
                                    for ep_data in season_data['episodes'].values():
                                        if 'first_aired' not in ep_data or not ep_data['first_aired']:
                                            logging.info(f"Episode {ep_data.get('episode_number', 'unknown')} has unknown air date")
                                            has_unaired_episodes = True
                                            break
                                        try:
                                            air_date = datetime.strptime(ep_data['first_aired'].split('T')[0], '%Y-%m-%d').date()
                                            if air_date > today:
                                                has_unaired_episodes = True
                                                logging.info(f"Episode {ep_data.get('episode_number', 'unknown')} hasn't aired yet (releases {air_date})")
                                                break
                                        except (ValueError, TypeError):
                                            logging.info(f"Episode {ep_data.get('episode_number', 'unknown')} has invalid air date format")
                                            has_unaired_episodes = True
                                            break

                                    # Enable multi-pack only if all episodes have aired
                                    if not has_unaired_episodes:
                                        is_multi_pack = True
                                        logging.info("All episodes have aired - enabling multi-pack")
                                    else:
                                        logging.info("Some episodes haven't aired yet - skipping multi-pack")
                            else:
                                logging.info("No episodes data found in season metadata")
                        else:
                            logging.info(f"Season {season_str} not found in show metadata")
                    else:
                        logging.info("No seasons data found in show metadata")

                logging.info(f"Scraping for {item_identifier}")
                results, filtered_out_results = self.scrape_with_fallback(item, is_multi_pack, queue_manager)
                
                # Ensure both results and filtered_out_results are lists
                results = results if results is not None else []
                filtered_out_results = filtered_out_results if filtered_out_results is not None else []
                
                if not results:
                    logging.warning(f"No results found for {item_identifier} after fallback.")
                    self.handle_no_results(item, queue_manager)
                    processed_count += 1
                    return True

                # Filter and process results
                filtered_results = []
                for result in results:
                    if not item.get('disable_not_wanted_check'):
                        if is_magnet_not_wanted(result['magnet']):
                            continue
                        if is_url_not_wanted(result['magnet']):
                            continue
                    filtered_results.append(result)
                
                if not filtered_results:
                    logging.warning(f"All results filtered out for {item_identifier}. Retrying individual scraping.")
                    individual_results, individual_filtered_out = self.scrape_with_fallback(item, False, queue_manager)
                    logging.info(f"Individual scraping returned {len(individual_results)} results")
                    
                    filtered_individual_results = []
                    for result in individual_results:
                        if not item.get('disable_not_wanted_check'):
                            if is_magnet_not_wanted(result['magnet']):
                                continue
                            if is_url_not_wanted(result['magnet']):
                                continue
                        filtered_individual_results.append(result)
                    
                    if not filtered_individual_results and item['type'] == 'episode':
                        # Final fallback - try multi-pack even if not all episodes have aired
                        logging.info(f"No individual episode results, trying final multi-pack fallback for {item_identifier}")
                        fallback_results, fallback_filtered_out = self.scrape_with_fallback(item, True, queue_manager)
                        
                        filtered_fallback_results = []
                        for result in fallback_results:
                            if not item.get('disable_not_wanted_check'):
                                if is_magnet_not_wanted(result['magnet']):
                                    continue
                                if is_url_not_wanted(result['magnet']):
                                    continue
                            filtered_fallback_results.append(result)
                        
                        if filtered_fallback_results:
                            logging.info(f"Found {len(filtered_fallback_results)} results in multi-pack fallback")
                            filtered_individual_results = filtered_fallback_results
                    
                    if not filtered_individual_results:
                        logging.warning(f"No valid results after individual scraping for {item_identifier}. Moving to Sleeping.")
                        queue_manager.move_to_sleeping(item, "Scraping")
                        self.reset_not_wanted_check(item['id'])
                        processed_count += 1
                        return True
                    filtered_results = filtered_individual_results

                if filtered_results:
                    best_result = filtered_results[0]
                    logging.info(f"Best result for {item_identifier}: {best_result['title']}")
                    
                    if get_setting("Debug", "enable_reverse_order_scraping", default=False):
                        filtered_results.reverse()
                    
                    logging.info(f"Moving {item_identifier} to Adding queue with {len(filtered_results)} results")
                    try:
                        queue_manager.move_to_adding(item, "Scraping", best_result['title'], filtered_results)
                        self.reset_not_wanted_check(item['id'])
                    except Exception as e:
                        logging.error(f"Failed to move {item_identifier} to Adding queue: {str(e)}", exc_info=True)
                        had_error = True
                else:
                    logging.info(f"No valid results for {item_identifier}, moving to Sleeping")
                    queue_manager.move_to_sleeping(item, "Scraping")
                    self.reset_not_wanted_check(item['id'])
                
                processed_count += 1
                
            except Exception as e:
                logging.error(f"Error processing item {item_identifier}: {str(e)}", exc_info=True)
                queue_manager.move_to_sleeping(item, "Scraping")
                had_error = True
                processed_count += 1

        # Return True if there are more items to process or if we processed something
        return len(self.items) > 0 or processed_count > 0

    def scrape_with_fallback(self, item, is_multi_pack, queue_manager, skip_filter=False):
        item_identifier = queue_manager.generate_identifier(item)

        # Add check for fall_back_to_single_scraper flag
        if get_media_item_by_id(item['id']).get('fall_back_to_single_scraper'):
            is_multi_pack = False

        results, filtered_out = scrape(
            item['imdb_id'],
            item['tmdb_id'],
            item['title'],
            item['year'],
            item['type'],
            item['version'],
            item.get('season_number'),
            item.get('episode_number'),
            is_multi_pack,  # This will now be False if fall_back_to_single_scraper is True
            item.get('genres')
        )

        # Ensure results and filtered_out are lists
        results = results if results is not None else []
        filtered_out = filtered_out if filtered_out is not None else []

        if not skip_filter and not item.get('disable_not_wanted_check'):
            # Filter out unwanted magnets and URLs
            results = [r for r in results if not (is_magnet_not_wanted(r['magnet']) or is_url_not_wanted(r['magnet']))]

        is_anime = True if item.get('genres') and 'anime' in item['genres'] else False
        
        # For episodes, filter by exact season/episode match
        if not is_anime:
            season = None
            episode = None
            if item['type'] == 'episode' and not is_multi_pack:
                season = item.get('season_number')
                episode = item.get('episode_number')
            results = [
                r for r in results 
                if (season is None or r.get('parsed_info', {}).get('season_episode_info', {}).get('seasons', []) == [season])
                and (episode is None or r.get('parsed_info', {}).get('season_episode_info', {}).get('episodes', []) == [episode])
            ]

        if results or item['type'] != 'episode':
            return results, filtered_out

        logging.info(f"No results for multi-pack {item_identifier}. Falling back to individual episode scraping.")

        individual_results, individual_filtered_out = scrape(
            item['imdb_id'],
            item['tmdb_id'],
            item['title'],
            item['year'],
            item['type'],
            item['version'],
            item.get('season_number'),
            item.get('episode_number'),
            False,
            item.get('genres')
        )

        # Ensure individual results and filtered_out are lists
        individual_results = individual_results if individual_results is not None else []
        individual_filtered_out = individual_filtered_out if individual_filtered_out is not None else []

        # Filter out unwanted magnets and URLs for individual results
        if not skip_filter and not item.get('disable_not_wanted_check'):
            individual_results = [r for r in individual_results if not (is_magnet_not_wanted(r['magnet']) or is_url_not_wanted(r['magnet']))]

        # For episodes, ensure we have the correct season and episode
        if item['type'] == 'episode':
            season = item.get('season_number')
            episode = item.get('episode_number')
            if season is not None and episode is not None:
                # First, mark any results that are date-based
                for result in individual_results:
                    if result.get('parsed_info', {}).get('date'):
                        result['is_date_based'] = True

                # Filter only non-date-based results by season/episode
                date_based_results = [r for r in individual_results if r.get('is_date_based', False)]
                regular_results = [r for r in individual_results if not r.get('is_date_based', False)]
                
                filtered_regular_results = [
                    r for r in regular_results 
                    if r.get('parsed_info', {}).get('season_episode_info', {}).get('seasons', []) == [season]
                    and r.get('parsed_info', {}).get('season_episode_info', {}).get('episodes', []) == [episode]
                ]

                # Combine date-based and filtered regular results
                individual_results = date_based_results + filtered_regular_results

        if individual_results:
            logging.info(f"Found results for individual episode scraping of {item_identifier}.")
        else:
            logging.warning(f"No results found even after individual episode scraping for {item_identifier}.")

        return individual_results, individual_filtered_out
     
    def handle_no_results(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        if self.is_item_old(item):
            if item['type'] == 'episode':
                logging.info(f"No results found for old episode {item_identifier}. Blacklisting item and related season items.")
                queue_manager.queues["Blacklisted"].blacklist_old_season_items(item, queue_manager)
                self.reset_not_wanted_check(item['id'])
            elif item['type'] == 'movie':
                logging.info(f"No results found for old movie {item_identifier}. Blacklisting item.")
                queue_manager.move_to_blacklisted(item, "Scraping")
                self.reset_not_wanted_check(item['id'])
            else:
                logging.warning(f"Unknown item type {item['type']} for {item_identifier}. Blacklisting item.")
                queue_manager.move_to_blacklisted(item, "Scraping")
                self.reset_not_wanted_check(item['id'])
        else:
            logging.warning(f"No results found for {item_identifier}. Moving to Sleeping queue.")
            wake_count = wake_count_manager.get_wake_count(item['id'])
            queue_manager.move_to_sleeping(item, "Scraping")
            self.reset_not_wanted_check(item['id'])
            
    def is_item_old(self, item: Dict[str, Any]) -> bool:
        if 'release_date' not in item or item['release_date'] is None or item['release_date'] == 'Unknown':
            return True
        try:
            release_date = datetime.strptime(item['release_date'], '%Y-%m-%d').date()
            days_since_release = (datetime.now().date() - release_date).days
            
            # Define thresholds for considering items as old
            movie_threshold = 30  # Consider movies old after 30 days
            episode_threshold = 7  # Consider episodes old after 7 days
            
            if item['type'] == 'movie':
                return days_since_release > movie_threshold
            elif item['type'] == 'episode':
                return days_since_release > episode_threshold
            else:
                return True  # Consider unknown item types as old
        except ValueError as e:
            logging.error(f"Error parsing release date for item {self.generate_identifier(item)}: {str(e)}")
            return True  # Consider items with unparseable dates as old
            
    @staticmethod
    def generate_identifier(item: Dict[str, Any]) -> str:
        if item['type'] == 'movie':
            return f"movie_{item['title']}_{item['imdb_id']}_{'_'.join(item['version'].split())}"
        elif item['type'] == 'episode':
            return f"episode_{item['title']}_{item['imdb_id']}_S{item['season_number']:02d}E{item['episode_number']:02d}_{'_'.join(item['version'].split())}"
        else:
            raise ValueError(f"Unknown item type: {item['type']}")