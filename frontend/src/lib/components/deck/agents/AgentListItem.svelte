<script lang="ts">
	/**
	 * AgentListItem - Compact agent display for list view
	 *
	 * Features:
	 * - Status indicator (color dot)
	 * - Agent name
	 * - Brief status text
	 * - Progress bar
	 * - Duration badge
	 * - Quick actions: Pause, View, Cancel
	 * - Click to expand/view details
	 */
	import type { BackgroundAgent, AgentStatus } from '$lib/stores/agents';

	interface Props {
		agent: BackgroundAgent;
		selected?: boolean;
		onselect?: () => void;
		onpause?: () => void;
		onresume?: () => void;
		oncancel?: () => void;
		onview?: () => void;
	}

	let {
		agent,
		selected = false,
		onselect,
		onpause,
		onresume,
		oncancel,
		onview
	}: Props = $props();

	// Get status indicator info
	function getStatusInfo(status: AgentStatus): { color: string; bgColor: string; label: string; pulse: boolean } {
		switch (status) {
			case 'running':
				return {
					color: 'bg-cyan-500',
					bgColor: 'bg-cyan-500/10',
					label: 'Running',
					pulse: true
				};
			case 'queued':
				return {
					color: 'bg-amber-500',
					bgColor: 'bg-amber-500/10',
					label: 'Queued',
					pulse: false
				};
			case 'paused':
				return {
					color: 'bg-gray-400',
					bgColor: 'bg-gray-400/10',
					label: 'Paused',
					pulse: false
				};
			case 'completed':
				return {
					color: 'bg-emerald-500',
					bgColor: 'bg-emerald-500/10',
					label: 'Completed',
					pulse: false
				};
			case 'failed':
				return {
					color: 'bg-red-500',
					bgColor: 'bg-red-500/10',
					label: 'Failed',
					pulse: false
				};
			default:
				return {
					color: 'bg-gray-400',
					bgColor: 'bg-gray-400/10',
					label: status,
					pulse: false
				};
		}
	}

	// Calculate duration
	function formatDuration(startedAt: Date, completedAt?: Date): string {
		const end = completedAt || new Date();
		const diff = Math.floor((end.getTime() - startedAt.getTime()) / 1000);

		if (diff < 60) return `${diff}s`;

		const minutes = Math.floor(diff / 60);
		const seconds = diff % 60;
		if (minutes < 60) return `${minutes}m ${seconds}s`;

		const hours = Math.floor(minutes / 60);
		const remainingMinutes = minutes % 60;
		return `${hours}h ${remainingMinutes}m`;
	}

	// Get brief status text
	function getStatusText(agent: BackgroundAgent): string {
		if (agent.error) return agent.error;

		const taskCount = agent.tasks.length;
		if (taskCount === 0) return 'Initializing...';

		const completed = agent.tasks.filter(t => t.status === 'completed').length;
		const inProgress = agent.tasks.find(t => t.status === 'in_progress');

		if (inProgress) {
			return inProgress.name;
		}

		return `${completed}/${taskCount} tasks completed`;
	}

	const statusInfo = $derived(getStatusInfo(agent.status));
	const duration = $derived(formatDuration(agent.startedAt, agent.completedAt));
	const statusText = $derived(getStatusText(agent));
</script>

<div
	class="agent-list-item group relative flex items-center gap-3 px-4 py-3 rounded-xl transition-all cursor-pointer
		{selected ? 'bg-primary/10 ring-1 ring-primary/30' : 'hover:bg-muted/50'}"
	role="button"
	tabindex="0"
	onclick={onselect}
	onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && onselect?.()}
>
	<!-- Status dot -->
	<div class="relative flex-shrink-0">
		<div
			class="w-3 h-3 rounded-full {statusInfo.color}"
			class:animate-pulse={statusInfo.pulse}
		></div>
		{#if statusInfo.pulse}
			<div class="absolute inset-0 w-3 h-3 rounded-full {statusInfo.color} animate-ping opacity-30"></div>
		{/if}
	</div>

	<!-- Main content -->
	<div class="flex-1 min-w-0">
		<!-- Name and branch -->
		<div class="flex items-center gap-2">
			<h4 class="text-sm font-medium text-foreground truncate">
				{agent.name}
			</h4>
			{#if agent.branch}
				<span class="text-[10px] px-1.5 py-0.5 bg-violet-500/10 text-violet-400 rounded font-mono truncate max-w-[100px]">
					{agent.branch}
				</span>
			{/if}
		</div>

		<!-- Status text -->
		<p class="text-xs text-muted-foreground truncate mt-0.5">
			{statusText}
		</p>

		<!-- Progress bar (only for running/queued) -->
		{#if agent.status === 'running' || agent.status === 'queued'}
			<div class="mt-2 h-1.5 bg-muted rounded-full overflow-hidden">
				<div
					class="h-full rounded-full transition-all duration-300 {agent.status === 'running' ? 'bg-cyan-500' : 'bg-amber-500'}"
					style="width: {agent.progress}%"
				></div>
			</div>
		{/if}
	</div>

	<!-- Duration badge -->
	<div class="flex-shrink-0 text-right">
		<span class="text-xs font-mono text-muted-foreground">
			{duration}
		</span>
	</div>

	<!-- Quick actions (show on hover) -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity"
		onclick={(e) => e.stopPropagation()}
		onkeydown={(e) => e.stopPropagation()}
	>
		{#if agent.status === 'running'}
			<button
				type="button"
				onclick={onpause}
				class="p-1.5 rounded-lg text-amber-500 hover:bg-amber-500/10 transition-colors"
				title="Pause"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 9v6m4-6v6m7-3a9 9 0 11-18 0 9 9 0 0118 0z" />
				</svg>
			</button>
		{:else if agent.status === 'paused'}
			<button
				type="button"
				onclick={onresume}
				class="p-1.5 rounded-lg text-emerald-500 hover:bg-emerald-500/10 transition-colors"
				title="Resume"
			>
				<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
					<path d="M5 3l14 9-14 9V3z" />
				</svg>
			</button>
		{/if}

		{#if agent.status === 'running' || agent.status === 'paused' || agent.status === 'queued'}
			<button
				type="button"
				onclick={oncancel}
				class="p-1.5 rounded-lg text-red-500 hover:bg-red-500/10 transition-colors"
				title="Cancel"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		{/if}

		<button
			type="button"
			onclick={onview}
			class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
			title="View details"
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
			</svg>
		</button>
	</div>
</div>

<style>
	.agent-list-item {
		border: 1px solid transparent;
	}

	.agent-list-item:hover {
		border-color: hsl(var(--border) / 0.5);
	}
</style>
