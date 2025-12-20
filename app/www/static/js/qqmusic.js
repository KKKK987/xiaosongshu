import { state } from './state.js';
import { ui } from './ui.js';
import { api } from './api.js';
import { playTrack, loadPlaylists } from './player.js';
import { showToast, formatTime } from './utils.js';

// QQ 音乐业务模块
let songRefreshCallback = null;

// 下载完成后处理歌单状态（后端会自动转换待下载歌曲，这里只是占位）
async function addDownloadedSongToPlaylist(song, failed = false) {
  // 后端在获取歌单歌曲时会自动检测并转换待下载歌曲
  // 这里不需要额外操作，loadPlaylists() 会在下载成功后被调用
  if (!failed) {
    console.log(`[QQ音乐] 歌曲下载完成: ${song.title}`);
  }
}

// 初始化 QQ 音乐登录状态
if (!state.qqmusicLoggedIn) state.qqmusicLoggedIn = false;
if (!state.qqmusicUser) state.qqmusicUser = null;
if (!state.qqmusicLoginPollTimer) state.qqmusicLoginPollTimer = null;

// 更新登录 UI
function updateQQMusicLoginUI() {
  const defaultAvatar = '/static/images/ICON_256.PNG';
  if (state.qqmusicLoggedIn && state.qqmusicUser) {
    // 已登录状态
    if (ui.qqmusicLoginBtnTop) ui.qqmusicLoginBtnTop.classList.add('hidden');
    if (ui.qqmusicUserDisplay) {
      ui.qqmusicUserDisplay.classList.remove('hidden');
      // 显示昵称
      if (ui.qqmusicUserName) {
        ui.qqmusicUserName.textContent = state.qqmusicUser.musicname || state.qqmusicUser.musicid || 'QQ用户';
      }
      // 显示头像 - 使用默认头像作为 fallback
      if (ui.qqmusicUserAvatar) {
        ui.qqmusicUserAvatar.src = state.qqmusicUser.headurl || defaultAvatar;
      }
      // 显示 VIP 状态
      if (ui.qqmusicVipBadge) {
        if (state.qqmusicUser.is_vip) {
          ui.qqmusicVipBadge.classList.remove('hidden');
        } else {
          ui.qqmusicVipBadge.classList.add('hidden');
        }
      }
    }
    if (ui.qqmusicUserMenu) ui.qqmusicUserMenu.classList.add('hidden');
    console.log('[QQ音乐] 登录状态更新:', state.qqmusicUser);
  } else {
    // 未登录状态
    if (ui.qqmusicLoginBtnTop) ui.qqmusicLoginBtnTop.classList.remove('hidden');
    if (ui.qqmusicUserDisplay) ui.qqmusicUserDisplay.classList.add('hidden');
    if (ui.qqmusicUserMenu) ui.qqmusicUserMenu.classList.add('hidden');
    if (ui.qqmusicVipBadge) ui.qqmusicVipBadge.classList.add('hidden');
  }
}

// 开始 QQ 音乐登录
async function startQQMusicLogin() {
  try {
    // 获取二维码
    const res = await api.qqmusic.loginQr('qq');
    if (!res.success || !res.qrimg) {
      showToast(res.error || '获取二维码失败');
      return;
    }

    // 显示二维码弹窗
    if (ui.qqmusicQrImg) ui.qqmusicQrImg.src = res.qrimg;
    if (ui.qqmusicQrHint) ui.qqmusicQrHint.textContent = '使用 QQ 扫码登录';
    if (ui.qqmusicQrModal) ui.qqmusicQrModal.classList.add('active');

    // 开始轮询检查状态
    const identifier = res.identifier;
    const qrType = res.qr_type || 'qq';
    
    if (state.qqmusicLoginPollTimer) {
      clearInterval(state.qqmusicLoginPollTimer);
    }

    state.qqmusicLoginPollTimer = setInterval(async () => {
      try {
        const checkRes = await api.qqmusic.loginCheck(identifier, qrType);
        if (!checkRes.success) return;

        const status = checkRes.status;
        
        if (status === 'scanned') {
          if (ui.qqmusicQrHint) ui.qqmusicQrHint.textContent = '已扫码，请在手机上确认';
        } else if (status === 'authorized') {
          // 登录成功
          clearInterval(state.qqmusicLoginPollTimer);
          state.qqmusicLoginPollTimer = null;
          if (ui.qqmusicQrModal) ui.qqmusicQrModal.classList.remove('active');
          
          state.qqmusicLoggedIn = true;
          state.qqmusicUser = checkRes.credential || { musicid: 'QQ用户' };
          updateQQMusicLoginUI();
          showToast('QQ 音乐登录成功！');
        } else if (status === 'expired') {
          clearInterval(state.qqmusicLoginPollTimer);
          state.qqmusicLoginPollTimer = null;
          if (ui.qqmusicQrHint) ui.qqmusicQrHint.textContent = '二维码已过期，请重新获取';
          showToast('二维码已过期');
        } else if (status === 'refused') {
          clearInterval(state.qqmusicLoginPollTimer);
          state.qqmusicLoginPollTimer = null;
          if (ui.qqmusicQrModal) ui.qqmusicQrModal.classList.remove('active');
          showToast('登录已取消');
        } else if (status === 'error') {
          // 授权失败（可能是频率限制）
          clearInterval(state.qqmusicLoginPollTimer);
          state.qqmusicLoginPollTimer = null;
          if (ui.qqmusicQrHint) ui.qqmusicQrHint.textContent = '授权失败，请稍后重试';
          showToast('QQ 授权失败，可能是操作过于频繁，请等待几分钟后重试');
        }
      } catch (e) {
        console.error('QQ Music login check error:', e);
      }
    }, 2000);

  } catch (e) {
    console.error('QQ Music login error:', e);
    showToast('登录失败');
  }
}

// 切换登录方式
function switchLoginTab(tab) {
  // 停止二维码轮询
  if (state.qqmusicLoginPollTimer) {
    clearInterval(state.qqmusicLoginPollTimer);
    state.qqmusicLoginPollTimer = null;
  }
  
  // 重置所有 tab 和 panel
  if (ui.qqmusicTabQr) ui.qqmusicTabQr.classList.remove('active');
  if (ui.qqmusicTabPhone) ui.qqmusicTabPhone.classList.remove('active');
  if (ui.qqmusicTabCookie) ui.qqmusicTabCookie.classList.remove('active');
  if (ui.qqmusicQrPanel) ui.qqmusicQrPanel.style.display = 'none';
  if (ui.qqmusicPhonePanel) ui.qqmusicPhonePanel.style.display = 'none';
  if (ui.qqmusicCookiePanel) ui.qqmusicCookiePanel.style.display = 'none';
  
  if (tab === 'qr') {
    if (ui.qqmusicTabQr) ui.qqmusicTabQr.classList.add('active');
    if (ui.qqmusicQrPanel) ui.qqmusicQrPanel.style.display = 'block';
    // 切换到二维码时，重新获取二维码
    startQQMusicLogin();
  } else if (tab === 'phone') {
    if (ui.qqmusicTabPhone) ui.qqmusicTabPhone.classList.add('active');
    if (ui.qqmusicPhonePanel) ui.qqmusicPhonePanel.style.display = 'block';
  } else if (tab === 'cookie') {
    if (ui.qqmusicTabCookie) ui.qqmusicTabCookie.classList.add('active');
    if (ui.qqmusicCookiePanel) ui.qqmusicCookiePanel.style.display = 'block';
  }
}

// 发送验证码倒计时
let sendCodeCountdown = 0;
let sendCodeTimer = null;

function updateSendCodeBtn() {
  if (!ui.qqmusicSendCodeBtn) return;
  if (sendCodeCountdown > 0) {
    ui.qqmusicSendCodeBtn.disabled = true;
    ui.qqmusicSendCodeBtn.textContent = `${sendCodeCountdown}s`;
  } else {
    ui.qqmusicSendCodeBtn.disabled = false;
    ui.qqmusicSendCodeBtn.textContent = '发送验证码';
  }
}

function startSendCodeCountdown() {
  sendCodeCountdown = 60;
  updateSendCodeBtn();
  if (sendCodeTimer) clearInterval(sendCodeTimer);
  sendCodeTimer = setInterval(() => {
    sendCodeCountdown--;
    updateSendCodeBtn();
    if (sendCodeCountdown <= 0) {
      clearInterval(sendCodeTimer);
      sendCodeTimer = null;
    }
  }, 1000);
}

// 发送手机验证码
async function sendPhoneCode() {
  const phone = ui.qqmusicPhoneInput?.value?.trim();
  if (!phone) {
    showToast('请输入手机号');
    return;
  }
  if (!/^1\d{10}$/.test(phone)) {
    showToast('请输入正确的手机号');
    return;
  }
  
  if (ui.qqmusicPhoneHint) ui.qqmusicPhoneHint.textContent = '正在发送...';
  
  try {
    const res = await api.qqmusic.loginPhoneSend(phone);
    if (res.success && res.status === 'sent') {
      showToast('验证码已发送');
      if (ui.qqmusicPhoneHint) ui.qqmusicPhoneHint.textContent = '验证码已发送到您的手机';
      startSendCodeCountdown();
    } else if (res.status === 'captcha') {
      if (ui.qqmusicPhoneHint) {
        ui.qqmusicPhoneHint.innerHTML = `需要完成验证：<a href="${res.security_url}" target="_blank" style="color: var(--primary);">点击验证</a>`;
      }
      showToast('请先完成滑块验证');
    } else {
      showToast(res.error || '发送失败');
      if (ui.qqmusicPhoneHint) ui.qqmusicPhoneHint.textContent = res.error || '发送失败';
    }
  } catch (e) {
    console.error('Send code error:', e);
    showToast('发送验证码失败');
    if (ui.qqmusicPhoneHint) ui.qqmusicPhoneHint.textContent = '发送失败';
  }
}

// 手机验证码登录
async function phoneLogin() {
  const phone = ui.qqmusicPhoneInput?.value?.trim();
  const code = ui.qqmusicCodeInput?.value?.trim();
  
  if (!phone || !code) {
    showToast('请输入手机号和验证码');
    return;
  }
  
  if (ui.qqmusicPhoneLoginBtn) {
    ui.qqmusicPhoneLoginBtn.disabled = true;
    ui.qqmusicPhoneLoginBtn.textContent = '登录中...';
  }
  
  try {
    const res = await api.qqmusic.loginPhoneVerify(phone, code);
    if (res.success && res.status === 'success') {
      showToast('登录成功！');
      if (ui.qqmusicQrModal) ui.qqmusicQrModal.classList.remove('active');
      state.qqmusicLoggedIn = true;
      state.qqmusicUser = res.credential || { musicid: '手机用户' };
      updateQQMusicLoginUI();
    } else {
      showToast(res.error || '登录失败');
      if (ui.qqmusicPhoneHint) ui.qqmusicPhoneHint.textContent = res.error || '登录失败';
    }
  } catch (e) {
    console.error('Phone login error:', e);
    showToast('登录失败');
  } finally {
    if (ui.qqmusicPhoneLoginBtn) {
      ui.qqmusicPhoneLoginBtn.disabled = false;
      ui.qqmusicPhoneLoginBtn.textContent = '登录';
    }
  }
}

// Cookie 登录
async function cookieLogin() {
  const musicid = ui.qqmusicCookieMusicid?.value?.trim();
  const musickey = ui.qqmusicCookieMusickey?.value?.trim();
  
  if (!musicid || !musickey) {
    showToast('请输入 musicid 和 qqmusic_key');
    return;
  }
  
  if (ui.qqmusicCookieLoginBtn) {
    ui.qqmusicCookieLoginBtn.disabled = true;
    ui.qqmusicCookieLoginBtn.textContent = '登录中...';
  }
  
  try {
    const res = await api.qqmusic.loginCookie(musicid, musickey);
    console.log('[QQ音乐] Cookie 登录响应:', res);
    if (res.success) {
      showToast('登录成功！');
      if (ui.qqmusicQrModal) ui.qqmusicQrModal.classList.remove('active');
      state.qqmusicLoggedIn = true;
      state.qqmusicUser = res.credential || { musicid: musicid };
      console.log('[QQ音乐] 用户信息:', state.qqmusicUser);
      updateQQMusicLoginUI();
    } else {
      showToast(res.error || '登录失败');
      if (ui.qqmusicCookieHint) ui.qqmusicCookieHint.textContent = res.error || '登录失败';
    }
  } catch (e) {
    console.error('Cookie login error:', e);
    showToast('登录失败');
  } finally {
    if (ui.qqmusicCookieLoginBtn) {
      ui.qqmusicCookieLoginBtn.disabled = false;
      ui.qqmusicCookieLoginBtn.textContent = '登录';
    }
  }
}

// 显示登录弹窗
function showLoginModal() {
  // 清空手机登录表单
  if (ui.qqmusicPhoneInput) ui.qqmusicPhoneInput.value = '';
  if (ui.qqmusicCodeInput) ui.qqmusicCodeInput.value = '';
  if (ui.qqmusicPhoneHint) ui.qqmusicPhoneHint.textContent = '';
  // 清空 Cookie 登录表单
  if (ui.qqmusicCookieMusicid) ui.qqmusicCookieMusicid.value = '';
  if (ui.qqmusicCookieMusickey) ui.qqmusicCookieMusickey.value = '';
  if (ui.qqmusicCookieHint) ui.qqmusicCookieHint.textContent = '';
  // 显示弹窗
  if (ui.qqmusicQrModal) ui.qqmusicQrModal.classList.add('active');
  // 重置为二维码登录（会自动获取二维码）
  switchLoginTab('qr');
}

const normalizeString = (str) => {
  if (!str) return '';
  // 先统一全角字符为半角，并去掉括号前的空格
  let s = str.replace(/（/g, '(').replace(/）/g, ')').replace(/【/g, '[').replace(/】/g, ']');
  s = s.replace(/ \(/g, '(').replace(/ \[/g, '[');  // 去掉括号前的空格
  return s.toLowerCase().normalize('NFKC').replace(/[^\p{L}\p{N}]+/gu, ' ').trim().replace(/\s+/g, ' ');
};

function normalizeArtist(artist) {
  if (!artist) return '';
  // 统一分隔符：/ , 、 & _ 都转为 ,
  let normalized = artist.toLowerCase().normalize('NFKC');
  for (const sep of ['/', '、', '&', '，', '_', ' _ ']) {
    normalized = normalized.split(sep).join(',');
  }
  // 分割、去空格、排序后重新组合
  let parts = normalized.split(',').map(p => p.trim()).filter(p => p);
  // 如果只有一个部分，且包含空格，可能是用空格分隔的多艺术家
  if (parts.length === 1 && parts[0].includes(' ')) {
    const spaceParts = parts[0].split(' ').map(p => p.trim()).filter(p => p);
    // 如果分隔后每个部分都像是一个独立的名字（不是太长），就用空格分隔
    if (spaceParts.length >= 2 && spaceParts.every(p => p.length <= 20)) {
      parts = spaceParts;
    }
  }
  return parts.sort().join(',');
}

function isSameSong(local, song) {
  const lt = normalizeString(local.title);
  const la = normalizeString(local.artist);
  const st = normalizeString(song.title);
  const sa = normalizeString(song.artist);
  
  // 1. 精确匹配标题和艺术家
  if (lt && st && lt === st) {
    if (!la || !sa || normalizeArtist(local.artist) === normalizeArtist(song.artist)) {
      return true;
    }
  }
  
  // 2. 从文件名提取标题和艺术家进行匹配
  const fname = (local.filename || '').replace(/\.[^/.]+$/, '');
  if (fname && fname.includes(' - ')) {
    const parts = fname.split(' - ');
    if (parts.length === 2) {
      const part1 = normalizeString(parts[0]);
      const part2 = normalizeString(parts[1]);
      
      // 格式1: "艺术家 - 标题"
      if (part2 === st) {
        if (!sa || normalizeArtist(parts[0]) === normalizeArtist(song.artist)) {
          return true;
        }
      }
      
      // 格式2: "标题 - 艺术家"
      if (part1 === st) {
        if (!sa || normalizeArtist(parts[1]) === normalizeArtist(song.artist)) {
          return true;
        }
      }
    }
  }
  
  return false;
}

function findLocalSongIndex(song) {
  return state.fullPlaylist.findIndex(local => isSameSong(local, song));
}

async function playDownloadedSong(song) {
  let idx = findLocalSongIndex(song);
  if (idx === -1 && songRefreshCallback) {
    await songRefreshCallback();
    idx = findLocalSongIndex(song);
  }
  if (idx === -1) {
    showToast('未在本地库找到已下载歌曲');
    return;
  }
  state.playQueue = [...state.fullPlaylist];
  await playTrack(idx);
}

function setPlayButton(btnEl, song) {
  if (!btnEl) return;
  btnEl.onclick = null;
  btnEl.disabled = false;
  btnEl.className = 'btn-primary btn-play';
  btnEl.innerHTML = '<i class="fas fa-play"></i> 播放';
  btnEl.onclick = () => playDownloadedSong(song);
}

function renderQQDownloadTasks() {
  const list = ui.qqmusicDownloadList;
  const tasks = state.qqmusicDownloadTasks;
  if (!list) return;
  if (!tasks.length) {
    list.innerHTML = '<div class="loading-text" style="padding: 3rem 0; opacity: 0.6;">暂无下载记录</div>';
    return;
  }

  const orderMap = { downloading: 0, preparing: 1, pending: 2, queued: 3, error: 4, success: 5 };
  const indexed = tasks.map((t, idx) => ({ t, idx }));
  indexed.sort((a, b) => (orderMap[a.t.status] ?? 99) - (orderMap[b.t.status] ?? 99) || a.idx - b.idx);

  list.innerHTML = '';
  const frag = document.createDocumentFragment();
  indexed.forEach(({ t: task }) => {
    const row = document.createElement('div');
    row.className = 'netease-download-row';
    const meta = document.createElement('div');
    meta.className = 'netease-download-meta';
    meta.innerHTML = `<div class="title">${task.title}</div><div class="artist">${task.artist}</div>`;
    const statusEl = document.createElement('div');
    const config = {
      pending: { icon: 'fas fa-clock', text: '等待中', class: 'status-wait' },
      queued: { icon: 'fas fa-clock', text: '等待中', class: 'status-wait' },
      preparing: { icon: 'fas fa-spinner fa-spin', text: '准备中', class: 'status-progress' },
      downloading: { icon: 'fas fa-sync fa-spin', text: '下载中', class: 'status-progress' },
      success: { icon: 'fas fa-check', text: '完成', class: 'status-done' },
      error: { icon: 'fas fa-times', text: '失败', class: 'status-error' }
    }[task.status] || { icon: 'fas fa-question', text: '未知', class: '' };
    statusEl.className = `download-status ${config.class}`;
    if (task.status === 'downloading' || task.status === 'preparing') {
      const p = task.progress || 0;
      statusEl.innerHTML = `<div style="display:flex;flex-direction:column;align-items:flex-end;width:8rem;">
        <div style="font-size:0.75rem;margin-bottom:0.2rem;opacity:0.8;">${task.status === 'preparing' ? '准备中...' : p + '%'}</div>
        <div style="width:100%;height:4px;background:rgba(255,255,255,0.1);border-radius:2px;overflow:hidden;">
          <div style="width:${p}%;height:100%;background:var(--primary);transition:width 0.3s;"></div>
        </div>
      </div>`;
    } else {
      statusEl.innerHTML = `<i class="${config.icon}"></i> <span>${config.text}</span>`;
    }
    row.appendChild(meta);
    row.appendChild(statusEl);
    frag.appendChild(row);
  });
  list.appendChild(frag);
}

function addQQDownloadTask(song, status = 'queued') {
  const task = {
    id: `qq_${Date.now()}_${Math.random().toString(16).slice(2, 6)}`,
    title: song.title || `歌曲 ${song.mid || ''}`,
    artist: song.artist || '',
    songMid: song.mid,
    status
  };
  state.qqmusicDownloadTasks.unshift(task);
  if (state.qqmusicDownloadTasks.length > 30) state.qqmusicDownloadTasks = state.qqmusicDownloadTasks.slice(0, 30);
  renderQQDownloadTasks();
  return task.id;
}

function updateQQDownloadTask(id, status, progress) {
  const task = state.qqmusicDownloadTasks.find(t => t.id === id);
  if (task) {
    task.status = status;
    if (progress !== undefined) task.progress = progress;
    renderQQDownloadTasks();
  }
}

function updateQQSelectAllState() {
  const total = state.qqmusicResults.length;
  const selectedCount = state.qqmusicResults.filter(s => state.qqmusicSelected.has(s.mid)).length;
  if (ui.qqmusicSelectAll) {
    ui.qqmusicSelectAll.indeterminate = selectedCount > 0 && selectedCount < total;
    ui.qqmusicSelectAll.checked = total > 0 && selectedCount === total;
  }
}

function toggleQQBulkActions(visible) {
  if (ui.qqmusicBulkActions) {
    ui.qqmusicBulkActions.classList.toggle('hidden', !visible);
  }
  // 控制"保存为歌单"按钮的显示（仅当解析的是歌单链接时显示）
  const savePlaylistBtn = document.getElementById('qqmusic-save-playlist');
  if (savePlaylistBtn) {
    savePlaylistBtn.classList.toggle('hidden', !visible || !state.qqmusicPlaylistInfo);
  }
}

function renderQQMusicResults() {
  const list = ui.qqmusicResultList;
  if (!list) return;
  const isRecommend = state.qqmusicResultSource === 'recommend';
  if (!state.qqmusicResults.length) {
    list.innerHTML = isRecommend
      ? `<div class="netease-empty-state">
          <div class="empty-title">暂无推荐</div>
          <div class="empty-desc">请检查 QQ 音乐 API 连接</div>
        </div>`
      : `<div class="netease-empty-state">
          <div class="empty-title">等待搜索...</div>
          <div class="empty-desc">请输入关键词开始</div>
        </div>`;
    toggleQQBulkActions(false);
    return;
  }
  list.innerHTML = '';
  const frag = document.createDocumentFragment();

  // 推荐列表标题
  if (isRecommend) {
    const head = document.createElement('div');
    head.className = 'netease-recommend-head';
    head.innerHTML = `
      <div class="netease-recommend-text">
        <div class="recommend-title">热门推荐</div>
      </div>
      <button class="btn-mini"><i class="fas fa-sync-alt"></i> 刷新</button>
    `;
    const btn = head.querySelector('button');
    btn?.addEventListener('click', () => loadQQMusicRecommendations(true));
    frag.appendChild(head);
  }

  if (ui.qqmusicBulkActions) {
    frag.appendChild(ui.qqmusicBulkActions);
  }

  state.qqmusicResults.forEach(song => {
    const card = document.createElement('div');
    card.className = 'netease-card';
    const isVipSong = !!song.is_vip;

    const selectWrap = document.createElement('div');
    selectWrap.className = 'netease-select';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = state.qqmusicSelected.has(song.mid);
    // VIP 歌曲：如果用户已登录则允许选择
    const canSelectVip = state.qqmusicLoggedIn;
    if (isVipSong && !canSelectVip) {
      checkbox.disabled = true;
      state.qqmusicSelected.delete(song.mid);
    }
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.qqmusicSelected.add(song.mid);
      else state.qqmusicSelected.delete(song.mid);
      updateQQSelectAllState();
    });
    selectWrap.appendChild(checkbox);

    const cover = document.createElement('img');
    cover.src = song.cover || '/static/images/ICON_256.PNG';
    cover.loading = 'lazy';

    const meta = document.createElement('div');
    meta.className = 'netease-meta';
    const vipBadge = song.is_vip ? '<span class="netease-vip-badge">VIP</span>' : '';
    meta.innerHTML = `<div class="title">${song.title}${vipBadge}</div>
      <div class="subtitle">${song.artist}</div>
      <div class="extra">${song.album || ''} · ${formatTime(song.duration || 0)}</div>`;

    const actions = document.createElement('div');
    actions.className = 'netease-actions';

    const isDownloaded = state.fullPlaylist && state.fullPlaylist.some(local => isSameSong(local, song));
    
    // VIP 歌曲：如果用户已登录则允许下载，否则显示锁定
    const canDownloadVip = state.qqmusicLoggedIn;

    if (isVipSong && !canDownloadVip) {
      // 未登录时显示 VIP 锁定
      const locked = document.createElement('div');
      locked.className = 'vip-locked';
      locked.innerHTML = '<i class="fas fa-lock"></i> VIP专享';
      actions.appendChild(locked);
    } else {
      const btn = document.createElement('button');
      if (isDownloaded) {
        setPlayButton(btn, song);
      } else {
        btn.className = 'btn-primary';
        btn.innerHTML = '<i class="fas fa-download"></i> 下载';
        btn.onclick = () => downloadQQSong(song, btn);
      }
      actions.appendChild(btn);
    }

    card.appendChild(selectWrap);
    card.appendChild(cover);
    card.appendChild(meta);
    card.appendChild(actions);
    frag.appendChild(card);
  });
  list.appendChild(frag);
  toggleQQBulkActions(true);
  updateQQSelectAllState();
}

function getQQActiveDownloadCount() {
  return state.qqmusicDownloadTasks.filter(t => ['pending', 'preparing', 'downloading'].includes(t.status)).length;
}

function processQQDownloadQueue() {
  const limit = 5;
  let available = limit - getQQActiveDownloadCount();
  while (available > 0 && state.qqmusicPendingQueue.length) {
    const next = state.qqmusicPendingQueue.shift();
    const task = state.qqmusicDownloadTasks.find(t => t.id === next.taskId);
    if (task) task.status = 'pending';
    available--;
    startQQDownload(next);
  }
  renderQQDownloadTasks();
}

async function startQQDownload({ taskId, song, btnEl, fileType }) {
  if (!taskId || !song) return;

  if (btnEl) { btnEl.disabled = true; btnEl.innerHTML = '<i class="fas fa-sync fa-spin"></i> 请求中'; }
  updateQQDownloadTask(taskId, 'preparing');

  if (ui.qqmusicDownloadPanel && ui.qqmusicDownloadPanel.classList.contains('hidden')) {
    ui.qqmusicDownloadPanel.classList.remove('hidden');
  }

  // 根据用户登录状态和设置选择音质
  // VIP 用户使用设置中的音质，普通用户使用 MP3_128
  const quality = fileType || (state.qqmusicLoggedIn ? (state.qqmusicQuality || 'FLAC') : 'MP3_128');

  try {
    const res = await api.qqmusic.download({
      mid: song.mid,
      title: song.title,
      artist: song.artist,
      cover: song.cover,
      file_type: quality,
      target_dir: state.qqmusicDownloadDir || undefined
    });

    if (res.success) {
      const backendTaskId = res.task_id;
      let failCount = 0;
      let pollCount = 0;
      const maxPollCount = 600; // 最多轮询 600 次 (约 5 分钟)

      const pollTimer = setInterval(async () => {
        pollCount++;
        
        // 超时保护
        if (pollCount > maxPollCount) {
          clearInterval(pollTimer);
          updateQQDownloadTask(taskId, 'error');
          console.warn(`下载任务超时: ${song.title}`);
          if (btnEl) { btnEl.disabled = false; btnEl.innerHTML = '<i class="fas fa-download"></i> 下载'; }
          // 超时也要处理歌单状态
          await addDownloadedSongToPlaylist(song, true);
          processQQDownloadQueue();
          return;
        }
        
        try {
          const taskRes = await api.qqmusic.task(backendTaskId);
          if (taskRes.success) {
            failCount = 0;
            const tData = taskRes.data;
            const currentTask = state.qqmusicDownloadTasks.find(t => t.id === taskId);

            if (btnEl) {
              if (tData.status === 'downloading') {
                btnEl.innerHTML = `<i class="fas fa-circle-notch fa-spin"></i> ${tData.progress}%`;
              } else if (tData.status === 'preparing') {
                btnEl.innerHTML = `<i class="fas fa-spinner fa-spin"></i> 准备...`;
              }
            }

            if (currentTask) {
              currentTask.status = tData.status;
              currentTask.progress = tData.progress;
              renderQQDownloadTasks();

              if (tData.status === 'success' || tData.status === 'error') {
                clearInterval(pollTimer);
                if (btnEl) {
                  btnEl.disabled = false;
                  if (tData.status === 'success') {
                    setPlayButton(btnEl, song);
                  } else {
                    btnEl.innerHTML = '<i class="fas fa-redo"></i> 重试';
                    setTimeout(() => { btnEl.innerHTML = '<i class="fas fa-download"></i> 下载'; }, 3000);
                  }
                }
                if (tData.status === 'success') {
                  if (songRefreshCallback) await songRefreshCallback();
                  // 如果有待添加的歌单，将下载的歌曲添加进去
                  await addDownloadedSongToPlaylist(song, false);
                  // 刷新歌单列表以更新待下载状态
                  loadPlaylists();
                } else {
                  console.warn(`下载失败: ${song.title} - ${tData.message || '未知错误'}`);
                  // 下载失败也要处理歌单状态
                  await addDownloadedSongToPlaylist(song, true);
                }
                processQQDownloadQueue();
              }
            } else {
              clearInterval(pollTimer);
              processQQDownloadQueue();
            }
          } else {
            updateQQDownloadTask(taskId, 'error');
            clearInterval(pollTimer);
            console.warn(`任务已失效: ${song.title}`);
            if (btnEl) { btnEl.disabled = false; btnEl.innerHTML = '<i class="fas fa-redo"></i> 重试'; }
            // 任务失效也要处理歌单状态
            await addDownloadedSongToPlaylist(song, true);
            processQQDownloadQueue();
          }
        } catch (e) {
          console.error(e);
          failCount++;
          if (failCount > 10) {
            clearInterval(pollTimer);
            updateQQDownloadTask(taskId, 'error');
            console.warn(`网络连接丢失: ${song.title}`);
            if (btnEl) { btnEl.disabled = false; btnEl.innerHTML = '<i class="fas fa-redo"></i> 重试'; }
            // 网络错误也要处理歌单状态
            await addDownloadedSongToPlaylist(song, true);
            processQQDownloadQueue();
          }
        }
      }, 500); // 增加轮询间隔到 500ms
    } else {
      updateQQDownloadTask(taskId, 'error');
      showToast(res.error || '请求失败');
      if (btnEl) { btnEl.disabled = false; btnEl.innerHTML = '<i class="fas fa-download"></i> 下载'; }
      // 请求失败也要处理歌单状态
      await addDownloadedSongToPlaylist(song, true);
      processQQDownloadQueue();
    }
  } catch (err) {
    console.error('download qq error', err);
    updateQQDownloadTask(taskId, 'error');
    if (btnEl) { btnEl.disabled = false; btnEl.innerHTML = '<i class="fas fa-download"></i> 下载'; }
    // 异常也要处理歌单状态
    await addDownloadedSongToPlaylist(song, true);
    processQQDownloadQueue();
  }
}

// 加载 QQ 音乐推荐歌曲
let qqRecommendLoading = false;
async function loadQQMusicRecommendations(forceReload = false) {
  if (qqRecommendLoading) return;
  
  // 如果已有缓存且不强制刷新，直接使用缓存
  if (!forceReload && state.qqmusicRecommendations.length) {
    state.qqmusicResults = state.qqmusicRecommendations;
    state.qqmusicResultSource = 'recommend';
    state.qqmusicSelected = new Set();
    renderQQMusicResults();
    return;
  }

  qqRecommendLoading = true;
  if (ui.qqmusicResultList && (state.qqmusicResultSource === 'recommend' || !state.qqmusicResults.length)) {
    ui.qqmusicResultList.innerHTML = `
      <div class="netease-empty-state" style="opacity:0.8; padding: 2rem;">
        <div class="loading-spinner" style="width:2rem;height:2rem;margin-bottom:1rem;"></div>
        <div class="loading-text">正在加载推荐...</div>
      </div>`;
  }

  try {
    // 获取热搜词
    const hotkeyRes = await api.qqmusic.hotkey();
    if (hotkeyRes.success && hotkeyRes.data && hotkeyRes.data.length > 0) {
      // 使用第一个热搜词搜索歌曲
      const firstHotkey = hotkeyRes.data[0].k || hotkeyRes.data[0].title || hotkeyRes.data[0];
      const searchRes = await api.qqmusic.search(firstHotkey, 30);
      
      if (searchRes.success) {
        state.qqmusicRecommendations = searchRes.songs || searchRes.data || [];
        state.qqmusicResults = state.qqmusicRecommendations;
        state.qqmusicResultSource = 'recommend';
        state.qqmusicSelected = new Set();
        renderQQMusicResults();
      } else {
        throw new Error(searchRes.error || '搜索失败');
      }
    } else {
      // 如果获取热搜失败，使用默认关键词
      const searchRes = await api.qqmusic.search('热门歌曲', 30);
      if (searchRes.success) {
        state.qqmusicRecommendations = searchRes.songs || searchRes.data || [];
        state.qqmusicResults = state.qqmusicRecommendations;
        state.qqmusicResultSource = 'recommend';
        state.qqmusicSelected = new Set();
        renderQQMusicResults();
      } else {
        if (ui.qqmusicResultList) {
          ui.qqmusicResultList.innerHTML = `
            <div class="netease-empty-state">
              <div class="empty-title">加载推荐失败</div>
              <div class="empty-desc">请检查 QQ 音乐 API 连接</div>
            </div>`;
        }
      }
    }
  } catch (err) {
    console.error('load QQ recommend failed', err);
    if (ui.qqmusicResultList && (state.qqmusicResultSource === 'recommend' || !state.qqmusicResults.length)) {
      ui.qqmusicResultList.innerHTML = `
        <div class="netease-empty-state">
          <div class="empty-title">加载推荐失败</div>
          <div class="empty-desc">请检查 QQ 音乐 API 连接</div>
        </div>`;
    }
    toggleQQBulkActions(false);
  } finally {
    qqRecommendLoading = false;
  }
}

async function downloadQQSong(song, btnEl) {
  if (!song || !song.mid) return;
  // VIP 歌曲需要登录才能下载
  if (song.is_vip && !state.qqmusicLoggedIn) {
    showToast('VIP 歌曲需要登录后才能下载');
    return;
  }

  const existingTask = state.qqmusicDownloadTasks.find(t => t.songMid === song.mid
    && ['preparing', 'downloading', 'pending', 'queued'].includes(t.status));
  if (existingTask) { showToast('该任务正在进行中'); return; }

  const limit = 5;
  const active = getQQActiveDownloadCount();

  if (active < limit) {
    const taskId = addQQDownloadTask(song, 'pending');
    if (btnEl) { btnEl.disabled = true; btnEl.innerHTML = '<i class="fas fa-sync fa-spin"></i> 请求中'; }
    startQQDownload({ taskId, song, btnEl });
  } else {
    const taskId = addQQDownloadTask(song, 'queued');
    if (btnEl) { btnEl.disabled = true; btnEl.innerHTML = '<i class="fas fa-clock"></i> 排队中'; }
    state.qqmusicPendingQueue.push({ taskId, song, btnEl });
  }
}

async function searchQQMusic() {
  if (!ui.qqmusicKeywordsInput) return;
  const inputVal = ui.qqmusicKeywordsInput.value.trim();
  if (!inputVal) { showToast('请输入关键词或歌单链接'); return; }

  // 检测是否是歌单链接
  const isPlaylistUrl = inputVal.includes('y.qq.com') || inputVal.includes('c.y.qq.com') || inputVal.includes('i.y.qq.com');
  
  if (ui.qqmusicResultList) {
    ui.qqmusicResultList.innerHTML = `<div class="netease-empty-state" style="opacity:0.8; padding: 2rem;">
      <div class="loading-spinner" style="width:2rem;height:2rem;margin-bottom:1rem;"></div>
      <div class="loading-text">${isPlaylistUrl ? '正在解析歌单...' : '正在搜索...'}</div>
    </div>`;
  }
  toggleQQBulkActions(false);

  try {
    let json;
    if (isPlaylistUrl) {
      // 解析歌单链接
      const res = await fetch('/api/qqmusic/playlist/parse', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: inputVal })
      });
      json = await res.json();
    } else {
      // 普通搜索
      json = await api.qqmusic.search(inputVal);
    }
    
    if (json.success) {
      state.qqmusicResults = json.songs || json.data || [];
      state.qqmusicResultSource = 'search';
      state.qqmusicSelected = new Set();
      
      // 保存歌单信息用于后续创建本地歌单
      if (isPlaylistUrl && json.playlist_name) {
        state.qqmusicPlaylistInfo = {
          name: json.playlist_name || 'QQ音乐歌单',
          url: inputVal,
          type: 'qq'
        };
      } else {
        state.qqmusicPlaylistInfo = null;
      }
      
      renderQQMusicResults();
      
      if (isPlaylistUrl && json.playlist_name) {
        showToast(`已解析歌单：${json.playlist_name}（${state.qqmusicResults.length} 首）`);
      }
    } else {
      ui.qqmusicResultList.innerHTML = `<div class="loading-text">${json.error || '搜索失败'}</div>`;
      toggleQQBulkActions(false);
    }
  } catch (err) {
    console.error('QQ Music search failed', err);
    if (ui.qqmusicResultList) ui.qqmusicResultList.innerHTML = '<div class="loading-text">搜索失败，请检查 API 服务</div>';
    toggleQQBulkActions(false);
  }
}

async function bulkDownloadQQSelected() {
  // 已登录用户可以下载 VIP 歌曲
  const canDownloadVip = state.qqmusicLoggedIn;
  const targets = state.qqmusicResults.filter(s => state.qqmusicSelected.has(s.mid) && (canDownloadVip || !s.is_vip));
  if (!targets.length) { showToast('请先选择歌曲'); return; }

  for (const song of targets) {
    const existingTask = state.qqmusicDownloadTasks.find(t => t.songMid === song.mid
      && ['preparing', 'downloading', 'pending', 'queued'].includes(t.status));
    if (existingTask) continue;

    const limit = 5;
    const active = getQQActiveDownloadCount();
    if (active < limit) {
      const taskId = addQQDownloadTask(song, 'pending');
      startQQDownload({ taskId, song, btnEl: null });
    } else {
      const taskId = addQQDownloadTask(song, 'queued');
      state.qqmusicPendingQueue.push({ taskId, song, btnEl: null });
    }
  }
  showToast(`已添加 ${targets.length} 首歌曲到下载队列`);
  state.qqmusicSelected.clear();
  renderQQMusicResults();
}

async function loadQQMusicConfig() {
  // 内置实现，无需外部 API 服务
  try {
    const json = await api.qqmusic.configGet();
    if (json.success) {
      state.qqmusicDownloadDir = json.download_dir || '';
      if (ui.qqmusicDownloadDirInput) ui.qqmusicDownloadDirInput.value = state.qqmusicDownloadDir;
    }
  } catch (err) {
    console.warn('QQ Music config load failed:', err);
  }

  // 加载本地保存的音质设置
  const savedQuality = localStorage.getItem('xiaosongshu_qqmusic_quality');
  if (savedQuality) {
    state.qqmusicQuality = savedQuality;
    if (ui.qqmusicQualitySelect) ui.qqmusicQualitySelect.value = savedQuality;
  }

  // 内置实现始终可用，直接显示内容
  toggleQQMusicGate(true);

  // 检查登录状态
  try {
    const statusJson = await api.qqmusic.loginStatus();
    if (statusJson.success && statusJson.logged_in && statusJson.user) {
      state.qqmusicLoggedIn = true;
      state.qqmusicUser = statusJson.user;
      updateQQMusicLoginUI();
    }
  } catch (e) {
    console.warn('QQ Music login status check failed:', e);
  }
}

async function saveQQMusicConfig() {
  const dir = ui.qqmusicDownloadDirInput ? ui.qqmusicDownloadDirInput.value.trim() : '';
  if (!dir) { showToast('请输入下载目录'); return; }
  
  // 保存音质设置到本地
  const quality = ui.qqmusicQualitySelect ? ui.qqmusicQualitySelect.value : 'FLAC';
  state.qqmusicQuality = quality;
  localStorage.setItem('xiaosongshu_qqmusic_quality', quality);
  
  try {
    const json = await api.qqmusic.configSave({ download_dir: dir });
    if (json.success) {
      state.qqmusicDownloadDir = json.download_dir;
      showToast('保存成功');
    } else {
      showToast(json.error || '保存失败');
    }
  } catch (err) {
    console.error('save qq config error', err);
    showToast('保存失败');
  }
}

function toggleQQMusicGate(connected) {
  if (ui.qqmusicConfigGate) {
    ui.qqmusicConfigGate.classList.toggle('hidden', connected);
  }
  if (ui.qqmusicContent) {
    ui.qqmusicContent.classList.toggle('hidden', !connected);
  }
}

// 保存为本地歌单（关联源链接，支持同步）并自动下载
async function saveQQAsPlaylist() {
  if (!state.qqmusicPlaylistInfo) {
    showToast('当前不是歌单，无法保存');
    return;
  }
  
  if (!state.qqmusicResults || state.qqmusicResults.length === 0) {
    showToast('歌单为空');
    return;
  }
  
  const playlistInfo = state.qqmusicPlaylistInfo;
  const playlistName = playlistInfo.name || 'QQ音乐歌单';
  
  // 找出本地没有的歌曲
  const missingSongs = state.qqmusicResults.filter(song => 
    !state.fullPlaylist || !state.fullPlaylist.some(local => isSameSong(local, song))
  );
  
  // 准备待下载歌曲列表（包含所有歌曲，后端会自动匹配本地已有的）
  const pendingSongs = state.qqmusicResults.map((song, idx) => ({
    qq_mid: song.mid,
    title: song.title || song.name || '未知歌曲',
    artist: song.artist || (song.singer && song.singer.map(s => s.name).join(', ')) || '',
    album: song.album || (song.albumInfo && song.albumInfo.name) || '',
    cover: song.cover || (song.albumInfo && song.albumInfo.mid ? `https://y.qq.com/music/photo_new/T002R300x300M000${song.albumInfo.mid}.jpg` : ''),
    source: 'qq',
    sort_order: idx
  }));
  
  try {
    const res = await api.playlists.createWithSource(
      playlistName,
      pendingSongs,
      playlistInfo.url,
      playlistInfo.type
    );
    
    if (res.success) {
      // 刷新歌单列表
      loadPlaylists();
      
      // 自动开始下载本地没有的歌曲
      if (missingSongs.length > 0) {
        showToast(`已保存歌单"${playlistName}"，开始下载 ${missingSongs.length} 首歌曲...`);
        startBulkDownload(missingSongs);
      } else {
        showToast(`已保存歌单"${playlistName}"（所有歌曲本地已有）`);
      }
    } else {
      showToast(res.error || '保存失败');
    }
  } catch (err) {
    console.error('Save playlist error:', err);
    showToast('保存歌单失败');
  }
}

// 批量下载歌曲
function startBulkDownload(songs) {
  if (!songs || songs.length === 0) return;
  
  // 已登录用户可以下载 VIP 歌曲
  const canDownloadVip = state.qqmusicLoggedIn;
  const targets = songs.filter(s => canDownloadVip || !s.is_vip);
  
  if (targets.length === 0) {
    showToast('所有歌曲都是 VIP 专享，请先登录');
    return;
  }
  
  // 添加到下载队列
  for (const song of targets) {
    const existingTask = state.qqmusicDownloadTasks.find(t => t.songMid === song.mid
      && ['preparing', 'downloading', 'pending', 'queued'].includes(t.status));
    if (existingTask) continue;

    const taskId = addQQDownloadTask(song, 'queued');
    state.qqmusicPendingQueue.push({ taskId, song, btnEl: null });
  }
  
  // 处理队列
  processQQDownloadQueue();
  
  // 打开下载面板
  if (ui.qqmusicDownloadPanel) {
    ui.qqmusicDownloadPanel.classList.remove('hidden');
  }
}

export function initQQMusic(refreshCallback) {
  songRefreshCallback = refreshCallback;

  // 搜索输入框回车事件
  if (ui.qqmusicKeywordsInput) {
    ui.qqmusicKeywordsInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') searchQQMusic();
    });
  }

  // 全选
  if (ui.qqmusicSelectAll) {
    ui.qqmusicSelectAll.addEventListener('change', () => {
      if (ui.qqmusicSelectAll.checked) {
        state.qqmusicResults.filter(s => !s.is_vip).forEach(s => state.qqmusicSelected.add(s.mid));
      } else {
        state.qqmusicSelected.clear();
      }
      renderQQMusicResults();
    });
  }

  // 批量下载
  if (ui.qqmusicBulkDownload) {
    ui.qqmusicBulkDownload.addEventListener('click', bulkDownloadQQSelected);
  }
  
  // 保存为歌单按钮
  const savePlaylistBtn = document.getElementById('qqmusic-save-playlist');
  if (savePlaylistBtn) {
    savePlaylistBtn.addEventListener('click', saveQQAsPlaylist);
  }

  // 下载面板切换
  if (ui.qqmusicDownloadFloating) {
    ui.qqmusicDownloadFloating.addEventListener('click', () => {
      if (ui.qqmusicDownloadPanel) ui.qqmusicDownloadPanel.classList.toggle('hidden');
    });
  }
  if (ui.qqmusicDownloadToggle) {
    ui.qqmusicDownloadToggle.addEventListener('click', () => {
      if (ui.qqmusicDownloadPanel) ui.qqmusicDownloadPanel.classList.add('hidden');
    });
  }

  // 设置保存
  if (ui.qqmusicSaveSettingsBtn) {
    ui.qqmusicSaveSettingsBtn.addEventListener('click', saveQQMusicConfig);
  }

  // 断开连接
  if (ui.qqmusicDisconnectBtn) {
    ui.qqmusicDisconnectBtn.addEventListener('click', async () => {
      state.qqmusicApiBase = '';
      await api.qqmusic.configSave({ api_base: '' });
      toggleQQMusicGate(false);
      showToast('已断开 QQ 音乐 API 连接');
    });
  }

  // 登录按钮
  if (ui.qqmusicLoginBtnTop) {
    ui.qqmusicLoginBtnTop.addEventListener('click', showLoginModal);
  }

  // 登录方式切换
  if (ui.qqmusicTabQr) {
    ui.qqmusicTabQr.addEventListener('click', () => switchLoginTab('qr'));
  }
  if (ui.qqmusicTabPhone) {
    ui.qqmusicTabPhone.addEventListener('click', () => switchLoginTab('phone'));
  }
  if (ui.qqmusicTabCookie) {
    ui.qqmusicTabCookie.addEventListener('click', () => switchLoginTab('cookie'));
  }

  // Cookie 登录
  if (ui.qqmusicCookieLoginBtn) {
    ui.qqmusicCookieLoginBtn.addEventListener('click', cookieLogin);
  }

  // 发送验证码
  if (ui.qqmusicSendCodeBtn) {
    ui.qqmusicSendCodeBtn.addEventListener('click', sendPhoneCode);
  }

  // 手机登录
  if (ui.qqmusicPhoneLoginBtn) {
    ui.qqmusicPhoneLoginBtn.addEventListener('click', phoneLogin);
  }

  // 验证码输入框回车登录
  if (ui.qqmusicCodeInput) {
    ui.qqmusicCodeInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') phoneLogin();
    });
  }

  // 关闭二维码弹窗
  if (ui.closeQQMusicQrModal) {
    ui.closeQQMusicQrModal.addEventListener('click', () => {
      if (ui.qqmusicQrModal) ui.qqmusicQrModal.classList.remove('active');
      if (state.qqmusicLoginPollTimer) {
        clearInterval(state.qqmusicLoginPollTimer);
        state.qqmusicLoginPollTimer = null;
      }
    });
  }

  // 用户菜单点击
  if (ui.qqmusicUserDisplay) {
    ui.qqmusicUserDisplay.addEventListener('click', () => {
      if (ui.qqmusicUserMenu) ui.qqmusicUserMenu.classList.toggle('hidden');
    });
  }

  // 退出登录
  if (ui.qqmusicMenuLogout) {
    ui.qqmusicMenuLogout.addEventListener('click', async () => {
      try {
        await fetch('/api/qqmusic/logout', { method: 'POST' });
        state.qqmusicLoggedIn = false;
        state.qqmusicUser = null;
        updateQQMusicLoginUI();
        showToast('已退出 QQ 音乐登录');
      } catch (e) {
        showToast('退出失败');
      }
    });
  }

  // 加载配置
  loadQQMusicConfig();
  
  // 加载推荐歌曲
  loadQQMusicRecommendations();
}

export { searchQQMusic, renderQQMusicResults, downloadQQSong, loadQQMusicRecommendations };
