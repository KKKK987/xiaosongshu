# -*- coding: utf-8 -*-
import os
import time
import json
from functools import wraps
from urllib.parse import quote

from flask import Blueprint, request, jsonify, session

from config import logger, USER_DATA_DIR
from models.db import get_db
from services.user import load_user_data, save_user_data, get_current_user

admin_bp = Blueprint('admin', __name__, url_prefix='')


# --- 管理员 API ---
def require_admin(f):
    """管理员权限装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({'success': False, 'error': '需要管理员权限'}), 403
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/api/admin/users', methods=['GET'])
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


@admin_bp.route('/api/admin/users/<user_id>', methods=['GET'])
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


@admin_bp.route('/api/admin/users/<user_id>', methods=['DELETE'])
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


@admin_bp.route('/api/admin/stats/overview', methods=['GET'])
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


@admin_bp.route('/api/admin/stats/user/<user_id>', methods=['GET'])
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


@admin_bp.route('/api/admin/stats/user/<user_id>/history', methods=['GET'])
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


@admin_bp.route('/api/admin/stats/top-songs', methods=['GET'])
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


@admin_bp.route('/api/admin/stats/active-users', methods=['GET'])
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


@admin_bp.route('/api/play/record', methods=['POST'])
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
