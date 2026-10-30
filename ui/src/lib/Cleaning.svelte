<script>
  // Cleaning mode ("Hey Taco, cleaning mode"): a clear shield over the whole
  // screen swallows every touch so wiping the glass can't press anything.
  // Everything underneath keeps updating, and voice still works.
  import { cleaningUntil } from './store.js';

  let left = $state(0);

  $effect(() => {
    const until = $cleaningUntil;
    const tick = () => {
      left = Math.max(0, Math.ceil((until - Date.now()) / 1000));
      if (!left && until) cleaningUntil.set(0);
    };
    tick();
    if (!until) return;
    const id = setInterval(tick, 250);
    return () => clearInterval(id);
  });

  function swallow(e) {
    e.preventDefault();
    e.stopPropagation();
  }
</script>

{#if left > 0}
  <div class="shield" role="presentation" onpointerdown={swallow} onpointerup={swallow} onclick={swallow}
       ontouchstart={swallow} ontouchmove={swallow} onwheel={swallow} oncontextmenu={swallow}>
    <div class="pill">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9zM19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9z" />
      </svg>
      Cleaning mode · touch is off · <span class="secs">{left}</span>
    </div>
  </div>
{/if}

<style>
  .shield {
    position: fixed;
    inset: 0;
    z-index: 200;   /* above night mode, the screensaver and ringing alerts */
    touch-action: none;
    background: transparent;
  }
  .pill {
    position: absolute; top: 12px; left: 50%; transform: translateX(-50%);
    display: flex; align-items: center; gap: 8px;
    background: #0e7490; color: #ecfeff; font-size: 16px; padding: 8px 16px; border-radius: 999px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
  }
  .secs { font-variant-numeric: tabular-nums; font-weight: 600; min-width: 2ch; text-align: right; }
</style>
