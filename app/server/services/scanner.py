#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import concurrent.futures
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import config
from config import (
    MUSIC_LIBRARY_PATH, AUDIO_EXTS, logger, generate_song_id
)
from models.db import get_db
from services.metadata import get_metadata, extract_embedded_cover, check_cover_exists


def index_single_file(file_path):
    try:
        if not os.path.exists(file_path):
            return
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in AUDIO_EXTS:
            return

        stat = os.stat(file_path)
        meta = get_metadata(file_path)
        sid = generate_song_id(file_path)
        base_name = os.path.splitext(os.path.basename(file_path))[0]

        has_cover = 1 if check_cover_exists(file_path, base_name) else 0
        if not has_cover:
            if extract_embedded_cover(file_path, base_name):
                has_cover = 1

        with get_db() as conn:
            dup = conn.execute(
                "SELECT path FROM songs WHERE filename=? AND size=? AND path!=?",
                (os.path.basename(file_path), stat.st_size, file_path)
            ).fetchone()
            if dup:
                logger.info(f"索引: 跳过重复文件 {file_path} (已存在: {dup['path']})")
                return
            conn.execute('''
                INSERT OR REPLACE INTO songs (id, path, filename, title, artist, album, mtime, size, has_cover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (sid, file_path, os.path.basename(file_path),
                  meta['title'], meta['artist'], meta['album'],
                  stat.st_mtime, stat.st_size, has_cover))
            conn.commit()
        logger.info(f"单文件索引完成: {file_path}")
    except Exception as e:
        logger.error(f"单文件索引失败: {e}")


def scan_library_incremental():
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
        config.SCAN_STATUS.update({'scanning': True, 'total': 0, 'processed': 0, 'current_file': '正在遍历文件...'})
        with open(lock_file, 'w') as f:
            f.write(str(time.time()))
        logger.info("开始增量扫描...")

        scan_roots = [MUSIC_LIBRARY_PATH]
        try:
            with get_db() as conn:
                rows = conn.execute("SELECT path FROM mount_points").fetchall()
                scan_roots.extend([r['path'] for r in rows])
        except Exception:
            pass

        disk_files = {}
        supported_exts = AUDIO_EXTS

        for root_dir in scan_roots:
            if not os.path.exists(root_dir):
                continue
            for root, dirs, files in os.walk(root_dir):
                dirs[:] = [d for d in dirs if d not in ('lyrics', 'covers')]
                for f in files:
                    if f.lower().endswith(supported_exts):
                        path = os.path.join(root, f)
                        try:
                            stat = os.stat(path)
                            info = {'mtime': stat.st_mtime, 'size': stat.st_size, 'path': path, 'filename': f}
                            disk_files[path] = info
                        except:
                            pass

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, path, mtime, size FROM songs")
            db_rows = {row['path']: row for row in cursor.fetchall()}

            to_delete_paths = set(db_rows.keys()) - set(disk_files.keys())
            if to_delete_paths:
                cursor.executemany("DELETE FROM songs WHERE path=?", [(p,) for p in to_delete_paths])
                conn.commit()

            files_to_process_list = []
            for path, info in disk_files.items():
                db_rec = db_rows.get(path)
                if not db_rec or db_rec['mtime'] != info['mtime'] or db_rec['size'] != info['size']:
                    files_to_process_list.append(info)

            total_files = len(files_to_process_list)
            config.SCAN_STATUS.update({'total': total_files, 'processed': 0})

            to_update_db = []

            if total_files > 0:
                logger.info(f"使用线程池处理 {total_files} 个文件...")

                def process_file_metadata(info):
                    meta = get_metadata(info['path'])
                    sid = generate_song_id(info['path'])
                    base_name = os.path.splitext(info['filename'])[0]
                    has_cover = 1 if check_cover_exists(info['path'], base_name) else 0
                    return (sid, info['path'], info['filename'],
                            meta['title'], meta['artist'], meta['album'],
                            info['mtime'], info['size'], has_cover)

                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {executor.submit(process_file_metadata, item): item for item in files_to_process_list}
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            res = future.result()
                            to_update_db.append(res)
                        except Exception:
                            pass
                        config.SCAN_STATUS['processed'] += 1
                        if config.SCAN_STATUS['processed'] % 10 == 0:
                            config.SCAN_STATUS['current_file'] = (
                                f"处理中... {int((config.SCAN_STATUS['processed']/total_files)*100)}%"
                            )

                final_update_db = []
                seen_in_batch = set()

                for item in to_update_db:
                    c_path, c_fname, c_size = item[1], item[2], item[7]
                    if (c_fname, c_size) in seen_in_batch:
                        logger.info(f"扫描: 跳过批次内重复文件 {c_path}")
                        continue
                    try:
                        dup = conn.execute(
                            "SELECT path FROM songs WHERE filename=? AND size=? AND path!=?",
                            (c_fname, c_size, c_path)
                        ).fetchone()
                        if dup:
                            logger.info(f"扫描: 跳过全局重复文件 {c_path} (已存在: {dup['path']})")
                            continue
                    except Exception:
                        pass
                    seen_in_batch.add((c_fname, c_size))
                    final_update_db.append(item)

                if final_update_db:
                    cursor.executemany('''
                        INSERT OR REPLACE INTO songs (id, path, filename, title, artist, album, mtime, size, has_cover)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', final_update_db)
                    conn.commit()

        logger.info("扫描完成。")
        config.LIBRARY_VERSION = time.time()

    except Exception as e:
        logger.error(f"扫描失败: {e}")
    finally:
        config.SCAN_STATUS['scanning'] = False
        config.SCAN_STATUS['current_file'] = ''
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
            except:
                pass


class MusicFileEventHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        self._process(event.src_path, 'created')

    def on_deleted(self, event):
        if event.is_directory:
            return
        self._process(event.src_path, 'deleted')

    def on_moved(self, event):
        if event.is_directory:
            return
        self._process(event.src_path, 'deleted')
        self._process(event.dest_path, 'created')

    def _process(self, path, action):
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
                    base = os.path.splitext(path)[0]
                    for aud in AUDIO_EXTS:
                        aud_path = base + aud
                        if os.path.exists(aud_path):
                            index_single_file(aud_path)

            config.LIBRARY_VERSION = time.time()

        except Exception as e:
            logger.error(f"处理文件变更失败: {e}")


def init_watchdog():
    if not Observer:
        return
    if config.global_observer:
        config.global_observer.stop()
        config.global_observer.join()

    config.global_observer = Observer()
    refresh_watchdog_paths()
    config.global_observer.start()
    logger.info("文件监听服务已启动")
    try:
        while True:
            time.sleep(1)
    except:
        config.global_observer.stop()
    config.global_observer.join()


def refresh_watchdog_paths():
    if not config.global_observer:
        return

    config.global_observer.unschedule_all()

    try:
        raw_paths = {os.path.abspath(MUSIC_LIBRARY_PATH)}
        with get_db() as conn:
            rows = conn.execute("SELECT path FROM mount_points").fetchall()
            for r in rows:
                if r['path']:
                    raw_paths.add(os.path.abspath(r['path']))
    except:
        raw_paths = {os.path.abspath(MUSIC_LIBRARY_PATH)}

    sorted_paths = sorted(list(raw_paths), key=len)
    final_targets = []
    for p in sorted_paths:
        if not any(p.startswith(parent + os.sep) or p == parent for parent in final_targets):
            final_targets.append(p)

    event_handler = MusicFileEventHandler()
    for path in final_targets:
        if os.path.exists(path):
            try:
                config.global_observer.schedule(event_handler, path, recursive=True)
                logger.info(f"监听目录: {path}")
            except Exception as e:
                logger.warning(f"无法监听目录 {path}: {e}")
