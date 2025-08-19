from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from functools import wraps
import os
import logging
from datetime import datetime
from utilities.settings import get_setting, get_all_settings
from utilities.local_library_scan import create_symlink, get_symlink_path, sanitize_filename
from database.core import get_db_connection
from routes.models import admin_required
from scraper.functions.ptt_parser import parse_with_ptt
import json

symlink_tools_bp = Blueprint('symlink_tools', __name__)

@symlink_tools_bp.route('/', methods=['GET', 'POST'])
@admin_required
def index():
    """Main symlink tools page."""
    if request.method == 'POST':
        return handle_symlink_creation()
    
    # Get settings for the form
    symlinked_files_path = get_setting('File Management', 'symlinked_files_path', '')
    original_files_path = get_setting('File Management', 'original_files_path', '')
    
    # Get available versions from settings
    versions = get_setting('Scraping', 'versions', {})
    version_choices = list(versions.keys()) if versions else ['Default']
    
    # Get content sources
    content_sources = get_setting('Content Sources', {})
    content_source_choices = list(content_sources.keys()) if content_sources else []
    
    return render_template('symlink_tools.html',
                         symlinked_files_path=symlinked_files_path,
                         original_files_path=original_files_path,
                         version_choices=version_choices,
                         content_source_choices=content_source_choices)

def handle_symlink_creation():
    """Handle the symlink creation form submission."""
    try:
        # Get form data
        source_file = request.form.get('source_file', '').strip()
        title = request.form.get('title', '').strip()
        year = request.form.get('year', '').strip()
        media_type = request.form.get('media_type', 'movie')
        season_number = request.form.get('season_number', '').strip()
        episode_number = request.form.get('episode_number', '').strip()
        episode_title = request.form.get('episode_title', '').strip()
        version = request.form.get('version', 'Default')
        content_source = request.form.get('content_source', '')
        imdb_id = request.form.get('imdb_id', '').strip()
        tmdb_id = request.form.get('tmdb_id', '').strip()
        
        # Validate required fields
        if not source_file:
            flash('Source file path is required.', 'error')
            return redirect(url_for('symlink_tools.index'))
        
        if not title:
            flash('Title is required.', 'error')
            return redirect(url_for('symlink_tools.index'))
        
        if not year:
            flash('Year is required.', 'error')
            return redirect(url_for('symlink_tools.index'))
        
        # Validate source file exists
        if not os.path.exists(source_file):
            flash(f'Source file does not exist: {source_file}', 'error')
            return redirect(url_for('symlink_tools.index'))
        
        # Validate season/episode for TV shows
        if media_type == 'episode':
            if not season_number or not episode_number:
                flash('Season number and episode number are required for TV episodes.', 'error')
                return redirect(url_for('symlink_tools.index'))
            
            try:
                season_number = int(season_number)
                episode_number = int(episode_number)
            except ValueError:
                flash('Season number and episode number must be valid integers.', 'error')
                return redirect(url_for('symlink_tools.index'))
        
        # Create item dictionary for symlink path generation
        item = {
            'title': title,
            'year': int(year) if year.isdigit() else year,
            'type': media_type,
            'version': version,
            'content_source': content_source,
            'imdb_id': imdb_id,
            'tmdb_id': tmdb_id,
            'filled_by_file': os.path.basename(source_file)
        }
        
        if media_type == 'episode':
            item.update({
                'season_number': season_number,
                'episode_number': episode_number,
                'episode_title': episode_title
            })
        
        # Generate symlink path
        symlink_path = get_symlink_path(item, os.path.basename(source_file))
        
        if not symlink_path:
            flash('Failed to generate symlink path. Check your settings.', 'error')
            return redirect(url_for('symlink_tools.index'))
        
        # Create the symlink
        success = create_symlink(source_file, symlink_path, skip_verification=True)
        
        if success:
            # Add to database if we have an IMDb ID
            if imdb_id:
                add_to_database(item, symlink_path)
            
            flash(f'Successfully created symlink: {symlink_path}', 'success')
        else:
            flash('Failed to create symlink. Check the logs for details.', 'error')
        
        return redirect(url_for('symlink_tools.index'))
        
    except Exception as e:
        logging.error(f"Error creating symlink: {str(e)}", exc_info=True)
        flash(f'Error creating symlink: {str(e)}', 'error')
        return redirect(url_for('symlink_tools.index'))

def add_to_database(item, symlink_path):
    """Add the item to the database if it doesn't already exist."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if item already exists
        if item['type'] == 'movie':
            cursor.execute('''
                SELECT id FROM media_items 
                WHERE imdb_id = ? AND type = 'movie'
            ''', (item['imdb_id'],))
        else:
            cursor.execute('''
                SELECT id FROM media_items 
                WHERE imdb_id = ? AND type = 'episode' 
                AND season_number = ? AND episode_number = ?
            ''', (item['imdb_id'], item['season_number'], item['episode_number']))
        
        existing_item = cursor.fetchone()
        
        if existing_item:
            # Update existing item with new symlink path
            cursor.execute('''
                UPDATE media_items 
                SET location_on_disk = ?, last_updated = ?
                WHERE id = ?
            ''', (symlink_path, datetime.now(), existing_item[0]))
            logging.info(f"Updated existing item {existing_item[0]} with symlink path: {symlink_path}")
        else:
            # Insert new item
            if item['type'] == 'movie':
                cursor.execute('''
                    INSERT INTO media_items 
                    (imdb_id, tmdb_id, title, year, type, version, content_source, 
                     location_on_disk, state, last_updated, metadata_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    item['imdb_id'], item['tmdb_id'], item['title'], item['year'],
                    'movie', item['version'], item['content_source'], symlink_path,
                    'Collected', datetime.now(), datetime.now()
                ))
            else:
                cursor.execute('''
                    INSERT INTO media_items 
                    (imdb_id, tmdb_id, title, year, type, season_number, episode_number, 
                     episode_title, version, content_source, location_on_disk, 
                     state, last_updated, metadata_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    item['imdb_id'], item['tmdb_id'], item['title'], item['year'],
                    'episode', item['season_number'], item['episode_number'],
                    item['episode_title'], item['version'], item['content_source'],
                    symlink_path, 'Collected', datetime.now(), datetime.now()
                ))
            
            logging.info(f"Added new item to database with symlink path: {symlink_path}")
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        logging.error(f"Error adding item to database: {str(e)}", exc_info=True)
        # Don't fail the symlink creation if database update fails
        pass

@symlink_tools_bp.route('/api/browse-files', methods=['POST'])
@admin_required
def browse_files():
    """API endpoint to browse files and directories."""
    try:
        data = request.get_json()
        path = data.get('path', '/')
        
        # Security check: ensure path is within allowed directories
        allowed_paths = [
            get_setting('File Management', 'original_files_path', ''),
            get_setting('File Management', 'symlinked_files_path', ''),
            '/mnt',  # Common mount point
            '/media',  # Common mount point
            '/home',  # User home directory
        ]
        
        # Filter out empty paths
        allowed_paths = [p for p in allowed_paths if p]
        
        # Check if the requested path is within allowed paths
        path_allowed = False
        for allowed_path in allowed_paths:
            if path.startswith(allowed_path) or path == '/':
                path_allowed = True
                break
        
        if not path_allowed:
            return jsonify({
                'success': False,
                'message': 'Access denied to this directory'
            })
        
        if not os.path.exists(path):
            return jsonify({
                'success': False,
                'message': 'Directory does not exist'
            })
        
        if not os.path.isdir(path):
            return jsonify({
                'success': False,
                'message': 'Path is not a directory'
            })
        
        # Get directory contents
        try:
            items = []
            for item in os.listdir(path):
                item_path = os.path.join(path, item)
                try:
                    stat = os.stat(item_path)
                    items.append({
                        'name': item,
                        'path': item_path,
                        'is_dir': os.path.isdir(item_path),
                        'size': stat.st_size if os.path.isfile(item_path) else None,
                        'modified': datetime.fromtimestamp(stat.st_mtime).isoformat()
                    })
                except (OSError, PermissionError):
                    # Skip items we can't access
                    continue
            
            # Sort: directories first, then files, both alphabetically
            items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
            
            return jsonify({
                'success': True,
                'path': path,
                'parent': os.path.dirname(path) if path != '/' else None,
                'items': items
            })
            
        except PermissionError:
            return jsonify({
                'success': False,
                'message': 'Permission denied accessing directory'
            })
            
    except Exception as e:
        logging.error(f"Error browsing files: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'Error: {str(e)}'
        })

@symlink_tools_bp.route('/api/validate-file', methods=['POST'])
@admin_required
def validate_file():
    """API endpoint to validate if a file exists."""
    try:
        data = request.get_json()
        file_path = data.get('file_path', '').strip()
        
        if not file_path:
            return jsonify({'valid': False, 'message': 'File path is required'})
        
        if os.path.exists(file_path):
            file_size = os.path.getsize(file_path)
            return jsonify({
                'valid': True, 
                'message': f'File exists ({file_size:,} bytes)',
                'filename': os.path.basename(file_path)
            })
        else:
            return jsonify({'valid': False, 'message': 'File does not exist'})
            
    except Exception as e:
        logging.error(f"Error validating file: {str(e)}")
        return jsonify({'valid': False, 'message': f'Error: {str(e)}'})

@symlink_tools_bp.route('/api/preview-path', methods=['POST'])
@admin_required
def preview_path():
    """API endpoint to preview the generated symlink path."""
    try:
        data = request.get_json()
        
        # Create item dictionary
        item = {
            'title': data.get('title', ''),
            'year': data.get('year', ''),
            'type': data.get('media_type', 'movie'),
            'version': data.get('version', 'Default'),
            'content_source': data.get('content_source', ''),
            'imdb_id': data.get('imdb_id', ''),
            'tmdb_id': data.get('tmdb_id', ''),
            'filled_by_file': data.get('filename', '')
        }
        
        if data.get('media_type') == 'episode':
            item.update({
                'season_number': int(data.get('season_number', 0)),
                'episode_number': int(data.get('episode_number', 0)),
                'episode_title': data.get('episode_title', '')
            })
        
        # Generate symlink path
        symlink_path = get_symlink_path(item, data.get('filename', ''))
        
        if symlink_path:
            return jsonify({
                'success': True,
                'path': symlink_path,
                'exists': os.path.exists(symlink_path)
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Failed to generate symlink path'
            })
            
    except Exception as e:
        logging.error(f"Error previewing path: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'Error: {str(e)}'
        })
