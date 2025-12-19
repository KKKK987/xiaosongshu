// 管理员功能模块
import { api } from './api.js';
import { showToast } from './utils.js';
import { switchTab } from './player.js';

// ============================================
// 管理员状态
// ============================================
export const adminState = {
  users: [],
  selectedUserId: null,
  userHistory: [],
  statsOverview: null,
  topSongs: [],
  activeUsers: [],
  isLoading: false,
  currentPeriod: 'week'
};

// ============================================
// 管理员 UI 渲染函数
// ============================================

// 渲染用户列表
export function renderUserList(users, container) {
  if (!container) return;
  
  if (!users || users.length === 0) {
    container.innerHTML = '<div class="loading-text" style="padding: 2rem 0; opacity: 0.6;">暂无用户数据</div>';
    return;
  }
  
  container.innerHTML = '';
  const table = document.createElement('table');
  table.className = 'admin-table';
  table.innerHTML = `
    <thead>
      <tr>
        <th>用户ID</th>
        <th>用户名</th>
        <th>角色</th>
        <th>播放次数</th>
        <th>最后活跃</th>
        <th>操作</th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  
  const tbody = table.querySelector('tbody');
  users.forEach(user => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${user.id}</td>
      <td>${user.username || '-'}</td>
      <td><span class="role-badge ${user.is_admin ? 'admin' : 'user'}">${user.is_admin ? '管理员' : '普通用户'}</span></td>
      <td>${user.play_count || 0}</td>
      <td>${user.last_active ? formatDateTime(user.last_active) : '-'}</td>
      <td>
        <button class="btn-mini btn-view" data-user-id="${user.id}">查看详情</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
  
  container.appendChild(table);
  
  // 绑定查看详情按钮事件
  container.querySelectorAll('.btn-view').forEach(btn => {
    btn.addEventListener('click', () => {
      const userId = btn.dataset.userId;
      showUserDetail(userId);
    });
  });
}

// 渲染用户听歌历史
export function renderUserHistory(history, container) {
  if (!container) return;
  
  if (!history || history.length === 0) {
    container.innerHTML = '<div class="loading-text" style="padding: 2rem 0; opacity: 0.6;">暂无播放记录</div>';
    return;
  }
  
  container.innerHTML = '';
  const list = document.createElement('div');
  list.className = 'history-list';
  
  history.forEach(record => {
    const item = document.createElement('div');
    item.className = 'history-item';
    item.innerHTML = `
      <img src="${record.cover || '/static/images/ICON_256.PNG'}" alt="cover" class="history-cover">
      <div class="history-info">
        <div class="history-title">${record.title || '未知歌曲'}</div>
        <div class="history-artist">${record.artist || '未知艺术家'}</div>
      </div>
      <div class="history-time">${formatDateTime(record.played_at)}</div>
      <div class="history-duration">${formatDuration(record.duration)}</div>
    `;
    list.appendChild(item);
  });
  
  container.appendChild(list);
}

// 渲染统计概览
export function renderStatsOverview(stats, container) {
  if (!container) return;
  
  if (!stats) {
    container.innerHTML = '<div class="loading-text">加载中...</div>';
    return;
  }
  
  // 直接渲染stat-card，container本身已经是stats-grid
  container.innerHTML = `
    <div class="stat-card">
      <div class="stat-value">${stats.total_users || 0}</div>
      <div class="stat-label">总用户数</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">${stats.active_users_today || 0}</div>
      <div class="stat-label">今日活跃</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">${stats.total_plays || 0}</div>
      <div class="stat-label">总播放次数</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">${stats.plays_today || 0}</div>
      <div class="stat-label">今日播放</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">${stats.total_songs || 0}</div>
      <div class="stat-label">歌曲总数</div>
    </div>
    <div class="stat-card">
      <div class="stat-value">${formatDurationLong(stats.total_duration || 0)}</div>
      <div class="stat-label">总播放时长</div>
    </div>
  `;
}

// 渲染热门歌曲排行
export function renderTopSongs(songs, container) {
  if (!container) return;
  
  if (!songs || songs.length === 0) {
    container.innerHTML = '<div class="loading-text" style="padding: 2rem 0; opacity: 0.6;">暂无数据</div>';
    return;
  }
  
  container.innerHTML = '';
  const list = document.createElement('div');
  list.className = 'top-songs-list';
  
  songs.forEach((song, index) => {
    const item = document.createElement('div');
    item.className = 'top-song-item';
    item.innerHTML = `
      <div class="rank ${index < 3 ? 'top-' + (index + 1) : ''}">${index + 1}</div>
      <img src="${song.cover || '/static/images/ICON_256.PNG'}" alt="cover" class="song-cover">
      <div class="song-info">
        <div class="song-title">${song.title || '未知歌曲'}</div>
        <div class="song-artist">${song.artist || '未知艺术家'}</div>
      </div>
      <div class="play-count">${song.play_count || 0} 次</div>
    `;
    list.appendChild(item);
  });
  
  container.appendChild(list);
}

// ============================================
// 管理员功能函数
// ============================================

// 加载用户列表
export async function loadUsers() {
  try {
    adminState.isLoading = true;
    const res = await api.admin.users.list();
    if (res.success) {
      adminState.users = res.users || [];
      const container = document.getElementById('admin-users-container');
      if (container) renderUserList(adminState.users, container);
    } else {
      showToast(res.error || '加载用户列表失败');
    }
  } catch (err) {
    console.error('加载用户列表失败:', err);
    showToast('加载用户列表失败');
  } finally {
    adminState.isLoading = false;
  }
}

// 显示用户详情
export async function showUserDetail(userId) {
  adminState.selectedUserId = userId;
  try {
    const [userRes, historyRes, statsRes] = await Promise.all([
      api.admin.users.get(userId),
      api.admin.stats.userHistory(userId, { limit: 50 }),
      api.admin.stats.userStats(userId)
    ]);
    
    // 渲染用户详情内容
    const contentContainer = document.getElementById('admin-user-detail-content');
    const overlayContainer = document.getElementById('admin-user-detail');
    
    if (contentContainer) {
      contentContainer.innerHTML = `
        <div class="user-detail-header">
          <h3>${userRes.user?.username || '用户 ' + userId}</h3>
          <span class="role-badge ${userRes.user?.is_admin ? 'admin' : 'user'}">
            ${userRes.user?.is_admin ? '管理员' : '普通用户'}
          </span>
        </div>
        <div class="user-stats">
          <div class="stat-item">
            <span class="stat-value">${statsRes.stats?.total_plays || 0}</span>
            <span class="stat-label">播放次数</span>
          </div>
          <div class="stat-item">
            <span class="stat-value">${statsRes.stats?.unique_songs || 0}</span>
            <span class="stat-label">播放歌曲数</span>
          </div>
          <div class="stat-item">
            <span class="stat-value">${formatDuration(statsRes.stats?.total_duration || 0)}</span>
            <span class="stat-label">总时长</span>
          </div>
        </div>
        <h4>最近播放</h4>
        <div id="user-history-container"></div>
      `;
      
      const historyContainer = document.getElementById('user-history-container');
      if (historyContainer) {
        renderUserHistory(historyRes.history || [], historyContainer);
      }
    }
    
    // 显示弹窗
    if (overlayContainer) {
      overlayContainer.classList.add('active');
    }
  } catch (err) {
    console.error('加载用户详情失败:', err);
    showToast('加载用户详情失败');
  }
}

// 加载统计概览
export async function loadStatsOverview() {
  try {
    const res = await api.admin.stats.overview();
    if (res.success) {
      adminState.statsOverview = res.stats;
      const container = document.getElementById('admin-stats-overview');
      if (container) renderStatsOverview(adminState.statsOverview, container);
    }
  } catch (err) {
    console.error('加载统计概览失败:', err);
  }
}

// 加载热门歌曲
export async function loadTopSongs(period = 'week') {
  adminState.currentPeriod = period;
  try {
    const res = await api.admin.stats.topSongs({ limit: 20, period });
    if (res.success) {
      adminState.topSongs = res.songs || [];
      const container = document.getElementById('admin-top-songs');
      if (container) renderTopSongs(adminState.topSongs, container);
    }
  } catch (err) {
    console.error('加载热门歌曲失败:', err);
  }
}

// 加载活跃用户
export async function loadActiveUsers(period = 'week') {
  try {
    const res = await api.admin.stats.activeUsers({ limit: 10, period });
    if (res.success) {
      adminState.activeUsers = res.users || [];
      const container = document.getElementById('admin-active-users');
      if (container) renderActiveUsers(adminState.activeUsers, container);
    }
  } catch (err) {
    console.error('加载活跃用户失败:', err);
  }
}

// 渲染活跃用户
function renderActiveUsers(users, container) {
  if (!container) return;
  
  if (!users || users.length === 0) {
    container.innerHTML = '<div class="loading-text" style="padding: 2rem 0; opacity: 0.6;">暂无数据</div>';
    return;
  }
  
  container.innerHTML = '';
  const list = document.createElement('div');
  list.className = 'active-users-grid';
  
  users.forEach((user, index) => {
    const item = document.createElement('div');
    item.className = 'active-user-item';
    item.innerHTML = `
      <div class="rank ${index < 3 ? 'top-' + (index + 1) : ''}">${index + 1}</div>
      <div class="user-info">
        <div class="user-name">${user.username || '用户 ' + user.user_id}</div>
        <div class="user-role">${user.is_admin ? '管理员' : '普通用户'}</div>
      </div>
      <div class="play-count">${user.play_count || 0} 次播放</div>
    `;
    list.appendChild(item);
  });
  
  container.appendChild(list);
}

// ============================================
// 工具函数
// ============================================

// 格式化日期时间
function formatDateTime(timestamp) {
  if (!timestamp) return '-';
  // 后端返回的是Unix时间戳（秒），需要转换为毫秒
  const date = new Date(timestamp * 1000);
  const now = new Date();
  const diff = now - date;
  
  // 1分钟内
  if (diff < 60000) return '刚刚';
  // 1小时内
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`;
  // 24小时内
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`;
  // 7天内
  if (diff < 604800000) return `${Math.floor(diff / 86400000)} 天前`;
  
  // 超过7天显示具体日期
  return date.toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  });
}

// 格式化时长（秒转为可读格式）
function formatDuration(seconds) {
  if (!seconds || seconds <= 0) return '0:00';
  
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);
  
  if (hours > 0) {
    return `${hours}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  }
  return `${minutes}:${secs.toString().padStart(2, '0')}`;
}

// 格式化长时长（用于统计概览，显示小时/分钟）
function formatDurationLong(seconds) {
  if (!seconds || seconds <= 0) return '0分钟';
  
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  
  if (days > 0) {
    return `${days}天${hours}小时`;
  }
  if (hours > 0) {
    return `${hours}小时${minutes}分`;
  }
  return `${minutes}分钟`;
}

// ============================================
// 初始化管理员模块
// ============================================
export function initAdmin() {
  console.log('[Admin] 管理员模块已加载');
  bindAdminEvents();
}

// 绑定管理员事件
function bindAdminEvents() {
  // 管理员导航点击
  const navAdmin = document.getElementById('nav-admin');
  if (navAdmin) {
    navAdmin.addEventListener('click', () => {
      switchTab('admin');
      loadAdminData();
    });
  }
  
  // 关闭用户详情面板
  const closeDetailBtn = document.getElementById('close-user-detail');
  if (closeDetailBtn) {
    closeDetailBtn.addEventListener('click', () => {
      const detailContainer = document.getElementById('admin-user-detail');
      if (detailContainer) detailContainer.classList.remove('active');
    });
  }
  
  // 热门歌曲时间段选择
  const periodSelect = document.getElementById('admin-top-songs-period');
  if (periodSelect) {
    periodSelect.addEventListener('change', (e) => {
      loadTopSongs(e.target.value);
    });
  }
}

// 加载管理员页面所有数据
export async function loadAdminData() {
  await Promise.all([
    loadStatsOverview(),
    loadUsers(),
    loadTopSongs(adminState.currentPeriod),
    loadActiveUsers(adminState.currentPeriod)
  ]);
}

// 导出默认初始化
export default {
  init: initAdmin,
  state: adminState,
  loadUsers,
  loadStatsOverview,
  loadTopSongs,
  loadActiveUsers,
  loadAdminData,
  showUserDetail
};
