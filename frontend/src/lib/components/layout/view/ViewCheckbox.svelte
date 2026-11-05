<script lang="ts">
	/** Shared table selection control. A button keeps row click/keyboard events isolated. */
	import { Check, Minus } from 'lucide-svelte';
	let { checked = false, mixed = false, label, disabled = false, onchange }: {
		checked?: boolean; mixed?: boolean; label: string; disabled?: boolean; onchange: (checked: boolean) => void;
	} = $props();
</script>
<button type="button" class="view-checkbox" role="checkbox" aria-label={label}
	aria-checked={mixed ? 'mixed' : checked} {disabled}
	onclick={(e) => { e.stopPropagation(); onchange(!checked); }} onkeydown={(e) => e.stopPropagation()}>
	<span class:checked={checked || mixed}>{#if mixed}<Minus size={12} />{:else if checked}<Check size={12} />{/if}</span>
</button>
<style>
	.view-checkbox { display: inline-grid; place-items: center; width: 32px; height: 32px; flex: none; border: 0; background: transparent; color: var(--st-fg); cursor: pointer; padding: 0; }
	span { display: grid; place-items: center; width: 15px; height: 15px; border: 1px solid var(--st-border-strong); border-radius: 4px; }
	.checked { background: var(--st-action); color: var(--st-action-fg); border-color: var(--st-action); }
	.view-checkbox:hover span { border-color: var(--st-mfg); }
	.view-checkbox:active { opacity: .7; }
	.view-checkbox:disabled { opacity: .45; cursor: not-allowed; }
	@media (max-width: 600px), (pointer: coarse) { .view-checkbox { width: 44px; height: 44px; } }
</style>
