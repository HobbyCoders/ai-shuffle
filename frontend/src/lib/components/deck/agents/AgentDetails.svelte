<script lang="ts">
	/**
	 * AgentDetails - Expanded agent view (modal or side panel)
	 *
	 * Features:
	 * - Full status header with all metadata
	 * - Branch info with link to GitHub
	 * - Task tree visualization (larger version)
	 * - Live log viewer (auto-scroll, syntax highlighting)
	 * - Intervention button (opens chat with agent context)
	 * - Control buttons: Pause/Resume, Cancel, Restart
	 */
	import type { BackgroundAgent, AgentStatus } from '$lib/stores/agents';
	import TaskTree from './TaskTree.svelte';
	import AgentLogs from './AgentLogs.svelte';

	interface Props {
		agent: BackgroundAgent;
		open?: boolean;
		onclose?: () => void;
		onpause?: () => void;
		onresume?: () => void;
		oncancel?: () => void;
		onintervene?: () => void;
		onrestart?: () => void;
	}

	let {
		agent,
		open = true,
		onclose,
		onpause,
		onresume,
		oncancel,
		onintervene,
		onrestart
	}: Props = $props();

	// Active tab
	let activeTab = $state<'tasks' | 'logs'>('tasks');

	// Get status info
	function getStatusInfo(status: AgentStatus): { label: string; color: string; bgColor: string; borderColor: string } {
		switch (status) {
			case 'running':
				return {
					label: 'Running',
					color: 'text-cyan-400',
					bgColor: 'bg-cyan-500/10',
					borderColor: 'border-cyan-500/30'
				};
			case 'queued':
				return {
					label: 'Queued',
					color: 'text-amber-400',
					bgColor: 'bg-amber-500/10',
					borderColor: 'border-amber-500/30'
				};
			case 'paused':
				return {
					label: 'Paused',
					color: 'text-gray-400',
					bgColor: 'bg-gray-500/10',
					borderColor: 'border-gray-500/30'
				};
			case 'completed':
				return {
					label: 'Completed',
					color: 'text-emerald-400',
					bgColor: 'bg-emerald-500/10',
					borderColor: 'border-emerald-500/30'
				};
			case 'failed':
				return {
					label: 'Failed',
					color: 'text-red-400',
					bgColor: 'bg-red-500/10',
					borderColor: 'border-red-500/30'
				};
			default:
				return {
					label: status,
					color: 'text-gray-400',
					bgColor: 'bg-gray-500/10',
					borderColor: 'border-gray-500/30'
				};
		}
	}

	// Format duration
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

	// Format date/time
	function formatDateTime(date: Date): string {
		return date.toLocaleString(undefined, {
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	// Calculate overall progress
	function calculateProgress(agent: BackgroundAgent): number {
		if (agent.tasks.length === 0) return agent.progress;

		const completed = agent.tasks.filter(t => t.status === 'completed').length;
		const inProgress = agent.tasks.filter(t => t.status === 'in_progress').length;
		const total = agent.tasks.length;

		return Math.round(((completed + inProgress * 0.5) / total) * 100);
	}

	// Handle escape key
	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			onclose?.();
		}
	}

	const statusInfo = $derived(getStatusInfo(agent.status));
	const duration = $derived(formatDuration(agent.startedAt, agent.completedAt));
	const progress = $derived(calculateProgress(agent));
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
	<!-- Backdrop -->
	<div class="fixed inset-0 z-50 flex items-center justify-center p-4">
		<button
			type="button"
			class="absolute inset-0 bg-black/60 backdrop-blur-sm"
			onclick={onclose}
			aria-label="Close"
		></button>

		<!-- Panel -->
		<div
			class="relative w-full max-w-4xl max-h-[90vh] bg-card border border-border rounded-2xl shadow-2xl flex flex-col overflow-hidden animate-modal-in"
			role="dialog"
			aria-modal="true"
			aria-labelledby="agent-details-title"
		>
			<!-- Header -->
			<header class="shrink-0 border-b border-border bg-gradient-to-r from-card to-muted/30">
				<div class="px-6 py-4">
					<div class="flex items-start justify-between gap-4">
						<!-- Left: Title and status -->
						<div class="flex-1 min-w-0">
							<div class="flex items-center gap-3 mb-2">
								<!-- Status indicator -->
								<div
									class="px-3 py-1 rounded-full text-xs font-medium {statusInfo.bgColor} {statusInfo.color} border {statusInfo.borderColor}"
								>
									{#if agent.status === 'running'}
										<span class="inline-block w-1.5 h-1.5 rounded-full bg-current animate-pulse mr-1.5"></span>
									{/if}
									{statusInfo.label}
								</div>

								<!-- Progress -->
								{#if agent.status === 'running' || agent.status === 'queued'}
									<span class="text-sm font-mono text-muted-foreground">{progress}%</span>
								{/if}
							</div>

							<h2 id="agent-details-title" class="text-xl font-semibold text-foreground truncate">
								{agent.name}
							</h2>

							{#if agent.prompt}
								<p class="text-sm text-muted-foreground mt-1 line-clamp-2">
									{agent.prompt}
								</p>
							{/if}
						</div>

						<!-- Close button -->
						<button
							type="button"
							onclick={onclose}
							class="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
							aria-label="Close"
						>
							<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
							</svg>
						</button>
					</div>

					<!-- Meta info row -->
					<div class="flex flex-wrap items-center gap-4 mt-4 text-sm">
						<!-- Duration -->
						<div class="flex items-center gap-2 text-muted-foreground">
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
							</svg>
							<span class="font-mono">{duration}</span>
						</div>

						<!-- Started at -->
						<div class="flex items-center gap-2 text-muted-foreground">
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
							</svg>
							<span>{formatDateTime(agent.startedAt)}</span>
						</div>

						<!-- Branch -->
						{#if agent.branch}
							<button
								type="button"
								class="flex items-center gap-2 text-violet-400 hover:text-violet-300 transition-colors"
							>
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
								</svg>
								<code class="text-xs bg-violet-500/10 px-2 py-0.5 rounded">{agent.branch}</code>
							</button>
						{/if}

						<!-- Session ID -->
						{#if agent.sessionId}
							<div class="flex items-center gap-2 text-muted-foreground">
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
								</svg>
								<code class="text-xs">{agent.sessionId.slice(0, 8)}...</code>
							</div>
						{/if}
					</div>

					<!-- Progress bar -->
					{#if agent.status === 'running' || agent.status === 'queued'}
						<div class="mt-4 h-2 bg-muted rounded-full overflow-hidden">
							<div
								class="h-full rounded-full transition-all duration-500 {agent.status === 'running' ? 'bg-gradient-to-r from-cyan-500 to-blue-500' : 'bg-amber-500'}"
								style="width: {progress}%"
							></div>
						</div>
					{/if}
				</div>

				<!-- Tab navigation -->
				<div class="flex gap-1 px-6">
					<button
						type="button"
						onclick={() => activeTab = 'tasks'}
						class="px-4 py-2.5 text-sm font-medium rounded-t-lg transition-all border-b-2 -mb-px {activeTab === 'tasks' ? 'text-primary border-primary bg-primary/5' : 'text-muted-foreground hover:text-foreground border-transparent hover:border-muted-foreground/30'}"
					>
						Tasks ({agent.tasks.length})
					</button>
					<button
						type="button"
						onclick={() => activeTab = 'logs'}
						class="px-4 py-2.5 text-sm font-medium rounded-t-lg transition-all border-b-2 -mb-px {activeTab === 'logs' ? 'text-primary border-primary bg-primary/5' : 'text-muted-foreground hover:text-foreground border-transparent hover:border-muted-foreground/30'}"
					>
						Logs ({agent.logs.length})
					</button>
				</div>
			</header>

			<!-- Content -->
			<div class="flex-1 overflow-y-auto">
				{#if activeTab === 'tasks'}
					<div class="p-6">
						{#if agent.tasks.length === 0}
							<div class="text-center py-12">
								<svg class="w-16 h-16 mx-auto text-muted-foreground/30 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
								</svg>
								<p class="text-muted-foreground">No tasks yet</p>
								<p class="text-sm text-muted-foreground/70 mt-1">Tasks will appear as the agent progresses</p>
							</div>
						{:else}
							<TaskTree tasks={agent.tasks} />
						{/if}
					</div>
				{:else}
					<AgentLogs logs={agent.logs} agentId={agent.id} maxHeight="100%" />
				{/if}
			</div>

			<!-- Error display -->
			{#if agent.error}
				<div class="shrink-0 mx-6 mb-4 p-4 bg-red-500/10 border border-red-500/30 rounded-xl">
					<div class="flex items-start gap-3">
						<svg class="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
						</svg>
						<div class="flex-1 min-w-0">
							<h4 class="text-sm font-medium text-red-400">Error</h4>
							<p class="text-sm text-red-300/80 mt-1">{agent.error}</p>
						</div>
					</div>
				</div>
			{/if}

			<!-- Footer with actions -->
			<footer class="shrink-0 px-6 py-4 border-t border-border bg-muted/30 flex flex-wrap items-center gap-3">
				{#if agent.status === 'running'}
					<button
						type="button"
						onclick={onpause}
						class="flex items-center gap-2 px-4 py-2 rounded-xl bg-amber-500/10 text-amber-400 hover:bg-amber-500/20 transition-colors text-sm font-medium"
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 9v6m4-6v6m7-3a9 9 0 11-18 0 9 9 0 0118 0z" />
						</svg>
						Pause
					</button>
				{:else if agent.status === 'paused'}
					<button
						type="button"
						onclick={onresume}
						class="flex items-center gap-2 px-4 py-2 rounded-xl bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition-colors text-sm font-medium"
					>
						<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
							<path d="M5 3l14 9-14 9V3z" />
						</svg>
						Resume
					</button>
				{/if}

				{#if agent.status === 'running' || agent.status === 'paused'}
					<button
						type="button"
						onclick={onintervene}
						class="flex items-center gap-2 px-4 py-2 rounded-xl bg-primary/10 text-primary hover:bg-primary/20 transition-colors text-sm font-medium"
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
						</svg>
						Intervene
					</button>
				{/if}

				{#if agent.status === 'running' || agent.status === 'paused' || agent.status === 'queued'}
					<button
						type="button"
						onclick={oncancel}
						class="flex items-center gap-2 px-4 py-2 rounded-xl bg-red-500/10 text-red-400 hover:bg-red-500/20 transition-colors text-sm font-medium"
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
						Cancel
					</button>
				{/if}

				{#if agent.status === 'completed' || agent.status === 'failed'}
					<button
						type="button"
						onclick={onrestart}
						class="flex items-center gap-2 px-4 py-2 rounded-xl bg-muted text-foreground hover:bg-accent transition-colors text-sm font-medium"
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
						</svg>
						Restart
					</button>
				{/if}

				<div class="flex-1"></div>

				<button
					type="button"
					onclick={onclose}
					class="px-4 py-2 rounded-xl bg-muted text-foreground hover:bg-accent transition-colors text-sm font-medium"
				>
					Close
				</button>
			</footer>
		</div>
	</div>
{/if}

<style>
	@keyframes modal-in {
		from {
			opacity: 0;
			transform: scale(0.95) translateY(10px);
		}
		to {
			opacity: 1;
			transform: scale(1) translateY(0);
		}
	}

	.animate-modal-in {
		animation: modal-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}
</style>
