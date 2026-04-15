#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sqlite3
from config import DB_PATH, AUDIO_EXTS, logger


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    def _init_db_core():
        with get_db() as conn:
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

            # Migrations
            try:
                conn.execute("ALTER TABLE playlists ADD COLUMN user_hash TEXT DEFAULT ''")
            except: pass
            try:
                conn.execute("ALTER TABLE playlists ADD COLUMN source_url TEXT")
            except: pass
            try:
                conn.execute("ALTER TABLE playlists ADD COLUMN source_type TEXT")
            except: pass
            try:
                conn.execute("ALTER TABLE playlists ADD COLUMN last_synced_at REAL")
            except: pass

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

            try:
                conn.execute('ALTER TABLE playlist_songs ADD COLUMN sort_order INTEGER DEFAULT 0')
            except: pass
            try:
                conn.execute('ALTER TABLE playlist_pending_songs ADD COLUMN sort_order INTEGER DEFAULT 0')
            except: pass

            # Clean non-audio rows
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
