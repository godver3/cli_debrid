from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json

db = SQLAlchemy()

class Magnet(db.Model):
    """Model for storing magnet assignments with file matches."""
    __tablename__ = 'magnets'

    id = db.Column(db.Integer, primary_key=True)
    magnet_link = db.Column(db.String(2048), nullable=False)
    tmdb_id = db.Column(db.String(20), nullable=False)
    media_type = db.Column(db.String(10), nullable=False)  # 'movie' or 'show'
    title = db.Column(db.String(255), nullable=False)
    year = db.Column(db.String(4), nullable=False)
    version = db.Column(db.String(20), nullable=False)
    _file_matches = db.Column('file_matches', db.Text, nullable=True)  # JSON string
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def file_matches(self):
        """Get file matches as a dictionary."""
        if self._file_matches:
            return json.loads(self._file_matches)
        return {}

    @file_matches.setter
    def file_matches(self, value):
        """Set file matches from a dictionary."""
        if value is None:
            self._file_matches = None
        else:
            self._file_matches = json.dumps(value)

    def __init__(self, magnet_link, tmdb_id, media_type, title, year, version, file_matches=None):
        self.magnet_link = magnet_link
        self.tmdb_id = tmdb_id
        self.media_type = media_type
        self.title = title
        self.year = year
        self.version = version
        self.file_matches = file_matches

    def to_dict(self):
        """Convert magnet assignment to dictionary."""
        return {
            'id': self.id,
            'magnet_link': self.magnet_link,
            'tmdb_id': self.tmdb_id,
            'media_type': self.media_type,
            'title': self.title,
            'year': self.year,
            'version': self.version,
            'file_matches': self.file_matches,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

    def __repr__(self):
        return f'<Magnet {self.title} ({self.year}) - {self.version}>' 