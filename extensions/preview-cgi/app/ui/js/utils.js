export function autoResizeUI() {
  if (window.innerWidth > 768) {
    let scale = Math.min(Math.max(window.innerWidth / 1440, 0.8), 1.2);
    document.documentElement.style.setProperty('--ui-scale', scale.toFixed(3));
  } else {
    document.documentElement.style.setProperty('--ui-scale', '1.0');
  }
}

export function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const iconMap = {
    'info': '<i class="fas fa-info-circle"></i>',
    'success': '<i class="fas fa-check-circle"></i>',
    'error': '<i class="fas fa-exclamation-circle"></i>',
    'warning': '<i class="fas fa-exclamation-triangle"></i>',
    'loading': '<i class="fas fa-spinner fa-spin"></i>'
  };

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
        <div class="toast-icon">${iconMap[type] || iconMap['info']}</div>
        <div class="toast-content">${message}</div>
    `;

  container.appendChild(toast);
  // Limit max toasts
  if (container.childElementCount > 5) {
    container.firstChild.remove();
  }

  // Animation frame for smooth entry
  requestAnimationFrame(() => toast.classList.add('show'));

  // Auto remove
  if (type !== 'loading') {
    setTimeout(() => {
      toast.classList.remove('show');
      setTimeout(() => toast.remove(), 300);
    }, 3000);
  }
}

export function formatTime(seconds) {
  if (isNaN(seconds)) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s < 10 ? '0' : ''}${s}`;
}
