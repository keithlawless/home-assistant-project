<script>
  // App shell: home screen, settings, voice overlay, screensaver, and a slow
  // pixel shift of the whole UI to spread wear on static elements.
  import { onMount } from 'svelte';
  import Home from './lib/Home.svelte';
  import Settings from './lib/Settings.svelte';
  import Screensaver from './lib/Screensaver.svelte';
  import VoiceOverlay from './lib/VoiceOverlay.svelte';
  import { connect, connected, settings, voiceEvents } from './lib/store.js';

  const SHIFT_EVERY_MS = 3 * 60_000;
  const SHIFT_PX = 4;

  let showSettings = $state(false);
  let saverOn = $state(false);
  let lastActivity = Date.now();
  let shift = $state({ x: 0, y: 0 });

  function activity() {
    lastActivity = Date.now();
  }

  // Voice activity counts as use: wake the screen when Taco hears its name.
  $effect(() => {
    const e = $voiceEvents;
    if (e && !(e.type === 'state' && e.state === 'idle')) {
      activity();
      saverOn = false;
    }
  });

  onMount(() => {
    if (!new URLSearchParams(location.search).has('cursor')) {
      document.body.classList.add('no-cursor');
    }
    connect();

    const idleCheck = setInterval(() => {
      const minutes = $settings?.screensaver_minutes ?? 5;
      if (!saverOn && Date.now() - lastActivity > minutes * 60_000) {
        saverOn = true;
        showSettings = false;
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
  <Home onSettings={() => (showSettings = true)} />
  {#if showSettings}
    <Settings onClose={() => (showSettings = false)} />
  {/if}
  <VoiceOverlay />
  {#if !$connected}
    <div class="offline">Reconnecting to Taco…</div>
  {/if}
</div>

{#if saverOn}
  <Screensaver onWake={() => { activity(); saverOn = false; }} />
{/if}

<style>
  .shell {
    position: fixed;
    inset: 0;
    transition: transform 2s ease;
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
