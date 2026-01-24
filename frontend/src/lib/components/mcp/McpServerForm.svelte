<script lang="ts">
	/**
	 * McpServerForm - Modal form for adding/editing MCP servers
	 *
	 * Supports:
	 * - Server type selection (stdio/sse/http)
	 * - Dynamic fields based on type
	 * - Environment variables editor
	 * - Headers editor (for sse/http)
	 */
	import type { McpServerInfo, McpServerType, McpScope } from '$lib/api/mcp';
	import { validateMcpServerConfig } from '$lib/api/mcp';

	interface Props {
		show: boolean;
		server?: McpServerInfo | null;
		scope: McpScope;
		loading?: boolean;
		showScopeSelector?: boolean;
		onSave: (config: {
			name: string;
			type: McpServerType;
			command?: string;
			args?: string[];
			url?: string;
			env?: Record<string, string>;
			headers?: Record<string, string>;
			description?: string;
			scope?: 'local' | 'project';
		}) => void;
		onClose: () => void;
	}

	let {
		show,
		server = null,
		scope,
		loading = false,
		showScopeSelector = false,
		onSave,
		onClose
	}: Props = $props();

	// Form state
	let name = $state('');
	let type = $state<McpServerType>('stdio');
	let command = $state('');
	let argsText = $state('');
	let url = $state('');
	let description = $state('');
	let selectedScope = $state<'local' | 'project'>('local');

	// Key-value pairs for env and headers
	let envPairs = $state<Array<{ key: string; value: string }>>([]);
	let headerPairs = $state<Array<{ key: string; value: string }>>([]);

	// Validation
	let errors = $state<string[]>([]);

	// Determine if editing
	let isEditing = $derived(!!server);

	// Reset form when server changes
	$effect(() => {
		if (show) {
			if (server) {
				name = server.name;
				type = server.type as McpServerType;
				command = server.command || '';
				argsText = server.args?.join(' ') || '';
				url = server.url || '';
				description = server.description || '';
				envPairs = Object.entries(server.env || {}).map(([key, value]) => ({ key, value }));
				headerPairs = Object.entries(server.headers || {}).map(([key, value]) => ({ key, value }));
				selectedScope = (server.scope === 'local' || server.scope === 'project') ? server.scope : 'local';
			} else {
				// Reset for new server
				name = '';
				type = 'stdio';
				command = '';
				argsText = '';
				url = '';
				description = '';
				envPairs = [];
				headerPairs = [];
				selectedScope = 'local';
			}
			errors = [];
		}
	});

	function addEnvPair() {
		envPairs = [...envPairs, { key: '', value: '' }];
	}

	function removeEnvPair(index: number) {
		envPairs = envPairs.filter((_, i) => i !== index);
	}

	function addHeaderPair() {
		headerPairs = [...headerPairs, { key: '', value: '' }];
	}

	function removeHeaderPair(index: number) {
		headerPairs = headerPairs.filter((_, i) => i !== index);
	}

	function handleSubmit() {
		// Build config
		const config = {
			name: name.trim(),
			type,
			command: type === 'stdio' ? command.trim() : undefined,
			args: type === 'stdio' && argsText.trim() ? argsText.trim().split(/\s+/) : undefined,
			url: type !== 'stdio' ? url.trim() : undefined,
			env: envPairs.length > 0
				? Object.fromEntries(envPairs.filter(p => p.key.trim()).map(p => [p.key.trim(), p.value]))
				: undefined,
			headers: type !== 'stdio' && headerPairs.length > 0
				? Object.fromEntries(headerPairs.filter(p => p.key.trim()).map(p => [p.key.trim(), p.value]))
				: undefined,
			description: description.trim() || undefined,
			scope: showScopeSelector ? selectedScope : undefined
		};

		// Validate
		const validation = validateMcpServerConfig(config);
		if (!validation.valid) {
			errors = validation.errors;
			return;
		}

		errors = [];
		onSave(config);
	}

	function handleKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			onClose();
		}
	}
</script>

{#if show}
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<div
		class="fixed inset-0 z-50 flex items-center justify-center p-4"
		role="dialog"
		aria-modal="true"
		onkeydown={handleKeydown}
	>
		<!-- Backdrop -->
		<button
			class="absolute inset-0 bg-background/80 backdrop-blur-sm"
			onclick={onClose}
			aria-label="Close"
		></button>

		<!-- Modal -->
		<div class="relative w-full max-w-lg bg-card border border-border rounded-xl shadow-xl max-h-[90vh] flex flex-col">
			<!-- Header -->
			<div class="flex items-center justify-between px-6 py-4 border-b border-border">
				<h2 class="text-lg font-semibold text-foreground">
					{isEditing ? 'Edit MCP Server' : 'Add MCP Server'}
				</h2>
				<button
					onclick={onClose}
					class="p-2 hover:bg-muted rounded-lg transition-colors"
					aria-label="Close"
				>
					<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<!-- Content -->
			<div class="flex-1 overflow-y-auto p-6 space-y-4">
				<!-- Errors -->
				{#if errors.length > 0}
					<div class="p-3 bg-destructive/10 border border-destructive/20 rounded-lg">
						<ul class="text-sm text-destructive space-y-1">
							{#each errors as error}
								<li>{error}</li>
							{/each}
						</ul>
					</div>
				{/if}

				<!-- Name -->
				<div>
					<label for="mcp-name" class="block text-sm font-medium text-foreground mb-1.5">
						Server Name
					</label>
					<input
						id="mcp-name"
						type="text"
						bind:value={name}
						disabled={isEditing}
						placeholder="my-mcp-server"
						class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
					/>
					<p class="text-xs text-muted-foreground mt-1">
						Alphanumeric with hyphens/underscores only
					</p>
				</div>

				<!-- Type -->
				<div>
					<label class="block text-sm font-medium text-foreground mb-1.5">
						Server Type
					</label>
					<div class="flex gap-2">
						<button
							type="button"
							onclick={() => type = 'stdio'}
							class="flex-1 px-3 py-2 rounded-lg border text-sm font-medium transition-colors {type === 'stdio' ? 'bg-primary text-primary-foreground border-primary' : 'bg-background border-border text-muted-foreground hover:text-foreground'}"
						>
							Local Process
						</button>
						<button
							type="button"
							onclick={() => type = 'sse'}
							class="flex-1 px-3 py-2 rounded-lg border text-sm font-medium transition-colors {type === 'sse' ? 'bg-primary text-primary-foreground border-primary' : 'bg-background border-border text-muted-foreground hover:text-foreground'}"
						>
							SSE (Remote)
						</button>
						<button
							type="button"
							onclick={() => type = 'http'}
							class="flex-1 px-3 py-2 rounded-lg border text-sm font-medium transition-colors {type === 'http' ? 'bg-primary text-primary-foreground border-primary' : 'bg-background border-border text-muted-foreground hover:text-foreground'}"
						>
							HTTP
						</button>
					</div>
				</div>

				<!-- Scope selector (for project servers) -->
				{#if showScopeSelector}
					<div>
						<label class="block text-sm font-medium text-foreground mb-1.5">
							Scope
						</label>
						<div class="flex gap-2">
							<button
								type="button"
								onclick={() => selectedScope = 'local'}
								class="flex-1 px-3 py-2 rounded-lg border text-sm font-medium transition-colors {selectedScope === 'local' ? 'bg-primary text-primary-foreground border-primary' : 'bg-background border-border text-muted-foreground hover:text-foreground'}"
							>
								Local (gitignored)
							</button>
							<button
								type="button"
								onclick={() => selectedScope = 'project'}
								class="flex-1 px-3 py-2 rounded-lg border text-sm font-medium transition-colors {selectedScope === 'project' ? 'bg-primary text-primary-foreground border-primary' : 'bg-background border-border text-muted-foreground hover:text-foreground'}"
							>
								Project (committed)
							</button>
						</div>
					</div>
				{/if}

				<!-- Type-specific fields -->
				{#if type === 'stdio'}
					<!-- Command -->
					<div>
						<label for="mcp-command" class="block text-sm font-medium text-foreground mb-1.5">
							Command
						</label>
						<input
							id="mcp-command"
							type="text"
							bind:value={command}
							placeholder="node, python, npx, etc."
							class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
						/>
					</div>

					<!-- Args -->
					<div>
						<label for="mcp-args" class="block text-sm font-medium text-foreground mb-1.5">
							Arguments <span class="text-muted-foreground font-normal">(optional)</span>
						</label>
						<input
							id="mcp-args"
							type="text"
							bind:value={argsText}
							placeholder="./server.js --port 3000"
							class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
						/>
						<p class="text-xs text-muted-foreground mt-1">
							Space-separated arguments
						</p>
					</div>
				{:else}
					<!-- URL -->
					<div>
						<label for="mcp-url" class="block text-sm font-medium text-foreground mb-1.5">
							URL
						</label>
						<input
							id="mcp-url"
							type="url"
							bind:value={url}
							placeholder="https://api.example.com/mcp"
							class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
						/>
					</div>

					<!-- Headers -->
					<div>
						<div class="flex items-center justify-between mb-1.5">
							<label class="text-sm font-medium text-foreground">
								Headers <span class="text-muted-foreground font-normal">(optional)</span>
							</label>
							<button
								type="button"
								onclick={addHeaderPair}
								class="text-xs text-primary hover:underline"
							>
								+ Add header
							</button>
						</div>
						{#if headerPairs.length > 0}
							<div class="space-y-2">
								{#each headerPairs as pair, i}
									<div class="flex gap-2">
										<input
											type="text"
											bind:value={pair.key}
											placeholder="Header name"
											class="flex-1 px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground text-sm focus:outline-none focus:ring-2 focus:ring-ring"
										/>
										<input
											type="text"
											bind:value={pair.value}
											placeholder="Value (supports $&#123;VAR&#125;)"
											class="flex-1 px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground text-sm focus:outline-none focus:ring-2 focus:ring-ring"
										/>
										<button
											type="button"
											onclick={() => removeHeaderPair(i)}
											class="p-2 hover:bg-destructive/10 rounded-lg transition-colors"
										>
											<svg class="w-4 h-4 text-destructive" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
											</svg>
										</button>
									</div>
								{/each}
							</div>
						{:else}
							<p class="text-xs text-muted-foreground">No headers configured</p>
						{/if}
					</div>
				{/if}

				<!-- Environment Variables (for all types) -->
				<div>
					<div class="flex items-center justify-between mb-1.5">
						<label class="text-sm font-medium text-foreground">
							Environment Variables <span class="text-muted-foreground font-normal">(optional)</span>
						</label>
						<button
							type="button"
							onclick={addEnvPair}
							class="text-xs text-primary hover:underline"
						>
							+ Add variable
						</button>
					</div>
					{#if envPairs.length > 0}
						<div class="space-y-2">
							{#each envPairs as pair, i}
								<div class="flex gap-2">
									<input
										type="text"
										bind:value={pair.key}
										placeholder="VARIABLE_NAME"
										class="flex-1 px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground text-sm focus:outline-none focus:ring-2 focus:ring-ring"
									/>
									<input
										type="text"
										bind:value={pair.value}
										placeholder="Value (supports $&#123;VAR:-default&#125;)"
										class="flex-1 px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground text-sm focus:outline-none focus:ring-2 focus:ring-ring"
									/>
									<button
										type="button"
										onclick={() => removeEnvPair(i)}
										class="p-2 hover:bg-destructive/10 rounded-lg transition-colors"
									>
										<svg class="w-4 h-4 text-destructive" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
										</svg>
									</button>
								</div>
							{/each}
						</div>
					{:else}
						<p class="text-xs text-muted-foreground">No environment variables configured</p>
					{/if}
				</div>

				<!-- Description -->
				<div>
					<label for="mcp-description" class="block text-sm font-medium text-foreground mb-1.5">
						Description <span class="text-muted-foreground font-normal">(optional)</span>
					</label>
					<input
						id="mcp-description"
						type="text"
						bind:value={description}
						placeholder="What this server provides"
						class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
					/>
				</div>
			</div>

			<!-- Footer -->
			<div class="flex items-center justify-end gap-3 px-6 py-4 border-t border-border">
				<button
					onclick={onClose}
					disabled={loading}
					class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
				>
					Cancel
				</button>
				<button
					onclick={handleSubmit}
					disabled={loading}
					class="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center gap-2"
				>
					{#if loading}
						<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
					{/if}
					{isEditing ? 'Save Changes' : 'Add Server'}
				</button>
			</div>
		</div>
	</div>
{/if}
