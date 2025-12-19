// 后端 API 封装
const jsonOrThrow = async (resp) => {
  const data = await resp.json();
  return data;
};

export const api = {
  library: {
    async list() {
      const res = await fetch('/api/music');
      return jsonOrThrow(res);
    },
    async deleteFile(filename) {
      const encodedName = encodeURIComponent(filename);
      const res = await fetch(`/api/music/delete/${encodedName}`, { method: 'DELETE' });
      return jsonOrThrow(res);
    },
    async importPath(path) {
      const res = await fetch('/api/music/import_path', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path })
      });
      return jsonOrThrow(res);
    },
    async externalMeta(path) {
      const res = await fetch(`/api/music/external/meta?path=${encodeURIComponent(path)}`);
      return jsonOrThrow(res);
    },
    async clearMetadata(id) {
      const res = await fetch(`/api/music/clear_metadata/${id}`, { method: 'POST' });
      return jsonOrThrow(res);
    },
    async lyrics(query) {
      const res = await fetch(`/api/music/lyrics${query}`);
      return jsonOrThrow(res);
    },
    async albumArt(query) {
      const res = await fetch(`/api/music/album-art${query}`);
      return jsonOrThrow(res);
    },
    async rescan() {
      const res = await fetch('/api/library/rescan', { method: 'POST' });
      return jsonOrThrow(res);
    }
  },
  system: {
    async status() {
      const res = await fetch('/api/system/status');
      return jsonOrThrow(res);
    }
  },
  mount: {
    async list() {
      const res = await fetch('/api/mount_points');
      return jsonOrThrow(res);
    },
    async add(path) {
      const res = await fetch('/api/mount_points', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path })
      });
      return jsonOrThrow(res);
    },
    async remove(path) {
      const res = await fetch('/api/mount_points', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path })
      });
      return jsonOrThrow(res);
    }
  },
  netease: {
    async search(keywords) {
      const res = await fetch(`/api/netease/search?keywords=${encodeURIComponent(keywords)}`);
      return jsonOrThrow(res);
    },
    async resolve(input) {
      const res = await fetch(`/api/netease/resolve?input=${encodeURIComponent(input)}`);
      return jsonOrThrow(res);
    },
    async download(body) {
      const res = await fetch('/api/netease/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      return jsonOrThrow(res);
    },
    async configGet() {
      const res = await fetch('/api/netease/config');
      return jsonOrThrow(res);
    },
    async configSave(payload) {
      const res = await fetch('/api/netease/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      return jsonOrThrow(res);
    },
    async loginStatus() {
      const res = await fetch('/api/netease/login/status');
      return jsonOrThrow(res);
    },
    async loginQr() {
      const res = await fetch('/api/netease/login/qrcode');
      return jsonOrThrow(res);
    },
    async loginCheck(key) {
      const res = await fetch(`/api/netease/login/check?key=${encodeURIComponent(key)}`);
      return jsonOrThrow(res);
    },
    async logout() {
      const res = await fetch('/api/netease/logout', { method: 'POST' });
      return jsonOrThrow(res);
    },
    async playlist(id) {
      const res = await fetch(`/api/netease/playlist?id=${encodeURIComponent(id)}`);
      return jsonOrThrow(res);
    },
    async song(id) {
      const res = await fetch(`/api/netease/song?id=${encodeURIComponent(id)}`);
      return jsonOrThrow(res);
    },
    async task(taskId) {
      const res = await fetch(`/api/netease/task/${encodeURIComponent(taskId)}`);
      return jsonOrThrow(res);
    },
    async installService() {
      const res = await fetch('/api/netease/install_service', { method: 'POST' });
      return jsonOrThrow(res);
    },
    async getInstallStatus() {
      const res = await fetch('/api/netease/install/status');
      return jsonOrThrow(res);
    },
    async recommend() {
      const res = await fetch('/api/netease/recommend');
      return jsonOrThrow(res);
    }
  },
  favorites: {
    async list() {
      const res = await fetch('/api/favorites');
      return jsonOrThrow(res);
    },
    async add(id) {
      const res = await fetch(`/api/favorites/${encodeURIComponent(id)}`, { method: 'POST' });
      return jsonOrThrow(res);
    },
    async remove(id) {
      const res = await fetch(`/api/favorites/${encodeURIComponent(id)}`, { method: 'DELETE' });
      return jsonOrThrow(res);
    }
  },
  qqmusic: {
    async search(keywords, num = 20) {
      const res = await fetch(`/api/qqmusic/search?keywords=${encodeURIComponent(keywords)}&num=${num}`);
      return jsonOrThrow(res);
    },
    async songDetail(mid) {
      const res = await fetch(`/api/qqmusic/song/detail?mid=${encodeURIComponent(mid)}`);
      return jsonOrThrow(res);
    },
    async songUrl(mid, type = 'MP3_128') {
      const res = await fetch(`/api/qqmusic/song/url?mid=${encodeURIComponent(mid)}&type=${type}`);
      return jsonOrThrow(res);
    },
    async lyric(mid) {
      const res = await fetch(`/api/qqmusic/lyric?mid=${encodeURIComponent(mid)}`);
      return jsonOrThrow(res);
    },
    async download(body) {
      const res = await fetch('/api/qqmusic/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      return jsonOrThrow(res);
    },
    async task(taskId) {
      const res = await fetch(`/api/qqmusic/task/${encodeURIComponent(taskId)}`);
      return jsonOrThrow(res);
    },
    async configGet() {
      const res = await fetch('/api/qqmusic/config');
      return jsonOrThrow(res);
    },
    async configSave(payload) {
      const res = await fetch('/api/qqmusic/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      return jsonOrThrow(res);
    },
    async loginStatus() {
      const res = await fetch('/api/qqmusic/login/status');
      return jsonOrThrow(res);
    },
    async loginQr(type = 'qq') {
      const res = await fetch(`/api/qqmusic/login/qrcode?type=${type}`);
      return jsonOrThrow(res);
    },
    async loginCheck(identifier, qrType = 'qq') {
      const res = await fetch(`/api/qqmusic/login/check?identifier=${encodeURIComponent(identifier)}&qr_type=${qrType}`);
      return jsonOrThrow(res);
    },
    async loginPhoneSend(phone, countryCode = '86') {
      const res = await fetch('/api/qqmusic/login/phone/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone, country_code: countryCode })
      });
      return jsonOrThrow(res);
    },
    async loginPhoneVerify(phone, authCode, countryCode = '86') {
      const res = await fetch('/api/qqmusic/login/phone/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone, auth_code: authCode, country_code: countryCode })
      });
      return jsonOrThrow(res);
    },
    async loginCookie(musicid, musickey) {
      const res = await fetch('/api/qqmusic/login/cookie', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ musicid, musickey })
      });
      return jsonOrThrow(res);
    },
    async hotkey() {
      const res = await fetch('/api/qqmusic/hotkey');
      return jsonOrThrow(res);
    }
  },
  playlists: {
    async list() {
      const res = await fetch('/api/playlists');
      return jsonOrThrow(res);
    },
    async create(name) {
      const res = await fetch('/api/playlists', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
      });
      return jsonOrThrow(res);
    },
    async delete(id) {
      const res = await fetch(`/api/playlists/${id}`, { method: 'DELETE' });
      return jsonOrThrow(res);
    },
    async rename(id, name) {
      const res = await fetch(`/api/playlists/${id}/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
      });
      return jsonOrThrow(res);
    },
    async getSongs(id) {
      const res = await fetch(`/api/playlists/${id}/songs`);
      return jsonOrThrow(res);
    },
    async addSong(playlistId, songId) {
      const res = await fetch(`/api/playlists/${playlistId}/songs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ song_id: songId })
      });
      return jsonOrThrow(res);
    },
    async removeSong(playlistId, songId) {
      const res = await fetch(`/api/playlists/${playlistId}/songs/${songId}`, { method: 'DELETE' });
      return jsonOrThrow(res);
    },
    async createWithSource(name, pendingSongs, sourceUrl, sourceType) {
      const res = await fetch('/api/playlists', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, pending_songs: pendingSongs, source_url: sourceUrl, source_type: sourceType })
      });
      return jsonOrThrow(res);
    },
    async sync(playlistId) {
      const res = await fetch(`/api/playlists/${playlistId}/sync`, { method: 'POST' });
      return jsonOrThrow(res);
    }
  },
  // 播放记录
  play: {
    async record(songId, title, artist, duration) {
      const res = await fetch('/api/play/record', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ song_id: songId, title, artist, duration })
      });
      return jsonOrThrow(res);
    }
  },
  // 管理员 API
  admin: {
    users: {
      async list() {
        const res = await fetch('/api/admin/users');
        return jsonOrThrow(res);
      },
      async get(userId) {
        const res = await fetch(`/api/admin/users/${userId}`);
        return jsonOrThrow(res);
      },
      async delete(userId) {
        const res = await fetch(`/api/admin/users/${userId}`, { method: 'DELETE' });
        return jsonOrThrow(res);
      }
    },
    stats: {
      async overview() {
        const res = await fetch('/api/admin/stats/overview');
        return jsonOrThrow(res);
      },
      async userStats(userId) {
        const res = await fetch(`/api/admin/stats/user/${userId}`);
        return jsonOrThrow(res);
      },
      async userHistory(userId, options = {}) {
        const params = new URLSearchParams();
        if (options.limit) params.append('limit', options.limit);
        if (options.offset) params.append('offset', options.offset);
        const query = params.toString() ? `?${params.toString()}` : '';
        const res = await fetch(`/api/admin/stats/user/${userId}/history${query}`);
        return jsonOrThrow(res);
      },
      async topSongs(options = {}) {
        const params = new URLSearchParams();
        if (options.limit) params.append('limit', options.limit);
        if (options.period) params.append('period', options.period);
        const query = params.toString() ? `?${params.toString()}` : '';
        const res = await fetch(`/api/admin/stats/top-songs${query}`);
        return jsonOrThrow(res);
      },
      async activeUsers(options = {}) {
        const params = new URLSearchParams();
        if (options.limit) params.append('limit', options.limit);
        if (options.period) params.append('period', options.period);
        const query = params.toString() ? `?${params.toString()}` : '';
        const res = await fetch(`/api/admin/stats/active-users${query}`);
        return jsonOrThrow(res);
      }
    }
  }
};
