#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from flask import Blueprint, request, jsonify, session, send_file, render_template
import os
import time
import re
import shutil
import threading
import requests
from urllib.parse import quote, unquote

import config
from config import (
    logger, COMMON_HEADERS, MUSIC_LIBRARY_PATH, AUDIO_EXTS, generate_song_id,
)
import mod
from models.db import get_db
from services.metadata import (
    get_metadata, extract_embedded_cover, extract_embedded_lyrics,
    check_cover_exists, get_default_download_dir,
)
from services.scanner import (
    index_single_file, scan_library_incremental, refresh_watchdog_paths,
)

music_bp = Blueprint('music_bp', __name__)


# ------------------------------------------------------------------
# Index
# ------------------------------------------------------------------
@music_bp.route('/')
def index():
    is_admin = session.get('is_admin', False)
    return render_template('index.html', is_admin=is_admin)


# ------------------------------------------------------------------
# 系统状态接口
# ------------------------------------------------------------------
@music_bp.route('/api/system/status')
def get_system_status():
    """返回当前扫描状态和进度"""
    status = dict(config.SCAN_STATUS)
    status['library_version'] = config.LIBRARY_VERSION
    return jsonify(status)


# ------------------------------------------------------------------
# 音乐列表
# ------------------------------------------------------------------
@music_bp.route('/api/music', methods=['GET'])
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
                    'id': row['id'],
                    'filename': row['filename'], 'title': row['title'],
                    'artist': row['artist'], 'album': row['album'], 'album_art': album_art
                })
        logger.info(f"返回音乐数量: {len(songs)}")
        return jsonify({'success': True, 'data': songs})
    except Exception as e:
        logger.exception(f"获取音乐列表失败: {e}")
        return jsonify({'success': False, 'error': str(e)})


# ------------------------------------------------------------------
# 播放
# ------------------------------------------------------------------
@music_bp.route('/api/music/play/<song_id>')
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


# ------------------------------------------------------------------
# 库管理
# ------------------------------------------------------------------
@music_bp.route('/api/library/rescan', methods=['POST'])
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


# ------------------------------------------------------------------
# 挂载相关
# ------------------------------------------------------------------
@music_bp.route('/api/mount_points', methods=['GET'])
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


@music_bp.route('/api/mount_points', methods=['POST'])
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


@music_bp.route('/api/mount_points', methods=['DELETE'])
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
        config.LIBRARY_VERSION = time.time()
            
        return jsonify({'success': True, 'message': '已移除'})
    except Exception as e: return jsonify({'success': False, 'error': str(e)})


# ------------------------------------------------------------------
# 歌词 API
# ------------------------------------------------------------------
@music_bp.route('/api/music/lyrics')
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

    # 3. 本地多源搜索 (QQ/网易/酷狗 并发)
    save_lrc_path = None
    if actual_path:
        local_dir = os.path.dirname(actual_path)
        base_name = os.path.splitext(os.path.basename(actual_path))[0]
        save_lrc_path = os.path.join(local_dir, 'lyrics', f"{base_name}.lrc")
    elif filename:
        lyrics_base_dir = get_default_download_dir()
        save_lrc_path = os.path.join(lyrics_base_dir, 'lyrics',
                                     f"{os.path.splitext(os.path.basename(filename))[0]}.lrc")

    try:
        logger.info(f"多源搜索歌词: {title} - {artist}")
        result = mod.search_all(title=title, artist=artist or '', album='')
        if result and result.get('lyrics'):
            lyrics_text = result['lyrics']
            if save_lrc_path:
                try:
                    os.makedirs(os.path.dirname(save_lrc_path), exist_ok=True)
                    with open(save_lrc_path, 'w', encoding='utf-8') as f:
                        f.write(lyrics_text)
                    logger.info(f"多源歌词保存: {save_lrc_path}")
                except Exception as e:
                    logger.warning(f"保存多源歌词失败: {e}")
            return jsonify({'success': True, 'lyrics': lyrics_text})
    except Exception as e:
        logger.warning(f"多源搜索歌词异常: {e}")

    logger.warning(f"歌词获取失败: {title} - {artist}")
    return jsonify({'success': False})


# ------------------------------------------------------------------
# 封面 API
# ------------------------------------------------------------------
@music_bp.route('/api/music/album-art')
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

    # 本地多源搜索封面 (QQ/网易/酷狗 并发)
    os.makedirs(cover_save_dir, exist_ok=True)

    try:
        logger.info(f"多源搜索封面: {title} - {artist}")
        result = mod.search_all(title=title, artist=artist or '', album='')
        if result and result.get('cover'):
            cover_url = result['cover']
            try:
                cover_resp = requests.get(cover_url, timeout=8, headers=COMMON_HEADERS)
                if cover_resp.status_code == 200 and cover_resp.content:
                    with open(local_path, 'wb') as f:
                        f.write(cover_resp.content)
                    logger.info(f"多源封面保存: {local_path}")
                    if not os.path.isabs(filename):
                        with get_db() as conn:
                            conn.execute("UPDATE songs SET has_cover=1 WHERE filename=?", (filename,))
                            conn.commit()
                    return jsonify({'success': True, 'album_art': f"/api/music/covers/{quote(base_name)}.jpg?filename={quote(filename)}"})
            except Exception as e:
                logger.warning(f"多源封面下载失败: {e}")
    except Exception as e:
        logger.warning(f"多源搜索封面异常: {e}")

    logger.warning(f"封面获取失败: {title} - {artist}")
    return jsonify({'success': False})


# ------------------------------------------------------------------
# 删除
# ------------------------------------------------------------------
@music_bp.route('/api/music/delete/<song_id>', methods=['DELETE'])
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


# ------------------------------------------------------------------
# 清除元数据
# ------------------------------------------------------------------
@music_bp.route('/api/music/clear_metadata', methods=['POST'])
@music_bp.route('/api/music/clear_metadata/<song_id>', methods=['POST'])
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


# ------------------------------------------------------------------
# 辅助接口 — 封面文件服务
# ------------------------------------------------------------------
@music_bp.route('/api/music/covers/<cover_name>')
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


# ------------------------------------------------------------------
# 上传
# ------------------------------------------------------------------
@music_bp.route('/api/music/upload', methods=['POST'])
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


# ------------------------------------------------------------------
# 路径导入
# ------------------------------------------------------------------
@music_bp.route('/api/music/import_path', methods=['POST'])
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


# ------------------------------------------------------------------
# 外部文件 — 元数据 & 播放
# ------------------------------------------------------------------
@music_bp.route('/api/music/external/meta')
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


@music_bp.route('/api/music/external/play')
def play_external_file():
    path = request.args.get('path')
    if path and os.path.exists(path): return send_file(path, conditional=True)
    return jsonify({'error': '文件未找到'}), 404
