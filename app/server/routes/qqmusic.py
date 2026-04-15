"""
QQ Music API Blueprint – all QQ-music related helpers and routes.
"""

from flask import Blueprint, request, jsonify, session, Response
import os
import sys
import time
import re
import threading
import json
import base64
import hashlib
import shutil
import random
import string
import binascii
import traceback
from base64 import b64encode, b64decode
from datetime import datetime, timedelta
from uuid import uuid4

import requests
from urllib.parse import quote, unquote, urlparse
from mutagen.easyid3 import EasyID3
from mutagen import File
from mutagen.flac import FLAC

try:
    from cryptography.hazmat.primitives.asymmetric import padding as crypto_padding
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.backends import default_backend
except ImportError:
    pass

import config
from config import (
    logger, COMMON_HEADERS, MUSIC_LIBRARY_PATH, AUDIO_EXTS,
    QIMEI_PUBLIC_KEY, QIMEI_SECRET, QIMEI_APP_KEY,
)
from models.db import get_db
from services.metadata import (
    fetch_cover_bytes, embed_cover_to_file, save_cover_file,
    embed_lyrics_to_file, get_metadata, get_default_download_dir,
)
from services.scanner import index_single_file
from services.download import sanitize_filename

qqmusic_bp = Blueprint('qqmusic', __name__, url_prefix='')

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _save_qqmusic_credential(credential: dict):
    """保存 QQ 音乐登录凭证到数据库"""
    try:
        with get_db() as conn:
            value = json.dumps(credential) if credential else ''
            conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                        ('qqmusic_credential', value))
            conn.commit()
        logger.info(f"[QQ音乐] 凭证已保存: musicid={credential.get('musicid') if credential else None}")
    except Exception as e:
        logger.warning(f"[QQ音乐] 保存凭证失败: {e}")


def _load_qqmusic_credential():
    """从数据库加载 QQ 音乐登录凭证"""
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM system_settings WHERE key = ?", ('qqmusic_credential',)).fetchone()
            if row and row['value']:
                config.QQMUSIC_CREDENTIAL = json.loads(row['value'])
                logger.info(f"[QQ音乐] 凭证已加载: musicid={config.QQMUSIC_CREDENTIAL.get('musicid')}")
                return config.QQMUSIC_CREDENTIAL
    except Exception as e:
        logger.warning(f"[QQ音乐] 加载凭证失败: {e}")
    return None


def _refresh_qqmusic_credential():
    """刷新 QQ 音乐凭证"""
    if not config.QQMUSIC_CREDENTIAL:
        logger.info("[QQ音乐] 无凭证，跳过刷新")
        return False

    refresh_key = config.QQMUSIC_CREDENTIAL.get('refresh_key')
    refresh_token = config.QQMUSIC_CREDENTIAL.get('refresh_token')
    musickey = config.QQMUSIC_CREDENTIAL.get('musickey')
    musicid = config.QQMUSIC_CREDENTIAL.get('musicid')
    login_type = config.QQMUSIC_CREDENTIAL.get('login_type', 2)

    if not refresh_key or not refresh_token:
        logger.warning("[QQ音乐] 缺少 refresh_key 或 refresh_token，无法刷新")
        return False

    try:
        logger.info(f"[QQ音乐] 开始刷新凭证: musicid={musicid}")
        result = _call_qqmusic_api_direct(
            'music.login.LoginServer',
            'Login',
            {
                'refresh_key': refresh_key,
                'refresh_token': refresh_token,
                'musickey': musickey,
                'musicid': musicid
            },
            extra_common={'tmeLoginType': str(login_type)}
        )

        data = result.get('data', result)
        if data and data.get('musicid'):
            config.QQMUSIC_CREDENTIAL = {
                'musicid': data.get('musicid'),
                'musickey': data.get('musickey'),
                'refresh_key': data.get('refresh_key'),
                'refresh_token': data.get('refresh_token'),
                'login_type': login_type,
                'refreshed_at': time.time()
            }
            _save_qqmusic_credential(config.QQMUSIC_CREDENTIAL)
            logger.info(f"[QQ音乐] 凭证刷新成功: musicid={config.QQMUSIC_CREDENTIAL.get('musicid')}")
            return True
        else:
            logger.warning(f"[QQ音乐] 凭证刷新失败: {result}")
            return False
    except Exception as e:
        logger.warning(f"[QQ音乐] 凭证刷新异常: {e}")
        return False


def _check_qqmusic_credential_expired():
    """检查 QQ 音乐凭证是否过期"""
    if not config.QQMUSIC_CREDENTIAL or not config.QQMUSIC_CREDENTIAL.get('musickey'):
        return True

    try:
        result = _call_qqmusic_api_direct(
            'music.UserInfo.userInfoServer',
            'GetLoginUserInfo',
            {}
        )
        return result.get('code', -1) != 0
    except Exception as e:
        logger.warning(f"[QQ音乐] 检查凭证状态失败: {e}")
        return True


def _start_qqmusic_credential_refresh_task():
    """启动 QQ 音乐凭证定时刷新任务"""
    def refresh_loop():
        while True:
            try:
                time.sleep(6 * 60 * 60)
                if config.QQMUSIC_CREDENTIAL and config.QQMUSIC_CREDENTIAL.get('refresh_key'):
                    logger.info("[QQ音乐] 定时刷新凭证...")
                    _refresh_qqmusic_credential()
            except Exception as e:
                logger.warning(f"[QQ音乐] 定时刷新任务异常: {e}")

    thread = threading.Thread(target=refresh_loop, daemon=True)
    thread.start()
    logger.info("[QQ音乐] 凭证定时刷新任务已启动 (每6小时)")


def _random_imei():
    """生成随机 IMEI 号码"""
    imei = []
    sum_ = 0
    for i in range(14):
        num = random.randint(0, 9)
        if (i + 2) % 2 == 0:
            num *= 2
            if num >= 10:
                num = (num % 10) + 1
        sum_ += num
        imei.append(str(num))
    ctrl_digit = (sum_ * 9) % 10
    imei.append(str(ctrl_digit))
    return "".join(imei)


def _get_qqmusic_device():
    """获取或生成 QQ 音乐设备信息"""
    if config.QQMUSIC_DEVICE:
        return config.QQMUSIC_DEVICE

    # 尝试从数据库加载
    try:
        with get_db() as conn:
            row = conn.execute("SELECT value FROM system_settings WHERE key = ?", ('qqmusic_device',)).fetchone()
            if row and row['value']:
                config.QQMUSIC_DEVICE = json.loads(row['value'])
                return config.QQMUSIC_DEVICE
    except Exception:
        pass

    # 生成新设备信息
    config.QQMUSIC_DEVICE = {
        'display': f"QMAPI.{random.randint(100000, 999999)}.001",
        'product': 'iarim',
        'device': 'sagit',
        'board': 'eomam',
        'model': 'MI 6',
        'fingerprint': f"xiaomi/iarim/sagit:10/eomam.200122.001/{random.randint(1000000, 9999999)}:user/release-keys",
        'boot_id': str(uuid4()),
        'proc_version': f"Linux 5.4.0-54-generic-{''.join(random.choices(string.ascii_letters + string.digits, k=8))} (android-build@google.com)",
        'imei': _random_imei(),
        'brand': 'Xiaomi',
        'android_id': binascii.hexlify(bytes([random.randint(0, 255) for _ in range(8)])).decode('utf-8'),
        'version_release': '10',
        'version_sdk': 29,
        'qimei': None
    }

    # 保存到数据库
    try:
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                        ('qqmusic_device', json.dumps(config.QQMUSIC_DEVICE)))
            conn.commit()
    except Exception:
        pass

    return config.QQMUSIC_DEVICE


def _random_beacon_id():
    """生成随机 BeaconID"""
    beacon_id = ""
    time_month = datetime.now().strftime("%Y-%m-") + "01"
    rand1 = random.randint(100000, 999999)
    rand2 = random.randint(100000000, 999999999)

    for i in range(1, 41):
        if i in [1, 2, 13, 14, 17, 18, 21, 22, 25, 26, 29, 30, 33, 34, 37, 38]:
            beacon_id += f"k{i}:{time_month}{rand1}.{rand2}"
        elif i == 3:
            beacon_id += "k3:0000000000000000"
        elif i == 4:
            beacon_id += f"k4:{''.join(random.choices('123456789abcdef', k=16))}"
        else:
            beacon_id += f"k{i}:{random.randint(0, 9999)}"
        beacon_id += ";"
    return beacon_id


def _calc_md5(*strings):
    """计算 MD5 值"""
    md5 = hashlib.md5()
    for item in strings:
        if isinstance(item, bytes):
            md5.update(item)
        elif isinstance(item, str):
            md5.update(item.encode())
    return md5.hexdigest()


def _get_qqmusic_qimei(version: str = "13.2.5.8"):
    """获取 QIMEI36"""
    device = _get_qqmusic_device()

    # 如果已有缓存的 QIMEI，直接返回
    if device.get('qimei'):
        config.QQMUSIC_QIMEI = device['qimei']
        return config.QQMUSIC_QIMEI

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        def rsa_encrypt(content: bytes) -> bytes:
            key = serialization.load_pem_public_key(QIMEI_PUBLIC_KEY.encode())
            return key.encrypt(content, padding.PKCS1v15())

        def aes_encrypt(key: bytes, content: bytes) -> bytes:
            cipher = Cipher(algorithms.AES(key), modes.CBC(key))
            padding_size = 16 - len(content) % 16
            encryptor = cipher.encryptor()
            return encryptor.update(content + (padding_size * chr(padding_size)).encode()) + encryptor.finalize()

        # 构建 payload
        fixed_rand = random.randint(0, 14400)
        reserved = {
            "harmony": "0",
            "clone": "0",
            "containe": "",
            "oz": "UhYmelwouA+V2nPWbOvLTgN2/m8jwGB+yUB5v9tysQg=",
            "oo": "Xecjt+9S1+f8Pz2VLSxgpw==",
            "kelong": "0",
            "uptimes": (datetime.now() - timedelta(seconds=fixed_rand)).strftime("%Y-%m-%d %H:%M:%S"),
            "multiUser": "0",
            "bod": device['brand'],
            "dv": device['device'],
            "firstLevel": "",
            "manufact": device['brand'],
            "name": device['model'],
            "host": "se.infra",
            "kernel": device['proc_version'],
        }

        payload = {
            "androidId": device['android_id'],
            "platformId": 1,
            "appKey": QIMEI_APP_KEY,
            "appVersion": version,
            "beaconIdSrc": _random_beacon_id(),
            "brand": device['brand'],
            "channelId": "10003505",
            "cid": "",
            "imei": device['imei'],
            "imsi": "",
            "mac": "",
            "model": device['model'],
            "networkType": "unknown",
            "oaid": "",
            "osVersion": f"Android {device['version_release']},level {device['version_sdk']}",
            "qimei": "",
            "qimei36": "",
            "sdkVersion": "1.2.13.6",
            "targetSdkVersion": "33",
            "audit": "",
            "userId": "{}",
            "packageId": "com.tencent.qqmusic",
            "deviceType": "Phone",
            "sdkName": "",
            "reserved": json.dumps(reserved, separators=(',', ':')),
        }

        crypt_key = "".join(random.choices("adbcdef1234567890", k=16))
        nonce = "".join(random.choices("adbcdef1234567890", k=16))
        ts = int(time.time())
        key = base64.b64encode(rsa_encrypt(crypt_key.encode())).decode()
        params = base64.b64encode(aes_encrypt(crypt_key.encode(), json.dumps(payload, separators=(',', ':')).encode())).decode()
        extra = '{"appKey":"' + QIMEI_APP_KEY + '"}'
        sign = _calc_md5(key, params, str(ts * 1000), nonce, QIMEI_SECRET, extra)

        resp = requests.post(
            "https://api.tencentmusic.com/tme/trpc/proxy",
            headers={
                "Host": "api.tencentmusic.com",
                "method": "GetQimei",
                "service": "trpc.tme_datasvr.qimeiproxy.QimeiProxy",
                "appid": "qimei_qq_android",
                "sign": _calc_md5("qimei_qq_androidpzAuCmaFAaFaHrdakPjLIEqKrGnSOOvH", str(ts)),
                "user-agent": "QQMusic",
                "timestamp": str(ts),
            },
            json={
                "app": 0,
                "os": 1,
                "qimeiParams": {
                    "key": key,
                    "params": params,
                    "time": str(ts),
                    "nonce": nonce,
                    "sign": sign,
                    "extra": extra,
                },
            },
            timeout=10,
        )

        resp_data = resp.json()
        data = json.loads(resp_data["data"])["data"]
        config.QQMUSIC_QIMEI = data["q36"]

        # 保存到设备信息
        device['qimei'] = config.QQMUSIC_QIMEI
        config.QQMUSIC_DEVICE = device
        try:
            with get_db() as conn:
                conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                            ('qqmusic_device', json.dumps(device)))
                conn.commit()
        except Exception:
            pass

        logger.info(f"[QQ音乐] 获取 QIMEI 成功: {config.QQMUSIC_QIMEI[:10]}...")
        return config.QQMUSIC_QIMEI

    except Exception as e:
        logger.warning(f"[QQ音乐] 获取 QIMEI 失败: {e}，使用默认值")
        config.QQMUSIC_QIMEI = "6c9d3cd110abca9b16311cee10001e717614"
        return config.QQMUSIC_QIMEI


def _get_qqmusic_guid():
    """获取或生成 QQ 音乐 GUID (现在返回 QIMEI)"""
    return _get_qqmusic_qimei()


def _qqmusic_sign(request_data: dict) -> str:
    """QQ 音乐请求签名 - 完全按照 QQMusicApi 实现"""
    PART_1_INDEXES = [23, 14, 6, 36, 16, 40, 7, 19]
    PART_2_INDEXES = [16, 1, 32, 12, 19, 27, 8, 5]
    SCRAMBLE_VALUES = [89, 39, 179, 150, 218, 82, 58, 252, 177, 52, 186, 123, 120, 64, 242, 133, 143, 161, 121, 179]

    # JavaScript quirks emulation - 过滤超出范围的索引
    part1_indexes = list(filter(lambda x: x < 40, PART_1_INDEXES))

    json_bytes = json.dumps(request_data, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    hash_str = hashlib.sha1(json_bytes).hexdigest().upper()

    part1 = ''.join(hash_str[i] for i in part1_indexes)
    part2 = ''.join(hash_str[i] for i in PART_2_INDEXES)

    part3 = bytearray(20)
    for i, v in enumerate(SCRAMBLE_VALUES):
        value = v ^ int(hash_str[i * 2: i * 2 + 2], 16)
        part3[i] = value

    b64_part = re.sub(rb'[\\/+=]', b'', b64encode(part3)).decode('utf-8')
    return f'zzc{part1}{b64_part}{part2}'.lower()


def _call_qqmusic_api_direct(module: str, method: str, params: dict = None, extra_common: dict = None):
    """直接调用 QQ 音乐 API (内置实现)

    Args:
        module: API 模块名
        method: API 方法名
        params: API 参数
        extra_common: 额外的 common 参数 (如 tmeLoginType)
    """
    guid = _get_qqmusic_guid()

    # 构建请求数据
    common = {
        'ct': '11',
        'cv': '13020508',
        'v': '13020508',
        'tmeAppID': 'qqmusic',
        'format': 'json',
        'inCharset': 'utf-8',
        'outCharset': 'utf-8',
        'QIMEI36': guid,
        'uid': '3931641530',
    }

    # 如果已登录，添加凭证到 common 参数
    cookies = {}
    if config.QQMUSIC_CREDENTIAL and config.QQMUSIC_CREDENTIAL.get('musickey'):
        musicid = str(config.QQMUSIC_CREDENTIAL.get('musicid', ''))
        musickey = config.QQMUSIC_CREDENTIAL.get('musickey', '')
        login_type = str(config.QQMUSIC_CREDENTIAL.get('login_type', 2))
        common.update({
            'qq': musicid,
            'authst': musickey,
            'tmeLoginType': login_type,
        })
        cookies = {
            'uin': musicid,
            'qqmusic_key': musickey,
            'qm_keyst': musickey,
            'tmeLoginType': login_type,
        }

    # 合并额外的 common 参数
    if extra_common:
        common.update(extra_common)

    request_key = f'{module}.{method}'
    request_data = {
        'comm': common,
        request_key: {
            'module': module,
            'method': method,
            'param': params or {}
        }
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 Edg/116.0.1938.54',
        'Referer': 'https://y.qq.com/',
        'Content-Type': 'application/json',
    }

    try:
        url = 'https://u.y.qq.com/cgi-bin/musics.fcg'
        sign_val = _qqmusic_sign(request_data)
        resp = requests.post(url, params={'sign': sign_val}, json=request_data, headers=headers, cookies=cookies, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        result = data.get(request_key, {})
        code = result.get('code', 0)

        # 如果签名失败 (code=2000)，尝试不签名的端点
        if code == 2000:
            logger.info(f"[QQ音乐] 签名端点失败，尝试无签名端点: {module}.{method}")
            url_nosign = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
            resp = requests.post(url_nosign, json=request_data, headers=headers, cookies=cookies, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            result = data.get(request_key, {})
            code = result.get('code', 0)

        if code != 0:
            logger.warning(f"QQ音乐API返回错误: {module}.{method}, code={code}")
        return result
    except Exception as e:
        logger.warning(f"QQ音乐API调用失败: {module}.{method}, 错误: {e}")
        raise


def _qqmusic_hash33(s: str, h: int = 0) -> int:
    """QQ 音乐 hash33 算法"""
    for c in s:
        h = (h << 5) + h + ord(c)
        h = h & 0xFFFFFFFF
    return h


def _get_qq_qrcode():
    """获取 QQ 登录二维码"""
    try:
        resp = requests.get(
            'https://ssl.ptlogin2.qq.com/ptqrshow',
            params={
                'appid': '716027609',
                'e': '2',
                'l': 'M',
                's': '3',
                'd': '72',
                'v': '4',
                't': str(random.random()),
                'daid': '383',
                'pt_3rd_aid': '100497308',
            },
            headers={'Referer': 'https://xui.ptlogin2.qq.com/'},
            timeout=10
        )
        qrsig = resp.cookies.get('qrsig')
        if not qrsig:
            return None, None
        img_b64 = b64encode(resp.content).decode('utf-8')
        return f'data:image/png;base64,{img_b64}', qrsig
    except Exception as e:
        logger.warning(f"获取 QQ 二维码失败: {e}")
        return None, None


def _get_wx_qrcode():
    """获取微信登录二维码"""
    try:
        resp = requests.get(
            'https://open.weixin.qq.com/connect/qrconnect',
            params={
                'appid': 'wx48db31d50e334801',
                'redirect_uri': 'https://y.qq.com/portal/wx_redirect.html?login_type=2&surl=https://y.qq.com/',
                'response_type': 'code',
                'scope': 'snsapi_login',
                'state': 'STATE',
                'href': 'https://y.qq.com/mediastyle/music_v17/src/css/popup_wechat.css#wechat_redirect',
            },
            timeout=10
        )
        match = re.findall(r"uuid=(.+?)\"", resp.text)
        if not match:
            return None, None
        uuid_val = match[0]
        qr_resp = requests.get(
            f'https://open.weixin.qq.com/connect/qrcode/{uuid_val}',
            headers={'Referer': 'https://open.weixin.qq.com/connect/qrconnect'},
            timeout=10
        )
        img_b64 = b64encode(qr_resp.content).decode('utf-8')
        return f'data:image/jpeg;base64,{img_b64}', uuid_val
    except Exception as e:
        logger.warning(f"获取微信二维码失败: {e}")
        return None, None


def _check_qq_qrcode(qrsig: str):
    """检查 QQ 二维码状态

    返回: (status, credential_or_none)
    - status: 'scan', 'conf', 'done', 'timeout', 'refuse', 'error'
    - 当 status='done' 时，第二个参数直接返回 credential（已完成授权）
    """
    # 先检查缓存
    qr_cache = config.QQMUSIC_QR_CACHE.get(qrsig, {})

    # 如果已经授权成功，直接返回凭证
    if qr_cache.get('authorized') and qr_cache.get('credential'):
        logger.info(f"[QQ二维码] 该二维码已授权，返回缓存凭证")
        return 'done', qr_cache.get('credential')

    # 如果已经尝试过授权但失败了，不再重复尝试
    if qr_cache.get('auth_attempted'):
        logger.info(f"[QQ二维码] 该二维码已尝试授权但失败，不再重复")
        return 'error', None

    try:
        ptqrtoken = _qqmusic_hash33(qrsig)
        logger.info(f"[QQ二维码] 检查状态: qrsig长度={len(qrsig)}, ptqrtoken={ptqrtoken}")

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://xui.ptlogin2.qq.com/',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Connection': 'keep-alive',
            'Cookie': f'qrsig={qrsig}'
        }

        resp = requests.get(
            'https://ssl.ptlogin2.qq.com/ptqrlogin',
            params={
                'u1': 'https://graph.qq.com/oauth2.0/login_jump',
                'ptqrtoken': ptqrtoken,
                'ptredirect': '0',
                'h': '1',
                't': '1',
                'g': '1',
                'from_ui': '1',
                'ptlang': '2052',
                'action': f'0-0-{int(time.time() * 1000)}',
                'js_ver': '20102616',
                'js_type': '1',
                'pt_uistyle': '40',
                'aid': '716027609',
                'daid': '383',
                'pt_3rd_aid': '100497308',
                'has_onekey': '1',
            },
            headers=headers,
            timeout=15
        )
        logger.info(f"[QQ二维码] HTTP状态码: {resp.status_code}, 响应长度: {len(resp.text)}")

        if resp.status_code == 403:
            logger.warning("[QQ二维码] 请求被拒绝 (403)，可能是频率限制")
            return 'scan', None

        match = re.search(r"ptuiCB\((.*?)\)", resp.text)
        if not match:
            logger.warning(f"[QQ二维码] 无法解析响应: '{resp.text[:500]}'")
            return 'scan', None
        data = [p.strip("'") for p in match.group(1).split(",")]
        code = int(data[0]) if data[0].isdigit() else -1

        logger.info(f"[QQ二维码] 响应码: {code}, 数据长度: {len(data)}")

        # 状态码: 66=等待扫码, 67=已扫码待确认, 65=超时, 0=成功, 68=拒绝
        if code == 0:
            redirect_url = data[2] if len(data) > 2 else ''
            logger.info(f"[QQ二维码] 登录成功，重定向URL: {redirect_url[:100]}...")
            sigx = re.findall(r"&ptsigx=(.+?)&s_url", redirect_url)
            uin = re.findall(r"&uin=(.+?)&service", redirect_url)

            if sigx and uin:
                logger.info(f"[QQ二维码] 提取成功: uin={uin[0]}, sigx长度={len(sigx[0])}")
                config.QQMUSIC_QR_CACHE[qrsig] = {
                    'auth_attempted': True,
                    'attempted_at': time.time()
                }
                credential = _authorize_qq_login(uin[0], sigx[0])
                if credential:
                    config.QQMUSIC_QR_CACHE[qrsig] = {
                        'authorized': True,
                        'credential': credential,
                        'authorized_at': time.time()
                    }
                    return 'done', credential
                else:
                    logger.warning("[QQ二维码] 授权失败，已标记不再重试")
                    return 'error', None
            logger.warning(f"[QQ二维码] 无法提取 sigx/uin，URL: {redirect_url}")
            return 'error', None
        elif code == 66:
            return 'scan', None
        elif code == 67:
            return 'conf', None
        elif code == 65:
            return 'timeout', None
        elif code == 68:
            return 'refuse', None
        return 'error', None
    except Exception as e:
        logger.warning(f"检查 QQ 二维码状态失败: {e}")
        return 'error', None


def _check_wx_qrcode(uuid: str):
    """检查微信二维码状态

    返回: (status, credential_or_none)
    - status: 'scan', 'conf', 'done', 'timeout', 'refuse', 'error'
    - 当 status='done' 时，第二个参数直接返回 credential（已完成授权）
    """
    # 先检查缓存
    qr_cache = config.QQMUSIC_QR_CACHE.get(uuid, {})

    # 如果已经授权成功，直接返回凭证
    if qr_cache.get('authorized') and qr_cache.get('credential'):
        logger.info(f"[微信二维码] 该二维码已授权，返回缓存凭证")
        return 'done', qr_cache.get('credential')

    # 如果已经尝试过授权但失败了，不再重复尝试
    if qr_cache.get('auth_attempted'):
        logger.info(f"[微信二维码] 该二维码已尝试授权但失败，不再重复")
        return 'error', None

    try:
        resp = requests.get(
            'https://lp.open.weixin.qq.com/connect/l/qrconnect',
            params={'uuid': uuid, '_': str(int(time.time()) * 1000)},
            headers={'Referer': 'https://open.weixin.qq.com/'},
            timeout=30
        )
        match = re.search(r"window\.wx_errcode=(\d+);window\.wx_code=\'([^\']*)\'", resp.text)
        if not match:
            return 'error', None
        wx_errcode = int(match.group(1))
        wx_code = match.group(2)

        # 状态码: 408=等待扫码, 404=已扫码待确认, 405=成功, 403=拒绝
        if wx_errcode == 405:
            config.QQMUSIC_QR_CACHE[uuid] = {
                'auth_attempted': True,
                'attempted_at': time.time()
            }
            logger.info(f"[微信二维码] 扫码成功，开始授权: code={wx_code[:20]}...")
            credential = _authorize_wx_login(wx_code)
            if credential:
                config.QQMUSIC_QR_CACHE[uuid] = {
                    'authorized': True,
                    'credential': credential,
                    'authorized_at': time.time()
                }
                return 'done', credential
            else:
                logger.warning("[微信二维码] 授权失败，已标记不再重试")
                return 'error', None
        elif wx_errcode == 408:
            return 'scan', None
        elif wx_errcode == 404:
            return 'conf', None
        elif wx_errcode == 403:
            return 'refuse', None
        return 'error', None
    except requests.exceptions.Timeout:
        return 'scan', None
    except Exception as e:
        logger.warning(f"检查微信二维码状态失败: {e}")
        return 'error', None


def _authorize_qq_login(uin: str, sigx: str):
    """QQ 登录授权"""
    try:
        logger.info(f"[QQ授权] 开始授权流程: uin={uin}")
        sess = requests.Session()
        # 第一步：check_sig
        resp = sess.get(
            'https://ssl.ptlogin2.graph.qq.com/check_sig',
            params={
                'uin': uin,
                'pttype': '1',
                'service': 'ptqrlogin',
                'nodirect': '0',
                'ptsigx': sigx,
                's_url': 'https://graph.qq.com/oauth2.0/login_jump',
                'ptlang': '2052',
                'ptredirect': '100',
                'aid': '716027609',
                'daid': '383',
                'j_later': '0',
                'low_login_hour': '0',
                'regmaster': '0',
                'pt_login_type': '3',
                'pt_aid': '0',
                'pt_aaid': '16',
                'pt_light': '0',
                'pt_3rd_aid': '100497308',
            },
            headers={'Referer': 'https://xui.ptlogin2.qq.com/'},
            allow_redirects=False,
            timeout=10
        )
        p_skey = resp.cookies.get('p_skey')
        if not p_skey:
            logger.warning(f"[QQ授权] 获取 p_skey 失败, cookies: {dict(resp.cookies)}")
            return None
        logger.info(f"[QQ授权] 获取 p_skey 成功")

        # 第二步：authorize
        resp = sess.post(
            'https://graph.qq.com/oauth2.0/authorize',
            data={
                'response_type': 'code',
                'client_id': '100497308',
                'redirect_uri': 'https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/',
                'scope': 'get_user_info,get_app_friends',
                'state': 'state',
                'switch': '',
                'from_ptlogin': '1',
                'src': '1',
                'update_auth': '1',
                'openapi': '1010_1030',
                'g_tk': _qqmusic_hash33(p_skey, 5381),
                'auth_time': str(int(time.time()) * 1000),
                'ui': str(uuid4()),
            },
            allow_redirects=False,
            timeout=10
        )
        location = resp.headers.get('Location', '')
        logger.info(f"[QQ授权] authorize 响应 Location: {location[:100] if location else 'None'}...")

        error_match = re.search(r'error=(\d+)', location)
        if error_match:
            error_code = error_match.group(1)
            if error_code == '100046':
                logger.warning(f"[QQ授权] QQ OAuth 返回错误 100046: 授权频率过快，请稍后再试")
            else:
                logger.warning(f"[QQ授权] QQ OAuth 返回错误: {error_code}")
            return None

        code_match = re.findall(r"(?<=code=)(.+?)(?=&)", location)
        if not code_match:
            logger.warning(f"[QQ授权] 获取 code 失败, Location: {location}")
            return None
        code = code_match[0]
        logger.info(f"[QQ授权] 获取 code 成功: {code[:20]}...")

        # 第三步：调用 QQ 音乐 API 完成登录
        result = _call_qqmusic_api_direct(
            'QQConnectLogin.LoginServer',
            'QQLogin',
            {'code': code},
            extra_common={'tmeLoginType': '2'}
        )
        logger.info(f"[QQ授权] QQLogin API 返回: {result}")
        data = result.get('data', result)
        if data and data.get('musicid'):
            config.QQMUSIC_CREDENTIAL = {
                'musicid': data.get('musicid'),
                'musickey': data.get('musickey'),
                'musicname': data.get('musicname') or data.get('nick') or f"QQ用户",
                'headurl': data.get('headurl') or data.get('headpic') or '',
                'refresh_key': data.get('refresh_key'),
                'refresh_token': data.get('refresh_token'),
                'login_type': 2  # QQ 登录
            }
            _save_qqmusic_credential(config.QQMUSIC_CREDENTIAL)
            logger.info(f"[QQ授权] 登录成功: musicid={config.QQMUSIC_CREDENTIAL.get('musicid')}, name={config.QQMUSIC_CREDENTIAL.get('musicname')}")
            return config.QQMUSIC_CREDENTIAL
        logger.warning(f"[QQ授权] QQLogin API 返回无效结果: {result}")
        return None
    except Exception as e:
        logger.warning(f"[QQ授权] 授权失败: {e}")
        logger.warning(f"[QQ授权] 堆栈: {traceback.format_exc()}")
        return None


def _authorize_wx_login(code: str):
    """微信登录授权"""
    try:
        result = _call_qqmusic_api_direct(
            'music.login.LoginServer',
            'Login',
            {'code': code, 'strAppid': 'wx48db31d50e334801'},
            extra_common={'tmeLoginType': '1'}
        )
        data = result.get('data', result)
        if data and data.get('musicid'):
            config.QQMUSIC_CREDENTIAL = {
                'musicid': data.get('musicid'),
                'musickey': data.get('musickey'),
                'musicname': data.get('musicname') or data.get('nick') or f"微信用户",
                'headurl': data.get('headurl') or data.get('headpic') or '',
                'refresh_key': data.get('refresh_key'),
                'refresh_token': data.get('refresh_token'),
                'login_type': 1  # 微信登录
            }
            _save_qqmusic_credential(config.QQMUSIC_CREDENTIAL)
            logger.info(f"QQ 音乐微信登录成功: musicid={config.QQMUSIC_CREDENTIAL.get('musicid')}, name={config.QQMUSIC_CREDENTIAL.get('musicname')}")
            return config.QQMUSIC_CREDENTIAL
        logger.warning(f"[微信授权] Login API 返回无效结果: {result}")
        return None
    except Exception as e:
        logger.warning(f"微信登录授权失败: {e}")
        return None


def call_qqmusic_api(category: str, method: str, params: dict = None) -> dict:
    """
    统一的 QQ 音乐 API 调用接口
    将高级 API 调用映射到内置的直接调用实现

    Args:
        category: API 类别 (search, song, lyric, login 等)
        method: 方法名
        params: 参数字典

    Returns:
        统一格式的响应: {'code': 200, 'data': ..., 'message': ...}
    """
    params = params or {}

    try:
        # 搜索相关
        if category == 'search':
            if method == 'search_by_type':
                keyword = params.get('keyword', '')
                num = params.get('num', 20)
                search_type = params.get('search_type', 0)

                searchid = ''.join(random.choices('0123456789', k=18))

                result = _call_qqmusic_api_direct(
                    'music.search.SearchCgiService',
                    'DoSearchForQQMusicMobile',
                    {
                        'searchid': searchid,
                        'query': keyword,
                        'num_per_page': num,
                        'page_num': 1,
                        'search_type': search_type,
                        'highlight': True,
                        'grp': True
                    }
                )

                data = result.get('data', result)
                body = data.get('body', {})
                songs = body.get('item_song', []) or body.get('song', {}).get('list', [])

                return {'code': 200, 'data': songs}

            elif method == 'hotkey':
                result = _call_qqmusic_api_direct(
                    'tencent_musicsoso_hotkey.HotkeyService',
                    'GetHotkeyForQQMusicPC',
                    {}
                )
                data = result.get('data', result)
                hotkeys = data.get('vec_hotkey', [])
                return {'code': 200, 'data': hotkeys}

        # 歌曲相关
        elif category == 'song':
            if method == 'get_detail':
                value = params.get('value', '')
                if isinstance(value, str) and not value.isdigit():
                    result = _call_qqmusic_api_direct(
                        'music.pf_song_detail_svr',
                        'get_song_detail_yqq',
                        {'song_mid': value}
                    )
                else:
                    result = _call_qqmusic_api_direct(
                        'music.pf_song_detail_svr',
                        'get_song_detail_yqq',
                        {'song_id': int(value)}
                    )
                data = result.get('data', result)
                return {'code': 200, 'data': data}

            elif method == 'get_song_urls':
                mid = params.get('mid', '')
                file_type = params.get('file_type', 'MP3_128')

                type_map = {
                    'MP3_128': ('M500', '.mp3'),
                    'MP3_320': ('M800', '.mp3'),
                    'FLAC': ('F000', '.flac'),
                    'OGG_192': ('O600', '.ogg'),
                    'OGG_320': ('O800', '.ogg'),
                    'ACC_192': ('C600', '.m4a'),
                    'ACC_96': ('C400', '.m4a'),
                }

                prefix, ext = type_map.get(file_type, ('M500', '.mp3'))
                filename = f"{prefix}{mid}{mid}{ext}"

                guid = _get_qqmusic_guid()

                api_params = {
                    'filename': [filename],
                    'guid': guid,
                    'songmid': [mid],
                    'songtype': [0],
                }

                is_logged_in = bool(config.QQMUSIC_CREDENTIAL and config.QQMUSIC_CREDENTIAL.get('musickey'))
                logger.info(f"[QQ音乐] 获取歌曲URL: mid={mid}, file_type={file_type}, 已登录={is_logged_in}")
                if is_logged_in:
                    logger.info(f"[QQ音乐] 使用凭证: musicid={config.QQMUSIC_CREDENTIAL.get('musicid')}")

                result = _call_qqmusic_api_direct(
                    'music.vkey.GetVkey',
                    'UrlGetVkey',
                    api_params
                )

                data = result.get('data', result)
                code = result.get('code', 0)
                logger.info(f"[QQ音乐] GetVkey 响应 code={code}")

                midurlinfo = data.get('midurlinfo', [])
                urls = {}
                domain = 'https://isure.stream.qqmusic.qq.com/'
                for info in midurlinfo:
                    song_mid = info.get('songmid', '')
                    song_url = info.get('wifiurl', '') or info.get('purl', '')
                    if song_url:
                        urls[song_mid] = domain + song_url
                        logger.info(f"[QQ音乐] 获取到URL: {song_mid} -> {song_url[:50]}...")
                    else:
                        logger.warning(f"[QQ音乐] 歌曲 {song_mid} 无法获取URL, info={info}")

                return {'code': 200, 'data': urls}

        # 歌词相关
        elif category == 'lyric':
            if method == 'get_lyric':
                value = params.get('value', '')

                if isinstance(value, str) and not value.isdigit():
                    song_mid = value
                    song_id = 0
                else:
                    song_mid = ''
                    song_id = int(value) if value else 0

                result = _call_qqmusic_api_direct(
                    'music.musichallSong.PlayLyricInfo',
                    'GetPlayLyricInfo',
                    {
                        'songMID': song_mid,
                        'songID': song_id
                    }
                )

                lyric_data = {}
                data = result.get('data', result)

                lyric_b64 = data.get('lyric', '')
                if lyric_b64:
                    try:
                        lyric_data['lrc'] = base64.b64decode(lyric_b64).decode('utf-8')
                    except:
                        lyric_data['lrc'] = ''

                trans_b64 = data.get('trans', '')
                if trans_b64:
                    try:
                        lyric_data['trans'] = base64.b64decode(trans_b64).decode('utf-8')
                    except:
                        lyric_data['trans'] = ''

                return {'code': 200, 'data': lyric_data}

        # 登录相关
        elif category == 'login':
            if method == 'get_qrcode':
                login_type = params.get('login_type', 'QQ').upper()
                if login_type == 'WX':
                    img_data, identifier = _get_wx_qrcode()
                else:
                    img_data, identifier = _get_qq_qrcode()

                if img_data and identifier:
                    config.QQMUSIC_QR_CACHE[identifier] = {
                        'type': login_type,
                        'created_at': time.time()
                    }
                    return {
                        'code': 200,
                        'data': {
                            'data': img_data,
                            'identifier': identifier,
                            'qr_type': login_type
                        }
                    }
                return {'code': 500, 'message': '获取二维码失败'}

            elif method == 'check_qrcode':
                identifier = params.get('identifier', '')
                qr_type = params.get('qr_type', 'QQ').upper()

                logger.info(f"[QQ登录] 检查二维码状态: identifier={identifier[:20]}..., qr_type={qr_type}")

                if qr_type == 'WX':
                    status, credential = _check_wx_qrcode(identifier)
                else:
                    status, credential = _check_qq_qrcode(identifier)

                logger.info(f"[QQ登录] 二维码状态: status={status}, has_credential={credential is not None}")

                event_map = {
                    'done': 'DONE',
                    'scan': 'SCAN',
                    'conf': 'CONF',
                    'timeout': 'TIMEOUT',
                    'refuse': 'REFUSE',
                    'error': 'OTHER'
                }

                result = {
                    'code': 200,
                    'data': {
                        'event': event_map.get(status, 'OTHER'),
                        'credential': credential
                    }
                }

                if status == 'done' and not credential:
                    logger.warning("[QQ登录] 状态为 done 但无 credential，授权可能失败")
                    result['data']['event'] = 'OTHER'

                return result

            elif method == 'get_status':
                if config.QQMUSIC_CREDENTIAL and config.QQMUSIC_CREDENTIAL.get('musickey'):
                    return {
                        'code': 200,
                        'data': {
                            'logged_in': True,
                            'musicid': config.QQMUSIC_CREDENTIAL.get('musicid')
                        }
                    }
                return {'code': 200, 'data': {'logged_in': False}}

            elif method == 'logout':
                config.QQMUSIC_CREDENTIAL = None
                return {'code': 200, 'data': {'success': True}}

            elif method == 'send_authcode':
                phone = params.get('phone', '')
                country_code = params.get('country_code', '86')

                if not phone:
                    return {'code': 400, 'message': '缺少手机号'}

                logger.info(f"[QQ手机登录] 发送验证码: phone={phone[:3]}***{phone[-4:]}")

                result = _call_qqmusic_api_direct(
                    'music.login.LoginServer',
                    'SendPhoneAuthCode',
                    {
                        'tmeAppid': 'qqmusic',
                        'phoneNo': str(phone),
                        'areaCode': str(country_code)
                    },
                    extra_common={'tmeLoginMethod': '3'}
                )

                code = result.get('code', -1)
                logger.info(f"[QQ手机登录] API返回: code={code}, result={result}")
                if code == 0:
                    logger.info("[QQ手机登录] 验证码发送成功")
                    return {'code': 200, 'data': {'status': 'sent'}}
                elif code == 20276:
                    data = result.get('data', {})
                    security_url = data.get('securityURL', '') or data.get('security_url', '')
                    logger.warning(f"[QQ手机登录] 需要滑块验证: {security_url}")
                    return {'code': 200, 'data': {'status': 'captcha', 'security_url': security_url}}
                elif code == 100001:
                    logger.warning("[QQ手机登录] 操作过于频繁")
                    return {'code': 200, 'data': {'status': 'frequency'}}
                else:
                    data = result.get('data', {})
                    err_msg = data.get('errMsg', '') or data.get('msg', '') or f'发送失败(code={code})'
                    logger.warning(f"[QQ手机登录] 发送验证码失败: code={code}, err_msg={err_msg}, data={data}")
                    return {'code': 500, 'message': err_msg}

            elif method == 'phone_login':
                phone = params.get('phone', '')
                auth_code = params.get('auth_code', '')
                country_code = params.get('country_code', '86')

                if not phone or not auth_code:
                    return {'code': 400, 'message': '缺少手机号或验证码'}

                logger.info(f"[QQ手机登录] 验证码登录: phone={phone[:3]}***{phone[-4:]}")

                result = _call_qqmusic_api_direct(
                    'music.login.LoginServer',
                    'Login',
                    {
                        'code': str(auth_code),
                        'phoneNo': str(phone),
                        'areaCode': str(country_code),
                        'loginMode': 1
                    },
                    extra_common={'tmeLoginMethod': '3', 'tmeLoginType': '0'}
                )

                code = result.get('code', -1)
                if code == 0:
                    data = result.get('data', result)
                    config.QQMUSIC_CREDENTIAL = {
                        'musicid': data.get('musicid'),
                        'musickey': data.get('musickey'),
                        'musicname': data.get('musicname') or data.get('nick') or '手机用户',
                        'headurl': data.get('headurl') or data.get('headpic') or '',
                        'refresh_key': data.get('refresh_key'),
                        'refresh_token': data.get('refresh_token'),
                        'login_type': 0  # 手机登录
                    }
                    _save_qqmusic_credential(config.QQMUSIC_CREDENTIAL)
                    logger.info(f"[QQ手机登录] 登录成功: musicid={config.QQMUSIC_CREDENTIAL.get('musicid')}")
                    return {'code': 200, 'data': {'status': 'success', 'credential': config.QQMUSIC_CREDENTIAL}}
                elif code == 20274:
                    logger.warning("[QQ手机登录] 设备数量限制")
                    return {'code': 200, 'data': {'status': 'device_limit'}}
                elif code == 20271:
                    logger.warning("[QQ手机登录] 验证码错误或已使用")
                    return {'code': 200, 'data': {'status': 'code_error'}}
                else:
                    logger.warning(f"[QQ手机登录] 登录失败: code={code}")
                    return {'code': 500, 'message': '登录失败'}

        # 歌单相关
        elif category == 'playlist':
            if method == 'get_user_playlists':
                uin = params.get('uin', '')
                if not uin:
                    return {'code': 400, 'message': '缺少用户ID'}

                result = _call_qqmusic_api_direct(
                    'music.srfDissInfo.DissInfo',
                    'CgiGetUserUgc',
                    {
                        'uin': str(uin),
                        'is_self': 1,
                        'start': 0,
                        'num': 100,
                        'type': 0
                    }
                )

                data = result.get('data', result)
                diss_list = data.get('ugclist', [])

                playlists = []
                for diss in diss_list:
                    playlists.append({
                        'tid': diss.get('tid', ''),
                        'diss_name': diss.get('title', ''),
                        'diss_cover': diss.get('cover', ''),
                        'song_cnt': diss.get('song_cnt', 0),
                        'creator': {'nick': diss.get('creator', {}).get('nick', '') if isinstance(diss.get('creator'), dict) else ''}
                    })

                return {'code': 200, 'data': playlists}

            elif method == 'get_playlist_detail':
                playlist_id = params.get('id', '')
                if not playlist_id:
                    return {'code': 400, 'message': '缺少歌单ID'}

                all_songs = []
                song_begin = 0
                page_size = 500
                dissname = ''

                while True:
                    result = _call_qqmusic_api_direct(
                        'music.srfDissInfo.DissInfo',
                        'CgiGetDiss',
                        {
                            'disstid': int(playlist_id),
                            'song_begin': song_begin,
                            'song_num': page_size,
                            'onlysonglist': 0 if song_begin == 0 else 1,
                            'orderlist': 1
                        }
                    )

                    data = result.get('data', result)

                    if song_begin == 0:
                        dirinfo = data.get('dirinfo', {})
                        dissname = dirinfo.get('title', '')

                    songlist = data.get('songlist', [])
                    if not songlist:
                        break

                    all_songs.extend(songlist)

                    if len(songlist) < page_size:
                        break

                    song_begin += page_size

                    if song_begin >= 3000:
                        logger.warning(f'歌单 {playlist_id} 歌曲数量超过3000首，停止获取')
                        break

                logger.info(f'获取歌单 {playlist_id} 完成，共 {len(all_songs)} 首歌曲')

                return {
                    'code': 200,
                    'data': {
                        'dissname': dissname,
                        'songlist': all_songs
                    }
                }

        # 未知的 API
        return {'code': 404, 'message': f'未知的 API: {category}.{method}'}

    except Exception as e:
        logger.warning(f"call_qqmusic_api 调用失败: {category}.{method}, 错误: {e}")
        return {'code': 500, 'message': str(e)}


def _format_qqmusic_songs(songs: list) -> list:
    """格式化 QQ 音乐歌曲列表"""
    result = []
    for item in songs:
        try:
            song_id = item.get('id') or item.get('songid')
            mid = item.get('mid') or item.get('songmid') or ''
            title = item.get('title') or item.get('name') or item.get('songname') or f'未命名 {song_id}'

            singers = item.get('singer') or item.get('singers') or []
            if isinstance(singers, list):
                artist = ' / '.join([s.get('name', '') for s in singers if s.get('name')])
            else:
                artist = str(singers)
            artist = artist or '未知艺术家'

            album_info = item.get('album') or {}
            if isinstance(album_info, dict):
                album = album_info.get('name') or album_info.get('title') or ''
                album_mid = album_info.get('mid') or album_info.get('pmid') or ''
            else:
                album = str(album_info) if album_info else ''
                album_mid = ''

            cover = ''
            if album_mid:
                cover = f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg"
            elif mid:
                cover = f"https://y.qq.com/music/photo_new/T002R300x300M000{mid}.jpg"
            cover = _to_proxy_cover_url(cover)

            duration = item.get('interval') or item.get('duration') or 0

            pay = item.get('pay') or {}
            is_vip = pay.get('pay_play') == 1 if isinstance(pay, dict) else False

            result.append({
                'id': song_id,
                'mid': mid,
                'title': title,
                'artist': artist,
                'album': album,
                'album_mid': album_mid,
                'cover': cover,
                'duration': duration,
                'is_vip': is_vip
            })
        except Exception as e:
            logger.warning(f"格式化QQ音乐歌曲失败: {e}")
            continue
    return result


def _to_proxy_cover_url(raw_url: str) -> str:
    """将外部QQ封面地址转换为同源代理地址，避免浏览器CORS拦截。"""
    if not raw_url:
        return ''
    return f"/api/qqmusic/cover?url={quote(raw_url, safe='')}"


@qqmusic_bp.route('/api/qqmusic/cover')
def qqmusic_cover_proxy():
    """QQ封面同源代理，前端通过该接口加载封面以规避跨域限制。"""
    raw_url = (request.args.get('url') or '').strip()
    if not raw_url:
        return jsonify({'success': False, 'error': 'missing url'}), 400

    parsed = urlparse(raw_url)
    host = (parsed.netloc or '').lower()
    allowed_hosts = {'y.qq.com', 'imgcache.qq.com', 'i.gtimg.cn'}
    if parsed.scheme not in ('http', 'https') or host not in allowed_hosts:
        return jsonify({'success': False, 'error': 'invalid cover host'}), 400

    try:
        resp = requests.get(raw_url, timeout=8, headers=COMMON_HEADERS)
        if resp.status_code != 200 or not resp.content:
            return jsonify({'success': False, 'error': 'cover not found'}), 404
        content_type = resp.headers.get('Content-Type', 'image/jpeg')
        return Response(
            resp.content,
            status=200,
            mimetype=content_type,
            headers={
                'Cache-Control': 'public, max-age=3600',
            },
        )
    except Exception as e:
        logger.warning(f"QQ封面代理失败: {e}")
        return jsonify({'success': False, 'error': 'proxy failed'}), 502


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@qqmusic_bp.route('/api/qqmusic/config', methods=['GET', 'POST'])
def qqmusic_config():
    """获取或更新 QQ 音乐配置 (内置实现，无需外部 API)"""
    if request.method == 'GET':
        default_dir = get_default_download_dir()
        return jsonify({
            'success': True,
            'download_dir': config.QQMUSIC_DOWNLOAD_DIR or default_dir
        })
    try:
        data = request.get_json() or {}
        if 'download_dir' in data:
            config.QQMUSIC_DOWNLOAD_DIR = data['download_dir'].strip() or None
        default_dir = get_default_download_dir()
        return jsonify({
            'success': True,
            'download_dir': config.QQMUSIC_DOWNLOAD_DIR or default_dir
        })
    except Exception as e:
        logger.error(f"保存QQ音乐配置失败: {e}")
        return jsonify({'success': False, 'error': '保存失败'})


@qqmusic_bp.route('/api/qqmusic/search')
def search_qqmusic():
    """搜索 QQ 音乐"""
    keywords = (request.args.get('keywords') or '').strip()
    if not keywords:
        return jsonify({'success': False, 'error': '请输入搜索关键词'})
    num = request.args.get('num', 20)
    try:
        num = max(1, min(int(num), 50))
    except:
        num = 20

    try:
        resp = call_qqmusic_api('search', 'search_by_type', {
            'keyword': keywords,
            'num': num,
            'search_type': 0
        })
        if resp.get('code') == 200:
            songs = resp.get('data') or []
            formatted = _format_qqmusic_songs(songs)
            return jsonify({'success': True, 'data': formatted})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '搜索失败'})
    except Exception as e:
        logger.warning(f"QQ音乐搜索失败: {e}")
        return jsonify({'success': False, 'error': '搜索失败，请检查 QQ 音乐 API 服务'})


@qqmusic_bp.route('/api/qqmusic/song/detail')
def qqmusic_song_detail():
    """获取歌曲详情"""
    mid = request.args.get('mid', '').strip()
    song_id = request.args.get('id', '').strip()
    if not mid and not song_id:
        return jsonify({'success': False, 'error': '请提供歌曲 mid 或 id'})

    try:
        value = mid if mid else song_id
        resp = call_qqmusic_api('song', 'get_detail', {'value': value})
        if resp.get('code') == 200:
            return jsonify({'success': True, 'data': resp.get('data')})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取详情失败'})
    except Exception as e:
        logger.warning(f"获取QQ音乐详情失败: {e}")
        return jsonify({'success': False, 'error': '获取详情失败'})


@qqmusic_bp.route('/api/qqmusic/song/url')
def qqmusic_song_url():
    """获取歌曲播放链接"""
    mid = request.args.get('mid', '').strip()
    if not mid:
        return jsonify({'success': False, 'error': '请提供歌曲 mid'})

    file_type = request.args.get('type', 'MP3_128')

    try:
        resp = call_qqmusic_api('song', 'get_song_urls', {
            'mid': mid,
            'file_type': file_type
        })
        if resp.get('code') == 200:
            urls = resp.get('data') or {}
            return jsonify({'success': True, 'data': urls})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取链接失败'})
    except Exception as e:
        logger.warning(f"获取QQ音乐链接失败: {e}")
        return jsonify({'success': False, 'error': '获取链接失败'})


@qqmusic_bp.route('/api/qqmusic/lyric')
def qqmusic_lyric():
    """获取歌词"""
    mid = request.args.get('mid', '').strip()
    song_id = request.args.get('id', '').strip()
    if not mid and not song_id:
        return jsonify({'success': False, 'error': '请提供歌曲 mid 或 id'})

    try:
        value = mid if mid else int(song_id)
        resp = call_qqmusic_api('lyric', 'get_lyric', {'value': value})
        if resp.get('code') == 200:
            return jsonify({'success': True, 'data': resp.get('data')})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取歌词失败'})
    except Exception as e:
        logger.warning(f"获取QQ音乐歌词失败: {e}")
        return jsonify({'success': False, 'error': '获取歌词失败'})


@qqmusic_bp.route('/api/qqmusic/login/qrcode')
def qqmusic_login_qrcode():
    """获取 QQ 音乐登录二维码"""
    login_type = request.args.get('type', 'qq')
    try:
        resp = call_qqmusic_api('login', 'get_qrcode', {'login_type': login_type.upper()})
        if resp.get('code') == 200:
            data = resp.get('data') or {}
            return jsonify({
                'success': True,
                'qrimg': data.get('data'),
                'identifier': data.get('identifier'),
                'qr_type': data.get('qr_type')
            })
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取二维码失败'})
    except Exception as e:
        logger.warning(f"获取QQ音乐二维码失败: {e}")
        return jsonify({'success': False, 'error': '获取二维码失败'})


@qqmusic_bp.route('/api/qqmusic/login/check')
def qqmusic_login_check():
    """检查 QQ 音乐登录状态"""
    identifier = request.args.get('identifier', '').strip()
    qr_type = request.args.get('qr_type', 'qq')
    if not identifier:
        return jsonify({'success': False, 'error': '缺少 identifier'})

    try:
        resp = call_qqmusic_api('login', 'check_qrcode', {
            'identifier': identifier,
            'qr_type': qr_type.upper()
        })
        if resp.get('code') == 200:
            data = resp.get('data') or {}
            event = data.get('event')
            credential = data.get('credential')

            status_map = {
                'DONE': 'authorized',
                'SCAN': 'waiting',
                'CONF': 'scanned',
                'TIMEOUT': 'expired',
                'REFUSE': 'refused',
                'OTHER': 'error'
            }

            return jsonify({
                'success': True,
                'status': status_map.get(event, 'error' if event == 'OTHER' else 'waiting'),
                'credential': credential
            })
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '检查状态失败'})
    except Exception as e:
        logger.warning(f"检查QQ音乐登录状态失败: {e}")
        return jsonify({'success': False, 'error': '检查状态失败'})


@qqmusic_bp.route('/api/qqmusic/login/status')
def qqmusic_login_status():
    """检查 QQ 音乐 API 连接和登录状态"""
    try:
        connected = True

        logged_in = False
        user_info = None
        if config.QQMUSIC_CREDENTIAL and config.QQMUSIC_CREDENTIAL.get('musickey'):
            logged_in = True
            user_info = {
                'musicid': config.QQMUSIC_CREDENTIAL.get('musicid'),
                'musicname': config.QQMUSIC_CREDENTIAL.get('musicname') or 'QQ用户',
                'headurl': config.QQMUSIC_CREDENTIAL.get('headurl') or '',
                'login_type': 'QQ' if config.QQMUSIC_CREDENTIAL.get('login_type') == 2 else 'WX',
                'is_vip': config.QQMUSIC_CREDENTIAL.get('is_vip', False)
            }

        return jsonify({
            'success': True,
            'connected': connected,
            'logged_in': logged_in,
            'user': user_info
        })
    except Exception as e:
        logger.warning(f"检查QQ音乐API状态失败: {e}")
        return jsonify({'success': False, 'connected': False, 'logged_in': False, 'error': str(e)})


@qqmusic_bp.route('/api/qqmusic/logout', methods=['POST'])
def qqmusic_logout():
    """退出 QQ 音乐登录"""
    config.QQMUSIC_CREDENTIAL = None
    _save_qqmusic_credential(None)
    return jsonify({'success': True, 'message': '已退出登录'})


@qqmusic_bp.route('/api/qqmusic/login/phone/send', methods=['POST'])
def qqmusic_phone_send():
    """发送手机验证码"""
    data = request.get_json() or {}
    phone = data.get('phone', '').strip()
    country_code = data.get('country_code', '86')

    if not phone:
        return jsonify({'success': False, 'error': '请输入手机号'})

    try:
        resp = call_qqmusic_api('login', 'send_authcode', {
            'phone': phone,
            'country_code': country_code
        })
        if resp.get('code') == 200:
            data = resp.get('data', {})
            status = data.get('status')
            if status == 'sent':
                return jsonify({'success': True, 'status': 'sent', 'message': '验证码已发送'})
            elif status == 'captcha':
                return jsonify({
                    'success': True,
                    'status': 'captcha',
                    'security_url': data.get('security_url'),
                    'message': '需要完成滑块验证'
                })
            elif status == 'frequency':
                return jsonify({'success': False, 'status': 'frequency', 'error': '操作过于频繁，请稍后再试'})
            else:
                return jsonify({'success': False, 'error': '发送失败'})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '发送失败'})
    except Exception as e:
        logger.warning(f"发送QQ音乐验证码失败: {e}")
        return jsonify({'success': False, 'error': '发送验证码失败'})


@qqmusic_bp.route('/api/qqmusic/login/phone/verify', methods=['POST'])
def qqmusic_phone_verify():
    """手机验证码登录"""
    data = request.get_json() or {}
    phone = data.get('phone', '').strip()
    auth_code = data.get('auth_code', '').strip()
    country_code = data.get('country_code', '86')

    if not phone or not auth_code:
        return jsonify({'success': False, 'error': '请输入手机号和验证码'})

    try:
        resp = call_qqmusic_api('login', 'phone_login', {
            'phone': phone,
            'auth_code': auth_code,
            'country_code': country_code
        })
        if resp.get('code') == 200:
            data = resp.get('data', {})
            status = data.get('status')
            if status == 'success':
                return jsonify({
                    'success': True,
                    'status': 'success',
                    'credential': data.get('credential'),
                    'message': '登录成功'
                })
            elif status == 'device_limit':
                return jsonify({'success': False, 'status': 'device_limit', 'error': '设备数量已达上限'})
            elif status == 'code_error':
                return jsonify({'success': False, 'status': 'code_error', 'error': '验证码错误或已过期'})
            else:
                return jsonify({'success': False, 'error': '登录失败'})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '登录失败'})
    except Exception as e:
        logger.warning(f"QQ音乐手机登录失败: {e}")
        return jsonify({'success': False, 'error': '登录失败'})


@qqmusic_bp.route('/api/qqmusic/login/cookie', methods=['POST'])
def qqmusic_cookie_login():
    """Cookie 登录 - 用户手动输入 musicid 和 musickey"""
    data = request.get_json() or {}
    musicid = data.get('musicid', '').strip()
    musickey = data.get('musickey', '').strip()

    if not musicid or not musickey:
        return jsonify({'success': False, 'error': '请输入 musicid 和 qqmusic_key'})

    if len(musickey) < 20:
        return jsonify({'success': False, 'error': 'qqmusic_key 格式不正确'})

    try:
        login_type = 2 if musickey.startswith('Q_H_') else 1

        musicname = f'用户{musicid[-4:]}'
        headurl = ''
        encrypt_uin = ''

        config.QQMUSIC_CREDENTIAL = {
            'musicid': musicid,
            'musickey': musickey,
            'musicname': musicname,
            'headurl': headurl,
            'refresh_key': '',
            'refresh_token': '',
            'login_type': login_type,
            'encrypt_uin': ''
        }

        # 方法1: 使用 GetLoginUserInfo API 获取当前登录用户信息
        try:
            result = _call_qqmusic_api_direct(
                'music.UserInfo.userInfoServer',
                'GetLoginUserInfo',
                {}
            )
            logger.info(f"[QQ音乐] GetLoginUserInfo 响应: {str(result)[:800]}")
            user_data = result.get('data', result)
            if user_data:
                nick = user_data.get('nick', '') or user_data.get('nickname', '') or user_data.get('name', '') or user_data.get('musicname', '')
                if nick:
                    musicname = nick
                pic = user_data.get('headpic', '') or user_data.get('headurl', '') or user_data.get('pic', '') or user_data.get('avatar', '') or user_data.get('picurl', '')
                if pic:
                    headurl = pic
                    if headurl and not headurl.startswith('http'):
                        headurl = f'https:{headurl}' if headurl.startswith('//') else f'https://{headurl}'
                encrypt_uin = user_data.get('encryptUin', '') or user_data.get('encrypt_uin', '') or user_data.get('euin', '')
                logger.info(f"[QQ音乐] 方法1(GetLoginUserInfo)获取用户信息: name={musicname}, headurl={headurl[:50] if headurl else ''}, encrypt_uin={encrypt_uin}")
        except Exception as e:
            logger.warning(f"[QQ音乐] 方法1(GetLoginUserInfo)获取用户信息失败: {e}")

        # 方法2: 使用 fcg_get_profile_homepage.fcg 获取用户信息和 encrypt_uin
        if musicname == f'用户{musicid[-4:]}' or not encrypt_uin:
            try:
                profile_url = 'https://c6.y.qq.com/rsc/fcgi-bin/fcg_get_profile_homepage.fcg'
                profile_params = {
                    'ct': 20,
                    'cv': 4747474,
                    'cid': 205360838,
                    'userid': musicid,
                }
                profile_resp = requests.get(profile_url, params=profile_params, timeout=5, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Referer': 'https://y.qq.com/',
                })
                logger.info(f"[QQ音乐] fcg_get_profile_homepage 响应: {profile_resp.status_code}")
                if profile_resp.status_code == 200:
                    profile_data = profile_resp.json()
                    logger.info(f"[QQ音乐] fcg_get_profile_homepage 数据: {str(profile_data)[:800]}")
                    creator = profile_data.get('data', {}).get('creator', {})
                    if creator:
                        if not encrypt_uin:
                            encrypt_uin = creator.get('encrypt_uin', '')
                        if musicname == f'用户{musicid[-4:]}':
                            nick = creator.get('nick', '') or creator.get('hostname', '') or creator.get('name', '')
                            if nick:
                                musicname = nick
                        if not headurl:
                            pic = creator.get('headpic', '') or creator.get('pic', '') or creator.get('avatar', '')
                            if pic:
                                headurl = pic
                                if headurl and not headurl.startswith('http'):
                                    headurl = f'https:{headurl}' if headurl.startswith('//') else f'https://{headurl}'
                        logger.info(f"[QQ音乐] 方法2(fcg_get_profile_homepage)获取用户信息: name={musicname}, headurl={headurl[:50] if headurl else ''}, encrypt_uin={encrypt_uin}")
            except Exception as e:
                logger.warning(f"[QQ音乐] 方法2(fcg_get_profile_homepage)获取用户信息失败: {e}")

        # 方法3: 如果还没有获取到昵称，尝试使用 GetHomepageHeader API (需要 encrypt_uin)
        if musicname == f'用户{musicid[-4:]}' and encrypt_uin:
            try:
                result = _call_qqmusic_api_direct(
                    'music.UnifiedHomepage.UnifiedHomepageSrv',
                    'GetHomepageHeader',
                    {'uin': encrypt_uin, 'IsQueryTabDetail': 1}
                )
                logger.info(f"[QQ音乐] GetHomepageHeader 响应: {str(result)[:500]}")
                resp_data = result.get('data', result)
                if resp_data:
                    user_info = resp_data.get('creator', {}) or resp_data.get('userInfo', {}) or resp_data
                    if user_info:
                        nick = user_info.get('nick', '') or user_info.get('name', '') or user_info.get('nickname', '')
                        if nick:
                            musicname = nick
                        pic = user_info.get('headpic', '') or user_info.get('pic', '') or user_info.get('headurl', '')
                        if pic and not headurl:
                            headurl = pic
                            if headurl and not headurl.startswith('http'):
                                headurl = f'https:{headurl}' if headurl.startswith('//') else f'https://{headurl}'
                        logger.info(f"[QQ音乐] 方法3(GetHomepageHeader)获取用户信息: name={musicname}, headurl={headurl[:50] if headurl else ''}")
            except Exception as e:
                logger.warning(f"[QQ音乐] 方法3(GetHomepageHeader)获取用户信息失败: {e}")

        # 方法4: 尝试使用 VIP 信息 API 验证凭证是否有效并获取用户信息和 VIP 状态
        is_vip = False
        try:
            result = _call_qqmusic_api_direct(
                'VipLogin.VipLoginInter',
                'vip_login_base',
                {}
            )
            logger.info(f"[QQ音乐] vip_login_base 响应: {str(result)[:800]}")
            vip_data = result.get('data', result)
            if vip_data:
                nick = vip_data.get('nick', '') or vip_data.get('name', '') or vip_data.get('nickname', '')
                if nick and musicname == f'用户{musicid[-4:]}':
                    musicname = nick
                pic = vip_data.get('headpic', '') or vip_data.get('headurl', '') or vip_data.get('pic', '')
                if pic and not headurl:
                    headurl = pic
                    if headurl and not headurl.startswith('http'):
                        headurl = f'https:{headurl}' if headurl.startswith('//') else f'https://{headurl}'
                vip_flag = vip_data.get('isvip', 0) or vip_data.get('vipflag', 0) or vip_data.get('vip_flag', 0)
                svip_flag = vip_data.get('svip_flag', 0) or vip_data.get('issvip', 0)
                vip_type = vip_data.get('vipType', 0) or vip_data.get('vip_type', 0)
                is_vip = bool(vip_flag or svip_flag or vip_type)
                logger.info(f"[QQ音乐] 方法4(vip_login_base)获取用户信息: name={musicname}, headurl={headurl[:50] if headurl else ''}, is_vip={is_vip}, vip_flag={vip_flag}, svip_flag={svip_flag}, vip_type={vip_type}")
        except Exception as e:
            logger.warning(f"[QQ音乐] 方法4(vip_login_base)获取VIP信息失败: {e}")

        config.QQMUSIC_CREDENTIAL['musicname'] = musicname
        config.QQMUSIC_CREDENTIAL['headurl'] = headurl
        config.QQMUSIC_CREDENTIAL['encrypt_uin'] = encrypt_uin
        config.QQMUSIC_CREDENTIAL['is_vip'] = is_vip
        _save_qqmusic_credential(config.QQMUSIC_CREDENTIAL)

        logger.info(f"[QQ音乐] Cookie 登录成功: musicid={musicid}, name={musicname}, headurl={headurl[:50] if headurl else 'none'}")
        return jsonify({
            'success': True,
            'credential': config.QQMUSIC_CREDENTIAL,
            'message': '登录成功'
        })
    except Exception as e:
        logger.warning(f"QQ音乐 Cookie 登录失败: {e}")
        logger.warning(f"[QQ音乐] Cookie 登录异常详情: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': '登录失败'})


@qqmusic_bp.route('/api/qqmusic/hotkey')
def qqmusic_hotkey():
    """获取热搜词"""
    try:
        resp = call_qqmusic_api('search', 'hotkey', {})
        if resp.get('code') == 200:
            return jsonify({'success': True, 'data': resp.get('data')})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取热搜失败'})
    except Exception as e:
        logger.warning(f"获取QQ音乐热搜失败: {e}")
        return jsonify({'success': False, 'error': '获取热搜失败'})


@qqmusic_bp.route('/api/qqmusic/download', methods=['POST'])
def download_qqmusic():
    """下载 QQ 音乐"""
    data = request.get_json() or {}
    mid = data.get('mid', '').strip()
    title = data.get('title', '未知歌曲')
    artist = data.get('artist', '未知艺术家')
    cover_url = data.get('cover', '')
    file_type = data.get('file_type', 'MP3_128')
    target_dir = data.get('target_dir', '').strip()

    if not mid:
        return jsonify({'success': False, 'error': '缺少歌曲 mid'})

    task_id = f"qq_{int(time.time() * 1000)}_{mid[:8]}"
    download_dir = target_dir or config.QQMUSIC_DOWNLOAD_DIR or get_default_download_dir()
    os.makedirs(download_dir, exist_ok=True)

    config.QQMUSIC_DOWNLOAD_TASKS[task_id] = {
        'status': 'preparing',
        'progress': 0,
        'message': '准备下载...',
        'filename': f"{artist} - {title}"
    }

    def do_download():
        try:
            config.QQMUSIC_DOWNLOAD_TASKS[task_id]['status'] = 'downloading'
            config.QQMUSIC_DOWNLOAD_TASKS[task_id]['progress'] = 10

            quality_fallback = {
                'FLAC': ['FLAC', 'MP3_320', 'MP3_128'],
                'MP3_320': ['MP3_320', 'MP3_128'],
                'MP3_128': ['MP3_128'],
                'OGG_320': ['OGG_320', 'OGG_192', 'MP3_128'],
                'OGG_192': ['OGG_192', 'MP3_128'],
                'ACC_192': ['ACC_192', 'ACC_96', 'MP3_128'],
                'ACC_96': ['ACC_96', 'MP3_128'],
            }

            qualities_to_try = quality_fallback.get(file_type, [file_type, 'MP3_128'])

            url = None
            actual_quality = file_type

            for try_quality in qualities_to_try:
                logger.info(f"[QQ音乐] 尝试获取 {title} 的 {try_quality} 下载链接...")
                resp = call_qqmusic_api('song', 'get_song_urls', {
                    'mid': mid,
                    'file_type': try_quality
                })

                if resp.get('code') == 200:
                    urls = resp.get('data') or {}
                    url = urls.get(mid)
                    if url:
                        actual_quality = try_quality
                        if try_quality != file_type:
                            logger.info(f"[QQ音乐] {title} 降级到 {try_quality} 成功")
                        break
                    else:
                        logger.warning(f"[QQ音乐] {title} 的 {try_quality} 无法获取URL，尝试降级...")
                else:
                    logger.warning(f"[QQ音乐] {title} 的 {try_quality} 请求失败: {resp.get('message')}")

            if not url:
                raise Exception('无法获取下载链接，所有音质均不可用')

            config.QQMUSIC_DOWNLOAD_TASKS[task_id]['progress'] = 20

            ext_map = {
                'MP3_128': '.mp3',
                'MP3_320': '.mp3',
                'FLAC': '.flac',
                'OGG_192': '.ogg',
                'OGG_320': '.ogg',
                'ACC_192': '.m4a',
                'ACC_96': '.m4a'
            }
            ext = ext_map.get(actual_quality, '.mp3')

            safe_title = re.sub(r'[<>:"/\\|?*]', '', title)
            safe_artist = re.sub(r'[<>:"/\\|?*]', '', artist)
            filename = f"{safe_artist} - {safe_title}{ext}"
            filepath = os.path.join(download_dir, filename)

            dl_headers = dict(COMMON_HEADERS)
            dl_headers['Referer'] = 'https://y.qq.com/'

            with requests.get(url, stream=True, timeout=60, headers=dl_headers) as r:
                r.raise_for_status()
                total = int(r.headers.get('content-length', 0))
                downloaded = 0

                with open(filepath, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                progress = 20 + int(downloaded / total * 70)
                                config.QQMUSIC_DOWNLOAD_TASKS[task_id]['progress'] = min(progress, 90)

            config.QQMUSIC_DOWNLOAD_TASKS[task_id]['progress'] = 95

            base_name = os.path.splitext(filename)[0]

            # 下载封面并嵌入
            if cover_url:
                try:
                    cover_bytes = fetch_cover_bytes(cover_url)
                    if cover_bytes:
                        embed_cover_to_file(filepath, cover_bytes)
                        cover_dir = os.path.join(download_dir, 'covers')
                        os.makedirs(cover_dir, exist_ok=True)
                        cover_path = os.path.join(cover_dir, f"{base_name}.jpg")
                        with open(cover_path, 'wb') as f:
                            f.write(cover_bytes)
                        logger.info(f"封面已保存: {cover_path}")
                except Exception as e:
                    logger.warning(f"嵌入封面失败: {e}")

            # 下载歌词
            try:
                lyric_resp = call_qqmusic_api('lyric', 'get_lyric', {'value': mid})
                if lyric_resp.get('code') == 200:
                    lyric_data = lyric_resp.get('data', {})
                    lrc_text = lyric_data.get('lrc', '')
                    if lrc_text:
                        lyrics_dir = os.path.join(download_dir, 'lyrics')
                        os.makedirs(lyrics_dir, exist_ok=True)
                        lrc_path = os.path.join(lyrics_dir, f"{base_name}.lrc")
                        with open(lrc_path, 'w', encoding='utf-8') as f:
                            f.write(lrc_text)
                        logger.info(f"歌词已保存: {lrc_path}")
                        embed_lyrics_to_file(filepath, lrc_text)
            except Exception as e:
                logger.warning(f"下载歌词失败: {e}")

            # 索引文件
            index_single_file(filepath)

            config.QQMUSIC_DOWNLOAD_TASKS[task_id]['status'] = 'success'
            config.QQMUSIC_DOWNLOAD_TASKS[task_id]['progress'] = 100
            config.QQMUSIC_DOWNLOAD_TASKS[task_id]['message'] = '下载完成'
            config.QQMUSIC_DOWNLOAD_TASKS[task_id]['filename'] = filename

            logger.info(f"QQ音乐下载完成: {filename}")

        except Exception as e:
            logger.error(f"QQ音乐下载失败: {e}")
            config.QQMUSIC_DOWNLOAD_TASKS[task_id]['status'] = 'error'
            config.QQMUSIC_DOWNLOAD_TASKS[task_id]['message'] = str(e)

    threading.Thread(target=do_download, daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id})


@qqmusic_bp.route('/api/qqmusic/task/<task_id>')
def get_qqmusic_task_status(task_id):
    """获取 QQ 音乐下载任务状态"""
    task = config.QQMUSIC_DOWNLOAD_TASKS.get(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'})
    return jsonify({'success': True, 'data': task})


@qqmusic_bp.route('/api/qqmusic/playlist/parse', methods=['POST'])
def parse_qqmusic_playlist():
    """解析 QQ 音乐歌单链接，返回歌曲列表（支持分页获取全部歌曲）"""
    data = request.get_json() or {}
    url = data.get('url', '').strip()

    if not url:
        return jsonify({'success': False, 'error': '请提供歌单链接'})

    try:
        logger.info(f'解析QQ音乐歌单链接: {url}')

        # 如果是短链接，先解析获取真实URL
        if 'fcgi-bin/u' in url or 'c.y.qq.com' in url or 'c6.y.qq.com' in url:
            try:
                logger.info(f'检测到QQ音乐短链接，尝试重定向解析: {url}')
                resp = requests.get(url, allow_redirects=True, timeout=10, headers=COMMON_HEADERS)
                real_url = resp.url
                logger.info(f'短链接重定向到: {real_url}')
                url = real_url
            except Exception as e:
                logger.warning(f'解析QQ音乐短链接失败: {e}')

        id_match = (
            re.search(r'id=(\d+)', url) or
            re.search(r'/playlist/(\d+)', url) or
            re.search(r'/(\d{8,})(?:/|$|\?)', url) or
            re.search(r'disstid[=:](\d+)', url)
        )

        if not id_match:
            return jsonify({'success': False, 'error': '无法从链接中提取歌单ID'})

        playlist_id = id_match.group(1)
        logger.info(f'提取到QQ音乐歌单ID: {playlist_id}')

        if len(playlist_id) < 8:
            try:
                resp = requests.get(url, allow_redirects=True, timeout=10, headers=COMMON_HEADERS)
                real_url = resp.url
                real_id_match = re.search(r'id=(\d+)', real_url) or re.search(r'/playlist/(\d+)', real_url)
                if real_id_match:
                    playlist_id = real_id_match.group(1)
                    logger.info(f'通过重定向获取到真实ID: {playlist_id}')
            except Exception as e:
                logger.warning(f'解析QQ音乐短链接失败: {e}')

        resp = call_qqmusic_api('playlist', 'get_playlist_detail', {'id': playlist_id})

        if resp.get('code') == 200:
            data = resp.get('data', {})
            songs = data.get('songlist', [])
            playlist_name = data.get('dissname', '未知歌单')

            formatted_songs = _format_qqmusic_songs(songs)

            logger.info(f'解析QQ音乐歌单成功: {playlist_name}, 共 {len(formatted_songs)} 首歌曲')

            return jsonify({
                'success': True,
                'playlist_name': playlist_name,
                'playlist_id': playlist_id,
                'creator': '',
                'song_count': len(formatted_songs),
                'songs': formatted_songs
            })
        else:
            return jsonify({'success': False, 'error': resp.get('message') or 'QQ音乐API返回错误'})

    except Exception as e:
        logger.error(f'解析QQ音乐歌单失败: {e}')
        return jsonify({'success': False, 'error': f'解析失败: {str(e)}'})


@qqmusic_bp.route('/api/qqmusic/playlist/user')
def get_user_playlists():
    """获取当前登录用户的歌单列表"""
    if not config.QQMUSIC_CREDENTIAL or not config.QQMUSIC_CREDENTIAL.get('musicid'):
        return jsonify({'success': False, 'error': '请先登录QQ音乐'})

    try:
        musicid = config.QQMUSIC_CREDENTIAL.get('musicid')

        resp = call_qqmusic_api('playlist', 'get_user_playlists', {'uin': musicid})

        if resp.get('code') == 200:
            playlists = resp.get('data', [])
            formatted = []
            for pl in playlists:
                raw_cover = pl.get('diss_cover') or pl.get('cover', '')
                formatted.append({
                    'id': pl.get('tid') or pl.get('id'),
                    'name': pl.get('diss_name') or pl.get('name', ''),
                    'cover': _to_proxy_cover_url(raw_cover) if raw_cover else '',
                    'song_count': pl.get('song_cnt') or pl.get('song_count', 0),
                    'creator': pl.get('creator', {}).get('nick', '') if isinstance(pl.get('creator'), dict) else ''
                })
            return jsonify({'success': True, 'playlists': formatted})
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取歌单失败'})

    except Exception as e:
        logger.error(f'获取用户歌单失败: {e}')
        return jsonify({'success': False, 'error': f'获取失败: {str(e)}'})


@qqmusic_bp.route('/api/qqmusic/playlist/detail/<playlist_id>')
def get_playlist_detail(playlist_id):
    """获取歌单详情（歌曲列表）"""
    try:
        resp = call_qqmusic_api('playlist', 'get_playlist_detail', {'id': playlist_id})

        if resp.get('code') == 200:
            data = resp.get('data', {})
            songs = data.get('songlist', [])

            formatted_songs = _format_qqmusic_songs(songs)

            return jsonify({
                'success': True,
                'playlist_name': data.get('dissname', ''),
                'playlist_id': playlist_id,
                'song_count': len(formatted_songs),
                'songs': formatted_songs
            })
        else:
            return jsonify({'success': False, 'error': resp.get('message') or '获取歌单详情失败'})

    except Exception as e:
        logger.error(f'获取歌单详情失败: {e}')
        return jsonify({'success': False, 'error': f'获取失败: {str(e)}'})
