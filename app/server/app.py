#!/usr/bin/env python3
# -*- coding: utf-8 -*-

print("[DEBUG] app.py 开始加载...", flush=True)

import os
import threading

import config
from config import STATIC_DIR, TEMPLATE_DIR, args, logger

print("[DEBUG] 开始导入第三方库...", flush=True)
from flask import Flask, request, jsonify, send_file, redirect, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import timedelta

print("[DEBUG] 所有第三方库导入成功", flush=True)

from models.db import init_db
from services.user import init_admin_user
from services.scanner import scan_library_incremental, init_watchdog
from routes import register_blueprints

# --- Flask app ---
app = Flask(__name__, static_folder=STATIC_DIR, template_folder=TEMPLATE_DIR)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000
app.secret_key = os.environ.get('APP_SECRET_KEY', 'xiaosongshu_secret')
app.permanent_session_lifetime = timedelta(days=30)


@app.route('/favicon.ico')
def favicon():
    return send_file(os.path.join(STATIC_DIR, 'images', 'ICON_256.PNG'), mimetype='image/png')


@app.route('/static/js/service-worker.js')
def serve_service_worker():
    response = send_file(
        os.path.join(STATIC_DIR, 'js', 'service-worker.js'),
        mimetype='application/javascript'
    )
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response


# --- Auth middleware ---
def _auth_failed():
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'unauthorized'}), 401
    return redirect(url_for('auth.login', next=request.path))


@app.before_request
def require_auth():
    path = request.path or ''
    if (path.startswith('/static') or path.startswith('/login') or
            path.startswith('/register') or path == '/favicon.ico'):
        return

    preview_allowed_paths = [
        '/api/music/external/meta',
        '/api/music/external/stream',
        '/api/music/external/cover',
        '/api/favorites',
        '/api/songs',
        '/api/playlists',
        '/api/lyrics',
        '/api/qqmusic/search',
        '/api/qqmusic/song/url',
        '/api/netease/search',
        '/api/netease/song/url',
    ]

    is_preview_api = any(path.startswith(p) for p in preview_allowed_paths)

    external_api_paths = [
        '/api/qqmusic/search',
        '/api/qqmusic/song/url',
        '/api/netease/search',
        '/api/netease/song/url',
    ]
    is_external_api = any(path.startswith(p) for p in external_api_paths)
    if is_external_api:
        return

    if is_preview_api:
        forwarded_prefix = request.headers.get('X-Forwarded-Prefix', '')
        if 'index.cgi' in forwarded_prefix:
            return
        user_agent = request.headers.get('User-Agent', '').lower()
        referer = request.headers.get('Referer', '').lower()
        if ('fnos' in user_agent or 'fnnas' in user_agent or
                'preview' in referer or 'index.cgi' in referer):
            return

    if session.get('authed') and session.get('user_hash'):
        return
    return _auth_failed()


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET,PUT,POST,DELETE,OPTIONS'
    return response


# --- Register all blueprints ---
register_blueprints(app)

# --- Init admin user ---
init_admin_user()

# --- Preload netease config ---
from routes.netease import load_netease_config, load_netease_cookie
load_netease_config()
load_netease_cookie()

# --- Background startup tasks ---
threading.Thread(target=lambda: (init_db(), scan_library_incremental()), daemon=True).start()
threading.Thread(target=init_watchdog, daemon=True).start()


# --- Entry point ---
if __name__ == '__main__':
    init_db()

    from routes.qqmusic import _load_qqmusic_credential, _start_qqmusic_credential_refresh_task
    _load_qqmusic_credential()
    _start_qqmusic_credential_refresh_task()

    port = args.port
    logger.info(f"服务启动在端口 {port}")
    app.run(host='0.0.0.0', port=port, threaded=True)
