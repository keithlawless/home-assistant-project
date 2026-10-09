// Live state from taco-core over a WebSocket, with automatic reconnect.
import { writable } from 'svelte/store';

export const weather = writable(null);
export const settings = writable(null);
export const connected = writable(false);
export const timers = writable([]);
// Each voice event from taco-voice: {type: 'state'|'heard'|'tool'|'reply', ...}
export const voiceEvents = writable(null);

function handle(msg) {
  switch (msg.type) {
    case 'snapshot':
      weather.set(msg.weather);
      settings.set(msg.settings);
      timers.set(msg.timers ?? []);
      break;
    case 'timers':
      timers.set(msg.timers);
      break;
    case 'weather':
      weather.set(msg.weather);
      break;
    case 'settings':
      settings.set(msg.settings);
      break;
    case 'voice':
      voiceEvents.set(msg.event);
      break;
  }
}

export function connect() {
  const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
  const ws = new WebSocket(url);
  ws.onopen = () => connected.set(true);
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => {
    connected.set(false);
    setTimeout(connect, 2000);
  };
}

export async function saveSettings(changes) {
  const r = await fetch('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(changes),
  });
  if (!r.ok) throw new Error(await r.text());
  settings.set(await r.json());
}

export async function searchPlaces(q) {
  const r = await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
  return r.ok ? r.json() : [];
}

// Silence ringing timers and cut off Taco mid-sentence.
export function stopAll() {
  return fetch('/api/stop', { method: 'POST' });
}

export function cancelTimer(id) {
  return fetch(`/api/timers/${id}`, { method: 'DELETE' });
}
