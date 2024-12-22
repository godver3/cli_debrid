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

# Store FFmpeg processes and video info cache
ffmpeg_processes = {}
video_info_cache = {}
process_lock = Lock()
cache_lock = Lock()

stream_bitrates = defaultdict(lambda: 6000000)  # Default to 6Mbps
stream_locks = defaultdict(Lock)

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

def analyze_streams(file_path):
    """Analyze streams in the video file and return compatible streams for MP4 output"""
    cmd = [
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'a',  # Select all audio streams
        '-show_entries', 'stream=index:stream=codec_name:stream=channels:stream=tags',
        '-of', 'json',
        file_path
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        streams_info = json.loads(result.stdout)
        
        # List of audio codecs that can be copied directly
        copy_codecs = {'aac', 'mp3'}
        # Codecs that need transcoding (excluding truehd which we'll skip)
        transcode_codecs = {'ac3', 'eac3', 'dts'}
        
        stream_configs = []
        ac3_stream = None  # Store the best AC3 stream candidate
        
        for stream in streams_info.get('streams', []):
            codec = stream.get('codec_name')
            channels = stream.get('channels', 2)
            index = stream['index']
            
            # Skip TrueHD streams
            if codec == 'truehd':
                continue
                
            if codec in copy_codecs:
                stream_configs.append({
                    'index': index,
                    'codec': codec,
                    'channels': channels,
                    'copy': True,
                    'language': stream.get('tags', {}).get('language', 'und')
                })
            elif codec in transcode_codecs:
                stream_info = {
                    'index': index,
                    'codec': codec,
                    'channels': channels,
                    'copy': False,
                    'language': stream.get('tags', {}).get('language', 'und')
                }
                
                # If this is an AC3 stream with 5.1/6 channels, mark it as our preferred choice
                if codec == 'ac3' and channels in (6, 8):
                    ac3_stream = stream_info
                stream_configs.append(stream_info)
        
        # If we found a good AC3 stream, move it to the front of the list
        if ac3_stream and ac3_stream in stream_configs:
            stream_configs.remove(ac3_stream)
            stream_configs.insert(0, ac3_stream)
        
        return stream_configs
    except Exception as e:
        logger.error(f"Error analyzing streams: {str(e)}")
        return []

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

@video_routes.route('/api/buffer_health', methods=['POST'])
def update_buffer_health():
    data = request.get_json()
    video_id = data.get('videoId')
    health = float(data.get('health', 1.0))
    
    with stream_locks[video_id]:
        current_bitrate = stream_bitrates[video_id]
        
        # Adjust bitrate based on buffer health
        if health < 0.3:  # Poor buffer health
            new_bitrate = max(1000000, current_bitrate - 500000)  # Reduce by 500kbps, min 1Mbps
        elif health > 0.8:  # Good buffer health
            new_bitrate = min(6000000, current_bitrate + 250000)  # Increase by 250kbps, max 6Mbps
        else:
            new_bitrate = current_bitrate  # Maintain current bitrate
            
        stream_bitrates[video_id] = new_bitrate
        
    return jsonify({'status': 'ok'})

def get_stream_bitrate(video_id):
    with stream_locks[video_id]:
        return stream_bitrates[video_id]

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
    try:
        # Get start time from query parameter, default to 0
        start_time = float(request.args.get('t', 0))
        logger.info(f"Starting stream for video {video_id} at time {start_time}")
        
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
            
            # Get audio stream info for transcoding if needed
            audio_streams = analyze_streams(file_path)
            needs_audio_transcode = any(not stream['copy'] for stream in audio_streams)
            
            if needs_audio_transcode:
                # Base command with video copy
                ffmpeg_cmd = [
                    'ffmpeg',
                    '-i', file_path,
                    '-map', '0:v:0',
                    '-c:v', 'copy'
                ]
                
                # Add audio streams with appropriate encoding
                for i, stream in enumerate(audio_streams):
                    ffmpeg_cmd.extend([
                        '-map', f'0:{stream["index"]}'
                    ])
                    if stream['copy']:
                        ffmpeg_cmd.extend([
                            f'-c:a:{i}', 'copy'
                        ])
                    else:
                        # Transcode to AAC with appropriate bitrate based on channels
                        bitrate = min(stream['channels'] * 64, 384)  # 64k per channel, max 384k
                        ffmpeg_cmd.extend([
                            f'-c:a:{i}', 'aac',
                            f'-b:a:{i}', f'{bitrate}k',
                            f'-ac:a:{i}', '2' if stream['channels'] <= 2 else '6'
                        ])
            else:
                # If all audio streams can be copied, use simple command
                ffmpeg_cmd = [
                    'ffmpeg',
                    '-i', file_path,
                    '-map', '0:v:0',
                    '-map', '0:a',
                    '-c:v', 'copy',
                    '-c:a', 'copy'
                ]
            
            # Add output options
            ffmpeg_cmd.extend([
                '-movflags', '+faststart+delay_moov',
                '-f', 'mp4',
                'pipe:1'
            ])
        else:
            # Check for hardware acceleration support
            use_vaapi = check_vaapi_support()
            logger.info(f"Using VAAPI hardware acceleration: {use_vaapi}")

            if use_vaapi:
                # Construct FFmpeg command with hardware acceleration
                ffmpeg_cmd = [
                    'ffmpeg',
                    # Hardware acceleration setup
                    '-hwaccel', 'vaapi',
                    '-hwaccel_device', '/dev/dri/renderD128',
                    '-hwaccel_output_format', 'vaapi'
                ]
                
                # Add seek time if specified
                if start_time > 0:
                    ffmpeg_cmd.extend(['-ss', str(start_time)])
                
                ffmpeg_cmd.extend([
                    # Input options
                    '-i', file_path,
                    
                    # Video encoding settings
                    '-vf', 'format=nv12|vaapi,hwupload,scale_vaapi=w=1920:h=1080:format=nv12',  # Reduce to 1080p
                    '-c:v', 'h264_vaapi',
                    '-rc_mode', 'CBR',  # Use CBR for more stable bitrate
                    '-b:v', f'{int(get_stream_bitrate(video_id) / 1000000)}M',
                    '-maxrate', f'{int(get_stream_bitrate(video_id) * 1.25 / 1000000)}M',
                    '-bufsize', f'{int(get_stream_bitrate(video_id) * 2 / 1000000)}M',
                    '-profile:v', 'main',  # Use main profile for better compatibility
                    '-level', '4.1',
                    
                    # GOP and keyframe settings
                    '-g', '24',  # One keyframe every second at 24fps
                    '-keyint_min', '24',
                    
                    # Stream selection
                    '-map', '0:v:0',
                    '-map', '0:a:0',  # Select the first audio stream
                    # '-map', selected_audio_stream,
                    
                    # Audio settings
                    '-c:a:0', 'aac',
                    '-b:a:0', '128k',  # Reduce audio bitrate
                    '-ac:a:0', '2',
                    
                    # Performance settings
                    '-threads', '4',
                    '-quality', '28',  # Lower quality for better performance
                    '-low_power', '1',  # Enable low power mode
                    
                    # Output format settings
                    '-movflags', '+faststart+delay_moov+frag_keyframe+empty_moov+default_base_moof',
                    '-max_muxing_queue_size', '9999',
                    
                    # Output format
                    '-f', 'mp4',
                    'pipe:1'
                ])
            else:
                # Software encoding fallback
                ffmpeg_cmd = [
                    'ffmpeg'
                ]
                
                # Add seek time if specified
                if start_time > 0:
                    ffmpeg_cmd.extend(['-ss', str(start_time)])
                
                ffmpeg_cmd.extend([
                    '-i', file_path,
                    '-vf', 'scale=1920:1080',
                    '-c:v', 'libx264',
                    '-preset', 'veryfast',
                    '-rc_mode', 'VBR',
                    '-b:v', '4M',
                    '-maxrate', '6M',
                    '-bufsize', '8M',
                    '-profile:v', 'main',
                    '-level', '4.1',
                    '-c:a', 'aac',
                    '-b:a', '192k',
                    '-ac', '2',
                    '-ar', '48000',
                    '-movflags', '+faststart+delay_moov+frag_keyframe+empty_moov',
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

        def log_stderr():
            while True:
                line = process.stderr.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    logger.info(f"FFmpeg stderr: {line.decode().strip()}")

        # Start stderr logging thread
        import threading
        stderr_thread = threading.Thread(target=log_stderr, daemon=True)
        stderr_thread.start()

        def generate():
            try:
                buffer = bytearray()
                buffer_size = 1024 * 1024  # 1MB buffer
                initial_buffer_size = 2 * 1024 * 1024  # 2MB initial buffer
                initial_buffering = True

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

                    buffer.extend(chunk)

                    # For initial buffering, wait until we have enough data
                    if initial_buffering:
                        if len(buffer) >= initial_buffer_size:
                            logger.info(f"Initial buffer filled ({len(buffer)} bytes), starting playback")
                            initial_buffering = False
                            yield bytes(buffer)
                            buffer.clear()
                        continue

                    # Regular streaming after initial buffer
                    if len(buffer) >= buffer_size:
                        yield bytes(buffer)
                        buffer.clear()

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
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'  # Disable Nginx buffering if present
        }

        return Response(
            generate(),
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
    try:
        with stream_locks[video_id]:
            if video_id in stream_bitrates:
                del stream_bitrates[video_id]
        if video_id in stream_locks:
            del stream_locks[video_id]
    except Exception as e:
        logger.error(f"Error cleaning up stream data: {str(e)}")
    cleanup_stream(video_id)
    return jsonify({'status': 'success'})

@video_routes.route('/log_seek', methods=['POST'])
def log_seek():
    """Log video seek events"""
    try:
        data = request.get_json()
        video_id = data.get('video_id')
        seek_time = data.get('seek_time')
        current_time = data.get('current_time')
        total_duration = data.get('total_duration')
        
        logger.info(f"Seek event - Video ID: {video_id}, "
                   f"From: {current_time:.2f}s, "
                   f"To: {seek_time:.2f}s, "
                   f"Duration: {total_duration:.2f}s")
        
        return jsonify({'status': 'success'})
    except Exception as e:
        logger.error(f"Error logging seek event: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@video_routes.route('/seek', methods=['POST'])
def seek_video():
    """Handle video seek requests"""
    try:
        data = request.get_json()
        video_id = data.get('video_id')
        seek_time = data.get('seek_time')
        
        if not video_id or seek_time is None:
            return jsonify({'error': 'Missing video_id or seek_time'}), 400
            
        # Clean up existing stream
        cleanup_stream(video_id)
        
        # Return success - the frontend will request a new stream
        return jsonify({
            'status': 'success',
            'seek_time': seek_time
        })
        
    except Exception as e:
        logger.error(f"Error in seek_video: {str(e)}")
        return jsonify({'error': str(e)}), 500

# Register cleanup functions
atexit.register(cleanup_temp_files)