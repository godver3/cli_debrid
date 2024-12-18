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

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Create blueprint with the correct name
video_routes = Blueprint('video', __name__)

# Store FFmpeg processes and video info cache
ffmpeg_processes = {}
video_info_cache = {}
process_lock = Lock()
cache_lock = Lock()

@video_routes.before_request
def log_request():
    """Log all requests to this blueprint"""
    logger.debug(f"Received request: {request.method} {request.path}")

def cleanup_stream(video_id):
    """Clean up resources for a specific video stream"""
    with process_lock:
        if video_id in ffmpeg_processes:
            try:
                process = ffmpeg_processes[video_id]
                logger.info(f"Cleaning up FFmpeg process for video {video_id}")
                
                # First try graceful termination
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    logger.warning(f"Process {video_id} did not terminate gracefully, forcing kill")
                    process.kill()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        logger.error(f"Failed to kill process {video_id}")

                # Clean up pipes
                try:
                    process.stdout.close()
                except:
                    pass
                try:
                    process.stderr.close()
                except:
                    pass

                del ffmpeg_processes[video_id]
                logger.info(f"Successfully cleaned up FFmpeg process for video {video_id}")
            except Exception as e:
                logger.error(f"Error cleaning up FFmpeg process for video {video_id}: {str(e)}")
                # Ensure process is removed from dictionary even if cleanup fails
                ffmpeg_processes.pop(video_id, None)

def cleanup_temp_files():
    """Clean up all temporary files and processes"""
    logger.info("Cleaning up all FFmpeg processes")
    with process_lock:
        for video_id in list(ffmpeg_processes.keys()):
            cleanup_stream(video_id)

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
            '-show_entries', 'format=duration,size,bit_rate:stream=width,height,codec_name,color_space,color_transfer,color_primaries,bits_per_raw_sample',
            '-of', 'json',
            file_path
        ]
        
        logger.info(f"Running ffprobe command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)  # Add timeout
        
        if result.returncode != 0:
            logger.error(f"ffprobe error: {result.stderr}")
            return None
            
        logger.info(f"ffprobe output: {result.stdout}")
        probe_data = json.loads(result.stdout)
        
        format_data = probe_data.get('format', {})
        stream_data = probe_data.get('streams', [{}])[0] if probe_data.get('streams') else {}
        
        # Check if video is HDR
        is_hdr = False
        bits_per_raw_sample = stream_data.get('bits_per_raw_sample', '8')
        try:
            bits_per_raw_sample = int(bits_per_raw_sample) if bits_per_raw_sample.isdigit() else 8
        except (ValueError, AttributeError):
            bits_per_raw_sample = 8
            
        if (stream_data.get('color_transfer', '').lower() in ['smpte2084', 'arib-std-b67'] or
            stream_data.get('color_primaries', '').lower() in ['bt2020'] or
            bits_per_raw_sample > 8):
            is_hdr = True
            logger.info("Detected HDR video")
        else:
            logger.info("Detected SDR video")

        video_info = {
            'duration': float(format_data.get('duration', 0)),
            'size': int(format_data.get('size', 0)),
            'bit_rate': int(format_data.get('bit_rate', 0)),
            'width': int(stream_data.get('width', 0)),
            'height': int(stream_data.get('height', 0)),
            'codec_name': stream_data.get('codec_name', ''),
            'is_hdr': is_hdr
        }

        # Log video info for debugging
        logger.info(f"Parsed video info: {json.dumps(video_info, indent=2)}")

        # Cache the result
        with cache_lock:
            video_info_cache[file_path] = video_info

        return video_info
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout getting video info for: {file_path}")
        return None
    except Exception as e:
        logger.error(f"Error getting video info: {str(e)}")
        return None

def check_vaapi_support():
    """Check if VAAPI hardware acceleration is available"""
    try:
        # Check if the VAAPI device exists
        if not os.path.exists('/dev/dri/renderD128'):
            logger.info("VAAPI device not found")
            return False
            
        # Try running ffmpeg with VAAPI to verify it works
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
        if result.returncode == 0:
            logger.info("VAAPI hardware acceleration is available")
            return True
        else:
            logger.info(f"VAAPI test failed: {result.stderr}")
            return False
    except Exception as e:
        logger.info(f"Error checking VAAPI support: {str(e)}")
        return False

def is_browser_compatible(video_info):
    """Check if video codec is directly compatible with modern browsers"""
    try:
        codec = video_info.get('codec_name', '').lower()
        logger.info(f"Checking browser compatibility - Raw codec info: {video_info.get('codec_name')}")
        
        # Most browsers support H.264/AVC, some newer ones support HEVC/H.265
        browser_compatible_codecs = ['h264', 'avc', 'avc1']
        
        # Check if video codec is supported
        if codec in browser_compatible_codecs:
            # Check if resolution is reasonable (<=1080p)
            width = int(video_info.get('width', 0))
            height = int(video_info.get('height', 0))
            if width <= 1920 and height <= 1080:
                # Check if it's not HDR (which might cause display issues)
                if not video_info.get('is_hdr', False):
                    logger.info(f"Video is browser compatible: codec={codec}, resolution={width}x{height}")
                    return True
                else:
                    logger.info("Video is not browser compatible: HDR content detected")
            else:
                logger.info(f"Video is not browser compatible: resolution {width}x{height} exceeds 1080p")
        else:
            logger.info(f"Video is not browser compatible: unsupported codec '{codec}'")
        
        return False
    except Exception as e:
        logger.error(f"Error checking browser compatibility: {str(e)}")
        return False

@video_routes.before_request
def log_request():
    """Log all requests to this blueprint"""
    logger.debug(f"Received request: {request.method} {request.path}")

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

@video_routes.route('/<int:video_id>/start')
def start_stream(video_id):
    """Initialize video stream and return metadata"""
    try:
        video = get_video_by_id(video_id)
        if not video:
            return jsonify({'error': 'Video not found'}), 404

        file_path = video.get('location_on_disk') or video.get('filled_by_file')
        if not file_path or not os.path.exists(file_path):
            return jsonify({'error': f'Video file not found at path: {file_path}'}), 404

        # Get video information once
        video_info = get_video_info(file_path)
        if not video_info:
            return jsonify({'error': 'Could not get video information'}), 500

        return jsonify({
            'status': 'ready',
            'duration': float(video_info.get('duration', 0)),
            'width': int(video_info.get('width', 0)),
            'height': int(video_info.get('height', 0)),
            'size': int(video_info.get('size', 0)),
            'bitrate': int(video_info.get('bit_rate', 0))
        })

    except Exception as e:
        logger.error(f"Error in start_stream: {str(e)}")
        return jsonify({'error': str(e)}), 500

@video_routes.route('/<int:video_id>/stream')
def stream_video(video_id):
    """Stream video content directly from FFmpeg"""
    try:
        logger.info(f"Attempting to stream video {video_id}")
        video = get_video_by_id(video_id)
        if not video:
            logger.error(f"Video {video_id} not found in database")
            return jsonify({'error': 'Video not found'}), 404

        file_path = video.get('location_on_disk') or video.get('filled_by_file')
        logger.info(f"Video file path: {file_path}")
        
        if not file_path or not os.path.exists(file_path):
            logger.error(f"Video file not found at path: {file_path}")
            return jsonify({'error': 'Video file not found'}), 404

        # Clean up any existing stream for this video
        cleanup_stream(video_id)

        # Get video info from cache if possible
        video_info = get_video_info(file_path)
        if not video_info:
            return jsonify({'error': 'Could not get video information'}), 500

        # Parse range header and time parameter
        range_header = request.headers.get('Range')
        start_time = request.args.get('t', type=float, default=0)

        if range_header:
            match = re.search(r'bytes=(\d+)-', range_header)
            if match:
                bytes_pos = int(match.group(1))
                file_size = int(video_info.get('size', 0))
                duration = float(video_info.get('duration', 0))
                if file_size and duration:
                    start_time = max(start_time, (bytes_pos / file_size) * duration)

        # Check if we can directly stream the video
        can_direct_stream = is_browser_compatible(video_info) and start_time == 0
        
        if can_direct_stream:
            logger.info("Using direct stream copy")
            ffmpeg_cmd = [
                'ffmpeg',
                '-i', file_path,
                '-c', 'copy',
                '-movflags', '+faststart+delay_moov',
                '-f', 'mp4',
                'pipe:1'
            ]
        else:
            # Check for hardware acceleration support
            use_vaapi = check_vaapi_support()
            logger.info(f"Using VAAPI hardware acceleration: {use_vaapi}")

            # Base ffmpeg command
            ffmpeg_cmd = ['ffmpeg', '-i', file_path]
            
            # Add seek parameter if needed
            if start_time > 0:
                ffmpeg_cmd.extend(['-ss', str(start_time)])

            if use_vaapi:
                # Add VAAPI hardware acceleration parameters
                ffmpeg_cmd[1:1] = [
                    '-hwaccel', 'vaapi',
                    '-hwaccel_output_format', 'vaapi',
                    '-hwaccel_device', '/dev/dri/renderD128'
                ]

                # Add video filters based on whether the source is HDR
                if video_info.get('is_hdr', False):
                    vf = 'format=p010le|vaapi,hwupload,scale_vaapi=w=1920:h=1080:format=nv12,tonemap_vaapi=format=nv12:p=bt709:t=bt709:m=bt709'
                else:
                    vf = 'format=nv12|vaapi,hwupload,scale_vaapi=w=1920:h=1080:format=nv12'
                
                ffmpeg_cmd.extend([
                    '-vf', vf,
                    '-c:v', 'h264_vaapi'
                ])
            else:
                # Software encoding fallback
                ffmpeg_cmd.extend([
                    '-vf', 'scale=1920:1080',
                    '-c:v', 'libx264',
                    '-preset', 'veryfast'
                ])

            # Common parameters for both hardware and software encoding
            ffmpeg_cmd.extend([
                '-rc_mode', 'VBR',
                '-b:v', '8M',
                '-maxrate', '10M',
                '-bufsize', '16M',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-ac', '2',
                '-ar', '48000',
                '-movflags', '+faststart+delay_moov',
                '-f', 'mp4',
                'pipe:1'
            ])

        logger.info(f"Starting FFmpeg with command: {' '.join(ffmpeg_cmd)}")

        process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=-1
        )

        # Store process in dictionary
        with process_lock:
            ffmpeg_processes[video_id] = process

        def generate():
            try:
                while True:
                    chunk = process.stdout.read(64*1024)
                    if not chunk:
                        retcode = process.poll()
                        if retcode is not None:
                            stderr_output = process.stderr.read()
                            if stderr_output:
                                logger.error(f"FFmpeg error output: {stderr_output.decode()}")
                            if retcode != 0 and retcode != 255:  # Ignore SIGTERM (255)
                                logger.error(f"FFmpeg process ended with return code: {retcode}")
                            break
                        continue
                    yield chunk
            except GeneratorExit:
                logger.info(f"Client disconnected from video {video_id}")
                cleanup_stream(video_id)
            except Exception as e:
                logger.error(f"Error in generate: {str(e)}")
                stderr_output = process.stderr.read()
                if stderr_output:
                    logger.error(f"FFmpeg error output: {stderr_output.decode()}")
                raise
            finally:
                cleanup_stream(video_id)

        headers = {
            'Content-Type': 'video/mp4',
            'Transfer-Encoding': 'chunked',
            'Connection': 'keep-alive',
            'Cache-Control': 'no-cache',
            'Accept-Ranges': 'none'
        }

        return Response(
            generate(),
            mimetype='video/mp4',
            headers=headers,
            direct_passthrough=True
        )

    except Exception as e:
        logger.error(f"Error in stream_video: {str(e)}")
        cleanup_stream(video_id)  # Ensure cleanup happens even if setup fails
        return jsonify({'error': str(e)}), 500

@video_routes.route('/<int:video_id>/cleanup', methods=['POST'])
def cleanup_video(video_id):
    """Cleanup video resources when playback is done"""
    cleanup_stream(video_id)
    return jsonify({'status': 'success'})

# Register cleanup functions
atexit.register(cleanup_temp_files)
