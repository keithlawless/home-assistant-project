<script>
  // Hand-drawn weather icons keyed by taco-core's icon names
  // (clear, clear-night, partly, partly-night, cloudy, fog, drizzle, rain, sleet, snow, thunder).
  let { icon = 'cloudy', size = 64 } = $props();

  const CLOUD = 'M18 46a9 9 0 0 1 0-18a13 13 0 0 1 25-4a10 10 0 0 1 5 22z';
  const SMALL_CLOUD = 'M24 50a8 8 0 0 1 0-16a11 11 0 0 1 21-3a8.5 8.5 0 0 1 4 19z';
  const MOON = 'M40 10a20 20 0 1 0 14 30a16 16 0 0 1-14-30z';
  const rays = Array.from({ length: 8 }, (_, i) => (i * Math.PI) / 4);

  let base = $derived(icon.replace('-night', ''));
  let night = $derived(icon.endsWith('-night'));
  let precip = $derived(['drizzle', 'rain', 'sleet', 'snow', 'thunder'].includes(base));
</script>

<svg viewBox="0 0 64 64" width={size} height={size} aria-label={icon}>
  {#if base === 'clear'}
    {#if night}
      <path d={MOON} fill="#e2e8f0" />
    {:else}
      <circle cx="32" cy="32" r="11" fill="#f5c542" />
      {#each rays as a}
        <line x1={32 + Math.cos(a) * 16} y1={32 + Math.sin(a) * 16}
              x2={32 + Math.cos(a) * 23} y2={32 + Math.sin(a) * 23}
              stroke="#f5c542" stroke-width="4" stroke-linecap="round" />
      {/each}
    {/if}
  {:else if base === 'partly'}
    {#if night}
      <path d={MOON} fill="#e2e8f0" transform="translate(4 -4) scale(0.7)" />
    {:else}
      <circle cx="22" cy="22" r="9" fill="#f5c542" />
      {#each rays as a}
        <line x1={22 + Math.cos(a) * 13} y1={22 + Math.sin(a) * 13}
              x2={22 + Math.cos(a) * 18} y2={22 + Math.sin(a) * 18}
              stroke="#f5c542" stroke-width="3" stroke-linecap="round" />
      {/each}
    {/if}
    <path d={SMALL_CLOUD} fill="#cbd5e1" />
  {:else if base === 'fog'}
    {#each [22, 32, 42] as y, i}
      <line x1={10 + i * 4} y1={y} x2={54 - i * 4} y2={y}
            stroke="#94a3b8" stroke-width="5" stroke-linecap="round" />
    {/each}
  {:else}
    <path d={CLOUD} fill={base === 'thunder' ? '#94a3b8' : '#cbd5e1'}
          transform={precip ? 'translate(0 -6)' : ''} />
    {#if base === 'drizzle'}
      {#each [22, 32, 42] as x}
        <line x1={x} y1="46" x2={x - 2} y2="51" stroke="#60a5fa" stroke-width="3" stroke-linecap="round" />
      {/each}
    {:else if base === 'rain'}
      {#each [22, 32, 42] as x}
        <line x1={x} y1="45" x2={x - 4} y2="56" stroke="#60a5fa" stroke-width="3.5" stroke-linecap="round" />
      {/each}
    {:else if base === 'sleet'}
      <line x1="22" y1="45" x2="18" y2="56" stroke="#60a5fa" stroke-width="3.5" stroke-linecap="round" />
      <circle cx="32" cy="51" r="3" fill="#e2e8f0" />
      <line x1="42" y1="45" x2="38" y2="56" stroke="#60a5fa" stroke-width="3.5" stroke-linecap="round" />
    {:else if base === 'snow'}
      {#each [[22, 48], [32, 54], [42, 48]] as [x, y]}
        <circle cx={x} cy={y} r="3.2" fill="#e2e8f0" />
      {/each}
    {:else if base === 'thunder'}
      <path d="M34 40l-8 11h7l-4 10 11-14h-7l4-7z" fill="#facc15" />
    {/if}
  {/if}
</svg>
