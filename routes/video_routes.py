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
            '-show_entries', 'format=duration,size,bit_rate:stream=width,height,codec_name',
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
        
        video_info = {
            'duration': float(format_data.get('duration', 0)),
            'size': int(format_data.get('size', 0)),
            'bit_rate': int(format_data.get('bit_rate', 0)),
            'width': int(stream_data.get('width', 0)),
            'height': int(stream_data.get('height', 0)),
            'codec': stream_data.get('codec_name', '')
        }

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

@video_routes.before_request
def log_request():
    """Log all requests to this blueprint"""
    logger.debug(f"Received request: {request.method} {request.path}")

@video_routes.route('/browse')
def browse_videos():
    videos = get_all_videos()
    return render_template('browse.html', videos=videos)

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

        file_path = video.get('filled_by_file')
        if not file_path or not os.path.exists(file_path):
            return jsonify({'error': 'Video file not found'}), 404

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

        file_path = video.get('filled_by_file')
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

        # Construct FFmpeg command with proper audio handling
        ffmpeg_cmd = [
            'ffmpeg',
            '-i', file_path,
            '-ss', str(start_time),
            '-c:v', 'copy',
            '-c:a', 'aac',  # Convert AC3 to AAC
            '-movflags', '+faststart+delay_moov',
            '-f', 'mp4',
            'pipe:1'
        ]

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
