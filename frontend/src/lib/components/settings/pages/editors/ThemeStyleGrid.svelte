<script lang="ts">
	/**
	 * ThemeStyleGrid: the theme presets as swatches, opened under the
	 * "Theme style" row. Swatch colors come from each preset's own data.
	 */
	import { themePresets } from '$lib/stores/theme.svelte';

	interface Props {
		/** Selected preset id, '' for the default style. */
		value: string;
		/** Which variant's colors to preview. */
		mode: 'light' | 'dark';
		onselect: (id: string) => void;
	}

	let { value, mode, onselect }: Props = $props();
</script>

<div class="grid" role="group" aria-label="Theme styles">
	<button type="button" class="swatch" aria-pressed={value === ''} onclick={() => onselect('')}>
		<span class="chips default" aria-hidden="true"><i></i><i></i><i></i></span>
		<span class="name">Default</span>
	</button>
	{#each themePresets as preset (preset.id)}
		{@const c = preset.styles[mode] ?? preset.styles.light}
		<button type="button" class="swatch" aria-pressed={value === preset.id} onclick={() => onselect(preset.id)}>
			<span class="chips" aria-hidden="true">
				<i style:background={c.primary}></i><i style:background={c.accent}></i><i style:background={c.background}></i>
			</span>
			<span class="name">{preset.label}</span>
		</button>
	{/each}
</div>

<style>
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
		gap: 6px;
		max-height: 300px;
		overflow-y: auto;
		padding: 2px;
	}
	.swatch {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		border: 1px solid var(--st-border);
		border-radius: var(--st-radius-md);
		padding: 6px 8px;
		font-size: 12.5px;
		text-align: left;
		background: none;
	}
	.swatch:hover {
		border-color: var(--st-border-strong);
	}
	.swatch[aria-pressed='true'] {
		border-color: var(--st-primary);
		box-shadow: inset 0 0 0 1px var(--st-primary);
	}
	.name {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.chips {
		display: flex;
		flex: none;
		border-radius: 3px;
		overflow: hidden;
		box-shadow: 0 0 0 1px var(--st-border);
	}
	.chips i {
		width: 9px;
		height: 18px;
		display: block;
	}
	.chips.default i:nth-child(1) {
		background: var(--st-mfg);
	}
	.chips.default i:nth-child(2) {
		background: var(--st-muted);
	}
	.chips.default i:nth-child(3) {
		background: var(--st-card);
	}
</style>
