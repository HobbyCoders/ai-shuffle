<script lang="ts">
	/**
	 * AgentCard - Background agent monitoring card for The Deck
	 *
	 * Extends BaseCard with:
	 * - Status header: running/paused/completed with duration
	 * - Branch info display
	 * - Progress tree visualization
	 * - Collapsible log viewer
	 * - Control buttons: Pause, Resume, Intervene, Cancel
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckAgent, AgentTask, AgentStatus, TaskStatus } from './types';

	interface Props {
		id: string;
		title: string;
		agent: DeckAgent;
		logs?: string[];
		pinned?: boolean;
		minimized?: boolean;
		active?: boolean;
		onpin?: () => void;
		onminimize?: () => void;
		onclose?: () => void;
		onactivate?: () => void;
		onpause?: () => void;
		onresume?: () => void;
		onintervene?: () => void;
		oncancel?: () => void;
	}

	let {
		id,
		title,
		agent,
		logs = [],
		pinned = false,
		minimized = false,
		active = false,
		onpin,
		onminimize,
		onclose,
		onactivate,
		onpause,
		onresume,
		onintervene,
		oncancel
	}: Props = $props();

	// UI state
	let showLogs = $state(false);
	let logsContainer: HTMLDivElement | undefined = $state();

	// Auto-scroll logs
	$effect(() => {
		if (showLogs && logsContainer && logs.length) {
			logsContainer.scrollTop = logsContainer.scrollHeight;
		}
	});

	// Format duration
	function formatDuration(seconds: number): string {
		if (seconds < 60) {
			return `${seconds}s`;
		}
		const minutes = Math.floor(seconds / 60);
		const remainingSeconds = seconds % 60;
		if (minutes < 60) {
			return `${minutes}m ${remainingSeconds}s`;
		}
		const hours = Math.floor(minutes / 60);
		const remainingMinutes = minutes % 60;
		return `${hours}h ${remainingMinutes}m`;
	}

	// Get status display info
	function getStatusInfo(status: AgentStatus): { label: string; color: string; bgColor: string; icon: string } {
		switch (status) {
			case 'running':
				return {
					label: 'Running',
					color: 'text-success',
					bgColor: 'bg-success/10',
					icon: 'M5 3l14 9-14 9V3z'
				};
			case 'paused':
				return {
					label: 'Paused',
					color: 'text-warning',
					bgColor: 'bg-warning/10',
					icon: 'M10 9v6m4-6v6m7-3a9 9 0 11-18 0 9 9 0 0118 0z'
				};
			case 'completed':
				return {
					label: 'Completed',
					color: 'text-primary',
					bgColor: 'bg-primary/10',
					icon: 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z'
				};
			case 'failed':
				return {
					label: 'Failed',
					color: 'text-destructive',
					bgColor: 'bg-destructive/10',
					icon: 'M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z'
				};
			case 'cancelled':
				return {
					label: 'Cancelled',
					color: 'text-muted-foreground',
					bgColor: 'bg-muted',
					icon: 'M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636'
				};
			default:
				return {
					label: status,
					color: 'text-foreground',
					bgColor: 'bg-muted',
					icon: 'M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z'
				};
		}
	}

	// Get task status icon
	function getTaskIcon(status: TaskStatus): { icon: string; color: string } {
		switch (status) {
			case 'completed':
				return { icon: 'M5 13l4 4L19 7', color: 'text-success' };
			case 'in_progress':
				return { icon: 'M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15', color: 'text-primary animate-spin' };
			case 'failed':
				return { icon: 'M6 18L18 6M6 6l12 12', color: 'text-destructive' };
			case 'pending':
			default:
				return { icon: 'M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z', color: 'text-muted-foreground' };
		}
	}

	// Render task tree recursively
	function getTreePrefix(isLast: boolean, depth: number): string {
		if (depth === 0) return '';
		return isLast ? '  ' : '  ';
	}

	function getTreeConnector(isLast: boolean): string {
		return isLast ? '' : '';
	}

	const statusInfo = $derived(getStatusInfo(agent.status));
</script>

<BaseCard
	{id}
	{title}
	type="agent"
	{pinned}
	{minimized}
	{active}
	{onpin}
	{onminimize}
	{onclose}
	{onactivate}
>
	{#snippet headerActions()}
		<!-- Status badge -->
		<span class="flex items-center gap-1.5 text-xs px-2 py-1 rounded-full {statusInfo.bgColor} {statusInfo.color}">
			{#if agent.status === 'running'}
				<span class="w-1.5 h-1.5 rounded-full bg-current animate-pulse"></span>
			{/if}
			{statusInfo.label}
		</span>
		<!-- Duration -->
		<span class="text-xs text-muted-foreground font-mono">
			{formatDuration(agent.duration)}
		</span>
	{/snippet}

	<div class="flex flex-col h-full overflow-hidden">
		<!-- Status header -->
		<div class="px-4 py-3 border-b border-border/30 space-y-2">
			<!-- Branch info -->
			{#if agent.branch}
				<div class="flex items-center gap-2">
					<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
					</svg>
					<code class="text-xs bg-muted px-2 py-0.5 rounded font-mono text-foreground">
						{agent.branch}
					</code>
				</div>
			{/if}

			<!-- Agent info -->
			<div class="flex items-center gap-2 text-sm text-muted-foreground">
				<span class="font-medium text-foreground">{agent.name}</span>
				<span class="text-xs">started {agent.startedAt.toLocaleTimeString()}</span>
			</div>
		</div>

		<!-- Progress tree -->
		<div class="flex-1 overflow-y-auto px-4 py-3">
			<h4 class="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-3">Progress</h4>

			{#if agent.tasks.length === 0}
				<div class="text-sm text-muted-foreground text-center py-4">
					No tasks yet
				</div>
			{:else}
				<div class="space-y-1 font-mono text-sm">
					{#each agent.tasks as task, i (task.id)}
						{@const isLast = i === agent.tasks.length - 1}
						{@const taskIcon = getTaskIcon(task.status)}

						<div class="flex items-start gap-2">
							<!-- Tree connector -->
							<span class="text-muted-foreground/50 select-none w-4">
								{isLast ? '' : ''}
							</span>

							<!-- Status icon -->
							<svg class="w-4 h-4 flex-shrink-0 mt-0.5 {taskIcon.color}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={taskIcon.icon} />
							</svg>

							<!-- Task content -->
							<div class="flex-1 min-w-0">
								<div class="flex items-center gap-2">
									<span class="text-foreground truncate">{task.name}</span>
									{#if task.progress}
										<span class="text-xs text-muted-foreground">
											({task.progress.current}/{task.progress.total})
										</span>
									{/if}
								</div>

								<!-- Child tasks -->
								{#if task.children && task.children.length > 0}
									<div class="mt-1 ml-2 space-y-0.5 border-l border-border/50 pl-3">
										{#each task.children as child, j (child.id)}
											{@const childIcon = getTaskIcon(child.status)}
											{@const isChildLast = j === task.children.length - 1}

											<div class="flex items-center gap-2 text-xs">
												<svg class="w-3 h-3 flex-shrink-0 {childIcon.color}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={childIcon.icon} />
												</svg>
												<span class="text-muted-foreground truncate" title={child.file || child.name}>
													{child.file || child.name}
												</span>
											</div>
										{/each}
									</div>
								{/if}
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</div>

		<!-- Logs section (collapsible) -->
		<div class="border-t border-border/30">
			<button
				type="button"
				class="w-full flex items-center justify-between px-4 py-2 text-sm text-muted-foreground hover:text-foreground hover:bg-muted/30 transition-colors"
				onclick={() => showLogs = !showLogs}
				aria-expanded={showLogs}
			>
				<span class="flex items-center gap-2">
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h7" />
					</svg>
					Logs ({logs.length})
				</span>
				<svg
					class="w-4 h-4 transition-transform {showLogs ? 'rotate-180' : ''}"
					fill="none"
					stroke="currentColor"
					viewBox="0 0 24 24"
				>
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
				</svg>
			</button>

			{#if showLogs}
				<div
					bind:this={logsContainer}
					class="h-32 overflow-y-auto bg-[#1a1a2e] px-3 py-2 font-mono text-xs"
				>
					{#if logs.length === 0}
						<span class="text-muted-foreground">No logs yet...</span>
					{:else}
						{#each logs.slice(-50) as log, i (i)}
							<div class="text-gray-300 whitespace-pre-wrap break-all leading-relaxed">{log}</div>
						{/each}
					{/if}
				</div>
			{/if}
		</div>
	</div>

	{#snippet footer()}
		<div class="flex items-center gap-2 px-4 py-3">
			{#if agent.status === 'running'}
				<button
					type="button"
					class="flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-warning/10 text-warning hover:bg-warning/20 transition-colors text-sm font-medium"
					onclick={onpause}
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 9v6m4-6v6m7-3a9 9 0 11-18 0 9 9 0 0118 0z" />
					</svg>
					Pause
				</button>
			{:else if agent.status === 'paused'}
				<button
					type="button"
					class="flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-success/10 text-success hover:bg-success/20 transition-colors text-sm font-medium"
					onclick={onresume}
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
					class="flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-primary/10 text-primary hover:bg-primary/20 transition-colors text-sm font-medium"
					onclick={onintervene}
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
					</svg>
					Intervene
				</button>

				<button
					type="button"
					class="flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-destructive/10 text-destructive hover:bg-destructive/20 transition-colors text-sm font-medium"
					onclick={oncancel}
					title="Cancel agent"
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			{/if}

			{#if agent.status === 'completed' || agent.status === 'failed' || agent.status === 'cancelled'}
				<button
					type="button"
					class="flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-muted text-foreground hover:bg-accent transition-colors text-sm font-medium"
					onclick={onclose}
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
					Dismiss
				</button>
			{/if}
		</div>
	{/snippet}
</BaseCard>
