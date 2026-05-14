<script lang="ts">
	import PermissionRequest, { type PermissionRequestData } from './PermissionRequest.svelte';

	interface Props {
		requests: PermissionRequestData[];
		/** Slice B: items the user previously deferred — rendered as a separate strip. */
		deferred?: PermissionRequestData[];
		onRespond?: (detail: {
			request_id: string;
			decision: 'allow' | 'deny' | 'defer';
			remember?: 'none' | 'session' | 'profile';
			pattern?: string;
		}) => void;
		onDismissAll?: () => void;
		/** Slice B: emit a `requeue_deferred_permissions` WS message. */
		onRequeueDeferred?: () => void;
	}

	let { requests, deferred = [], onRespond, onDismissAll, onRequeueDeferred }: Props = $props();

	let showPendingList = $state(false);
	let showDeferredList = $state(false);

	function handleRespond(detail: {
		request_id: string;
		decision: 'allow' | 'deny' | 'defer';
		remember?: 'none' | 'session' | 'profile';
		pattern?: string;
	}) {
		onRespond?.(detail);
	}

	function handleDenyAll() {
		for (const req of requests) {
			onRespond?.({
				request_id: req.request_id,
				decision: 'deny',
				remember: 'none'
			});
		}
		onDismissAll?.();
	}

	function handleAllowAll() {
		// Allow all pending permission requests
		for (const req of requests) {
			onRespond?.({
				request_id: req.request_id,
				decision: 'allow',
				remember: 'none'
			});
		}
	}

	function handleReviewDeferred() {
		onRequeueDeferred?.();
	}

	function getDeferredLabel(req: PermissionRequestData): string {
		return (
			(req.title && req.title.trim()) ||
			(req.display_name && req.display_name.trim()) ||
			req.tool_name
		);
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
			case 'Agent':
				return String(input.description || '').substring(0, 20);
			default:
				return '';
		}
	}

	// Tool colors for queue badges - using CSS class names that map to CSS variables
	const toolColors: Record<string, string> = {
		'Bash': 'tool-bash',
		'Read': 'tool-read',
		'Write': 'tool-write',
		'Edit': 'tool-edit',
		'Glob': 'tool-glob',
		'Grep': 'tool-grep',
		'WebFetch': 'tool-web',
		'WebSearch': 'tool-web',
		'Task': 'tool-task',
		'Agent': 'tool-task',
		'NotebookEdit': 'tool-notebook'
	};

	function getToolColor(name: string): string {
		return toolColors[name] || 'tool-default';
	}
</script>

{#if requests.length > 0 || deferred.length > 0}
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
		{#if requests.length > 0}
			<PermissionRequest
				request={requests[0]}
				isFirst={true}
				queueCount={requests.length}
				onRespond={handleRespond}
			/>
		{/if}

		<!-- Slice B: Deferred queue strip — collapsible pill summarizing every
		     request the user previously chose to "Defer". Tapping "Review" tells
		     the backend to re-broadcast each deferred item as a fresh
		     permission_request, which flows through the existing handler. -->
		{#if deferred.length > 0}
			<div class="deferred-strip" role="region" aria-label="Deferred permission requests">
				<button
					type="button"
					class="deferred-pill"
					onclick={() => (showDeferredList = !showDeferredList)}
					aria-expanded={showDeferredList}
				>
					<span class="deferred-dot" aria-hidden="true"></span>
					<span class="deferred-count">{deferred.length} deferred</span>
					<span class="deferred-sep" aria-hidden="true">·</span>
					<span class="deferred-cta">{showDeferredList ? 'hide' : 'review'} →</span>
				</button>

				{#if showDeferredList}
					<div class="deferred-list">
						<ul class="deferred-items">
							{#each deferred as req (req.request_id)}
								<li class="deferred-item">
									<span class="deferred-tool" title={req.tool_name}>{getDeferredLabel(req)}</span>
									<span class="deferred-tool-sub">{req.tool_name}</span>
								</li>
							{/each}
						</ul>
						<button
							type="button"
							class="deferred-review-btn"
							onclick={handleReviewDeferred}
						>
							Review all
						</button>
					</div>
				{/if}
			</div>
		{/if}
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

	/* Tool color classes using CSS variables */
	:global(.tool-bash) {
		background: color-mix(in oklch, var(--tool-bash) 20%, transparent);
		color: var(--tool-bash);
		border-color: color-mix(in oklch, var(--tool-bash) 30%, transparent);
	}
	:global(.tool-read) {
		background: color-mix(in oklch, var(--tool-read) 20%, transparent);
		color: var(--tool-read);
		border-color: color-mix(in oklch, var(--tool-read) 30%, transparent);
	}
	:global(.tool-write) {
		background: color-mix(in oklch, var(--tool-write) 20%, transparent);
		color: var(--tool-write);
		border-color: color-mix(in oklch, var(--tool-write) 30%, transparent);
	}
	:global(.tool-edit) {
		background: color-mix(in oklch, var(--tool-edit) 20%, transparent);
		color: var(--tool-edit);
		border-color: color-mix(in oklch, var(--tool-edit) 30%, transparent);
	}
	:global(.tool-glob) {
		background: color-mix(in oklch, var(--tool-glob) 20%, transparent);
		color: var(--tool-glob);
		border-color: color-mix(in oklch, var(--tool-glob) 30%, transparent);
	}
	:global(.tool-grep) {
		background: color-mix(in oklch, var(--tool-grep) 20%, transparent);
		color: var(--tool-grep);
		border-color: color-mix(in oklch, var(--tool-grep) 30%, transparent);
	}
	:global(.tool-web) {
		background: color-mix(in oklch, var(--tool-web) 20%, transparent);
		color: var(--tool-web);
		border-color: color-mix(in oklch, var(--tool-web) 30%, transparent);
	}
	:global(.tool-task) {
		background: color-mix(in oklch, var(--tool-task) 20%, transparent);
		color: var(--tool-task);
		border-color: color-mix(in oklch, var(--tool-task) 30%, transparent);
	}
	:global(.tool-notebook) {
		background: color-mix(in oklch, var(--tool-notebook) 20%, transparent);
		color: var(--tool-notebook);
		border-color: color-mix(in oklch, var(--tool-notebook) 30%, transparent);
	}
	:global(.tool-default) {
		background: color-mix(in oklch, var(--tool-default) 20%, transparent);
		color: var(--tool-default);
		border-color: color-mix(in oklch, var(--tool-default) 30%, transparent);
	}

	/* ─── Slice B: deferred queue strip ─────────────────────────── */

	.deferred-strip {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.deferred-pill {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		align-self: flex-start;
		min-height: 36px;
		padding: 6px 12px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--warning, #d97706) 12%, var(--card));
		border: 1px solid color-mix(in oklch, var(--warning, #d97706) 35%, var(--border));
		color: var(--foreground);
		font-size: 0.75rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.deferred-pill:hover {
		background: color-mix(in oklch, var(--warning, #d97706) 18%, var(--card));
		border-color: color-mix(in oklch, var(--warning, #d97706) 50%, var(--border));
	}

	.deferred-pill:focus-visible {
		outline: 2px solid var(--warning, #d97706);
		outline-offset: 2px;
	}

	.deferred-dot {
		display: inline-block;
		width: 6px;
		height: 6px;
		border-radius: 999px;
		background: var(--warning, #d97706);
	}

	.deferred-count {
		font-variant-numeric: tabular-nums;
		font-weight: 600;
	}

	.deferred-sep {
		color: var(--muted-foreground);
	}

	.deferred-cta {
		color: var(--warning, #d97706);
		font-weight: 600;
	}

	.deferred-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 10px 12px;
		border-radius: var(--radius, 0.75rem);
		border: 1px solid color-mix(in oklch, var(--warning, #d97706) 25%, var(--border));
		background: color-mix(in oklch, var(--warning, #d97706) 5%, var(--card));
		animation: deferred-expand 0.18s ease-out;
	}

	@keyframes deferred-expand {
		from {
			opacity: 0;
			transform: translateY(-4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.deferred-items {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
		max-height: 220px;
		overflow-y: auto;
	}

	.deferred-item {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 8px;
		border-radius: 8px;
		background: var(--card);
		border: 1px solid var(--border);
		font-size: 0.75rem;
	}

	.deferred-tool {
		flex: 1;
		min-width: 0;
		font-weight: 500;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.deferred-tool-sub {
		flex-shrink: 0;
		font-family: var(--font-mono);
		font-size: 0.625rem;
		color: var(--muted-foreground);
		padding: 2px 6px;
		background: var(--muted);
		border-radius: 4px;
	}

	.deferred-review-btn {
		align-self: flex-end;
		min-height: 36px;
		padding: 6px 14px;
		font-size: 0.75rem;
		font-weight: 600;
		border-radius: var(--radius, 0.75rem);
		background: var(--warning, #d97706);
		color: var(--primary-foreground, #fff);
		border: 0;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.deferred-review-btn:hover {
		filter: brightness(1.08);
	}

	.deferred-review-btn:active {
		transform: scale(0.98);
	}

	.deferred-review-btn:focus-visible {
		outline: 2px solid var(--warning, #d97706);
		outline-offset: 2px;
	}

	@media (max-width: 639px) {
		.deferred-pill {
			min-height: 44px;
		}
		.deferred-review-btn {
			min-height: 44px;
			width: 100%;
		}
	}
</style>
