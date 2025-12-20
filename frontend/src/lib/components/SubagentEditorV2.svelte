<script lang="ts">
	/**
	 * SubagentEditorV2 - Redesigned modal for creating/editing subagents
	 *
	 * Clean tabbed interface:
	 * - Tab 1: Identity (ID, Name, Description)
	 * - Tab 2: Configuration (Model, Tools)
	 * - Tab 3: Prompt (System prompt)
	 *
	 * Mobile-first responsive design with no nested accordions.
	 */
	import { api } from '$lib/api/client';

	// Tool interfaces
	interface ToolInfo {
		name: string;
		category: string;
		description: string;
		mcp_server?: string;
	}

	interface ToolCategory {
		id: string;
		name: string;
		description: string;
		tools: ToolInfo[];
	}

	interface ToolsResponse {
		categories: ToolCategory[];
		all_tools: ToolInfo[];
	}

	// Model options with descriptions
	const MODEL_OPTIONS = [
		{ value: '', label: 'Inherit', description: 'Use profile default' },
		{ value: 'haiku', label: 'Haiku', description: 'Fast & cheap' },
		{ value: 'sonnet', label: 'Sonnet', description: 'Balanced' },
		{ value: 'sonnet-1m', label: 'Sonnet 1M', description: 'Extended context' },
		{ value: 'opus', label: 'Opus', description: 'Most capable' }
	];

	interface Subagent {
		id?: string;
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
		model?: string;
	}

	interface Props {
		profileId?: string; // Optional for global subagent mode
		subagent?: Subagent | null;
		isGlobal?: boolean; // True if managing global subagents (not profile-specific)
		onClose: () => void;
		onSave: () => void;
	}

	let { profileId, subagent = null, isGlobal = false, onClose, onSave }: Props = $props();

	// Active tab
	type Tab = 'identity' | 'config' | 'prompt';
	let activeTab = $state<Tab>('identity');

	// Form state
	let formId = $state(subagent?.id || '');
	let formName = $state(subagent?.name || '');
	let formDescription = $state(subagent?.description || '');
	let formPrompt = $state(subagent?.prompt || '');
	let formTools = $state<string[]>(subagent?.tools || []);
	let formModel = $state(subagent?.model || '');

	// UI state
	let saving = $state(false);
	let error = $state<string | null>(null);
	let availableTools = $state<ToolsResponse>({ categories: [], all_tools: [] });
	let toolSelectionMode = $state<'all' | 'select'>(
		subagent?.tools && subagent.tools.length > 0 ? 'select' : 'all'
	);

	const isEditMode = $derived(!!subagent);

	// Validation per tab
	const identityValid = $derived(() => {
		if (!isEditMode && !formId.match(/^[a-z0-9-]+$/)) return false;
		if (isGlobal && formId.length === 0) return false;
		if (formName.length === 0) return false;
		if (formDescription.length === 0) return false;
		return true;
	});

	const configValid = $derived(() => {
		// Config is always valid - model has default, tools are optional
		return true;
	});

	const promptValid = $derived(() => {
		return formPrompt.length > 0;
	});

	const isValid = $derived(() => identityValid() && configValid() && promptValid());

	// Tab validation indicators
	const tabStatus = $derived(() => ({
		identity: identityValid(),
		config: configValid(),
		prompt: promptValid()
	}));

	// Load tools on mount
	$effect(() => {
		loadTools();
	});

	async function loadTools() {
		try {
			availableTools = await api.get<ToolsResponse>('/tools');
		} catch (e) {
			console.error('Failed to load tools:', e);
		}
	}

	// Tool selection helpers
	function toggleTool(toolName: string) {
		if (formTools.includes(toolName)) {
			formTools = formTools.filter(t => t !== toolName);
		} else {
			formTools = [...formTools, toolName];
		}
	}

	function selectAllInCategory(category: ToolCategory) {
		const toolNames = category.tools.map(t => t.name);
		const allSelected = toolNames.every(name => formTools.includes(name));

		if (allSelected) {
			formTools = formTools.filter(t => !toolNames.includes(t));
		} else {
			const newTools = toolNames.filter(name => !formTools.includes(name));
			formTools = [...formTools, ...newTools];
		}
	}

	function getCategorySelectionState(category: ToolCategory): 'none' | 'partial' | 'all' {
		const toolNames = category.tools.map(t => t.name);
		const selectedCount = toolNames.filter(name => formTools.includes(name)).length;
		if (selectedCount === 0) return 'none';
		if (selectedCount === toolNames.length) return 'all';
		return 'partial';
	}

	function selectAllTools() {
		formTools = availableTools.all_tools.map(t => t.name);
	}

	function clearAllTools() {
		formTools = [];
	}

	// Save handler
	async function handleSave() {
		if (!isValid()) return;

		saving = true;
		error = null;

		try {
			const body: Record<string, unknown> = {
				name: formName,
				description: formDescription,
				prompt: formPrompt,
			};

			if (toolSelectionMode === 'select' && formTools.length > 0) {
				body.tools = formTools;
			}
			if (formModel) {
				body.model = formModel;
			}

			if (isGlobal) {
				// Global subagent API
				if (isEditMode) {
					await api.put(`/subagents/${subagent!.id}`, body);
				} else {
					body.id = formId;
					await api.post('/subagents', body);
				}
			} else {
				// Profile-specific API
				if (isEditMode) {
					await api.put(`/profiles/${profileId}/agents/${subagent!.name}`, body);
				} else {
					body.name = formName;
					await api.post(`/profiles/${profileId}/agents`, body);
				}
			}

			onSave();
			onClose();
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to save subagent';
		} finally {
			saving = false;
		}
	}

	// Keyboard handler
	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			onClose();
		}
	}

	// Tab navigation
	const tabs: { id: Tab; label: string; icon: string }[] = [
		{ id: 'identity', label: 'Identity', icon: 'M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z' },
		{ id: 'config', label: 'Config', icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z' },
		{ id: 'prompt', label: 'Prompt', icon: 'M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z' }
	];
</script>

<svelte:window on:keydown={handleKeydown} />

<!-- Modal backdrop -->
<div
	class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-2 sm:p-4"
	onclick={(e) => e.target === e.currentTarget && onClose()}
	role="dialog"
	aria-modal="true"
>
	<!-- Modal content -->
	<div class="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-2xl max-h-[95vh] sm:max-h-[90vh] overflow-hidden flex flex-col">
		<!-- Header -->
		<div class="flex items-center justify-between px-4 sm:px-6 py-4 border-b border-border bg-gradient-to-r from-primary/5 to-transparent">
			<div>
				<h2 class="text-lg font-semibold text-foreground">
					{isEditMode ? 'Edit Subagent' : 'New Subagent'}
				</h2>
				{#if isEditMode && subagent?.id}
					<p class="text-xs text-muted-foreground font-mono">{subagent.id}</p>
				{/if}
			</div>
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

		<!-- Tab Navigation - Horizontal on mobile, Vertical on desktop -->
		<div class="flex flex-col md:flex-row flex-1 overflow-hidden">
			<nav class="shrink-0 border-b md:border-b-0 md:border-r border-border bg-muted/30">
				<!-- Mobile: Horizontal tabs -->
				<div class="flex md:hidden">
					{#each tabs as tab}
						<button
							onclick={() => activeTab = tab.id}
							class="flex-1 flex items-center justify-center gap-2 px-4 py-3 text-sm font-medium transition-all relative
								{activeTab === tab.id
									? 'text-primary'
									: 'text-muted-foreground hover:text-foreground'}"
						>
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={tab.icon} />
							</svg>
							<span class="hidden sm:inline">{tab.label}</span>

							<!-- Validation indicator -->
							{#if !tabStatus()[tab.id]}
								<span class="absolute top-2 right-2 sm:relative sm:top-0 sm:right-0 w-2 h-2 rounded-full bg-amber-500"></span>
							{/if}

							<!-- Active indicator -->
							{#if activeTab === tab.id}
								<div class="absolute bottom-0 left-0 right-0 h-0.5 bg-primary"></div>
							{/if}
						</button>
					{/each}
				</div>
				<!-- Desktop: Vertical tabs -->
				<div class="hidden md:flex flex-col w-36 p-3 space-y-1">
					{#each tabs as tab}
						<button
							onclick={() => activeTab = tab.id}
							class="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all relative
								{activeTab === tab.id
									? 'bg-primary/10 text-primary'
									: 'text-muted-foreground hover:text-foreground hover:bg-muted/50'}"
						>
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d={tab.icon} />
							</svg>
							<span>{tab.label}</span>

							<!-- Validation indicator -->
							{#if !tabStatus()[tab.id]}
								<span class="absolute top-1 right-1 w-2 h-2 rounded-full bg-amber-500"></span>
							{/if}
						</button>
					{/each}
				</div>
			</nav>

		<!-- Tab Content -->
		<div class="flex-1 overflow-y-auto">
			{#if error}
				<div class="mx-4 sm:mx-6 mt-4 p-3 bg-red-500/10 border border-red-500/20 rounded-lg text-red-500 text-sm flex items-center gap-2">
					<svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
					</svg>
					<span>{error}</span>
					<button onclick={() => error = null} class="ml-auto hover:text-red-400">
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				</div>
			{/if}

			<!-- Identity Tab -->
			{#if activeTab === 'identity'}
				<div class="p-4 sm:p-6 space-y-5">
					<!-- ID Field (only for new global subagents) -->
					{#if isGlobal && !isEditMode}
						<div class="space-y-2">
							<label for="agent-id" class="block text-sm font-medium text-foreground">
								ID <span class="text-red-500">*</span>
							</label>
							<input
								id="agent-id"
								type="text"
								bind:value={formId}
								placeholder="my-agent-id"
								class="w-full px-4 py-2.5 bg-background border border-border rounded-xl text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary transition-all"
							/>
							<p class="text-xs text-muted-foreground">
								Lowercase letters, numbers, and hyphens only. Cannot be changed later.
							</p>
						</div>
					{/if}

					<!-- Name Field -->
					<div class="space-y-2">
						<label for="agent-name" class="block text-sm font-medium text-foreground">
							Display Name <span class="text-red-500">*</span>
						</label>
						<input
							id="agent-name"
							type="text"
							bind:value={formName}
							placeholder="Research Assistant"
							class="w-full px-4 py-2.5 bg-background border border-border rounded-xl text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary transition-all"
						/>
						<p class="text-xs text-muted-foreground">
							Human-readable name shown in the UI
						</p>
					</div>

					<!-- Description Field -->
					<div class="space-y-2">
						<label for="agent-description" class="block text-sm font-medium text-foreground">
							Description <span class="text-red-500">*</span>
						</label>
						<textarea
							id="agent-description"
							bind:value={formDescription}
							placeholder="Use this agent when you need to research topics, find information, or explore ideas..."
							rows={3}
							class="w-full px-4 py-2.5 bg-background border border-border rounded-xl text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary transition-all resize-none"
						></textarea>
						<p class="text-xs text-muted-foreground">
							Describes when Claude should invoke this subagent
						</p>
					</div>
				</div>
			{/if}

			<!-- Config Tab -->
			{#if activeTab === 'config'}
				<div class="p-4 sm:p-6 space-y-6">
					<!-- Model Selection -->
					<div class="space-y-3">
						<label class="block text-sm font-medium text-foreground">Model</label>
						<div class="grid grid-cols-2 sm:grid-cols-5 gap-2">
							{#each MODEL_OPTIONS as opt}
								<button
									type="button"
									onclick={() => formModel = opt.value}
									class="flex flex-col items-center gap-1 p-3 rounded-xl border transition-all
										{formModel === opt.value
											? 'bg-primary/10 border-primary text-primary'
											: 'bg-background border-border text-foreground hover:border-primary/50'}"
								>
									<span class="text-sm font-medium">{opt.label}</span>
									<span class="text-[10px] text-muted-foreground">{opt.description}</span>
								</button>
							{/each}
						</div>
					</div>

					<!-- Tool Access Mode -->
					<div class="space-y-3">
						<div class="flex items-center justify-between">
							<label class="block text-sm font-medium text-foreground">Tool Access</label>
							{#if toolSelectionMode === 'select'}
								<span class="text-xs text-muted-foreground">
									{formTools.length} selected
								</span>
							{/if}
						</div>

						<div class="flex gap-2">
							<button
								type="button"
								onclick={() => { toolSelectionMode = 'all'; formTools = []; }}
								class="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl border transition-all
									{toolSelectionMode === 'all'
										? 'bg-primary text-primary-foreground border-primary'
										: 'bg-background border-border text-foreground hover:border-primary/50'}"
							>
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
								</svg>
								All Tools
							</button>
							<button
								type="button"
								onclick={() => toolSelectionMode = 'select'}
								class="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl border transition-all
									{toolSelectionMode === 'select'
										? 'bg-primary text-primary-foreground border-primary'
										: 'bg-background border-border text-foreground hover:border-primary/50'}"
							>
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4" />
								</svg>
								Custom
							</button>
						</div>

						{#if toolSelectionMode === 'all'}
							<p class="text-xs text-muted-foreground bg-muted/50 rounded-lg p-3">
								The subagent will have access to all tools available in the profile.
							</p>
						{/if}
					</div>

					<!-- Tool Selection (only when custom mode) -->
					{#if toolSelectionMode === 'select'}
						<div class="space-y-4">
							<!-- Quick actions -->
							<div class="flex gap-2 text-xs">
								<button
									type="button"
									onclick={selectAllTools}
									class="text-primary hover:underline"
								>
									Select all
								</button>
								<span class="text-muted-foreground">•</span>
								<button
									type="button"
									onclick={clearAllTools}
									class="text-primary hover:underline"
								>
									Clear all
								</button>
							</div>

							<!-- Tool categories -->
							<div class="space-y-4">
								{#each availableTools.categories as category}
									{#if category.tools.length > 0}
										<div class="space-y-2">
											<!-- Category header with select all -->
											<button
												type="button"
												onclick={() => selectAllInCategory(category)}
												class="flex items-center gap-2 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
											>
												<!-- Checkbox indicator -->
												<span class="w-4 h-4 rounded flex items-center justify-center transition-colors
													{getCategorySelectionState(category) === 'all'
														? 'bg-primary'
														: getCategorySelectionState(category) === 'partial'
															? 'bg-primary/50'
															: 'bg-muted border border-border'}">
													{#if getCategorySelectionState(category) !== 'none'}
														<svg class="w-3 h-3 text-primary-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															{#if getCategorySelectionState(category) === 'all'}
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
															{:else}
																<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 12h14" />
															{/if}
														</svg>
													{/if}
												</span>
												<span class="uppercase tracking-wider">{category.name}</span>
											</button>

											<!-- Tool pills -->
											<div class="flex flex-wrap gap-2">
												{#each category.tools as tool}
													<button
														type="button"
														onclick={() => toggleTool(tool.name)}
														class="group relative px-3 py-1.5 text-sm rounded-lg border transition-all
															{formTools.includes(tool.name)
																? 'bg-primary/10 border-primary text-primary'
																: 'bg-background border-border text-foreground hover:border-primary/50'}"
														title={tool.description}
													>
														{tool.name}
														<!-- Tooltip on hover -->
														<span class="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-2 py-1 text-xs bg-popover text-popover-foreground rounded shadow-lg opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none whitespace-nowrap z-10 hidden sm:block">
															{tool.description}
														</span>
													</button>
												{/each}
											</div>
										</div>
									{/if}
								{/each}
							</div>

							{#if formTools.length === 0}
								<p class="text-xs text-amber-500 bg-amber-500/10 rounded-lg p-3">
									No tools selected. The subagent won't be able to perform any actions.
								</p>
							{/if}
						</div>
					{/if}
				</div>
			{/if}

			<!-- Prompt Tab -->
			{#if activeTab === 'prompt'}
				<div class="p-4 sm:p-6 h-full flex flex-col">
					<div class="flex-1 flex flex-col space-y-2">
						<div class="flex items-center justify-between">
							<label for="agent-prompt" class="block text-sm font-medium text-foreground">
								System Prompt <span class="text-red-500">*</span>
							</label>
							<span class="text-xs text-muted-foreground">
								{formPrompt.length} characters
							</span>
						</div>
						<textarea
							id="agent-prompt"
							bind:value={formPrompt}
							placeholder="You are a specialized agent that helps users with..."
							class="flex-1 min-h-[300px] sm:min-h-[400px] w-full px-4 py-3 bg-background border border-border rounded-xl text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary transition-all font-mono text-sm resize-none"
						></textarea>
						<p class="text-xs text-muted-foreground">
							Instructions that define the subagent's behavior, personality, and capabilities.
						</p>
					</div>
				</div>
			{/if}
		</div>
		</div>

		<!-- Footer -->
		<div class="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 px-4 sm:px-6 py-4 border-t border-border bg-muted/30">
			<!-- Progress indicator -->
			<div class="hidden sm:flex items-center gap-2 text-xs text-muted-foreground">
				<span class="flex items-center gap-1">
					{#if tabStatus().identity}
						<svg class="w-4 h-4 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>
					{:else}
						<svg class="w-4 h-4 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01" />
						</svg>
					{/if}
					Identity
				</span>
				<span class="text-muted-foreground/50">→</span>
				<span class="flex items-center gap-1">
					{#if tabStatus().config}
						<svg class="w-4 h-4 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>
					{:else}
						<svg class="w-4 h-4 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01" />
						</svg>
					{/if}
					Config
				</span>
				<span class="text-muted-foreground/50">→</span>
				<span class="flex items-center gap-1">
					{#if tabStatus().prompt}
						<svg class="w-4 h-4 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>
					{:else}
						<svg class="w-4 h-4 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01" />
						</svg>
					{/if}
					Prompt
				</span>
			</div>

			<!-- Action buttons -->
			<div class="flex flex-col-reverse sm:flex-row gap-2">
				<button
					type="button"
					onclick={onClose}
					class="px-4 py-2.5 text-sm font-medium text-foreground bg-background border border-border rounded-xl hover:bg-muted transition-colors"
				>
					Cancel
				</button>
				<button
					type="button"
					onclick={handleSave}
					disabled={!isValid() || saving}
					class="px-6 py-2.5 text-sm font-medium text-primary-foreground bg-primary rounded-xl hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
				>
					{#if saving}
						<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
						Saving...
					{:else}
						{isEditMode ? 'Save Changes' : 'Create Subagent'}
					{/if}
				</button>
			</div>
		</div>
	</div>
</div>
