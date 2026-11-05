<script lang="ts">
 import { Check, Minus } from 'lucide-svelte';
 import type { Snippet } from 'svelte';
 import type { HTMLInputAttributes } from 'svelte/elements';
 /** One native checkbox for forms, labelled choices and mixed table selection. */
 let { checked = $bindable(false), indeterminate = $bindable(false), disabled = false,
  block = false, label, onchange, children, isolate = false, ...props }:
  Omit<HTMLInputAttributes, 'type' | 'onchange' | 'children'> & {
   block?: boolean; label?: string; onchange?: (checked: boolean) => void;
   children?: Snippet; isolate?: boolean;
  } = $props();
</script>
{#snippet control()}
 <span class="control">
  <input {...props} type="checkbox" bind:checked bind:indeterminate {disabled} aria-label={label ?? props['aria-label']} aria-checked={indeterminate ? 'mixed' : checked}
   onclick={(event) => { if (isolate) event.stopPropagation(); }}
   onkeydown={(event) => { if (isolate) event.stopPropagation(); }}
   onchange={(event) => { checked = event.currentTarget.checked; onchange?.(checked); }} />
  <span class="mark" aria-hidden="true">{#if indeterminate}<Minus size={12} />{:else if checked}<Check size={12} />{/if}</span>
 </span>
{/snippet}
{#if children}
 <label class="ui-checkbox" class:disabled class:block>{@render control()}<span class="copy">{@render children()}</span></label>
{:else}
 <span class="ui-checkbox" class:disabled class:block>{@render control()}</span>
{/if}
<style>
 .ui-checkbox { display: inline-flex; align-items: center; gap: 8px; min-height: 32px; flex: none; color: var(--foreground); font: inherit; font-size: 13px; text-align: left; cursor: pointer; min-width: 0; max-width: 100%; vertical-align: middle; }
 .block { width: 100%; }
 .copy { display: block; min-width: 0; flex: 1; }
 .control { position: relative; display: inline-flex; width: 32px; height: 32px; flex: none; }
 input { appearance: none; position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; border: 0; background: transparent; cursor: inherit; }
 .mark { position: absolute; inset: 8px; display: grid; place-items: center; border: 1px solid var(--field-border); border-radius: 4px; background: var(--field-bg); color: var(--action-foreground); pointer-events: none; transition: background 150ms, border-color 150ms; }
 input:checked + .mark, input:indeterminate + .mark { background: var(--action); border-color: var(--action); }
 .ui-checkbox:hover:not(.disabled) .mark { border-color: var(--muted-foreground); }
 input:focus-visible { outline: 2px solid var(--ring); outline-offset: 0; border-radius: var(--radius-control); }
 .ui-checkbox:active:not(.disabled) .mark { transform: scale(.94); }
 .disabled { opacity: .5; cursor: not-allowed; }
 @media (max-width: 600px), (pointer: coarse) { .ui-checkbox { min-height: 44px; } .control { width: 44px; height: 44px; } .mark { inset: 14px; } }
 @media (prefers-reduced-motion: reduce) { .mark { transition: none; } }
</style>
