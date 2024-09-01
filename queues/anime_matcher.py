import logging
from guessit import guessit
from typing import List, Dict, Any, Tuple

class AnimeMatcher:
    def __init__(self, calculate_absolute_episode_func):
        self.calculate_absolute_episode = calculate_absolute_episode_func
        logging.info("AnimeMatcher initialized")

    def match_anime_files(self, files: List[str], items: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
        logging.info(f"Matching anime files. Files count: {len(files)}, Items count: {len(items)}")
        file_format = self.detect_anime_file_format(files)
        logging.info(f"Detected file format: {file_format}")
        
        # Log some sample filenames
        sample_files = files[:5] + files[-5:] if len(files) > 10 else files
        logging.debug(f"Sample filenames: {sample_files}")
        
        if file_format == "absolute_episode":
            logging.info("Using absolute episode format")
            return self.match_absolute_episode_format(files, items)
        elif file_format == "season_episode":
            logging.info("Using season-episode format")
            return self.match_season_episode_format(files, items)
        elif file_format == "pseudo_season_episode":
            logging.info("Using pseudo season-episode format (actually absolute)")
            return self.match_pseudo_season_episode_format(files, items)
        else:
            logging.warning(f"Unknown anime file format detected: {file_format}")
            return []

    def detect_anime_file_format(self, files: List[str]) -> str:
        season_episode_count = 0
        absolute_episode_count = 0
        pseudo_season_episode_count = 0
        max_season = 0
        max_episode = 0
        continuous_episode_count = 0

        for file in files:
            guess = guessit(file)
            if 'season' in guess and 'episode' in guess:
                season = guess['season']
                episode = guess['episode']
                max_season = max(max_season, season)
                max_episode = max(max_episode, episode)
                
                if episode == continuous_episode_count + 1:
                    continuous_episode_count += 1
                
                if season > 1 and episode > 50:  # Changed this condition
                    pseudo_season_episode_count += 1
                else:
                    season_episode_count += 1
            elif 'episode' in guess and 'season' not in guess:
                absolute_episode_count += 1

        logging.debug(f"File format detection: Season-Episode count: {season_episode_count}, "
                      f"Absolute Episode count: {absolute_episode_count}, "
                      f"Pseudo Season-Episode count: {pseudo_season_episode_count}, "
                      f"Max Season: {max_season}, Max Episode: {max_episode}, "
                      f"Continuous Episode Count: {continuous_episode_count}")
        
        if pseudo_season_episode_count > 0:  # Changed this condition
            return "pseudo_season_episode"
        elif continuous_episode_count > 0.9 * len(files) and max_season > 1:
            return "pseudo_season_episode"
        elif absolute_episode_count > season_episode_count:
            return "absolute_episode"
        else:
            return "season_episode"

    def match_absolute_episode_format(self, files: List[str], items: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
        logging.info("Matching files using absolute episode format")
        matches = []
        absolute_items = self.convert_items_to_absolute_format(items)

        for file in files:
            guess = guessit(file)
            if 'episode' in guess:
                file_episode = guess['episode']
                logging.debug(f"Trying to match file: {file}, Guessed episode: {file_episode}")
                for item in absolute_items:
                    if item['absolute_episode'] == file_episode:
                        logging.info(f"Match found: File {file} matches item with absolute episode {file_episode}")
                        matches.append((file, item['original_item']))
                        break

        logging.info(f"Absolute episode matching complete. Matches found: {len(matches)}")
        return matches

    def match_season_episode_format(self, files: List[str], items: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
        logging.info("Matching files using season-episode format")
        matches = []

        for file in files:
            guess = guessit(file)
            if 'season' in guess and 'episode' in guess:
                file_season = guess['season']
                file_episode = guess['episode']
                logging.debug(f"Trying to match file: {file}, Guessed season: {file_season}, episode: {file_episode}")
                for item in items:
                    if int(item['season_number']) == file_season and int(item['episode_number']) == file_episode:
                        logging.info(f"Match found: File {file} matches item S{file_season}E{file_episode}")
                        matches.append((file, item))
                        break

        logging.info(f"Season-episode matching complete. Matches found: {len(matches)}")
        return matches

    def match_pseudo_season_episode_format(self, files: List[str], items: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
        logging.info("Matching files using pseudo season-episode format (treating as absolute)")
        matches = []
        absolute_items = self.convert_items_to_absolute_format(items)

        for file in files:
            guess = guessit(file)
            if 'season' in guess and 'episode' in guess:
                file_absolute_episode = (guess['season'] - 1) * 100 + guess['episode']
                logging.debug(f"Trying to match file: {file}, Calculated absolute episode: {file_absolute_episode}")
                for item in absolute_items:
                    if item['absolute_episode'] == file_absolute_episode:
                        logging.info(f"Match found: File {file} matches item with absolute episode {file_absolute_episode}")
                        matches.append((file, item['original_item']))
                        break

        logging.info(f"Pseudo season-episode matching complete. Matches found: {len(matches)}")
        return matches

    def convert_items_to_absolute_format(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        logging.info(f"Converting {len(items)} items to absolute format")
        converted_items = []
        for item in items:
            absolute_episode = self.calculate_absolute_episode(item)
            converted_items.append({
                'absolute_episode': absolute_episode,
                'original_item': item
            })
            logging.debug(f"Converted item: S{item['season_number']}E{item['episode_number']} to absolute episode {absolute_episode}")
        return converted_items
    
