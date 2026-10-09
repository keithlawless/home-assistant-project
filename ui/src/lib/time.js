// A ticking clock store plus formatters shared by the home screen and screensaver.
import { readable } from 'svelte/store';

// Chromium on the Pi defaults to en-GB; the household reads US dates.
const LOCALE = 'en-US';

export const now = readable(new Date(), (set) => {
  const id = setInterval(() => set(new Date()), 1000);
  return () => clearInterval(id);
});

export function formatTime(date, h24 = false) {
  return date.toLocaleTimeString(LOCALE, { hour: 'numeric', minute: '2-digit', hour12: !h24 })
    .replace(/\s?[AP]M$/i, '');
}

export function ampm(date, h24 = false) {
  return h24 ? '' : date.getHours() < 12 ? 'AM' : 'PM';
}

export function formatDate(date) {
  return date.toLocaleDateString(LOCALE, { weekday: 'long', month: 'long', day: 'numeric' });
}

// Open-Meteo returns local times without a zone ("2026-10-09T14:00").
export function hourLabel(iso, h24 = false) {
  const h = Number(iso.slice(11, 13));
  if (h24) return `${String(h).padStart(2, '0')}:00`;
  return `${h % 12 || 12} ${h < 12 ? 'AM' : 'PM'}`;
}

export function dayLabel(isoDate, index) {
  if (index === 0) return 'Today';
  const [y, m, d] = isoDate.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(LOCALE, { weekday: 'short' });
}
