from flask import Flask, render_template_string, jsonify
import threading
import time
from queue_manager import QueueManager

app = Flask(__name__)
queue_manager = QueueManager()

# Global variables for statistics
start_time = time.time()
total_processed = 0
successful_additions = 0
failed_additions = 0

@app.route('/')
def index():
    queue_contents = queue_manager.get_queue_contents()
    logs = get_recent_logs(100)  # Get the last 100 log entries
    uptime = time.time() - start_time
    stats = {
        'total_processed': total_processed,
        'successful_additions': successful_additions,
        'failed_additions': failed_additions,
        'uptime': uptime
    }
    return render_template_string(template, queue_contents=queue_contents, logs=logs, stats=stats)

@app.route('/api/queue_contents')
def api_queue_contents():
    return jsonify(queue_manager.get_queue_contents())

@app.route('/api/stats')
def api_stats():
    uptime = time.time() - start_time
    return jsonify({
        'total_processed': total_processed,
        'successful_additions': successful_additions,
        'failed_additions': failed_additions,
        'uptime': uptime
    })

def get_recent_logs(n):
    with open('activity.log', 'r') as f:
        logs = f.readlines()
    return logs[-n:]

template = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Queue Manager</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: Arial, sans-serif; }
        .container { width: 90%; margin: 0 auto; }
        .queue, .logs, .stats { margin-top: 20px; }
        .queue-title { font-size: 24px; }
        .item { margin-left: 20px; }
        .log-entry { margin-left: 20px; }
        .chart-container { width: 400px; height: 400px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Queue Manager</h1>
        <div class="stats">
            <h2>Statistics</h2>
            <p>Total Processed: <span id="total-processed">{{ stats.total_processed }}</span></p>
            <p>Successful Additions: <span id="successful-additions">{{ stats.successful_additions }}</span></p>
            <p>Failed Additions: <span id="failed-additions">{{ stats.failed_additions }}</span></p>
            <p>Uptime: <span id="uptime">{{ '%d days, %d hours, %d minutes' % (stats.uptime // 86400, (stats.uptime % 86400) // 3600, (stats.uptime % 3600) // 60) }}</span></p>
        </div>
        <div class="chart-container">
            <canvas id="queueChart"></canvas>
        </div>
        <div id="queue-contents">
        {% for queue_name, items in queue_contents.items() %}
        <div class="queue">
            <div class="queue-title">{{ queue_name }} (<span class="queue-count">{{ items|length }}</span> items)</div>
            <div class="queue-items">
            {% for item in items %}
            <div class="item">
                {{ item.title }} ({{ item.year }}){% if item.type == 'episode' %} S{{ item.season_number }}E{{ item.episode_number }}{% endif %}
            </div>
            {% endfor %}
            </div>
        </div>
        {% endfor %}
        </div>
        <div class="logs">
            <div class="queue-title">Logs</div>
            <div id="log-entries">
            {% for log in logs %}
            <div class="log-entry">{{ log }}</div>
            {% endfor %}
            </div>
        </div>
    </div>
    <script>
        function updateQueueContents() {
            fetch('/api/queue_contents')
                .then(response => response.json())
                .then(data => {
                    let queueContents = document.getElementById('queue-contents');
                    queueContents.innerHTML = '';
                    for (let [queueName, items] of Object.entries(data)) {
                        let queueDiv = document.createElement('div');
                        queueDiv.className = 'queue';
                        queueDiv.innerHTML = `
                            <div class="queue-title">${queueName} (<span class="queue-count">${items.length}</span> items)</div>
                            <div class="queue-items">
                                ${items.map(item => `
                                    <div class="item">
                                        ${item.title} (${item.year})${item.type === 'episode' ? ` S${item.season_number}E${item.episode_number}` : ''}
                                    </div>
                                `).join('')}
                            </div>
                        `;
                        queueContents.appendChild(queueDiv);
                    }
                    updateChart(data);
                });
        }

        function updateStats() {
            fetch('/api/stats')
                .then(response => response.json())
                .then(data => {
                    document.getElementById('total-processed').textContent = data.total_processed;
                    document.getElementById('successful-additions').textContent = data.successful_additions;
                    document.getElementById('failed-additions').textContent = data.failed_additions;
                    let uptime = Math.floor(data.uptime);
                    let days = Math.floor(uptime / 86400);
                    let hours = Math.floor((uptime % 86400) / 3600);
                    let minutes = Math.floor((uptime % 3600) / 60);
                    document.getElementById('uptime').textContent = `${days} days, ${hours} hours, ${minutes} minutes`;
                });
        }

        let chart;
        function updateChart(data) {
            let ctx = document.getElementById('queueChart').getContext('2d');
            let labels = Object.keys(data);
            let values = labels.map(label => data[label].length);
            
            if (chart) {
                chart.data.labels = labels;
                chart.data.datasets[0].data = values;
                chart.update();
            } else {
                chart = new Chart(ctx, {
                    type: 'pie',
                    data: {
                        labels: labels,
                        datasets: [{
                            data: values,
                            backgroundColor: [
                                'rgba(255, 99, 132, 0.8)',
                                'rgba(54, 162, 235, 0.8)',
                                'rgba(255, 206, 86, 0.8)',
                                'rgba(75, 192, 192, 0.8)',
                                'rgba(153, 102, 255, 0.8)'
                            ]
                        }]
                    },
                    options: {
                        responsive: true,
                        title: {
                            display: true,
                            text: 'Queue Distribution'
                        }
                    }
                });
            }
        }

        setInterval(updateQueueContents, 5000);  // Update every 5 seconds
        setInterval(updateStats, 5000);  // Update every 5 seconds
    </script>
</body>
</html>
"""

def run_server():
    app.run(debug=True, use_reloader=False, host='0.0.0.0')

def start_server():
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()

# Function to update statistics
def update_stats(processed=0, successful=0, failed=0):
    global total_processed, successful_additions, failed_additions
    total_processed += processed
    successful_additions += successful
    failed_additions += failed
