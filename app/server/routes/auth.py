#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib

from flask import Blueprint, jsonify, make_response, redirect, render_template, request, session, url_for
from flask import current_app
from services.user import (
    create_user,
    get_current_user,
    load_user_data,
    save_user_data,
    validate_password,
)

auth_bp = Blueprint('auth', __name__, url_prefix='')


@auth_bp.route('/login', methods=['GET', 'POST'])
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

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    # 注册功能已整合到登录页面，重定向到登录页
    return redirect(url_for('auth.login'))

@auth_bp.route('/logout')
def logout():
    session.pop('authed', None)
    session.pop('user_hash', None)
    session.pop('is_admin', None)
    session.clear()
    resp = make_response(redirect(url_for('auth.login')))
    resp.delete_cookie(current_app.config.get('SESSION_COOKIE_NAME', 'session'))
    return resp

@auth_bp.route('/api/user/info')
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
