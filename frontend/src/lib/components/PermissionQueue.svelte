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

	// Group requests by tool name for batch operations
	const groupedByTool = $derived(() => {
		const groups: Record<string, PermissionRequestData[]> = {};
		for (const req of requests) {
			if (!groups[req.tool_name]) {
				groups[req.tool_name] = [];
			}
			groups[req.tool_name].push(req);
		}
		return groups;
	});

	function handleRespond(event: CustomEvent<{
		request_id: string;
		decision: 'allow' | 'deny';
		remember?: 'none' | 'session' | 'profile';
		pattern?: string;
	}>) {
		dispatch('respond', event.detail);
	}

	function handleDismissAll() {
		// Deny all pending requests
		for (const req of requests) {
			dispatch('respond', {
				request_id: req.request_id,
				decision: 'deny',
				remember: 'none'
			});
		}
		dispatch('dismissAll');
	}
</script>

{#if requests.length > 0}
	<div class="permission-queue space-y-3">
		<!-- Queue Header if multiple requests -->
		{#if requests.length > 1}
			<div class="flex items-center justify-between px-2 py-1.5 bg-amber-900/10 rounded-lg border border-amber-500/30">
				<div class="flex items-center gap-2">
					<svg class="w-4 h-4 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
					</svg>
					<span class="text-sm font-medium text-amber-400">
						{requests.length} tool permissions pending
					</span>
				</div>
				<button
					type="button"
					onclick={handleDismissAll}
					class="text-xs text-muted-foreground hover:text-red-400 transition-colors"
				>
					Deny All
				</button>
			</div>
		{/if}

		<!-- Show first request prominently -->
		{#if requests.length > 0}
			<PermissionRequest
				request={requests[0]}
				isFirst={true}
				queueCount={requests.length}
				on:respond={handleRespond}
			/>
		{/if}

		<!-- Show summary of other pending requests -->
		{#if requests.length > 1}
			<div class="px-3 py-2 bg-muted/20 rounded-lg border border-border">
				<div class="text-xs text-muted-foreground mb-2">More pending requests:</div>
				<div class="flex flex-wrap gap-2">
					{#each requests.slice(1) as req}
						<div class="px-2 py-1 bg-muted/30 rounded text-xs text-muted-foreground flex items-center gap-1.5">
							<span class="font-medium text-foreground">{req.tool_name}</span>
							{#if req.tool_input.command}
								<span class="truncate max-w-[120px]" title={String(req.tool_input.command)}>
									{String(req.tool_input.command).split(' ')[0]}
								</span>
							{:else if req.tool_input.file_path}
								<span class="truncate max-w-[120px]" title={String(req.tool_input.file_path)}>
									{String(req.tool_input.file_path).split('/').pop()}
								</span>
							{:else if req.tool_input.path}
								<span class="truncate max-w-[120px]" title={String(req.tool_input.path)}>
									{String(req.tool_input.path).split('/').pop()}
								</span>
							{/if}
						</div>
					{/each}
				</div>
				<div class="mt-2 text-xs text-muted-foreground">
					Tip: Use "Allow All Similar" to approve multiple requests at once
				</div>
			</div>
		{/if}
	</div>
{/if}
