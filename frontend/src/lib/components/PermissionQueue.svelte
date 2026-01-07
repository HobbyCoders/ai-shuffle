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

	// Get tool icon for compact display
	function getToolIcon(toolName: string): string {
		const icons: Record<string, string> = {
			'Bash': 'terminal',
			'Read': 'file',
			'Write': 'file-plus',
			'Edit': 'edit',
			'Glob': 'search',
			'Grep': 'search',
			'WebFetch': 'globe',
			'WebSearch': 'search',
			'Task': 'cpu',
			'NotebookEdit': 'book'
		};
		return icons[toolName] || 'tool';
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
</script>

{#if requests.length > 0}
	<div class="permission-queue">
		<!-- Pending Queue Header (when multiple) -->
		{#if requests.length > 1}
			<div class="mb-3 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 px-1">
				<div class="flex items-center gap-2">
					<!-- Animated Warning Icon -->
					<div class="relative">
						<svg class="w-5 h-5 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
						</svg>
						<span class="absolute -top-1 -right-1 w-2 h-2 bg-amber-500 rounded-full animate-ping"></span>
					</div>
					<span class="text-sm font-medium text-foreground">
						{requests.length} permissions pending
					</span>
				</div>

				<div class="flex items-center gap-2">
					<!-- Toggle Pending List -->
					<button
						type="button"
						onclick={() => showPendingList = !showPendingList}
						class="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors"
					>
						<svg class="w-3.5 h-3.5 transition-transform {showPendingList ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
						</svg>
						{showPendingList ? 'Hide queue' : 'Show queue'}
					</button>

					<!-- Deny All Button -->
					<button
						type="button"
						onclick={handleDenyAll}
						class="px-2.5 py-1 text-xs font-medium rounded-lg
							bg-red-500/10 text-red-400/80 border border-red-500/20
							hover:bg-red-500/20 hover:text-red-400 hover:border-red-500/30
							transition-all duration-150"
					>
						Deny All
					</button>
				</div>
			</div>

			<!-- Pending Queue List (collapsible) -->
			{#if showPendingList}
				<div class="mb-3 p-3 bg-muted/20 rounded-xl border border-border/50 animate-in slide-in-from-top-2">
					<div class="text-xs text-muted-foreground mb-2 font-medium">Queued requests:</div>
					<div class="flex flex-wrap gap-1.5">
						{#each requests.slice(1) as req, idx}
							<div class="inline-flex items-center gap-1.5 px-2 py-1 bg-muted/40 rounded-lg text-xs border border-border/30">
								<span class="text-muted-foreground">{idx + 2}.</span>
								<span class="font-medium text-foreground">{req.tool_name}</span>
								{#if getShortSummary(req)}
									<span class="text-muted-foreground truncate max-w-[100px] sm:max-w-[150px]" title={getShortSummary(req)}>
										{getShortSummary(req)}
									</span>
								{/if}
							</div>
						{/each}
					</div>
					<div class="mt-2 text-xs text-muted-foreground/80">
						Use "Allow All Similar" to approve multiple matching requests at once
					</div>
				</div>
			{/if}
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
	.animate-in {
		animation: animate-in 0.2s ease-out;
	}

	.slide-in-from-top-2 {
		--tw-enter-translate-y: -0.5rem;
	}

	@keyframes animate-in {
		from {
			opacity: 0;
			transform: translateY(var(--tw-enter-translate-y, 0));
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}
</style>
