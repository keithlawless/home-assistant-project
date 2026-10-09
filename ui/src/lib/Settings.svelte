<script>
  // Settings sheet: location, units, clock format, screensaver timeout.
  // Phase 5 adds voice selection and wake sensitivity here.
  import { settings, saveSettings, searchPlaces } from './store.js';

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
    <div class="rows">
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
  .rows { margin-top: 18px; display: flex; flex-direction: column; gap: 10px; }
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
