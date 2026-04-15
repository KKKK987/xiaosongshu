from .auth import auth_bp
from .music import music_bp
from .netease import netease_bp
from .qqmusic import qqmusic_bp
from .playlist import playlist_bp
from .admin import admin_bp


def register_blueprints(app):
    app.register_blueprint(auth_bp)
    app.register_blueprint(music_bp)
    app.register_blueprint(netease_bp)
    app.register_blueprint(qqmusic_bp)
    app.register_blueprint(playlist_bp)
    app.register_blueprint(admin_bp)
