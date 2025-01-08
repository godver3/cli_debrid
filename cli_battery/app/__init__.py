from flask import Flask
from .database import init_db
import os
from .limiter import init_limiter

def create_app():
    app = Flask(__name__)
    
    # Get db_content directory from environment variable with fallback
    db_directory = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    os.makedirs(db_directory, exist_ok=True)
    
    db_path = os.path.join(db_directory, 'cli_battery.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    with app.app_context():
        init_db(app)
        init_limiter(app)  # Initialize the rate limiter

    # Import and register blueprints
    from app.routes.site_routes import main_bp
    from app.routes.api_routes import api_bp
    from app.routes.trakt_routes import trakt_bp
    from app.routes.settings_routes import settings_bp
    #from app.routes.queues_routes import queues_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(trakt_bp)
    app.register_blueprint(settings_bp)
    #app.register_blueprint(queues_bp)

    return app