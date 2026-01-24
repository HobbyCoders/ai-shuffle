<script lang="ts">
	/**
	 * McpServerList - Displays and manages MCP servers
	 *
	 * Shows configured MCP servers with:
	 * - Type badge (stdio/sse/http)
	 * - Edit button
	 * - Remove button with confirmation
	 * - Add button to add new servers
	 */
	import type { McpServerInfo, McpScope } from '$lib/api/mcp';
	import { getServerTypeLabel, getScopeLabel } from '$lib/api/mcp';

	interface Props {
		servers: McpServerInfo[];
		scope: McpScope;
		loading?: boolean;
		onEdit?: (server: McpServerInfo) => void;
		onRemove?: (serverName: string, scope: McpScope) => void;
		onAdd?: () => void;
		showScope?: boolean;
		readonly?: boolean;
	}

	let {
		servers,
		scope,
		loading = false,
		onEdit,
		onRemove,
		onAdd,
		showScope = false,
		readonly = false
	}: Props = $props();

	// Confirm delete state
	let confirmDelete = $state<string | null>(null);

	function handleRemove(serverName: string) {
		if (confirmDelete === serverName) {
			onRemove?.(serverName, scope);
			confirmDelete = null;
		} else {
			confirmDelete = serverName;
		}
	}

	function cancelDelete() {
		confirmDelete = null;
	}

	// Get icon for server type
	function getTypeIcon(type: string): string {
		switch (type) {
			case 'stdio':
				return 'M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z'; // Terminal
			case 'sse':
				return 'M13 10V3L4 14h7v7l9-11h-7z'; // Lightning
			case 'http':
				return 'M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9'; // Globe
			default:
				return 'M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z'; // Chip
		}
	}

	// Get badge color for type
	function getTypeBadgeClass(type: string): string {
		switch (type) {
			case 'stdio':
				return 'bg-blue-500/10 text-blue-500';
			case 'sse':
				return 'bg-yellow-500/10 text-yellow-500';
			case 'http':
				return 'bg-green-500/10 text-green-500';
			default:
				return 'bg-muted text-muted-foreground';
		}
	}
</script>

<div class="space-y-3">
	<!-- Add new server button -->
	{#if !readonly && onAdd}
		<button
			onclick={onAdd}
			disabled={loading}
			class="w-full px-4 py-3 rounded-lg border border-dashed border-border text-muted-foreground hover:text-foreground hover:border-primary/50 add-btn-hover transition-colors flex items-center justify-center gap-2 disabled:opacity-50"
		>
			<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
			</svg>
			Add MCP Server
		</button>
	{/if}

	<!-- Server list -->
	{#if servers.length === 0}
		<div class="text-center py-8">
			<div class="w-12 h-12 mx-auto mb-3 rounded-full bg-muted flex items-center justify-center">
				<svg class="w-6 h-6 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
				</svg>
			</div>
			<p class="text-muted-foreground">No MCP servers configured</p>
			<p class="text-sm text-muted-foreground mt-1">Add an MCP server to extend Claude's capabilities</p>
		</div>
	{:else}
		<div class="space-y-2">
			{#each servers as server (server.name)}
				<div class="border border-border rounded-lg bg-card overflow-hidden hover:border-border/80 transition-colors">
					<div class="px-4 py-3">
						<div class="flex items-start gap-3">
							<!-- Icon -->
							<div class="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0">
								<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={getTypeIcon(server.type)} />
								</svg>
							</div>

							<!-- Info -->
							<div class="flex-1 min-w-0">
								<div class="flex items-center gap-2 flex-wrap">
									<h3 class="font-medium text-foreground">{server.name}</h3>
									<span class="px-1.5 py-0.5 text-xs rounded {getTypeBadgeClass(server.type)}">
										{getServerTypeLabel(server.type)}
									</span>
									{#if showScope}
										<span class="px-1.5 py-0.5 text-xs bg-muted text-muted-foreground rounded">
											{getScopeLabel(server.scope)}
										</span>
									{/if}
								</div>

								{#if server.description}
									<p class="text-sm text-muted-foreground mt-0.5 line-clamp-2">
										{server.description}
									</p>
								{/if}

								<!-- Connection info -->
								<div class="flex items-center gap-3 mt-2 text-xs text-muted-foreground">
									{#if server.type === 'stdio' && server.command}
										<span class="flex items-center gap-1 truncate">
											<svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
											</svg>
											<code class="truncate">{server.command}{server.args ? ' ' + server.args.join(' ') : ''}</code>
										</span>
									{:else if (server.type === 'sse' || server.type === 'http') && server.url}
										<span class="flex items-center gap-1 truncate">
											<svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
											</svg>
											<span class="truncate">{server.url}</span>
										</span>
									{/if}
								</div>
							</div>

							<!-- Actions -->
							{#if !readonly}
								<div class="flex items-center gap-1 flex-shrink-0">
									<!-- Edit button -->
									{#if onEdit}
										<button
											onclick={() => onEdit(server)}
											disabled={loading}
											class="p-2 action-btn rounded-lg transition-colors disabled:opacity-50"
											title="Edit server"
										>
											<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
											</svg>
										</button>
									{/if}

									<!-- Remove button -->
									{#if onRemove}
										{#if confirmDelete === server.name}
											<div class="flex items-center gap-1">
												<span class="text-xs text-destructive px-1">Remove?</span>
												<button
													onclick={() => handleRemove(server.name)}
													class="p-1.5 hover:bg-destructive/10 rounded-md transition-colors"
													title="Confirm remove"
												>
													<svg class="w-4 h-4 text-destructive" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
													</svg>
												</button>
												<button
													onclick={cancelDelete}
													class="p-1.5 action-btn rounded-md transition-colors"
													title="Cancel"
												>
													<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
													</svg>
												</button>
											</div>
										{:else}
											<button
												onclick={() => handleRemove(server.name)}
												disabled={loading}
												class="p-2 hover:bg-red-500/10 rounded-lg transition-colors disabled:opacity-50"
												title="Remove server"
											>
												<svg class="w-4 h-4 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
												</svg>
											</button>
										{/if}
									{/if}
								</div>
							{/if}
						</div>
					</div>
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.action-btn:hover {
		background: var(--hover-overlay);
	}

	.action-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.add-btn-hover:hover {
		background: color-mix(in oklch, var(--accent) 10%, transparent);
	}
</style>
