<script>
  // App shell: home screen, settings, voice overlay, screensaver, and a slow
  // pixel shift of the whole UI to spread wear on static elements.
  import { onMount } from 'svelte';
  import Home from './lib/Home.svelte';
  import Settings from './lib/Settings.svelte';
  import Devices from './lib/Devices.svelte';
  import Lists from './lib/Lists.svelte';
  import Screensaver from './lib/Screensaver.svelte';
  import VoiceOverlay from './lib/VoiceOverlay.svelte';
  import Ringing from './lib/Ringing.svelte';
  import Recipe from './lib/Recipe.svelte';
  import Cleaning from './lib/Cleaning.svelte';
  import { connect, connected, settings, timers, voiceEvents, screenMode, saveSettings, recipe,
           notificationWake, cleaningUntil } from './lib/store.js';

  const SHIFT_EVERY_MS = 3 * 60_000;
  const SHIFT_PX = 4;

  let showSettings = $state(false);
  let showDevices = $state(false);
  let showLists = $state(false);
  let night = $state(false);
  let saverOn = $state(false);
  let lastActivity = Date.now();
  let shift = $state({ x: 0, y: 0 });

  function activity() {
    lastActivity = Date.now();
  }

  // Tell core when the UI itself goes dark or wakes, so it can power the
  // panel down/up (night_screen_off).
  function reportScreen(mode) {
    fetch('/api/screen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, from_ui: true }),
    }).catch(() => {});
  }

  function setNight(on) {
    if (night === on) return;
    night = on;
    reportScreen(on ? 'night' : 'on');
  }

  // "Good night" and other scenes can put the screen in night mode
  // (core has already powered the panel down, so no report back).
  $effect(() => {
    if ($screenMode === 'night') {
      night = true;
      saverOn = false;
      showSettings = showDevices = showLists = false;
      screenMode.set('on');
    }
  });

  function inNightHours(nh) {
    if (!nh?.enabled) return false;
    const now = new Date();
    const mins = now.getHours() * 60 + now.getMinutes();
    const toMins = (hhmm) => { const [h, m] = hhmm.split(':').map(Number); return h * 60 + m; };
    const start = toMins(nh.start);
    const end = toMins(nh.end);
    return start <= end ? mins >= start && mins < end : mins >= start || mins < end;
  }

  function closeSheets() {
    showSettings = showDevices = showLists = false;
  }

  function wakeFromNight() {
    setNight(false);
    activity();
  }

  // A ringing timer wakes the screen.
  $effect(() => {
    if ($timers.some((t) => t.state === 'ringing')) {
      activity();
      saverOn = false;
      setNight(false);
    }
  });

  // A notification that asks to be seen (camera motion) wakes the screen,
  // even from night mode.
  $effect(() => {
    if ($notificationWake) {
      activity();
      saverOn = false;
      setNight(false);
    }
  });

  // Cleaning mode wakes the screen so the countdown (and the smudges) show.
  $effect(() => {
    if ($cleaningUntil) {
      activity();
      saverOn = false;
      setNight(false);
    }
  });

  // Voice activity counts as use: wake the screen when Taco hears its name.
  $effect(() => {
    const e = $voiceEvents;
    if (e && !(e.type === 'state' && e.state === 'idle')) {
      activity();
      saverOn = false;
      setNight(false);
    }
  });

  onMount(() => {
    if (!new URLSearchParams(location.search).has('cursor')) {
      document.body.classList.add('no-cursor');
    }
    connect();
    // Deep links for testing and remote screens: /#devices, /#settings
    showDevices = location.hash === '#devices';
    showSettings = location.hash === '#settings';
    showLists = location.hash === '#lists';

    const idleCheck = setInterval(() => {
      const minutes = $settings?.screensaver_minutes ?? 5;
      if ($recipe) return;   // keep the recipe on screen while cooking
      if (!saverOn && !night && Date.now() - lastActivity > minutes * 60_000) {
        if (inNightHours($settings?.night_hours)) {
          setNight(true);   // fully dark overnight instead of the clock screensaver
          closeSheets();
          return;
        }
        saverOn = true;
        showSettings = false;
        showDevices = false;
        showLists = false;
      }
    }, 5_000);

    const shiftTimer = setInterval(() => {
      const r = () => Math.round((Math.random() * 2 - 1) * SHIFT_PX);
      shift = { x: r(), y: r() };
    }, SHIFT_EVERY_MS);

    return () => {
      clearInterval(idleCheck);
      clearInterval(shiftTimer);
    };
  });
</script>

<svelte:window onpointerdown={activity} onkeydown={activity} />

<div class="shell" style="transform: translate({shift.x}px, {shift.y}px)">
  <Home onSettings={() => (showSettings = true)} onDevices={() => (showDevices = true)} onLists={() => (showLists = true)} />
  {#if showLists}
    <Lists onClose={() => (showLists = false)} />
  {/if}
  {#if showDevices}
    <Devices onClose={() => (showDevices = false)} />
  {/if}
  {#if showSettings}
    <Settings onClose={() => (showSettings = false)} />
  {/if}
  <Recipe />
  <VoiceOverlay />
  {#if $settings?.mic_muted}
    <button class="mic-off" onclick={() => saveSettings({ mic_muted: false })}>
      <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M2 2l20 20M9 9v3a3 3 0 0 0 5.1 2.1M15 9.3V5a3 3 0 0 0-5.9-.7M17 16.9A7 7 0 0 1 5 12v-2m14 0v2c0 .8-.1 1.5-.4 2.2M12 19v3" />
      </svg>
      Mic off · tap to listen
    </button>
  {/if}
  {#if !$connected}
    <div class="offline">Reconnecting to Taco…</div>
  {/if}
</div>

{#if night}
  <!-- Night mode: fully black; the touch that wakes it is swallowed. -->
  <div class="night" role="presentation" onpointerdown={(e) => e.stopPropagation()}
       onclick={(e) => { e.stopPropagation(); wakeFromNight(); }}></div>
{/if}
{#if saverOn}
  <Screensaver onWake={() => { activity(); saverOn = false; }} />
{/if}
<Ringing />
<Cleaning />

<style>
  .shell {
    position: fixed;
    inset: 0;
    transition: transform 2s ease;
  }
  .night { position: fixed; inset: 0; background: #000; z-index: 110; }
  .mic-off {
    position: fixed; top: 12px; left: 16px; z-index: 60;
    display: flex; align-items: center; gap: 8px;
    background: #7f1d1d; color: #fee2e2; font-size: 16px; padding: 8px 14px; border-radius: 999px;
  }
  .offline {
    position: fixed;
    top: 12px;
    left: 50%;
    transform: translateX(-50%);
    background: #7f1d1d;
    color: #fee2e2;
    padding: 6px 16px;
    border-radius: 999px;
    font-size: 15px;
    z-index: 60;
  }
</style>
