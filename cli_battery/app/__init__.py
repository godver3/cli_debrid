from flask import Flask
from .database import init_db

def create_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////user/db_content/cli_battery.db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    with app.app_context():
        init_db(app)

    # Import and register blueprints
    from app.routes.site_routes import main_bp
    from app.routes.api_routes import api_bp
    from app.routes.trakt_routes import trakt_bp
    from app.routes.settings_routes import settings_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(trakt_bp)
    app.register_blueprint(settings_bp)

    return app