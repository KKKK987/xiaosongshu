#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared download task helpers for NetEase and QQ Music."""

import re

from config import logger


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', '_', name).strip().strip('.')
    return cleaned or 'song'
