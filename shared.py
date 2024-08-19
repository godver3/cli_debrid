from flask import Flask

app = Flask(__name__)
app.config['SESSION_TYPE'] = 'filesystem'

total_processed = 0
successful_additions = 0
failed_additions = 0

def update_stats(processed=0, successful=0, failed=0):
    global total_processed, successful_additions, failed_additions
    total_processed += processed
    successful_additions += successful
    failed_additions += failed