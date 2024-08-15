from flask import render_template, jsonify, request
from settings import load_config, save_config

def get_settings_page():
    settings = load_config()
    return render_template('settings_base.html', settings=settings)

def update_settings():
    new_settings = request.json
    current_settings = load_config()
    update_nested_settings(current_settings, new_settings)
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