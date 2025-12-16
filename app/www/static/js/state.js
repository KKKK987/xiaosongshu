import { ui } from './ui.js';

const cachedNeteaseUser = JSON.parse(localStorage.getItem('xiaosongshu_netease_user') || 'null');

// 状态集中管理
export const state = {
  fullPlaylist: JSON.parse(localStorage.getItem('xiaosongshu_playlist') || '[]'),
  displayPlaylist: [],
  playQueue: [],
  currentTrackIndex: 0,
  isPlaying: false,
  playMode: 0,
  lyricsData: [],
  currentFetchId: 0,
  favorites: new Set(JSON.parse(localStorage.getItem('xiaosongshu_favs') || '[]')),
  savedState: JSON.parse(localStorage.getItem('xiaosongshu_state') || '{}'),
  currentTab: JSON.parse(localStorage.getItem('xiaosongshu_state') || '{}').tab || 'local',
  neteaseResults: [],
  neteaseRecommendations: [],
  neteaseResultSource: 'recommend',
  neteasePollingTimer: null,
  currentLoginKey: null,
  neteaseDownloadDir: '',
  neteaseApiBase: '',
  neteaseSelected: new Set(),
  neteaseUser: cachedNeteaseUser,
  neteaseIsVip: cachedNeteaseUser?.isVip || false,
  neteaseDownloadTasks: [],
  neteasePendingQueue: [],
  neteaseQueueToastShown: false,
  neteaseMaxConcurrent: 20,
  isPolling: false,
  progressToastEl: null,
  currentConfirmAction: null,
  libraryVersion: 0,
  // QQ 音乐状态
  qqmusicResults: [],
  qqmusicSelected: new Set(),
  qqmusicDownloadTasks: [],
  qqmusicPendingQueue: [],
  qqmusicDownloadDir: '',
  qqmusicApiBase: '',
  qqmusicQuality: localStorage.getItem('xiaosongshu_qqmusic_quality') || 'FLAC',
  // 待添加歌曲的歌单（用于下载完成后自动添加）
  pendingPlaylistForDownload: null,
};

export function persistState(audio) {
  const { playQueue, currentTrackIndex, playMode, currentTab } = state;
  const currentSong = playQueue[currentTrackIndex];
  if (currentSong && currentSong.isExternal) return;

  const nextState = {
    volume: audio?.volume ?? 1,
    playMode,
    currentTime: audio?.currentTime ?? 0,
    currentFilename: currentSong ? currentSong.filename : null,
    tab: currentTab,
    isFullScreen: ui.overlay ? ui.overlay.classList.contains('active') : false
  };
  localStorage.setItem('xiaosongshu_state', JSON.stringify(nextState));
}

export function saveFavorites() {
  localStorage.setItem('xiaosongshu_favs', JSON.stringify([...state.favorites]));
}

export function savePlaylist() {
  localStorage.setItem('xiaosongshu_playlist', JSON.stringify(state.fullPlaylist));
}
