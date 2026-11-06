<script lang="ts">
	/**
	 * SettingsIndex: title, search, then the ten categories in three groups.
	 * A category with something to look at shows a dot and count (the
	 * module's `attention()`). Up/Down move between categories.
	 */
	import { ChevronLeft, ChevronRight } from 'lucide-svelte';
	import { CATEGORIES, CATEGORY_GROUPS } from '$lib/settings/categories';
	import { MODULES } from '$lib/settings/registry';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { backToChats } from '$lib/services/session-navigation';
	import { platform } from '$lib/stores/platform.svelte';
	import type { Attention, CategoryId } from '$lib/settings/types';
	import StatusDot from '../rows/StatusDot.svelte';
	import SettingsSearch from './SettingsSearch.svelte';
	import ViewPanel from '../../layout/view/ViewPanel.svelte';

	let { onkeydown: onPanelKeydown }: { onkeydown?: (event: KeyboardEvent) => void } = $props();
	let query = $state('');
	const searching = $derived(query.trim().length > 0);

	const attention = $derived.by(() => {
		const out: Partial<Record<CategoryId, Attention>> = {};
		for (const c of CATEGORIES) {
			try {
				const a = MODULES[c.id].attention?.();
				if (a && a.count > 0) out[c.id] = a;
			} catch {
				// A failing badge must not break the index.
			}
		}
		return out;
	});

	function isCurrent(id: CategoryId): boolean {
		return settingsNav.category === id && !(settingsNav.stacked && settingsNav.view === 'index');
	}

	/** Choosing the section already open returns to its list, leaving any open item. */
	function openCategory(id: CategoryId) {
		if (isCurrent(id) && !settingsNav.stacked) settingsNav.clearSelection(id);
		settingsNav.selectCategory(id);
	}

	function onkeydown(event: KeyboardEvent) {
		if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
		const from = event.currentTarget as HTMLElement;
		const nav = from.closest('nav');
		if (!nav) return;
		const items = Array.from(nav.querySelectorAll<HTMLElement>('.st-nav-item'));
		const next = items[items.indexOf(from) + (event.key === 'ArrowDown' ? 1 : -1)];
		if (next) {
			event.preventDefault();
			next.focus();
		}
	}
</script>

{#snippet footer()}
	<button type="button" class="st-chats" onclick={backToChats}>
		<ChevronLeft /><span>Back to chats</span>
	</button>
{/snippet}
<ViewPanel title="Settings" listLabel="Settings categories" onkeydown={onPanelKeydown} footer={platform.isMobile ? undefined : footer}>
	{#snippet search()}<SettingsSearch bind:query />{/snippet}
	<nav class="settings-index" aria-label="Settings">
	{#if !searching}
		{#each CATEGORY_GROUPS as group (group.id)}
			<div class="st-nav-g" id="st-nav-g-{group.id}">{group.label}</div>
			<ul class="st-nav" aria-labelledby="st-nav-g-{group.id}">
				{#each CATEGORIES.filter((c) => c.group === group.id) as cat (cat.id)}
					{@const a = attention[cat.id]}
					<li>
						<button
							type="button"
							class="st-nav-item"
							aria-current={isCurrent(cat.id) ? 'page' : undefined}
							onclick={() => openCategory(cat.id)}
							{onkeydown}
						>
							<cat.icon />
							<span class="lbl">{cat.label}</span>
							{#if a}
								<span class="cnt" title="Needs attention">
									<StatusDot tone={a.tone} />{a.count}<span class="st-vh">
										{a.count === 1 ? 'item needs' : 'items need'} attention</span
									>
								</span>
							{/if}
							<span class="st-go" aria-hidden="true"><ChevronRight /></span>
						</button>
					</li>
				{/each}
			</ul>
		{/each}
	{/if}
	</nav>
</ViewPanel>

<style>
	.settings-index {
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	.st-chats {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: none;
		height: 36px;
		padding: 0 12px;
		width: 100%;
		border: 0;
		border-radius: var(--st-radius-md);
		background: none;
		color: var(--st-mfg);
		font-size: 13px;
		text-align: left;
	}
	.st-chats:hover {
		background: var(--st-hover);
		color: var(--st-fg);
	}
	.st-nav-g {
		display: flex;
		align-items: center;
		gap: 7px;
		font-size: 12px;
		color: var(--st-subtle);
		padding: 16px 12px 6px;
	}
	.st-nav {
		display: grid;
		gap: 1px;
		list-style: none;
		margin: 0;
		padding: 0;
	}
	/* The Chats row grid (SessionCard .scard): 8px inset, the 16px icon in the
	   leading slot, a 6px gap, so labels start on the Chats title x. */
	.st-nav-item {
		display: flex;
		align-items: center;
		gap: 6px;
		height: 34px;
		padding: 0 12px 0 8px;
		border-radius: var(--st-radius-md);
		border: 0;
		background: none;
		color: var(--st-mfg);
		font-size: 13px;
		font-weight: 450;
		text-align: left;
		width: 100%;
	}
	.st-nav-item:hover {
		background: var(--st-hover);
		color: var(--st-fg);
	}
	.st-nav-item[aria-current='page'] {
		background: var(--st-accent);
		color: var(--st-fg);
		font-weight: 500;
	}
	.lbl {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.cnt {
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		gap: 5px;
		font-size: 12px;
		color: var(--st-mfg);
		font-variant-numeric: tabular-nums;
	}
	/* One phone row style: each group is a card of 48px rows with bare 18px glyphs,
	   on the same 16px edge as the sheets. */
	@media (max-width: 480px) {
		.st-nav-g {
			padding: 18px 0 6px;
		}
		.st-nav {
			flex: none;
			gap: 0;
			overflow: hidden;
			border: 1px solid var(--st-border);
			border-radius: var(--st-radius);
			background: var(--st-card);
		}
		.st-nav > li + li {
			border-top: 1px solid var(--st-border);
		}
		.st-nav-item {
			height: 48px;
			gap: 12px;
			padding: 0 14px;
			border-radius: 0;
			color: var(--st-fg);
			font-size: 15px;
		}
		.st-nav-item :global(svg:first-child) {
			width: 18px;
			height: 18px;
			color: var(--st-mfg);
		}
	}
	.st-go {
		display: none;
		color: var(--st-subtle);
		margin-left: auto;
	}
	.cnt + .st-go {
		margin-left: 4px;
	}
</style>
