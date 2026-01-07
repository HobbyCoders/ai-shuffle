<script lang="ts">
	import PermissionRequest, { type PermissionRequestData } from './PermissionRequest.svelte';
	import { createEventDispatcher } from 'svelte';

	interface Props {
		requests: PermissionRequestData[];
	}

	let { requests }: Props = $props();

	const dispatch = createEventDispatcher<{
		respond: {
			request_id: string;
			decision: 'allow' | 'deny';
			remember?: 'none' | 'session' | 'profile';
			pattern?: string;
		};
		dismissAll: void;
	}>();

	let showPendingList = $state(false);

	function handleRespond(event: CustomEvent<{
		request_id: string;
		decision: 'allow' | 'deny';
		remember?: 'none' | 'session' | 'profile';
		pattern?: string;
	}>) {
		dispatch('respond', event.detail);
	}

	function handleDenyAll() {
		for (const req of requests) {
			dispatch('respond', {
				request_id: req.request_id,
				decision: 'deny',
				remember: 'none'
			});
		}
		dispatch('dismissAll');
	}

	function handleAllowAll() {
		// Allow all pending permission requests
		for (const req of requests) {
			dispatch('respond', {
				request_id: req.request_id,
				decision: 'allow',
				remember: 'none'
			});
		}
	}

	// Get a short summary for queued items
	function getShortSummary(req: PermissionRequestData): string {
		const input = req.tool_input;
		switch (req.tool_name) {
			case 'Bash':
				return String(input.command || '').split(' ')[0];
			case 'Read':
			case 'Write':
			case 'Edit':
				return String(input.file_path || '').split('/').pop() || '';
			case 'Glob':
			case 'Grep':
				return String(input.path || input.pattern || '').split('/').pop() || '';
			case 'WebFetch':
				try {
					return new URL(String(input.url || '')).hostname;
				} catch {
					return '';
				}
			case 'Task':
				return String(input.description || '').substring(0, 20);
			default:
				return '';
		}
	}

	// Tool colors for queue badges
	const toolColors: Record<string, string> = {
		'Bash': 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
		'Read': 'bg-blue-500/20 text-blue-400 border-blue-500/30',
		'Write': 'bg-amber-500/20 text-amber-400 border-amber-500/30',
		'Edit': 'bg-orange-500/20 text-orange-400 border-orange-500/30',
		'Glob': 'bg-purple-500/20 text-purple-400 border-purple-500/30',
		'Grep': 'bg-violet-500/20 text-violet-400 border-violet-500/30',
		'WebFetch': 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
		'WebSearch': 'bg-sky-500/20 text-sky-400 border-sky-500/30',
		'Task': 'bg-indigo-500/20 text-indigo-400 border-indigo-500/30',
		'NotebookEdit': 'bg-rose-500/20 text-rose-400 border-rose-500/30'
	};

	function getToolColor(name: string): string {
		return toolColors[name] || 'bg-gray-500/20 text-gray-400 border-gray-500/30';
	}
</script>

{#if requests.length > 0}
	<div class="permission-queue space-y-3">
		<!-- Queue Header (when multiple requests) -->
		{#if requests.length > 1}
			<div class="queue-header rounded-xl border border-warning/30 bg-warning/5 p-3">
				<div class="flex flex-col sm:flex-row sm:items-center gap-3">
					<!-- Queue Info -->
					<div class="flex items-center gap-2 flex-1">
						<div class="relative">
							<div class="w-8 h-8 rounded-lg bg-warning/15 border border-warning/25 flex items-center justify-center">
								<svg class="w-4 h-4 text-warning" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
								</svg>
							</div>
							<span class="absolute -top-1 -right-1 w-4 h-4 flex items-center justify-center text-[9px] font-bold bg-warning text-warning-foreground rounded-full">
								{requests.length}
							</span>
						</div>
						<div>
							<p class="text-sm font-medium text-foreground">
								{requests.length} permissions pending
							</p>
							<p class="text-xs text-muted-foreground">
								Respond to each request or use bulk actions
							</p>
						</div>
					</div>

					<!-- Bulk Actions -->
					<div class="flex items-center gap-2">
						<button
							type="button"
							onclick={() => showPendingList = !showPendingList}
							class="px-3 py-1.5 text-xs font-medium rounded-lg border transition-all
								bg-card text-muted-foreground border-border
								hover:text-foreground hover:border-border/80"
						>
							{showPendingList ? 'Hide Queue' : 'View Queue'}
						</button>
						<button
							type="button"
							onclick={handleAllowAll}
							class="px-3 py-1.5 text-xs font-medium rounded-lg border transition-all
								bg-success/10 text-success border-success/25
								hover:bg-success/20 hover:border-success/35"
						>
							Allow All
						</button>
						<button
							type="button"
							onclick={handleDenyAll}
							class="px-3 py-1.5 text-xs font-medium rounded-lg border transition-all
								bg-destructive/10 text-destructive border-destructive/25
								hover:bg-destructive/20 hover:border-destructive/35"
						>
							Deny All
						</button>
					</div>
				</div>

				<!-- Pending Queue List (collapsible) -->
				{#if showPendingList}
					<div class="mt-3 pt-3 border-t border-warning/20 animate-expand">
						<p class="text-[10px] uppercase tracking-wider text-muted-foreground font-medium mb-2">
							Queued Requests
						</p>
						<div class="flex flex-wrap gap-1.5">
							{#each requests.slice(1) as req, idx}
								<div class="queue-item inline-flex items-center gap-1.5 px-2 py-1 rounded-lg text-xs border {getToolColor(req.tool_name)}">
									<span class="font-mono text-[10px] opacity-60">{idx + 2}</span>
									<span class="font-medium">{req.tool_name}</span>
									{#if getShortSummary(req)}
										<span class="truncate max-w-[80px] sm:max-w-[120px] opacity-75" title={getShortSummary(req)}>
											{getShortSummary(req)}
										</span>
									{/if}
								</div>
							{/each}
						</div>
					</div>
				{/if}
			</div>
		{/if}

		<!-- Current Permission Request -->
		<PermissionRequest
			request={requests[0]}
			isFirst={true}
			queueCount={requests.length}
			on:respond={handleRespond}
		/>
	</div>
{/if}

<style>
	.animate-expand {
		animation: expand 0.2s ease-out;
	}

	@keyframes expand {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.queue-item {
		animation: item-enter 0.15s ease-out backwards;
	}

	.queue-item:nth-child(1) { animation-delay: 0ms; }
	.queue-item:nth-child(2) { animation-delay: 30ms; }
	.queue-item:nth-child(3) { animation-delay: 60ms; }
	.queue-item:nth-child(4) { animation-delay: 90ms; }
	.queue-item:nth-child(5) { animation-delay: 120ms; }
	.queue-item:nth-child(n+6) { animation-delay: 150ms; }

	@keyframes item-enter {
		from {
			opacity: 0;
			transform: scale(0.9);
		}
		to {
			opacity: 1;
			transform: scale(1);
		}
	}
</style>
