#!/usr/bin/env python3
# -*- coding: utf-8 -*-

print("[DEBUG] app.py 开始加载...", flush=True)

import os
import sys
import time

print("[DEBUG] 基础模块导入完成", flush=True)
import re
import sqlite3
import threading
import shutil
import logging
import argparse
import locale
import concurrent.futures
import traceback
from urllib.parse import quote, unquote, urlparse, parse_qs
import hashlib
import json
from datetime import timedelta

# 全局异常捕获，写入日志文件
def global_exception_handler(exc_type, exc_value, exc_tb):
    error_msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(f"[FATAL ERROR] 未捕获的异常:\n{error_msg}", file=sys.stderr)
    # 同时写入文件
    try:
        with open('/tmp/xiaosongshu_crash.log', 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*50}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n{error_msg}\n")
    except:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = global_exception_handler

if getattr(sys, 'frozen', False):
    # 【打包模式】基准目录是二进制文件所在位置
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # 【源码模式】基准目录是脚本所在位置
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    # 仅在源码模式下加载 lib
    sys.path.insert(0, os.path.join(BASE_DIR, 'lib'))

print("[DEBUG] 开始导入第三方库...", flush=True)
try:
    from flask import Flask, render_template, request, jsonify, send_file, redirect, Response, session, url_for, make_response
    print("[DEBUG] Flask 导入成功", flush=True)
    import requests
    print("[DEBUG] requests 导入成功", flush=True)
    from mutagen import File
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, APIC, USLT
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover
    print("[DEBUG] mutagen 导入成功", flush=True)
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    print("[DEBUG] watchdog 导入成功", flush=True)
    from werkzeug.middleware.proxy_fix import ProxyFix
    print("[DEBUG] 所有第三方库导入成功", flush=True)
except ImportError as e:
    print(f"错误：无法导入依赖库。\n详情: {e}", flush=True)
    sys.exit(1)

# 计算 www 的绝对路径
TEMPLATE_DIR = os.path.abspath(os.path.join(BASE_DIR, '../www/templates'))
STATIC_DIR = os.path.abspath(os.path.join(BASE_DIR, '../www/static'))

# --- 环境配置 ---
os.environ['PYTHONIOENCODING'] = 'utf-8'
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

for encoding in ['UTF-8', 'utf-8', 'en_US.UTF-8', 'zh_CN.UTF-8']:
    try:
        locale.setlocale(locale.LC_ALL, f'en_US.{encoding}')
        break
    except:
        continue

# --- 参数解析 ---
parser = argparse.ArgumentParser(description='2FMusic Server')
parser.add_argument('--music-library-path', type=str, default=os.environ.get('MUSIC_LIBRARY_PATH'), help='Path to music library')
parser.add_argument('--log-path', type=str, default=os.environ.get('LOG_PATH'), help='Path to log file')
parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 28999)), help='Server port')
parser.add_argument('--password', type=str, default=os.environ.get('APP_AUTH_PASSWORD') or os.environ.get('APP_PASSWORD'),
                    help='Optional password for web access; leave empty to disable auth')
args = parser.parse_args()

# --- 路径初始化 ---
MUSIC_LIBRARY_PATH = args.music_library_path or os.getcwd()
os.makedirs(MUSIC_LIBRARY_PATH, exist_ok=True)
os.makedirs(os.path.join(MUSIC_LIBRARY_PATH, 'lyrics'), exist_ok=True)
os.makedirs(os.path.join(MUSIC_LIBRARY_PATH, 'covers'), exist_ok=True)

log_file = args.log_path or os.path.join(os.getcwd(), 'app.log')
os.makedirs(os.path.dirname(log_file), exist_ok=True)
DB_PATH = os.path.join(MUSIC_LIBRARY_PATH, 'data.db')

# --- 日志配置 ---
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.handlers.clear()
file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# 过滤 Werkzeug 访问日志，隐藏心跳检测的 200 响应
class AccessLogFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return not ('/api/system/status' in msg and '" 200 ' in msg)

logging.getLogger('werkzeug').addFilter(AccessLogFilter())

logger.info(f"Music Library Path: {MUSIC_LIBRARY_PATH}")

# --- 全局状态变量 ---
SCAN_STATUS = {
    'scanning': False,
    'total': 0,
    'processed': 0,
    'current_file': ''
}

# 库版本戳，用于前端检测变更
LIBRARY_VERSION = time.time()

# 辅助: 生成ID
def generate_song_id(path):
    return hashlib.md5(path.encode('utf-8')).hexdigest()

# --- 文件监听器 ---
class MusicFileEventHandler(FileSystemEventHandler):
    """监听音乐库文件变动"""
    def on_created(self, event):
        if event.is_directory: return
        self._process(event.src_path, 'created')

    def on_deleted(self, event):
        if event.is_directory: return
        self._process(event.src_path, 'deleted')

    def on_moved(self, event):
        if event.is_directory: return
        # 视为删除旧文件，添加新文件
        self._process(event.src_path, 'deleted')
        self._process(event.dest_path, 'created')

    def _process(self, path, action):
        global LIBRARY_VERSION
        filename = os.path.basename(path)
        ext = os.path.splitext(filename)[1].lower()
        
        is_audio = ext in AUDIO_EXTS
        is_misc = ext in ('.lrc', '.jpg', '.jpeg', '.png')
        
        if not is_audio and not is_misc:
            return

        logger.info(f"检测到文件变更 [{action}]: {filename}")
        
        try:
            if action == 'created':
                time.sleep(0.5)
                if is_audio:
                    index_single_file(path)
                elif is_misc:
                    # 如果是附件，尝试重新索引同名音频文件以更新状态
                    base = os.path.splitext(path)[0]
                    for aud in AUDIO_EXTS:
                        aud_path = base + aud
                        if os.path.exists(aud_path):
                            index_single_file(aud_path)
                            
            elif action == 'deleted':
                if is_audio:
                    with get_db() as conn:
                        conn.execute("DELETE FROM songs WHERE path=?", (path,))
                        conn.commit()
                elif is_misc:
                    # 附件删除，同样反向更新音频状态
                    base = os.path.splitext(path)[0]
                    for aud in AUDIO_EXTS:
                        aud_path = base + aud
                        if os.path.exists(aud_path):
                            index_single_file(aud_path)
            
            LIBRARY_VERSION = time.time()
            
        except Exception as e:
            logger.error(f"处理文件变更失败: {e}")

# 全局 Observer 实例
global_observer = None

def init_watchdog():
    global global_observer
    if not Observer: return
    
    if global_observer:
        global_observer.stop()
        global_observer.join()
        
    global_observer = Observer()
    refresh_watchdog_paths()
    global_observer.start()
    logger.info("文件监听服务已启动")
    try:
        while True:
            time.sleep(1)
    except:
        global_observer.stop()
    global_observer.join()

def refresh_watchdog_paths():
    """根据数据库刷新监听目录"""
    global global_observer
    if not global_observer: return
    
    # 1. 移除现有所有 schedule
    global_observer.unschedule_all()
    
    # 2. 获取目标路径
    # 2. 获取目标路径并去重
    try:
        raw_paths = {os.path.abspath(MUSIC_LIBRARY_PATH)}
        with get_db() as conn:
            rows = conn.execute("SELECT path FROM mount_points").fetchall()
            for r in rows: 
                if r['path']:
                    raw_paths.add(os.path.abspath(r['path']))
    except: 
        raw_paths = {os.path.abspath(MUSIC_LIBRARY_PATH)}

    # 路径规范化与去重 (排除子目录)
    sorted_paths = sorted(list(raw_paths), key=len)
    final_targets = []
    for p in sorted_paths:
        # 如果当前路径是已添加路径的子目录，则跳过
        if not any(p.startswith(parent + os.sep) or p == parent for parent in final_targets):
            final_targets.append(p)
    
    # 3. 重新添加 schedule
    event_handler = MusicFileEventHandler()
    for path in final_targets:
        if os.path.exists(path):
            try:
                global_observer.schedule(event_handler, path, recursive=True)
                logger.info(f"监听目录: {path}")
            except Exception as e:
                logger.warning(f"无法监听目录 {path}: {e}")


NETEASE_DOWNLOAD_DIR = os.path.join(MUSIC_LIBRARY_PATH, 'NetEase')
NETEASE_API_BASE_DEFAULT = 'http://localhost:28998'
NETEASE_API_BASE = None
NETEASE_COOKIE = None
NETEASE_MAX_CONCURRENT = 5
NETEASE_QUALITY_DEFAULT = 'exhigh'
# NETEASE_QUALITY = None # Configured quality - REMOVED

DOWNLOAD_TASKS = {} # task_id -> {status, progress, message, filename}

# QQ 音乐下载目录（提前声明，后面会被重新赋值）
QQMUSIC_DOWNLOAD_DIR = None

def get_default_download_dir():
    """获取默认下载目录：优先使用第一个挂载路径，否则使用音乐库目录"""
    try:
        with get_db() as conn:
            row = conn.execute("SELECT path FROM mount_points ORDER BY created_at ASC LIMIT 1").fetchone()
            if row and row['path'] and os.path.exists(row['path']):
                return row['path']
    except Exception:
        pass
    return MUSIC_LIBRARY_PATH


# 修复路径问题
app = Flask(__name__, static_folder=STATIC_DIR, template_folder=TEMPLATE_DIR)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
# 配置静态文件缓存过期时间为 1 年 (31536000 秒)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000
app.secret_key = os.environ.get('APP_SECRET_KEY', 'xiaosongshu_secret')
app.permanent_session_lifetime = timedelta(days=30)

@app.route('/favicon.ico')
def favicon():
    return send_file(os.path.join(STATIC_DIR, 'images', 'ICON_256.PNG'), mimetype='image/png')

APP_AUTH_PASSWORD = args.password  # 管理员密码

# --- 多用户系统 ---
USER_DATA_DIR = os.path.join(MUSIC_LIBRARY_PATH, 'user_data')
os.makedirs(USER_DATA_DIR, exist_ok=True)

def validate_password(password: str) -> tuple:
    """验证密码格式：6位以上，必须包含数字和字母"""
    if len(password) < 6:
        return False, '密码必须至少6位'
    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not has_letter or not has_digit:
        return False, '密码必须包含数字和字母'
    return True, ''

def get_user_file_path(password_hash: str) -> str:
    """获取用户数据文件路径"""
    return os.path.join(USER_DATA_DIR, f"{password_hash[:16]}.json")

def load_user_data(password_hash: str) -> dict:
    """加载用户数据"""
    file_path = get_user_file_path(password_hash)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None

def save_user_data(password_hash: str, data: dict):
    """保存用户数据"""
    file_path = get_user_file_path(password_hash)
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"保存用户数据失败: {e}")
        return False

def create_user(password_hash: str, is_admin: bool = False) -> dict:
    """创建新用户"""
    user_data = {
        'password_hash': password_hash,
        'is_admin': is_admin,
        'favorites': [],
        'playlists': [],
        'created_at': time.time()
    }
    save_user_data(password_hash, user_data)
    return user_data

def get_current_user() -> dict:
    """获取当前登录用户的数据"""
    password_hash = session.get('user_hash')
    if not password_hash:
        return None
    return load_user_data(password_hash)

def init_admin_user():
    """初始化管理员用户"""
    if not APP_AUTH_PASSWORD:
        return
    admin_hash = hashlib.sha256(APP_AUTH_PASSWORD.encode()).hexdigest()
    if not load_user_data(admin_hash):
        create_user(admin_hash, is_admin=True)
        logger.info("管理员用户已创建")

# 启动时初始化管理员
init_admin_user()

def _auth_failed():
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'unauthorized'}), 401
    return redirect(url_for('login', next=request.path))

@app.before_request
def require_auth():
    path = request.path or ''
    # 静态资源和登录/注册页面不需要认证
    if path.startswith('/static') or path.startswith('/login') or path.startswith('/register') or path == '/favicon.ico':
        return
    
    # 预览功能允许的 API 路径（用于 CGI 代理和移动端 APP）
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
    ]
    
    # 检查是否是预览相关的 API 请求
    is_preview_api = any(path.startswith(p) for p in preview_allowed_paths)
    
    # 外部点歌 API 白名单（无需认证）
    external_api_paths = [
        '/api/qqmusic/search',
        '/api/qqmusic/song/url',
        '/api/netease/search',
    ]
    is_external_api = any(path.startswith(p) for p in external_api_paths)
    if is_external_api:
        return  # 允许外部直接访问搜索和播放链接 API
    
    if is_preview_api:
        # 来自 CGI 代理的请求（通过 X-Forwarded-Prefix 识别）
        forwarded_prefix = request.headers.get('X-Forwarded-Prefix', '')
        if 'index.cgi' in forwarded_prefix:
            return
        
        # 来自飞牛移动端 APP 的请求（通过 User-Agent 或 Referer 识别）
        user_agent = request.headers.get('User-Agent', '').lower()
        referer = request.headers.get('Referer', '').lower()
        # 飞牛 APP 或预览页面的请求
        if 'fnos' in user_agent or 'fnnas' in user_agent or 'preview' in referer or 'index.cgi' in referer:
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

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    next_path = request.args.get('next') or '/'
    if request.method == 'POST':
        pwd = request.form.get('password') or ''
        mode = request.form.get('mode', 'login')  # login 或 register
        raw_pwd = request.form.get('raw_password') or pwd
        
        # 前端已经做了 SHA256，这里直接使用
        if len(pwd) != 64:
            password_hash = hashlib.sha256(pwd.encode()).hexdigest()
        else:
            password_hash = pwd.lower()
        
        if mode == 'register':
            # 注册模式
            valid, msg = validate_password(raw_pwd)
            if not valid:
                error = msg
            elif load_user_data(password_hash):
                error = '该密码已被注册'
            else:
                create_user(password_hash, is_admin=False)
                # 注册成功后自动登录
                session['authed'] = True
                session['user_hash'] = password_hash
                session['is_admin'] = False
                if request.form.get('remember'):
                    session.permanent = True
                return redirect(next_path)
        else:
            # 登录模式
            user_data = load_user_data(password_hash)
            if user_data:
                session['authed'] = True
                session['user_hash'] = password_hash
                session['is_admin'] = user_data.get('is_admin', False)
                if request.form.get('remember'):
                    session.permanent = True
                else:
                    session.permanent = False
                return redirect(next_path)
            else:
                error = '密码不存在，请先注册'
    return render_template('login.html', error=error, next_path=next_path)

@app.route('/register', methods=['GET', 'POST'])
def register():
    # 注册功能已整合到登录页面，重定向到登录页
    return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.pop('authed', None)
    session.pop('user_hash', None)
    session.pop('is_admin', None)
    session.clear()
    resp = make_response(redirect(url_for('login')))
    resp.delete_cookie(app.config.get('SESSION_COOKIE_NAME', 'session'))
    return resp

@app.route('/api/user/info')
def get_user_info():
    """获取当前用户信息"""
    user = get_current_user()
    if not user:
        return jsonify({'success': False, 'error': 'not logged in'})
    return jsonify({
        'success': True,
        'is_admin': user.get('is_admin', False),
        'created_at': user.get('created_at')
    })

# --- 数据库管理 ---
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    def _init_db_core():
        with get_db() as conn:
            # 检查旧模式并迁移
            try:
                cursor = conn.execute("SELECT path FROM songs LIMIT 1")
            except Exception:
                conn.execute("DROP TABLE IF EXISTS songs")
                conn.execute("DROP TABLE IF EXISTS mount_files")

            conn.execute('''
                CREATE TABLE IF NOT EXISTS songs (
                    id TEXT PRIMARY KEY,
                    path TEXT UNIQUE,
                    filename TEXT,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    mtime REAL,
                    size INTEGER,
                    has_cover INTEGER DEFAULT 0
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS favorites (
                   song_id TEXT PRIMARY KEY,
                   created_at REAL
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS mount_points (
                    path TEXT PRIMARY KEY,
                    created_at REAL
                )
            ''')
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            
            # 本地歌单表 (添加 user_hash 字段支持多用户)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    cover TEXT,
                    created_at REAL,
                    updated_at REAL,
                    user_hash TEXT DEFAULT ''
                )
            ''')
            
            # 迁移：为旧表添加 user_hash 字段
            try:
                conn.execute("ALTER TABLE playlists ADD COLUMN user_hash TEXT DEFAULT ''")
            except: pass
            
            # 迁移：为歌单添加源链接字段（用于同步）
            try:
                conn.execute("ALTER TABLE playlists ADD COLUMN source_url TEXT")
            except: pass
            try:
                conn.execute("ALTER TABLE playlists ADD COLUMN source_type TEXT")
            except: pass
            try:
                conn.execute("ALTER TABLE playlists ADD COLUMN last_synced_at REAL")
            except: pass
            
            # 歌单歌曲关联表
            conn.execute('''
                CREATE TABLE IF NOT EXISTS playlist_songs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL,
                    song_id TEXT NOT NULL,
                    added_at REAL,
                    sort_order INTEGER DEFAULT 0,
                    FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
                    UNIQUE(playlist_id, song_id)
                )
            ''')
            
            # 歌单待下载歌曲表（存储本地没有的歌曲元信息）
            conn.execute('''
                CREATE TABLE IF NOT EXISTS playlist_pending_songs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL,
                    qq_mid TEXT,
                    netease_id TEXT,
                    title TEXT NOT NULL,
                    artist TEXT,
                    album TEXT,
                    cover TEXT,
                    source TEXT DEFAULT 'qq',
                    added_at REAL,
                    sort_order INTEGER DEFAULT 0,
                    FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
                    UNIQUE(playlist_id, qq_mid),
                    UNIQUE(playlist_id, netease_id)
                )
            ''')
            
            # 迁移：为旧表添加 sort_order 字段
            try:
                conn.execute('ALTER TABLE playlist_songs ADD COLUMN sort_order INTEGER DEFAULT 0')
            except: pass
            try:
                conn.execute('ALTER TABLE playlist_pending_songs ADD COLUMN sort_order INTEGER DEFAULT 0')
            except: pass
            
            # 清理错误索引的非音频文件
            try:
                placeholders = ' OR '.join([f"filename NOT LIKE '%{ext}'" for ext in AUDIO_EXTS])
                conn.execute(f"DELETE FROM songs WHERE {placeholders}")
            except: pass
            
            conn.commit()

    try:
        _init_db_core()
        logger.info("数据库初始化完成。")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}，尝试重建数据库...")
        try:
            if os.path.exists(DB_PATH):
                os.remove(DB_PATH)
            _init_db_core()
            logger.info("数据库重建完成。")
        except Exception as e2:
             logger.exception(f"数据库重建失败: {e2}")

# --- 元数据提取 ---
# 无效的元数据值（音乐平台名称等）
INVALID_METADATA_VALUES = {'kuwo', 'kugou', 'qqmusic', 'netease', 'xiami', 'unknown', '未知', '未知艺术家'}

def _is_valid_metadata(value):
    """检查元数据值是否有效（不是平台名称等无效值）"""
    if not value:
        return False
    val_lower = value.strip().lower()
    # 检查是否是无效值
    if val_lower in INVALID_METADATA_VALUES:
        return False
    # 检查是否只包含数字（可能是ID）
    if val_lower.isdigit():
        return False
    return True

def get_metadata(file_path):
    metadata = {'title': None, 'artist': None, 'album': None}
    try:
        audio = None
        try:
            audio = EasyID3(file_path)
        except Exception as e1:
            try:
                audio = File(file_path, easy=True)
            except Exception as e2:
                try:
                    # 最后尝试不使用 easy 模式
                    audio = File(file_path)
                except Exception as e3:
                    # 文件可能损坏，跳过元数据提取
                    logger.warning(f"文件 {file_path} 无法解析，可能已损坏: {e3}")
                    audio = None
        if audio:
            def get_tag(key):
                if hasattr(audio, 'get'):
                    val = audio.get(key)
                    if isinstance(val, list): 
                        val = val[0] if val else None
                    # 确保返回字符串类型（处理 ASFUnicodeAttribute 等特殊类型）
                    if val is not None:
                        return str(val)
                    return val
                return None
            # 获取元数据，但过滤无效值
            title_val = get_tag('title')
            artist_val = get_tag('artist')
            album_val = get_tag('album')
            
            metadata['title'] = title_val if _is_valid_metadata(title_val) else None
            metadata['artist'] = artist_val if _is_valid_metadata(artist_val) else None
            metadata['album'] = album_val if _is_valid_metadata(album_val) else None
    except Exception as e:
        # 捕获所有异常，确保不会因为单个文件导致整个扫描失败
        logger.warning(f"提取元数据失败: {file_path}, 错误: {e}")
    
    filename = os.path.splitext(os.path.basename(file_path))[0]
    
    # 如果标题无效，尝试从文件名解析
    if not metadata['title']:
        if ' - ' in filename:
            parts = filename.split(' - ', 1)
            parsed_artist = parts[0].strip()
            parsed_title = parts[1].strip()
            # 只有当解析出的值有效时才使用
            if _is_valid_metadata(parsed_title):
                metadata['title'] = parsed_title
            if not metadata['artist'] and _is_valid_metadata(parsed_artist):
                metadata['artist'] = parsed_artist
        
        # 如果还是没有标题，使用文件名（但过滤无效值）
        if not metadata['title']:
            if _is_valid_metadata(filename):
                metadata['title'] = filename
            else:
                # 文件名也是无效值，使用"未知歌曲"
                metadata['title'] = "未知歌曲"
    
    if not metadata['artist']: 
        metadata['artist'] = "未知艺术家"
    
    logger.debug(f"文件 {file_path} 元数据: {metadata}")
    return metadata

def extract_embedded_cover(file_path: str, base_name: str = None, target_dir: str = None):
    """提取音频内嵌封面并保存为 covers/<base_name>.jpg，成功返回 True。"""
    try:
        if not os.path.exists(file_path):
            return False
        base_name = base_name or os.path.splitext(os.path.basename(file_path))[0]
        # 优先使用指定目录，否则使用挂载目录
        cover_base_dir = target_dir or get_default_download_dir()
        cover_dir = os.path.join(cover_base_dir, 'covers')
        os.makedirs(cover_dir, exist_ok=True)
        target_path = os.path.join(cover_dir, f"{base_name}.jpg")
        if os.path.exists(target_path):
            return True

        audio = File(file_path)
        if not audio:
            return False

        data = None

        # MP3 / ID3
        if hasattr(audio, 'tags') and audio.tags:
            if hasattr(audio.tags, 'getall'):
                for tag in audio.tags.getall('APIC'):
                    if getattr(tag, 'data', None):
                        data = tag.data
                        break
            if not data:
                covr = audio.tags.get('covr')
                if covr:
                    val = covr[0] if isinstance(covr, (list, tuple)) else covr
                    try:
                        data = bytes(val)
                    except Exception:
                        pass

        # FLAC / 其他
        if not data and hasattr(audio, 'pictures'):
            pics = getattr(audio, 'pictures') or []
            if pics:
                data = pics[0].data

        if not data:
            logger.info(f"未找到内嵌封面: {file_path}")
            return False

        with open(target_path, 'wb') as f:
            f.write(data)
        logger.info(f"内嵌封面提取并保存: {target_path}")
        return True
    except Exception as e:
        logger.warning(f"提取内嵌封面失败: {file_path}, 错误: {repr(e)}")
        return False

def extract_embedded_lyrics(file_path: str):
    """提取音频内嵌歌词，返回歌词字符串或 None。"""
    try:
        if not os.path.exists(file_path):
            return None
        
        audio = File(file_path)
        if not audio:
            return None

        # 1. MP3 / ID3 (USLT)
        if hasattr(audio, 'tags') and isinstance(audio.tags, ID3):
            for key in audio.tags.keys():
                if key.startswith('USLT'):
                    return audio.tags[key].text
        
        # 2. FLAC / Vorbis Comments
        if hasattr(audio, 'tags'):
            lyrics = audio.tags.get('lyrics') or audio.tags.get('LYRICS') or audio.tags.get('unsyncedlyrics') or audio.tags.get('UNSYNCEDLYRICS')
            if lyrics:
                return lyrics[0]
                
        # 3. M4A / MP4
        if hasattr(audio, 'tags') and '©lyr' in audio.tags:
             return audio.tags['©lyr'][0]

    except Exception as e:
        logger.warning(f"提取内嵌歌词失败: {file_path}, 错误: {repr(e)}")
    return None

# --- 通用请求头 (提前定义，供后续函数使用) ---
COMMON_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Authorization': '2FMusic'
}

def fetch_cover_bytes(url: str):
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=8, headers=COMMON_HEADERS)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except Exception as e:
        logger.warning(f"封面下载失败: {url}, 错误: {e}")
    return None

def embed_cover_to_file(audio_path: str, cover_bytes: bytes):
    """将封面嵌入音频文件（支持 mp3/flac/m4a）。"""
    if not cover_bytes or not os.path.exists(audio_path):
        return
    ext = os.path.splitext(audio_path)[1].lower()
    try:
        if ext == '.mp3':
            audio = None
            try:
                audio = ID3(audio_path)
            except Exception:
                audio = File(audio_path)
                audio.add_tags()
                audio.save()
                audio = ID3(audio_path)
            if audio:
                audio.delall('APIC')
                audio.add(APIC(mime='image/jpeg', type=3, desc='Cover', data=cover_bytes))
                audio.save()
        elif ext == '.flac':
            audio = FLAC(audio_path)
            pic = Picture()
            pic.data = cover_bytes
            pic.type = 3
            pic.mime = 'image/jpeg'
            audio.clear_pictures()
            audio.add_picture(pic)
            audio.save()
        elif ext in ('.m4a', '.m4b', '.m4p'):
            audio = MP4(audio_path)
            fmt = MP4Cover.FORMAT_JPEG
            if cover_bytes.startswith(b'\x89PNG'):
                fmt = MP4Cover.FORMAT_PNG
            audio['covr'] = [MP4Cover(cover_bytes, fmt)]
            audio.save()
    except Exception as e:
        logger.warning(f"内嵌封面失败: {audio_path}, 错误: {e}")

def save_cover_file(cover_bytes: bytes, base_name: str, target_dir: str = None):
    """保存封面文件到指定目录或默认目录"""
    if not cover_bytes or not base_name:
        return None
    try:
        # 优先使用指定目录，否则使用默认挂载目录
        base_dir = target_dir or get_default_download_dir()
        cover_dir = os.path.join(base_dir, 'covers')
        os.makedirs(cover_dir, exist_ok=True)
        cover_path = os.path.join(cover_dir, f"{base_name}.jpg")
        with open(cover_path, 'wb') as f:
            f.write(cover_bytes)
        return cover_path
    except Exception as e:
        logger.warning(f"封面保存失败: {base_name}, 错误: {e}")
        return None

def fetch_netease_lyrics(song_id: str):
    """返回 (lrc, yrc) 字符串；若无则为 None。"""
    if not song_id:
        return None, None
    lrc_text = None
    yrc_text = None
    try:
        lyr_resp = call_netease_api('/lyric/new', {'id': song_id}, need_cookie=False)
        if isinstance(lyr_resp, dict):
            yrc_text = (lyr_resp.get('yrc') or {}).get('lyric')
            lrc_text = (lyr_resp.get('lrc') or {}).get('lyric')
        if not lrc_text:
            old_resp = call_netease_api('/lyric', {'id': song_id}, need_cookie=False)
            if isinstance(old_resp, dict):
                lrc_text = (old_resp.get('lrc') or {}).get('lyric') or lrc_text
                if not yrc_text:
                    yrc_text = (old_resp.get('yrc') or {}).get('lyric')
    except Exception as e:
        logger.warning(f"获取网易歌词失败: {e}")
    return lrc_text, yrc_text

def embed_lyrics_to_file(audio_path: str, lrc_text: str):
    """将歌词嵌入音频（行级歌词）。"""
    if not lrc_text or not os.path.exists(audio_path):
        return
    ext = os.path.splitext(audio_path)[1].lower()
    try:
        if ext == '.mp3':
            try:
                tags = ID3(audio_path)
            except Exception:
                tags = File(audio_path)
                tags.add_tags()
                tags.save()
                tags = ID3(audio_path)
            tags.delall('USLT')
            tags.add(USLT(encoding=3, lang='chi', desc='Lyric', text=lrc_text))
            tags.save()
        elif ext == '.flac':
            audio = FLAC(audio_path)
            audio['LYRICS'] = lrc_text
            audio.save()
        elif ext in ('.m4a', '.m4b', '.m4p'):
            audio = MP4(audio_path)
            audio['\xa9lyr'] = lrc_text
            audio.save()
        elif ext in ('.ogg', '.oga'):
            audio = File(audio_path)
            audio['LYRICS'] = lrc_text
            audio.save()
    except Exception as e:
        logger.warning(f"内嵌歌词失败: {audio_path}, 错误: {e}")

AUDIO_EXTS = ('.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a')

def check_cover_exists(file_path: str, base_name: str = None) -> bool:
    """检查封面是否存在，搜索所有可能的位置"""
    if not base_name:
        base_name = os.path.splitext(os.path.basename(file_path))[0]
    base_path = os.path.splitext(file_path)[0]
    
    # 1. 歌曲同目录下的同名 jpg
    if os.path.exists(base_path + ".jpg"):
        return True
    
    # 2. 歌曲所在目录的 covers 子目录
    song_dir = os.path.dirname(file_path)
    if os.path.exists(os.path.join(song_dir, 'covers', f"{base_name}.jpg")):
        return True
    
    # 3. 所有挂载目录的 covers 子目录
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT path FROM mount_points").fetchall()
            for r in rows:
                if r['path'] and os.path.exists(os.path.join(r['path'], 'covers', f"{base_name}.jpg")):
                    return True
    except Exception:
        pass
    
    # 4. 默认音乐库目录的 covers 子目录
    if os.path.exists(os.path.join(MUSIC_LIBRARY_PATH, 'covers', f"{base_name}.jpg")):
        return True
    
    return False

def index_single_file(file_path):
    """单独索引一个文件。"""
    try:
        if not os.path.exists(file_path): return
        # 严格限制只能索引音频文件
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in AUDIO_EXTS: return
        
        stat = os.stat(file_path)
        meta = get_metadata(file_path)
        sid = generate_song_id(file_path)
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        
        # 使用统一的封面检测函数
        has_cover = 1 if check_cover_exists(file_path, base_name) else 0
        if not has_cover:
            # 尝试提取内嵌封面
            if extract_embedded_cover(file_path, base_name):
                has_cover = 1
        
        with get_db() as conn:
            # 全局去重检测
            dup = conn.execute("SELECT path FROM songs WHERE filename=? AND size=? AND path!=?", (os.path.basename(file_path), stat.st_size, file_path)).fetchone()
            if dup:
                logger.info(f"索引: 跳过重复文件 {file_path} (已存在: {dup['path']})")
                return

            conn.execute('''
                INSERT OR REPLACE INTO songs (id, path, filename, title, artist, album, mtime, size, has_cover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (sid, file_path, os.path.basename(file_path), meta['title'], meta['artist'], meta['album'], stat.st_mtime, stat.st_size, has_cover))
            conn.commit()
        logger.info(f"单文件索引完成: {file_path}")
    except Exception as e:
        logger.error(f"单文件索引失败: {e}")

# --- 优化后的并发扫描逻辑 ---
def scan_library_incremental():
    global SCAN_STATUS
    
    lock_file = os.path.join(MUSIC_LIBRARY_PATH, '.scan_lock')
    if os.path.exists(lock_file):
        if time.time() - os.path.getmtime(lock_file) > 300:
            try:
                os.remove(lock_file)
                logger.info("过期扫描锁文件已移除。")
            except Exception as e:
                logger.warning(f"移除扫描锁文件失败: {e}")
        else:
            return 

    try:
        # 更新状态：开始
        SCAN_STATUS.update({'scanning': True, 'total': 0, 'processed': 0, 'current_file': '正在遍历文件...'})
        
        with open(lock_file, 'w') as f: f.write(str(time.time()))
        logger.info("开始增量扫描...")
        
        # 1. 获取所有扫描根目录
        scan_roots = [MUSIC_LIBRARY_PATH]
        try:
            with get_db() as conn:
                rows = conn.execute("SELECT path FROM mount_points").fetchall()
                scan_roots.extend([r['path'] for r in rows])
        except Exception: pass
        
        disk_files = {} # path -> info
        supported_exts = AUDIO_EXTS
        
        # 2. 遍历所有目录
        for root_dir in scan_roots:
            if not os.path.exists(root_dir): continue
            for root, dirs, files in os.walk(root_dir):
                # 排除自动生成的目录
                dirs[:] = [d for d in dirs if d not in ('lyrics', 'covers')]
                for f in files:
                    if f.lower().endswith(supported_exts):
                        path = os.path.join(root, f)
                        try:
                            stat = os.stat(path)
                            info = {'mtime': stat.st_mtime, 'size': stat.st_size, 'path': path, 'filename': f}
                            disk_files[path] = info
                        except: pass

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, path, mtime, size FROM songs")
            db_rows = {row['path']: row for row in cursor.fetchall()}
            
            # 删除不存在的文件
            # 注意：如果某个挂载点被临时拔出，这里会删除其歌曲。
            # 简单起见：全量比对，消失即删除。
            to_delete_paths = set(db_rows.keys()) - set(disk_files.keys())
            if to_delete_paths:
                cursor.executemany("DELETE FROM songs WHERE path=?", [(p,) for p in to_delete_paths])
                conn.commit()

            # 筛选需要更新的文件
            files_to_process_list = []
            for path, info in disk_files.items():
                db_rec = db_rows.get(path)
                if not db_rec or db_rec['mtime'] != info['mtime'] or db_rec['size'] != info['size']:
                    files_to_process_list.append(info)

            # 更新状态
            total_files = len(files_to_process_list)
            SCAN_STATUS.update({'total': total_files, 'processed': 0})
            
            to_update_db = []
            
            # 3. 多线程处理
            if total_files > 0:
                logger.info(f"使用线程池处理 {total_files} 个文件...")
                
                def process_file_metadata(info):
                    meta = get_metadata(info['path'])
                    sid = generate_song_id(info['path'])
                    base_name = os.path.splitext(info['filename'])[0]
                    # 使用统一的封面检测函数
                    has_cover = 1 if check_cover_exists(info['path'], base_name) else 0
                    return (sid, info['path'], info['filename'], meta['title'], meta['artist'], meta['album'], info['mtime'], info['size'], has_cover)

                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {executor.submit(process_file_metadata, item): item for item in files_to_process_list}
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            res = future.result()
                            to_update_db.append(res)
                        except Exception: pass
                        
                        SCAN_STATUS['processed'] += 1
                        if SCAN_STATUS['processed'] % 10 == 0:
                            SCAN_STATUS['current_file'] = f"处理中... {int((SCAN_STATUS['processed']/total_files)*100)}%"

                # 过滤重复文件 (批次内去重 + 数据库去重)
                final_update_db = []
                seen_in_batch = set() # (filename, size)

                for item in to_update_db:
                    # structure: (sid, path, filename, title, artist, album, mtime, size, has_cover)
                    # item[1]=path, item[2]=filename, item[7]=size
                    c_path, c_fname, c_size = item[1], item[2], item[7]
                    
                    # 1. 批次内查重
                    if (c_fname, c_size) in seen_in_batch:
                        logger.info(f"扫描: 跳过批次内重复文件 {c_path}")
                        continue
                        
                    # 2. 数据库查重 (排除自己)
                    # 注意: 这里使用 conn (外层已开启)
                    # 需要确保 conn 线程安全? sqlite3 单线程模式下需要注意。
                    # 但 Flask 这里的 conn 是 thread-local 还是? 
                    # scan_library_incremental 是后台任务，单线程执行 (executor 是处理 metadata 的)。
                    # "with get_db() as conn" 在上层。所以是安全的。
                    try:
                        dup = conn.execute("SELECT path FROM songs WHERE filename=? AND size=? AND path!=?", (c_fname, c_size, c_path)).fetchone()
                        if dup:
                            logger.info(f"扫描: 跳过全局重复文件 {c_path} (已存在: {dup['path']})")
                            continue
                    except Exception: pass
                    
                    seen_in_batch.add((c_fname, c_size))
                    final_update_db.append(item)

                if final_update_db:
                    cursor.executemany('''
                        INSERT OR REPLACE INTO songs (id, path, filename, title, artist, album, mtime, size, has_cover)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', final_update_db)
                    conn.commit()

        logger.info("扫描完成。")
        global LIBRARY_VERSION; LIBRARY_VERSION = time.time()
        
    except Exception as e:
        logger.error(f"扫描失败: {e}")
    finally:
        SCAN_STATUS['scanning'] = False
        SCAN_STATUS['current_file'] = ''
        if os.path.exists(lock_file): 
            try: os.remove(lock_file)
            except: pass

threading.Thread(target=lambda: (init_db(), scan_library_incremental()), daemon=True).start()
threading.Thread(target=init_watchdog, daemon=True).start()

# --- 路由定义 ---
@app.route('/')
def index():
    is_admin = session.get('is_admin', False)
    return render_template('index.html', is_admin=is_admin)

# --- 系统状态接口 ---
@app.route('/api/system/status')
def get_system_status():
    """返回当前扫描状态和进度"""
    status = dict(SCAN_STATUS)
    status['library_version'] = LIBRARY_VERSION
    return jsonify(status)

@app.route('/api/music', methods=['GET'])
def get_music_list():
    logger.info("API请求: 获取音乐列表")
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM songs ORDER BY title")
            songs = []
            seen = set()
            
            for row in cursor.fetchall():
                # 去重逻辑：如果 标题+歌手+大小 完全一致，视为重复文件，仅保留第一个
                # 这样可以解决不同目录下存放相同文件导致的列表重复问题
                unique_key = (row['title'], row['artist'], row['size'])
                if unique_key in seen:
                    continue
                seen.add(unique_key)
                
                album_art = None
                if row['has_cover']:
                    base_name = os.path.splitext(row['filename'])[0]
                    # 封面图链接带上 filename 参数仅作缓存区分，实际通过 scan 查找
                    album_art = f"/api/music/covers/{quote(base_name)}.jpg?filename={quote(row['filename'])}"
                songs.append({
                    'id': row['id'], # 新增 ID
                    'filename': row['filename'], 'title': row['title'],
                    'artist': row['artist'], 'album': row['album'], 'album_art': album_art
                })
        logger.info(f"返回音乐数量: {len(songs)}")
        return jsonify({'success': True, 'data': songs})
    except Exception as e:
        logger.exception(f"获取音乐列表失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/music/play/<song_id>')
def play_music(song_id):
    logger.info(f"API请求: 播放音乐 ID={song_id}")
    try:
        with get_db() as conn:
            row = conn.execute("SELECT path FROM songs WHERE id=?", (song_id,)).fetchone()
            if row and os.path.exists(row['path']):
                return send_file(row['path'], conditional=True)
            
    except Exception as e:
        logger.error(f"播放失败: {e}")

    logger.warning(f"文件未找到或ID无效: {song_id}")
    return jsonify({'error': 'Not Found'}), 404

# --- 库管理 ---
@app.route('/api/library/rescan', methods=['POST'])
def rescan_library():
    """强制重新扫描所有音乐目录，更新元数据"""
    logger.info("API请求: 重新扫描音乐库")
    try:
        # 清空数据库中的歌曲记录，强制重新索引
        with get_db() as conn:
            conn.execute("DELETE FROM songs")
            conn.commit()
        
        # 启动后台扫描
        threading.Thread(target=scan_library_incremental, daemon=True).start()
        return jsonify({'success': True, 'message': '已开始重新扫描'})
    except Exception as e:
        logger.error(f"重新扫描失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

# --- 挂载相关 ---
@app.route('/api/mount_points', methods=['GET'])
def list_mount_points():
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT path FROM mount_points ORDER BY created_at DESC").fetchall()
            return jsonify({'success': True, 'data': [row['path'] for row in rows]})
    except Exception as e: return jsonify({'success': False, 'error': str(e)})

def check_has_music(path):
    """检查目录是否包含音乐文件"""
    try:
        for root, _, files in os.walk(path):
            for f in files:
                if f.lower().endswith(AUDIO_EXTS):
                    return True
    except Exception:
        pass
    return False

@app.route('/api/mount_points', methods=['POST'])
def add_mount_point():
    logger.info("API请求: 添加挂载点")
    try:
        path = request.json.get('path')
        if not path or not os.path.exists(path):
            return jsonify({'success': False, 'error': '路径不存在'})
            
        path = os.path.abspath(path)

        # 校验目录内容
        if not check_has_music(path):
            return jsonify({'success': False, 'error': '该目录及其子目录中未发现可识别的音乐文件'})
        
        with get_db() as conn:
            if conn.execute("SELECT 1 FROM mount_points WHERE path=?", (path,)).fetchone():
                return jsonify({'success': False, 'error': '已挂载'})
            conn.execute("INSERT INTO mount_points (path, created_at) VALUES (?, ?)", (path, time.time()))
            conn.commit()

        # 刷新监听并触发扫描
        refresh_watchdog_paths()
        threading.Thread(target=scan_library_incremental, daemon=True).start()
        
        return jsonify({'success': True, 'message': '挂载点已添加，正在后台处理...'})
    except Exception as e:
        logger.exception(f"添加挂载点失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/mount_points', methods=['DELETE'])
def remove_mount_point():
    try:
        path = request.json.get('path')
        with get_db() as conn:
            # 清理该路径下的歌曲
            conn.execute("DELETE FROM songs WHERE path LIKE ? || '%'", (path,))
            conn.execute("DELETE FROM mount_points WHERE path=?", (path,))
            conn.commit()
            
        refresh_watchdog_paths()
        
        # 触发一次库版本更新
        global LIBRARY_VERSION; LIBRARY_VERSION = time.time()
            
        return jsonify({'success': True, 'message': '已移除'})
    except Exception as e: return jsonify({'success': False, 'error': str(e)})

# --- 资源获取 ---
NETEASE_API_BASE_DEFAULT = os.environ.get('NETEASE_API_BASE', 'http://localhost:28998')
NETEASE_API_BASE = NETEASE_API_BASE_DEFAULT
NETEASE_DOWNLOAD_DIR = os.environ.get('NETEASE_DOWNLOAD_PATH', MUSIC_LIBRARY_PATH)
NETEASE_COOKIE = None
NETEASE_MAX_CONCURRENT = 20
LYRICS_DIR = os.path.join(MUSIC_LIBRARY_PATH, 'lyrics')

os.makedirs(LYRICS_DIR, exist_ok=True)

def parse_cookie_string(cookie_str: str):
    """将 Set-Cookie 字符串解析为 requests 兼容的字典。"""
    if not cookie_str: 
        return {}
    cookies = {}
    # 只取 key=value 形式，忽略 Path/Expires 等属性
    for part in cookie_str.split(';'):
        if '=' in part:
            k, v = part.strip().split('=', 1)
            if k.lower() in ('path', 'expires', 'max-age', 'domain', 'samesite', 'secure'): 
                continue
            cookies[k] = v
    return cookies

def normalize_cookie_string(raw: str) -> str:
    """规范化 cookie 字符串，移除换行并过滤非关键属性。"""
    if not raw: 
        return ''
    parts = []
    # 常见的 Set-Cookie 属性，不应出现在请求头 Cookie 中
    skip_keys = ('path', 'expires', 'max-age', 'domain', 'samesite', 'secure', 'httponly')
    
    for part in raw.replace('\n', ';').split(';'):
        part = part.strip()
        if not part: continue
        
        # 忽略没有等号的属性 (如 Secure, HttpOnly)
        if '=' not in part: 
            # 但有些 cookie 值可能就是没有等号？不太可能，标准cookie都是 k=v
            # 如果是 Secure/HttpOnly 这种flag，肯定要忽略
            continue
            
        k, v = part.split('=', 1)
        if k.strip().lower() in skip_keys:
            continue
            
        parts.append(part)
        
    return '; '.join(parts)

def load_netease_cookie():
    global NETEASE_COOKIE
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM system_settings WHERE key='netease_cookie'").fetchone()
            if row and row['value']:
                NETEASE_COOKIE = normalize_cookie_string(row['value'])
    except Exception as e:
        logger.warning(f"读取网易云 cookie 失败: {e}")

def save_netease_cookie(cookie_str: str):
    global NETEASE_COOKIE
    NETEASE_COOKIE = normalize_cookie_string(cookie_str or '')
    try:
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", ('netease_cookie', NETEASE_COOKIE))
            conn.commit()
    except Exception as e:
        logger.warning(f"保存网易云 cookie 失败: {e}")

def load_netease_config():
    global NETEASE_DOWNLOAD_DIR, NETEASE_API_BASE
    try:
        with get_db() as conn:
            # Download Dir - 优先使用已保存的配置，否则使用挂载目录
            row = conn.execute("SELECT value FROM system_settings WHERE key='netease_download_dir'").fetchone()
            if row and row['value']: 
                NETEASE_DOWNLOAD_DIR = row['value']
            else:
                # 默认使用挂载目录
                NETEASE_DOWNLOAD_DIR = get_default_download_dir()
            
            # API Base
            row = conn.execute("SELECT value FROM system_settings WHERE key='netease_api_base'").fetchone()
            if row and row['value']: NETEASE_API_BASE = row['value']
            
            # Quality - REMOVED
            
    except Exception as e:
        logger.warning(f"读取网易云配置失败: {e}")

def save_netease_config(download_dir: str = None, api_base: str = None): # Removed quality parameter
    global NETEASE_DOWNLOAD_DIR, NETEASE_API_BASE
    if download_dir: NETEASE_DOWNLOAD_DIR = download_dir
    if api_base: NETEASE_API_BASE = api_base.rstrip('/') or NETEASE_API_BASE_DEFAULT
    # if quality: NETEASE_QUALITY = quality # Removed quality processing
    
    try:
        with get_db() as conn:
            if download_dir:
                conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", ('netease_download_dir', NETEASE_DOWNLOAD_DIR))
            if api_base:
                conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", ('netease_api_base', NETEASE_API_BASE))
            # if quality: # Removed quality processing
            #     conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", ('netease_quality', NETEASE_QUALITY))
            conn.commit()
    except Exception as e:
        logger.warning(f"保存网易云配置失败: {e}")

def sanitize_filename(name: str) -> str:
    """移除非法字符，避免文件名错误。"""
    cleaned = re.sub(r'[\\/:*?"<>|]+', '_', name).strip().strip('.')
    return cleaned or 'netease_song'

def call_netease_api(path: str, params: dict, method: str = 'GET', need_cookie: bool = True):
    """调用本地网易云 API，统一处理错误。"""
    base = (NETEASE_API_BASE or NETEASE_API_BASE_DEFAULT).rstrip('/')
    url = f"{base}{path}"
    headers = dict(COMMON_HEADERS)
    params = dict(params or {})
    cookies = {}
    if need_cookie and NETEASE_COOKIE:
        # 直接透传原始 cookie 字符串，保证完整性
        headers['Cookie'] = NETEASE_COOKIE
        # 部分接口（如 login/status）需要 cookie 字符串参数
        params.setdefault('cookie', NETEASE_COOKIE)
        cookies = parse_cookie_string(NETEASE_COOKIE)
    if method.upper() == 'POST':
        resp = requests.post(url, data=params, timeout=10, headers=headers, cookies=cookies)
    else:
        resp = requests.get(url, params=params, timeout=10, headers=headers, cookies=cookies)
    resp.raise_for_status()
    return resp.json()

def _extract_song_level(privilege: dict):
    """返回(用户可下载的最高音质, 曲目最高音质)。"""
    privilege = privilege or {}
    def _norm(val):
        if not val:
            return 'standard'
        v = str(val).lower()
        if v == 'none': return 'standard'
        # Map numeric maxbr to levels
        if v.isdigit():
            br = int(v)
            if br >= 999000: return 'lossless'
            if br >= 320000: return 'exhigh'
            if br >= 192000: return 'higher'
            return 'standard'
        return v
    
    max_br = privilege.get('maxBrLevel') or privilege.get('maxbr') or privilege.get('maxLevel')
    max_level = _norm(max_br)
    user_level = _norm(privilege.get('dlLevel') or privilege.get('plLevel') or max_level)
    return (user_level or 'standard', max_level or user_level or 'standard')

def _extract_song_size(track: dict): # Removed preferred parameter
    """根据期望音质优先取对应大小（字节），找不到再按从低到高回退。"""
    if not track:
        return None
    level = 'exhigh' # Default to exhigh
    # 映射期望音质到字段优先级（标准优先用 l）
    prefer_map = {
        'standard': ('l', 'm', 'h', 'sq', 'hr'),
        'higher': ('m', 'h', 'sq', 'hr'),
        'exhigh': ('h', 'sq', 'hr', 'm'),
        'lossless': ('sq', 'hr', 'h', 'm'),
        'hires': ('hr', 'sq', 'h', 'm'),
        'jyeffect': ('sq', 'h', 'm'),
        'sky': ('hr', 'sq', 'h', 'm'),
        'dolby': ('hr', 'sq', 'h', 'm'),
        'jymaster': ('hr', 'sq', 'h', 'm')
    }
    orders = prefer_map.get(level) or ('l', 'm', 'h', 'sq', 'hr')
    for key in orders:
        data = track.get(key) or {}
        size = data.get('size')
        if size:
            try:
                return int(size)
            except Exception:
                continue
    return None

def _format_netease_songs(source_tracks):
    """将网易云接口返回的曲目统一格式化。"""
    songs = []
    for item in source_tracks or []:
        sid = item.get('id')
        if not sid:
            continue
        fee = item.get('fee')
        privilege = item.get('privilege') or {}
        privilege_fee = privilege.get('fee')
        # 仅在明确 fee==1（VIP 曲目）时标记 VIP，避免 fee=8 的“会员高音质”误标
        is_vip = (fee == 1) or (privilege_fee == 1)
        user_level, max_level = _extract_song_level(privilege)
        artists = ' / '.join([a.get('name') for a in item.get('ar', []) if a.get('name')]) or '未知艺术家'
        album_info = item.get('al') or {}
        size_bytes = _extract_song_size(item) # Removed user_level parameter
        songs.append({
            'id': sid,
            'title': item.get('name') or f"未命名 {sid}",
            'artist': artists,
            'album': album_info.get('name') or '',
            'cover': (album_info.get('picUrl') or '').replace('http://', 'https://'),
            'duration': (item.get('dt') or 0) / 1000,
            'is_vip': is_vip,
            'level': user_level,
            'max_level': max_level,
            'size': size_bytes
        })
    return songs

def _resolve_netease_input(raw: str, prefer: str = None):
    """支持短链/长链/纯数字的资源解析，返回 {'type': 'song'|'playlist', 'id': '123'}。"""
    if not raw:
        return None
    prefer = prefer if prefer in ('song', 'playlist') else None
    text = str(raw).strip()

    # 处理纯数字直接返回
    if text.isdigit():
        return {'type': prefer or 'song', 'id': text}

    candidate = text
    # 链接补全 scheme
    if candidate.startswith(('music.163.com', 'y.music.163.com', '163cn.tv')):
        candidate = f"https://{candidate}"
    # 跟随短链跳转获取真实地址，兼容 163cn.tv
    if re.match(r'^https?://', candidate, re.I):
        def _follow(url):
            try:
                resp = requests.get(url, allow_redirects=True, timeout=8, headers=COMMON_HEADERS)
                return resp.url or url
            except Exception as e:
                logger.warning(f"网易云链接解析失败: {e}")
                return None

        followed = _follow(candidate)
        # 针对 163cn.tv 短链再尝试一次 HEAD，避免部分环境 GET 被拦截
        if not followed and '163cn.tv' in candidate:
            try:
                resp = requests.head(candidate, allow_redirects=True, timeout=6, headers=COMMON_HEADERS)
                followed = resp.url or resp.headers.get('Location')
            except Exception as e:
                logger.warning(f"网易云短链 HEAD 解析失败: {e}")
        if followed:
            candidate = followed

    def extract_from_url(url_str: str):
        parsed = urlparse(url_str)
        path = parsed.path or ''
        fragment = parsed.fragment or ''
        frag_path, frag_query = '', {}
        if fragment:
            if '?' in fragment:
                frag_path, frag_qs = fragment.split('?', 1)
                frag_query = parse_qs(frag_qs)
            else:
                frag_path = fragment
        query = parse_qs(parsed.query or '')

        def pick_id(qs):
            for key in ('id', 'songId', 'playlistId'):
                if qs.get(key):
                    return str(qs[key][0])
            return None

        rid = pick_id(query) or pick_id(frag_query)
        route_hint = None
        for seg in (path, frag_path):
            if 'playlist' in seg:
                route_hint = 'playlist'; break
            if 'song' in seg:
                route_hint = 'song'
        if not rid:
            m = re.search(r'/(song|playlist)/(\d+)', path)
            if not m and frag_path:
                m = re.search(r'(song|playlist)[^0-9]*(\d+)', frag_path)
            if m:
                route_hint = route_hint or m.group(1)
                rid = m.group(2)
        if not rid:
            m = re.search(r'id=(\d+)', url_str)
            if m:
                rid = m.group(1)
        if rid:
            return {'type': route_hint or prefer or 'song', 'id': rid}
        return None

    parsed = extract_from_url(candidate)
    if parsed:
        return parsed

    # 回退：直接在文本中寻找
    m = re.search(r'(playlist|song)[^0-9]*(\d+)', text, re.IGNORECASE)
    if m:
        return {'type': m.group(1).lower(), 'id': m.group(2)}
    m = re.search(r'(\d{5,})', text)
    if m:
        return {'type': prefer or 'song', 'id': m.group(1)}
    return None

def _fetch_playlist_songs(playlist_id: str):
    detail_resp = call_netease_api('/playlist/detail', {'id': playlist_id})
    playlist = detail_resp.get('playlist') if isinstance(detail_resp, dict) else None
    if not playlist:
        raise Exception('无法获取歌单信息')
    track_ids = [t.get('id') for t in playlist.get('trackIds', []) if t.get('id')]
    tracks = playlist.get('tracks') or []
    if not tracks and track_ids:
        ids_str = ','.join(map(str, track_ids[:300]))  # protect from huge lists
        song_detail = call_netease_api('/song/detail', {'ids': ids_str})
        tracks = song_detail.get('songs', []) if isinstance(song_detail, dict) else []
    songs = _format_netease_songs(tracks)
    return songs, playlist.get('name')

def _fetch_song_detail(song_id: str):
    detail_resp = call_netease_api('/song/detail', {'ids': song_id})
    songs = detail_resp.get('songs', []) if isinstance(detail_resp, dict) else []
    parsed = _format_netease_songs(songs)
    if not parsed:
        raise Exception('未获取到歌曲信息')
    return parsed

# 预加载网易云 cookie
load_netease_config()
load_netease_cookie()

@app.route('/api/music/lyrics')
def get_lyrics_api():
    logger.info("API请求: 获取歌词")
    title = request.args.get('title')
    artist = request.args.get('artist')
    filename = request.args.get('filename')
    if not title:
        logger.warning("歌词请求缺少title参数")
        return jsonify({'success': False})
    filename = unquote(filename) if filename else None
    
    # Resolve actual local path
    actual_path = None
    if filename:
        if os.path.isabs(filename) and os.path.exists(filename):
            actual_path = filename
        else:
            try:
                with get_db() as conn:
                    # Try to find path by filename in DB
                    row = conn.execute("SELECT path FROM songs WHERE filename=?", (os.path.basename(filename),)).fetchone()
                    if row and os.path.exists(row['path']):
                        actual_path = row['path']
            except Exception as e:
                logger.warning(f"查询歌曲路径失败: {e}")

    # 1. 优先读取本地 .lrc 文件
    lrc_path = None
    if actual_path:
        local_dir = os.path.dirname(actual_path)
        base_name = os.path.splitext(os.path.basename(actual_path))[0]
        
        # 构建搜索路径列表
        search_paths = []
        
        # 1.1 歌曲同目录的 .lrc 文件
        search_paths.append(os.path.join(local_dir, f"{base_name}.lrc"))
        
        # 1.2 歌曲所在目录的 lyrics 子目录
        search_paths.append(os.path.join(local_dir, 'lyrics', f"{base_name}.lrc"))
        
        # 1.3 所有挂载目录的 lyrics 子目录
        try:
            with get_db() as conn:
                rows = conn.execute("SELECT path FROM mount_points").fetchall()
                for r in rows:
                    if r['path']:
                        search_paths.append(os.path.join(r['path'], 'lyrics', f"{base_name}.lrc"))
        except Exception:
            pass
        
        # 1.4 默认音乐库目录的 lyrics 子目录
        search_paths.append(os.path.join(MUSIC_LIBRARY_PATH, 'lyrics', f"{base_name}.lrc"))
        
        # 查找第一个存在的歌词文件
        for path in search_paths:
            if os.path.exists(path):
                lrc_path = path
                break

    if lrc_path and os.path.exists(lrc_path):
        try:
            with open(lrc_path, 'r', encoding='utf-8') as f:
                logger.info(f"本地歌词命中: {lrc_path}")
                return jsonify({'success': True, 'lyrics': f.read()})
        except Exception as e:
            logger.warning(f"读取本地歌词失败: {lrc_path}, 错误: {e}")

    # 2. 尝试提取内嵌歌词
    if actual_path:
        embedded_lrc = extract_embedded_lyrics(actual_path)
        if embedded_lrc:
            # Save to cache if possible - 保存到歌曲所在目录
            try:
                local_dir = os.path.dirname(actual_path)
                save_dir = os.path.join(local_dir, 'lyrics')
                os.makedirs(save_dir, exist_ok=True)
                base_name = os.path.splitext(os.path.basename(actual_path))[0]
                save_path = os.path.join(save_dir, f"{base_name}.lrc")
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write(embedded_lrc)
                logger.info(f"内嵌歌词提取并保存: {save_path}")
            except Exception as e:
                logger.warning(f"保存内嵌歌词失败: {e}")
            return jsonify({'success': True, 'lyrics': embedded_lrc})

    # 3. 网络获取
    api_urls = [
        f"https://api.lrc.cx/lyrics?artist={quote(artist or '')}&title={quote(title)}",
        f"https://lrcapi.msfxp.top/lyrics?artist={quote(artist or '')}&title={quote(title)}"
    ]
    
    # Determine save path for network lyrics - 保存到歌曲所在目录
    save_lrc_path = None
    if actual_path:
        local_dir = os.path.dirname(actual_path)
        base_name = os.path.splitext(os.path.basename(actual_path))[0]
        save_lrc_path = os.path.join(local_dir, 'lyrics', f"{base_name}.lrc")
    elif filename:
        # 如果没有实际路径，保存到默认目录
        lyrics_base_dir = get_default_download_dir()
        save_lrc_path = os.path.join(lyrics_base_dir, 'lyrics', f"{os.path.splitext(os.path.basename(filename))[0]}.lrc")

    for idx, api_url in enumerate(api_urls):
        try:
            label = "主API" if idx == 0 else "备用API"
            safe_url = re.sub(r'^https?://[^/]+', f'[{label}]', api_url)
            logger.info(f"请求网络歌词API: {safe_url}")
            resp = requests.get(api_url, timeout=3, headers=COMMON_HEADERS)
            if resp.status_code == 200:
                if save_lrc_path:
                    try:
                        os.makedirs(os.path.dirname(save_lrc_path), exist_ok=True)
                        with open(save_lrc_path, 'wb') as f:
                            f.write(resp.text.encode('utf-8'))
                        logger.info(f"网络歌词保存: {save_lrc_path}")
                    except Exception as e:
                        logger.warning(f"保存网络歌词失败: {e}")
                return jsonify({'success': True, 'lyrics': resp.text})
            else:
                logger.warning(f"歌词API响应异常: {api_url}, 状态码: {resp.status_code}")
        except:
            pass
    logger.warning(f"歌词获取失败: {title} - {artist}")
    return jsonify({'success': False})

@app.route('/api/music/album-art')
def get_album_art_api():
    title = request.args.get('title')
    artist = request.args.get('artist') or ''
    filename = request.args.get('filename')
    
    if not title or not filename: return jsonify({'success': False})
    filename = unquote(filename)
    base_name = os.path.splitext(os.path.basename(filename))[0]
    
    # 先获取歌曲的实际路径
    actual_path = None
    if os.path.isabs(filename) and os.path.exists(filename):
        actual_path = filename
    else:
        try:
            with get_db() as conn:
                row = conn.execute("SELECT path FROM songs WHERE filename=?", (os.path.basename(filename),)).fetchone()
                if row and os.path.exists(row['path']):
                    actual_path = row['path']
        except Exception as e:
            logger.warning(f"查询歌曲路径失败: {e}")
    
    # 构建搜索路径列表
    search_paths = []
    
    # 1. 歌曲所在目录的 covers 子目录
    if actual_path:
        local_dir = os.path.dirname(actual_path)
        search_paths.append(os.path.join(local_dir, 'covers', f"{base_name}.jpg"))
    
    # 2. 所有挂载目录的 covers 子目录
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT path FROM mount_points").fetchall()
            for r in rows:
                if r['path']:
                    search_paths.append(os.path.join(r['path'], 'covers', f"{base_name}.jpg"))
    except Exception:
        pass
    
    # 3. 默认音乐库目录的 covers 子目录
    search_paths.append(os.path.join(MUSIC_LIBRARY_PATH, 'covers', f"{base_name}.jpg"))
    
    # 查找第一个存在的封面文件
    for path in search_paths:
        if os.path.exists(path):
            return jsonify({'success': True, 'album_art': f"/api/music/covers/{quote(base_name)}.jpg?filename={quote(filename)}"})

    # 确定封面保存目录（优先保存到歌曲所在目录）
    if actual_path:
        cover_save_dir = os.path.join(os.path.dirname(actual_path), 'covers')
    else:
        cover_save_dir = os.path.join(get_default_download_dir(), 'covers')
    local_path = os.path.join(cover_save_dir, f"{base_name}.jpg")
    
    # 尝试从音频内嵌封面提取
    if actual_path and extract_embedded_cover(actual_path, base_name, os.path.dirname(actual_path)):
        try:
            if not os.path.isabs(filename):
                with get_db() as conn:
                    conn.execute("UPDATE songs SET has_cover=1 WHERE filename=?", (os.path.basename(filename),))
                    conn.commit()
        except Exception:
            pass
        return jsonify({'success': True, 'album_art': f"/api/music/covers/{quote(base_name)}.jpg?filename={quote(filename)}"})

    # 网络获取并保存
    api_urls = [
        f"https://api.lrc.cx/cover?artist={quote(artist)}&title={quote(title)}",
        f"https://lrcapi.msfxp.top/cover?artist={quote(artist)}&title={quote(title)}"
    ]
    
    # 确保封面目录存在
    os.makedirs(cover_save_dir, exist_ok=True)
    
    for idx, api_url in enumerate(api_urls):
        try:
            label = "主API" if idx == 0 else "备用API"
            safe_url = re.sub(r'^https?://[^/]+', f'[{label}]', api_url)
            logger.info(f"请求网络封面API: {safe_url}")
            resp = requests.get(api_url, timeout=3, headers=COMMON_HEADERS)
            if resp.status_code == 200 and resp.headers.get('content-type', '').startswith('image/'):
                with open(local_path, 'wb') as f: 
                    f.write(resp.content)
                logger.info(f"网络封面保存: {local_path}")
                
                # 更新数据库标识
                if not os.path.isabs(filename):
                    with get_db() as conn: 
                        conn.execute("UPDATE songs SET has_cover=1 WHERE filename=?", (filename,))
                        conn.commit()
                        
                return jsonify({'success': True, 'album_art': f"/api/music/covers/{quote(base_name)}.jpg?filename={quote(filename)}"})
            else:
                logger.warning(f"封面API响应异常: {api_url}, 状态码: {resp.status_code}")
        except:
            pass
    logger.warning(f"封面获取失败: {title} - {artist}")
    return jsonify({'success': False})

@app.route('/api/music/delete/<song_id>', methods=['DELETE'])
def delete_file(song_id):
    try:
        # 1. 查询路径
        target_path = None
        with get_db() as conn:
            row = conn.execute("SELECT path FROM songs WHERE id=?", (song_id,)).fetchone()
            if row: target_path = row['path']
        
        if not target_path or not os.path.exists(target_path):
            return jsonify({'success': False, 'error': '文件未找到'})

        # 2. 执行删除
        # 永久删除操作。不管是主音乐库还是外部添加目录都执行物理删除。
        # 安全加固：仅允许删除特定后缀的文件，防止误删系统文件
        ALLOWED_DELETE_EXTS = {'.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a'}
        _, ext = os.path.splitext(target_path)
        if ext.lower() not in ALLOWED_DELETE_EXTS:
             return jsonify({'success': False, 'error': f'为了安全，禁止删除 {ext} 类型的文件'})

        # 重试机制应对 Windows 文件锁
        for i in range(10):
            try:
                os.remove(target_path)
                break
            except PermissionError:
                if i < 9: time.sleep(0.2)
                else: return jsonify({'success': False, 'error': '文件正被占用，无法删除'})
        
        # 清理同级关联资源 (封面/歌词/逐字歌词)
        base = os.path.splitext(target_path)[0]
        for ext in ['.lrc', '.yrc', '.jpg']:
            try:
                if os.path.exists(base + ext): os.remove(base + ext)
            except: pass
            
        # 尝试清理主库下的 covers/lyrics
        filename = os.path.basename(target_path)
        base_name = os.path.splitext(filename)[0]
        
        # 清理封面
        try:
             cv_path = os.path.join(MUSIC_LIBRARY_PATH, 'covers', base_name + '.jpg')
             if os.path.exists(cv_path): os.remove(cv_path)
        except: pass

        # 清理歌词 (.lrc / .yrc)
        for lext in ['.lrc', '.yrc']:
            try:
                ly_path = os.path.join(MUSIC_LIBRARY_PATH, 'lyrics', base_name + lext)
                if os.path.exists(ly_path): os.remove(ly_path)
            except: pass
        
        # 4. 数据库清理 (Watchdog 也会做，但双重保障)
        with get_db() as conn:
            conn.execute("DELETE FROM songs WHERE path=?", (target_path,))
            conn.commit()
            
        return jsonify({'success': True})
    except Exception as e: 
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/music/clear_metadata', methods=['POST'])
@app.route('/api/music/clear_metadata/<song_id>', methods=['POST'])
def clear_metadata(song_id=None):
    """清除元数据（封面/歌词）。
    支持两种模式：
    1. URL带 song_id: 库内文件，清理并更新数据库。
    2. JSON带 path: 外部文件，仅通过路径清理缓存。
    统一只清理主音乐库 covers/lyrics 目录下的文件。
    """
    try:
        target_path = None
        
        # 模式1: ID模式
        if song_id:
            with get_db() as conn:
                row = conn.execute("SELECT path FROM songs WHERE id=?", (song_id,)).fetchone()
                if row: target_path = row['path']
        # 模式2: Path模式
        else:
            data = request.get_json() or {}
            target_path = data.get('path')

        if not target_path:
            return jsonify({'success': False, 'error': '未找到对应文件路径'})

        # 安全检查：确保路径在允许的范围内
        target_path = os.path.abspath(target_path)
        allowed_roots = [os.path.abspath(MUSIC_LIBRARY_PATH)]
        try:
            with get_db() as conn:
                rows = conn.execute("SELECT path FROM mount_points").fetchall()
                allowed_roots.extend([os.path.abspath(r['path']) for r in rows])
        except Exception: pass
        
        if not any(target_path.startswith(root) for root in allowed_roots):
            return jsonify({'success': False, 'error': '非法路径：仅允许操作音乐库内的文件'})

        # 核心逻辑：清理主库下的 centralized covers/lyrics
        filename = os.path.basename(target_path)
        base_name = os.path.splitext(filename)[0]
        deleted_count = 0
        
        for sub in ['lyrics', 'covers']:
            ext = '.lrc' if sub == 'lyrics' else '.jpg'
            sub_path = os.path.join(MUSIC_LIBRARY_PATH, sub, base_name + ext)
            try: 
                if os.path.exists(sub_path): 
                    os.remove(sub_path)
                    deleted_count += 1
            except: pass

        # 如果是库内文件（有song_id），还需要重置数据库状态
        if song_id:
            with get_db() as conn:
                conn.execute("UPDATE songs SET has_cover=0 WHERE id=?", (song_id,))
                conn.commit()
            
        logger.info(f"元数据已清除: {filename}, ID: {song_id}, 删除数: {deleted_count}")
        return jsonify({'success': True})
    except Exception as e: 
        logger.warning(f"元数据清除失败: {e}")
        return jsonify({'success': False, 'error': str(e)})

# --- 辅助接口 ---
@app.route('/api/music/covers/<cover_name>')
def get_cover(cover_name):
    cover_name = unquote(cover_name)
    filename = request.args.get('filename', '')
    
    # 构建搜索目录列表
    search_dirs = []
    
    # 1. 如果提供了 filename，尝试从歌曲所在目录的 covers 子目录查找
    if filename:
        try:
            with get_db() as conn:
                row = conn.execute('SELECT path FROM songs WHERE filename = ?', (unquote(filename),)).fetchone()
                if row and row['path']:
                    song_dir = os.path.dirname(row['path'])
                    search_dirs.append(os.path.join(song_dir, 'covers'))
        except Exception:
            pass
    
    # 2. 从所有挂载目录的 covers 子目录查找
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT path FROM mount_points").fetchall()
            for r in rows:
                if r['path']:
                    search_dirs.append(os.path.join(r['path'], 'covers'))
    except Exception:
        pass
    
    # 3. 从默认音乐库目录查找
    search_dirs.append(os.path.join(MUSIC_LIBRARY_PATH, 'covers'))
    
    # 去重
    search_dirs = list(dict.fromkeys(search_dirs))
    
    for cover_dir in search_dirs:
        path = os.path.join(cover_dir, cover_name)
        if os.path.exists(path):
            return send_file(path, mimetype='image/jpeg')
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/music/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files: return jsonify({'success': False, 'error': '未收到文件'})
    file = request.files['file']
    if file.filename == '': return jsonify({'success': False, 'error': '文件名为空'})
    if file:
        filename = file.filename
        target_dir = request.form.get('target_dir') or MUSIC_LIBRARY_PATH
        target_dir = os.path.abspath(target_dir)
        # 仅允许保存到音乐库或已添加的挂载目录（及其子目录）
        allowed_roots = [os.path.abspath(MUSIC_LIBRARY_PATH)]
        try:
            with get_db() as conn:
                rows = conn.execute("SELECT path FROM mount_points").fetchall()
                allowed_roots.extend([os.path.abspath(r['path']) for r in rows])
        except Exception:
            pass
        if not any(target_dir.startswith(root) for root in allowed_roots):
            return jsonify({'success': False, 'error': '无效保存路径，请先在目录管理中添加'})
        os.makedirs(target_dir, exist_ok=True)
        save_path = os.path.join(target_dir, filename)

        # 数据库查重
        try:
            with get_db() as conn:
                exists = conn.execute("SELECT 1 FROM songs WHERE path=?", (save_path,)).fetchone()
                if exists:
                    return jsonify({'success': False, 'error': '该文件已存在于当前目录下'})
                
                # 全局查重 (文件名 + 大小)
                file.seek(0, os.SEEK_END)
                file_size = file.tell()
                file.seek(0)
                
                dup = conn.execute("SELECT path FROM songs WHERE filename=? AND size=?", (filename, file_size)).fetchone()
                if dup:
                    return jsonify({'success': False, 'error': f'音乐库中已存在相同文件: {dup["path"]}'})

        except Exception as e:
            logger.error(f"查重失败: {e}")
            pass

        try:
            file.save(save_path)
            # 让 Watchdog 处理索引
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    return jsonify({'success': False, 'error': '未知错误'})

@app.route('/api/music/import_path', methods=['POST'])
def import_music_by_path():
    try:
        data = request.json
        src_path = data.get('path')
        if not src_path or not os.path.exists(src_path): return jsonify({'success': False, 'error': '无效路径'})
        filename = os.path.basename(src_path)
        dst_path = os.path.join(MUSIC_LIBRARY_PATH, filename)
        # 查重 (与上传保持一致)
        if os.path.exists(dst_path):
             # 目标已存在 (文件名冲突)
             pass

        # 全局查重
        src_size = os.path.getsize(src_path)
        with get_db() as conn:
             dup = conn.execute("SELECT path FROM songs WHERE filename=? AND size=?", (filename, src_size)).fetchone()
             if dup:
                 # 如果已存在的文件就是目标位置的文件（即重复导入自己），则是允许的（当作刷新）
                 # 如果 duplicates path != dst_path -> 真正的异地重复 -> 报错
                 if dup['path'] != os.path.abspath(dst_path):
                     return jsonify({'success': False, 'error': f'音乐库中已存在相同文件: {dup["path"]}'})

        if not os.path.exists(dst_path):
            shutil.copy2(src_path, dst_path)
            # 立即索引，确保入库
            index_single_file(dst_path)
        
        # 计算预期的 ID (与扫描逻辑一致)
        song_id = generate_song_id(dst_path)
        return jsonify({'success': True, 'id': song_id, 'filename': filename})
    except Exception as e: return jsonify({'success': False, 'error': str(e)})

# --- 收藏夹接口 (用户隔离) ---
@app.route('/api/favorites', methods=['GET'])
def get_favorites():
    try:
        user_hash = session.get('user_hash')
        if not user_hash:
            return jsonify({'success': False, 'error': 'not logged in'})
        user_data = load_user_data(user_hash)
        if not user_data:
            return jsonify({'success': True, 'data': []})
        return jsonify({'success': True, 'data': user_data.get('favorites', [])})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/favorites/<song_id>', methods=['POST'])
def add_favorite(song_id):
    try:
        user_hash = session.get('user_hash')
        if not user_hash:
            return jsonify({'success': False, 'error': 'not logged in'})
        user_data = load_user_data(user_hash)
        if not user_data:
            return jsonify({'success': False, 'error': 'user not found'})
        
        favorites = user_data.get('favorites', [])
        if song_id not in favorites:
            favorites.append(song_id)
            user_data['favorites'] = favorites
            save_user_data(user_hash, user_data)
        
        logger.info(f"收藏成功: {song_id}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"收藏失败: {e}")
        return jsonify({'success': False, 'error': "添加失败"})

@app.route('/api/favorites/<song_id>', methods=['DELETE'])
def remove_favorite(song_id):
    try:
        user_hash = session.get('user_hash')
        if not user_hash:
            return jsonify({'success': False, 'error': 'not logged in'})
        user_data = load_user_data(user_hash)
        if not user_data:
            return jsonify({'success': False, 'error': 'user not found'})
        
        favorites = user_data.get('favorites', [])
        if song_id in favorites:
            favorites.remove(song_id)
            user_data['favorites'] = favorites
            save_user_data(user_hash, user_data)
        
        logger.info(f"取消收藏成功: {song_id}")
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"取消收藏失败: {e}")
        return jsonify({'success': False, 'error': "移除失败"})

@app.route('/api/netease/search')
def search_netease_music():
    """通过本地网易云 API 搜索歌曲。"""
    keywords = (request.args.get('keywords') or '').strip()
    if not keywords:
        return jsonify({'success': False, 'error': '请输入搜索关键词'})
    limit = request.args.get('limit', 20)
    try:
        limit = max(1, min(int(limit), 50))
    except Exception:
        limit = 20

    try:
        api_resp = call_netease_api('/cloudsearch', {'keywords': keywords, 'type': 1, 'limit': limit})
        songs = []
        for item in api_resp.get('result', {}).get('songs', []):
            song_id = item.get('id')
            if not song_id: 
                continue
            artists = ' / '.join([a.get('name') for a in item.get('ar', []) if a.get('name')]) or '未知艺术家'
            album_info = item.get('al') or {}
            privilege = item.get('privilege') or {}
            fee = item.get('fee')
            privilege_fee = privilege.get('fee')
            # 仅 fee==1 视为 VIP；fee=8 只代表会员可享高音质，不强制标 VIP
            # 仅 fee==1 视为 VIP；fee=8 只代表会员可享高音质，不强制标 VIP
            is_vip = (fee == 1) or (privilege_fee == 1)
            user_level, max_level = _extract_song_level(privilege)
            songs.append({
                'id': song_id,
                'title': item.get('name') or f"未命名 {song_id}",
                'artist': artists,
                'album': album_info.get('name') or '',
                'cover': (album_info.get('picUrl') or '').replace('http://', 'https://'),
                'duration': (item.get('dt') or 0) / 1000,
                'level': user_level,
                'max_level': max_level,
                'size': _extract_song_size(item), # Removed user_level parameter
                'is_vip': is_vip
            })
        return jsonify({'success': True, 'data': songs})
    except Exception as e:
        logger.warning(f"网易云搜索失败: {e}")
        return jsonify({'success': False, 'error': '搜索失败，请检查网易云 API 服务'})

@app.route('/api/netease/recommend')
def netease_daily_recommend():
    """获取每日推荐歌曲，需要已登录网易云账号。"""
    try:
        api_resp = call_netease_api('/recommend/songs', {'timestamp': int(time.time() * 1000)}, need_cookie=True)
        if isinstance(api_resp, dict) and api_resp.get('code') == 301:
            return jsonify({'success': False, 'error': '需要登录以获取每日推荐'})
        daily = (api_resp.get('data') or {}).get('dailySongs', []) if isinstance(api_resp, dict) else []
        songs = _format_netease_songs(daily)
        return jsonify({'success': True, 'data': songs})
    except Exception as e:
        logger.warning(f"获取每日推荐失败: {e}")
        return jsonify({'success': False, 'error': '获取每日推荐失败，请检查登录状态或 API 服务'})

@app.route('/api/netease/login/status')
def netease_login_status():
    """检测当前 cookie 是否已登录。"""
    try:
        if not NETEASE_COOKIE:
            logger.info("网易云登录状态检查：当前未加载 cookie")
        api_resp = call_netease_api('/login/status', {'timestamp': int(time.time() * 1000)}, need_cookie=True)
        profile = api_resp.get('data', {}).get('profile') if isinstance(api_resp, dict) else None
        if profile:
            is_vip = False
            vip_info = {}
            try:
                vip_resp = call_netease_api('/vip/info', {'uid': profile.get('userId')})
                if isinstance(vip_resp, dict):
                    vip_info = vip_resp.get('data') or vip_resp
                    data = vip_info or {}
                    now_ms = int(time.time() * 1000)

                    def _active(pkg: dict):
                        """vipCode>0 且未过期的套餐视为有效；expireTime 为空默认为有效。"""
                        if not pkg:
                            return False
                        code = pkg.get('vipCode') or 0
                        exp = pkg.get('expireTime') or pkg.get('expiretime')
                        if code <= 0:
                            return False
                        if exp is None:
                            return False
                        try:
                            return int(exp) > now_ms
                        except Exception:
                            return False

                    # 综合判断：isVip 明确标记 > 任一未过期套餐/标识 > redVipLevel>0
                    is_vip = bool(data.get('isVip'))
                    if not is_vip:
                        is_vip = any([
                            _active(data.get('associator')),
                            _active(data.get('musicPackage')),
                            _active(data.get('redplus')),
                            _active(data.get('familyVip'))
                        ])
            except Exception as e:
                logger.warning(f"获取VIP信息失败: {e}")
            return jsonify({
                'success': True,
                'logged_in': True,
                'nickname': profile.get('nickname'),
                'user_id': profile.get('userId'),
                'avatar': profile.get('avatarUrl'),
                'is_vip': is_vip,
                'vip_info': vip_info
            })
        return jsonify({'success': True, 'logged_in': False, 'error': '未登录'})
    except Exception as e:
        logger.warning(f"检查网易云登录状态失败: {e}")
        return jsonify({'success': False, 'error': '状态检查失败'})

@app.route('/api/netease/logout', methods=['POST'])
def netease_logout():
    """退出登录并清空本地保存的网易云 cookie。"""
    try:
        if NETEASE_COOKIE:
            try:
                call_netease_api('/logout', {'timestamp': int(time.time() * 1000)}, need_cookie=True)
            except Exception as e:
                logger.info(f"网易云 API 注销调用失败，继续清理本地 cookie: {e}")
        save_netease_cookie('')
        return jsonify({'success': True})
    except Exception as e:
        logger.warning(f"网易云退出登录失败: {e}")
        return jsonify({'success': False, 'error': '退出失败'})

@app.route('/api/netease/login/qrcode')
def netease_login_qrcode():
    """生成扫码登录二维码。"""
    try:
        key_resp = call_netease_api('/login/qr/key', {'timestamp': int(time.time() * 1000)}, need_cookie=False)
        unikey = key_resp.get('data', {}).get('unikey')
        if not unikey:
            return jsonify({'success': False, 'error': '获取登录 key 失败'})
        qr_resp = call_netease_api('/login/qr/create', {'key': unikey, 'qrimg': 1, 'timestamp': int(time.time() * 1000)}, need_cookie=False)
        qrimg = qr_resp.get('data', {}).get('qrimg')
        if not qrimg:
            return jsonify({'success': False, 'error': '获取二维码失败'})
        return jsonify({'success': True, 'unikey': unikey, 'qrimg': qrimg})
    except Exception as e:
        logger.warning(f"生成网易云二维码失败: {e}")
        return jsonify({'success': False, 'error': '二维码生成失败'})

@app.route('/api/netease/login/check')
def netease_login_check():
    """轮询扫码状态，成功后保存 cookie。"""
    key = request.args.get('key')
    if not key:
        return jsonify({'success': False, 'error': '缺少 key'})
    try:
        resp = call_netease_api('/login/qr/check', {'key': key, 'timestamp': int(time.time() * 1000)}, need_cookie=False)
        code = resp.get('code')
        message = resp.get('message')
        cookie_str = resp.get('cookie')
        if not cookie_str and isinstance(resp.get('cookies'), list):
            cookie_str = '; '.join(resp.get('cookies'))
        
        # Debug Log
        if code == 803:
            logger.info(f"扫码成功 (803). Raw cookie: {bool(cookie_str)}, Length: {len(cookie_str) if cookie_str else 0}")
            
        if code == 803 and cookie_str:
            save_netease_cookie(cookie_str)
            return jsonify({'success': True, 'status': 'authorized', 'message': message})
        status_map = {
            800: 'expired',
            801: 'waiting',
            802: 'scanned'
        }
        return jsonify({'success': True, 'status': status_map.get(code, 'unknown'), 'message': message})
    except Exception as e:
        logger.warning(f"扫码检查失败: {e}")
        return jsonify({'success': False, 'error': '扫码轮询失败'})

@app.route('/api/netease/download_page')
def netease_download_page():
    """重定向到网易云音乐客户端下载页面。"""
    return redirect("https://music.163.com/client")

@app.route('/api/netease/config', methods=['GET', 'POST'])
def netease_config():
    """获取或更新网易云下载配置。"""
    try:
        if request.method == 'GET':
            return jsonify({
                'success': True, 
                'download_dir': NETEASE_DOWNLOAD_DIR, 
                'api_base': NETEASE_API_BASE, 
                'max_concurrent': NETEASE_MAX_CONCURRENT,
                'quality': NETEASE_QUALITY_DEFAULT # Always return default quality
            })
        data = request.json or {}
        target_dir = data.get('download_dir')
        api_base = (data.get('api_base') or '').strip()
        # quality = data.get('quality') # Removed quality processing
        
        if target_dir:
            target_dir = os.path.abspath(target_dir)
            os.makedirs(target_dir, exist_ok=True)
        else:
            target_dir = None
            
        if api_base:
            api_base = api_base.rstrip('/')
            
        if not target_dir and not api_base: # Removed quality check
            return jsonify({'success': False, 'error': '未提供任何配置项'})
            
        save_netease_config(target_dir, api_base) # Removed quality parameter
        return jsonify({
            'success': True, 
            'download_dir': NETEASE_DOWNLOAD_DIR, 
            'api_base': NETEASE_API_BASE, 
            'max_concurrent': NETEASE_MAX_CONCURRENT,
            'quality': NETEASE_QUALITY_DEFAULT # Always return default quality
        })
    except Exception as e:
        logger.warning(f"更新网易云配置失败: {e}")
        return jsonify({'success': False, 'error': '保存失败'})

@app.route('/api/netease/debug')
def netease_debug():
    """调试用，查看 cookie 是否加载。"""
    info = {
        'cookie_loaded': bool(NETEASE_COOKIE),
        'api_base': NETEASE_API_BASE,
        'download_dir': NETEASE_DOWNLOAD_DIR
    }
    return jsonify(info)

def _normalize_cover_url(url: str):
    if not url:
        return None
    u = url.replace('http://', 'https://')
    if '//' not in u:
        return None
    # NetEase 图片参数：确保有清晰尺寸
    if 'param=' not in u and '?param=' not in u:
        sep = '&' if '?' in u else '?'
        u = f"{u}{sep}param=1024y1024"
    return u

def run_download_task(task_id, payload):
    song_id = payload.get('id')
    title = (payload.get('title') or '').strip()
    artist = (payload.get('artist') or '').strip()
    album = (payload.get('album') or '').strip()
    # Priority: Payload Level -> Configured Level -> Default (exhigh)
    level = payload.get('level') or NETEASE_QUALITY or NETEASE_QUALITY_DEFAULT
    
    cover_url = _normalize_cover_url(payload.get('cover') or payload.get('album_art'))
    cover_bytes = fetch_cover_bytes(cover_url) if cover_url else None
    
    target_dir = payload.get('target_dir') or NETEASE_DOWNLOAD_DIR
    target_dir = os.path.abspath(target_dir)
    
    DOWNLOAD_TASKS[task_id]['status'] = 'preparing'
    DOWNLOAD_TASKS[task_id]['progress'] = 0

    try:
        os.makedirs(target_dir, exist_ok=True)
        
        # 1. Fetch Song Detail if missing critical info
        need_detail = (not title) or (not artist)
        if need_detail:
            meta_resp = call_netease_api('/song/detail', {'ids': song_id})
            songs = meta_resp.get('songs', []) if isinstance(meta_resp, dict) else []
            if songs:
                info = songs[0]
                title = info.get('name') or title
                artist = ' / '.join([a.get('name') for a in info.get('ar', []) if a.get('name')]) or artist
                album = (info.get('al') or {}).get('name') or album
                if not cover_url:
                    cover_url = _normalize_cover_url((info.get('al') or {}).get('picUrl'))
                    if cover_url: cover_bytes = fetch_cover_bytes(cover_url)

        # Update Task Info
        DOWNLOAD_TASKS[task_id]['title'] = title
        DOWNLOAD_TASKS[task_id]['artist'] = artist
        
        # 2. Get Download URL
        DOWNLOAD_TASKS[task_id]['status'] = 'downloading'
        url_resp = call_netease_api('/song/url/v1', {'id': song_id, 'level': level})
        data_list = url_resp.get('data', []) if isinstance(url_resp, dict) else []
        if not data_list:
             raise Exception('Failed to get download URL data')
        
        song_info = data_list[0]
        down_url = song_info.get('url')
        if not down_url:
             # Try fallback to standard if high quality fails? 
             # For now just error
             raise Exception(f'No download URL for level: {level}')
             
        file_ext = (song_info.get('type') or 'mp3').lower()
        if not file_ext.startswith('.'): file_ext = '.' + file_ext
        
        # Filename
        fname = sanitize_filename(f"{artist} - {title}{file_ext}")
        file_path = os.path.join(target_dir, fname)
        DOWNLOAD_TASKS[task_id]['filename'] = fname
        
        # 3. Download File
        size = song_info.get('size') or 0
        dl_resp = requests.get(down_url, stream=True, timeout=30, headers=COMMON_HEADERS)
        dl_resp.raise_for_status()
        
        downloaded = 0
        with open(file_path, 'wb') as f:
            for chunk in dl_resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if size > 0:
                        percent = int(downloaded / size * 100)
                        # Throttle updates
                        if percent > DOWNLOAD_TASKS[task_id]['progress']:
                            DOWNLOAD_TASKS[task_id]['progress'] = percent
                            
        DOWNLOAD_TASKS[task_id]['progress'] = 100
        
        # 4. Write Metadata
        try:
            # Basic Tags
            if file_ext == '.mp3':
                try:
                    audio = EasyID3(file_path)
                except:
                    audio = File(file_path, easy=True)
                    audio.add_tags()
                audio['title'] = title
                audio['artist'] = artist
                audio['album'] = album
                audio.save()
            elif file_ext == '.flac':
                audio = FLAC(file_path)
                audio['title'] = title
                audio['artist'] = artist
                audio['album'] = album
                audio.save()
            
            # Cover
            if cover_bytes: 
                embed_cover_to_file(file_path, cover_bytes)
                if fname:
                    save_cover_file(cover_bytes, os.path.splitext(fname)[0], target_dir)
            
            # Lyrics
            lrc, _ = fetch_netease_lyrics(song_id)
            if lrc:
                embed_lyrics_to_file(file_path, lrc)
                
        except Exception as e:
            logger.warning(f"Metadata embedding failed: {e}")
            
        # 5. Index
        index_single_file(file_path)
        
        DOWNLOAD_TASKS[task_id]['status'] = 'success'
        
    except Exception as e:
        logger.error(f"Download task failed: {e}")
        DOWNLOAD_TASKS[task_id]['status'] = 'error'
        DOWNLOAD_TASKS[task_id]['message'] = str(e)

@app.route('/api/netease/resolve')
def netease_resolve():
    """通过分享链接或ID自动识别资源并返回歌曲列表。"""
    raw_input = request.args.get('input') or request.args.get('link') or request.args.get('id')
    parsed_input = _resolve_netease_input(raw_input)
    if not parsed_input:
        return jsonify({'success': False, 'error': '请粘贴网易云分享链接或输入ID'})
    try:
        if parsed_input['type'] == 'playlist':
            songs, name = _fetch_playlist_songs(parsed_input['id'])
            return jsonify({'success': True, 'type': 'playlist', 'id': parsed_input['id'], 'name': name, 'data': songs})
        songs = _fetch_song_detail(parsed_input['id'])
        return jsonify({'success': True, 'type': 'song', 'id': parsed_input['id'], 'data': songs})
    except Exception as e:
        logger.warning(f"解析网易云链接失败: {e}")
        return jsonify({'success': False, 'error': '解析失败，请确认歌曲或歌单链接有效'})

@app.route('/api/netease/playlist')
def netease_playlist_detail():
    """获取歌单详情及歌曲列表。"""
    raw_input = request.args.get('id') or request.args.get('link') or request.args.get('input')
    parsed_input = _resolve_netease_input(raw_input, prefer='playlist')
    if not parsed_input or parsed_input.get('type') != 'playlist':
        return jsonify({'success': False, 'error': '缺少歌单链接或无法识别'})
    try:
        songs, name = _fetch_playlist_songs(parsed_input['id'])
        return jsonify({'success': True, 'name': name, 'id': parsed_input['id'], 'data': songs})
    except Exception as e:
        logger.warning(f"歌单获取失败: {e}")
        return jsonify({'success': False, 'error': '获取歌单失败'})

@app.route('/api/netease/song')
def netease_song_detail():
    """根据单曲ID获取歌曲详情，用于解析而非直接下载。"""
    raw_input = request.args.get('id') or request.args.get('link') or request.args.get('input')
    parsed_input = _resolve_netease_input(raw_input, prefer='song')
    if not parsed_input:
        return jsonify({'success': False, 'error': '缺少歌曲链接或ID'})
    if parsed_input.get('type') == 'playlist':
        return jsonify({'success': False, 'error': '检测到歌单链接，请切换歌单解析'})
    try:
        parsed = _fetch_song_detail(parsed_input['id'])
        return jsonify({'success': True, 'id': parsed_input['id'], 'data': parsed})
    except Exception as e:
        logger.warning(f"获取单曲详情失败: {e}")
        return jsonify({'success': False, 'error': '获取歌曲信息失败'})

        # 索引文件
        index_single_file(target_path)
        
        DOWNLOAD_TASKS[task_id]['status'] = 'success'
        DOWNLOAD_TASKS[task_id]['progress'] = 100
        logger.info(f"网易云歌曲已下载: {filename} | {title} - {artist}")
        
    except Exception as e:
        logger.warning(f"网易云下载失败: {e}")
        DOWNLOAD_TASKS[task_id]['status'] = 'error'
        DOWNLOAD_TASKS[task_id]['message'] = str(e)
    finally:
        # 10分钟后清理任务状态
        def clean_task():
            time.sleep(600)
            DOWNLOAD_TASKS.pop(task_id, None)
        threading.Thread(target=clean_task, daemon=True).start()

@app.route('/api/netease/download', methods=['POST'])
def download_netease_music():
    """根据歌曲ID下载网易云音乐到本地库。(异步)"""
    payload = request.json or {}
    song_id = payload.get('id')
    if not song_id:
        return jsonify({'success': False, 'error': '缺少歌曲ID'})

    active = sum(1 for t in DOWNLOAD_TASKS.values() if t.get('status') in ('pending', 'preparing', 'downloading'))
    if active >= NETEASE_MAX_CONCURRENT:
        return jsonify({'success': False, 'error': f'并发下载已达上限 ({NETEASE_MAX_CONCURRENT})，请稍后再试'})
    
    task_id = f"task_{int(time.time()*1000)}_{os.urandom(4).hex()}"
    DOWNLOAD_TASKS[task_id] = {
        'status': 'pending', 
        'progress': 0, 
        'title': payload.get('title', '未知'),
        'artist': payload.get('artist', '未知')
    }
    
    threading.Thread(target=run_download_task, args=(task_id, payload), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id})

def _normalize_cover_url(url: str):
    if not url:
        return None
    u = url.replace('http://', 'https://')
    if '//' not in u:
        return None
    # NetEase 图片参数：确保有清晰尺寸
    if 'param=' not in u and '?param=' not in u:
        sep = '&' if '?' in u else '?'
        u = f"{u}{sep}param=1024y1024"
    return u

def run_download_task(task_id, payload):
    song_id = payload.get('id')
    title = (payload.get('title') or '').strip()
    artist = (payload.get('artist') or '').strip()
    album = (payload.get('album') or '').strip()
    level = payload.get('level') or 'exhigh'
    cover_url = _normalize_cover_url(payload.get('cover') or payload.get('album_art'))
    cover_bytes = fetch_cover_bytes(cover_url) if cover_url else None
    target_dir = payload.get('target_dir') or NETEASE_DOWNLOAD_DIR
    target_dir = os.path.abspath(target_dir)
    
    target_dir = os.path.abspath(target_dir)
    
    DOWNLOAD_TASKS[task_id]['status'] = 'preparing'

    try:
        os.makedirs(target_dir, exist_ok=True)
        need_detail_for_level = not payload.get('level')
        need_detail_for_cover = cover_bytes is None
        if not title or need_detail_for_level or need_detail_for_cover:
            # 拉取歌曲详情补充元信息、下载音质和封面
            meta_resp = call_netease_api('/song/detail', {'ids': song_id})
            songs = meta_resp.get('songs', []) if isinstance(meta_resp, dict) else []
            if songs:
                info = songs[0]
                if need_detail_for_level:
                    level, _ = _extract_song_level(info.get('privilege') or {})
                title = info.get('name') or title or f"未命名 {song_id}"
                artist = ' / '.join([a.get('name') for a in info.get('ar', []) if a.get('name')]) or artist
                album = (info.get('al') or {}).get('name') or album
                if need_detail_for_cover and not cover_bytes:
                    pic_url = _normalize_cover_url((info.get('al') or {}).get('picUrl'))
                    if pic_url:
                        cover_bytes = fetch_cover_bytes(pic_url)
                base_filename = sanitize_filename(f"{artist or '未知艺术家'} - {title}")
        if not title:
            title = f"未命名 {song_id}"
        if not artist:
            artist = '未知艺术家'
        if 'base_filename' not in locals() or not base_filename:
            base_filename = sanitize_filename(payload.get('filename') or f"{artist} - {title}")
            
        # 更新任务信息
        DOWNLOAD_TASKS[task_id]['title'] = title
        DOWNLOAD_TASKS[task_id]['artist'] = artist

        api_resp = call_netease_api('/song/url/v1', {'id': song_id, 'level': level}, need_cookie=bool(NETEASE_COOKIE))
        data_list = api_resp.get('data') if isinstance(api_resp, dict) else None
        track_info = None
        if isinstance(data_list, list) and data_list:
            track_info = data_list[0]
        elif isinstance(data_list, dict):
            track_info = data_list

        if not track_info or (not track_info.get('url') and not track_info.get('proxyUrl')):
            # 回退到标准音质再试一次
            if level != 'standard':
                try:
                    api_resp_std = call_netease_api('/song/url/v1', {'id': song_id, 'level': 'standard'}, need_cookie=bool(NETEASE_COOKIE))
                    data_list = api_resp_std.get('data') if isinstance(api_resp_std, dict) else None
                    if isinstance(data_list, list) and data_list:
                        track_info = data_list[0]
                    elif isinstance(data_list, dict):
                        track_info = data_list
                except Exception:
                    track_info = track_info
            if not track_info or (not track_info.get('url') and not track_info.get('proxyUrl')):
                raise Exception('暂无可用下载地址，可能需要切换音质或登录')

        download_url = track_info.get('url') or track_info.get('proxyUrl')
        ext = (track_info.get('type') or track_info.get('encodeType') or 'mp3').lower()
        filename = base_filename if base_filename.lower().endswith(f".{ext}") else f"{base_filename}.{ext}"
        target_path = os.path.join(target_dir, filename)

        counter = 1
        while os.path.exists(target_path):
            filename = f"{base_filename} ({counter}).{ext}"
            target_path = os.path.join(target_dir, filename)
            counter += 1

        tmp_path = target_path + ".part"
        DOWNLOAD_TASKS[task_id]['status'] = 'downloading'
        try:
            with requests.get(download_url, stream=True, timeout=20, headers=COMMON_HEADERS) as resp:
                resp.raise_for_status()
                total_size = int(resp.headers.get('content-length', 0))
                downloaded = 0
                
                with open(tmp_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                progress = int((downloaded / total_size) * 100)
                                DOWNLOAD_TASKS[task_id]['progress'] = progress
                                
            shutil.move(tmp_path, target_path)
        finally:
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except: pass
            
        # 索引文件
        base_name_for_cover = os.path.splitext(os.path.basename(target_path))[0]
        if cover_bytes:
            embed_cover_to_file(target_path, cover_bytes)
            # 保存封面到下载目录
            save_cover_file(cover_bytes, base_name_for_cover, target_dir)
        # 保存并内嵌歌词（无需登录）
        lrc_text, yrc_text = fetch_netease_lyrics(song_id)
        # 使用下载目录的 lyrics 子目录
        lyrics_save_dir = os.path.join(target_dir, 'lyrics')
        if lrc_text:
            try:
                os.makedirs(lyrics_save_dir, exist_ok=True)
                lrc_path = os.path.join(lyrics_save_dir, f"{base_name_for_cover}.lrc")
                with open(lrc_path, 'w', encoding='utf-8') as f:
                    f.write(lrc_text)
            except Exception as e:
                logger.warning(f"保存歌词失败: {e}")
            embed_lyrics_to_file(target_path, lrc_text)
        if yrc_text:
            try:
                os.makedirs(lyrics_save_dir, exist_ok=True)
                with open(os.path.join(lyrics_save_dir, f"{base_name_for_cover}.yrc"), 'w', encoding='utf-8') as f:
                    f.write(yrc_text)
            except Exception as e:
                logger.warning(f"保存逐字歌词失败: {e}")
        index_single_file(target_path)
        
        DOWNLOAD_TASKS[task_id]['status'] = 'success'
        DOWNLOAD_TASKS[task_id]['progress'] = 100
        logger.info(f"网易云歌曲已下载: {filename} | {title} - {artist}")
        
    except Exception as e:
        logger.warning(f"网易云下载失败: {e}")
        DOWNLOAD_TASKS[task_id]['status'] = 'error'
        DOWNLOAD_TASKS[task_id]['message'] = str(e)
    finally:
        # 10分钟后清理任务状态
        def clean_task():
            time.sleep(600)
            DOWNLOAD_TASKS.pop(task_id, None)
        threading.Thread(target=clean_task, daemon=True).start()

@app.route('/api/netease/task/<task_id>')
def get_netease_task_status(task_id):
    task = DOWNLOAD_TASKS.get(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'})
    return jsonify({'success': True, 'data': task})

@app.route('/api/music/external/meta')
def get_external_meta():
    path = request.args.get('path')
    if not path or not os.path.exists(path): return jsonify({'success': False, 'error': '文件未找到'})
    try:
        meta = get_metadata(path)
        song_id = generate_song_id(path)
        album_art = None
        base_name = os.path.splitext(os.path.basename(path))[0]
        cached_cover = os.path.join(MUSIC_LIBRARY_PATH, 'covers', f"{base_name}.jpg")
        cached_cover = os.path.join(MUSIC_LIBRARY_PATH, 'covers', f"{base_name}.jpg")
        if os.path.exists(cached_cover): album_art = f"/api/music/covers/{quote(base_name)}.jpg?filename={quote(base_name)}"
        
        in_library = False
        with get_db() as conn:
             if conn.execute("SELECT 1 FROM songs WHERE id=?", (song_id,)).fetchone():
                 in_library = True

        return jsonify({'success': True, 'data': {'id': song_id, 'filename': path, 'title': meta['title'] or os.path.basename(path), 'artist': meta['artist'] or '未知艺术家', 'album': meta['album'] or '', 'album_art': album_art, 'in_library': in_library}})
    except Exception as e: return jsonify({'success': False, 'error': str(e)})

@app.route('/api/music/external/play')
def play_external_file():
    path = request.args.get('path')
    if path and os.path.exists(path): return send_file(path, conditional=True)
    return jsonify({'error': '文件未找到'}), 404

# --- 安装状态管理 ---
INSTALL_STATUS = {
    'status': 'idle', # idle, running, success, error
    'progress': 0,
    'step': '',
    'error': None
}

@app.route('/api/netease/install/status')
def get_install_status():
    return jsonify(INSTALL_STATUS)

@app.route('/api/netease/install_service', methods=['POST'])
def install_netease_service():
    """尝试自动拉取并运行网易云 API 容器"""
    import subprocess
    global INSTALL_STATUS
    
    if INSTALL_STATUS['status'] == 'running':
         return jsonify({'success': False, 'error': '安装任务正在进行中'})

    INSTALL_STATUS = {'status': 'running', 'progress': 0, 'step': '准备安装...', 'error': None}
    logger.info("API请求: 安装网易云服务")
    
    def run_install():
        global INSTALL_STATUS
        try:
            # 1. 检查 Docker 是否可用
            INSTALL_STATUS.update({'progress': 10, 'step': '检查 Docker 环境...'})
            subprocess.run(["docker", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # 2. 检查由我们创建的容器是否已存在
            container_name = "2fmusic-ncm-api"
            INSTALL_STATUS.update({'progress': 20, 'step': f'检查容器 {container_name}...'})
            
            check_proc = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
                capture_output=True, text=True
            )
            
            if container_name in check_proc.stdout.strip():
                # 容器已存在，尝试启动
                INSTALL_STATUS.update({'progress': 60, 'step': '容器已存在，正在启动...'})
                logger.info("容器已存在，尝试启动...")
                subprocess.run(["docker", "start", container_name], check=True)
            else:
                # 容器不存在，拉取并运行
                INSTALL_STATUS.update({'progress': 30, 'step': '正在拉取镜像 (耗时较长)...'})
                logger.info("正在拉取镜像 moefurina/ncm-api...")
                subprocess.run(["docker", "pull", "moefurina/ncm-api:latest"], check=True)
                
                INSTALL_STATUS.update({'progress': 70, 'step': '镜像拉取完成，正在启动容器...'})
                logger.info("正在启动容器...")
                # 映射端口 28999:3000
                subprocess.run([
                    "docker", "run", "-d", 
                    "-p", "28998:3000", 
                    "--name", container_name, 
                    "--restart", "always",
                    "moefurina/ncm-api"
                ], check=True)
            
            INSTALL_STATUS.update({'status': 'success', 'progress': 100, 'step': '服务启动成功！'})
            logger.info("网易云服务安装/启动指令执行完成")
            
        except subprocess.CalledProcessError as e:
            msg = f"操作失败: {e}"
            logger.error(msg)
            INSTALL_STATUS.update({'status': 'error', 'error': msg, 'step': '发生错误'})
        except FileNotFoundError:
            msg = "未找到 Docker，请确保已安装 Docker Desktop"
            logger.error(msg)
            INSTALL_STATUS.update({'status': 'error', 'error': msg, 'step': '环境缺失'})
        except Exception as e:
            msg = f"未知错误: {str(e)}"
            logger.exception(msg)
            INSTALL_STATUS.update({'status': 'error', 'error': msg, 'step': '系统异常'})

    # 异步执行，避免阻塞
    threading.Thread(target=run_install, daemon=True).start()
    
    return jsonify({'success': True, 'message': '安装任务已启动'})

# --- QQ 音乐 API 配置 (内置实现，无需外部服务) ---
QQMUSIC_DOWNLOAD_DIR = None
QQMUSIC_DOWNLOAD_TASKS = {}
QQMUSIC_GUID = None
QQMUSIC_QIMEI = None  # 存储 QIMEI36
QQMUSIC_DEVICE = None  # 存储设备信息
QQMUSIC_CREDENTIAL = None  # 存储登录凭证 {musicid, musickey, musicname, headurl, refresh_key, refresh_token, login_type}
QQMUSIC_QR_CACHE = {}  # 存储二维码信息 {identifier: {type, qrsig/uuid, created_at}}

# QIMEI 相关常量
QIMEI_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDEIxgwoutfwoJxcGQeedgP7FG9qaIuS0qzfR8gWkrkTZKM2iWHn2ajQpBRZjMSoSf6+KJGvar2ORhBfpDXyVtZCKpqLQ+FLkpncClKVIrBwv6PHyUvuCb0rIarmgDnzkfQAqVufEtR64iazGDKatvJ9y6B9NMbHddGSAUmRTCrHQIDAQAB
-----END PUBLIC KEY-----"""
QIMEI_SECRET = "ZdJqM15EeO2zWc08"
QIMEI_APP_KEY = "0AND0HD6FE4HY80F"

import json as json_module
from base64 import b64encode, b64decode

def _save_qqmusic_credential(credential: dict):
    """保存 QQ 音乐登录凭证到数据库"""
    try:
        with get_db() as conn:
            value = json_module.dumps(credential) if credential else ''
            conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", 
                        ('qqmusic_credential', value))
            conn.commit()
        logger.info(f"[QQ音乐] 凭证已保存: musicid={credential.get('musicid') if credential else None}")
    except Exception as e:
        logger.warning(f"[QQ音乐] 保存凭证失败: {e}")

def _load_qqmusic_credential():
    """从数据库加载 QQ 音乐登录凭证"""
    global QQMUSIC_CREDENTIAL
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM system_settings WHERE key = ?", ('qqmusic_credential',)).fetchone()
            if row and row['value']:
                QQMUSIC_CREDENTIAL = json_module.loads(row['value'])
                logger.info(f"[QQ音乐] 凭证已加载: musicid={QQMUSIC_CREDENTIAL.get('musicid')}")
                return QQMUSIC_CREDENTIAL
    except Exception as e:
        logger.warning(f"[QQ音乐] 加载凭证失败: {e}")
    return None

def _refresh_qqmusic_credential():
    """刷新 QQ 音乐凭证"""
    global QQMUSIC_CREDENTIAL
    if not QQMUSIC_CREDENTIAL:
        logger.info("[QQ音乐] 无凭证，跳过刷新")
        return False
    
    refresh_key = QQMUSIC_CREDENTIAL.get('refresh_key')
    refresh_token = QQMUSIC_CREDENTIAL.get('refresh_token')
    musickey = QQMUSIC_CREDENTIAL.get('musickey')
    musicid = QQMUSIC_CREDENTIAL.get('musicid')
    login_type = QQMUSIC_CREDENTIAL.get('login_type', 2)
    
    if not refresh_key or not refresh_token:
        logger.warning("[QQ音乐] 缺少 refresh_key 或 refresh_token，无法刷新")
        return False
    
    try:
        logger.info(f"[QQ音乐] 开始刷新凭证: musicid={musicid}")
        result = _call_qqmusic_api_direct(
            'music.login.LoginServer',
            'Login',
            {
                'refresh_key': refresh_key,
                'refresh_token': refresh_token,
                'musickey': musickey,
                'musicid': musicid
            },
            extra_common={'tmeLoginType': str(login_type)}
        )
        
        data = result.get('data', result)
        if data and data.get('musicid'):
            QQMUSIC_CREDENTIAL = {
                'musicid': data.get('musicid'),
                'musickey': data.get('musickey'),
                'refresh_key': data.get('refresh_key'),
                'refresh_token': data.get('refresh_token'),
                'login_type': login_type,
                'refreshed_at': time.time()
            }
            _save_qqmusic_credential(QQMUSIC_CREDENTIAL)
            logger.info(f"[QQ音乐] 凭证刷新成功: musicid={QQMUSIC_CREDENTIAL.get('musicid')}")
            return True
        else:
            logger.warning(f"[QQ音乐] 凭证刷新失败: {result}")
            return False
    except Exception as e:
        logger.warning(f"[QQ音乐] 凭证刷新异常: {e}")
        return False

def _check_qqmusic_credential_expired():
    """检查 QQ 音乐凭证是否过期"""
    global QQMUSIC_CREDENTIAL
    if not QQMUSIC_CREDENTIAL or not QQMUSIC_CREDENTIAL.get('musickey'):
        return True
    
    try:
        # 调用用户信息接口检查凭证是否有效
        result = _call_qqmusic_api_direct(
            'music.UserInfo.userInfoServer',
            'GetLoginUserInfo',
            {}
        )
        # 如果返回了用户信息，说明凭证有效
        return result.get('code', -1) != 0
    except Exception as e:
        logger.warning(f"[QQ音乐] 检查凭证状态失败: {e}")
        return True

def _start_qqmusic_credential_refresh_task():
    """启动 QQ 音乐凭证定时刷新任务"""
    def refresh_loop():
        while True:
            try:
                # 每 6 小时检查一次
                time.sleep(6 * 60 * 60)
                
                if QQMUSIC_CREDENTIAL and QQMUSIC_CREDENTIAL.get('refresh_key'):
                    logger.info("[QQ音乐] 定时刷新凭证...")
                    _refresh_qqmusic_credential()
            except Exception as e:
                logger.warning(f"[QQ音乐] 定时刷新任务异常: {e}")
    
    thread = threading.Thread(target=refresh_loop, daemon=True)
    thread.start()
    logger.info("[QQ音乐] 凭证定时刷新任务已启动 (每6小时)")

def _random_imei():
    """生成随机 IMEI 号码"""
    import random
    imei = []
    sum_ = 0
    for i in range(14):
        num = random.randint(0, 9)
        if (i + 2) % 2 == 0:
            num *= 2
            if num >= 10:
                num = (num % 10) + 1
        sum_ += num
        imei.append(str(num))
    ctrl_digit = (sum_ * 9) % 10
    imei.append(str(ctrl_digit))
    return "".join(imei)

def _get_qqmusic_device():
    """获取或生成 QQ 音乐设备信息"""
    global QQMUSIC_DEVICE
    import random
    import string
    import binascii
    from uuid import uuid4
    
    if QQMUSIC_DEVICE:
        return QQMUSIC_DEVICE
    
    # 尝试从数据库加载
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM system_settings WHERE key = ?", ('qqmusic_device',)).fetchone()
            if row and row['value']:
                QQMUSIC_DEVICE = json_module.loads(row['value'])
                return QQMUSIC_DEVICE
    except Exception:
        pass
    
    # 生成新设备信息
    QQMUSIC_DEVICE = {
        'display': f"QMAPI.{random.randint(100000, 999999)}.001",
        'product': 'iarim',
        'device': 'sagit',
        'board': 'eomam',
        'model': 'MI 6',
        'fingerprint': f"xiaomi/iarim/sagit:10/eomam.200122.001/{random.randint(1000000, 9999999)}:user/release-keys",
        'boot_id': str(uuid4()),
        'proc_version': f"Linux 5.4.0-54-generic-{''.join(random.choices(string.ascii_letters + string.digits, k=8))} (android-build@google.com)",
        'imei': _random_imei(),
        'brand': 'Xiaomi',
        'android_id': binascii.hexlify(bytes([random.randint(0, 255) for _ in range(8)])).decode('utf-8'),
        'version_release': '10',
        'version_sdk': 29,
        'qimei': None
    }
    
    # 保存到数据库
    try:
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                        ('qqmusic_device', json_module.dumps(QQMUSIC_DEVICE)))
            conn.commit()
    except Exception:
        pass
    
    return QQMUSIC_DEVICE

def _random_beacon_id():
    """生成随机 BeaconID"""
    import random
    from datetime import datetime
    
    beacon_id = ""
    time_month = datetime.now().strftime("%Y-%m-") + "01"
    rand1 = random.randint(100000, 999999)
    rand2 = random.randint(100000000, 999999999)
    
    for i in range(1, 41):
        if i in [1, 2, 13, 14, 17, 18, 21, 22, 25, 26, 29, 30, 33, 34, 37, 38]:
            beacon_id += f"k{i}:{time_month}{rand1}.{rand2}"
        elif i == 3:
            beacon_id += "k3:0000000000000000"
        elif i == 4:
            beacon_id += f"k4:{''.join(random.choices('123456789abcdef', k=16))}"
        else:
            beacon_id += f"k{i}:{random.randint(0, 9999)}"
        beacon_id += ";"
    return beacon_id

def _calc_md5(*strings):
    """计算 MD5 值"""
    md5 = hashlib.md5()
    for item in strings:
        if isinstance(item, bytes):
            md5.update(item)
        elif isinstance(item, str):
            md5.update(item.encode())
    return md5.hexdigest()

def _get_qqmusic_qimei(version: str = "13.2.5.8"):
    """获取 QIMEI36"""
    global QQMUSIC_QIMEI, QQMUSIC_DEVICE
    import random
    import base64
    from datetime import datetime, timedelta
    
    device = _get_qqmusic_device()
    
    # 如果已有缓存的 QIMEI，直接返回
    if device.get('qimei'):
        QQMUSIC_QIMEI = device['qimei']
        return QQMUSIC_QIMEI
    
    try:
        # 尝试使用 cryptography 库进行 RSA 和 AES 加密
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        
        def rsa_encrypt(content: bytes) -> bytes:
            key = serialization.load_pem_public_key(QIMEI_PUBLIC_KEY.encode())
            return key.encrypt(content, padding.PKCS1v15())
        
        def aes_encrypt(key: bytes, content: bytes) -> bytes:
            cipher = Cipher(algorithms.AES(key), modes.CBC(key))
            padding_size = 16 - len(content) % 16
            encryptor = cipher.encryptor()
            return encryptor.update(content + (padding_size * chr(padding_size)).encode()) + encryptor.finalize()
        
        # 构建 payload
        fixed_rand = random.randint(0, 14400)
        reserved = {
            "harmony": "0",
            "clone": "0",
            "containe": "",
            "oz": "UhYmelwouA+V2nPWbOvLTgN2/m8jwGB+yUB5v9tysQg=",
            "oo": "Xecjt+9S1+f8Pz2VLSxgpw==",
            "kelong": "0",
            "uptimes": (datetime.now() - timedelta(seconds=fixed_rand)).strftime("%Y-%m-%d %H:%M:%S"),
            "multiUser": "0",
            "bod": device['brand'],
            "dv": device['device'],
            "firstLevel": "",
            "manufact": device['brand'],
            "name": device['model'],
            "host": "se.infra",
            "kernel": device['proc_version'],
        }
        
        import json as json_lib
        payload = {
            "androidId": device['android_id'],
            "platformId": 1,
            "appKey": QIMEI_APP_KEY,
            "appVersion": version,
            "beaconIdSrc": _random_beacon_id(),
            "brand": device['brand'],
            "channelId": "10003505",
            "cid": "",
            "imei": device['imei'],
            "imsi": "",
            "mac": "",
            "model": device['model'],
            "networkType": "unknown",
            "oaid": "",
            "osVersion": f"Android {device['version_release']},level {device['version_sdk']}",
            "qimei": "",
            "qimei36": "",
            "sdkVersion": "1.2.13.6",
            "targetSdkVersion": "33",
            "audit": "",
            "userId": "{}",
            "packageId": "com.tencent.qqmusic",
            "deviceType": "Phone",
            "sdkName": "",
            "reserved": json_lib.dumps(reserved, separators=(',', ':')),
        }
        
        crypt_key = "".join(random.choices("adbcdef1234567890", k=16))
        nonce = "".join(random.choices("adbcdef1234567890", k=16))
        ts = int(time.time())
        key = base64.b64encode(rsa_encrypt(crypt_key.encode())).decode()
        params = base64.b64encode(aes_encrypt(crypt_key.encode(), json_lib.dumps(payload, separators=(',', ':')).encode())).decode()
        extra = '{"appKey":"' + QIMEI_APP_KEY + '"}'
        sign = _calc_md5(key, params, str(ts * 1000), nonce, QIMEI_SECRET, extra)
        
        resp = requests.post(
            "https://api.tencentmusic.com/tme/trpc/proxy",
            headers={
                "Host": "api.tencentmusic.com",
                "method": "GetQimei",
                "service": "trpc.tme_datasvr.qimeiproxy.QimeiProxy",
                "appid": "qimei_qq_android",
                "sign": _calc_md5("qimei_qq_androidpzAuCmaFAaFaHrdakPjLIEqKrGnSOOvH", str(ts)),
                "user-agent": "QQMusic",
                "timestamp": str(ts),
            },
            json={
                "app": 0,
                "os": 1,
                "qimeiParams": {
                    "key": key,
                    "params": params,
                    "time": str(ts),
                    "nonce": nonce,
                    "sign": sign,
                    "extra": extra,
                },
            },
            timeout=10,
        )
        
        resp_data = resp.json()
        data = json_lib.loads(resp_data["data"])["data"]
        QQMUSIC_QIMEI = data["q36"]
        
        # 保存到设备信息
        device['qimei'] = QQMUSIC_QIMEI
        QQMUSIC_DEVICE = device
        try:
            with get_db() as conn:
                conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                            ('qqmusic_device', json_module.dumps(device)))
                conn.commit()
        except Exception:
            pass
        
        logger.info(f"[QQ音乐] 获取 QIMEI 成功: {QQMUSIC_QIMEI[:10]}...")
        return QQMUSIC_QIMEI
        
    except Exception as e:
        logger.warning(f"[QQ音乐] 获取 QIMEI 失败: {e}，使用默认值")
        # 使用默认 QIMEI
        QQMUSIC_QIMEI = "6c9d3cd110abca9b16311cee10001e717614"
        return QQMUSIC_QIMEI

def _get_qqmusic_guid():
    """获取或生成 QQ 音乐 GUID (现在返回 QIMEI)"""
    return _get_qqmusic_qimei()

def _qqmusic_sign(request_data: dict) -> str:
    """QQ 音乐请求签名 - 完全按照 QQMusicApi 实现"""
    import re
    import json
    
    PART_1_INDEXES = [23, 14, 6, 36, 16, 40, 7, 19]
    PART_2_INDEXES = [16, 1, 32, 12, 19, 27, 8, 5]
    SCRAMBLE_VALUES = [89, 39, 179, 150, 218, 82, 58, 252, 177, 52, 186, 123, 120, 64, 242, 133, 143, 161, 121, 179]
    
    # JavaScript quirks emulation - 过滤超出范围的索引
    part1_indexes = list(filter(lambda x: x < 40, PART_1_INDEXES))
    
    # 使用 json 序列化（orjson 可能不可用）
    # 注意：需要 separators 去除空格以匹配 orjson 的紧凑输出
    json_bytes = json.dumps(request_data, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    hash_str = hashlib.sha1(json_bytes).hexdigest().upper()
    
    part1 = ''.join(hash_str[i] for i in part1_indexes)
    part2 = ''.join(hash_str[i] for i in PART_2_INDEXES)
    
    part3 = bytearray(20)
    for i, v in enumerate(SCRAMBLE_VALUES):
        value = v ^ int(hash_str[i * 2: i * 2 + 2], 16)
        part3[i] = value
    
    b64_part = re.sub(rb'[\\/+=]', b'', b64encode(part3)).decode('utf-8')
    return f'zzc{part1}{b64_part}{part2}'.lower()

def _call_qqmusic_api_direct(module: str, method: str, params: dict = None, extra_common: dict = None):
    """直接调用 QQ 音乐 API (内置实现)
    
    Args:
        module: API 模块名
        method: API 方法名
        params: API 参数
        extra_common: 额外的 common 参数 (如 tmeLoginType)
    """
    import random
    global QQMUSIC_CREDENTIAL
    
    guid = _get_qqmusic_guid()
    
    # 构建请求数据
    common = {
        'ct': '11',
        'cv': '13020508',
        'v': '13020508',
        'tmeAppID': 'qqmusic',
        'format': 'json',
        'inCharset': 'utf-8',
        'outCharset': 'utf-8',
        'QIMEI36': guid,
        'uid': '3931641530',
    }
    
    # 如果已登录，添加凭证到 common 参数
    cookies = {}
    if QQMUSIC_CREDENTIAL and QQMUSIC_CREDENTIAL.get('musickey'):
        musicid = str(QQMUSIC_CREDENTIAL.get('musicid', ''))
        musickey = QQMUSIC_CREDENTIAL.get('musickey', '')
        login_type = str(QQMUSIC_CREDENTIAL.get('login_type', 2))
        common.update({
            'qq': musicid,
            'authst': musickey,
            'tmeLoginType': login_type,
        })
        # 设置 cookies
        cookies = {
            'uin': musicid,
            'qqmusic_key': musickey,
            'qm_keyst': musickey,
            'tmeLoginType': login_type,
        }
    
    # 合并额外的 common 参数
    if extra_common:
        common.update(extra_common)
    
    request_key = f'{module}.{method}'
    request_data = {
        'comm': common,
        request_key: {
            'module': module,
            'method': method,
            'param': params or {}
        }
    }
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 Edg/116.0.1938.54',
        'Referer': 'https://y.qq.com/',
        'Content-Type': 'application/json',
    }
    
    try:
        # 尝试使用签名端点
        url = 'https://u.y.qq.com/cgi-bin/musics.fcg'
        sign_val = _qqmusic_sign(request_data)
        resp = requests.post(url, params={'sign': sign_val}, json=request_data, headers=headers, cookies=cookies, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        result = data.get(request_key, {})
        code = result.get('code', 0)
        
        # 如果签名失败 (code=2000)，尝试不签名的端点
        if code == 2000:
            logger.info(f"[QQ音乐] 签名端点失败，尝试无签名端点: {module}.{method}")
            url_nosign = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
            resp = requests.post(url_nosign, json=request_data, headers=headers, cookies=cookies, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            result = data.get(request_key, {})
            code = result.get('code', 0)
        
        if code != 0:
            logger.warning(f"QQ音乐API返回错误: {module}.{method}, code={code}")
        return result
    except Exception as e:
        logger.warning(f"QQ音乐API调用失败: {module}.{method}, 错误: {e}")
        raise

def _qqmusic_hash33(s: str, h: int = 0) -> int:
    """QQ 音乐 hash33 算法"""
    for c in s:
        h = (h << 5) + h + ord(c)
        h = h & 0xFFFFFFFF
    return h

def _get_qq_qrcode():
    """获取 QQ 登录二维码"""
    import random
    try:
        resp = requests.get(
            'https://ssl.ptlogin2.qq.com/ptqrshow',
            params={
                'appid': '716027609',
                'e': '2',
                'l': 'M',
                's': '3',
                'd': '72',
                'v': '4',
                't': str(random.random()),
                'daid': '383',
                'pt_3rd_aid': '100497308',
            },
            headers={'Referer': 'https://xui.ptlogin2.qq.com/'},
            timeout=10
        )
        qrsig = resp.cookies.get('qrsig')
        if not qrsig:
            return None, None
        # 返回 base64 编码的图片和 qrsig
        img_b64 = b64encode(resp.content).decode('utf-8')
        return f'data:image/png;base64,{img_b64}', qrsig
    except Exception as e:
        logger.warning(f"获取 QQ 二维码失败: {e}")
        return None, None

def _get_wx_qrcode():
    """获取微信登录二维码"""
    try:
        resp = requests.get(
            'https://open.weixin.qq.com/connect/qrconnect',
            params={
                'appid': 'wx48db31d50e334801',
                'redirect_uri': 'https://y.qq.com/portal/wx_redirect.html?login_type=2&surl=https://y.qq.com/',
                'response_type': 'code',
                'scope': 'snsapi_login',
                'state': 'STATE',
                'href': 'https://y.qq.com/mediastyle/music_v17/src/css/popup_wechat.css#wechat_redirect',
            },
            timeout=10
        )
        import re
        match = re.findall(r"uuid=(.+?)\"", resp.text)
        if not match:
            return None, None
        uuid = match[0]
        # 获取二维码图片
        qr_resp = requests.get(
            f'https://open.weixin.qq.com/connect/qrcode/{uuid}',
            headers={'Referer': 'https://open.weixin.qq.com/connect/qrconnect'},
            timeout=10
        )
        img_b64 = b64encode(qr_resp.content).decode('utf-8')
        return f'data:image/jpeg;base64,{img_b64}', uuid
    except Exception as e:
        logger.warning(f"获取微信二维码失败: {e}")
        return None, None

def _check_qq_qrcode(qrsig: str):
    """检查 QQ 二维码状态
    
    返回: (status, credential_or_none)
    - status: 'scan', 'conf', 'done', 'timeout', 'refuse', 'error'
    - 当 status='done' 时，第二个参数直接返回 credential（已完成授权）
    """
    global QQMUSIC_CREDENTIAL
    
    # 先检查缓存
    qr_cache = QQMUSIC_QR_CACHE.get(qrsig, {})
    
    # 如果已经授权成功，直接返回凭证
    if qr_cache.get('authorized') and qr_cache.get('credential'):
        logger.info(f"[QQ二维码] 该二维码已授权，返回缓存凭证")
        return 'done', qr_cache.get('credential')
    
    # 如果已经尝试过授权但失败了，不再重复尝试
    if qr_cache.get('auth_attempted'):
        logger.info(f"[QQ二维码] 该二维码已尝试授权但失败，不再重复")
        return 'error', None
    
    try:
        ptqrtoken = _qqmusic_hash33(qrsig)
        logger.info(f"[QQ二维码] 检查状态: qrsig长度={len(qrsig)}, ptqrtoken={ptqrtoken}")
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://xui.ptlogin2.qq.com/',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Connection': 'keep-alive',
            'Cookie': f'qrsig={qrsig}'
        }
        
        resp = requests.get(
            'https://ssl.ptlogin2.qq.com/ptqrlogin',
            params={
                'u1': 'https://graph.qq.com/oauth2.0/login_jump',
                'ptqrtoken': ptqrtoken,
                'ptredirect': '0',
                'h': '1',
                't': '1',
                'g': '1',
                'from_ui': '1',
                'ptlang': '2052',
                'action': f'0-0-{int(time.time() * 1000)}',
                'js_ver': '20102616',
                'js_type': '1',
                'pt_uistyle': '40',
                'aid': '716027609',
                'daid': '383',
                'pt_3rd_aid': '100497308',
                'has_onekey': '1',
            },
            headers=headers,
            timeout=15
        )
        logger.info(f"[QQ二维码] HTTP状态码: {resp.status_code}, 响应长度: {len(resp.text)}")
        
        if resp.status_code == 403:
            logger.warning("[QQ二维码] 请求被拒绝 (403)，可能是频率限制")
            return 'scan', None
        
        import re
        match = re.search(r"ptuiCB\((.*?)\)", resp.text)
        if not match:
            logger.warning(f"[QQ二维码] 无法解析响应: '{resp.text[:500]}'")
            return 'scan', None
        data = [p.strip("'") for p in match.group(1).split(",")]
        code = int(data[0]) if data[0].isdigit() else -1
        
        logger.info(f"[QQ二维码] 响应码: {code}, 数据长度: {len(data)}")
        
        # 状态码: 66=等待扫码, 67=已扫码待确认, 65=超时, 0=成功, 68=拒绝
        if code == 0:
            # 登录成功，提取 sigx 和 uin，并立即进行授权
            redirect_url = data[2] if len(data) > 2 else ''
            logger.info(f"[QQ二维码] 登录成功，重定向URL: {redirect_url[:100]}...")
            sigx = re.findall(r"&ptsigx=(.+?)&s_url", redirect_url)
            uin = re.findall(r"&uin=(.+?)&service", redirect_url)
            
            if sigx and uin:
                logger.info(f"[QQ二维码] 提取成功: uin={uin[0]}, sigx长度={len(sigx[0])}")
                # 标记已尝试授权，防止重复调用
                QQMUSIC_QR_CACHE[qrsig] = {
                    'auth_attempted': True,
                    'attempted_at': time.time()
                }
                # 立即进行授权
                credential = _authorize_qq_login(uin[0], sigx[0])
                if credential:
                    # 授权成功，更新缓存
                    QQMUSIC_QR_CACHE[qrsig] = {
                        'authorized': True,
                        'credential': credential,
                        'authorized_at': time.time()
                    }
                    return 'done', credential
                else:
                    # 授权失败，保持 auth_attempted 标记
                    logger.warning("[QQ二维码] 授权失败，已标记不再重试")
                    return 'error', None
            logger.warning(f"[QQ二维码] 无法提取 sigx/uin，URL: {redirect_url}")
            return 'error', None
        elif code == 66:
            return 'scan', None
        elif code == 67:
            return 'conf', None
        elif code == 65:
            return 'timeout', None
        elif code == 68:
            return 'refuse', None
        return 'error', None
    except Exception as e:
        logger.warning(f"检查 QQ 二维码状态失败: {e}")
        return 'error', None

def _check_wx_qrcode(uuid: str):
    """检查微信二维码状态
    
    返回: (status, credential_or_none)
    - status: 'scan', 'conf', 'done', 'timeout', 'refuse', 'error'
    - 当 status='done' 时，第二个参数直接返回 credential（已完成授权）
    """
    global QQMUSIC_CREDENTIAL
    
    # 先检查缓存
    qr_cache = QQMUSIC_QR_CACHE.get(uuid, {})
    
    # 如果已经授权成功，直接返回凭证
    if qr_cache.get('authorized') and qr_cache.get('credential'):
        logger.info(f"[微信二维码] 该二维码已授权，返回缓存凭证")
        return 'done', qr_cache.get('credential')
    
    # 如果已经尝试过授权但失败了，不再重复尝试
    if qr_cache.get('auth_attempted'):
        logger.info(f"[微信二维码] 该二维码已尝试授权但失败，不再重复")
        return 'error', None
    
    try:
        resp = requests.get(
            'https://lp.open.weixin.qq.com/connect/l/qrconnect',
            params={'uuid': uuid, '_': str(int(time.time()) * 1000)},
            headers={'Referer': 'https://open.weixin.qq.com/'},
            timeout=30
        )
        import re
        match = re.search(r"window\.wx_errcode=(\d+);window\.wx_code=\'([^\']*)\'", resp.text)
        if not match:
            return 'error', None
        wx_errcode = int(match.group(1))
        wx_code = match.group(2)
        
        # 状态码: 408=等待扫码, 404=已扫码待确认, 405=成功, 403=拒绝
        if wx_errcode == 405:
            # 标记已尝试授权，防止重复调用
            QQMUSIC_QR_CACHE[uuid] = {
                'auth_attempted': True,
                'attempted_at': time.time()
            }
            # 登录成功，立即进行授权
            logger.info(f"[微信二维码] 扫码成功，开始授权: code={wx_code[:20]}...")
            credential = _authorize_wx_login(wx_code)
            if credential:
                # 授权成功，更新缓存
                QQMUSIC_QR_CACHE[uuid] = {
                    'authorized': True,
                    'credential': credential,
                    'authorized_at': time.time()
                }
                return 'done', credential
            else:
                # 授权失败，保持 auth_attempted 标记
                logger.warning("[微信二维码] 授权失败，已标记不再重试")
                return 'error', None
        elif wx_errcode == 408:
            return 'scan', None
        elif wx_errcode == 404:
            return 'conf', None
        elif wx_errcode == 403:
            return 'refuse', None
        return 'error', None
    except requests.exceptions.Timeout:
        return 'scan', None
    except Exception as e:
        logger.warning(f"检查微信二维码状态失败: {e}")
        return 'error', None

def _authorize_qq_login(uin: str, sigx: str):
    """QQ 登录授权"""
    global QQMUSIC_CREDENTIAL
    try:
        logger.info(f"[QQ授权] 开始授权流程: uin={uin}")
        session = requests.Session()
        # 第一步：check_sig
        resp = session.get(
            'https://ssl.ptlogin2.graph.qq.com/check_sig',
            params={
                'uin': uin,
                'pttype': '1',
                'service': 'ptqrlogin',
                'nodirect': '0',
                'ptsigx': sigx,
                's_url': 'https://graph.qq.com/oauth2.0/login_jump',
                'ptlang': '2052',
                'ptredirect': '100',
                'aid': '716027609',
                'daid': '383',
                'j_later': '0',
                'low_login_hour': '0',
                'regmaster': '0',
                'pt_login_type': '3',
                'pt_aid': '0',
                'pt_aaid': '16',
                'pt_light': '0',
                'pt_3rd_aid': '100497308',
            },
            headers={'Referer': 'https://xui.ptlogin2.qq.com/'},
            allow_redirects=False,
            timeout=10
        )
        p_skey = resp.cookies.get('p_skey')
        if not p_skey:
            logger.warning(f"[QQ授权] 获取 p_skey 失败, cookies: {dict(resp.cookies)}")
            return None
        logger.info(f"[QQ授权] 获取 p_skey 成功")
        
        # 第二步：authorize
        from uuid import uuid4
        resp = session.post(
            'https://graph.qq.com/oauth2.0/authorize',
            data={
                'response_type': 'code',
                'client_id': '100497308',
                'redirect_uri': 'https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/',
                'scope': 'get_user_info,get_app_friends',
                'state': 'state',
                'switch': '',
                'from_ptlogin': '1',
                'src': '1',
                'update_auth': '1',
                'openapi': '1010_1030',
                'g_tk': _qqmusic_hash33(p_skey, 5381),
                'auth_time': str(int(time.time()) * 1000),
                'ui': str(uuid4()),
            },
            allow_redirects=False,
            timeout=10
        )
        location = resp.headers.get('Location', '')
        logger.info(f"[QQ授权] authorize 响应 Location: {location[:100] if location else 'None'}...")
        import re
        
        # 检查是否有错误码
        error_match = re.search(r'error=(\d+)', location)
        if error_match:
            error_code = error_match.group(1)
            if error_code == '100046':
                logger.warning(f"[QQ授权] QQ OAuth 返回错误 100046: 授权频率过快，请稍后再试")
            else:
                logger.warning(f"[QQ授权] QQ OAuth 返回错误: {error_code}")
            return None
        
        code_match = re.findall(r"(?<=code=)(.+?)(?=&)", location)
        if not code_match:
            logger.warning(f"[QQ授权] 获取 code 失败, Location: {location}")
            return None
        code = code_match[0]
        logger.info(f"[QQ授权] 获取 code 成功: {code[:20]}...")
        
        # 第三步：调用 QQ 音乐 API 完成登录 (需要 tmeLoginType=2 表示 QQ 登录)
        result = _call_qqmusic_api_direct(
            'QQConnectLogin.LoginServer',
            'QQLogin',
            {'code': code},
            extra_common={'tmeLoginType': '2'}
        )
        logger.info(f"[QQ授权] QQLogin API 返回: {result}")
        data = result.get('data', result)
        if data and data.get('musicid'):
            QQMUSIC_CREDENTIAL = {
                'musicid': data.get('musicid'),
                'musickey': data.get('musickey'),
                'musicname': data.get('musicname') or data.get('nick') or f"QQ用户",
                'headurl': data.get('headurl') or data.get('headpic') or '',
                'refresh_key': data.get('refresh_key'),
                'refresh_token': data.get('refresh_token'),
                'login_type': 2  # QQ 登录
            }
            _save_qqmusic_credential(QQMUSIC_CREDENTIAL)  # 保存凭证
            logger.info(f"[QQ授权] 登录成功: musicid={QQMUSIC_CREDENTIAL.get('musicid')}, name={QQMUSIC_CREDENTIAL.get('musicname')}")
            return QQMUSIC_CREDENTIAL
        logger.warning(f"[QQ授权] QQLogin API 返回无效结果: {result}")
        return None
    except Exception as e:
        logger.warning(f"[QQ授权] 授权失败: {e}")
        import traceback
        logger.warning(f"[QQ授权] 堆栈: {traceback.format_exc()}")
        return None

def _authorize_wx_login(code: str):
    """微信登录授权"""
    global QQMUSIC_CREDENTIAL
    try:
        # 微信登录需要 tmeLoginType=1
        result = _call_qqmusic_api_direct(
            'music.login.LoginServer',
            'Login',
            {'code': code, 'strAppid': 'wx48db31d50e334801'},
            extra_common={'tmeLoginType': '1'}
        )
        data = result.get('data', result)
        if data and data.get('musicid'):
            QQMUSIC_CREDENTIAL = {
                'musicid': data.get('musicid'),
                'musickey': data.get('musickey'),
                'musicname': data.get('musicname') or data.get('nick') or f"微信用户",
                'headurl': data.get('headurl') or data.get('headpic') or '',
                'refresh_key': data.get('refresh_key'),
                'refresh_token': data.get('refresh_token'),
                'login_type': 1  # 微信登录
            }
            _save_qqmusic_credential(QQMUSIC_CREDENTIAL)  # 保存凭证
            logger.info(f"QQ 音乐微信登录成功: musicid={QQMUSIC_CREDENTIAL.get('musicid')}, name={QQMUSIC_CREDENTIAL.get('musicname')}")
            return QQMUSIC_CREDENTIAL
        logger.warning(f"[微信授权] Login API 返回无效结果: {result}")
        return None
    except Exception as e:
        logger.warning(f"微信登录授权失败: {e}")
        return None

def call_qqmusic_api(category: str, method: str, params: dict = None) -> dict:
    """
    统一的 QQ 音乐 API 调用接口
    将高级 API 调用映射到内置的直接调用实现
    
    Args:
        category: API 类别 (search, song, lyric, login 等)
        method: 方法名
        params: 参数字典
    
    Returns:
        统一格式的响应: {'code': 200, 'data': ..., 'message': ...}
    """
    global QQMUSIC_CREDENTIAL
    params = params or {}
    
    try:
        # 搜索相关
        if category == 'search':
            if method == 'search_by_type':
                keyword = params.get('keyword', '')
                num = params.get('num', 20)
                search_type = params.get('search_type', 0)
                
                # 生成 searchid
                import random
                searchid = ''.join(random.choices('0123456789', k=18))
                
                result = _call_qqmusic_api_direct(
                    'music.search.SearchCgiService',
                    'DoSearchForQQMusicMobile',
                    {
                        'searchid': searchid,
                        'query': keyword,
                        'num_per_page': num,
                        'page_num': 1,
                        'search_type': search_type,
                        'highlight': True,
                        'grp': True
                    }
                )
                
                # 提取歌曲列表
                data = result.get('data', result)
                body = data.get('body', {})
                # Mobile 版本返回的是 item_song
                songs = body.get('item_song', []) or body.get('song', {}).get('list', [])
                
                return {'code': 200, 'data': songs}
            
            elif method == 'hotkey':
                result = _call_qqmusic_api_direct(
                    'tencent_musicsoso_hotkey.HotkeyService',
                    'GetHotkeyForQQMusicPC',
                    {}
                )
                data = result.get('data', result)
                hotkeys = data.get('vec_hotkey', [])
                return {'code': 200, 'data': hotkeys}
        
        # 歌曲相关
        elif category == 'song':
            if method == 'get_detail':
                value = params.get('value', '')
                # 判断是 mid 还是 id
                if isinstance(value, str) and not value.isdigit():
                    # 是 mid
                    result = _call_qqmusic_api_direct(
                        'music.pf_song_detail_svr',
                        'get_song_detail_yqq',
                        {'song_mid': value}
                    )
                else:
                    # 是 id
                    result = _call_qqmusic_api_direct(
                        'music.pf_song_detail_svr',
                        'get_song_detail_yqq',
                        {'song_id': int(value)}
                    )
                data = result.get('data', result)
                return {'code': 200, 'data': data}
            
            elif method == 'get_song_urls':
                mid = params.get('mid', '')
                file_type = params.get('file_type', 'MP3_128')
                
                # 文件类型映射 (使用正确的前缀)
                type_map = {
                    'MP3_128': ('M500', '.mp3'),
                    'MP3_320': ('M800', '.mp3'),
                    'FLAC': ('F000', '.flac'),
                    'OGG_192': ('O600', '.ogg'),
                    'OGG_320': ('O800', '.ogg'),
                    'ACC_192': ('C600', '.m4a'),
                    'ACC_96': ('C400', '.m4a'),
                }
                
                prefix, ext = type_map.get(file_type, ('M500', '.mp3'))
                filename = f"{prefix}{mid}{mid}{ext}"
                
                guid = _get_qqmusic_guid()
                
                # 构建请求参数 - 参考 QQMusicApi 的实现
                api_params = {
                    'filename': [filename],
                    'guid': guid,
                    'songmid': [mid],
                    'songtype': [0],
                }
                
                is_logged_in = bool(QQMUSIC_CREDENTIAL and QQMUSIC_CREDENTIAL.get('musickey'))
                logger.info(f"[QQ音乐] 获取歌曲URL: mid={mid}, file_type={file_type}, 已登录={is_logged_in}")
                if is_logged_in:
                    logger.info(f"[QQ音乐] 使用凭证: musicid={QQMUSIC_CREDENTIAL.get('musicid')}")
                
                result = _call_qqmusic_api_direct(
                    'music.vkey.GetVkey',
                    'UrlGetVkey',
                    api_params
                )
                
                # 解析返回的 URL
                data = result.get('data', result)
                code = result.get('code', 0)
                logger.info(f"[QQ音乐] GetVkey 响应 code={code}")
                
                midurlinfo = data.get('midurlinfo', [])
                urls = {}
                domain = 'https://isure.stream.qqmusic.qq.com/'
                for info in midurlinfo:
                    song_mid = info.get('songmid', '')
                    # 优先使用 wifiurl，然后是 purl
                    song_url = info.get('wifiurl', '') or info.get('purl', '')
                    if song_url:
                        urls[song_mid] = domain + song_url
                        logger.info(f"[QQ音乐] 获取到URL: {song_mid} -> {song_url[:50]}...")
                    else:
                        logger.warning(f"[QQ音乐] 歌曲 {song_mid} 无法获取URL, info={info}")
                
                return {'code': 200, 'data': urls}
        
        # 歌词相关
        elif category == 'lyric':
            if method == 'get_lyric':
                value = params.get('value', '')
                
                # 判断是 mid 还是 id
                if isinstance(value, str) and not value.isdigit():
                    song_mid = value
                    song_id = 0
                else:
                    song_mid = ''
                    song_id = int(value) if value else 0
                
                result = _call_qqmusic_api_direct(
                    'music.musichallSong.PlayLyricInfo',
                    'GetPlayLyricInfo',
                    {
                        'songMID': song_mid,
                        'songID': song_id
                    }
                )
                
                # 解析歌词
                import base64
                lyric_data = {}
                data = result.get('data', result)
                
                lyric_b64 = data.get('lyric', '')
                if lyric_b64:
                    try:
                        lyric_data['lrc'] = base64.b64decode(lyric_b64).decode('utf-8')
                    except:
                        lyric_data['lrc'] = ''
                
                trans_b64 = data.get('trans', '')
                if trans_b64:
                    try:
                        lyric_data['trans'] = base64.b64decode(trans_b64).decode('utf-8')
                    except:
                        lyric_data['trans'] = ''
                
                return {'code': 200, 'data': lyric_data}
        
        # 登录相关
        elif category == 'login':
            if method == 'get_qrcode':
                login_type = params.get('login_type', 'QQ').upper()
                if login_type == 'WX':
                    img_data, identifier = _get_wx_qrcode()
                else:
                    img_data, identifier = _get_qq_qrcode()
                
                if img_data and identifier:
                    # 缓存二维码信息
                    QQMUSIC_QR_CACHE[identifier] = {
                        'type': login_type,
                        'created_at': time.time()
                    }
                    return {
                        'code': 200,
                        'data': {
                            'data': img_data,
                            'identifier': identifier,
                            'qr_type': login_type
                        }
                    }
                return {'code': 500, 'message': '获取二维码失败'}
            
            elif method == 'check_qrcode':
                identifier = params.get('identifier', '')
                qr_type = params.get('qr_type', 'QQ').upper()
                
                logger.info(f"[QQ登录] 检查二维码状态: identifier={identifier[:20]}..., qr_type={qr_type}")
                
                # 调用检查函数（内部已包含授权逻辑和缓存检查）
                if qr_type == 'WX':
                    status, credential = _check_wx_qrcode(identifier)
                else:
                    status, credential = _check_qq_qrcode(identifier)
                
                logger.info(f"[QQ登录] 二维码状态: status={status}, has_credential={credential is not None}")
                
                event_map = {
                    'done': 'DONE',
                    'scan': 'SCAN',
                    'conf': 'CONF',
                    'timeout': 'TIMEOUT',
                    'refuse': 'REFUSE',
                    'error': 'OTHER'
                }
                
                result = {
                    'code': 200,
                    'data': {
                        'event': event_map.get(status, 'OTHER'),
                        'credential': credential  # 直接使用返回的 credential
                    }
                }
                
                # 如果状态是 done 但没有 credential，说明授权失败
                if status == 'done' and not credential:
                    logger.warning("[QQ登录] 状态为 done 但无 credential，授权可能失败")
                    result['data']['event'] = 'OTHER'
                
                return result
            
            elif method == 'get_status':
                # 检查当前登录状态
                if QQMUSIC_CREDENTIAL and QQMUSIC_CREDENTIAL.get('musickey'):
                    return {
                        'code': 200,
                        'data': {
                            'logged_in': True,
                            'musicid': QQMUSIC_CREDENTIAL.get('musicid')
                        }
                    }
                return {'code': 200, 'data': {'logged_in': False}}
            
            elif method == 'logout':
                QQMUSIC_CREDENTIAL = None
                return {'code': 200, 'data': {'success': True}}
            
            elif method == 'send_authcode':
                # 发送手机验证码
                phone = params.get('phone', '')
                country_code = params.get('country_code', '86')
                
                if not phone:
                    return {'code': 400, 'message': '缺少手机号'}
                
                logger.info(f"[QQ手机登录] 发送验证码: phone={phone[:3]}***{phone[-4:]}")
                
                result = _call_qqmusic_api_direct(
                    'music.login.LoginServer',
                    'SendPhoneAuthCode',
                    {
                        'tmeAppid': 'qqmusic',
                        'phoneNo': str(phone),
                        'areaCode': str(country_code)
                    },
                    extra_common={'tmeLoginMethod': '3'}
                )
                
                code = result.get('code', -1)
                logger.info(f"[QQ手机登录] API返回: code={code}, result={result}")
                if code == 0:
                    logger.info("[QQ手机登录] 验证码发送成功")
                    return {'code': 200, 'data': {'status': 'sent'}}
                elif code == 20276:
                    # 需要滑块验证
                    data = result.get('data', {})
                    security_url = data.get('securityURL', '') or data.get('security_url', '')
                    logger.warning(f"[QQ手机登录] 需要滑块验证: {security_url}")
                    return {'code': 200, 'data': {'status': 'captcha', 'security_url': security_url}}
                elif code == 100001:
                    logger.warning("[QQ手机登录] 操作过于频繁")
                    return {'code': 200, 'data': {'status': 'frequency'}}
                else:
                    data = result.get('data', {})
                    err_msg = data.get('errMsg', '') or data.get('msg', '') or f'发送失败(code={code})'
                    logger.warning(f"[QQ手机登录] 发送验证码失败: code={code}, err_msg={err_msg}, data={data}")
                    return {'code': 500, 'message': err_msg}
            
            elif method == 'phone_login':
                # 手机验证码登录
                phone = params.get('phone', '')
                auth_code = params.get('auth_code', '')
                country_code = params.get('country_code', '86')
                
                if not phone or not auth_code:
                    return {'code': 400, 'message': '缺少手机号或验证码'}
                
                logger.info(f"[QQ手机登录] 验证码登录: phone={phone[:3]}***{phone[-4:]}")
                
                result = _call_qqmusic_api_direct(
                    'music.login.LoginServer',
                    'Login',
                    {
                        'code': str(auth_code),
                        'phoneNo': str(phone),
                        'areaCode': str(country_code),
                        'loginMode': 1
                    },
                    extra_common={'tmeLoginMethod': '3', 'tmeLoginType': '0'}
                )
                
                code = result.get('code', -1)
                if code == 0:
                    data = result.get('data', result)
                    QQMUSIC_CREDENTIAL = {
                        'musicid': data.get('musicid'),
                        'musickey': data.get('musickey'),
                        'musicname': data.get('musicname') or data.get('nick') or '手机用户',
                        'headurl': data.get('headurl') or data.get('headpic') or '',
                        'refresh_key': data.get('refresh_key'),
                        'refresh_token': data.get('refresh_token'),
                        'login_type': 0  # 手机登录
                    }
                    _save_qqmusic_credential(QQMUSIC_CREDENTIAL)
                    logger.info(f"[QQ手机登录] 登录成功: musicid={QQMUSIC_CREDENTIAL.get('musicid')}")
                    return {'code': 200, 'data': {'status': 'success', 'credential': QQMUSIC_CREDENTIAL}}
                elif code == 20274:
                    logger.warning("[QQ手机登录] 设备数量限制")
                    return {'code': 200, 'data': {'status': 'device_limit'}}
                elif code == 20271:
                    logger.warning("[QQ手机登录] 验证码错误或已使用")
                    return {'code': 200, 'data': {'status': 'code_error'}}
                else:
                    logger.warning(f"[QQ手机登录] 登录失败: code={code}")
                    return {'code': 500, 'message': '登录失败'}
        
        # 歌单相关
        elif category == 'playlist':
            if method == 'get_user_playlists':
                uin = params.get('uin', '')
                if not uin:
                    return {'code': 400, 'message': '缺少用户ID'}
                
                result = _call_qqmusic_api_direct(
                    'music.srfDissInfo.DissInfo',
                    'CgiGetUserUgc',
                    {
                        'uin': str(uin),
                        'is_self': 1,
                        'start': 0,
                        'num': 100,
                        'type': 0
                    }
                )
                
                data = result.get('data', result)
                diss_list = data.get('ugclist', [])
                
                # 格式化歌单列表
                playlists = []
                for diss in diss_list:
                    playlists.append({
                        'tid': diss.get('tid', ''),
                        'diss_name': diss.get('title', ''),
                        'diss_cover': diss.get('cover', ''),
                        'song_cnt': diss.get('song_cnt', 0),
                        'creator': {'nick': diss.get('creator', {}).get('nick', '') if isinstance(diss.get('creator'), dict) else ''}
                    })
                
                return {'code': 200, 'data': playlists}
            
            elif method == 'get_playlist_detail':
                playlist_id = params.get('id', '')
                if not playlist_id:
                    return {'code': 400, 'message': '缺少歌单ID'}
                
                # 分页获取所有歌曲（每次最多500首）
                all_songs = []
                song_begin = 0
                page_size = 500
                dissname = ''
                
                while True:
                    result = _call_qqmusic_api_direct(
                        'music.srfDissInfo.DissInfo',
                        'CgiGetDiss',
                        {
                            'disstid': int(playlist_id),
                            'song_begin': song_begin,
                            'song_num': page_size,
                            'onlysonglist': 0 if song_begin == 0 else 1,  # 第一次获取歌单信息
                            'orderlist': 1
                        }
                    )
                    
                    data = result.get('data', result)
                    
                    # 第一次获取歌单名称
                    if song_begin == 0:
                        dirinfo = data.get('dirinfo', {})
                        dissname = dirinfo.get('title', '')
                    
                    songlist = data.get('songlist', [])
                    if not songlist:
                        break
                    
                    all_songs.extend(songlist)
                    
                    # 如果返回的歌曲数量少于请求的数量，说明已经获取完毕
                    if len(songlist) < page_size:
                        break
                    
                    song_begin += page_size
                    
                    # 安全限制：最多获取3000首
                    if song_begin >= 3000:
                        logger.warning(f'歌单 {playlist_id} 歌曲数量超过3000首，停止获取')
                        break
                
                logger.info(f'获取歌单 {playlist_id} 完成，共 {len(all_songs)} 首歌曲')
                
                return {
                    'code': 200,
                    'data': {
                        'dissname': dissname,
                        'songlist': all_songs
                    }
                }
        
        # 未知的 API
        return {'code': 404, 'message': f'未知的 API: {category}.{method}'}
        
    except Exception as e:
        logger.warning(f"call_qqmusic_api 调用失败: {category}.{method}, 错误: {e}")
        return {'code': 500, 'message': str(e)}


def _format_qqmusic_songs(songs: list) -> list:
    """格式化 QQ 音乐歌曲列表"""
    result = []
    for item in songs:
        try:
            song_id = item.get('id') or item.get('songid')
            mid = item.get('mid') or item.get('songmid') or ''
            title = item.get('title') or item.get('name') or item.get('songname') or f'未命名 {song_id}'
            
            # 处理歌手
            singers = item.get('singer') or item.get('singers') or []
            if isinstance(singers, list):
                artist = ' / '.join([s.get('name', '') for s in singers if s.get('name')])
            else:
                artist = str(singers)
            artist = artist or '未知艺术家'
            
            # 处理专辑
            album_info = item.get('album') or {}
            if isinstance(album_info, dict):
                album = album_info.get('name') or album_info.get('title') or ''
                album_mid = album_info.get('mid') or album_info.get('pmid') or ''
            else:
                album = str(album_info) if album_info else ''
                album_mid = ''
            
            # 封面
            cover = ''
            if album_mid:
                cover = f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg"
            elif mid:
                cover = f"https://y.qq.com/music/photo_new/T002R300x300M000{mid}.jpg"
            
            # 时长
            duration = item.get('interval') or item.get('duration') or 0
            
            # VIP 状态
            pay = item.get('pay') or {}
            is_vip = pay.get('pay_play') == 1 if isinstance(pay, dict) else False
            
            result.append({
                'id': song_id,
                'mid': mid,
                'title': title,
                'artist': artist,
                'album': album,
                'album_mid': album_mid,
                'cover': cover,
                'duration': duration,
                'is_vip': is_vip
            })
        except Exception as e:
            logger.warning(f"格式化QQ音乐歌曲失败: {e}")
            continue
    return result

@app.route('/api/qqmusic/config', methods=['GET', 'POST'])
def qqmusic_config():
    """获取或更新 QQ 音乐配置 (内置实现，无需外部 API)"""
    global QQMUSIC_DOWNLOAD_DIR
    if request.method == 'GET':
        # 默认使用挂载目录
        default_dir = get_default_download_dir()
        return jsonify({
            'success': True,
            'download_dir': QQMUSIC_DOWNLOAD_DIR or default_dir
        })
    try:
        data = request.get_json() or {}
        if 'download_dir' in data:
            QQMUSIC_DOWNLOAD_DIR = data['download_dir'].strip() or None
        default_dir = get_default_download_dir()
        return jsonify({
            'success': True,
            'download_dir': QQMUSIC_DOWNLOAD_DIR or default_dir
        })
    except Exception as e:
        logger.error(f"保存QQ音乐配置失败: {e}")
        return jsonify({'success': False, 'error': '保存失败'})

@app.route('/api/qqmusic/search')
def search_qqmusic():
    """搜索 QQ 音乐"""
    keywords = (request.args.get('keywords') or '').strip()
    if not keywords:
        return jsonify({'success': False, 'error': '请输入搜索关键词'})
    num = request.args.get('num', 20)
    try:
        num = max(1, min(int(num), 50))
    except:
        num = 20
    
    try:
        resp = call_qqmusic_api('search', 'search_by_type', {
            'keyword': keywords,
            'num': num,
            'search_type': 0  # 歌曲
        })
        if resp.get('code') == 200:
            songs = resp.get('data') or []
            formatted = _format_qqmusic_songs(songs)
            return jsonify({'success': True, 'data': formatted})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '搜索失败'})
    except Exception as e:
        logger.warning(f"QQ音乐搜索失败: {e}")
        return jsonify({'success': False, 'error': '搜索失败，请检查 QQ 音乐 API 服务'})

@app.route('/api/qqmusic/song/detail')
def qqmusic_song_detail():
    """获取歌曲详情"""
    mid = request.args.get('mid', '').strip()
    song_id = request.args.get('id', '').strip()
    if not mid and not song_id:
        return jsonify({'success': False, 'error': '请提供歌曲 mid 或 id'})
    
    try:
        value = mid if mid else song_id
        resp = call_qqmusic_api('song', 'get_detail', {'value': value})
        if resp.get('code') == 200:
            return jsonify({'success': True, 'data': resp.get('data')})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取详情失败'})
    except Exception as e:
        logger.warning(f"获取QQ音乐详情失败: {e}")
        return jsonify({'success': False, 'error': '获取详情失败'})

@app.route('/api/qqmusic/song/url')
def qqmusic_song_url():
    """获取歌曲播放链接"""
    mid = request.args.get('mid', '').strip()
    if not mid:
        return jsonify({'success': False, 'error': '请提供歌曲 mid'})
    
    file_type = request.args.get('type', 'MP3_128')
    
    try:
        resp = call_qqmusic_api('song', 'get_song_urls', {
            'mid': mid,
            'file_type': file_type
        })
        if resp.get('code') == 200:
            urls = resp.get('data') or {}
            return jsonify({'success': True, 'data': urls})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取链接失败'})
    except Exception as e:
        logger.warning(f"获取QQ音乐链接失败: {e}")
        return jsonify({'success': False, 'error': '获取链接失败'})

@app.route('/api/qqmusic/lyric')
def qqmusic_lyric():
    """获取歌词"""
    mid = request.args.get('mid', '').strip()
    song_id = request.args.get('id', '').strip()
    if not mid and not song_id:
        return jsonify({'success': False, 'error': '请提供歌曲 mid 或 id'})
    
    try:
        value = mid if mid else int(song_id)
        resp = call_qqmusic_api('lyric', 'get_lyric', {'value': value})
        if resp.get('code') == 200:
            return jsonify({'success': True, 'data': resp.get('data')})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取歌词失败'})
    except Exception as e:
        logger.warning(f"获取QQ音乐歌词失败: {e}")
        return jsonify({'success': False, 'error': '获取歌词失败'})

@app.route('/api/qqmusic/login/qrcode')
def qqmusic_login_qrcode():
    """获取 QQ 音乐登录二维码"""
    login_type = request.args.get('type', 'qq')  # qq 或 wx
    try:
        resp = call_qqmusic_api('login', 'get_qrcode', {'login_type': login_type.upper()})
        if resp.get('code') == 200:
            data = resp.get('data') or {}
            return jsonify({
                'success': True,
                'qrimg': data.get('data'),  # base64 图片数据
                'identifier': data.get('identifier'),
                'qr_type': data.get('qr_type')
            })
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取二维码失败'})
    except Exception as e:
        logger.warning(f"获取QQ音乐二维码失败: {e}")
        return jsonify({'success': False, 'error': '获取二维码失败'})

@app.route('/api/qqmusic/login/check')
def qqmusic_login_check():
    """检查 QQ 音乐登录状态"""
    identifier = request.args.get('identifier', '').strip()
    qr_type = request.args.get('qr_type', 'qq')
    if not identifier:
        return jsonify({'success': False, 'error': '缺少 identifier'})
    
    try:
        resp = call_qqmusic_api('login', 'check_qrcode', {
            'identifier': identifier,
            'qr_type': qr_type.upper()
        })
        if resp.get('code') == 200:
            data = resp.get('data') or {}
            event = data.get('event')
            credential = data.get('credential')
            
            status_map = {
                'DONE': 'authorized',
                'SCAN': 'waiting',
                'CONF': 'scanned',
                'TIMEOUT': 'expired',
                'REFUSE': 'refused',
                'OTHER': 'error'  # 授权失败
            }
            
            return jsonify({
                'success': True,
                'status': status_map.get(event, 'error' if event == 'OTHER' else 'waiting'),
                'credential': credential
            })
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '检查状态失败'})
    except Exception as e:
        logger.warning(f"检查QQ音乐登录状态失败: {e}")
        return jsonify({'success': False, 'error': '检查状态失败'})

@app.route('/api/qqmusic/login/status')
def qqmusic_login_status():
    """检查 QQ 音乐 API 连接和登录状态"""
    try:
        # 内置实现，始终连接
        connected = True
        
        # 检查登录状态
        logged_in = False
        user_info = None
        if QQMUSIC_CREDENTIAL and QQMUSIC_CREDENTIAL.get('musickey'):
            logged_in = True
            user_info = {
                'musicid': QQMUSIC_CREDENTIAL.get('musicid'),
                'musicname': QQMUSIC_CREDENTIAL.get('musicname') or 'QQ用户',
                'headurl': QQMUSIC_CREDENTIAL.get('headurl') or '',
                'login_type': 'QQ' if QQMUSIC_CREDENTIAL.get('login_type') == 2 else 'WX',
                'is_vip': QQMUSIC_CREDENTIAL.get('is_vip', False)
            }
        
        return jsonify({
            'success': True,
            'connected': connected,
            'logged_in': logged_in,
            'user': user_info
        })
    except Exception as e:
        logger.warning(f"检查QQ音乐API状态失败: {e}")
        return jsonify({'success': False, 'connected': False, 'logged_in': False, 'error': str(e)})

@app.route('/api/qqmusic/logout', methods=['POST'])
def qqmusic_logout():
    """退出 QQ 音乐登录"""
    global QQMUSIC_CREDENTIAL
    QQMUSIC_CREDENTIAL = None
    _save_qqmusic_credential(None)  # 清除保存的凭证
    return jsonify({'success': True, 'message': '已退出登录'})

@app.route('/api/qqmusic/login/phone/send', methods=['POST'])
def qqmusic_phone_send():
    """发送手机验证码"""
    data = request.get_json() or {}
    phone = data.get('phone', '').strip()
    country_code = data.get('country_code', '86')
    
    if not phone:
        return jsonify({'success': False, 'error': '请输入手机号'})
    
    try:
        resp = call_qqmusic_api('login', 'send_authcode', {
            'phone': phone,
            'country_code': country_code
        })
        if resp.get('code') == 200:
            data = resp.get('data', {})
            status = data.get('status')
            if status == 'sent':
                return jsonify({'success': True, 'status': 'sent', 'message': '验证码已发送'})
            elif status == 'captcha':
                return jsonify({
                    'success': True, 
                    'status': 'captcha', 
                    'security_url': data.get('security_url'),
                    'message': '需要完成滑块验证'
                })
            elif status == 'frequency':
                return jsonify({'success': False, 'status': 'frequency', 'error': '操作过于频繁，请稍后再试'})
            else:
                return jsonify({'success': False, 'error': '发送失败'})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '发送失败'})
    except Exception as e:
        logger.warning(f"发送QQ音乐验证码失败: {e}")
        return jsonify({'success': False, 'error': '发送验证码失败'})

@app.route('/api/qqmusic/login/phone/verify', methods=['POST'])
def qqmusic_phone_verify():
    """手机验证码登录"""
    data = request.get_json() or {}
    phone = data.get('phone', '').strip()
    auth_code = data.get('auth_code', '').strip()
    country_code = data.get('country_code', '86')
    
    if not phone or not auth_code:
        return jsonify({'success': False, 'error': '请输入手机号和验证码'})
    
    try:
        resp = call_qqmusic_api('login', 'phone_login', {
            'phone': phone,
            'auth_code': auth_code,
            'country_code': country_code
        })
        if resp.get('code') == 200:
            data = resp.get('data', {})
            status = data.get('status')
            if status == 'success':
                return jsonify({
                    'success': True, 
                    'status': 'success',
                    'credential': data.get('credential'),
                    'message': '登录成功'
                })
            elif status == 'device_limit':
                return jsonify({'success': False, 'status': 'device_limit', 'error': '设备数量已达上限'})
            elif status == 'code_error':
                return jsonify({'success': False, 'status': 'code_error', 'error': '验证码错误或已过期'})
            else:
                return jsonify({'success': False, 'error': '登录失败'})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '登录失败'})
    except Exception as e:
        logger.warning(f"QQ音乐手机登录失败: {e}")
        return jsonify({'success': False, 'error': '登录失败'})

@app.route('/api/qqmusic/login/cookie', methods=['POST'])
def qqmusic_cookie_login():
    """Cookie 登录 - 用户手动输入 musicid 和 musickey"""
    global QQMUSIC_CREDENTIAL
    data = request.get_json() or {}
    musicid = data.get('musicid', '').strip()
    musickey = data.get('musickey', '').strip()
    
    if not musicid or not musickey:
        return jsonify({'success': False, 'error': '请输入 musicid 和 qqmusic_key'})
    
    # 验证 musickey 格式 - 放宽验证，只检查长度
    if len(musickey) < 20:
        return jsonify({'success': False, 'error': 'qqmusic_key 格式不正确'})
    
    try:
        # 判断登录类型：Q_H_ 是 QQ 登录，W_X_ 是微信登录
        login_type = 2 if musickey.startswith('Q_H_') else 1
        
        # 默认用户信息
        musicname = f'用户{musicid[-4:]}'
        headurl = ''
        encrypt_uin = ''
        
        # 先保存凭证，这样后续 API 调用可以使用
        QQMUSIC_CREDENTIAL = {
            'musicid': musicid,
            'musickey': musickey,
            'musicname': musicname,
            'headurl': headurl,
            'refresh_key': '',
            'refresh_token': '',
            'login_type': login_type,
            'encrypt_uin': ''
        }
        
        # 方法1: 使用 GetLoginUserInfo API 获取当前登录用户信息
        try:
            result = _call_qqmusic_api_direct(
                'music.UserInfo.userInfoServer',
                'GetLoginUserInfo',
                {}
            )
            logger.info(f"[QQ音乐] GetLoginUserInfo 响应: {str(result)[:800]}")
            user_data = result.get('data', result)
            if user_data:
                # 尝试多个可能的字段名获取昵称
                nick = user_data.get('nick', '') or user_data.get('nickname', '') or user_data.get('name', '') or user_data.get('musicname', '')
                if nick:
                    musicname = nick
                # 尝试多个可能的字段名获取头像
                pic = user_data.get('headpic', '') or user_data.get('headurl', '') or user_data.get('pic', '') or user_data.get('avatar', '') or user_data.get('picurl', '')
                if pic:
                    headurl = pic
                    if headurl and not headurl.startswith('http'):
                        headurl = f'https:{headurl}' if headurl.startswith('//') else f'https://{headurl}'
                # 获取 encrypt_uin
                encrypt_uin = user_data.get('encryptUin', '') or user_data.get('encrypt_uin', '') or user_data.get('euin', '')
                logger.info(f"[QQ音乐] 方法1(GetLoginUserInfo)获取用户信息: name={musicname}, headurl={headurl[:50] if headurl else ''}, encrypt_uin={encrypt_uin}")
        except Exception as e:
            logger.warning(f"[QQ音乐] 方法1(GetLoginUserInfo)获取用户信息失败: {e}")
        
        # 方法2: 使用 fcg_get_profile_homepage.fcg 获取用户信息和 encrypt_uin
        if musicname == f'用户{musicid[-4:]}' or not encrypt_uin:
            try:
                profile_url = 'https://c6.y.qq.com/rsc/fcgi-bin/fcg_get_profile_homepage.fcg'
                profile_params = {
                    'ct': 20,
                    'cv': 4747474,
                    'cid': 205360838,
                    'userid': musicid,
                }
                profile_resp = requests.get(profile_url, params=profile_params, timeout=5, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Referer': 'https://y.qq.com/',
                })
                logger.info(f"[QQ音乐] fcg_get_profile_homepage 响应: {profile_resp.status_code}")
                if profile_resp.status_code == 200:
                    profile_data = profile_resp.json()
                    logger.info(f"[QQ音乐] fcg_get_profile_homepage 数据: {str(profile_data)[:800]}")
                    creator = profile_data.get('data', {}).get('creator', {})
                    if creator:
                        # 获取 encrypt_uin 用于后续 API 调用
                        if not encrypt_uin:
                            encrypt_uin = creator.get('encrypt_uin', '')
                        # 尝试多个可能的字段名
                        if musicname == f'用户{musicid[-4:]}':
                            nick = creator.get('nick', '') or creator.get('hostname', '') or creator.get('name', '')
                            if nick:
                                musicname = nick
                        if not headurl:
                            pic = creator.get('headpic', '') or creator.get('pic', '') or creator.get('avatar', '')
                            if pic:
                                headurl = pic
                                if headurl and not headurl.startswith('http'):
                                    headurl = f'https:{headurl}' if headurl.startswith('//') else f'https://{headurl}'
                        logger.info(f"[QQ音乐] 方法2(fcg_get_profile_homepage)获取用户信息: name={musicname}, headurl={headurl[:50] if headurl else ''}, encrypt_uin={encrypt_uin}")
            except Exception as e:
                logger.warning(f"[QQ音乐] 方法2(fcg_get_profile_homepage)获取用户信息失败: {e}")
        
        # 方法3: 如果还没有获取到昵称，尝试使用 GetHomepageHeader API (需要 encrypt_uin)
        if musicname == f'用户{musicid[-4:]}' and encrypt_uin:
            try:
                result = _call_qqmusic_api_direct(
                    'music.UnifiedHomepage.UnifiedHomepageSrv',
                    'GetHomepageHeader',
                    {'uin': encrypt_uin, 'IsQueryTabDetail': 1}
                )
                logger.info(f"[QQ音乐] GetHomepageHeader 响应: {str(result)[:500]}")
                resp_data = result.get('data', result)
                if resp_data:
                    user_info = resp_data.get('creator', {}) or resp_data.get('userInfo', {}) or resp_data
                    if user_info:
                        nick = user_info.get('nick', '') or user_info.get('name', '') or user_info.get('nickname', '')
                        if nick:
                            musicname = nick
                        pic = user_info.get('headpic', '') or user_info.get('pic', '') or user_info.get('headurl', '')
                        if pic and not headurl:
                            headurl = pic
                            if headurl and not headurl.startswith('http'):
                                headurl = f'https:{headurl}' if headurl.startswith('//') else f'https://{headurl}'
                        logger.info(f"[QQ音乐] 方法3(GetHomepageHeader)获取用户信息: name={musicname}, headurl={headurl[:50] if headurl else ''}")
            except Exception as e:
                logger.warning(f"[QQ音乐] 方法3(GetHomepageHeader)获取用户信息失败: {e}")
        
        # 方法4: 尝试使用 VIP 信息 API 验证凭证是否有效并获取用户信息和 VIP 状态
        is_vip = False
        try:
            result = _call_qqmusic_api_direct(
                'VipLogin.VipLoginInter',
                'vip_login_base',
                {}
            )
            logger.info(f"[QQ音乐] vip_login_base 响应: {str(result)[:800]}")
            vip_data = result.get('data', result)
            if vip_data:
                # 可能包含用户信息
                nick = vip_data.get('nick', '') or vip_data.get('name', '') or vip_data.get('nickname', '')
                if nick and musicname == f'用户{musicid[-4:]}':
                    musicname = nick
                pic = vip_data.get('headpic', '') or vip_data.get('headurl', '') or vip_data.get('pic', '')
                if pic and not headurl:
                    headurl = pic
                    if headurl and not headurl.startswith('http'):
                        headurl = f'https:{headurl}' if headurl.startswith('//') else f'https://{headurl}'
                # 检查 VIP 状态 - 尝试多个可能的字段
                # 常见字段: isvip, vipflag, vip_flag, svip_flag, is_vip, vipType
                vip_flag = vip_data.get('isvip', 0) or vip_data.get('vipflag', 0) or vip_data.get('vip_flag', 0)
                svip_flag = vip_data.get('svip_flag', 0) or vip_data.get('issvip', 0)
                vip_type = vip_data.get('vipType', 0) or vip_data.get('vip_type', 0)
                is_vip = bool(vip_flag or svip_flag or vip_type)
                logger.info(f"[QQ音乐] 方法4(vip_login_base)获取用户信息: name={musicname}, headurl={headurl[:50] if headurl else ''}, is_vip={is_vip}, vip_flag={vip_flag}, svip_flag={svip_flag}, vip_type={vip_type}")
        except Exception as e:
            logger.warning(f"[QQ音乐] 方法4(vip_login_base)获取VIP信息失败: {e}")
        
        # 更新凭证中的用户信息
        QQMUSIC_CREDENTIAL['musicname'] = musicname
        QQMUSIC_CREDENTIAL['headurl'] = headurl
        QQMUSIC_CREDENTIAL['encrypt_uin'] = encrypt_uin
        QQMUSIC_CREDENTIAL['is_vip'] = is_vip
        _save_qqmusic_credential(QQMUSIC_CREDENTIAL)
        
        logger.info(f"[QQ音乐] Cookie 登录成功: musicid={musicid}, name={musicname}, headurl={headurl[:50] if headurl else 'none'}")
        return jsonify({
            'success': True,
            'credential': QQMUSIC_CREDENTIAL,
            'message': '登录成功'
        })
    except Exception as e:
        logger.warning(f"QQ音乐 Cookie 登录失败: {e}")
        import traceback
        logger.warning(f"[QQ音乐] Cookie 登录异常详情: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': '登录失败'})

@app.route('/api/qqmusic/hotkey')
def qqmusic_hotkey():
    """获取热搜词"""
    try:
        resp = call_qqmusic_api('search', 'hotkey', {})
        if resp.get('code') == 200:
            return jsonify({'success': True, 'data': resp.get('data')})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取热搜失败'})
    except Exception as e:
        logger.warning(f"获取QQ音乐热搜失败: {e}")
        return jsonify({'success': False, 'error': '获取热搜失败'})

@app.route('/api/qqmusic/download', methods=['POST'])
def download_qqmusic():
    """下载 QQ 音乐"""
    global QQMUSIC_DOWNLOAD_TASKS
    data = request.get_json() or {}
    mid = data.get('mid', '').strip()
    title = data.get('title', '未知歌曲')
    artist = data.get('artist', '未知艺术家')
    cover_url = data.get('cover', '')
    file_type = data.get('file_type', 'MP3_128')
    target_dir = data.get('target_dir', '').strip()
    
    if not mid:
        return jsonify({'success': False, 'error': '缺少歌曲 mid'})
    
    task_id = f"qq_{int(time.time() * 1000)}_{mid[:8]}"
    # 默认使用挂载目录
    download_dir = target_dir or QQMUSIC_DOWNLOAD_DIR or get_default_download_dir()
    os.makedirs(download_dir, exist_ok=True)
    
    QQMUSIC_DOWNLOAD_TASKS[task_id] = {
        'status': 'preparing',
        'progress': 0,
        'message': '准备下载...',
        'filename': f"{artist} - {title}"
    }
    
    def do_download():
        try:
            QQMUSIC_DOWNLOAD_TASKS[task_id]['status'] = 'downloading'
            QQMUSIC_DOWNLOAD_TASKS[task_id]['progress'] = 10
            
            # 音质降级顺序：FLAC -> MP3_320 -> MP3_128
            quality_fallback = {
                'FLAC': ['FLAC', 'MP3_320', 'MP3_128'],
                'MP3_320': ['MP3_320', 'MP3_128'],
                'MP3_128': ['MP3_128'],
                'OGG_320': ['OGG_320', 'OGG_192', 'MP3_128'],
                'OGG_192': ['OGG_192', 'MP3_128'],
                'ACC_192': ['ACC_192', 'ACC_96', 'MP3_128'],
                'ACC_96': ['ACC_96', 'MP3_128'],
            }
            
            # 获取要尝试的音质列表
            qualities_to_try = quality_fallback.get(file_type, [file_type, 'MP3_128'])
            
            url = None
            actual_quality = file_type
            
            for try_quality in qualities_to_try:
                logger.info(f"[QQ音乐] 尝试获取 {title} 的 {try_quality} 下载链接...")
                resp = call_qqmusic_api('song', 'get_song_urls', {
                    'mid': mid,
                    'file_type': try_quality
                })
                
                if resp.get('code') == 200:
                    urls = resp.get('data') or {}
                    url = urls.get(mid)
                    if url:
                        actual_quality = try_quality
                        if try_quality != file_type:
                            logger.info(f"[QQ音乐] {title} 降级到 {try_quality} 成功")
                        break
                    else:
                        logger.warning(f"[QQ音乐] {title} 的 {try_quality} 无法获取URL，尝试降级...")
                else:
                    logger.warning(f"[QQ音乐] {title} 的 {try_quality} 请求失败: {resp.get('message')}")
            
            if not url:
                raise Exception('无法获取下载链接，所有音质均不可用')
            
            QQMUSIC_DOWNLOAD_TASKS[task_id]['progress'] = 20
            
            # 确定文件扩展名
            ext_map = {
                'MP3_128': '.mp3',
                'MP3_320': '.mp3',
                'FLAC': '.flac',
                'OGG_192': '.ogg',
                'OGG_320': '.ogg',
                'ACC_192': '.m4a',
                'ACC_96': '.m4a'
            }
            ext = ext_map.get(actual_quality, '.mp3')
            
            # 清理文件名
            safe_title = re.sub(r'[<>:"/\\|?*]', '', title)
            safe_artist = re.sub(r'[<>:"/\\|?*]', '', artist)
            filename = f"{safe_artist} - {safe_title}{ext}"
            filepath = os.path.join(download_dir, filename)
            
            # 下载文件
            headers = dict(COMMON_HEADERS)
            headers['Referer'] = 'https://y.qq.com/'
            
            with requests.get(url, stream=True, timeout=60, headers=headers) as r:
                r.raise_for_status()
                total = int(r.headers.get('content-length', 0))
                downloaded = 0
                
                with open(filepath, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                progress = 20 + int(downloaded / total * 70)
                                QQMUSIC_DOWNLOAD_TASKS[task_id]['progress'] = min(progress, 90)
            
            QQMUSIC_DOWNLOAD_TASKS[task_id]['progress'] = 95
            
            base_name = os.path.splitext(filename)[0]
            
            # 下载封面并嵌入
            if cover_url:
                try:
                    cover_bytes = fetch_cover_bytes(cover_url)
                    if cover_bytes:
                        embed_cover_to_file(filepath, cover_bytes)
                        # 保存封面到 download_dir/covers/ 目录
                        cover_dir = os.path.join(download_dir, 'covers')
                        os.makedirs(cover_dir, exist_ok=True)
                        cover_path = os.path.join(cover_dir, f"{base_name}.jpg")
                        with open(cover_path, 'wb') as f:
                            f.write(cover_bytes)
                        logger.info(f"封面已保存: {cover_path}")
                except Exception as e:
                    logger.warning(f"嵌入封面失败: {e}")
            
            # 下载歌词
            try:
                lyric_resp = call_qqmusic_api('lyric', 'get_lyric', {'value': mid})
                if lyric_resp.get('code') == 200:
                    lyric_data = lyric_resp.get('data', {})
                    lrc_text = lyric_data.get('lrc', '')
                    if lrc_text:
                        # 保存歌词到 download_dir/lyrics/ 目录
                        lyrics_dir = os.path.join(download_dir, 'lyrics')
                        os.makedirs(lyrics_dir, exist_ok=True)
                        lrc_path = os.path.join(lyrics_dir, f"{base_name}.lrc")
                        with open(lrc_path, 'w', encoding='utf-8') as f:
                            f.write(lrc_text)
                        logger.info(f"歌词已保存: {lrc_path}")
                        # 嵌入歌词到音频文件
                        embed_lyrics_to_file(filepath, lrc_text)
            except Exception as e:
                logger.warning(f"下载歌词失败: {e}")
            
            # 索引文件
            index_single_file(filepath)
            
            QQMUSIC_DOWNLOAD_TASKS[task_id]['status'] = 'success'
            QQMUSIC_DOWNLOAD_TASKS[task_id]['progress'] = 100
            QQMUSIC_DOWNLOAD_TASKS[task_id]['message'] = '下载完成'
            QQMUSIC_DOWNLOAD_TASKS[task_id]['filename'] = filename
            
            logger.info(f"QQ音乐下载完成: {filename}")
            
        except Exception as e:
            logger.error(f"QQ音乐下载失败: {e}")
            QQMUSIC_DOWNLOAD_TASKS[task_id]['status'] = 'error'
            QQMUSIC_DOWNLOAD_TASKS[task_id]['message'] = str(e)
    
    threading.Thread(target=do_download, daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id})

@app.route('/api/qqmusic/task/<task_id>')
def get_qqmusic_task_status(task_id):
    """获取 QQ 音乐下载任务状态"""
    task = QQMUSIC_DOWNLOAD_TASKS.get(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'})
    return jsonify({'success': True, 'data': task})

@app.route('/api/qqmusic/playlist/parse', methods=['POST'])
def parse_qqmusic_playlist():
    """解析 QQ 音乐歌单链接，返回歌曲列表（支持分页获取全部歌曲）"""
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    
    if not url:
        return jsonify({'success': False, 'error': '请提供歌单链接'})
    
    try:
        # 从URL中提取歌单ID - 支持多种格式
        logger.info(f'解析QQ音乐歌单链接: {url}')
        
        # 如果是短链接，先解析获取真实URL
        if 'fcgi-bin/u' in url or 'c.y.qq.com' in url or 'c6.y.qq.com' in url:
            try:
                logger.info(f'检测到QQ音乐短链接，尝试重定向解析: {url}')
                resp = requests.get(url, allow_redirects=True, timeout=10, headers=COMMON_HEADERS)
                real_url = resp.url
                logger.info(f'短链接重定向到: {real_url}')
                # 使用重定向后的URL继续解析
                url = real_url
            except Exception as e:
                logger.warning(f'解析QQ音乐短链接失败: {e}')
        
        id_match = (
            re.search(r'id=(\d+)', url) or 
            re.search(r'/playlist/(\d+)', url) or
            re.search(r'/(\d{8,})(?:/|$|\?)', url) or
            re.search(r'disstid[=:](\d+)', url)
        )
        
        if not id_match:
            return jsonify({'success': False, 'error': '无法从链接中提取歌单ID'})
        
        playlist_id = id_match.group(1)
        logger.info(f'提取到QQ音乐歌单ID: {playlist_id}')
        
        # 如果ID太短，可能还需要进一步解析
        if len(playlist_id) < 8:
            try:
                resp = requests.get(url, allow_redirects=True, timeout=10, headers=COMMON_HEADERS)
                real_url = resp.url
                real_id_match = re.search(r'id=(\d+)', real_url) or re.search(r'/playlist/(\d+)', real_url)
                if real_id_match:
                    playlist_id = real_id_match.group(1)
                    logger.info(f'通过重定向获取到真实ID: {playlist_id}')
            except Exception as e:
                logger.warning(f'解析QQ音乐短链接失败: {e}')
        
        # 使用内部API分页获取全部歌曲
        resp = call_qqmusic_api('playlist', 'get_playlist_detail', {'id': playlist_id})
        
        if resp.get('code') == 200:
            data = resp.get('data', {})
            songs = data.get('songlist', [])
            playlist_name = data.get('dissname', '未知歌单')
            
            # 格式化歌曲列表
            formatted_songs = _format_qqmusic_songs(songs)
            
            logger.info(f'解析QQ音乐歌单成功: {playlist_name}, 共 {len(formatted_songs)} 首歌曲')
            
            return jsonify({
                'success': True,
                'playlist_name': playlist_name,
                'playlist_id': playlist_id,
                'creator': '',
                'song_count': len(formatted_songs),
                'songs': formatted_songs
            })
        else:
            return jsonify({'success': False, 'error': resp.get('message') or 'QQ音乐API返回错误'})
            
    except Exception as e:
        logger.error(f'解析QQ音乐歌单失败: {e}')
        return jsonify({'success': False, 'error': f'解析失败: {str(e)}'})

@app.route('/api/qqmusic/playlist/user')
def get_user_playlists():
    """获取当前登录用户的歌单列表"""
    if not QQMUSIC_CREDENTIAL or not QQMUSIC_CREDENTIAL.get('musicid'):
        return jsonify({'success': False, 'error': '请先登录QQ音乐'})
    
    try:
        musicid = QQMUSIC_CREDENTIAL.get('musicid')
        
        # 调用QQ音乐API获取用户歌单
        resp = call_qqmusic_api('playlist', 'get_user_playlists', {'uin': musicid})
        
        if resp.get('code') == 200:
            playlists = resp.get('data', [])
            formatted = []
            for pl in playlists:
                formatted.append({
                    'id': pl.get('tid') or pl.get('id'),
                    'name': pl.get('diss_name') or pl.get('name', ''),
                    'cover': pl.get('diss_cover') or pl.get('cover', ''),
                    'song_count': pl.get('song_cnt') or pl.get('song_count', 0),
                    'creator': pl.get('creator', {}).get('nick', '') if isinstance(pl.get('creator'), dict) else ''
                })
            return jsonify({'success': True, 'playlists': formatted})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取歌单失败'})
            
    except Exception as e:
        logger.error(f'获取用户歌单失败: {e}')
        return jsonify({'success': False, 'error': f'获取失败: {str(e)}'})

@app.route('/api/qqmusic/playlist/detail/<playlist_id>')
def get_playlist_detail(playlist_id):
    """获取歌单详情（歌曲列表）"""
    try:
        # 调用QQ音乐API获取歌单详情
        resp = call_qqmusic_api('playlist', 'get_playlist_detail', {'id': playlist_id})
        
        if resp.get('code') == 200:
            data = resp.get('data', {})
            songs = data.get('songlist', [])
            
            # 格式化歌曲列表
            formatted_songs = _format_qqmusic_songs(songs)
            
            return jsonify({
                'success': True,
                'playlist_name': data.get('dissname', ''),
                'playlist_id': playlist_id,
                'song_count': len(formatted_songs),
                'songs': formatted_songs
            })
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取歌单详情失败'})
            
    except Exception as e:
        logger.error(f'获取歌单详情失败: {e}')
        return jsonify({'success': False, 'error': f'获取失败: {str(e)}'})


# ==================== 本地歌单管理 API (用户隔离) ====================

@app.route('/api/playlists')
def get_local_playlists():
    """获取本地歌单列表（按用户隔离）"""
    try:
        user_hash = session.get('user_hash', '')
        is_admin = session.get('is_admin', False)
        with get_db() as conn:
            # 管理员可以看到自己的歌单和旧数据（user_hash为空），普通用户只能看到自己的
            if is_admin:
                rows = conn.execute('''
                    SELECT p.id, p.name, p.cover, p.created_at, p.updated_at, p.source_url, p.source_type, p.last_synced_at,
                           (SELECT COUNT(*) FROM playlist_songs WHERE playlist_id = p.id) as song_count,
                           (SELECT COUNT(*) FROM playlist_pending_songs WHERE playlist_id = p.id) as pending_count
                    FROM playlists p
                    WHERE p.user_hash = ? OR p.user_hash = '' OR p.user_hash IS NULL
                    ORDER BY p.updated_at DESC
                ''', (user_hash,)).fetchall()
            else:
                rows = conn.execute('''
                    SELECT p.id, p.name, p.cover, p.created_at, p.updated_at, p.source_url, p.source_type, p.last_synced_at,
                           (SELECT COUNT(*) FROM playlist_songs WHERE playlist_id = p.id) as song_count,
                           (SELECT COUNT(*) FROM playlist_pending_songs WHERE playlist_id = p.id) as pending_count
                    FROM playlists p
                    WHERE p.user_hash = ?
                    ORDER BY p.updated_at DESC
                ''', (user_hash,)).fetchall()
            
            playlists = []
            for row in rows:
                song_count = row['song_count'] or 0
                pending_count = row['pending_count'] or 0
                playlists.append({
                    'id': row['id'],
                    'name': row['name'],
                    'cover': row['cover'] or '/static/images/ICON_256.PNG',
                    'song_count': song_count + pending_count,  # 总数包括待下载
                    'local_count': song_count,
                    'pending_count': pending_count,
                    'created_at': row['created_at'],
                    'updated_at': row['updated_at'],
                    'source_url': row['source_url'],
                    'source_type': row['source_type'],
                    'last_synced_at': row['last_synced_at']
                })
            
            return jsonify({'success': True, 'playlists': playlists})
    except Exception as e:
        logger.error(f'获取本地歌单失败: {e}')
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/playlists', methods=['POST'])
def create_local_playlist():
    """创建本地歌单"""
    try:
        data = request.get_json() or {}
        name = data.get('name', '').strip()
        pending_songs = data.get('pending_songs', [])  # 待下载歌曲列表
        source_url = data.get('source_url', '')  # 源歌单链接（用于同步）
        source_type = data.get('source_type', '')  # 源类型：qq/netease
        
        if not name:
            return jsonify({'success': False, 'error': '歌单名称不能为空'})
        
        now = time.time()
        user_hash = session.get('user_hash', '')
        with get_db() as conn:
            cursor = conn.execute(
                'INSERT INTO playlists (name, created_at, updated_at, user_hash, source_url, source_type, last_synced_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (name, now, now, user_hash, source_url or None, source_type or None, now if source_url else None)
            )
            playlist_id = cursor.lastrowid
            
            # 保存待下载歌曲（保持原始顺序）
            pending_count = 0
            skipped_count = 0
            error_count = 0
            logger.info(f'创建歌单 "{name}"，准备保存 {len(pending_songs)} 首歌曲')
            
            for idx, song in enumerate(pending_songs):
                try:
                    qq_mid = song.get('mid') or song.get('qq_mid')
                    netease_id = song.get('netease_id')
                    source = song.get('source', 'qq')
                    # 优先使用前端传递的 sort_order，否则使用索引
                    sort_order = song.get('sort_order', idx)
                    
                    cursor = conn.execute('''
                        INSERT OR IGNORE INTO playlist_pending_songs 
                        (playlist_id, qq_mid, netease_id, title, artist, album, cover, source, added_at, sort_order)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        playlist_id,
                        qq_mid,
                        netease_id,
                        song.get('title', '未知歌曲'),
                        song.get('artist', ''),
                        song.get('album', ''),
                        song.get('cover', ''),
                        source,
                        now,
                        sort_order  # 保存原始顺序
                    ))
                    # 只有实际插入成功才计数
                    if cursor.rowcount > 0:
                        pending_count += 1
                    else:
                        skipped_count += 1
                        if skipped_count <= 5:  # 只记录前5个跳过的
                            logger.debug(f'跳过重复歌曲: {song.get("title")} (qq_mid={qq_mid})')
                except Exception as e:
                    error_count += 1
                    logger.warning(f'保存待下载歌曲失败: {e}')
            
            conn.commit()
            logger.info(f'创建歌单 "{name}" 完成: 保存 {pending_count} 首, 跳过 {skipped_count} 首重复, {error_count} 首失败')
            
            return jsonify({
                'success': True,
                'playlist': {
                    'id': playlist_id,
                    'name': name,
                    'cover': '/static/images/ICON_256.PNG',
                    'song_count': 0,
                    'pending_count': pending_count,
                    'created_at': now,
                    'updated_at': now,
                    'source_url': source_url or None,
                    'source_type': source_type or None,
                    'last_synced_at': now if source_url else None
                }
            })
    except Exception as e:
        logger.error(f'创建歌单失败: {e}')
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/playlists/<int:playlist_id>', methods=['DELETE'])
def delete_local_playlist(playlist_id):
    """删除本地歌单"""
    try:
        user_hash = session.get('user_hash', '')
        is_admin = session.get('is_admin', False)
        with get_db() as conn:
            # 验证歌单所有权
            playlist = conn.execute('SELECT user_hash FROM playlists WHERE id = ?', (playlist_id,)).fetchone()
            if not playlist:
                return jsonify({'success': False, 'error': '歌单不存在'})
            playlist_owner = playlist['user_hash'] or ''
            # 只有歌单所有者或管理员（对于旧数据）可以删除
            if playlist_owner != user_hash and not (is_admin and playlist_owner == ''):
                return jsonify({'success': False, 'error': '无权删除此歌单'})
            
            conn.execute('DELETE FROM playlist_songs WHERE playlist_id = ?', (playlist_id,))
            conn.execute('DELETE FROM playlist_pending_songs WHERE playlist_id = ?', (playlist_id,))
            conn.execute('DELETE FROM playlists WHERE id = ?', (playlist_id,))
            conn.commit()
            return jsonify({'success': True})
    except Exception as e:
        logger.error(f'删除歌单失败: {e}')
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/playlists/<int:playlist_id>/rename', methods=['POST'])
def rename_local_playlist(playlist_id):
    """重命名歌单"""
    try:
        data = request.get_json() or {}
        name = data.get('name', '').strip()
        
        if not name:
            return jsonify({'success': False, 'error': '歌单名称不能为空'})
        
        user_hash = session.get('user_hash', '')
        is_admin = session.get('is_admin', False)
        with get_db() as conn:
            # 验证歌单所有权
            playlist = conn.execute('SELECT user_hash FROM playlists WHERE id = ?', (playlist_id,)).fetchone()
            if not playlist:
                return jsonify({'success': False, 'error': '歌单不存在'})
            playlist_owner = playlist['user_hash'] or ''
            if playlist_owner != user_hash and not (is_admin and playlist_owner == ''):
                return jsonify({'success': False, 'error': '无权修改此歌单'})
            
            conn.execute(
                'UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?',
                (name, time.time(), playlist_id)
            )
            conn.commit()
            return jsonify({'success': True})
    except Exception as e:
        logger.error(f'重命名歌单失败: {e}')
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/playlists/<int:playlist_id>/songs')
def get_playlist_songs(playlist_id):
    """获取歌单中的歌曲"""
    try:
        with get_db() as conn:
            # 获取歌单信息
            playlist = conn.execute(
                'SELECT id, name, cover FROM playlists WHERE id = ?',
                (playlist_id,)
            ).fetchone()
            
            if not playlist:
                return jsonify({'success': False, 'error': '歌单不存在'})
            
            # 获取本地歌曲列表（按原始顺序排序）
            rows = conn.execute('''
                SELECT s.id, s.path, s.filename, s.title, s.artist, s.album, s.has_cover, ps.added_at, ps.sort_order
                FROM playlist_songs ps
                JOIN songs s ON ps.song_id = s.id
                WHERE ps.playlist_id = ?
                ORDER BY ps.sort_order ASC, ps.added_at ASC
            ''', (playlist_id,)).fetchall()
            
            songs = []
            for row in rows:
                base_name = os.path.splitext(row['filename'])[0]
                cover = '/static/images/ICON_256.PNG'
                if row['has_cover']:
                    cover = f"/api/music/covers/{quote(base_name)}.jpg?filename={quote(row['filename'])}"
                
                songs.append({
                    'id': row['id'],
                    'filename': row['filename'],
                    'title': row['title'],
                    'artist': row['artist'],
                    'album': row['album'],
                    'cover': cover,
                    'added_at': row['added_at'],
                    'sort_order': row['sort_order'] or 0,
                    'is_local': True
                })
            
            # 获取待下载歌曲列表（按原始顺序排序）
            pending_rows = conn.execute('''
                SELECT id, qq_mid, netease_id, title, artist, album, cover, source, added_at, sort_order
                FROM playlist_pending_songs
                WHERE playlist_id = ?
                ORDER BY sort_order ASC, added_at ASC
            ''', (playlist_id,)).fetchall()
            
            # 获取所有本地歌曲用于匹配
            all_local_songs = conn.execute('SELECT id, title, artist, filename FROM songs').fetchall()
            
            pending_songs = []
            converted_count = 0
            now = time.time()
            
            for row in pending_rows:
                # 检查是否已经在本地存在（通过标题和艺术家匹配）
                pending_title = (row['title'] or '').lower().strip()
                pending_artist = (row['artist'] or '').lower().strip()
                
                matched_local = None
                for local in all_local_songs:
                    local_title = (local['title'] or '').lower().strip()
                    local_artist = (local['artist'] or '').lower().strip()
                    # 精确匹配标题和艺术家
                    if pending_title and local_title and pending_title == local_title:
                        if not pending_artist or not local_artist or pending_artist == local_artist:
                            matched_local = local
                            break
                    # 或者文件名包含标题
                    filename_base = os.path.splitext(local['filename'] or '')[0].lower()
                    if pending_title and pending_title in filename_base:
                        matched_local = local
                        break
                
                if matched_local:
                    # 自动转换：删除待下载记录，添加本地歌曲
                    try:
                        conn.execute('DELETE FROM playlist_pending_songs WHERE id = ?', (row['id'],))
                        conn.execute(
                            'INSERT OR IGNORE INTO playlist_songs (playlist_id, song_id, added_at, sort_order) VALUES (?, ?, ?, ?)',
                            (playlist_id, matched_local['id'], row['added_at'], row['sort_order'] or 0)
                        )
                        converted_count += 1
                    except Exception as e:
                        logger.warning(f'自动转换待下载歌曲失败: {e}')
                        # 转换失败，仍然显示为待下载
                        pending_songs.append({
                            'id': f"pending_{row['id']}",
                            'pending_id': row['id'],
                            'qq_mid': row['qq_mid'],
                            'netease_id': row['netease_id'],
                            'title': row['title'],
                            'artist': row['artist'],
                            'album': row['album'],
                            'cover': row['cover'] or '/static/images/ICON_256.PNG',
                            'source': row['source'],
                            'added_at': row['added_at'],
                            'sort_order': row['sort_order'] or 0,
                            'is_local': False,
                            'is_pending': True
                        })
                else:
                    # 本地没有，显示为待下载
                    pending_songs.append({
                        'id': f"pending_{row['id']}",
                        'pending_id': row['id'],
                        'qq_mid': row['qq_mid'],
                        'netease_id': row['netease_id'],
                        'title': row['title'],
                        'artist': row['artist'],
                        'album': row['album'],
                        'cover': row['cover'] or '/static/images/ICON_256.PNG',
                        'source': row['source'],
                        'added_at': row['added_at'],
                        'sort_order': row['sort_order'] or 0,
                        'is_local': False,
                        'is_pending': True
                    })
            
            if converted_count > 0:
                conn.execute('UPDATE playlists SET updated_at = ? WHERE id = ?', (now, playlist_id))
                conn.commit()
                logger.info(f'歌单 {playlist_id} 自动转换了 {converted_count} 首待下载歌曲')
                # 重新获取本地歌曲列表
                rows = conn.execute('''
                    SELECT s.id, s.path, s.filename, s.title, s.artist, s.album, s.has_cover, ps.added_at, ps.sort_order
                    FROM playlist_songs ps
                    JOIN songs s ON ps.song_id = s.id
                    WHERE ps.playlist_id = ?
                    ORDER BY ps.sort_order ASC, ps.added_at ASC
                ''', (playlist_id,)).fetchall()
                
                songs = []
                for r in rows:
                    base_name = os.path.splitext(r['filename'])[0]
                    cover = '/static/images/ICON_256.PNG'
                    if r['has_cover']:
                        cover = f"/api/music/covers/{quote(base_name)}.jpg?filename={quote(r['filename'])}"
                    songs.append({
                        'id': r['id'],
                        'filename': r['filename'],
                        'title': r['title'],
                        'artist': r['artist'],
                        'album': r['album'],
                        'cover': cover,
                        'added_at': r['added_at'],
                        'sort_order': r['sort_order'] or 0,
                        'is_local': True
                    })
            
            return jsonify({
                'success': True,
                'playlist': {
                    'id': playlist['id'],
                    'name': playlist['name'],
                    'cover': playlist['cover'] or '/static/images/ICON_256.PNG'
                },
                'songs': songs,
                'pending_songs': pending_songs
            })
    except Exception as e:
        logger.error(f'获取歌单歌曲失败: {e}')
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/playlists/<int:playlist_id>/songs', methods=['POST'])
def add_song_to_playlist(playlist_id):
    """添加歌曲到歌单"""
    try:
        data = request.get_json() or {}
        song_id = data.get('song_id')
        sort_order = data.get('sort_order')  # 可选的排序顺序
        
        if not song_id:
            return jsonify({'success': False, 'error': '缺少歌曲ID'})
        
        user_hash = session.get('user_hash', '')
        is_admin = session.get('is_admin', False)
        now = time.time()
        with get_db() as conn:
            # 检查歌单是否存在并验证权限
            playlist = conn.execute('SELECT id, user_hash FROM playlists WHERE id = ?', (playlist_id,)).fetchone()
            if not playlist:
                return jsonify({'success': False, 'error': '歌单不存在'})
            playlist_owner = playlist['user_hash'] or ''
            if playlist_owner != user_hash and not (is_admin and playlist_owner == ''):
                return jsonify({'success': False, 'error': '无权修改此歌单'})
            
            # 检查歌曲是否存在
            song = conn.execute('SELECT id FROM songs WHERE id = ?', (song_id,)).fetchone()
            if not song:
                return jsonify({'success': False, 'error': '歌曲不存在'})
            
            # 添加到歌单（忽略重复）
            try:
                # 如果没有指定 sort_order，则排在最后
                if sort_order is None:
                    max_order = conn.execute(
                        'SELECT COALESCE(MAX(sort_order), -1) FROM playlist_songs WHERE playlist_id = ?',
                        (playlist_id,)
                    ).fetchone()[0]
                    sort_order = max_order + 1
                
                conn.execute(
                    'INSERT OR IGNORE INTO playlist_songs (playlist_id, song_id, added_at, sort_order) VALUES (?, ?, ?, ?)',
                    (playlist_id, song_id, now, sort_order)
                )
                conn.execute('UPDATE playlists SET updated_at = ? WHERE id = ?', (now, playlist_id))
                conn.commit()
            except sqlite3.IntegrityError:
                return jsonify({'success': False, 'error': '歌曲已在歌单中'})
            
            return jsonify({'success': True})
    except Exception as e:
        logger.error(f'添加歌曲到歌单失败: {e}')
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/playlists/<int:playlist_id>/songs/<song_id>', methods=['DELETE'])
def remove_song_from_playlist(playlist_id, song_id):
    """从歌单移除歌曲"""
    try:
        user_hash = session.get('user_hash', '')
        is_admin = session.get('is_admin', False)
        with get_db() as conn:
            # 验证歌单所有权
            playlist = conn.execute('SELECT user_hash FROM playlists WHERE id = ?', (playlist_id,)).fetchone()
            if not playlist:
                return jsonify({'success': False, 'error': '歌单不存在'})
            playlist_owner = playlist['user_hash'] or ''
            if playlist_owner != user_hash and not (is_admin and playlist_owner == ''):
                return jsonify({'success': False, 'error': '无权修改此歌单'})
            
            conn.execute(
                'DELETE FROM playlist_songs WHERE playlist_id = ? AND song_id = ?',
                (playlist_id, song_id)
            )
            conn.execute('UPDATE playlists SET updated_at = ? WHERE id = ?', (time.time(), playlist_id))
            conn.commit()
            return jsonify({'success': True})
    except Exception as e:
        logger.error(f'从歌单移除歌曲失败: {e}')
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/playlists/<int:playlist_id>/pending/<int:pending_id>', methods=['DELETE'])
def remove_pending_song_from_playlist(playlist_id, pending_id):
    """从歌单移除待下载歌曲"""
    try:
        user_hash = session.get('user_hash', '')
        is_admin = session.get('is_admin', False)
        with get_db() as conn:
            # 验证歌单所有权
            playlist = conn.execute('SELECT user_hash FROM playlists WHERE id = ?', (playlist_id,)).fetchone()
            if not playlist:
                return jsonify({'success': False, 'error': '歌单不存在'})
            playlist_owner = playlist['user_hash'] or ''
            if playlist_owner != user_hash and not (is_admin and playlist_owner == ''):
                return jsonify({'success': False, 'error': '无权修改此歌单'})
            
            conn.execute(
                'DELETE FROM playlist_pending_songs WHERE playlist_id = ? AND id = ?',
                (playlist_id, pending_id)
            )
            conn.execute('UPDATE playlists SET updated_at = ? WHERE id = ?', (time.time(), playlist_id))
            conn.commit()
            return jsonify({'success': True})
    except Exception as e:
        logger.error(f'从歌单移除待下载歌曲失败: {e}')
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/playlists/<int:playlist_id>/pending/convert', methods=['POST'])
def convert_pending_to_local(playlist_id):
    """将待下载歌曲转换为本地歌曲（下载完成后调用）"""
    try:
        data = request.get_json() or {}
        pending_id = data.get('pending_id')
        song_id = data.get('song_id')  # 本地歌曲ID
        
        if not pending_id or not song_id:
            return jsonify({'success': False, 'error': '缺少参数'})
        
        now = time.time()
        with get_db() as conn:
            # 删除待下载记录
            conn.execute(
                'DELETE FROM playlist_pending_songs WHERE playlist_id = ? AND id = ?',
                (playlist_id, pending_id)
            )
            # 添加本地歌曲
            try:
                conn.execute(
                    'INSERT OR IGNORE INTO playlist_songs (playlist_id, song_id, added_at) VALUES (?, ?, ?)',
                    (playlist_id, song_id, now)
                )
            except:
                pass
            conn.execute('UPDATE playlists SET updated_at = ? WHERE id = ?', (now, playlist_id))
            conn.commit()
            return jsonify({'success': True})
    except Exception as e:
        logger.error(f'转换待下载歌曲失败: {e}')
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/playlists/<int:playlist_id>/sync', methods=['POST'])
def sync_playlist(playlist_id):
    """同步歌单（从源链接获取新歌曲）"""
    try:
        user_hash = session.get('user_hash', '')
        with get_db() as conn:
            # 获取歌单信息
            playlist = conn.execute(
                'SELECT id, name, source_url, source_type, user_hash FROM playlists WHERE id = ?',
                (playlist_id,)
            ).fetchone()
            
            if not playlist:
                return jsonify({'success': False, 'error': '歌单不存在'})
            
            # 验证权限
            if playlist['user_hash'] and playlist['user_hash'] != user_hash:
                return jsonify({'success': False, 'error': '无权操作此歌单'})
            
            source_url = playlist['source_url']
            source_type = playlist['source_type']
            
            if not source_url:
                return jsonify({'success': False, 'error': '此歌单没有关联源链接，无法同步'})
            
            # 获取当前歌单中已有的歌曲ID（包括待下载和已下载的本地歌曲）
            existing_qq_mids = set()
            existing_netease_ids = set()
            existing_local_titles = set()  # 用于匹配已下载的本地歌曲（只用标题）
            
            # 从待下载歌曲表获取
            pending_rows = conn.execute(
                'SELECT qq_mid, netease_id, title, artist FROM playlist_pending_songs WHERE playlist_id = ?',
                (playlist_id,)
            ).fetchall()
            for row in pending_rows:
                if row['qq_mid']:
                    existing_qq_mids.add(row['qq_mid'])
                if row['netease_id']:
                    existing_netease_ids.add(str(row['netease_id']))
                # 记录标题用于匹配（只用标题，不用艺术家，因为艺术家名称可能有差异）
                if row['title']:
                    # 规范化标题：转小写、去除空格和特殊字符
                    normalized_title = ''.join(c for c in row['title'].lower() if c.isalnum())
                    if normalized_title:
                        existing_local_titles.add(normalized_title)
            
            # 从已下载的本地歌曲表获取（通过 playlist_songs 关联）
            local_rows = conn.execute('''
                SELECT s.title, s.artist FROM playlist_songs ps
                JOIN songs s ON ps.song_id = s.id
                WHERE ps.playlist_id = ?
            ''', (playlist_id,)).fetchall()
            for row in local_rows:
                if row['title']:
                    # 规范化标题
                    normalized_title = ''.join(c for c in row['title'].lower() if c.isalnum())
                    if normalized_title:
                        existing_local_titles.add(normalized_title)
            
            # 从源获取歌曲列表
            new_songs = []
            
            if source_type == 'qq':
                # 解析QQ音乐歌单ID - 支持多种格式
                logger.info(f'同步歌单: 尝试解析QQ音乐链接: {source_url}')
                
                # 如果是短链接，先解析获取真实URL
                parse_url = source_url
                if 'fcgi-bin/u' in source_url or 'c.y.qq.com' in source_url or 'c6.y.qq.com' in source_url:
                    try:
                        logger.info(f'同步歌单: 检测到短链接，尝试重定向解析')
                        redirect_resp = requests.get(source_url, allow_redirects=True, timeout=10, headers=COMMON_HEADERS)
                        parse_url = redirect_resp.url
                        logger.info(f'同步歌单: 短链接重定向到: {parse_url}')
                    except Exception as e:
                        logger.warning(f'同步歌单: 解析短链接失败: {e}')
                
                id_match = (
                    re.search(r'id=(\d+)', parse_url) or 
                    re.search(r'/playlist/(\d+)', parse_url) or
                    re.search(r'disstid[=:](\d+)', parse_url) or
                    re.search(r'/(\d{8,})(?:/|$|\?)', parse_url)
                )
                if not id_match:
                    return jsonify({'success': False, 'error': f'无法解析QQ音乐歌单ID，链接: {source_url}'})
                
                playlist_tid = id_match.group(1)
                logger.info(f'同步歌单: 提取到QQ音乐歌单ID: {playlist_tid}')
                
                # 使用正确的参数名 'id'
                resp = call_qqmusic_api('playlist', 'get_playlist_detail', {'id': playlist_tid})
                
                if resp.get('code') != 200:
                    return jsonify({'success': False, 'error': resp.get('message') or '获取歌单失败'})
                
                # 歌曲列表在 songlist 字段中
                songs = resp.get('data', {}).get('songlist', [])
                for song in songs:
                    # 支持多种字段名
                    mid = song.get('mid') or song.get('songmid', '')
                    if mid and mid not in existing_qq_mids:
                        # 处理歌手
                        singers = song.get('singer', [])
                        artist = ', '.join([s.get('name', '') for s in singers if s.get('name')]) if singers else ''
                        
                        # 处理专辑
                        album_info = song.get('album', {})
                        if isinstance(album_info, dict):
                            album_name = album_info.get('name', '')
                            album_mid = album_info.get('mid', '')
                        else:
                            album_name = song.get('albumname', '')
                            album_mid = song.get('albummid', '')
                        
                        cover = f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else ''
                        
                        title = song.get('title') or song.get('name') or song.get('songname', '未知歌曲')
                        
                        # 检查是否已存在于本地歌曲中（通过规范化标题匹配）
                        normalized_title = ''.join(c for c in title.lower() if c.isalnum())
                        if normalized_title and normalized_title in existing_local_titles:
                            logger.debug(f'同步歌单: 跳过已存在的歌曲: {title} - {artist}')
                            continue
                        
                        new_songs.append({
                            'qq_mid': mid,
                            'title': title,
                            'artist': artist,
                            'album': album_name,
                            'cover': cover,
                            'source': 'qq'
                        })
            
            elif source_type == 'netease':
                # 解析网易云歌单ID
                match = re.search(r'id[=:](\d+)', source_url)
                if not match:
                    match = re.search(r'/playlist[/?](\d+)', source_url)
                if not match:
                    return jsonify({'success': False, 'error': '无法解析网易云歌单ID'})
                
                playlist_nid = match.group(1)
                
                # 调用网易云API
                if not NETEASE_API_URL:
                    return jsonify({'success': False, 'error': '网易云API未配置'})
                
                netease_resp = requests.get(f'{NETEASE_API_URL}/playlist/track/all', params={'id': playlist_nid}, timeout=30)
                data = netease_resp.json()
                
                if data.get('code') != 200:
                    return jsonify({'success': False, 'error': '获取网易云歌单失败'})
                
                songs = data.get('songs', [])
                for song in songs:
                    nid = str(song.get('id', ''))
                    if nid and nid not in existing_netease_ids:
                        artists = song.get('ar', [])
                        album = song.get('al', {})
                        title = song.get('name', '未知歌曲')
                        artist = ', '.join(a.get('name', '') for a in artists) if artists else ''
                        
                        # 检查是否已存在于本地歌曲中（通过规范化标题匹配）
                        normalized_title = ''.join(c for c in title.lower() if c.isalnum())
                        if normalized_title and normalized_title in existing_local_titles:
                            logger.debug(f'同步歌单: 跳过已存在的歌曲: {title} - {artist}')
                            continue
                        
                        new_songs.append({
                            'netease_id': nid,
                            'title': title,
                            'artist': artist,
                            'album': album.get('name', '') if album else '',
                            'cover': album.get('picUrl', '') if album else '',
                            'source': 'netease'
                        })
            
            else:
                return jsonify({'success': False, 'error': f'不支持的源类型: {source_type}'})
            
            # 添加新歌曲到待下载列表
            now = time.time()
            added_count = 0
            skipped_by_mid = 0
            skipped_by_title = 0
            
            # 记录已有歌曲数量
            logger.info(f'同步歌单 {playlist_id}: 已有 {len(existing_qq_mids)} 个qq_mid, {len(existing_local_titles)} 个标题')
            logger.info(f'同步歌单 {playlist_id}: 从源获取 {len(songs) if source_type == "qq" else len(data.get("songs", []))} 首歌曲, 待添加 {len(new_songs)} 首')
            
            max_sort = conn.execute(
                'SELECT MAX(sort_order) as max_sort FROM playlist_pending_songs WHERE playlist_id = ?',
                (playlist_id,)
            ).fetchone()
            sort_order = (max_sort['max_sort'] or 0) + 1 if max_sort else 0
            
            for song in new_songs:
                try:
                    cursor = conn.execute('''
                        INSERT OR IGNORE INTO playlist_pending_songs 
                        (playlist_id, qq_mid, netease_id, title, artist, album, cover, source, added_at, sort_order)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        playlist_id,
                        song.get('qq_mid'),
                        song.get('netease_id'),
                        song.get('title', '未知歌曲'),
                        song.get('artist', ''),
                        song.get('album', ''),
                        song.get('cover', ''),
                        song.get('source', 'qq'),
                        now,
                        sort_order
                    ))
                    sort_order += 1
                    # 只有实际插入成功才计数
                    if cursor.rowcount > 0:
                        added_count += 1
                except Exception as e:
                    logger.warning(f'同步歌曲失败: {e}')
            
            # 更新歌单同步时间
            conn.execute(
                'UPDATE playlists SET last_synced_at = ?, updated_at = ? WHERE id = ?',
                (now, now, playlist_id)
            )
            conn.commit()
            
            logger.info(f'歌单 {playlist_id} 同步完成，新增 {added_count} 首歌曲')
            
            return jsonify({
                'success': True,
                'added_count': added_count,
                'message': f'同步完成，新增 {added_count} 首歌曲' if added_count > 0 else '歌单已是最新，没有新歌曲'
            })
    except Exception as e:
        logger.error(f'同步歌单失败: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


# --- 管理员 API ---
def require_admin(f):
    """管理员权限装饰器"""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({'success': False, 'error': '需要管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated

@app.route('/api/admin/users', methods=['GET'])
@require_admin
def admin_list_users():
    """获取所有用户列表"""
    try:
        users = []
        for filename in os.listdir(USER_DATA_DIR):
            if filename.endswith('.json'):
                filepath = os.path.join(USER_DATA_DIR, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        user_data = json.load(f)
                        # 从播放历史统计播放次数
                        play_count = len(user_data.get('play_history', []))
                        last_active = None
                        if user_data.get('play_history'):
                            last_active = max(h.get('played_at', 0) for h in user_data['play_history'])
                        users.append({
                            'id': filename.replace('.json', ''),
                            'username': user_data.get('username', ''),
                            'is_admin': user_data.get('is_admin', False),
                            'play_count': play_count,
                            'last_active': last_active,
                            'created_at': user_data.get('created_at')
                        })
                except Exception as e:
                    logger.warning(f'读取用户文件失败 {filename}: {e}')
        return jsonify({'success': True, 'users': users})
    except Exception as e:
        logger.error(f'获取用户列表失败: {e}')
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/admin/users/<user_id>', methods=['GET'])
@require_admin
def admin_get_user(user_id):
    """获取单个用户详情"""
    try:
        filepath = os.path.join(USER_DATA_DIR, f'{user_id}.json')
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'error': '用户不存在'})
        with open(filepath, 'r', encoding='utf-8') as f:
            user_data = json.load(f)
        return jsonify({
            'success': True,
            'user': {
                'id': user_id,
                'username': user_data.get('username', ''),
                'is_admin': user_data.get('is_admin', False),
                'created_at': user_data.get('created_at'),
                'favorites_count': len(user_data.get('favorites', [])),
                'playlists_count': len(user_data.get('playlists', []))
            }
        })
    except Exception as e:
        logger.error(f'获取用户详情失败: {e}')
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/admin/users/<user_id>', methods=['DELETE'])
@require_admin
def admin_delete_user(user_id):
    """删除用户"""
    try:
        filepath = os.path.join(USER_DATA_DIR, f'{user_id}.json')
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'error': '用户不存在'})
        # 检查是否是管理员
        with open(filepath, 'r', encoding='utf-8') as f:
            user_data = json.load(f)
        if user_data.get('is_admin'):
            return jsonify({'success': False, 'error': '不能删除管理员账户'})
        os.remove(filepath)
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f'删除用户失败: {e}')
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/admin/stats/overview', methods=['GET'])
@require_admin
def admin_stats_overview():
    """获取统计概览"""
    try:
        total_users = 0
        total_plays = 0
        plays_today = 0
        active_users_today = 0
        total_duration = 0
        today_start = time.time() - (time.time() % 86400)
        
        for filename in os.listdir(USER_DATA_DIR):
            if filename.endswith('.json'):
                total_users += 1
                filepath = os.path.join(USER_DATA_DIR, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        user_data = json.load(f)
                        history = user_data.get('play_history', [])
                        total_plays += len(history)
                        user_plays_today = sum(1 for h in history if h.get('played_at', 0) >= today_start)
                        plays_today += user_plays_today
                        if user_plays_today > 0:
                            active_users_today += 1
                        total_duration += sum(h.get('duration', 0) for h in history)
                except:
                    pass
        
        # 获取歌曲总数
        total_songs = 0
        try:
            with get_db() as conn:
                row = conn.execute('SELECT COUNT(*) as cnt FROM songs').fetchone()
                total_songs = row['cnt'] if row else 0
        except:
            pass
        
        return jsonify({
            'success': True,
            'stats': {
                'total_users': total_users,
                'active_users_today': active_users_today,
                'total_plays': total_plays,
                'plays_today': plays_today,
                'total_songs': total_songs,
                'total_duration': total_duration
            }
        })
    except Exception as e:
        logger.error(f'获取统计概览失败: {e}')
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/admin/stats/user/<user_id>', methods=['GET'])
@require_admin
def admin_user_stats(user_id):
    """获取指定用户的统计信息"""
    try:
        filepath = os.path.join(USER_DATA_DIR, f'{user_id}.json')
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'error': '用户不存在'})
        with open(filepath, 'r', encoding='utf-8') as f:
            user_data = json.load(f)
        history = user_data.get('play_history', [])
        unique_songs = len(set(h.get('song_id') for h in history if h.get('song_id')))
        total_duration = sum(h.get('duration', 0) for h in history)
        return jsonify({
            'success': True,
            'stats': {
                'total_plays': len(history),
                'unique_songs': unique_songs,
                'total_duration': total_duration
            }
        })
    except Exception as e:
        logger.error(f'获取用户统计失败: {e}')
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/admin/stats/user/<user_id>/history', methods=['GET'])
@require_admin
def admin_user_history(user_id):
    """获取指定用户的播放历史"""
    try:
        filepath = os.path.join(USER_DATA_DIR, f'{user_id}.json')
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'error': '用户不存在'})
        
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)
        
        with open(filepath, 'r', encoding='utf-8') as f:
            user_data = json.load(f)
        history = user_data.get('play_history', [])
        # 按时间倒序
        history.sort(key=lambda x: x.get('played_at', 0), reverse=True)
        
        # 分页
        paginated = history[offset:offset + limit]
        
        # 补充歌曲信息
        result = []
        with get_db() as conn:
            for h in paginated:
                song_id = h.get('song_id')
                if song_id:
                    row = conn.execute('SELECT title, artist, filename FROM songs WHERE id = ?', (song_id,)).fetchone()
                    if row:
                        result.append({
                            'song_id': song_id,
                            'title': row['title'],
                            'artist': row['artist'],
                            'played_at': h.get('played_at'),
                            'duration': h.get('duration', 0),
                            'cover': f'/api/music/covers/{quote(os.path.splitext(row["filename"])[0])}.jpg?filename={quote(row["filename"])}'
                        })
                    else:
                        result.append({
                            'song_id': song_id,
                            'title': h.get('title', '未知歌曲'),
                            'artist': h.get('artist', '未知艺术家'),
                            'played_at': h.get('played_at'),
                            'duration': h.get('duration', 0)
                        })
        
        return jsonify({
            'success': True,
            'history': result,
            'total': len(history)
        })
    except Exception as e:
        logger.error(f'获取用户播放历史失败: {e}')
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/admin/stats/top-songs', methods=['GET'])
@require_admin
def admin_top_songs():
    """获取热门歌曲排行"""
    try:
        limit = request.args.get('limit', 20, type=int)
        period = request.args.get('period', 'all')  # day, week, month, all
        
        # 计算时间范围
        now = time.time()
        if period == 'day':
            start_time = now - 86400
        elif period == 'week':
            start_time = now - 86400 * 7
        elif period == 'month':
            start_time = now - 86400 * 30
        else:
            start_time = 0
        
        # 统计所有用户的播放记录
        song_counts = {}
        for filename in os.listdir(USER_DATA_DIR):
            if filename.endswith('.json'):
                filepath = os.path.join(USER_DATA_DIR, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        user_data = json.load(f)
                        for h in user_data.get('play_history', []):
                            if h.get('played_at', 0) >= start_time:
                                song_id = h.get('song_id')
                                if song_id:
                                    song_counts[song_id] = song_counts.get(song_id, 0) + 1
                except:
                    pass
        
        # 排序并取前N
        sorted_songs = sorted(song_counts.items(), key=lambda x: x[1], reverse=True)[:limit]
        
        # 补充歌曲信息
        result = []
        with get_db() as conn:
            for song_id, count in sorted_songs:
                row = conn.execute('SELECT title, artist, filename FROM songs WHERE id = ?', (song_id,)).fetchone()
                if row:
                    base_name = os.path.splitext(row['filename'])[0]
                    result.append({
                        'song_id': song_id,
                        'title': row['title'],
                        'artist': row['artist'],
                        'play_count': count,
                        'cover': f'/api/music/covers/{quote(base_name)}.jpg?filename={quote(row["filename"])}'
                    })
        
        return jsonify({'success': True, 'songs': result})
    except Exception as e:
        logger.error(f'获取热门歌曲失败: {e}')
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/admin/stats/active-users', methods=['GET'])
@require_admin
def admin_active_users():
    """获取活跃用户排行"""
    try:
        limit = request.args.get('limit', 20, type=int)
        period = request.args.get('period', 'all')
        
        now = time.time()
        if period == 'day':
            start_time = now - 86400
        elif period == 'week':
            start_time = now - 86400 * 7
        elif period == 'month':
            start_time = now - 86400 * 30
        else:
            start_time = 0
        
        user_stats = []
        for filename in os.listdir(USER_DATA_DIR):
            if filename.endswith('.json'):
                filepath = os.path.join(USER_DATA_DIR, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        user_data = json.load(f)
                        history = user_data.get('play_history', [])
                        play_count = sum(1 for h in history if h.get('played_at', 0) >= start_time)
                        if play_count > 0:
                            user_stats.append({
                                'user_id': filename.replace('.json', ''),
                                'username': user_data.get('username', ''),
                                'is_admin': user_data.get('is_admin', False),
                                'play_count': play_count
                            })
                except:
                    pass
        
        # 排序
        user_stats.sort(key=lambda x: x['play_count'], reverse=True)
        
        return jsonify({'success': True, 'users': user_stats[:limit]})
    except Exception as e:
        logger.error(f'获取活跃用户失败: {e}')
        return jsonify({'success': False, 'error': str(e)})

# 记录播放历史的辅助函数
def record_play_history(song_id, title=None, artist=None, duration=0):
    """记录用户播放历史"""
    user_hash = session.get('user_hash')
    if not user_hash:
        return
    try:
        user_data = load_user_data(user_hash)
        if not user_data:
            return
        if 'play_history' not in user_data:
            user_data['play_history'] = []
        user_data['play_history'].append({
            'song_id': song_id,
            'title': title,
            'artist': artist,
            'duration': duration,
            'played_at': time.time()
        })
        # 限制历史记录数量
        if len(user_data['play_history']) > 10000:
            user_data['play_history'] = user_data['play_history'][-10000:]
        save_user_data(user_hash, user_data)
    except Exception as e:
        logger.warning(f'记录播放历史失败: {e}')

@app.route('/api/play/record', methods=['POST'])
def api_record_play():
    """记录播放历史 API"""
    try:
        data = request.get_json() or {}
        song_id = data.get('song_id')
        if not song_id:
            return jsonify({'success': False, 'error': '缺少 song_id'})
        record_play_history(
            song_id=song_id,
            title=data.get('title'),
            artist=data.get('artist'),
            duration=data.get('duration', 0)
        )
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


if __name__ == '__main__':
    import traceback
    logger.info(f"服务启动，端口: {args.port} ...")
    logger.info(f"Python 版本: {sys.version}")
    logger.info(f"工作目录: {os.getcwd()}")
    logger.info(f"BASE_DIR: {BASE_DIR}")
    try:
        logger.info("正在初始化数据库...")
        init_db()
        
        # 加载 QQ 音乐凭证并启动定时刷新
        logger.info("正在加载 QQ 音乐凭证...")
        _load_qqmusic_credential()
        _start_qqmusic_credential_refresh_task()
        
        logger.info("数据库初始化完成，正在启动 Flask 服务...")
        app.run(host='0.0.0.0', port=args.port, threaded=True, use_reloader=False)
    except Exception as e:
        error_detail = traceback.format_exc()
        logger.error(f"服务启动失败: {e}\n{error_detail}")
        # 同时写入崩溃日志
        try:
            crash_log = os.path.join(os.path.dirname(args.log_path or '/tmp'), 'crash.log')
            with open(crash_log, 'w', encoding='utf-8') as f:
                f.write(f"启动失败时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"错误: {e}\n")
                f.write(f"详细信息:\n{error_detail}\n")
            logger.info(f"崩溃日志已写入: {crash_log}")
        except:
            pass
        raise
