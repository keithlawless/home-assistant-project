<script>
  // "Now playing" chip for the radio, with a stop button.
  import { media, stopMedia } from './store.js';
</script>

{#if $media}
  <div class="chip">
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2"
         stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M9 18V5l12-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM21 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z" />
    </svg>
    <span class="name">{$media.station}</span>
    <button aria-label="Stop the radio" onclick={stopMedia}>
      <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
    </button>
  </div>
{/if}

<style>
  .chip {
    display: flex; align-items: center; gap: 8px; flex-shrink: 0;
    background: rgba(96, 165, 250, 0.15); border: 1px solid rgba(96, 165, 250, 0.45);
    color: #bfdbfe; border-radius: 999px; padding: 4px 6px 4px 14px; font-size: 18px;
  }
  .name { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  button { background: rgba(96, 165, 250, 0.25); color: #dbeafe; border-radius: 999px; padding: 6px 8px; display: flex; }
</style>
