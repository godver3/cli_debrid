#!/usr/bin/env python3
import os
import sys
import logging
from pathlib import Path

# Handle both relative and absolute imports
try:
    from .config.downsub_config import config
except ImportError:
    # Add the current directory to the Python path for absolute imports
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config.downsub_config import config

# Import subliminal components
try:
    from subliminal import download_best_subtitles, region
    from subliminal.video import Video
    from babelfish import Language
except ImportError as e:
    logging.error(f"Required subliminal packages not installed: {e}")
    logging.error("Please install: pip install subliminal babelfish")
    sys.exit(1)

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def test_subtitle_scoring():
    """Test subtitle scoring to understand why pt-BR might not be selected"""
    print("=== Testing Subtitle Scoring ===")
    
    video_path = "/media/Movies/How to Train Your Dragon (2025)/How to Train Your Dragon (2025) - tt26743210 - 1080p - (How.to.Train.Your.Dragon.2025.720p.AMZN.WEB-DLRip.H264_il68k).mkv"
    
    if not os.path.exists(video_path):
        print(f"Video file not found: {video_path}")
        return
    
    # Create video object
    video = Video.fromname(Path(video_path).name)
    video.path = video_path
    
    # Test with your current configuration: ['pob', 'eng', 'fra']
    languages = []
    for code in ['pob', 'eng', 'fra']:
        if code == 'pob':
            languages.append(Language('por', 'BR'))
        elif code == 'eng':
            languages.append(Language('eng'))
        elif code == 'fra':
            languages.append(Language('fra'))
    
    print(f"Testing with languages: {languages}")
    
    try:
        # Configure cache
        region.configure('dogpile.cache.memory', replace_existing_backend=True)
        
        # Search for subtitles
        subtitles = download_best_subtitles([video], set(languages), providers={'opensubtitles'})
        
        if subtitles[video]:
            print(f"\n✅ Found {len(subtitles[video])} subtitle(s):")
            for sub in subtitles[video]:
                lang_info = getattr(sub.language, 'ietf', None) or str(sub.language)
                print(f"  - {sub} [{lang_info}]")
                
            # Now let's check what was actually found vs what was selected
            print(f"\n=== Analysis ===")
            print(f"Your configuration: {config.SUBTITLE_LANGUAGES}")
            print(f"Languages requested: {[str(lang) for lang in languages]}")
            print(f"Subtitles downloaded: {[str(sub.language) for sub in subtitles[video]]}")
            
            # Check if pt-BR was found but not selected
            pt_br_found = any(str(sub.language) == 'pt-BR' for sub in subtitles[video])
            print(f"Portuguese Brazilian in results: {pt_br_found}")
            
        else:
            print(f"❌ No subtitles found")
            
    except Exception as e:
        print(f"❌ Error: {e}")

def test_individual_language_search():
    """Test searching for each language individually to see what's available"""
    print("\n=== Testing Individual Language Search ===")
    
    video_path = "/media/Movies/How to Train Your Dragon (2025)/How to Train Your Dragon (2025) - tt26743210 - 1080p - (How.to.Train.Your.Dragon.2025.720p.AMZN.WEB-DLRip.H264_il68k).mkv"
    
    if not os.path.exists(video_path):
        print(f"Video file not found: {video_path}")
        return
    
    # Create video object
    video = Video.fromname(Path(video_path).name)
    video.path = video_path
    
    # Test each language individually
    test_languages = [
        ('pob', Language('por', 'BR')),
        ('eng', Language('eng')),
        ('fra', Language('fra')),
    ]
    
    for lang_code, lang_obj in test_languages:
        print(f"\n--- Testing {lang_code} ({lang_obj}) ---")
        
        try:
            # Configure cache
            region.configure('dogpile.cache.memory', replace_existing_backend=True)
            
            # Search for subtitles
            subtitles = download_best_subtitles([video], {lang_obj}, providers={'opensubtitles'})
            
            if subtitles[video]:
                print(f"  ✅ Found {len(subtitles[video])} subtitle(s):")
                for sub in subtitles[video]:
                    lang_info = getattr(sub.language, 'ietf', None) or str(sub.language)
                    print(f"    - {sub} [{lang_info}]")
            else:
                print(f"  ❌ No subtitles found")
                
        except Exception as e:
            print(f"  ❌ Error: {e}")

if __name__ == "__main__":
    test_subtitle_scoring()
    test_individual_language_search() 