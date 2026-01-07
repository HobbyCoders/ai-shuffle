<script lang="ts">
	/**
	 * MarketplaceList - Displays and manages plugin marketplaces/sources
	 *
	 * Shows registered marketplaces with:
	 * - Sync button to pull latest from git
	 * - Remove button to unregister
	 * - Add button to add new marketplace
	 */
	import type { MarketplaceInfo } from '$lib/api/plugins';

	interface Props {
		marketplaces: MarketplaceInfo[];
		syncing: Record<string, boolean>;
		onSync: (id: string) => void;
		onRemove: (id: string) => void;
		onAdd: () => void;
	}

	let { marketplaces, syncing, onSync, onRemove, onAdd }: Props = $props();

	// Confirm delete state
	let confirmDelete = $state<string | null>(null);

	function handleRemove(id: string) {
		if (confirmDelete === id) {
			onRemove(id);
			confirmDelete = null;
		} else {
			confirmDelete = id;
		}
	}

	function cancelDelete() {
		confirmDelete = null;
	}

	// Format date for display
	function formatDate(dateStr: string | null): string {
		if (!dateStr) return 'Never synced';
		try {
			const date = new Date(dateStr);
			return date.toLocaleDateString(undefined, {
				month: 'short',
				day: 'numeric',
				hour: '2-digit',
				minute: '2-digit'
			});
		} catch {
			return 'Unknown';
		}
	}
</script>

<div class="p-5 space-y-4">
	<!-- Add new marketplace button -->
	<button
		onclick={onAdd}
		class="w-full px-4 py-3 rounded-lg border border-dashed border-border text-muted-foreground hover:text-foreground hover:border-primary/50 hover:bg-accent/50 transition-colors flex items-center justify-center gap-2"
	>
		<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
		</svg>
		Add Plugin Source
	</button>

	<!-- Marketplace list -->
	{#if marketplaces.length === 0}
		<div class="text-center py-8">
			<div class="w-12 h-12 mx-auto mb-3 rounded-full bg-muted flex items-center justify-center">
				<svg class="w-6 h-6 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
				</svg>
			</div>
			<p class="text-muted-foreground">No plugin sources configured</p>
			<p class="text-sm text-muted-foreground mt-1">Add a git repository to browse plugins</p>
		</div>
	{:else}
		<div class="space-y-3">
			{#each marketplaces as marketplace (marketplace.id)}
				<div class="border border-border rounded-lg bg-card overflow-hidden">
					<div class="px-4 py-3">
						<div class="flex items-start gap-3">
							<!-- Icon -->
							<div class="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center flex-shrink-0">
								<svg class="w-5 h-5 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
								</svg>
							</div>

							<!-- Info -->
							<div class="flex-1 min-w-0">
								<div class="flex items-center gap-2">
									<h3 class="font-medium text-foreground">{marketplace.id}</h3>
									<span class="px-1.5 py-0.5 text-xs bg-muted text-muted-foreground rounded">
										{marketplace.source}
									</span>
								</div>

								<p class="text-sm text-muted-foreground mt-0.5 truncate">
									{marketplace.url}
								</p>

								<div class="flex items-center gap-3 mt-2 text-xs text-muted-foreground">
									<span class="flex items-center gap-1">
										<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
										</svg>
										Last synced: {formatDate(marketplace.last_updated)}
									</span>
								</div>
							</div>

							<!-- Actions -->
							<div class="flex items-center gap-1 flex-shrink-0">
								<!-- Sync button -->
								<button
									onclick={() => onSync(marketplace.id)}
									disabled={syncing[marketplace.id]}
									class="p-2 hover:bg-muted rounded-lg transition-colors disabled:opacity-50"
									title="Sync from git"
								>
									{#if syncing[marketplace.id]}
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

								<!-- Remove button -->
								{#if confirmDelete === marketplace.id}
									<div class="flex items-center gap-1">
										<span class="text-xs text-destructive px-1">Remove?</span>
										<button
											onclick={() => handleRemove(marketplace.id)}
											class="p-1.5 hover:bg-destructive/10 rounded-md transition-colors"
											title="Confirm remove"
										>
											<svg class="w-4 h-4 text-destructive" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
											</svg>
										</button>
										<button
											onclick={cancelDelete}
											class="p-1.5 hover:bg-muted rounded-md transition-colors"
											title="Cancel"
										>
											<svg class="w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
											</svg>
										</button>
									</div>
								{:else}
									<button
										onclick={() => handleRemove(marketplace.id)}
										class="p-2 hover:bg-red-500/10 rounded-lg transition-colors"
										title="Remove source"
									>
										<svg class="w-4 h-4 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
										</svg>
									</button>
								{/if}
							</div>
						</div>
					</div>
				</div>
			{/each}
		</div>
	{/if}

	<!-- Info section -->
	<div class="mt-6 p-4 rounded-lg bg-muted/50 border border-border">
		<h4 class="text-sm font-medium text-foreground mb-2">About Plugin Sources</h4>
		<p class="text-sm text-muted-foreground">
			Plugin sources are git repositories containing Claude Code plugins.
			Official sources from Anthropic are automatically included.
			You can add custom sources to access community or private plugins.
		</p>
		<div class="mt-3 flex items-center gap-4 text-xs text-muted-foreground">
			<a
				href="https://github.com/anthropics/claude-plugins-official"
				target="_blank"
				rel="noopener noreferrer"
				class="flex items-center gap-1 hover:text-primary transition-colors"
			>
				<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24">
					<path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
				</svg>
				Official Plugins
			</a>
			<a
				href="https://docs.anthropic.com/en/docs/claude-code/plugins"
				target="_blank"
				rel="noopener noreferrer"
				class="flex items-center gap-1 hover:text-primary transition-colors"
			>
				<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
				</svg>
				Documentation
			</a>
		</div>
	</div>
</div>
