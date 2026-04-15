#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared configuration, constants, and global mutable state.
All modules import from here to avoid circular dependencies.
"""

import os
import sys
import time
import logging
import argparse
import locale
import traceback
import hashlib
from datetime import timedelta

# --- Global exception handler ---
def global_exception_handler(exc_type, exc_value, exc_tb):
    error_msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(f"[FATAL ERROR] 未捕获的异常:\n{error_msg}", file=sys.stderr)
    try:
        with open('/tmp/xiaosongshu_crash.log', 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*50}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n{error_msg}\n")
    except:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = global_exception_handler

# --- Base directory ---
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(BASE_DIR, 'lib'))

# --- Paths ---
TEMPLATE_DIR = os.path.abspath(os.path.join(BASE_DIR, '../www/templates'))
STATIC_DIR = os.path.abspath(os.path.join(BASE_DIR, '../www/static'))

# --- Encoding ---
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

# --- CLI arguments ---
parser = argparse.ArgumentParser(description='小松鼠 Music Server')
parser.add_argument('--music-library-path', type=str,
                    default=os.environ.get('MUSIC_LIBRARY_PATH'), help='Path to music library')
parser.add_argument('--log-path', type=str,
                    default=os.environ.get('LOG_PATH'), help='Path to log file')
parser.add_argument('--port', type=int,
                    default=int(os.environ.get('PORT', 28999)), help='Server port')
parser.add_argument('--password', type=str,
                    default=os.environ.get('APP_AUTH_PASSWORD') or os.environ.get('APP_PASSWORD'),
                    help='Optional password for web access; leave empty to disable auth')
args = parser.parse_args()

# --- Path initialization ---
MUSIC_LIBRARY_PATH = args.music_library_path or os.getcwd()
os.makedirs(MUSIC_LIBRARY_PATH, exist_ok=True)
os.makedirs(os.path.join(MUSIC_LIBRARY_PATH, 'lyrics'), exist_ok=True)
os.makedirs(os.path.join(MUSIC_LIBRARY_PATH, 'covers'), exist_ok=True)

log_file = args.log_path or os.path.join(os.getcwd(), 'app.log')
os.makedirs(os.path.dirname(log_file), exist_ok=True)
DB_PATH = os.path.join(MUSIC_LIBRARY_PATH, 'data.db')

# --- Logging ---
logger = logging.getLogger('xiaosongshu')
logger.setLevel(logging.INFO)
logger.handlers.clear()
file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
logger.addHandler(file_handler)
logger.addHandler(console_handler)


class AccessLogFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return not ('/api/system/status' in msg and '" 200 ' in msg)


logging.getLogger('werkzeug').addFilter(AccessLogFilter())
logger.info(f"Music Library Path: {MUSIC_LIBRARY_PATH}")

# --- Audio ---
AUDIO_EXTS = ('.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a')

# --- Scan state ---
SCAN_STATUS = {
    'scanning': False,
    'total': 0,
    'processed': 0,
    'current_file': ''
}

LIBRARY_VERSION = time.time()

# --- HTTP helpers ---
COMMON_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Authorization': '2FMusic'
}

INVALID_METADATA_VALUES = {
    'kuwo', 'kugou', 'qqmusic', 'netease', 'xiami', 'unknown', '未知', '未知艺术家'
}

# --- Auth ---
APP_AUTH_PASSWORD = args.password

# --- Multi-user ---
USER_DATA_DIR = os.path.join(MUSIC_LIBRARY_PATH, 'user_data')
os.makedirs(USER_DATA_DIR, exist_ok=True)

# --- NetEase state ---
NETEASE_API_BASE_DEFAULT = os.environ.get('NETEASE_API_BASE', 'http://localhost:28998')
NETEASE_API_BASE = NETEASE_API_BASE_DEFAULT
NETEASE_DOWNLOAD_DIR = os.environ.get('NETEASE_DOWNLOAD_PATH', MUSIC_LIBRARY_PATH)
NETEASE_COOKIE = None
NETEASE_MAX_CONCURRENT = 20
NETEASE_QUALITY_DEFAULT = 'exhigh'
LYRICS_DIR = os.path.join(MUSIC_LIBRARY_PATH, 'lyrics')
os.makedirs(LYRICS_DIR, exist_ok=True)

DOWNLOAD_TASKS = {}

INSTALL_STATUS = {
    'installing': False,
    'progress': '',
    'success': None,
    'error': None
}

# --- QQ Music state ---
QQMUSIC_DOWNLOAD_DIR = None
QQMUSIC_DOWNLOAD_TASKS = {}
QQMUSIC_GUID = None
QQMUSIC_QIMEI = None
QQMUSIC_DEVICE = None
QQMUSIC_CREDENTIAL = None
QQMUSIC_QR_CACHE = {}

QIMEI_PUBLIC_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC1pB7vZZ9ig3JrjJCP/S3qX0g7"
    "AA9WDn0SMBmSt6fWNdvs5TxWGKYE24xWtGBSbCfwjk0bBR0FZZ7Cgn5BPBI5A6ep"
    "2cPB+VrcZpY/bgHBJuJFe3VLtVCPqfGF3naFOATaq1DIR2W0hZGel08sOBMgb/gpp"
    "XDE3GRDSxgUziiQZ+QIDAQAB"
)
QIMEI_SECRET = "ZdJqM15EeO2zWc08"
QIMEI_APP_KEY = "0AND0HD6FE4HY80F"

# --- Filesystem watcher ---
global_observer = None


def generate_song_id(path):
    return hashlib.md5(path.encode('utf-8')).hexdigest()
