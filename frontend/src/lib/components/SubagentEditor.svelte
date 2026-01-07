<script lang="ts">
	/**
	 * SubagentEditor - Modal for creating/editing subagents
	 *
	 * Responsive design that works on both mobile and desktop.
	 * Supports CRUD operations on subagents within a profile.
	 */
	import { api } from '$lib/api/client';

	// Available tools that can be assigned to subagents
	const AVAILABLE_TOOLS = [
		'Read',
		'Write',
		'Edit',
		'Grep',
		'Glob',
		'Bash',
		'WebFetch',
		'WebSearch',
		'Task',
		'NotebookEdit'
	];

	// Model options
	const MODEL_OPTIONS = [
		{ value: '', label: 'Inherit from profile' },
		{ value: 'haiku', label: 'Haiku (fast, cheap)' },
		{ value: 'sonnet', label: 'Sonnet (balanced)' },
		{ value: 'opus', label: 'Opus (powerful)' }
	];

	interface Subagent {
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
		model?: string;
	}

	interface Props {
		profileId: string;
		subagent?: Subagent | null; // null = create mode, defined = edit mode
		onClose: () => void;
		onSave: () => void;
	}

	let { profileId, subagent = null, onClose, onSave }: Props = $props();

	// Form state
	let name = $state(subagent?.name || '');
	let description = $state(subagent?.description || '');
	let prompt = $state(subagent?.prompt || '');
	let selectedTools = $state<string[]>(subagent?.tools || []);
	let model = $state(subagent?.model || '');

	let saving = $state(false);
	let error = $state<string | null>(null);

	const isEditMode = $derived(!!subagent);

	// Tool selection
	function toggleTool(tool: string) {
		if (selectedTools.includes(tool)) {
			selectedTools = selectedTools.filter(t => t !== tool);
		} else {
			selectedTools = [...selectedTools, tool];
		}
	}

	function selectAllTools() {
		selectedTools = [...AVAILABLE_TOOLS];
	}

	function clearAllTools() {
		selectedTools = [];
	}

	// Form validation
	const isValid = $derived(() => {
		if (!isEditMode && !name.match(/^[a-z0-9-]+$/)) return false;
		if (name.length === 0) return false;
		if (description.length === 0) return false;
		if (prompt.length === 0) return false;
		return true;
	});

	async function handleSave() {
		if (!isValid()) return;

		saving = true;
		error = null;

		try {
			const body: Record<string, unknown> = {
				description,
				prompt,
			};

			if (selectedTools.length > 0) {
				body.tools = selectedTools;
			}
			if (model) {
				body.model = model;
			}

			if (isEditMode) {
				// Update existing subagent
				await api.put(`/profiles/${profileId}/agents/${subagent!.name}`, body);
			} else {
				// Create new subagent
				body.name = name;
				await api.post(`/profiles/${profileId}/agents`, body);
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

	// Close on escape key
	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			onClose();
		}
	}
</script>

<svelte:window on:keydown={handleKeydown} />

<!-- Modal backdrop -->
<div
	class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
	onclick={(e) => e.target === e.currentTarget && onClose()}
	role="dialog"
	aria-modal="true"
>
	<!-- Modal content - responsive width -->
	<div class="bg-card border border-border rounded-lg shadow-xl w-full max-w-2xl max-h-[90vh] overflow-hidden flex flex-col">
		<!-- Header -->
		<div class="flex items-center justify-between px-4 py-3 border-b border-border bg-muted/30">
			<h2 class="text-lg font-semibold text-foreground">
				{isEditMode ? 'Edit Subagent' : 'Create Subagent'}
			</h2>
			<button
				onclick={onClose}
				class="p-1 hover:bg-muted rounded-md transition-colors"
				aria-label="Close"
			>
				<svg class="w-5 h-5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		</div>

		<!-- Scrollable content -->
		<div class="flex-1 overflow-y-auto p-4 space-y-4">
			{#if error}
				<div class="p-3 bg-red-500/10 border border-red-500/20 rounded-md text-red-500 text-sm">
					{error}
				</div>
			{/if}

			<!-- Name (only for new subagents) -->
			{#if !isEditMode}
				<div class="space-y-1.5">
					<label for="agent-name" class="block text-sm font-medium text-foreground">
						Name
					</label>
					<input
						id="agent-name"
						type="text"
						bind:value={name}
						placeholder="my-agent-name"
						class="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
					/>
					<p class="text-xs text-muted-foreground">
						Lowercase letters, numbers, and hyphens only
					</p>
				</div>
			{:else}
				<div class="space-y-1.5">
					<label class="block text-sm font-medium text-foreground">Name</label>
					<div class="px-3 py-2 bg-muted/50 border border-border rounded-md text-foreground">
						{subagent?.name}
					</div>
				</div>
			{/if}

			<!-- Description -->
			<div class="space-y-1.5">
				<label for="agent-description" class="block text-sm font-medium text-foreground">
					Description
				</label>
				<input
					id="agent-description"
					type="text"
					bind:value={description}
					placeholder="When to use this agent..."
					class="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
				/>
				<p class="text-xs text-muted-foreground">
					Describes when Claude should invoke this subagent
				</p>
			</div>

			<!-- Model -->
			<div class="space-y-1.5">
				<label for="agent-model" class="block text-sm font-medium text-foreground">
					Model
				</label>
				<select
					id="agent-model"
					bind:value={model}
					class="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
				>
					{#each MODEL_OPTIONS as opt}
						<option value={opt.value}>{opt.label}</option>
					{/each}
				</select>
			</div>

			<!-- Tools -->
			<div class="space-y-2">
				<div class="flex items-center justify-between">
					<label class="block text-sm font-medium text-foreground">
						Allowed Tools
					</label>
					<div class="flex gap-2 text-xs">
						<button
							type="button"
							onclick={selectAllTools}
							class="text-primary hover:underline"
						>
							Select all
						</button>
						<span class="text-muted-foreground">|</span>
						<button
							type="button"
							onclick={clearAllTools}
							class="text-primary hover:underline"
						>
							Clear
						</button>
					</div>
				</div>
				<div class="flex flex-wrap gap-2">
					{#each AVAILABLE_TOOLS as tool}
						<button
							type="button"
							onclick={() => toggleTool(tool)}
							class="px-2.5 py-1 text-sm rounded-md border transition-colors {selectedTools.includes(tool)
								? 'bg-primary text-primary-foreground border-primary'
								: 'bg-background text-foreground border-border hover:bg-muted'}"
						>
							{tool}
						</button>
					{/each}
				</div>
				<p class="text-xs text-muted-foreground">
					{selectedTools.length === 0 ? 'All tools will be available (inherits from profile)' : `${selectedTools.length} tool(s) selected`}
				</p>
			</div>

			<!-- System Prompt -->
			<div class="space-y-1.5">
				<label for="agent-prompt" class="block text-sm font-medium text-foreground">
					System Prompt
				</label>
				<textarea
					id="agent-prompt"
					bind:value={prompt}
					placeholder="You are a specialized agent..."
					rows={10}
					class="w-full px-3 py-2 bg-background border border-border rounded-md text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 font-mono text-sm resize-y"
				></textarea>
				<p class="text-xs text-muted-foreground">
					Instructions that define the subagent's behavior and capabilities
				</p>
			</div>
		</div>

		<!-- Footer -->
		<div class="flex flex-col-reverse sm:flex-row items-stretch sm:items-center justify-end gap-2 px-4 py-3 border-t border-border bg-muted/30">
			<button
				type="button"
				onclick={onClose}
				class="px-4 py-2 text-sm font-medium text-foreground bg-background border border-border rounded-md hover:bg-muted transition-colors"
			>
				Cancel
			</button>
			<button
				type="button"
				onclick={handleSave}
				disabled={!isValid() || saving}
				class="px-4 py-2 text-sm font-medium text-primary-foreground bg-primary rounded-md hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
			>
				{#if saving}
					<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
						<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
						<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
					</svg>
				{/if}
				{isEditMode ? 'Save Changes' : 'Create Subagent'}
			</button>
		</div>
	</div>
</div>
