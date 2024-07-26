from flask import Flask, render_template_string
import threading
from queue_manager import QueueManager

app = Flask(__name__)
queue_manager = QueueManager()

@app.route('/')
def index():
    queue_contents = queue_manager.get_queue_contents()
    logs = []
    with open('activity.log', 'r') as f:
        logs = f.readlines()
    return render_template_string(template, queue_contents=queue_contents, logs=logs)

template = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Queue Manager</title>
    <style>
        body { font-family: Arial, sans-serif; }
        .container { width: 80%; margin: 0 auto; }
        .queue, .logs { margin-top: 20px; }
        .queue-title { font-size: 24px; }
        .item { margin-left: 20px; }
        .log-entry { margin-left: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Queue Manager</h1>
        {% for queue_name, items in queue_contents.items() %}
        <div class="queue">
            <div class="queue-title">{{ queue_name }} ({{ items|length }} items)</div>
            {% for item in items %}
            <div class="item">
                {{ item.title }} ({{ item.year }}){% if item.type == 'episode' %} S{{ item.season_number }}E{{ item.episode_number }}{% endif %}
            </div>
            {% endfor %}
        </div>
        {% endfor %}
        <div class="logs">
            <div class="queue-title">Logs</div>
            {% for log in logs %}
            <div class="log-entry">{{ log }}</div>
            {% endfor %}
        </div>
    </div>
</body>
</html>
"""

def run_server():
    app.run(debug=True, use_reloader=False, host='0.0.0.0')

def start_server():
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()
