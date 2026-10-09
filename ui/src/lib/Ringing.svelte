<script>
  // Full-screen alert while a timer, alarm or reminder is ringing.
  // A tap anywhere stops it (and the sound in taco-voice).
  import { timers, stopAll } from './store.js';

  let ringing = $derived($timers.filter((t) => t.state === 'ringing'));

  function title(t) {
    if (t.kind === 'reminder') return 'Reminder';
    if (t.kind === 'alarm') return t.label ? `${t.label} alarm` : 'Alarm';
    return t.label ? `${t.label} timer` : 'Timer';
  }

  function stop(e) {
    e.preventDefault();
    e.stopPropagation();
    stopAll();
  }
</script>

{#if ringing.length}
  <div class="ringing" role="presentation" onpointerdown={(e) => e.stopPropagation()} onclick={stop}>
    <div class="pulse"></div>
    {#each ringing as t (t.id)}
      <div class="title">{title(t)}</div>
      <div class="sub">{t.kind === 'reminder' ? t.message : t.kind === 'alarm' ? 'Time to get up' : 'Done!'}</div>
    {/each}
    <button class="stop">Stop</button>
  </div>
{/if}

<style>
  .ringing {
    position: fixed;
    inset: 0;
    z-index: 120;
    background: radial-gradient(circle at center, #3a2306, #0b1220 70%);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 6px;
    text-align: center;
  }
  .pulse {
    position: absolute;
    width: 420px;
    height: 420px;
    border-radius: 50%;
    border: 4px solid var(--accent);
    animation: ring 1.4s ease-out infinite;
  }
  @keyframes ring {
    from { transform: scale(0.6); opacity: 0.9; }
    to { transform: scale(1.4); opacity: 0; }
  }
  .title { font-size: 64px; font-weight: 600; text-transform: capitalize; }
  .sub { font-size: 28px; color: var(--accent); max-width: 800px; }
  .stop {
    margin-top: 30px;
    background: var(--accent);
    color: #1a1205;
    font-size: 34px;
    font-weight: 700;
    padding: 18px 90px;
    border-radius: 999px;
  }
</style>
