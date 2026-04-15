#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sqlite3
import time

import requests
from flask import Blueprint, jsonify, request, session
from urllib.parse import quote, unquote

from config import (
    AUDIO_EXTS,
    COMMON_HEADERS,
    MUSIC_LIBRARY_PATH,
    NETEASE_API_BASE,
    NETEASE_API_BASE_DEFAULT,
    logger,
)
from models.db import get_db
from routes.qqmusic import call_qqmusic_api
from services.user import load_user_data, save_user_data

NETEASE_API_URL = (NETEASE_API_BASE or NETEASE_API_BASE_DEFAULT or '').rstrip('/') or None

playlist_bp = Blueprint('playlist_bp', __name__, url_prefix='')


@playlist_bp.route('/api/playlists')
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


@playlist_bp.route('/api/playlists', methods=['POST'])
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
            # 记录已添加的歌曲，用于检测重复
            added_songs = {}  # qq_mid -> {title, artist, index}
            skipped_songs = []  # 记录所有跳过的歌曲详情

            logger.info(f'创建歌单 "{name}"，准备保存 {len(pending_songs)} 首歌曲')

            for idx, song in enumerate(pending_songs):
                try:
                    qq_mid = song.get('mid') or song.get('qq_mid')
                    netease_id = song.get('netease_id')
                    source = song.get('source', 'qq')
                    title = song.get('title', '未知歌曲')
                    artist = song.get('artist', '')
                    # 优先使用前端传递的 sort_order，否则使用索引
                    sort_order = song.get('sort_order', idx)

                    # 先检查是否已经添加过（用于详细日志）
                    if qq_mid and qq_mid in added_songs:
                        skipped_count += 1
                        original = added_songs[qq_mid]
                        skipped_songs.append({
                            'index': idx + 1,
                            'title': title,
                            'artist': artist,
                            'qq_mid': qq_mid,
                            'original_index': original['index'],
                            'original_title': original['title'],
                            'original_artist': original['artist']
                        })
                        continue

                    cursor = conn.execute('''
                        INSERT OR IGNORE INTO playlist_pending_songs
                        (playlist_id, qq_mid, netease_id, title, artist, album, cover, source, added_at, sort_order)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        playlist_id,
                        qq_mid,
                        netease_id,
                        title,
                        artist,
                        song.get('album', ''),
                        song.get('cover', ''),
                        source,
                        now,
                        sort_order  # 保存原始顺序
                    ))
                    # 只有实际插入成功才计数
                    if cursor.rowcount > 0:
                        pending_count += 1
                        if qq_mid:
                            added_songs[qq_mid] = {'title': title, 'artist': artist, 'index': idx + 1}
                    else:
                        skipped_count += 1
                except Exception as e:
                    error_count += 1
                    logger.warning(f'保存待下载歌曲失败: {e}')

            conn.commit()
            logger.info(f'创建歌单 "{name}" 完成: 保存 {pending_count} 首, 跳过 {skipped_count} 首重复, {error_count} 首失败')

            # 详细输出所有重复歌曲
            if skipped_songs:
                logger.info(f'========== 重复歌曲详情 ({len(skipped_songs)} 首) ==========')
                for s in skipped_songs:
                    logger.info(f'  #{s["index"]} "{s["title"]}" - {s["artist"]} (mid={s["qq_mid"]})')
                    logger.info(f'      ↳ 与 #{s["original_index"]} "{s["original_title"]}" - {s["original_artist"]} 重复')
                logger.info(f'========== 重复歌曲详情结束 ==========')

            # 保存歌单数据到用户文件
            if user_hash:
                try:
                    user_data = load_user_data(user_hash)
                    if user_data:
                        if 'playlists' not in user_data:
                            user_data['playlists'] = []
                        # 构建歌单数据（包含所有歌曲信息，去重）
                        playlist_data = {
                            'id': playlist_id,
                            'name': name,
                            'source_url': source_url or None,
                            'source_type': source_type or None,
                            'created_at': now,
                            'songs': []
                        }
                        # 保存所有歌曲的详细信息（去重）
                        seen_mids = set()
                        for song in pending_songs:
                            qq_mid = song.get('mid') or song.get('qq_mid')
                            netease_id = song.get('netease_id')
                            # 用 qq_mid 或 netease_id 去重
                            song_key = qq_mid or netease_id
                            if song_key and song_key in seen_mids:
                                continue
                            if song_key:
                                seen_mids.add(song_key)

                            song_data = {
                                'qq_mid': qq_mid,
                                'netease_id': netease_id,
                                'title': song.get('title', '未知歌曲'),
                                'artist': song.get('artist', ''),
                                'album': song.get('album', ''),
                                'cover': song.get('cover', ''),
                                'source': song.get('source', 'qq'),
                                'sort_order': len(playlist_data['songs'])  # 使用去重后的索引
                            }
                            playlist_data['songs'].append(song_data)
                        user_data['playlists'].append(playlist_data)
                        save_user_data(user_hash, user_data)
                        logger.info(f'歌单数据已保存到用户文件: {len(playlist_data["songs"])} 首歌曲')
                except Exception as e:
                    logger.warning(f'保存歌单到用户文件失败: {e}')

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


@playlist_bp.route('/api/playlists/<int:playlist_id>', methods=['DELETE'])
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

            # 同步删除用户文件中的歌单数据
            if user_hash:
                try:
                    user_data = load_user_data(user_hash)
                    if user_data and 'playlists' in user_data:
                        user_data['playlists'] = [p for p in user_data['playlists'] if p.get('id') != playlist_id]
                        save_user_data(user_hash, user_data)
                        logger.info(f'已从用户文件中删除歌单 {playlist_id}')
                except Exception as e:
                    logger.warning(f'从用户文件删除歌单失败: {e}')

            return jsonify({'success': True})
    except Exception as e:
        logger.error(f'删除歌单失败: {e}')
        return jsonify({'success': False, 'error': str(e)})


@playlist_bp.route('/api/playlists/<int:playlist_id>/rename', methods=['POST'])
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


@playlist_bp.route('/api/playlists/<int:playlist_id>/songs')
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

            def normalize_artist(artist):
                """规范化艺术家名称：统一分隔符，排序后比较"""
                if not artist:
                    return ''
                # 统一分隔符：/ , 、 & _ 空格 都转为 ,
                normalized = artist.lower().strip()
                for sep in ['/', '、', '&', '，', '_', ' _ ']:
                    normalized = normalized.replace(sep, ',')
                # 特殊处理：如果没有逗号分隔符，尝试用空格分隔（针对多艺术家用空格分隔的情况）
                # 但要避免把单个艺术家名字中的空格也分开
                parts = [p.strip() for p in normalized.split(',') if p.strip()]
                # 如果只有一个部分，且包含空格，可能是用空格分隔的多艺术家
                # 检查是否像 "川青 morerare" 这样的格式（两个中文/英文名用空格分隔）
                if len(parts) == 1 and ' ' in parts[0]:
                    # 尝试用空格分隔
                    space_parts = [p.strip() for p in parts[0].split(' ') if p.strip()]
                    # 如果分隔后每个部分都像是一个独立的名字（不是太长），就用空格分隔
                    if len(space_parts) >= 2 and all(len(p) <= 20 for p in space_parts):
                        parts = space_parts
                return ','.join(sorted(parts))

            def normalize_filename_underscores(s):
                """规范化文件名中的下划线：
                - ' _ ' (空格下划线空格) 转为 ' / ' (艺术家分隔符)
                - 'feat_' 转为 'feat.'
                - 其他单独的 '_' 保持不变
                """
                if not s:
                    return ''
                # 先处理 feat_ 等常见缩写
                s = re.sub(r'\bfeat_', 'feat.', s, flags=re.IGNORECASE)
                s = re.sub(r'\bft_', 'ft.', s, flags=re.IGNORECASE)
                # 处理艺术家分隔符 ' _ ' -> ' / '
                s = s.replace(' _ ', ' / ')
                return s

            # 规范化函数：统一全角括号为半角，并去掉括号前的空格（仅用于标题）
            def normalize_brackets(s):
                if not s:
                    return ''
                s = s.replace('（', '(').replace('）', ')').replace('【', '[').replace('】', ']')
                # 去掉括号前的空格: "标题 (xxx)" -> "标题(xxx)"
                s = s.replace(' (', '(').replace(' [', '[')
                # 移除音质标签：[无损]、[高质量]、[无损 高质量] 等
                import re
                s = re.sub(r'\[无损[^\]]*\]', '', s)
                s = re.sub(r'\[高质量[^\]]*\]', '', s)
                s = re.sub(r'\[hq[^\]]*\]', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\[flac[^\]]*\]', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\[lossless[^\]]*\]', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\[hi-?res[^\]]*\]', '', s, flags=re.IGNORECASE)
                return s.strip()

            # 规范化函数：只统一全角括号为半角，不去掉空格（用于文件名分割前）
            def normalize_brackets_keep_space(s):
                if not s:
                    return ''
                s = s.replace('（', '(').replace('）', ')').replace('【', '[').replace('】', ']')
                # 移除音质标签
                import re
                s = re.sub(r'\[无损[^\]]*\]', '', s)
                s = re.sub(r'\[高质量[^\]]*\]', '', s)
                s = re.sub(r'\[hq[^\]]*\]', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\[flac[^\]]*\]', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\[lossless[^\]]*\]', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\[hi-?res[^\]]*\]', '', s, flags=re.IGNORECASE)
                return s.strip()

            for row in pending_rows:
                # 检查是否已经在本地存在（通过标题和艺术家匹配）
                pending_title = normalize_brackets(row['title'] or '').lower().strip()
                pending_artist = (row['artist'] or '').lower().strip()
                pending_artist_normalized = normalize_artist(row['artist'])

                # 调试日志
                if '一路往南走' in (row['title'] or '') or '下潜' in (row['title'] or ''):
                    logger.info(f'[匹配调试] 待下载: "{row["title"]}" -> 规范化: "{pending_title}"')

                matched_local = None
                for local in all_local_songs:
                    local_title = normalize_brackets(local['title'] or '').lower().strip()
                    local_artist = (local['artist'] or '').lower().strip()
                    local_artist_normalized = normalize_artist(local['artist'])
                    # 文件名处理：先规范化下划线，再保留空格以便正确分割艺术家和标题
                    filename_raw = normalize_filename_underscores(os.path.splitext(local['filename'] or '')[0])
                    filename_base_raw = normalize_brackets_keep_space(filename_raw).lower().strip()
                    # 用于直接比较的版本（去掉括号前空格）
                    filename_base = normalize_brackets(filename_raw).lower().strip()

                    # 调试日志
                    if ('一路往南走' in (row['title'] or '') or '下潜' in (row['title'] or '') or '嚣张' in (row['title'] or '')) and \
                       ('一路往南走' in (local['filename'] or '') or '下潜' in (local['filename'] or '') or '嚣张' in (local['filename'] or '')):
                        logger.info(f'[匹配调试] 本地文件: "{local["filename"]}"')
                        logger.info(f'[匹配调试]   local_title="{local_title}", filename_base="{filename_base}"')
                        logger.info(f'[匹配调试]   pending_title="{pending_title}" == local_title? {pending_title == local_title}')
                        logger.info(f'[匹配调试]   pending_title="{pending_title}" == filename_base? {pending_title == filename_base}')
                        if ' - ' in filename_base_raw:
                            dbg_parts = filename_base_raw.split(' - ', 1)
                            logger.info(f'[匹配调试]   文件名分割(raw): part1="{dbg_parts[0]}", part2="{dbg_parts[1]}"')
                            logger.info(f'[匹配调试]   pending_title == part2? {pending_title == normalize_brackets(dbg_parts[1]).strip()}')

                    # 1. 精确匹配标题 + 艺术家（规范化后比较）
                    if pending_title and local_title and pending_title == local_title:
                        if not pending_artist or not local_artist or pending_artist_normalized == local_artist_normalized:
                            matched_local = local
                            break

                    # 2. 文件名精确匹配（去掉扩展名后完全相同）
                    if pending_title and filename_base and pending_title == filename_base:
                        matched_local = local
                        break

                    # 3. 文件名包含 " - " 格式，支持两种格式：
                    #    - "艺术家 - 标题"
                    #    - "标题 - 艺术家"
                    if pending_title and ' - ' in filename_base_raw:
                        parts = filename_base_raw.split(' - ', 1)
                        if len(parts) == 2:
                            # part1 和 part2 保留原始空格，用于艺术家比较
                            part1_raw = parts[0].strip()
                            part2_raw = parts[1].strip()
                            # 标题比较时去掉括号前空格
                            part1 = normalize_brackets(part1_raw)
                            part2 = normalize_brackets(part2_raw)

                            # 尝试格式1: "艺术家 - 标题" - 只要标题匹配就认为是同一首歌
                            if pending_title == part2:
                                matched_local = local
                                break

                            # 尝试格式2: "标题 - 艺术家" - 只要标题匹配就认为是同一首歌
                            if pending_title == part1:
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


@playlist_bp.route('/api/playlists/<int:playlist_id>/songs', methods=['POST'])
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


@playlist_bp.route('/api/playlists/<int:playlist_id>/songs/<song_id>', methods=['DELETE'])
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


@playlist_bp.route('/api/playlists/<int:playlist_id>/pending/<int:pending_id>', methods=['DELETE'])
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


@playlist_bp.route('/api/playlists/<int:playlist_id>/pending/convert', methods=['POST'])
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


@playlist_bp.route('/api/playlists/<int:playlist_id>/sync', methods=['POST'])
def sync_playlist(playlist_id):
    """同步歌单（从源链接获取新歌曲，和用户文件对比）"""
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
            playlist_name = playlist['name']

            if not source_url:
                return jsonify({'success': False, 'error': '此歌单没有关联源链接，无法同步'})

            # ========== 第一步：从用户文件读取旧歌单数据 ==========
            old_songs_map = {}  # qq_mid/netease_id -> song_data
            user_playlist_data = None

            if user_hash:
                user_data = load_user_data(user_hash)
                if user_data and 'playlists' in user_data:
                    for p in user_data['playlists']:
                        if p.get('id') == playlist_id:
                            user_playlist_data = p
                            for song in p.get('songs', []):
                                if source_type == 'qq' and song.get('qq_mid'):
                                    old_songs_map[song['qq_mid']] = song
                                elif source_type == 'netease' and song.get('netease_id'):
                                    old_songs_map[str(song['netease_id'])] = song
                            break

            logger.info(f'同步歌单 {playlist_id} "{playlist_name}": 用户文件中有 {len(old_songs_map)} 首歌曲')

            # ========== 第二步：从源获取最新歌曲列表 ==========
            all_source_songs = []  # 源的完整歌曲列表
            source_songs_map = {}  # qq_mid/netease_id -> song_data

            if source_type == 'qq':
                # 解析QQ音乐歌单ID
                logger.info(f'同步歌单: 尝试解析QQ音乐链接: {source_url}')

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

                resp = call_qqmusic_api('playlist', 'get_playlist_detail', {'id': playlist_tid})

                if resp.get('code') != 200:
                    return jsonify({'success': False, 'error': resp.get('message') or '获取歌单失败'})

                songs = resp.get('data', {}).get('songlist', [])
                for idx, song in enumerate(songs):
                    mid = song.get('mid') or song.get('songmid', '')
                    if not mid:
                        continue

                    # 跳过重复的 qq_mid（歌单里可能有重复歌曲）
                    if mid in source_songs_map:
                        continue

                    singers = song.get('singer', [])
                    artist = ', '.join([s.get('name', '') for s in singers if s.get('name')]) if singers else ''

                    album_info = song.get('album', {})
                    if isinstance(album_info, dict):
                        album_name = album_info.get('name', '')
                        album_mid = album_info.get('mid', '')
                    else:
                        album_name = song.get('albumname', '')
                        album_mid = song.get('albummid', '')

                    cover = f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else ''
                    title = song.get('title') or song.get('name') or song.get('songname', '未知歌曲')

                    song_data = {
                        'qq_mid': mid,
                        'netease_id': None,
                        'title': title,
                        'artist': artist,
                        'album': album_name,
                        'cover': cover,
                        'source': 'qq',
                        'sort_order': len(all_source_songs)  # 使用去重后的索引
                    }
                    all_source_songs.append(song_data)
                    source_songs_map[mid] = song_data

            elif source_type == 'netease':
                match = re.search(r'id[=:](\d+)', source_url)
                if not match:
                    match = re.search(r'/playlist[/?](\d+)', source_url)
                if not match:
                    return jsonify({'success': False, 'error': '无法解析网易云歌单ID'})

                playlist_nid = match.group(1)

                if not NETEASE_API_URL:
                    return jsonify({'success': False, 'error': '网易云API未配置'})

                netease_resp = requests.get(f'{NETEASE_API_URL}/playlist/track/all', params={'id': playlist_nid}, timeout=30)
                data = netease_resp.json()

                if data.get('code') != 200:
                    return jsonify({'success': False, 'error': '获取网易云歌单失败'})

                songs = data.get('songs', [])
                for idx, song in enumerate(songs):
                    nid = str(song.get('id', ''))
                    if not nid:
                        continue

                    # 跳过重复的 netease_id
                    if nid in source_songs_map:
                        continue

                    artists = song.get('ar', [])
                    album = song.get('al', {})
                    title = song.get('name', '未知歌曲')
                    artist = ', '.join(a.get('name', '') for a in artists) if artists else ''

                    song_data = {
                        'qq_mid': None,
                        'netease_id': nid,
                        'title': title,
                        'artist': artist,
                        'album': album.get('name', '') if album else '',
                        'cover': album.get('picUrl', '') if album else '',
                        'source': 'netease',
                        'sort_order': len(all_source_songs)  # 使用去重后的索引
                    }
                    all_source_songs.append(song_data)
                    source_songs_map[nid] = song_data

            else:
                return jsonify({'success': False, 'error': f'不支持的源类型: {source_type}'})

            logger.info(f'同步歌单 {playlist_id}: 从源获取到 {len(all_source_songs)} 首歌曲')

            # ========== 第三步：对比找出新增和删除的歌曲 ==========
            old_ids = set(old_songs_map.keys())
            new_ids = set(source_songs_map.keys())

            added_ids = new_ids - old_ids  # 新增的
            removed_ids = old_ids - new_ids  # 删除的

            logger.info(f'同步歌单 {playlist_id}: 新增 {len(added_ids)} 首, 删除 {len(removed_ids)} 首')

            # ========== 第四步：更新数据库（清空后重新插入） ==========
            now = time.time()

            # 清空该歌单的所有记录（包括已下载和待下载）
            # 这样可以避免重复计算歌曲数量
            conn.execute('DELETE FROM playlist_songs WHERE playlist_id = ?', (playlist_id,))
            conn.execute('DELETE FROM playlist_pending_songs WHERE playlist_id = ?', (playlist_id,))

            # 重新插入所有歌曲
            inserted_count = 0
            for song in all_source_songs:
                try:
                    conn.execute('''
                        INSERT INTO playlist_pending_songs
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
                        song.get('sort_order', 0)
                    ))
                    inserted_count += 1
                except Exception as e:
                    logger.warning(f'插入歌曲失败: {e}')

            # 更新歌单同步时间
            conn.execute(
                'UPDATE playlists SET last_synced_at = ?, updated_at = ? WHERE id = ?',
                (now, now, playlist_id)
            )
            conn.commit()

            logger.info(f'同步歌单 {playlist_id}: 数据库已更新，共 {inserted_count} 首歌曲')

            # ========== 第五步：更新用户文件 ==========
            if user_hash and (added_ids or removed_ids or not user_playlist_data):
                try:
                    user_data = load_user_data(user_hash)
                    if user_data:
                        if 'playlists' not in user_data:
                            user_data['playlists'] = []

                        # 查找并更新歌单数据
                        found = False
                        for i, p in enumerate(user_data['playlists']):
                            if p.get('id') == playlist_id:
                                user_data['playlists'][i] = {
                                    'id': playlist_id,
                                    'name': playlist_name,
                                    'source_url': source_url,
                                    'source_type': source_type,
                                    'created_at': p.get('created_at', now),
                                    'last_synced_at': now,
                                    'songs': all_source_songs
                                }
                                found = True
                                break

                        # 如果没找到，添加新的
                        if not found:
                            user_data['playlists'].append({
                                'id': playlist_id,
                                'name': playlist_name,
                                'source_url': source_url,
                                'source_type': source_type,
                                'created_at': now,
                                'last_synced_at': now,
                                'songs': all_source_songs
                            })

                        save_user_data(user_hash, user_data)
                        logger.info(f'同步歌单 {playlist_id}: 用户文件已更新，共 {len(all_source_songs)} 首歌曲')
                except Exception as e:
                    logger.warning(f'更新用户文件失败: {e}')

            # 构建返回消息
            if added_ids or removed_ids:
                message = f'同步完成：新增 {len(added_ids)} 首'
                if removed_ids:
                    message += f'，移除 {len(removed_ids)} 首'
            else:
                message = '歌单已是最新，没有变化'

            logger.info(f'歌单 {playlist_id} 同步完成: {message}')

            return jsonify({
                'success': True,
                'added_count': len(added_ids),
                'removed_count': len(removed_ids),
                'total_count': len(all_source_songs),
                'message': message
            })
    except Exception as e:
        logger.error(f'同步歌单失败: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})
