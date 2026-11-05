<script lang="ts">
 import { Check, Minus } from 'lucide-svelte';
 let { checked = $bindable(false), indeterminate = false, disabled = false, block = false, label, onchange, children }:
 { checked?: boolean; indeterminate?: boolean; disabled?: boolean; block?: boolean; label?: string; onchange?: (checked: boolean) => void; children?: import('svelte').Snippet } = $props();
</script>
<label class="ui-checkbox" class:disabled class:block>
 <span class="control">
  <input type="checkbox" bind:checked {indeterminate} {disabled} aria-label={label}
   onchange={(event) => { checked = event.currentTarget.checked; onchange?.(checked); }} />
  <span class="mark" aria-hidden="true">{#if indeterminate}<Minus size={12} />{:else if checked}<Check size={12} />{/if}</span>
 </span>
 {#if children}<span class="copy">{@render children()}</span>{/if}
</label>
<style>
 .ui-checkbox { display: inline-flex; align-items: center; gap: 8px; min-height: 32px; color: var(--foreground); font: inherit; font-size: 13px; text-align: left; cursor: pointer; min-width: 0; max-width: 100%; }
 .block { width: 100%; }
 .copy { display: block; min-width: 0; flex: 1; }
 .control { position: relative; display: inline-flex; width: 16px; height: 16px; flex: none; }
 input { appearance: none; width: 16px; height: 16px; margin: 0; border: 1px solid var(--field-border); border-radius: 4px; background: var(--field-bg); cursor: inherit; }
 input:checked, input:indeterminate { background: var(--action); border-color: var(--action); }
 .mark { position: absolute; inset: 0; display: grid; place-items: center; color: var(--action-foreground); pointer-events: none; }
 .ui-checkbox:hover:not(.disabled) input { border-color: var(--muted-foreground); }
 input:focus-visible { outline: 2px solid var(--ring); outline-offset: 3px; }
 .ui-checkbox:active:not(.disabled) { opacity: .8; }
 .disabled { opacity: .5; cursor: not-allowed; }
 @media (max-width: 600px) { .ui-checkbox { min-height: 44px; } }
</style>
