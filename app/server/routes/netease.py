#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Blueprint, request, jsonify, session, redirect
import os
import time
import re
import threading
import shutil
import subprocess
import requests
from urllib.parse import quote, unquote, urlparse, parse_qs
from mutagen.easyid3 import EasyID3
from mutagen import File
from mutagen.flac import FLAC

import config
from config import (
    logger, COMMON_HEADERS, MUSIC_LIBRARY_PATH,
    NETEASE_API_BASE_DEFAULT, NETEASE_QUALITY_DEFAULT,
    DOWNLOAD_TASKS, INSTALL_STATUS
)
from models.db import get_db
from services.metadata import (
    fetch_cover_bytes, embed_cover_to_file, save_cover_file,
    embed_lyrics_to_file, get_metadata, get_default_download_dir
)
from services.scanner import index_single_file
from services.download import sanitize_filename
from services.user import load_user_data, save_user_data

netease_bp = Blueprint('netease_bp', __name__, url_prefix='')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_cookie_string(cookie_str: str):
    """将 Set-Cookie 字符串解析为 requests 兼容的字典。"""
    if not cookie_str:
        return {}
    cookies = {}
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
    skip_keys = ('path', 'expires', 'max-age', 'domain', 'samesite', 'secure', 'httponly')

    for part in raw.replace('\n', ';').split(';'):
        part = part.strip()
        if not part:
            continue
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        if k.strip().lower() in skip_keys:
            continue
        parts.append(part)

    return '; '.join(parts)


def load_netease_cookie():
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM system_settings WHERE key='netease_cookie'").fetchone()
            if row and row['value']:
                config.NETEASE_COOKIE = normalize_cookie_string(row['value'])
    except Exception as e:
        logger.warning(f"读取网易云 cookie 失败: {e}")


def save_netease_cookie(cookie_str: str):
    config.NETEASE_COOKIE = normalize_cookie_string(cookie_str or '')
    try:
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                         ('netease_cookie', config.NETEASE_COOKIE))
            conn.commit()
    except Exception as e:
        logger.warning(f"保存网易云 cookie 失败: {e}")


def load_netease_config():
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM system_settings WHERE key='netease_download_dir'").fetchone()
            if row and row['value']:
                config.NETEASE_DOWNLOAD_DIR = row['value']
            else:
                config.NETEASE_DOWNLOAD_DIR = get_default_download_dir()

            row = conn.execute("SELECT value FROM system_settings WHERE key='netease_api_base'").fetchone()
            if row and row['value']:
                config.NETEASE_API_BASE = row['value']
    except Exception as e:
        logger.warning(f"读取网易云配置失败: {e}")


def save_netease_config(download_dir: str = None, api_base: str = None):
    if download_dir:
        config.NETEASE_DOWNLOAD_DIR = download_dir
    if api_base:
        config.NETEASE_API_BASE = api_base.rstrip('/') or NETEASE_API_BASE_DEFAULT
    try:
        with get_db() as conn:
            if download_dir:
                conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                             ('netease_download_dir', config.NETEASE_DOWNLOAD_DIR))
            if api_base:
                conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                             ('netease_api_base', config.NETEASE_API_BASE))
            conn.commit()
    except Exception as e:
        logger.warning(f"保存网易云配置失败: {e}")


def call_netease_api(path: str, params: dict, method: str = 'GET', need_cookie: bool = True):
    """调用本地网易云 API，统一处理错误。"""
    base = (config.NETEASE_API_BASE or NETEASE_API_BASE_DEFAULT).rstrip('/')
    url = f"{base}{path}"
    headers = dict(COMMON_HEADERS)
    params = dict(params or {})
    cookies = {}
    if need_cookie and config.NETEASE_COOKIE:
        headers['Cookie'] = config.NETEASE_COOKIE
        params.setdefault('cookie', config.NETEASE_COOKIE)
        cookies = parse_cookie_string(config.NETEASE_COOKIE)
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
        if v == 'none':
            return 'standard'
        if v.isdigit():
            br = int(v)
            if br >= 999000:
                return 'lossless'
            if br >= 320000:
                return 'exhigh'
            if br >= 192000:
                return 'higher'
            return 'standard'
        return v

    max_br = privilege.get('maxBrLevel') or privilege.get('maxbr') or privilege.get('maxLevel')
    max_level = _norm(max_br)
    user_level = _norm(privilege.get('dlLevel') or privilege.get('plLevel') or max_level)
    return (user_level or 'standard', max_level or user_level or 'standard')


def _extract_song_size(track: dict):
    """根据期望音质优先取对应大小（字节），找不到再按从低到高回退。"""
    if not track:
        return None
    level = 'exhigh'
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
        is_vip = (fee == 1) or (privilege_fee == 1)
        user_level, max_level = _extract_song_level(privilege)
        artists = ' / '.join([a.get('name') for a in item.get('ar', []) if a.get('name')]) or '未知艺术家'
        album_info = item.get('al') or {}
        size_bytes = _extract_song_size(item)
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

    if text.isdigit():
        return {'type': prefer or 'song', 'id': text}

    candidate = text
    if candidate.startswith(('music.163.com', 'y.music.163.com', '163cn.tv')):
        candidate = f"https://{candidate}"
    if re.match(r'^https?://', candidate, re.I):
        def _follow(url):
            try:
                resp = requests.get(url, allow_redirects=True, timeout=8, headers=COMMON_HEADERS)
                return resp.url or url
            except Exception as e:
                logger.warning(f"网易云链接解析失败: {e}")
                return None

        followed = _follow(candidate)
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
                route_hint = 'playlist'
                break
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
        ids_str = ','.join(map(str, track_ids[:300]))
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


def _normalize_cover_url(url: str):
    if not url:
        return None
    u = url.replace('http://', 'https://')
    if '//' not in u:
        return None
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
    target_dir = payload.get('target_dir') or config.NETEASE_DOWNLOAD_DIR
    target_dir = os.path.abspath(target_dir)

    target_dir = os.path.abspath(target_dir)

    DOWNLOAD_TASKS[task_id]['status'] = 'preparing'

    try:
        os.makedirs(target_dir, exist_ok=True)
        need_detail_for_level = not payload.get('level')
        need_detail_for_cover = cover_bytes is None
        if not title or need_detail_for_level or need_detail_for_cover:
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

        DOWNLOAD_TASKS[task_id]['title'] = title
        DOWNLOAD_TASKS[task_id]['artist'] = artist

        api_resp = call_netease_api('/song/url/v1', {'id': song_id, 'level': level},
                                    need_cookie=bool(config.NETEASE_COOKIE))
        data_list = api_resp.get('data') if isinstance(api_resp, dict) else None
        track_info = None
        if isinstance(data_list, list) and data_list:
            track_info = data_list[0]
        elif isinstance(data_list, dict):
            track_info = data_list

        if not track_info or (not track_info.get('url') and not track_info.get('proxyUrl')):
            if level != 'standard':
                try:
                    api_resp_std = call_netease_api('/song/url/v1', {'id': song_id, 'level': 'standard'},
                                                   need_cookie=bool(config.NETEASE_COOKIE))
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
                try:
                    os.remove(tmp_path)
                except:
                    pass

        base_name_for_cover = os.path.splitext(os.path.basename(target_path))[0]
        if cover_bytes:
            embed_cover_to_file(target_path, cover_bytes)
            save_cover_file(cover_bytes, base_name_for_cover, target_dir)
        lrc_text, yrc_text = fetch_netease_lyrics(song_id)
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
        def clean_task():
            time.sleep(600)
            DOWNLOAD_TASKS.pop(task_id, None)
        threading.Thread(target=clean_task, daemon=True).start()


# ---------------------------------------------------------------------------
# Pre-load config
# ---------------------------------------------------------------------------
load_netease_config()
load_netease_cookie()


# ---------------------------------------------------------------------------
# Routes – Favorites
# ---------------------------------------------------------------------------

@netease_bp.route('/api/favorites', methods=['GET'])
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


@netease_bp.route('/api/favorites/<song_id>', methods=['POST'])
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


@netease_bp.route('/api/favorites/<song_id>', methods=['DELETE'])
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


# ---------------------------------------------------------------------------
# Routes – NetEase
# ---------------------------------------------------------------------------

@netease_bp.route('/api/netease/search')
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
                'size': _extract_song_size(item),
                'is_vip': is_vip
            })
        return jsonify({'success': True, 'data': songs})
    except Exception as e:
        logger.warning(f"网易云搜索失败: {e}")
        return jsonify({'success': False, 'error': '搜索失败，请检查网易云 API 服务'})


@netease_bp.route('/api/netease/recommend')
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


@netease_bp.route('/api/netease/login/status')
def netease_login_status():
    """检测当前 cookie 是否已登录。"""
    try:
        if not config.NETEASE_COOKIE:
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


@netease_bp.route('/api/netease/logout', methods=['POST'])
def netease_logout():
    """退出登录并清空本地保存的网易云 cookie。"""
    try:
        if config.NETEASE_COOKIE:
            try:
                call_netease_api('/logout', {'timestamp': int(time.time() * 1000)}, need_cookie=True)
            except Exception as e:
                logger.info(f"网易云 API 注销调用失败，继续清理本地 cookie: {e}")
        save_netease_cookie('')
        return jsonify({'success': True})
    except Exception as e:
        logger.warning(f"网易云退出登录失败: {e}")
        return jsonify({'success': False, 'error': '退出失败'})


@netease_bp.route('/api/netease/login/qrcode')
def netease_login_qrcode():
    """生成扫码登录二维码。"""
    try:
        key_resp = call_netease_api('/login/qr/key', {'timestamp': int(time.time() * 1000)}, need_cookie=False)
        unikey = key_resp.get('data', {}).get('unikey')
        if not unikey:
            return jsonify({'success': False, 'error': '获取登录 key 失败'})
        qr_resp = call_netease_api('/login/qr/create',
                                   {'key': unikey, 'qrimg': 1, 'timestamp': int(time.time() * 1000)},
                                   need_cookie=False)
        qrimg = qr_resp.get('data', {}).get('qrimg')
        if not qrimg:
            return jsonify({'success': False, 'error': '获取二维码失败'})
        return jsonify({'success': True, 'unikey': unikey, 'qrimg': qrimg})
    except Exception as e:
        logger.warning(f"生成网易云二维码失败: {e}")
        return jsonify({'success': False, 'error': '二维码生成失败'})


@netease_bp.route('/api/netease/login/check')
def netease_login_check():
    """轮询扫码状态，成功后保存 cookie。"""
    key = request.args.get('key')
    if not key:
        return jsonify({'success': False, 'error': '缺少 key'})
    try:
        resp = call_netease_api('/login/qr/check', {'key': key, 'timestamp': int(time.time() * 1000)},
                                need_cookie=False)
        code = resp.get('code')
        message = resp.get('message')
        cookie_str = resp.get('cookie')
        if not cookie_str and isinstance(resp.get('cookies'), list):
            cookie_str = '; '.join(resp.get('cookies'))

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


@netease_bp.route('/api/netease/download_page')
def netease_download_page():
    """重定向到网易云音乐客户端下载页面。"""
    return redirect("https://music.163.com/client")


@netease_bp.route('/api/netease/config', methods=['GET', 'POST'])
def netease_config_route():
    """获取或更新网易云下载配置。"""
    try:
        if request.method == 'GET':
            return jsonify({
                'success': True,
                'download_dir': config.NETEASE_DOWNLOAD_DIR,
                'api_base': config.NETEASE_API_BASE,
                'max_concurrent': config.NETEASE_MAX_CONCURRENT,
                'quality': NETEASE_QUALITY_DEFAULT
            })
        data = request.json or {}
        target_dir = data.get('download_dir')
        api_base = (data.get('api_base') or '').strip()

        if target_dir:
            target_dir = os.path.abspath(target_dir)
            os.makedirs(target_dir, exist_ok=True)
        else:
            target_dir = None

        if api_base:
            api_base = api_base.rstrip('/')

        if not target_dir and not api_base:
            return jsonify({'success': False, 'error': '未提供任何配置项'})

        save_netease_config(target_dir, api_base)
        return jsonify({
            'success': True,
            'download_dir': config.NETEASE_DOWNLOAD_DIR,
            'api_base': config.NETEASE_API_BASE,
            'max_concurrent': config.NETEASE_MAX_CONCURRENT,
            'quality': NETEASE_QUALITY_DEFAULT
        })
    except Exception as e:
        logger.warning(f"更新网易云配置失败: {e}")
        return jsonify({'success': False, 'error': '保存失败'})


@netease_bp.route('/api/netease/debug')
def netease_debug():
    """调试用，查看 cookie 是否加载。"""
    info = {
        'cookie_loaded': bool(config.NETEASE_COOKIE),
        'api_base': config.NETEASE_API_BASE,
        'download_dir': config.NETEASE_DOWNLOAD_DIR
    }
    return jsonify(info)


@netease_bp.route('/api/netease/resolve')
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


@netease_bp.route('/api/netease/playlist')
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


@netease_bp.route('/api/netease/song')
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


@netease_bp.route('/api/netease/song/url')
def netease_song_url():
    """获取网易云单曲可试听/可下载链接。"""
    raw_input = request.args.get('id') or request.args.get('link') or request.args.get('input')
    level = (request.args.get('level') or 'standard').strip().lower()
    parsed_input = _resolve_netease_input(raw_input, prefer='song')
    if not parsed_input:
        return jsonify({'success': False, 'error': '缺少歌曲链接或ID'})
    if parsed_input.get('type') == 'playlist':
        return jsonify({'success': False, 'error': '检测到歌单链接，请切换歌单解析'})

    song_id = parsed_input['id']
    valid_levels = {'standard', 'higher', 'exhigh', 'lossless', 'hires', 'jyeffect', 'sky', 'dolby', 'jymaster'}
    if level not in valid_levels:
        level = 'standard'

    try:
        api_resp = call_netease_api('/song/url/v1', {'id': song_id, 'level': level},
                                    need_cookie=bool(config.NETEASE_COOKIE))
        data_list = api_resp.get('data') if isinstance(api_resp, dict) else None
        track_info = None
        if isinstance(data_list, list) and data_list:
            track_info = data_list[0]
        elif isinstance(data_list, dict):
            track_info = data_list

        if (not track_info or not (track_info.get('url') or track_info.get('proxyUrl'))) and level != 'standard':
            api_resp_std = call_netease_api('/song/url/v1', {'id': song_id, 'level': 'standard'},
                                           need_cookie=bool(config.NETEASE_COOKIE))
            data_list = api_resp_std.get('data') if isinstance(api_resp_std, dict) else None
            if isinstance(data_list, list) and data_list:
                track_info = data_list[0]
            elif isinstance(data_list, dict):
                track_info = data_list
            level = 'standard'

        if not track_info:
            return jsonify({'success': False, 'error': '未获取到歌曲链接信息'})

        url = track_info.get('url') or track_info.get('proxyUrl')
        if not url:
            return jsonify({'success': False, 'error': '当前音质暂无可用链接'})

        return jsonify({
            'success': True,
            'data': {
                'id': song_id,
                'url': url,
                'level': level,
                'size': track_info.get('size') or 0,
                'type': track_info.get('type') or track_info.get('encodeType') or 'mp3'
            }
        })
    except Exception as e:
        logger.warning(f"获取网易云歌曲链接失败: {e}")
        return jsonify({'success': False, 'error': '获取链接失败'})


@netease_bp.route('/api/netease/download', methods=['POST'])
def download_netease_music():
    """根据歌曲ID下载网易云音乐到本地库。(异步)"""
    payload = request.json or {}
    song_id = payload.get('id')
    if not song_id:
        return jsonify({'success': False, 'error': '缺少歌曲ID'})

    active = sum(1 for t in DOWNLOAD_TASKS.values() if t.get('status') in ('pending', 'preparing', 'downloading'))
    if active >= config.NETEASE_MAX_CONCURRENT:
        return jsonify({'success': False, 'error': f'并发下载已达上限 ({config.NETEASE_MAX_CONCURRENT})，请稍后再试'})

    task_id = f"task_{int(time.time()*1000)}_{os.urandom(4).hex()}"
    DOWNLOAD_TASKS[task_id] = {
        'status': 'pending',
        'progress': 0,
        'title': payload.get('title', '未知'),
        'artist': payload.get('artist', '未知')
    }

    threading.Thread(target=run_download_task, args=(task_id, payload), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id})


@netease_bp.route('/api/netease/task/<task_id>')
def get_netease_task_status(task_id):
    task = DOWNLOAD_TASKS.get(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'})
    return jsonify({'success': True, 'data': task})


# ---------------------------------------------------------------------------
# Routes – Install status
# ---------------------------------------------------------------------------

@netease_bp.route('/api/netease/install/status')
def get_install_status():
    return jsonify(INSTALL_STATUS)


@netease_bp.route('/api/netease/install_service', methods=['POST'])
def install_netease_service():
    """尝试自动拉取并运行网易云 API 容器"""
    if INSTALL_STATUS['status'] == 'running':
        return jsonify({'success': False, 'error': '安装任务正在进行中'})

    INSTALL_STATUS.update({'status': 'running', 'progress': 0, 'step': '准备安装...', 'error': None})
    logger.info("API请求: 安装网易云服务")

    def run_install():
        try:
            INSTALL_STATUS.update({'progress': 10, 'step': '检查 Docker 环境...'})
            subprocess.run(["docker", "--version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            container_name = "2fmusic-ncm-api"
            INSTALL_STATUS.update({'progress': 20, 'step': f'检查容器 {container_name}...'})

            check_proc = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
                capture_output=True, text=True
            )

            if container_name in check_proc.stdout.strip():
                INSTALL_STATUS.update({'progress': 60, 'step': '容器已存在，正在启动...'})
                logger.info("容器已存在，尝试启动...")
                subprocess.run(["docker", "start", container_name], check=True)
            else:
                INSTALL_STATUS.update({'progress': 30, 'step': '正在拉取镜像 (耗时较长)...'})
                logger.info("正在拉取镜像 moefurina/ncm-api...")
                subprocess.run(["docker", "pull", "moefurina/ncm-api:latest"], check=True)

                INSTALL_STATUS.update({'progress': 70, 'step': '镜像拉取完成，正在启动容器...'})
                logger.info("正在启动容器...")
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

    threading.Thread(target=run_install, daemon=True).start()

    return jsonify({'success': True, 'message': '安装任务已启动'})
