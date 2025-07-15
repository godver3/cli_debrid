#!/usr/bin/env python3
"""
SubSource Subtitle Downloader Library

Fast subtitle downloading using the SubSource API.
Much faster than traditional methods (1-2 seconds vs 25+ seconds).
"""

import os
import re
import time
import logging
import zipfile
import io
import unicodedata
import requests
from thefuzz import process
from pathlib import Path


class SubSourceDownloader:
    def __init__(self, timeout=10):
        self.api_url = "https://api.subsource.net/api/"
        self.headers = {'Content-Type': 'application/json'}
        self.timeout = timeout
        
        # Language code to SubSource language name mapping
        self.language_code_map = {
            'ara': 'Arabic',
            'eng': 'English', 
            'fre': 'French',
            'ger': 'German',
            'spa': 'Spanish',
            'ita': 'Italian',
            'por': 'Portuguese',
            'dut': 'Dutch',
            'rus': 'Russian',
            'chi': 'Chinese BG code',
            'jpn': 'Japanese',
            'kor': 'Korean',
            'en': 'English',
            'ar': 'Arabic',
            'fr': 'French',
            'de': 'German',
            'es': 'Spanish',
            'it': 'Italian',
            'pt': 'Portuguese',
            'nl': 'Dutch',
            'ru': 'Russian',
            'zh': 'Chinese BG code',
            'ja': 'Japanese',
            'ko': 'Korean'
        }
    
    def get_language_name(self, language_input):
        """
        Convert language code to language name if needed.
        If already a language name, return as-is.
        """
        # If it's already a language name that exists in SubSource, return it
        if language_input in ['Arabic', 'English', 'French', 'German', 'Spanish', 'Italian', 
                              'Portuguese', 'Dutch', 'Russian', 'Chinese BG code', 'Japanese', 
                              'Korean', 'Norwegian', 'Swedish', 'Danish', 'Thai', 'Vietnamese',
                              'Turkish', 'Indonesian', 'Farsi/Persian', 'Big 5 code']:
            return language_input
        
        # Otherwise, try to map from code to name
        return self.language_code_map.get(language_input.lower(), language_input)
        
    def cleanchar(self, text):
        """Clean special characters from text"""
        text = unicodedata.normalize('NFKD', text)
        text = re.sub(u'[\u2013\u2014\u3161\u1173\uFFDA]', '-', text)
        text = re.sub(u'[\u00B7\u2000-\u206F\u22C5\u318D]', '.', text)
        return text
    
    def extract_movie_info(self, filepath):
        """Extract movie information from filepath"""
        filename = os.path.basename(filepath)
        
        # Extract IMDB ID if present
        imdb_match = re.search(r'tt(\d+)', filename)
        imdb_id = f"tt{imdb_match.group(1)}" if imdb_match else None
        
        # Extract year
        year_match = re.search(r'\((\d{4})\)', filename)
        year = int(year_match.group(1)) if year_match else None
        
        # Extract title (everything before the year)
        if year_match:
            title = filename[:year_match.start()].strip()
        else:
            title = filename.split('-')[0].strip()
        
        # Extract resolution
        resolution = None
        for res in ['2160p', '1080p', '720p', '480p']:
            if res in filename:
                resolution = res
                break
        
        return {
            'title': title,
            'year': year,
            'imdb_id': imdb_id,
            'resolution': resolution,
            'filepath': filepath
        }
    
    def search_movie(self, movie_info):
        """Search for movie on SubSource API"""
        # Try searching by IMDB ID first if available, otherwise use title
        search_query = movie_info['imdb_id'] if movie_info['imdb_id'] else movie_info['title']
        
        logging.info(f"Searching SubSource with query: {search_query}")
        
        response = requests.post(f"{self.api_url}searchMovie", 
                               headers=self.headers, 
                               json={'query': search_query}, 
                               timeout=self.timeout)
        
        if response.status_code != 200:
            raise Exception(f"Search failed with status {response.status_code}")
        
        search_data = response.json()
        
        if not search_data.get('found'):
            # If IMDB ID search failed, try searching by title
            if movie_info['imdb_id'] and search_query == movie_info['imdb_id']:
                logging.info("IMDB ID search failed, trying title search...")
                response = requests.post(f"{self.api_url}searchMovie", 
                                       headers=self.headers, 
                                       json={'query': movie_info['title']}, 
                                       timeout=self.timeout)
                
                if response.status_code == 200:
                    search_data = response.json()
                
                if not search_data.get('found'):
                    raise Exception("No results found with title search either")
            else:
                raise Exception("No results found")
        
        return self.find_best_match(search_data['found'], movie_info)
    
    def find_best_match(self, movies, movie_info):
        """Find the best matching movie from search results"""
        # If we searched by IMDB ID, prioritize exact IMDB matches
        if movie_info['imdb_id']:
            for movie in movies:
                if movie.get('imdb') == movie_info['imdb_id']:
                    logging.info(f"✅ Found exact IMDB match: {movie['title']} ({movie['releaseYear']})")
                    return movie
        
        # Try to match by title and year
        for movie in movies:
            if (movie['title'].lower() == movie_info['title'].lower() and 
                movie.get('releaseYear') == movie_info['year']):
                logging.info(f"✅ Found title+year match: {movie['title']} ({movie['releaseYear']})")
                return movie
        
        # Fall back to fuzzy matching
        titles = {i: movies[i]['title'] for i in range(len(movies))}
        score = process.extractOne(movie_info['title'], titles)
        
        if score[1] >= 90:
            found_movie = movies[score[2]]
            logging.info(f"📋 Using fuzzy match: {found_movie['title']} ({found_movie['releaseYear']})")
            return found_movie
        
        raise Exception("No good title match found")
    
    def get_subtitles(self, movie, language='English'):
        """Get subtitles for a movie"""
        # Handle movies vs TV series differently
        if movie['type'] == 'Movie':
            movie_params = {'movieName': movie['linkName']}
        else:
            # For TV series, we'd need to handle seasons
            # For now, default to season 1
            movie_params = {
                'movieName': movie['linkName'],
                'season': 'season-1'
            }
        
        response = requests.post(f"{self.api_url}getMovie", 
                               headers=self.headers, 
                               json=movie_params, 
                               timeout=self.timeout)
        
        if response.status_code != 200:
            raise Exception(f"Failed to get movie data: {response.status_code}")
        
        movie_data = response.json()
        all_subs = movie_data.get('subs', [])
        
        # Filter for desired language
        language_subs = [sub for sub in all_subs if sub.get('lang', '').lower() == language.lower()]
        
        if not language_subs:
            available_languages = list(set([sub.get('lang', 'Unknown') for sub in all_subs if sub.get('lang')]))
            raise Exception(f"No {language} subtitles found. Available: {available_languages}")
        
        logging.info(f"Found {len(language_subs)} {language} subtitles")
        return language_subs
    
    def download_subtitle(self, subtitle, output_path):
        """Download a specific subtitle"""
        # Get download token
        download_params = {
            'movie': subtitle['linkName'],
            'lang': subtitle['lang'],
            'id': subtitle['subId']
        }
        
        token_response = requests.post(f"{self.api_url}getSub", 
                                     headers=self.headers, 
                                     json=download_params, 
                                     timeout=self.timeout)
        
        if token_response.status_code != 200:
            raise Exception(f"Failed to get download token: {token_response.status_code}")
        
        token_data = token_response.json()
        download_token = token_data['sub']['downloadToken']
        
        # Download the subtitle file
        download_response = requests.get(f"{self.api_url}downloadSub/{download_token}", 
                                       headers=self.headers, 
                                       timeout=self.timeout)
        
        if download_response.status_code != 200:
            raise Exception(f"Failed to download subtitle: {download_response.status_code}")
        
        # Handle ZIP file or direct content
        try:
            with zipfile.ZipFile(io.BytesIO(download_response.content)) as z:
                for info_file in z.infolist():
                    if info_file.filename.endswith(('.srt', '.sub', '.ass', '.ssa')):
                        subtitle_content = z.read(info_file).decode('utf-8', errors='ignore')
                        break
                else:
                    raise Exception("No subtitle file found in ZIP")
        except zipfile.BadZipFile:
            # If it's not a ZIP file, use content directly
            subtitle_content = download_response.text
        
        # Save subtitle
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(subtitle_content)
        
        return True
    
    def download_for_video(self, video_path, language='English', language_code='en'):
        """
        Download subtitle for a video file
        
        Args:
            video_path (str): Path to the video file
            language (str): Language name or code (e.g., 'English' or 'eng')
            language_code (str): Language code for filename (e.g., 'en')
            
        Returns:
            tuple: (success: bool, subtitle_path: str, duration: float)
        """
        start_time = time.time()
        
        try:
            # Convert language code to language name if needed
            language_name = self.get_language_name(language)
            
            # Extract movie information
            movie_info = self.extract_movie_info(video_path)
            logging.info(f"Movie info: {movie_info['title']} ({movie_info['year']}) - {movie_info['imdb_id']}")
            
            # Search for movie
            movie = self.search_movie(movie_info)
            
            # Get subtitles
            subtitles = self.get_subtitles(movie, language_name)
            
            # Use the first available subtitle
            subtitle = subtitles[0]
            
            # Generate output path
            subtitle_path = f"{os.path.splitext(video_path)[0]}.{language_code}.srt"
            
            # Download subtitle
            self.download_subtitle(subtitle, subtitle_path)
            
            end_time = time.time()
            duration = end_time - start_time
            
            logging.info(f"✅ SubSource: Downloaded in {duration:.2f} seconds")
            logging.info(f"   Subtitle saved: {subtitle_path}")
            logging.info(f"   Release: {self.cleanchar(subtitle['releaseName'])}")
            
            return True, subtitle_path, duration
            
        except Exception as e:
            end_time = time.time()
            duration = end_time - start_time
            logging.error(f"❌ SubSource failed: {e}")
            return False, None, duration


def download_subtitle(video_path, language='English', language_code='en'):
    """
    Convenience function to download subtitle for a video file
    
    Args:
        video_path (str): Path to the video file
        language (str): Language name or code (e.g., 'English' or 'eng')
        language_code (str): Language code for filename (e.g., 'en')
        
    Returns:
        tuple: (success: bool, subtitle_path: str, duration: float)
    """
    downloader = SubSourceDownloader()
    return downloader.download_for_video(video_path, language, language_code)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python subsource_downloader.py <video_file> [language] [language_code]")
        print("Examples:")
        print("  python subsource_downloader.py video.mkv")
        print("  python subsource_downloader.py video.mkv English")  
        print("  python subsource_downloader.py video.mkv Arabic ara")
        sys.exit(1)
    
    video_file = sys.argv[1]
    language = sys.argv[2] if len(sys.argv) > 2 else 'English'
    language_code = sys.argv[3] if len(sys.argv) > 3 else 'en'
    
    # If only language is provided, try to map common languages to codes
    if len(sys.argv) == 3:
        # First check if it's already a language code
        if language in ['ara', 'eng', 'fre', 'ger', 'spa', 'ita', 'por', 'dut', 'rus', 'chi', 'jpn', 'kor',
                       'en', 'ar', 'fr', 'de', 'es', 'it', 'pt', 'nl', 'ru', 'zh', 'ja', 'ko']:
            language_code = language
        else:
            # Otherwise, try to map language names to codes
            language_to_code_map = {
                'English': 'en',
                'Arabic': 'ara', 
                'Spanish': 'spa',
                'French': 'fre',
                'German': 'ger',
                'Italian': 'ita',
                'Portuguese': 'por',
                'Dutch': 'dut',
                'Russian': 'rus',
                'Chinese BG code': 'chi',
                'Korean': 'kor'
            }
            language_code = language_to_code_map.get(language, 'en')
    
    # Set up logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    success, subtitle_path, duration = download_subtitle(video_file, language, language_code)
    
    if success:
        print(f"✅ Success! Subtitle downloaded in {duration:.2f}s: {subtitle_path}")
    else:
        print(f"❌ Failed after {duration:.2f}s")
        sys.exit(1) 