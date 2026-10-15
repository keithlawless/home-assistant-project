<script>
  // Notification cards, stacked top right, newest first. They stay until
  // dismissed (×), or all at once with "Dismiss all".
  import { notifications, dismissNotification, dismissAllNotifications } from './store.js';

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
    {#if $notifications.length > 1}
      <button class="all" onclick={() => dismissAllNotifications().catch(() => {})}>Dismiss all</button>
    {/if}
    <div class="cards">
      {#each $notifications as n (n.id)}
        <div class="card {n.kind}" class:resolved={n.resolved}>
          <svg class="icon" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor"
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
            <div class="title">
              {n.title}{#if n.count > 1}<span class="count">×{n.count}</span>{/if}
            </div>
            {#if n.body}<div class="body">{n.body}</div>{/if}
            <div class="meta">
              {#if n.sender}{n.sender} · {/if}{clock(n.updated)}
              {#if n.resolved} · back to normal at {clock(n.resolved)}{/if}
            </div>
          </div>
          <button class="x" aria-label="Dismiss" onclick={(e) => dismiss(e, n.id)}>
            <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.4"
                 stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
          </button>
        </div>
      {/each}
    </div>
  </aside>
{/if}

<style>
  .stack {
    position: fixed; top: 12px; right: 16px; z-index: 70;
    width: 380px; max-height: calc(100% - 24px);
    display: flex; flex-direction: column; align-items: flex-end; gap: 8px;
  }
  .cards { display: flex; flex-direction: column; gap: 8px; overflow-y: auto; width: 100%; }
  .all { font-size: 15px; padding: 6px 14px; background: var(--panel); box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5); }
  .card {
    display: flex; align-items: flex-start; gap: 12px;
    background: var(--panel-2); border-left: 5px solid #60a5fa; border-radius: 14px;
    padding: 12px 8px 12px 14px; box-shadow: 0 6px 22px rgba(0, 0, 0, 0.55);
  }
  .card.camera { border-left-color: var(--accent); }
  .card.warning { border-left-color: #f87171; }
  .card.health { border-left-color: #f87171; }
  .card.resolved { border-left-color: #4ade80; opacity: 0.85; }
  .icon { flex: none; margin-top: 2px; color: var(--muted); }
  .camera .icon { color: var(--accent); }
  .warning .icon, .health .icon { color: #f87171; }
  .resolved .icon { color: #4ade80; }
  .text { flex: 1; min-width: 0; }
  .title { font-size: 19px; font-weight: 600; overflow-wrap: anywhere; }
  .count { margin-left: 8px; font-size: 15px; font-weight: 500; color: var(--accent); }
  .body { font-size: 16px; margin-top: 2px; overflow-wrap: anywhere; }
  .meta { font-size: 14px; color: var(--muted); margin-top: 4px; }
  .x { flex: none; background: transparent; color: var(--muted); padding: 6px; }
</style>
