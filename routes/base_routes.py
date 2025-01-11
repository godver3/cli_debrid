from flask import Blueprint, jsonify
import requests
import logging
from datetime import datetime

base_bp = Blueprint('base', __name__)

@base_bp.route('/api/release-notes', methods=['GET'])
def get_release_notes():
    try:
        # GitHub API endpoint for commits (using public API)
        api_url = "https://api.github.com/repos/godver3/cli_debrid/commits"
        
        # Make request to GitHub API with a user agent (required by GitHub)
        headers = {
            'User-Agent': 'cli-debrid-app'
        }
        
        # Get the latest 10 commits
        params = {
            'per_page': 10,
            'page': 1
        }
        
        response = requests.get(api_url, headers=headers, params=params)
        if response.status_code == 200:
            commits = response.json()
            if not commits:
                return jsonify({
                    'success': True,
                    'version': 'No Commits',
                    'name': 'No Commits Available',
                    'body': 'No commit history is available.',
                    'published_at': ''
                })
            
            # Format the commit messages into markdown
            commit_notes = []
            for commit in commits:
                date = datetime.strptime(commit['commit']['author']['date'], '%Y-%m-%dT%H:%M:%SZ').strftime('%Y-%m-%d %H:%M:%S')
                message = commit['commit']['message']
                sha = commit['sha'][:7]  # Short SHA
                commit_notes.append(f"### {date} - {sha}\n{message}\n")
            
            body = "\n".join(commit_notes)
            
            return jsonify({
                'success': True,
                'version': f"Latest Commit: {commits[0]['sha'][:7]}",
                'name': 'Recent Changes',
                'body': body,
                'published_at': commits[0]['commit']['author']['date']
            })
        else:
            logging.error(f"Failed to fetch commit history. Status code: {response.status_code}")
            return jsonify({
                'success': False,
                'error': 'Failed to fetch commit history'
            }), 500
            
    except Exception as e:
        logging.error(f"Error fetching commit history: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500 