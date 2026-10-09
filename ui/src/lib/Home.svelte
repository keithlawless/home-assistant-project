<script>
  // Default screen: current weather, clock, next hours and the coming days.
  // Laid out for the 1024x600 Kogoda panel.
  import WeatherIcon from './WeatherIcon.svelte';
  import TimerChips from './TimerChips.svelte';
  import { weather, settings } from './store.js';
  import { now, formatTime, ampm, formatDate, hourLabel, dayLabel } from './time.js';

  let { onSettings, onDevices } = $props();

  let h24 = $derived($settings?.clock_24h ?? false);
  let w = $derived($weather);
  let deg = $derived(w ? w.units.temp.slice(0, 1) : '°');
</script>

<main class="home">
  <section class="top">
    <div class="current">
      {#if w}
        <div class="now">
          <WeatherIcon icon={w.current.icon} size={130} />
          <div class="temp">{w.current.temp}<span class="deg">{deg}</span></div>
        </div>
        <div class="condition">{w.current.condition}</div>
        <div class="details">
          Feels like {w.current.feels_like}{deg}
          · H {w.today.high}{deg} L {w.today.low}{deg}
        </div>
        <div class="details muted">
          Humidity {w.current.humidity}% · Wind {w.current.wind} {w.units.wind}
          {#if w.today.precip >= 20}· Rain {w.today.precip}%{/if}
        </div>
      {:else}
        <div class="loading">Loading weather…</div>
      {/if}
    </div>

    <div class="clock">
      <div class="time">{formatTime($now, h24)}<span class="ampm">{ampm($now, h24)}</span></div>
      <div class="date">{formatDate($now)}</div>
      {#if w}<div class="place">{w.location}</div>{/if}
      <button class="devices-btn" onclick={onDevices} aria-label="Devices">
        <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z" />
        </svg>
        Devices
      </button>
      <button class="gear" onclick={onSettings} aria-label="Settings">
        <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
        </svg>
      </button>
    </div>
  </section>

  <TimerChips />

  {#if w}
    <section class="strip hourly">
      {#each w.hourly.slice(0, 8) as h, i}
        <div class="cell">
          <div class="label">{i === 0 ? 'Now' : hourLabel(h.time, h24)}</div>
          <WeatherIcon icon={h.icon} size={40} />
          <div class="value">{h.temp}{deg}</div>
          <div class="precip">{h.precip >= 20 ? `${h.precip}%` : ''}</div>
        </div>
      {/each}
    </section>

    <section class="strip daily">
      {#each w.daily.slice(0, 6) as d, i}
        <div class="cell">
          <div class="label">{dayLabel(d.date, i)}</div>
          <WeatherIcon icon={d.icon} size={40} />
          <div class="value">{d.high}{deg} <span class="low">{d.low}{deg}</span></div>
          <div class="precip">{d.precip >= 20 ? `${d.precip}%` : ''}</div>
        </div>
      {/each}
    </section>
  {/if}
</main>

<style>
  .home {
    height: 100%;
    padding: 20px 24px;
    display: grid;
    grid-template-rows: 1fr auto auto auto;
    gap: 12px;
  }
  .top { display: flex; justify-content: space-between; }

  .now { display: flex; align-items: center; gap: 8px; margin-left: -10px; }
  .temp { font-size: 120px; font-weight: 300; line-height: 1; letter-spacing: -4px; }
  .deg { font-size: 64px; vertical-align: top; color: var(--muted); }
  .condition { font-size: 30px; font-weight: 500; margin-top: 4px; }
  .details { font-size: 19px; margin-top: 6px; }
  .muted, .loading { color: var(--muted); }
  .loading { font-size: 24px; margin-top: 80px; }

  .clock { position: relative; text-align: right; padding-top: 8px; }
  .time { font-size: 92px; font-weight: 200; line-height: 1; letter-spacing: -2px; }
  .ampm { font-size: 26px; font-weight: 400; color: var(--muted); margin-left: 6px; letter-spacing: 0; }
  .date { font-size: 22px; margin-top: 6px; }
  .place { font-size: 16px; color: var(--muted); margin-top: 4px; }
  .gear {
    position: absolute; right: -8px; bottom: 0;
    background: transparent; color: var(--muted); padding: 10px;
  }
  .devices-btn {
    position: absolute; right: 52px; bottom: 4px;
    display: flex; align-items: center; gap: 8px;
    background: var(--panel); color: var(--text); font-size: 17px; padding: 8px 16px;
  }

  .strip {
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: 1fr;
    background: var(--panel);
    border-radius: var(--radius);
    padding: 8px 4px;
  }
  .cell { display: flex; flex-direction: column; align-items: center; gap: 1px; }
  .label { font-size: 15px; color: var(--muted); }
  .value { font-size: 19px; font-weight: 600; }
  .low { color: var(--muted); font-weight: 400; }
  .precip { font-size: 12px; color: #60a5fa; height: 14px; }
</style>
