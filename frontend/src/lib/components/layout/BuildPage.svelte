<script lang="ts">
	/**
	 * BuildPage — the unified "Build" surface (Studio-v2 design).
	 *
	 * One page that hosts the agent-configuration tools (Profiles / Subagents /
	 * MCP / Plugins / Hooks) under a single heading + underline sub-nav. Each
	 * section renders a purpose-built, *header-less* UI so nothing stacks.
	 *
	 * Every section is now native: a card grid for Profiles and compact `.lrow`
	 * lists for Subagents / MCP / Plugins / Hooks (each reuses its card's data +
	 * editor and only replaces the list view). The editor takes the full canvas
	 * via onDetailChange, which hides this page's heading + sub-nav.
	 *
	 * Styled with the app's existing theme tokens, not the mockup palette.
	 */
	import { browser } from '$app/environment';
	import { untrack } from 'svelte';
	import { Users, Bot, Server, Puzzle, Webhook } from 'lucide-svelte';
	import ProfileCard from '$lib/components/deck/cards/ProfileCard.svelte';
	import SubagentCard from '$lib/components/deck/cards/SubagentCard.svelte';
	import McpManagerCard from '$lib/components/deck/cards/McpManagerCard.svelte';
	import BuildPluginList from '$lib/components/layout/build/BuildPluginList.svelte';
	import HooksCard from '$lib/components/deck/cards/HooksCard.svelte';

	type BuildSub = 'profile' | 'subagent' | 'mcp' | 'plugins' | 'hooks';

	interface Props {
		/** Deep-link target (e.g. "Configure agents →", "Manage plugins"). */
		requestedSub?: BuildSub;
	}
	let { requestedSub }: Props = $props();

	const STORAGE_KEY = 'ai-shuffle-build-subtab';
	const SUBS: BuildSub[] = ['profile', 'subagent', 'mcp', 'plugins', 'hooks'];

	const items: { id: BuildSub; label: string; icon: typeof Users }[] = [
		{ id: 'profile', label: 'Profiles', icon: Users },
		{ id: 'subagent', label: 'Subagents', icon: Bot },
		{ id: 'mcp', label: 'MCP Servers', icon: Server },
		{ id: 'plugins', label: 'Plugins', icon: Puzzle },
		{ id: 'hooks', label: 'Hooks', icon: Webhook }
	];

	// Every section is now a native, header-less UI (scroll inside the page body).
	const NATIVE = new Set<BuildSub>(['profile', 'subagent', 'mcp', 'plugins', 'hooks']);

	function loadStored(): BuildSub {
		if (!browser) return 'profile';
		try {
			const v = localStorage.getItem(STORAGE_KEY);
			return v && SUBS.includes(v as BuildSub) ? (v as BuildSub) : 'profile';
		} catch {
			return 'profile';
		}
	}

	let sub = $state<BuildSub>(untrack(() => requestedSub) ?? loadStored());
	// True while a section shows a full-canvas detail/editor (e.g. profile form),
	// in which case we hide the page heading + sub-nav so the editor takes over.
	let detail = $state(false);

	// Honor deep-link requests; reset detail when the target changes.
	$effect(() => {
		if (requestedSub) {
			sub = requestedSub;
			detail = false;
		}
	});

	// Remember the last-open tool so the top-bar "Build" tab reopens it.
	$effect(() => {
		const current = sub;
		if (browser) {
			try { localStorage.setItem(STORAGE_KEY, current); } catch { /* ignore */ }
		}
	});

	function select(next: BuildSub) {
		if (next === sub) return;
		sub = next;
		detail = false;
	}

	const gridMode = $derived(NATIVE.has(sub) && !detail);
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
						onclick={() => select(item.id)}
					>
						<item.icon size={15} strokeWidth={1.7} />
						<span>{item.label}</span>
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
				<ProfileCard embedded onDetailChange={(d) => (detail = d)} />
			{:else if sub === 'subagent'}
				<SubagentCard embedded onDetailChange={(d) => (detail = d)} />
			{:else if sub === 'mcp'}
				<McpManagerCard embedded onDetailChange={(d) => (detail = d)} />
			{:else if sub === 'plugins'}
				<BuildPluginList />
			{:else if sub === 'hooks'}
				<HooksCard embedded onDetailChange={(d) => (detail = d)} />
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
		.bp-subnav { overflow-x: auto; flex-wrap: nowrap; -webkit-overflow-scrolling: touch; }
		.bp-tab { flex-shrink: 0; }
	}
</style>
