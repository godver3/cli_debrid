from flask import Blueprint, jsonify, current_app
import requests
import logging
from datetime import datetime
import os
import sys

base_bp = Blueprint('base', __name__)

def get_current_branch():
    try:
        if getattr(sys, 'frozen', False):
            application_path = sys._MEIPASS
        else:
            application_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        branch_path = os.path.join(application_path, 'branch_id')
        
        with open(branch_path, 'r') as f:
            return f.read().strip()
    except Exception as e:
        logging.error(f"Error reading branch_id file: {str(e)}")
        return 'main'  # Default to main if there's an error

def get_branch_suffix():
    branch = get_current_branch()
    return 'm' if branch == 'main' else 'd'

# Register the function to be available in templates
@base_bp.app_template_global()
def get_version_with_branch():
    try:
        if getattr(sys, 'frozen', False):
            application_path = sys._MEIPASS
        else:
            application_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        version_path = os.path.join(application_path, 'version.txt')
        
        with open(version_path, 'r') as f:
            version = f.read().strip()
        return f"{version}{get_branch_suffix()}"
    except Exception as e:
        logging.error(f"Error reading version: {str(e)}")
        return f"0.0.0{get_branch_suffix()}"

@base_bp.route('/api/release-notes', methods=['GET'])
def get_release_notes():
    try:
        # Get current branch from branch_id file
        current_branch = get_current_branch()
        
        # GitHub API endpoint for commits (using public API)
        api_url = f"https://api.github.com/repos/godver3/cli_debrid/commits"
        
        # Make request to GitHub API with a user agent (required by GitHub)
        headers = {
            'User-Agent': 'cli-debrid-app'
        }
        
        # Get the latest 10 commits for the current branch
        params = {
            'per_page': 10,
            'page': 1,
            'sha': current_branch  # Specify the branch to fetch commits from
        }
        
        response = requests.get(api_url, headers=headers, params=params)
        if response.status_code == 200:
            commits = response.json()
            if not commits:
                return jsonify({
                    'success': True,
                    'version': 'No Commits',
                    'name': f'No Commits Available ({current_branch} branch)',
                    'body': 'No commit history is available.',
                    'published_at': ''
                })
            
            # Format the commit messages into markdown
            commit_notes = []
            seen_versions = set()
            
            for commit in commits:
                message = commit['commit']['message']
                # Only process commits that start with version numbers (e.g., "0.5.35 -")
                if not message.strip().startswith(('0.', '1.', '2.')):
                    continue
                    
                # Extract version from message (assuming format "X.Y.Z - description")
                version = message.split(' - ')[0].strip()
                
                # Skip if we've already seen this version
                if version in seen_versions:
                    continue
                    
                seen_versions.add(version)
                date = datetime.strptime(commit['commit']['author']['date'], '%Y-%m-%dT%H:%M:%SZ').strftime('%Y-%m-%d %H:%M:%S')
                sha = commit['sha'][:7]  # Short SHA
                commit_notes.append(f"### {date} - {sha}\n{message}\n")
            
            body = "\n".join(commit_notes) if commit_notes else "No version commits available."
            
            return jsonify({
                'success': True,
                'version': f"Latest Commit: {commits[0]['sha'][:7]} ({current_branch} branch)",
                'name': f'Recent Changes - {current_branch} branch',
                'body': body,
                'published_at': commits[0]['commit']['author']['date']
            })
        else:
            logging.error(f"Failed to fetch commit history for branch {current_branch}. Status code: {response.status_code}")
            return jsonify({
                'success': False,
                'error': f'Failed to fetch commit history for branch {current_branch}'
            }), 500
            
    except Exception as e:
        logging.error(f"Error fetching commit history: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500 