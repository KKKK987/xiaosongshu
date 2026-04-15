#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import hashlib
from flask import session

import config
from config import USER_DATA_DIR, APP_AUTH_PASSWORD, logger


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
    return os.path.join(USER_DATA_DIR, f"{password_hash[:16]}.json")


def load_user_data(password_hash: str) -> dict:
    file_path = get_user_file_path(password_hash)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_user_data(password_hash: str, data: dict):
    file_path = get_user_file_path(password_hash)
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"保存用户数据失败: {e}")
        return False


def create_user(password_hash: str, is_admin: bool = False) -> dict:
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
    password_hash = session.get('user_hash')
    if not password_hash:
        return None
    return load_user_data(password_hash)


def init_admin_user():
    if not APP_AUTH_PASSWORD:
        return
    admin_hash = hashlib.sha256(APP_AUTH_PASSWORD.encode()).hexdigest()
    if not load_user_data(admin_hash):
        create_user(admin_hash, is_admin=True)
        logger.info("管理员用户已创建")
