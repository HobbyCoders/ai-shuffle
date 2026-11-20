<script lang="ts">
	/** A read-only roster of available models, not a picker checklist. */
	import ProviderGlyph, { type ProviderGlyphKind } from '../../ProviderGlyph.svelte';
	import StatusDot from '../../rows/StatusDot.svelte';

	let { models, emptyMessage, providerGlyph }: {
		models: readonly { id: string; label: string }[];
		emptyMessage: string;
		providerGlyph?: ProviderGlyphKind;
	} = $props();
</script>

<div class="st-card pv-card">
	<div class="pv-mlist">
		{#each models as model (model.id)}
			<div class="pv-mrow static">
				{#if providerGlyph}
					<ProviderGlyph kind={providerGlyph} />
				{:else}
					<StatusDot tone="ok" />
				{/if}
				<span class="id">{model.id}</span>
				<span class="lbl">{model.label}</span>
			</div>
		{:else}
			<p class="pv-empty">{emptyMessage}</p>
		{/each}
	</div>
</div>

<style>
	/* Keep both strings readable on touch as well as desktop. A long label
	   must leave space for the model ID in a narrow pane. */
	:global(.settings-shell) .pv-mrow {
		grid-template-columns: auto minmax(0, 1fr) fit-content(45%);
	}
	:global(.settings-shell) .pv-mrow > :is(.id, .lbl) {
		white-space: normal;
		overflow-wrap: anywhere;
	}
</style>
