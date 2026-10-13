<script lang="ts">
	/**
	 * RunsCodeList: what a plugin runs once it is on (each MCP server and hook
	 * event), in plain words with the exact command line or URL under it.
	 */
	import { Plug, Terminal, Zap } from 'lucide-svelte';
	import CodeBlock from '../rows/CodeBlock.svelte';
	import type { RunsCode } from './plugin-parts';

	interface Props {
		items: RunsCode[];
	}

	let { items }: Props = $props();
</script>

<ul class="runs" data-testid="plugin-runs-code">
	{#each items as item (item.key)}
		<li>
			<span class="ic" aria-hidden="true">
				{#if item.key.startsWith('hook:')}<Zap />{:else if item.what.startsWith('Connects')}<Plug />{:else}<Terminal />{/if}
			</span>
			<span class="tx">
				<span class="what">{item.what}<span class="nm">{item.name}</span></span>
				{#if item.detail}
					<CodeBlock code={item.detail} label="Copy {item.name}" />
				{:else}
					<span class="none">No command listed</span>
				{/if}
			</span>
		</li>
	{/each}
</ul>

<style>
	.runs {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
	}
	li {
		display: grid;
		grid-template-columns: 22px minmax(0, 1fr);
		gap: 10px;
		padding: 10px 14px;
		min-width: 0;
	}
	li + li {
		border-top: 1px solid var(--st-border);
	}
	.ic {
		display: grid;
		place-items: center;
		width: 22px;
		height: 22px;
		border-radius: var(--st-radius-sm);
		background: var(--st-muted);
		color: var(--st-mfg);
	}
	.ic :global(svg) {
		width: 13px;
		height: 13px;
	}
	.tx {
		display: grid;
		gap: 6px;
		min-width: 0;
	}
	.what {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 4px 8px;
		font-size: 13.5px;
		font-weight: 500;
	}
	.nm {
		font-family: var(--st-mono);
		font-size: 12px;
		font-weight: 400;
		color: var(--st-mfg);
		overflow-wrap: anywhere;
	}
	.none {
		font-size: 12.5px;
		color: var(--st-subtle);
	}
	.tx :global(.st-code pre) {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
</style>
