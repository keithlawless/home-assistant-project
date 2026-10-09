<script>
  // Devices sheet: tiles grouped by room (tap to toggle), brightness for
  // dimmable devices, and discovery of new ones. Renaming and nicknames are
  // done by voice ("Taco, rename the living room lamp one to reading lamp").
  import { devices, found, settings, scenes, runScene, setPower, setLevel, setRoomPower, scanDevices, addDevice } from './store.js';

  let { onClose } = $props();

  let scanning = $state(false);
  let scanned = $state(false);
  let error = $state('');
  let busy = $state(new Set());

  let rooms = $derived.by(() => {
    const order = $settings?.rooms ?? [];
    const groups = new Map();
    for (const d of $devices) {
      const room = d.room || 'Other';
      if (!groups.has(room)) groups.set(room, []);
      groups.get(room).push(d);
    }
    const rank = (r) => (order.indexOf(r) === -1 ? 999 : order.indexOf(r));
    return [...groups.entries()].sort((a, b) => rank(a[0]) - rank(b[0]));
  });

  // "Living Room Lamp One" in the Living Room section reads better as "Lamp One".
  function shortName(d) {
    if (d.room && d.name.toLowerCase().startsWith(d.room.toLowerCase() + ' ')) {
      return d.name.slice(d.room.length + 1);
    }
    return d.name;
  }

  async function act(id, fn) {
    error = '';
    busy = new Set([...busy, id]);
    try {
      await fn();
    } catch (e) {
      error = String(e.message || e);
    } finally {
      busy.delete(id);
      busy = new Set(busy);
    }
  }

  async function scan() {
    scanning = true;
    error = '';
    try {
      found.set(await scanDevices());
    } catch (e) {
      error = String(e.message || e);
    }
    scanning = false;
    scanned = true;
  }

  const add = (f) => act(f.key, () => addDevice(f.key, null, f.suggested_room));
</script>

<div class="sheet">
  <header>
    <h1>Devices</h1>
    <div class="actions">
      <button onclick={scan} disabled={scanning}>{scanning ? 'Searching…' : 'Find devices'}</button>
      <button class="done" onclick={onClose}>Done</button>
    </div>
  </header>

  {#if error}<div class="error">{error}</div>{/if}

  {#if $scenes.length && $devices.length}
    <div class="scenes">
      {#each $scenes as sc (sc.name)}
        <button onclick={() => act(sc.name, () => runScene(sc.name))}>
          {sc.name.replace(/^./, (c) => c.toUpperCase()).replace("i'm", "I'm")}
        </button>
      {/each}
    </div>
  {/if}

  {#if $found.length}
    <section class="found">
      <h2>New devices</h2>
      {#each $found as f (f.key)}
        <div class="found-row">
          <div>
            <div class="name">{f.name}</div>
            <div class="sub">{f.model} · {f.suggested_room ?? 'No room'}</div>
          </div>
          <button class="add" disabled={busy.has(f.key)} onclick={() => add(f)}>Add</button>
        </div>
      {/each}
    </section>
  {:else if scanned}
    <div class="note">No new devices found.</div>
  {/if}

  {#if !$devices.length && !$found.length}
    <div class="empty">
      No devices yet. Tap <b>Find devices</b>, or say “Hey Taco, find new devices.”
    </div>
  {/if}

  {#each rooms as [room, list] (room)}
    <section>
      <div class="room-head">
        <h2>{room}</h2>
        {#if list.length > 1}
          <div class="room-actions">
            <button onclick={() => act(room, () => setRoomPower(list.map((d) => d.id), true))}>All on</button>
            <button onclick={() => act(room, () => setRoomPower(list.map((d) => d.id), false))}>All off</button>
          </div>
        {/if}
      </div>
      <div class="grid">
        {#each list as d (d.id)}
          <div class="tile" class:on={d.state.on} class:offline={d.state.online === false}>
            <button class="toggle" disabled={busy.has(d.id) || d.state.online === false}
                    onclick={() => act(d.id, () => setPower(d.id, !d.state.on))}>
              <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor"
                   stroke-width="2" stroke-linecap="round" aria-hidden="true">
                {#if d.kind === 'plug'}
                  <path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0zM12 17v5" />
                {:else}
                  <path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.1V17h6v-.2c0-.8.4-1.6 1-2.1A7 7 0 0 0 12 2z" />
                {/if}
              </svg>
              <div class="tile-name">{shortName(d)}</div>
              <div class="tile-state">
                {d.state.online === false ? 'Not responding' : d.state.on ? (d.state.level != null ? `On · ${d.state.level}%` : 'On') : 'Off'}
              </div>
            </button>
            {#if d.capabilities.includes('level') && d.state.online !== false}
              <input type="range" min="1" max="100" value={d.state.level ?? 100}
                     onchange={(e) => act(d.id, () => setLevel(d.id, Number(e.currentTarget.value)))} />
            {/if}
          </div>
        {/each}
      </div>
    </section>
  {/each}
</div>

<style>
  .sheet {
    position: fixed;
    inset: 0;
    z-index: 40;
    background: var(--bg);
    padding: 20px 28px 30px;
    overflow-y: auto;
  }
  header { display: flex; justify-content: space-between; align-items: center; }
  h1 { font-size: 30px; font-weight: 600; margin: 0; }
  h2 { font-size: 18px; font-weight: 600; color: var(--muted); margin: 0; text-transform: uppercase; letter-spacing: 0.06em; }
  .actions { display: flex; gap: 10px; }
  .actions button { padding: 12px 22px; font-size: 18px; }
  .done { background: var(--accent); color: #1a1205; font-weight: 600; }
  section { margin-top: 22px; }
  .room-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  .room-actions { display: flex; gap: 8px; }
  .room-actions button { font-size: 15px; padding: 8px 14px; }
  .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
  .tile {
    background: var(--panel);
    border-radius: var(--radius);
    border: 1px solid transparent;
    overflow: hidden;
  }
  .tile.on { background: var(--accent-soft); border-color: rgba(245, 165, 36, 0.5); }
  .tile.on .toggle svg { color: var(--accent); }
  .tile.offline { opacity: 0.5; }
  .toggle {
    width: 100%;
    background: transparent;
    text-align: left;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    color: var(--text);
  }
  .toggle svg { color: var(--muted); }
  .tile-name { font-size: 19px; font-weight: 600; line-height: 1.2; }
  .tile-state { font-size: 15px; color: var(--muted); }
  .tile.on .tile-state { color: var(--accent); }
  input[type='range'] { width: calc(100% - 32px); margin: 0 16px 14px; accent-color: var(--accent); }
  .scenes { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
  .scenes button { font-size: 17px; padding: 10px 18px; background: var(--panel-2); }
  .found { background: var(--panel); border-radius: var(--radius); padding: 16px 18px; }
  .found h2 { margin-bottom: 8px; }
  .found-row { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-top: 1px solid #22304d; }
  .found-row:first-of-type { border-top: 0; }
  .name { font-size: 20px; }
  .sub { font-size: 15px; color: var(--muted); }
  .add { background: var(--accent); color: #1a1205; font-weight: 600; padding: 10px 26px; }
  .note, .empty { color: var(--muted); font-size: 19px; margin-top: 20px; }
  .empty { margin-top: 60px; text-align: center; line-height: 1.6; }
  .error { color: #f87171; margin-top: 12px; }
</style>
