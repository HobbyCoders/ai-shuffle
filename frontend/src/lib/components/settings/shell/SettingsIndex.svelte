<script lang="ts">
	/**
	 * SettingsIndex: title, search, then the nine categories in three groups.
	 * A category with something to look at shows a dot and count (the
	 * module's `attention()`). Up/Down move between categories.
	 */
	import { ChevronRight } from 'lucide-svelte';
	import { CATEGORIES, CATEGORY_GROUPS } from '$lib/settings/categories';
	import { MODULES } from '$lib/settings/registry';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import type { Attention, CategoryId } from '$lib/settings/types';
	import StatusDot from '../rows/StatusDot.svelte';
	import SettingsSearch from './SettingsSearch.svelte';

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

<nav class="st-index st-scroll" aria-label="Settings">
	<h1 class="st-ix-title">Settings</h1>
	<SettingsSearch bind:query />
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
							onclick={() => settingsNav.selectCategory(cat.id)}
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

<style>
	.st-index {
		border-right: 1px solid var(--st-border);
		display: flex;
		flex-direction: column;
		padding: 16px 10px 12px;
		min-height: 0;
		overflow-y: auto;
		scrollbar-width: none;
	}
	.st-ix-title {
		font-size: 15px;
		font-weight: 600;
		letter-spacing: -0.01em;
		padding: 2px 10px 12px;
		margin: 0;
	}
	.st-nav-g {
		display: flex;
		align-items: center;
		gap: 7px;
		font-size: 12px;
		color: var(--st-subtle);
		padding: 14px 10px 5px;
	}
	.st-nav {
		display: grid;
		gap: 1px;
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.st-nav-item {
		display: flex;
		align-items: center;
		gap: 10px;
		height: 34px;
		padding: 0 10px;
		border-radius: var(--st-radius-md);
		border: 0;
		background: none;
		color: var(--st-mfg);
		font-size: 13.5px;
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
	.st-go {
		display: none;
		color: var(--st-subtle);
		margin-left: auto;
	}
	.cnt + .st-go {
		margin-left: 4px;
	}
</style>
