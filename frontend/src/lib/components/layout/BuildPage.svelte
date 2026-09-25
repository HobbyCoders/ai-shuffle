<script module lang="ts">
	/**
	 * Module-scoped freshness stamp for the sub-nav count loads.
	 *
	 * BuildPage used to refire four list loads on EVERY mount (R14). It is now
	 * kept alive by MainPanel, so mounts are rare — but a deep link or a reload
	 * still remounts it, and re-fetching profiles/MCP/plugins/hooks that were
	 * read seconds ago just to redraw the same badges is wasted work. The stores
	 * keep their last value, so this is a plain stale-while-revalidate: render
	 * the cached counts immediately, revalidate only when they are actually old.
	 */
	const BUILD_COUNTS_TTL_MS = 60_000;
	let lastCountsLoadAt = 0;

	/** True when the sub-nav counts should be refetched on mount. */
	export function buildCountsAreStale(now: number = Date.now()): boolean {
		return now - lastCountsLoadAt >= BUILD_COUNTS_TTL_MS;
	}
</script>

<script lang="ts">
	/**
	 * BuildPage — the unified "Build" surface (Studio-v2 design).
	 *
	 * One page that hosts the agent-configuration tools (Agents / MCP / Plugins /
	 * Hooks) under a single heading + underline sub-nav. Each section renders a
	 * purpose-built, *header-less* UI so nothing stacks.
	 *
	 * Every section is now native: a card grid for Agents and compact `.lrow`
	 * lists for MCP / Plugins / Hooks (each reuses its card's data + editor and
	 * only replaces the list view). The editor takes the full canvas via
	 * onDetailChange, which hides this page's heading + sub-nav.
	 *
	 * Styled with the app's existing theme tokens, not the mockup palette.
	 */
	import { untrack, onMount } from 'svelte';
	import { buildPreference, type BuildSub } from '$lib/stores/build-preference.svelte';
	import { Users, Server, Puzzle, Webhook } from 'lucide-svelte';
	import { appData, profiles } from '$lib/stores/app-data.svelte';
	import { mcp, globalMcpServers } from '$lib/stores/mcp.svelte';
	import { hooks, projectHooks } from '$lib/stores/hooks.svelte';
	import LazyPanel from '$lib/components/deck/lazy/LazyPanel.svelte';
	import {
		createLazyComponent,
		type LazyComponentHandle
	} from '$lib/components/deck/lazy/lazy-component';

	// Each sub-tab is its own chunk. ProfileCard/McpManagerCard/HooksCard drag in
	// their editors, forms and validation; loading all four to show one of them
	// is what made "Build" the heaviest branch of the route bundle. The sub-nav
	// prefetches on hover/focus, so the switch is a same-frame render in
	// practice (LazyPanel renders a resolved handle synchronously).
	const SUB_PANELS: Record<BuildSub, LazyComponentHandle<unknown>> = {
		profile: createLazyComponent('build:profile', () =>
			import('$lib/components/deck/cards/ProfileCard.svelte')
		),
		mcp: createLazyComponent('build:mcp', () =>
			import('$lib/components/deck/cards/McpManagerCard.svelte')
		),
		plugins: createLazyComponent('build:plugins', () =>
			import('$lib/components/plugins/PluginsHub.svelte')
		),
		hooks: createLazyComponent('build:hooks', () =>
			import('$lib/components/deck/cards/HooksCard.svelte')
		)
	};

	interface Props {
		/** Deep-link target (e.g. "Configure agents →", "Manage plugins"). */
		requestedSub?: BuildSub;
		/** One-shot deep-link item id (agent) to open in its editor. */
		requestedItemId?: string | null;
	}
	let { requestedSub, requestedItemId }: Props = $props();

	const items: { id: BuildSub; label: string; icon: typeof Users }[] = [
		{ id: 'profile', label: 'Agents', icon: Users },
		{ id: 'mcp', label: 'MCP Servers', icon: Server },
		{ id: 'plugins', label: 'Plugins', icon: Puzzle },
		{ id: 'hooks', label: 'Hooks', icon: Webhook }
	];

	// Every section is now a native, header-less UI (scroll inside the page body).
	const NATIVE = new Set<BuildSub>(['profile', 'mcp', 'plugins', 'hooks']);

	let sub = $state<BuildSub>(untrack(() => requestedSub ?? buildPreference.current));
	// True while a section shows a full-canvas detail/editor (e.g. profile form),
	// in which case we hide the page heading + sub-nav so the editor takes over.
	let detail = $state(false);

	// Honor deep-link requests; reset detail when the target changes.
	$effect(() => {
		if (requestedSub) {
			sub = requestedSub;
			detail = false;
			untrack(() => buildPreference.set(requestedSub!));
		}
	});

	// A remote choice can update the list, but cannot unmount an open editor.
	$effect(() => {
		const preferred = buildPreference.current;
		if (!detail && !requestedItemId) sub = preferred;
	});

	function select(next: BuildSub) {
		if (next === sub) return;
		sub = next;
		detail = false;
		buildPreference.set(next);
	}

	/** Prefetch a sub-tab's chunk on hover/focus so the click is instant. */
	function prefetch(next: BuildSub) {
		SUB_PANELS[next].preload();
	}

	/** Passed to the section components; hoisted so the markup stays type-free. */
	function setDetail(next: boolean) {
		detail = next;
	}

	const gridMode = $derived(NATIVE.has(sub) && !detail);

	// Per-section counts for the sub-nav badges. Trigger lightweight loads on mount
	// so the numbers show even before a section is opened; badge hidden when 0.
	onMount(() => {
		// Warm the sub-tab the page will actually show first.
		SUB_PANELS[untrack(() => sub)].preload();
		// Stale-while-revalidate: the stores still hold the previous values, so
		// the badges render instantly; only refetch when they've gone stale.
		if (!buildCountsAreStale()) return;
		lastCountsLoadAt = Date.now();
		appData.loadProfiles();
		// Badge counts only: a failed load leaves the badge hidden.
		mcp.loadGlobalServers().catch(() => {});
		hooks.loadAppHooks().catch(() => {});
	});

	const counts = $derived<Record<BuildSub, number>>({
		profile: ($profiles ?? []).length,
		mcp: ($globalMcpServers ?? []).length,
		plugins: 0,
		hooks: ($projectHooks ?? []).length
	});
</script>

<div class="build-page">
	{#if !detail}
		<header class="bp-head">
			<div class="bp-eyebrow">Configure how your agents behave</div>
			<h1 class="bp-title">Build</h1>
			<nav class="bp-subnav" aria-label="Build tools">
				{#each items as item}
					<button
						class="bp-tab"
						class:on={sub === item.id}
						aria-current={sub === item.id ? 'page' : undefined}
						onpointerenter={() => prefetch(item.id)}
						onfocus={() => prefetch(item.id)}
						onclick={() => select(item.id)}
					>
						<item.icon size={15} strokeWidth={1.7} />
						<span>{item.label}</span>
						{#if counts[item.id] > 0}<span class="bp-tab-count">{counts[item.id]}</span>{/if}
					</button>
				{/each}
			</nav>
		</header>
	{/if}

	<div class="bp-body" class:grid={gridMode}>
		<!-- Stable inner wrapper: ProfileCard stays mounted across the grid↔form
		     toggle (rendering it in two branches would remount it → loop). -->
		<div class="bp-inner">
			{#if sub === 'profile'}
				<LazyPanel
					handle={SUB_PANELS.profile}
					label="Agents"
					props={{
						embedded: true,
						initialSelectedId: requestedItemId,
						onDetailChange: setDetail
					}}
				/>
			{:else if sub === 'mcp'}
				<LazyPanel
					handle={SUB_PANELS.mcp}
					label="MCP Servers"
					props={{ embedded: true, initialSelectedId: requestedItemId, onDetailChange: setDetail }}
				/>
			{:else if sub === 'plugins'}
				<LazyPanel handle={SUB_PANELS.plugins} props={{ initialSelectedId: requestedItemId }} label="Plugins" />
			{:else if sub === 'hooks'}
				<LazyPanel
					handle={SUB_PANELS.hooks}
					label="Hooks"
					props={{ embedded: true, onDetailChange: setDetail }}
				/>
			{/if}
		</div>
	</div>
</div>

<style>
	.build-page {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
		background: var(--background);
	}

	/* Fixed heading + sub-nav */
	.bp-head {
		flex-shrink: 0;
		padding: 22px 28px 0;
		max-width: 1240px;
		width: 100%;
		margin: 0 auto;
	}
	.bp-eyebrow {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.1em;
		color: var(--muted-foreground);
	}
	.bp-title {
		margin: 6px 0 0;
		font-family: var(--font-display, Georgia, serif);
		font-size: 1.6rem;
		font-weight: 700;
		letter-spacing: -0.02em;
		color: var(--foreground);
		line-height: 1.1;
	}

	/* Underline sub-nav */
	.bp-subnav {
		display: flex;
		gap: 2px;
		margin-top: 16px;
		border-bottom: 1px solid var(--border);
		flex-wrap: wrap;
	}
	.bp-tab {
		position: relative;
		display: inline-flex;
		align-items: center;
		gap: 7px;
		padding: 10px 14px;
		background: transparent;
		border: none;
		color: var(--muted-foreground);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		white-space: nowrap;
		transition: color 120ms ease;
	}
	.bp-tab:hover { color: var(--foreground); }
	.bp-tab.on { color: var(--foreground); }
	.bp-tab.on::after {
		content: '';
		position: absolute;
		left: 12px;
		right: 12px;
		bottom: -1px;
		height: 2px;
		border-radius: 2px;
		background: var(--primary);
	}
	.bp-tab :global(svg) { flex-shrink: 0; opacity: 0.85; }
	.bp-tab:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; border-radius: 6px; }
	.bp-tab-count {
		font-family: var(--font-mono, monospace);
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 45%, transparent);
		padding: 1px 7px;
		border-radius: 999px;
		line-height: 1.5;
	}
	.bp-tab.on .bp-tab-count { color: var(--foreground); }

	/* Body */
	.bp-body {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}
	.bp-inner { display: flex; flex-direction: column; min-height: 0; flex: 1; }

	/* Native grid sections scroll inside the page with comfortable padding. */
	.bp-body.grid { overflow-y: auto; display: block; }
	.bp-body.grid .bp-inner {
		display: block;
		max-width: 1240px;
		width: 100%;
		margin: 0 auto;
		padding: 20px 28px 48px;
	}

	/* Non-grid (editor / interim cards) fill the remaining height. */
	.bp-body:not(.grid) .bp-inner > :global(*) {
		flex: 1;
		min-height: 0;
		height: 100%;
	}

	@media (max-width: 639px) {
		.bp-head { padding: 16px 16px 0; }
		.bp-body.grid .bp-inner { padding: 14px 16px 32px; }
		.bp-subnav { overflow-x: auto; flex-wrap: nowrap; -webkit-overflow-scrolling: touch; scrollbar-width: none; }
		.bp-subnav::-webkit-scrollbar { display: none; }
		.bp-tab { flex-shrink: 0; }
	}
</style>
