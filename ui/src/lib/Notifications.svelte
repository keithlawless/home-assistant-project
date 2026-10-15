<script>
  // Notification cards, one line each, in the band above the hourly forecast
  // (Home.svelte places them); more than one stacks down over the forecast.
  // Newest first. Tap a card to read all of it; they stay until dismissed (×),
  // or all at once with "Dismiss all".
  import { notifications, dismissNotification, dismissAllNotifications } from './store.js';

  let open = $state(null);   // id of the card showing its full text

  function clock(epoch) {
    return new Date(epoch * 1000).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  }

  function dismiss(e, id) {
    e.stopPropagation();
    dismissNotification(id).catch(() => {});
  }
</script>

{#if $notifications.length}
  <aside class="stack">
    <div class="cards">
    {#each $notifications as n (n.id)}
      <div class="card {n.kind}" class:resolved={n.resolved} class:open={open === n.id}
           role="button" tabindex="0" onclick={() => (open = open === n.id ? null : n.id)}
           onkeydown={(e) => e.key === 'Enter' && (open = open === n.id ? null : n.id)}>
        <svg class="icon" viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          {#if n.kind === 'camera'}
            <path d="M23 7l-7 5 7 5zM1 5h15v14H1z" />
          {:else if n.kind === 'health'}
            <path d="M14 14.8V4a2 2 0 0 0-4 0v10.8a4 4 0 1 0 4 0zM12 10v7" />
          {:else if n.kind === 'warning'}
            <path d="M12 3 2 20h20zM12 10v4M12 17h.01" />
          {:else}
            <circle cx="12" cy="12" r="10" /><path d="M12 16v-4M12 8h.01" />
          {/if}
        </svg>
        <div class="text">
          <span class="title">{n.title}</span>{#if n.count > 1}<span class="count">×{n.count}</span>{/if}
          {#if n.body}<span class="body">{n.body}</span>{/if}
        </div>
        <div class="meta">
          {#if n.resolved}back to normal {clock(n.resolved)}{:else}{#if n.sender}{n.sender} · {/if}{clock(n.updated)}{/if}
        </div>
        <button class="x" aria-label="Dismiss" onclick={(e) => dismiss(e, n.id)}>
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.4"
               stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
        </button>
      </div>
    {/each}
    </div>
    {#if $notifications.length > 1}
      <button class="all" onclick={() => dismissAllNotifications().catch(() => {})}>
        Dismiss all ({$notifications.length})
      </button>
    {/if}
  </aside>
{/if}

<style>
  .stack {
    position: absolute; top: 0; left: 0; right: 0; z-index: 70;
    display: flex; flex-direction: column; align-items: flex-end; gap: 8px;
  }
  .cards {
    width: 100%; max-height: 262px; overflow-y: auto;
    background: var(--bg); border-radius: 12px;   /* hide the forecast between cards */
    display: flex; flex-direction: column; gap: 6px;
  }
  .card {
    display: flex; align-items: center; gap: 12px; min-height: 48px; flex: none;
    background: var(--panel-2); border-left: 5px solid #60a5fa; border-radius: 12px;
    padding: 4px 4px 4px 14px; box-shadow: 0 6px 22px rgba(0, 0, 0, 0.55);
  }
  .card.camera { border-left-color: var(--accent); }
  .card.warning, .card.health { border-left-color: #f87171; }
  .card.resolved { border-left-color: #4ade80; }
  .icon { flex: none; color: var(--muted); }
  .camera .icon { color: var(--accent); }
  .warning .icon, .health .icon { color: #f87171; }
  .resolved .icon { color: #4ade80; }
  .text { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .open .text { white-space: normal; overflow-wrap: anywhere; padding: 6px 0; }
  .title { font-size: 19px; font-weight: 600; }
  .count { margin-left: 8px; font-size: 16px; font-weight: 500; color: var(--accent); }
  .body { margin-left: 12px; font-size: 17px; color: var(--muted); }
  .meta { flex: none; font-size: 15px; color: var(--muted); }
  .x { flex: none; background: transparent; color: var(--muted); padding: 10px; }
  .all {
    flex: none; font-size: 15px; padding: 6px 14px;
    background: var(--panel); box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
  }
</style>
