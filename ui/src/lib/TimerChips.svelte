<script>
  // Countdown chips for running timers and upcoming alarms/reminders.
  // Tap the × to cancel one.
  import { timers, cancelTimer } from './store.js';
  import { now } from './time.js';

  function remaining(t, nowMs) {
    return t.state === 'paused' ? t.remaining_s : Math.max(0, t.ends_at - nowMs / 1000);
  }

  function countdown(secs) {
    secs = Math.ceil(secs);
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
  }

  function clock(epoch) {
    return new Date(epoch * 1000).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  }

  function name(t) {
    const text = t.kind === 'reminder' ? t.message
      : t.kind === 'alarm' ? (t.label ? `${t.label} alarm` : 'Alarm')
      : (t.label || 'Timer');
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  let visible = $derived($timers.filter((t) => t.state !== 'ringing'));
</script>

{#if visible.length}
  <section class="chips">
    {#each visible as t (t.id)}
      <div class="chip" class:paused={t.state === 'paused'} class:soon={t.kind === 'timer' && remaining(t, $now.getTime()) < 60}>
        <svg class="kind" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
             stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
          {#if t.kind === 'timer'}
            <circle cx="12" cy="13" r="8" /><path d="M12 9v4l2.5 2.5M10 2h4" />
          {:else if t.kind === 'alarm'}
            <path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4zM10 21h4" />
          {:else}
            <circle cx="12" cy="12" r="4" fill="currentColor" />
          {/if}
        </svg>
        <span class="name">{name(t)}</span>
        <span class="time">
          {t.kind === 'timer' ? countdown(remaining(t, $now.getTime())) : clock(t.ends_at)}
        </span>
        <button aria-label="Cancel" onclick={() => cancelTimer(t.id)}>×</button>
      </div>
    {/each}
  </section>
{/if}

<style>
  .chips { display: flex; gap: 10px; overflow: hidden; }
  .chip {
    display: flex; align-items: center; gap: 8px;
    background: var(--accent-soft);
    border: 1px solid rgba(245, 165, 36, 0.4);
    border-radius: 999px;
    padding: 4px 6px 4px 14px;
    font-size: 18px;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .chip.soon { border-color: var(--accent); }
  .chip.paused { opacity: 0.6; }
  .kind { color: var(--accent); flex-shrink: 0; }
  .name { max-width: 200px; overflow: hidden; text-overflow: ellipsis; }
  .time { font-variant-numeric: tabular-nums; font-weight: 600; color: var(--accent); }
  button {
    background: transparent; color: var(--muted);
    font-size: 22px; line-height: 1; padding: 4px 8px;
  }
</style>
