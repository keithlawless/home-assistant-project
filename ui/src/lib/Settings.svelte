<script>
  // Settings sheet: voice, wake sensitivity, location, units, clock format,
  // screensaver timeout.
  import { onMount } from 'svelte';
  import { settings, saveSettings, searchPlaces } from './store.js';

  const DEFAULT_VOICE = 'piper:en_US-lessac-medium';
  let voices = $state([]);
  let previewing = $state('');

  onMount(async () => {
    const r = await fetch('/api/voices');
    if (r.ok) voices = await r.json();
  });

  async function preview(id) {
    previewing = id;
    await fetch('/api/voice/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ voice: id }),
    });
    setTimeout(() => { if (previewing === id) previewing = ''; }, 5000);
  }

  let { onClose } = $props();

  let query = $state('');
  let results = $state([]);
  let searching = $state(false);
  let error = $state('');

  async function save(changes) {
    error = '';
    try {
      await saveSettings(changes);
    } catch (e) {
      error = String(e.message || e);
    }
  }

  async function search(e) {
    e.preventDefault();
    if (!query.trim()) return;
    searching = true;
    results = await searchPlaces(query.trim());
    searching = false;
  }

  async function pick(place) {
    await save({ location: place });
    results = [];
    query = '';
  }

  let s = $derived($settings);
</script>

<div class="sheet">
  <header>
    <h1>Settings</h1>
    <button class="done" onclick={onClose}>Done</button>
  </header>

  {#if s}
    <h2>Voice</h2>
    {#each [['piper', 'Fast'], ['kokoro', 'Most natural · a little slower to start']] as [engine, title]}
      <div class="group-title">{title}</div>
      <div class="voices">
        {#each voices.filter((v) => v.engine === engine) as v (v.id)}
          {@const current = (s.voice ?? DEFAULT_VOICE) === v.id}
          <div class="voice" class:current>
            <button class="pick" onclick={() => save({ voice: v.id })}>
              <div class="vname">{v.name}</div>
              <div class="sub">{v.accent} {v.gender} · {v.note}</div>
            </button>
            <button class="play" aria-label="Preview {v.name}" onclick={() => preview(v.id)}>
              <svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true">
                {#if previewing === v.id}
                  <rect x="5" y="9" width="3" height="6" rx="1" /><rect x="10.5" y="6" width="3" height="12" rx="1" /><rect x="16" y="9" width="3" height="6" rx="1" />
                {:else}
                  <path d="M8 5.5v13l11-6.5z" />
                {/if}
              </svg>
            </button>
          </div>
        {/each}
      </div>
    {/each}

    <h2>General</h2>
    <div class="rows">
      <div class="row">
        <div class="name">Wake word sensitivity<div class="sub">Higher hears you more easily, but wakes by mistake more often</div></div>
        <div class="seg">
          {#each ['low', 'medium', 'high'] as level}
            <button class:on={s.wake_sensitivity === level} onclick={() => save({ wake_sensitivity: level })}>
              {level[0].toUpperCase() + level.slice(1)}
            </button>
          {/each}
        </div>
      </div>

      <div class="row">
        <div class="name">Location<div class="sub">{s.location?.name ?? 'Not set'}</div></div>
        <form onsubmit={search}>
          <input bind:value={query} placeholder="City or ZIP code" />
          <button type="submit">{searching ? '…' : 'Search'}</button>
        </form>
      </div>
      {#each results as place}
        <button class="result" onclick={() => pick(place)}>{place.name}</button>
      {/each}

      <div class="row">
        <div class="name">Temperature</div>
        <div class="seg">
          <button class:on={s.units === 'fahrenheit'} onclick={() => save({ units: 'fahrenheit' })}>°F</button>
          <button class:on={s.units === 'celsius'} onclick={() => save({ units: 'celsius' })}>°C</button>
        </div>
      </div>

      <div class="row">
        <div class="name">Clock</div>
        <div class="seg">
          <button class:on={!s.clock_24h} onclick={() => save({ clock_24h: false })}>12-hour</button>
          <button class:on={s.clock_24h} onclick={() => save({ clock_24h: true })}>24-hour</button>
        </div>
      </div>

      <div class="row">
        <div class="name">Screensaver after</div>
        <div class="stepper">
          <button onclick={() => save({ screensaver_minutes: Math.max(1, s.screensaver_minutes - 1) })}>−</button>
          <span>{s.screensaver_minutes} min</span>
          <button onclick={() => save({ screensaver_minutes: Math.min(60, s.screensaver_minutes + 1) })}>+</button>
        </div>
      </div>
    </div>
    {#if error}<div class="error">{error}</div>{/if}
  {/if}
</div>

<style>
  .sheet {
    position: fixed;
    inset: 0;
    z-index: 40;
    background: var(--bg);
    padding: 20px 28px;
    overflow-y: auto;
  }
  header { display: flex; justify-content: space-between; align-items: center; }
  h1 { font-size: 30px; font-weight: 600; margin: 0; }
  .done { background: var(--accent); color: #1a1205; font-weight: 600; padding: 12px 26px; }
  .rows { display: flex; flex-direction: column; gap: 10px; }
  h2 { font-size: 16px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin: 24px 0 10px; }
  .group-title { font-size: 16px; color: var(--muted); margin: 6px 0 8px; }
  .voices { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 12px; }
  .voice { display: flex; background: var(--panel); border-radius: var(--radius); border: 1px solid transparent; overflow: hidden; }
  .voice.current { border-color: var(--accent); background: var(--accent-soft); }
  .pick { flex: 1; text-align: left; background: transparent; padding: 12px 14px; }
  .vname { font-size: 19px; font-weight: 600; }
  .voice.current .vname { color: var(--accent); }
  .play { background: transparent; color: var(--muted); padding: 0 14px; border-radius: 0; }
  .play:active { color: var(--accent); }
  .row {
    display: flex; justify-content: space-between; align-items: center;
    background: var(--panel); border-radius: var(--radius); padding: 14px 18px;
    font-size: 20px;
  }
  .sub { font-size: 15px; color: var(--muted); margin-top: 2px; }
  form { display: flex; gap: 8px; }
  form input { width: 260px; }
  .result { text-align: left; font-size: 19px; padding: 14px 18px; background: var(--panel-2); }
  .seg { display: flex; gap: 6px; }
  .seg button { min-width: 90px; }
  .seg .on { background: var(--accent); color: #1a1205; font-weight: 600; }
  .stepper { display: flex; align-items: center; gap: 14px; }
  .stepper button { width: 52px; font-size: 24px; padding: 6px 0; }
  .stepper span { min-width: 70px; text-align: center; }
  .error { color: #f87171; margin-top: 12px; }
</style>
