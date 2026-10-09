<script>
  // Shows what Taco heard and is saying. Follows taco-voice's events and
  // lingers after the reply so it can be read; tap to dismiss.
  import { voiceEvents } from './store.js';

  const LINGER_MS = 10_000;
  const TOOL_LABELS = {
    web_search: 'Searching the web',
    web_extract: 'Reading a web page',
  };

  let phase = $state('idle');   // idle | listening | thinking | speaking | done
  let heard = $state('');
  let reply = $state('');
  let activity = $state('');
  let hideTimer;

  $effect(() => {
    const e = $voiceEvents;
    if (!e) return;
    clearTimeout(hideTimer);
    if (e.type === 'state') {
      if (e.state === 'listening') {
        phase = 'listening';
        heard = reply = activity = '';
      } else if (e.state === 'idle') {
        if (phase !== 'idle') {
          phase = reply ? 'done' : 'idle';
          hideTimer = setTimeout(() => (phase = 'idle'), LINGER_MS);
        }
      } else {
        phase = e.state;
      }
    } else if (e.type === 'heard') {
      heard = e.text;
    } else if (e.type === 'tool') {
      activity = e.status === 'running'
        ? (TOOL_LABELS[e.tool] ?? 'Working') + (e.label ? `: ${e.label}` : '')
        : '';
    } else if (e.type === 'reply') {
      reply = e.text;
      activity = '';
    }
  });

  let status = $derived(
    phase === 'listening' ? 'Listening…'
      : phase === 'thinking' ? (activity || 'Working…')
      : '',
  );

  function dismiss() {
    clearTimeout(hideTimer);
    phase = 'idle';
  }
</script>

{#if phase !== 'idle'}
  <div class="overlay" role="presentation" onclick={dismiss}>
    <div class="card">
      {#if status}
        <div class="status">
          <span class="pulse" class:listening={phase === 'listening'}></span>{status}
        </div>
      {/if}
      {#if heard}<div class="heard">“{heard}”</div>{/if}
      {#if reply}<div class="reply">{reply}</div>{/if}
    </div>
  </div>
{/if}

<style>
  .overlay {
    position: fixed;
    inset: 0;
    z-index: 50;
    display: flex;
    align-items: flex-end;
    padding: 20px;
    background: linear-gradient(transparent 30%, rgba(5, 9, 18, 0.85));
  }
  .card {
    width: 100%;
    max-height: 70%;
    overflow: hidden;
    background: var(--panel);
    border: 1px solid #263556;
    border-radius: var(--radius);
    padding: 20px 26px;
    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
  }
  .status { display: flex; align-items: center; gap: 12px; font-size: 22px; color: var(--accent); }
  .pulse {
    width: 14px; height: 14px; border-radius: 50%;
    background: var(--accent);
    animation: pulse 1.2s ease-in-out infinite;
  }
  .pulse.listening { animation-duration: 0.7s; }
  @keyframes pulse {
    0%, 100% { transform: scale(0.7); opacity: 0.5; }
    50% { transform: scale(1.15); opacity: 1; }
  }
  .heard { font-size: 22px; color: var(--muted); margin-top: 8px; }
  .reply { font-size: 28px; line-height: 1.35; margin-top: 10px; }
</style>
