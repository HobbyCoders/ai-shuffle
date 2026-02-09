<script lang="ts">
	/**
	 * McpToolList - Displays discovered MCP tools
	 *
	 * Shows tools grouped by their source MCP server.
	 * Supports removing MCP servers (uninstalling tools installed via CLI).
	 */
	import type { McpTool } from '$lib/api/mcp';
	import { X } from 'lucide-svelte';

	interface Props {
		tools: McpTool[];
		loading?: boolean;
		onRefresh?: () => void;
		onRemoveServer?: (serverName: string) => void;
	}

	let { tools, loading = false, onRefresh, onRemoveServer }: Props = $props();

	let confirmingRemove = $state<string | null>(null);

	// Group tools by server
	let toolsByServer = $derived(() => {
		const groups: Record<string, McpTool[]> = {};
		for (const tool of tools) {
			if (!groups[tool.server]) {
				groups[tool.server] = [];
			}
			groups[tool.server].push(tool);
		}
		return groups;
	});

	let serverNames = $derived(Object.keys(toolsByServer()).sort());
</script>

<div class="space-y-4">
	<!-- Header with refresh -->
	<div class="flex items-center justify-between">
		<div>
			<h3 class="text-sm font-medium text-foreground">Discovered MCP Tools</h3>
			<p class="text-xs text-muted-foreground mt-0.5">
				{tools.length} tool{tools.length !== 1 ? 's' : ''} from {serverNames.length} server{serverNames.length !== 1 ? 's' : ''}
			</p>
		</div>
		{#if onRefresh}
			<button
				onclick={onRefresh}
				disabled={loading}
				class="p-2 hover:bg-muted rounded-lg transition-colors disabled:opacity-50"
				title="Refresh tools"
			>
				{#if loading}
					<svg class="w-4 h-4 text-primary animate-spin" fill="none" viewBox="0 0 24 24">
						<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
						<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
					</svg>
				{:else}
					<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
					</svg>
				{/if}
			</button>
		{/if}
	</div>

	<!-- Tools list -->
	{#if tools.length === 0}
		<div class="text-center py-8">
			<div class="w-12 h-12 mx-auto mb-3 rounded-full bg-muted flex items-center justify-center">
				<svg class="w-6 h-6 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
				</svg>
			</div>
			<p class="text-muted-foreground">No MCP tools discovered</p>
			<p class="text-sm text-muted-foreground mt-1">
				Configure MCP servers to expose tools
			</p>
		</div>
	{:else}
		<div class="space-y-4">
			{#each serverNames as serverName}
				<div class="border border-border rounded-lg overflow-hidden">
					<!-- Server header -->
					<div class="px-4 py-2 bg-muted/50 border-b border-border flex items-center gap-2">
						<svg class="w-4 h-4 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
						</svg>
						<span class="font-medium text-sm text-foreground flex-1">{serverName}</span>
						<span class="text-xs text-muted-foreground">
							({toolsByServer()[serverName].length} tool{toolsByServer()[serverName].length !== 1 ? 's' : ''})
						</span>
						{#if onRemoveServer}
							{#if confirmingRemove === serverName}
								<div class="flex items-center gap-1.5 ml-2">
									<button
										onclick={() => { onRemoveServer(serverName); confirmingRemove = null; }}
										class="text-xs px-2 py-0.5 rounded bg-destructive text-destructive-foreground hover:opacity-90 transition-opacity"
									>
										Remove
									</button>
									<button
										onclick={() => confirmingRemove = null}
										class="text-xs px-2 py-0.5 rounded bg-muted text-muted-foreground hover:text-foreground transition-colors"
									>
										Cancel
									</button>
								</div>
							{:else}
								<button
									onclick={() => confirmingRemove = serverName}
									class="p-1 hover:bg-muted rounded transition-colors text-muted-foreground hover:text-destructive ml-1"
									title="Remove MCP server"
								>
									<X size={14} />
								</button>
							{/if}
						{/if}
					</div>

					<!-- Tools -->
					<div class="divide-y divide-border">
						{#each toolsByServer()[serverName] as tool}
							<div class="px-4 py-2 flex items-start gap-3">
								<div class="w-6 h-6 rounded bg-primary/10 flex items-center justify-center flex-shrink-0 mt-0.5">
									<svg class="w-3 h-3 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
									</svg>
								</div>
								<div class="flex-1 min-w-0">
									<code class="text-sm font-mono text-foreground">{tool.name}</code>
									{#if tool.description}
										<p class="text-xs text-muted-foreground mt-0.5 line-clamp-2">
											{tool.description}
										</p>
									{/if}
								</div>
							</div>
						{/each}
					</div>
				</div>
			{/each}
		</div>
	{/if}

	<!-- Info box -->
	<div class="mt-4 p-3 rounded-lg bg-muted/50 border border-border">
		<p class="text-xs text-muted-foreground">
			MCP tools are discovered from the Claude CLI when you start a session.
			Tools are prefixed with <code class="text-foreground">mcp__server__</code> in prompts.
		</p>
	</div>
</div>
