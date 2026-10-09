<script>
  // Hands-free recipe mode: the current step large, ingredients alongside.
  // Voice: "next step", "go back", "repeat that", "read the ingredients",
  // "close the recipe". Opened by Hermes's show_recipe tool.
  import { recipe, recipeStep, closeRecipe } from './store.js';

  let r = $derived($recipe);
</script>

{#if r}
  <div class="recipe">
    <header>
      <div>
        <h1>{r.title}</h1>
        {#if r.servings}<div class="sub">Serves {r.servings}</div>{/if}
      </div>
      <button class="close" onclick={closeRecipe}>Close</button>
    </header>

    <div class="body">
      <aside>
        <h2>Ingredients</h2>
        <ul>
          {#each r.ingredients as item}<li>{item}</li>{/each}
        </ul>
      </aside>

      <section class="step">
        <div class="count">Step {r.step + 1} of {r.steps.length}</div>
        <p>{r.steps[r.step]}</p>
        <div class="dots">
          {#each r.steps as _, i}<span class:done={i < r.step} class:now={i === r.step}></span>{/each}
        </div>
        <div class="nav">
          <button disabled={r.step === 0} onclick={() => recipeStep(-1)}>Back</button>
          <button class="next" disabled={r.step === r.steps.length - 1} onclick={() => recipeStep(1)}>Next</button>
        </div>
      </section>
    </div>
  </div>
{/if}

<style>
  .recipe { position: fixed; inset: 0; z-index: 45; background: var(--bg); padding: 18px 24px; display: flex; flex-direction: column; }
  header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
  h1 { font-size: 28px; font-weight: 600; margin: 0; line-height: 1.2; }
  .sub { color: var(--muted); font-size: 16px; margin-top: 2px; }
  .close { padding: 10px 22px; font-size: 18px; flex-shrink: 0; }
  .body { flex: 1; display: grid; grid-template-columns: 300px 1fr; gap: 20px; margin-top: 14px; min-height: 0; }
  aside { background: var(--panel); border-radius: var(--radius); padding: 14px 18px; overflow-y: auto; }
  h2 { font-size: 15px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin: 0 0 8px; }
  ul { margin: 0; padding-left: 20px; font-size: 18px; line-height: 1.45; }
  .step { display: flex; flex-direction: column; min-height: 0; }
  .count { color: var(--accent); font-size: 20px; font-weight: 600; }
  p { flex: 1; font-size: 34px; line-height: 1.35; margin: 10px 0; overflow-y: auto; }
  .dots { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
  .dots span { width: 12px; height: 12px; border-radius: 50%; background: var(--panel-2); }
  .dots .done { background: rgba(245, 165, 36, 0.45); }
  .dots .now { background: var(--accent); }
  .nav { display: flex; gap: 12px; }
  .nav button { flex: 1; font-size: 22px; padding: 16px; }
  .nav .next { background: var(--accent); color: #1a1205; font-weight: 700; }
  .nav button:disabled { opacity: 0.35; }
</style>
