#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import requests
from mutagen import File
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC, USLT
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover

import config
from config import INVALID_METADATA_VALUES, COMMON_HEADERS, MUSIC_LIBRARY_PATH, logger
from models.db import get_db


def _is_valid_metadata(value):
    if not value:
        return False
    val_lower = value.strip().lower()
    if val_lower in INVALID_METADATA_VALUES:
        return False
    if val_lower.isdigit():
        return False
    return True


def get_metadata(file_path):
    metadata = {'title': None, 'artist': None, 'album': None}
    try:
        audio = None
        try:
            audio = EasyID3(file_path)
        except Exception:
            try:
                audio = File(file_path, easy=True)
            except Exception:
                try:
                    audio = File(file_path)
                except Exception as e3:
                    logger.warning(f"文件 {file_path} 无法解析，可能已损坏: {e3}")
                    audio = None
        if audio:
            def get_tag(key):
                if hasattr(audio, 'get'):
                    val = audio.get(key)
                    if isinstance(val, list):
                        val = val[0] if val else None
                    if val is not None:
                        return str(val)
                    return val
                return None

            title_val = get_tag('title')
            artist_val = get_tag('artist')
            album_val = get_tag('album')
            metadata['title'] = title_val if _is_valid_metadata(title_val) else None
            metadata['artist'] = artist_val if _is_valid_metadata(artist_val) else None
            metadata['album'] = album_val if _is_valid_metadata(album_val) else None
    except Exception as e:
        logger.warning(f"提取元数据失败: {file_path}, 错误: {e}")

    filename = os.path.splitext(os.path.basename(file_path))[0]

    if not metadata['title']:
        if ' - ' in filename:
            parts = filename.split(' - ', 1)
            parsed_artist = parts[0].strip()
            parsed_title = parts[1].strip()
            if _is_valid_metadata(parsed_title):
                metadata['title'] = parsed_title
            if not metadata['artist'] and _is_valid_metadata(parsed_artist):
                metadata['artist'] = parsed_artist
        if not metadata['title']:
            if _is_valid_metadata(filename):
                metadata['title'] = filename
            else:
                metadata['title'] = "未知歌曲"

    if not metadata['artist']:
        metadata['artist'] = "未知艺术家"

    logger.debug(f"文件 {file_path} 元数据: {metadata}")
    return metadata


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


def extract_embedded_cover(file_path: str, base_name: str = None, target_dir: str = None):
    try:
        if not os.path.exists(file_path):
            return False
        base_name = base_name or os.path.splitext(os.path.basename(file_path))[0]
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
    try:
        if not os.path.exists(file_path):
            return None
        audio = File(file_path)
        if not audio:
            return None
        if hasattr(audio, 'tags') and isinstance(audio.tags, ID3):
            for key in audio.tags.keys():
                if key.startswith('USLT'):
                    return audio.tags[key].text
        if hasattr(audio, 'tags'):
            lyrics = (audio.tags.get('lyrics') or audio.tags.get('LYRICS') or
                      audio.tags.get('unsyncedlyrics') or audio.tags.get('UNSYNCEDLYRICS'))
            if lyrics:
                return lyrics[0]
        if hasattr(audio, 'tags') and '©lyr' in audio.tags:
            return audio.tags['©lyr'][0]
    except Exception as e:
        logger.warning(f"提取内嵌歌词失败: {file_path}, 错误: {repr(e)}")
    return None


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
    if not cover_bytes or not base_name:
        return None
    try:
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


def embed_lyrics_to_file(audio_path: str, lrc_text: str):
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


def check_cover_exists(file_path: str, base_name: str = None) -> bool:
    if not base_name:
        base_name = os.path.splitext(os.path.basename(file_path))[0]
    base_path = os.path.splitext(file_path)[0]

    if os.path.exists(base_path + ".jpg"):
        return True

    song_dir = os.path.dirname(file_path)
    if os.path.exists(os.path.join(song_dir, 'covers', f"{base_name}.jpg")):
        return True

    try:
        with get_db() as conn:
            rows = conn.execute("SELECT path FROM mount_points").fetchall()
            for r in rows:
                if r['path'] and os.path.exists(os.path.join(r['path'], 'covers', f"{base_name}.jpg")):
                    return True
    except Exception:
        pass

    if os.path.exists(os.path.join(MUSIC_LIBRARY_PATH, 'covers', f"{base_name}.jpg")):
        return True

    return False
