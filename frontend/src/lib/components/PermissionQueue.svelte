<script lang="ts">
	import { untrack } from 'svelte';
	import PermissionRequest, { type PermissionRequestData } from './PermissionRequest.svelte';
	import { focusTrap } from './ui/focus-trap';

	interface Props {
		requests: PermissionRequestData[];
		onRespond?: (detail: {
			request_id: string;
			decision: 'allow' | 'deny';
			remember?: 'none' | 'session' | 'profile';
			pattern?: string;
		}) => void;
		onDismissAll?: () => void;
	}

	let { requests, onRespond, onDismissAll }: Props = $props();

	let showPendingList = $state(false);
	let queueEl: HTMLElement | undefined = $state();

	/** The chat composer, where focus goes when the last request is answered
	 *  and nothing else (the element focused before the queue opened) took it. */
	const COMPOSER_SELECTOR = '[data-composer-editor], .ProseMirror';

	function nothingFocused(): boolean {
		const active = document.activeElement;
		return !active || active === document.body || !active.isConnected;
	}

	// Focus follows the answer. When the queue advances, the answered prompt
	// (and whatever in it had focus) is replaced: focus moves onto the new
	// prompt so its shortcuts and screen readers reach it, as focusTrap does
	// for the first one. When the last request is answered, focusTrap returns
	// focus to where the user was; if that is gone, it goes to the composer.
	let shownRequestId: string | null = null;
	$effect(() => {
		const id = requests[0]?.request_id ?? null;
		const previous = shownRequestId;
		shownRequestId = id;
		if (!previous || id === previous) return;
		if (!id) {
			if (nothingFocused()) {
				document.querySelector<HTMLElement>(COMPOSER_SELECTOR)?.focus({ preventScroll: true });
			}
			return;
		}
		if (!queueEl) return;
		if (queueEl.contains(document.activeElement) && !nothingFocused()) return;
		focusPanel();
	});

	function focusPanel() {
		queueEl?.querySelector<HTMLElement>('.permission-panel')?.focus({ preventScroll: true });
	}

	/** An answer disables (or removes) the button that had focus, and a
	 *  browser then drops focus to <body>, outside the focus trap, where Tab
	 *  escapes into the page behind the modal and a screen reader loses its
	 *  place. Park focus on the prompt itself first; if the answer is lost and
	 *  the buttons come back, focus is still inside the trap. */
	function parkFocusBeforeAnswer() {
		if (!queueEl) return;
		if (nothingFocused() || queueEl.contains(document.activeElement)) focusPanel();
	}

	function handleRespond(detail: {
		request_id: string;
		decision: 'allow' | 'deny';
		remember?: 'none' | 'session' | 'profile';
		pattern?: string;
	}) {
		parkFocusBeforeAnswer();
		onRespond?.(detail);
	}

	// Bulk-action arming, the same rules the prompt's own buttons follow
	// (PermissionRequest): Allow All / Deny All answer every queued request,
	// including one that appeared an instant ago under the pointer or the
	// keyboard, so they count only once the newest request has been visible
	// for BULK_ARM_DELAY_MS, and re-arm whenever another request arrives.
	// - A click counts only once armed.
	// - Enter / Space on a focused bulk button counts only once armed and only
	//   for a fresh press (not an auto-repeat, not a key already down when the
	//   newest request arrived); an unarmed press is held back, and so is the
	//   click it would generate (Space's comes on keyup).
	// - After a bulk answer the buttons stay disarmed until another request
	//   arrives or, if the queue is still shown after BULK_RETRY_MS (the
	//   answers were lost), until they re-arm so the action can be retried.
	const BULK_ARM_DELAY_MS = 500;
	const BULK_RETRY_MS = 5000;
	let bulkArmed = false;
	/** When the newest request arrived, on the clock events' `timeStamp` uses. */
	let bulkShownAt = 0;
	let bulkKeyClickBlocked = false;
	let bulkTimer: ReturnType<typeof setTimeout> | undefined;
	let seenRequestIds = new Set<string>();

	function armBulkAfter(delayMs: number) {
		bulkArmed = false;
		clearTimeout(bulkTimer);
		bulkTimer = setTimeout(() => (bulkArmed = true), delayMs);
	}

	$effect(() => {
		const ids = requests.map((r) => r.request_id);
		untrack(() => {
			const arrived = ids.some((id) => !seenRequestIds.has(id));
			seenRequestIds = new Set(ids);
			if (!arrived) return;
			bulkKeyClickBlocked = false;
			// A synthetic event's timeStamp reads the same clock as input events.
			bulkShownAt = new Event('permission-bulk-shown').timeStamp;
			armBulkAfter(BULK_ARM_DELAY_MS);
		});
	});
	$effect(() => () => clearTimeout(bulkTimer));

	function isBulkKey(e: KeyboardEvent): boolean {
		return e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar';
	}

	function handleBulkKeydown(e: KeyboardEvent) {
		if (!isBulkKey(e)) return;
		const fresh = !e.repeat && !(e.timeStamp > 0 && e.timeStamp < bulkShownAt);
		if (fresh && bulkArmed) return;
		e.preventDefault();
		bulkKeyClickBlocked = true;
	}

	function handleBulkKeyup(e: KeyboardEvent) {
		if (!bulkKeyClickBlocked) return;
		if (e.key === ' ' || e.key === 'Spacebar') e.preventDefault();
		setTimeout(() => (bulkKeyClickBlocked = false), 0);
	}

	function bulkActionAllowed(e: MouseEvent): boolean {
		if (!bulkArmed) return false;
		// A click with no pointer behind it (detail 0) that an unarmed Enter or
		// Space on the button generated.
		if (e.detail === 0 && bulkKeyClickBlocked) return false;
		return true;
	}

	function answerAll(decision: 'allow' | 'deny', e: MouseEvent): boolean {
		if (!bulkActionAllowed(e)) return false;
		armBulkAfter(BULK_RETRY_MS);
		parkFocusBeforeAnswer();
		for (const req of requests) {
			onRespond?.({
				request_id: req.request_id,
				decision,
				remember: 'none'
			});
		}
		return true;
	}

	function handleDenyAll(e: MouseEvent) {
		if (answerAll('deny', e)) onDismissAll?.();
	}

	function handleAllowAll(e: MouseEvent) {
		answerAll('allow', e);
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

{#if requests.length > 0}
	<!-- The queue is shown as a blocking modal: focus moves onto the prompt so its
	     Enter/Esc shortcuts and screen readers reach it, stays inside while it
	     blocks, and returns to the composer once every request is answered. -->
	<div class="permission-queue space-y-3" bind:this={queueEl} use:focusTrap={{ initial: '.permission-panel' }}>
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
							onkeydown={handleBulkKeydown}
							onkeyup={handleBulkKeyup}
							class="px-3 py-1.5 text-xs font-medium rounded-lg border transition-all
								bg-success/10 text-success border-success/25
								hover:bg-success/20 hover:border-success/35"
						>
							Allow All
						</button>
						<button
							type="button"
							onclick={handleDenyAll}
							onkeydown={handleBulkKeydown}
							onkeyup={handleBulkKeyup}
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
		<!-- Keyed so each request gets a fresh prompt: nothing ticked or armed
		     for one request (the "always" box, a pending click) carries over. -->
		{#if requests.length > 0}
			{#key requests[0].request_id}
				<PermissionRequest
					request={requests[0]}
					queueCount={requests.length}
					onRespond={handleRespond}
				/>
			{/key}
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

</style>
