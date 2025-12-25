<script lang="ts">
	/**
	 * ActivityTabs - Tab navigation bar for the Activity Panel
	 *
	 * Tabs:
	 * - Threads: Recent sessions, history
	 * - Agents: Background agents (running + completed)
	 * - Studio: Image/video generations
	 * - Info: Session stats, costs, tokens
	 */

	import { MessageSquare, Bot, Palette, Info } from 'lucide-svelte';
	import type { ActivityTabType } from './ActivityPanel.svelte';

	interface TabConfig {
		id: ActivityTabType;
		label: string;
		icon: typeof MessageSquare;
	}

	interface Props {
		activeTab?: ActivityTabType;
		badges?: Partial<Record<ActivityTabType, number | undefined>>;
		onTabChange?: (tab: ActivityTabType) => void;
	}

	let {
		activeTab = 'threads',
		badges = {},
		onTabChange
	}: Props = $props();

	const tabs: TabConfig[] = [
		{ id: 'threads', label: 'Threads', icon: MessageSquare },
		{ id: 'agents', label: 'Agents', icon: Bot },
		{ id: 'studio', label: 'Studio', icon: Palette },
		{ id: 'info', label: 'Info', icon: Info }
	];

	function handleTabClick(tab: ActivityTabType) {
		onTabChange?.(tab);
	}
</script>

<div class="activity-tabs">
	{#each tabs as tab (tab.id)}
		<button
			class="tab-button"
			class:active={activeTab === tab.id}
			onclick={() => handleTabClick(tab.id)}
			aria-selected={activeTab === tab.id}
			role="tab"
		>
			<svelte:component this={tab.icon} size={14} strokeWidth={1.5} />
			<span class="tab-label">{tab.label}</span>
			{#if badges[tab.id]}
				<span class="tab-badge">{badges[tab.id]}</span>
			{/if}
		</button>
	{/each}
</div>

<style>
	.activity-tabs {
		display: flex;
		border-bottom: 1px solid var(--border);
		padding: 0 8px;
		flex-shrink: 0;
		background: var(--card);
	}

	.tab-button {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 10px 8px;
		background: transparent;
		border: none;
		border-bottom: 2px solid transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 0.15s ease;
		font-size: 0.75rem;
		font-weight: 500;
		position: relative;
	}

	.tab-button:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	.tab-button.active {
		color: var(--foreground);
		border-bottom-color: var(--primary);
	}

	.tab-button :global(svg) {
		flex-shrink: 0;
	}

	.tab-label {
		display: none;
	}

	/* Show labels on wider panels */
	@media (min-width: 300px) {
		.tab-label {
			display: inline;
		}
	}

	.tab-badge {
		min-width: 16px;
		height: 16px;
		padding: 0 4px;
		background: color-mix(in oklch, var(--primary) 20%, transparent);
		color: var(--primary);
		border-radius: 8px;
		font-size: 0.625rem;
		font-weight: 700;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.tab-button.active .tab-badge {
		background: var(--primary);
		color: var(--primary-foreground);
	}
</style>
