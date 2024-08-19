from flask import render_template, jsonify, request
from settings import load_config, save_config
import json

def get_settings_page():
    settings = load_config()
    return render_template('settings_base.html', settings=settings)

def update_settings():
    new_settings = request.json
    current_settings = load_config()
    update_nested_settings(current_settings, new_settings)
    
    # Special handling for Content Sources
    if 'Content Sources' in current_settings:
        for key, value in current_settings['Content Sources'].items():
            if isinstance(value, str):
                try:
                    current_settings['Content Sources'][key] = json.loads(value)
                except json.JSONDecodeError:
                    pass  # Keep the original string if it's not valid JSON
    
    save_config(current_settings)
    return jsonify({"status": "success"})

def update_nested_settings(current, new):
    for key, value in new.items():
        if isinstance(value, dict):
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            update_nested_settings(current[key], value)
        else:
            current[key] = value

def get_settings():
    return jsonify(load_config())