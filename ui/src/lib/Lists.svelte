<script>
  // Lists sheet: shopping, to do, and any list created by voice
  // ("add batteries to the hardware store list"). Tap an item to check it off.
  import { lists, toggleItem, deleteItem, clearDone } from './store.js';

  let { onClose } = $props();
  let current = $state('shopping');

  let names = $derived(Object.keys($lists));
  let items = $derived($lists[current] ?? []);
  let doneCount = $derived(items.filter((i) => i.done).length);

  const title = (n) => (n === 'to do' ? 'To do' : n[0].toUpperCase() + n.slice(1));
</script>

<div class="sheet">
  <header>
    <h1>Lists</h1>
    <div class="actions">
      {#if doneCount}
        <button onclick={() => clearDone(current)}>Clear checked ({doneCount})</button>
      {/if}
      <button class="done" onclick={onClose}>Done</button>
    </div>
  </header>

  <div class="tabs">
    {#each names as n (n)}
      <button class:on={n === current} onclick={() => (current = n)}>
        {title(n)} <span class="count">{($lists[n] ?? []).filter((i) => !i.done).length}</span>
      </button>
    {/each}
  </div>

  {#if items.length}
    <ul>
      {#each items as item (item.id)}
        <li class:checked={item.done}>
          <button class="check" onclick={() => toggleItem(item.id, !item.done)}>
            <span class="box">
              {#if item.done}
                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="3"
                     stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L20 7" /></svg>
              {/if}
            </span>
            <span class="text">{item.text}</span>
          </button>
          <button class="remove" aria-label="Remove {item.text}" onclick={() => deleteItem(item.id)}>×</button>
        </li>
      {/each}
    </ul>
  {:else}
    <div class="empty">
      Nothing here yet. Try “Hey Taco, add milk and eggs to the {current === 'to do' ? 'to-do' : current} list.”
    </div>
  {/if}
</div>

<style>
  .sheet { position: fixed; inset: 0; z-index: 40; background: var(--bg); padding: 20px 28px; overflow-y: auto; }
  header { display: flex; justify-content: space-between; align-items: center; }
  h1 { font-size: 30px; font-weight: 600; margin: 0; }
  .actions { display: flex; gap: 10px; }
  .actions button { padding: 12px 22px; font-size: 18px; }
  .done { background: var(--accent); color: #1a1205; font-weight: 600; }
  .tabs { display: flex; gap: 8px; margin: 18px 0 12px; flex-wrap: wrap; }
  .tabs button { font-size: 18px; padding: 10px 18px; }
  .tabs .on { background: var(--accent); color: #1a1205; font-weight: 600; }
  .count { opacity: 0.7; margin-left: 4px; }
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
  li { display: flex; background: var(--panel); border-radius: 14px; }
  .check { flex: 1; display: flex; align-items: center; gap: 14px; background: transparent; text-align: left; padding: 14px 16px; font-size: 22px; }
  .box {
    width: 30px; height: 30px; border-radius: 8px; border: 2px solid var(--muted);
    display: grid; place-items: center; flex-shrink: 0; color: #1a1205;
  }
  .checked .box { background: var(--accent); border-color: var(--accent); }
  .checked .text { text-decoration: line-through; color: var(--muted); }
  .remove { background: transparent; color: var(--muted); font-size: 28px; padding: 0 20px; }
  .empty { color: var(--muted); font-size: 20px; text-align: center; margin-top: 70px; line-height: 1.6; }
</style>
