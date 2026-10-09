<script>
  // Burn-in protection: near-black screen with a dim clock that moves every
  // minute. The touch that wakes the screen is swallowed here so it can't
  // also press a button underneath.
  import { onMount } from 'svelte';
  import WeatherIcon from './WeatherIcon.svelte';
  import { weather, settings } from './store.js';
  import { now, formatTime } from './time.js';

  let { onWake } = $props();

  let x = $state(40);
  let y = $state(40);
  let visible = $state(true);

  function move() {
    visible = false;
    setTimeout(() => {
      x = 5 + Math.random() * 60;   // % of width; keeps the block on screen
      y = 5 + Math.random() * 65;   // % of height
      visible = true;
    }, 1500);
  }

  onMount(() => {
    move();
    const id = setInterval(move, 60_000);
    return () => clearInterval(id);
  });

  function swallow(e) {
    e.preventDefault();
    e.stopPropagation();
  }

  function wake(e) {
    swallow(e);
    onWake();
  }
</script>

<!-- pointerdown is swallowed; click (which fires on this same element) wakes. -->
<div class="saver" role="presentation" onpointerdown={swallow} onclick={wake}>
  <div class="block" class:visible style="left: {x}%; top: {y}%">
    <div class="time">{formatTime($now, $settings?.clock_24h)}</div>
    {#if $weather}
      <div class="wx">
        <WeatherIcon icon={$weather.current.icon} size={34} />
        {$weather.current.temp}{$weather.units.temp.slice(0, 1)}
      </div>
    {/if}
  </div>
</div>

<style>
  .saver {
    position: fixed;
    inset: 0;
    background: #000;
    z-index: 100;
  }
  .block {
    position: absolute;
    color: #5b6578;
    opacity: 0;
    transition: opacity 1.5s ease;
  }
  .block.visible { opacity: 1; }
  .time { font-size: 72px; font-weight: 200; line-height: 1; }
  .wx {
    display: flex; align-items: center; gap: 8px;
    font-size: 26px; margin-top: 6px;
    filter: saturate(0.3) brightness(0.55);
  }
</style>
