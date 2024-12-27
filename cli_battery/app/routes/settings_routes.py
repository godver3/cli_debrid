from flask import render_template, request, jsonify, send_file, redirect, url_for, Blueprint
from app.settings import Settings
from app.metadata_manager import MetadataManager
import io
from app.logger_config import logger
from app.trakt_auth import TraktAuth
from flask import flash
from sqlalchemy import inspect
from app.database import Session, Item, Metadata, Season, Poster  # Add this line
from app.trakt_metadata import TraktMetadata  # Add this import at the top of the file
import json
import time
import os

settings = Settings()

settings_bp = Blueprint('settings', __name__)

@settings_bp.route('/providers')
def providers():
    settings = Settings()
    providers = settings.providers
    
    # Ensure all providers have both rank types
    for i, provider in enumerate(providers, start=1):
        if 'metadata_rank' not in provider:
            provider['metadata_rank'] = i
        if 'poster_rank' not in provider:
            provider['poster_rank'] = i
    
    settings.providers = providers
    settings.save()
    
    any_provider_enabled = any(provider['enabled'] for provider in providers)
    
    # Check if Trakt is authenticated and enabled
    trakt = TraktAuth()
    trakt_authenticated = trakt.is_authenticated()
    trakt_enabled = next((provider['enabled'] for provider in providers if provider['name'] == 'trakt'), False)
    
    return render_template('providers.html', 
                           providers=providers, 
                           any_provider_enabled=any_provider_enabled,
                           trakt_authenticated=trakt_authenticated,
                           trakt_enabled=trakt_enabled)

@settings_bp.route('/set_active_provider', methods=['POST'])
def set_active_provider():
    data = request.json
    provider = data.get('provider')
    settings = Settings()
    if provider == 'none' or any(p['name'] == provider and p['enabled'] for p in settings.providers):
        settings.active_provider = provider
        settings.save()
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Invalid provider'}), 400

@settings_bp.route('/toggle_provider', methods=['POST'])
def toggle_provider():
    data = request.json
    provider_name = data.get('provider')
    action = data.get('action')
    
    settings = Settings()
    providers = settings.providers

    for provider in providers:
        if provider['name'] == provider_name:
            provider['enabled'] = (action == 'enable')
            break
    else:
        return jsonify({'success': False, 'error': 'Provider not found'}), 404
    
    settings.providers = providers
    settings.save()

    return jsonify({
        'success': True, 
        'providers': providers
    })

@settings_bp.route('/update_provider_rank', methods=['POST'])
def update_provider_rank():
    data = request.json
    provider_name = data.get('provider')
    rank_type = data.get('type')
    new_rank = data.get('rank')

    if rank_type not in ['metadata', 'poster']:
        return jsonify({'success': False, 'error': 'Invalid rank type'}), 400

    MetadataManager.update_provider_rank(provider_name, rank_type, new_rank)

    settings = Settings()
    return jsonify({
        'success': True,
        'providers': settings.providers
    })

@settings_bp.route('/settings')
def settings_page():
    return render_template('settings.html', settings=settings.get_all())

@settings_bp.route('/save_settings', methods=['POST'])
def save_settings():
    try:
        new_settings = request.form.to_dict()
        
        # Log received data for debugging
        print(f"Received settings data: {new_settings}")
        
        # Handle checkboxes (convert to boolean)
        for key, value in new_settings.items():
            if value == 'true':
                new_settings[key] = True
            elif value == 'false':
                new_settings[key] = False

        # Handle providers separately
        new_settings['providers'] = request.form.getlist('providers')

        # Update settings
        settings.update(new_settings)

        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving settings: {str(e)}", exc_info=True)
        return jsonify({"success": False, "error": str(e)})