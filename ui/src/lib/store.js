// Live state from taco-core over a WebSocket, with automatic reconnect.
import { writable } from 'svelte/store';

export const weather = writable(null);
export const settings = writable(null);
export const connected = writable(false);
export const timers = writable([]);
export const devices = writable([]);
export const found = writable([]);
export const lists = writable({});
export const scenes = writable([]);
export const schedules = writable([]);
export const alerts = writable([]);
export const media = writable(null);    // {station} while the radio plays
export const recipe = writable(null);   // {title, ingredients, steps, step}
export const notifications = writable([]);   // cards, newest first
// Bumped when a new card should wake the screen (camera motion etc.).
export const notificationWake = writable(0);
// Screen mode pushed by core: 'night' (black until touched) or 'on'.
export const screenMode = writable('on');
// Each voice event from taco-voice: {type: 'state'|'heard'|'tool'|'reply', ...}
export const voiceEvents = writable(null);
// Cleaning mode: when (Date.now() ms) the touchscreen starts answering again; 0 = off.
export const cleaningUntil = writable(0);

const cleaningEnd = (seconds) => (seconds > 0 ? Date.now() + seconds * 1000 : 0);

function handle(msg) {
  switch (msg.type) {
    case 'snapshot':
      weather.set(msg.weather);
      settings.set(msg.settings);
      timers.set(msg.timers ?? []);
      devices.set(msg.devices ?? []);
      found.set(msg.found ?? []);
      lists.set(msg.lists ?? {});
      scenes.set(msg.scenes ?? []);
      schedules.set(msg.schedules ?? []);
      alerts.set(msg.alerts ?? []);
      media.set(msg.media ?? null);
      recipe.set(msg.recipe ?? null);
      notifications.set(msg.notifications ?? []);
      cleaningUntil.set(cleaningEnd(msg.cleaning ?? 0));
      break;
    case 'notifications':
      notifications.set(msg.notifications);
      if (msg.wake) notificationWake.update((n) => n + 1);
      break;
    case 'media':
      media.set(msg.media);
      break;
    case 'recipe':
      recipe.set(msg.recipe);
      break;
    case 'schedules':
      schedules.set(msg.schedules);
      break;
    case 'alerts':
      alerts.set(msg.alerts);
      break;
    case 'lists':
      lists.set(msg.lists);
      break;
    case 'scenes':
      scenes.set(msg.scenes);
      break;
    case 'screen':
      screenMode.set(msg.mode);
      break;
    case 'devices':
      devices.set(msg.devices);
      break;
    case 'found':
      found.set(msg.found);
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
    case 'cleaning':
      cleaningUntil.set(cleaningEnd(msg.seconds));
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

async function post(url, body, method = 'POST') {
  const r = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

export const setPower = (id, on) => post('/api/devices/control', { ids: [id], on });
export const setLevel = (id, level) => post('/api/devices/control', { ids: [id], level });
export const setRoomPower = (ids, on) => post('/api/devices/control', { ids, on });
export const scanDevices = () => post('/api/devices/scan');
export const addDevice = (key, name, room) => post('/api/devices', { key, name, room });

export const toggleItem = (id, done) => post(`/api/lists/items/${id}`, { done }, 'PATCH');
export const deleteItem = (id) => post(`/api/lists/items/${id}`, undefined, 'DELETE');
export const clearDone = (name) => post(`/api/lists/${encodeURIComponent(name)}?done_only=true`, undefined, 'DELETE');
export const runScene = (name) => post(`/api/scenes/${encodeURIComponent(name)}/run`);
export const setScheduleEnabled = (id, enabled) => post(`/api/schedules/${id}`, { enabled }, 'PATCH');
export const stopMedia = () => post('/api/media/stop');
export const recipeStep = (delta) => post('/api/recipe/step', { delta });
export const closeRecipe = () => post('/api/recipe', undefined, 'DELETE');
export const dismissNotification = (id) => post(`/api/notifications/${id}`, undefined, 'DELETE');
export const dismissAllNotifications = () => post('/api/notifications', undefined, 'DELETE');
