from flask import Blueprint, render_template, send_file, abort, Response, request, jsonify
from database.database_reading import get_all_videos, get_video_by_id
import os
import mimetypes
import re
import subprocess
import threading
import logging
import sys
import json
import tempfile
import atexit
from werkzeug.wsgi import FileWrapper
import time
from threading import Lock
import errno
from threading import Lock
from collections import defaultdict

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Create blueprint with the correct name
video_routes = Blueprint('video', __name__)

@video_routes.before_request
def log_request():
    """Log all requests to this blueprint"""
    logger.debug(f"Received request: {request.method} {request.path}")

# Store FFmpeg processes and video info cache
ffmpeg_processes = {}
video_info_cache = {}
process_lock = Lock()
cache_lock = Lock()

def get_video_info(file_path):
    """Get video duration and bitrate information using ffprobe"""
    try:
        # Check cache first
        with cache_lock:
            if file_path in video_info_cache:
                return video_info_cache[file_path]

        logger.info(f"Getting video info for: {file_path}")
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'format=duration,size,bit_rate:stream=width,height,codec_name',
            '-sexagesimal',  # Use HH:MM:SS.ms format for duration
            '-of', 'json',
            file_path
        ]
        
        logger.info(f"Running ffprobe command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0:
            logger.error(f"ffprobe error: {result.stderr}")
            return None
            
        logger.info(f"ffprobe output: {result.stdout}")
        probe_data = json.loads(result.stdout)
        
        # Convert duration to seconds if it's in HH:MM:SS.ms format
        duration_str = probe_data['format']['duration']
        try:
            if ':' in duration_str:
                # Parse HH:MM:SS.ms format
                h, m, s = duration_str.split(':')
                duration = float(h) * 3600 + float(m) * 60 + float(s)
            else:
                duration = float(duration_str)
        except (ValueError, TypeError):
            logger.error(f"Failed to parse duration: {duration_str}")
            duration = 0
        
        video_info = {
            'duration': duration,
            'size': int(probe_data['format']['size']),
            'bit_rate': int(probe_data['format']['bit_rate']),
            'width': int(probe_data['streams'][0]['width']),
            'height': int(probe_data['streams'][0]['height']),
            'codec_name': probe_data['streams'][0]['codec_name']
        }

        logger.info(f"Parsed video info: {video_info}")

        # Cache the result
        with cache_lock:
            video_info_cache[file_path] = video_info

        return video_info
    except Exception as e:
        logger.error(f"Error getting video info: {str(e)}")
        return None

def check_vaapi_support():
    """Check if VAAPI hardware acceleration is available"""
    try:
        if not os.path.exists('/dev/dri/renderD128'):
            return False
            
        test_cmd = [
            'ffmpeg',
            '-hwaccel', 'vaapi',
            '-hwaccel_device', '/dev/dri/renderD128',
            '-v', 'error',
            '-f', 'lavfi',
            '-i', 'nullsrc=s=1280x720:d=1',
            '-vf', 'format=nv12|vaapi,hwupload',
            '-c:v', 'h264_vaapi',
            '-f', 'null',
            '-'
        ]
        
        result = subprocess.run(test_cmd, capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False

@video_routes.route('/browse')
@video_routes.route('/browse/<media_type>')
@video_routes.route('/browse/<media_type>/<letter>')
def browse_videos(media_type='movies', letter=None):
    """
    Get all videos grouped by title with their versions.
    For TV shows, group by show name/year and then by season.
    """
    videos = get_all_videos()
    
    grouped_videos = {}
    if media_type == 'movies':
        # Process movies
        for video in videos['movies']:
            title = video['title']
            year = video['year']
            display_title = f"{title} ({year})" if year else title
            
            version_info = {
                'id': video['id'],
                'filled_by_file': video['filled_by_file'],
                'location_on_disk': video['location_on_disk'],
                'version': video['version']
            }
            
            if display_title not in grouped_videos:
                grouped_videos[display_title] = []
            grouped_videos[display_title].append(version_info)
    elif media_type == 'tv':
        # First pass: find earliest year for each show
        show_years = {}
        for episode in videos['episodes']:
            title = episode['title']
            year = episode['year'] if episode['year'] else 9999  # Use 9999 for unknown years
            
            if title not in show_years:
                show_years[title] = year
            else:
                show_years[title] = min(show_years[title], year)
        
        # Process TV shows using earliest year
        for episode in videos['episodes']:
            title = episode['title']
            year = show_years[title]
            season = episode['season_number']
            episode_num = episode['episode_number']
            episode_title = episode['episode_title']
            
            show_title = f"{title} ({year})" if year and year != 9999 else title
            
            if show_title not in grouped_videos:
                grouped_videos[show_title] = {'seasons': {}}
            
            if season not in grouped_videos[show_title]['seasons']:
                grouped_videos[show_title]['seasons'][season] = []
            
            episode_info = {
                'id': episode['id'],
                'filled_by_file': episode['filled_by_file'],
                'location_on_disk': episode['location_on_disk'],
                'version': episode['version'],
                'episode_number': episode_num,
                'episode_title': episode_title
            }
            
            grouped_videos[show_title]['seasons'][season].append(episode_info)
    
    # Sort the dictionary by keys (titles), ignoring 'The' at the start
    def sort_key(title):
        # Remove 'The ' from the start for sorting purposes
        if title.lower().startswith('the '):
            return title[4:]
        return title
    
    sorted_videos = dict(sorted(grouped_videos.items(), key=lambda x: sort_key(x[0])))
    
    # For TV shows, sort episodes within each season
    if media_type == 'tv':
        for show in sorted_videos.values():
            for season in show['seasons'].values():
                season.sort(key=lambda x: x['episode_number'])
            # Sort seasons by number
            show['seasons'] = dict(sorted(show['seasons'].items()))
    
    # Create alphabet pagination based on the sort key
    available_letters = sorted(set(
        sort_key(title)[0].upper()
        for title in sorted_videos.keys()
        if sort_key(title)[0].isalpha()
    ))
    
    # Check for any titles that start with non-letters (after removing 'The')
    if any(not sort_key(title)[0].isalpha() for title in sorted_videos.keys()):
        available_letters.insert(0, '#')
    
    # If no letter specified and there are videos, default to showing all videos
    if letter is None:
        letter = ''  # Show all videos when no letter is selected
    
    # Filter by letter if specified and not empty
    if letter:
        if letter == '#':
            sorted_videos = {k: v for k, v in sorted_videos.items() 
                           if not sort_key(k)[0].isalpha()}
        else:
            sorted_videos = {k: v for k, v in sorted_videos.items() 
                           if sort_key(k)[0].upper() == letter.upper()}
    
    return render_template(
        'browse.html',
        videos=sorted_videos,
        media_type=media_type,
        current_letter=letter,
        available_letters=available_letters
    )

@video_routes.route('/<int:video_id>')
def play_video(video_id):
    video = get_video_by_id(video_id)
    if not video:
        abort(404)
    return render_template('play.html', video=video)

@video_routes.route('/<int:video_id>/metadata')
def get_video_metadata(video_id):
    """Get video metadata including duration"""
    try:
        video = get_video_by_id(video_id)
        if not video:
            return jsonify({'error': 'Video not found'}), 404

        file_path = video.get('location_on_disk') or video.get('filled_by_file')
        if not file_path or not os.path.exists(file_path):
            return jsonify({'error': 'Video file not found'}), 404

        # Get video info
        video_info = get_video_info(file_path)
        if not video_info:
            return jsonify({'error': 'Could not get video information'}), 500

        return jsonify({
            'duration': video_info['duration'],
            'width': video_info['width'],
            'height': video_info['height'],
            'bitrate': video_info['bit_rate']
        })

    except Exception as e:
        logger.error(f"Error getting video metadata: {str(e)}")
        return jsonify({'error': str(e)}), 500

@video_routes.route('/<int:video_id>/stream')
def stream_video(video_id):
    try:
        video = get_video_by_id(video_id)
        if not video:
            return jsonify({'error': 'Video not found'}), 404

        file_path = video.get('location_on_disk') or video.get('filled_by_file')
        if not file_path or not os.path.exists(file_path):
            return jsonify({'error': 'Video file not found'}), 404

        # Get video info
        video_info = get_video_info(file_path)
        if not video_info:
            return jsonify({'error': 'Could not get video information'}), 500

        # Use VAAPI if available
        use_vaapi = check_vaapi_support()
        logger.info(f"Using VAAPI hardware acceleration: {use_vaapi}")

        if use_vaapi:
            ffmpeg_cmd = [
                'ffmpeg',
                '-hwaccel', 'vaapi',
                '-hwaccel_device', '/dev/dri/renderD128',
                '-hwaccel_output_format', 'vaapi',
                '-i', file_path,
                '-vf', 'format=nv12|vaapi,hwupload,scale_vaapi=w=1920:h=1080:format=nv12',
                '-c:v', 'h264_vaapi',
                '-b:v', '4M',
                '-maxrate', '6M',
                '-bufsize', '8M',
                '-profile:v', 'main',
                '-level', '4.1',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-ac', '2',
                '-ar', '48000',
                '-movflags', '+faststart+frag_keyframe+empty_moov',
                '-f', 'mp4',
                'pipe:1'
            ]
        else:
            ffmpeg_cmd = [
                'ffmpeg',
                '-i', file_path,
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-crf', '23',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-ac', '2',
                '-ar', '48000',
                '-movflags', '+faststart+frag_keyframe+empty_moov',
                '-f', 'mp4',
                'pipe:1'
            ]

        process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=-1
        )

        def generate():
            try:
                while True:
                    chunk = process.stdout.read(64*1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

        return Response(
            generate(),
            mimetype='video/mp4',
            headers={
                'Accept-Ranges': 'bytes',
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            },
            direct_passthrough=True
        )

    except Exception as e:
        logger.error(f"Error in stream_video: {str(e)}")
        return jsonify({'error': str(e)}), 500
