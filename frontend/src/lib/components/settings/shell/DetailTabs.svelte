<script lang="ts" module>
	export interface DetailTab {
		id: string;
		label?: string;
		count?: number | string;
	}
</script>

<script lang="ts">
	/**
	 * DetailTabs: tabs of one detail (role=tablist). Arrow keys, Home and End
	 * move and select. DetailPane renders it only when there are 2+ tabs.
	 */
	interface Props {
		tabs: DetailTab[];
		current: string | null;
		onselect: (id: string) => void;
		/** Accessible name, e.g. "Claude Max subscription sections". */
		label: string;
		/** Prefix for tab/panel ids. */
		idPrefix: string;
	}

	let { tabs, current, onselect, label, idPrefix }: Props = $props();
	let list: HTMLDivElement | undefined = $state();

	function onkeydown(event: KeyboardEvent) {
		const i = tabs.findIndex((t) => t.id === current);
		let next = -1;
		if (event.key === 'ArrowRight') next = (i + 1) % tabs.length;
		else if (event.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length;
		else if (event.key === 'Home') next = 0;
		else if (event.key === 'End') next = tabs.length - 1;
		if (next < 0) return;
		event.preventDefault();
		onselect(tabs[next].id);
		list?.querySelectorAll<HTMLElement>('[role="tab"]')[next]?.focus();
	}
</script>

<div class="st-tabs" role="tablist" aria-label={label} bind:this={list} tabindex="-1" {onkeydown}>
	{#each tabs as tab (tab.id)}
		<button
			type="button"
			role="tab"
			id="{idPrefix}-tab-{tab.id}"
			aria-selected={tab.id === current}
			aria-controls="{idPrefix}-panel"
			tabindex={tab.id === current ? 0 : -1}
			onclick={() => onselect(tab.id)}
		>
			{tab.label ?? tab.id}{#if tab.count !== undefined && tab.count !== ''}<span class="n">{tab.count}</span>{/if}
		</button>
	{/each}
</div>

<style>
	.st-tabs {
		display: flex;
		gap: 20px;
		border-bottom: 1px solid var(--st-border);
		margin-top: 14px;
		overflow-x: auto;
		scrollbar-width: none;
	}
	.st-tabs:focus {
		outline: none;
	}
	button {
		padding: 9px 0 10px;
		font-size: 13.5px;
		color: var(--st-mfg);
		border: 0;
		background: none;
		border-bottom: 2px solid transparent;
		margin-bottom: -1px;
		white-space: nowrap;
	}
	button:hover {
		color: var(--st-fg);
	}
	button[aria-selected='true'] {
		color: var(--st-fg);
		border-bottom-color: var(--st-fg);
		font-weight: 500;
	}
	button:focus-visible {
		outline-offset: -2px;
	}
	.n {
		font-size: 11.5px;
		color: var(--st-subtle);
		margin-left: 5px;
	}
</style>
