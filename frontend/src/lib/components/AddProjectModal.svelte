<script lang="ts">
	/**
	 * AddProjectModal - Shared "Add Project" modal with directory browser
	 *
	 * Used by WorkspaceSelector and ProjectCard to create new projects.
	 * Includes DirectoryBrowser for location selection, auto-derived name/ID from path.
	 */
	import { X } from 'lucide-svelte';
	import { tabs } from '$lib/stores/tabs';
	import { getWorkspaceConfig } from '$lib/api/auth';
	import { onMount } from 'svelte';
	import DirectoryBrowser from '$lib/components/DirectoryBrowser.svelte';

	interface Props {
		open: boolean;
		onClose: () => void;
		onCreated?: (projectId: string) => void;
	}

	let { open, onClose, onCreated }: Props = $props();

	let isLocalMode = $state(false);

	let projectId = $state('');
	let projectName = $state('');
	let projectPath = $state('');
	let adding = $state(false);
	let addError = $state<string | null>(null);
	let nameManuallyEdited = $state(false);
	let idManuallyEdited = $state(false);

	onMount(async () => {
		try {
			const config = await getWorkspaceConfig();
			isLocalMode = config.is_local_mode;
		} catch {
			// Default to non-local on error
		}
	});

	// Reset form when modal opens
	$effect(() => {
		if (open) {
			projectId = '';
			projectName = '';
			projectPath = '';
			addError = null;
			nameManuallyEdited = false;
			idManuallyEdited = false;
		}
	});

	function deriveNameAndId(path: string) {
		const dirName = path.split(/[/\\]/).filter(Boolean).pop() || '';
		if (!nameManuallyEdited) {
			projectName = dirName;
		}
		if (!idManuallyEdited) {
			projectId = dirName.toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/-+/g, '-').slice(0, 50);
		}
	}

	function handleDirectorySelect(path: string) {
		projectPath = path;
		deriveNameAndId(path);
	}

	function handlePathInput(e: Event) {
		const value = (e.target as HTMLInputElement).value;
		projectPath = value;
		if (value.trim()) {
			deriveNameAndId(value);
		}
	}

	async function handleAddProject() {
		if (!projectId.trim() || !projectName.trim() || !projectPath.trim()) return;

		adding = true;
		addError = null;

		try {
			await tabs.createProject({
				id: projectId.trim(),
				name: projectName.trim(),
				path: projectPath.trim()
			});
			onClose();
			onCreated?.(projectId.trim());
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			addError = err.detail || err.message || 'Failed to add project';
		} finally {
			adding = false;
		}
	}
</script>

{#if open}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div class="create-modal-backdrop" onclick={onClose}>
		<div class="create-modal add-project-modal" onclick={(e) => e.stopPropagation()}>
			<div class="create-modal-header">
				<h3 class="create-modal-title">Add Project</h3>
				<button class="create-modal-close" onclick={onClose}>
					<X size={16} strokeWidth={1.5} />
				</button>
			</div>

			{#if addError}
				<div class="create-modal-error">
					{addError}
				</div>
			{/if}

			<div class="create-modal-field">
				<label class="create-modal-label">Location</label>
				<DirectoryBrowser
					selectedPath={projectPath}
					onSelect={handleDirectorySelect}
				/>
			</div>

			<div class="create-modal-field">
				<label class="create-modal-label">Path</label>
				<input
					type="text"
					class="create-modal-input create-modal-input--mono"
					placeholder={isLocalMode ? '/path/to/project' : '/workspace/my-project'}
					value={projectPath}
					oninput={handlePathInput}
					onkeydown={(e) => { if (e.key === 'Enter' && projectId.trim() && projectName.trim() && projectPath.trim()) handleAddProject(); }}
				/>
				<span class="path-hint">Select a folder above or type a path. New folders will be created automatically.</span>
			</div>

			<div class="create-modal-row">
				<div class="create-modal-field create-modal-field--half">
					<label class="create-modal-label">Display Name</label>
					<input
						type="text"
						class="create-modal-input"
						placeholder="My Project"
						bind:value={projectName}
						oninput={() => { nameManuallyEdited = true; }}
						onkeydown={(e) => { if (e.key === 'Enter' && projectId.trim() && projectName.trim() && projectPath.trim()) handleAddProject(); }}
					/>
				</div>
				<div class="create-modal-field create-modal-field--half">
					<label class="create-modal-label">Project ID</label>
					<input
						type="text"
						class="create-modal-input create-modal-input--mono"
						placeholder="my-project"
						bind:value={projectId}
						oninput={() => { idManuallyEdited = true; }}
						onkeydown={(e) => { if (e.key === 'Enter' && projectId.trim() && projectName.trim() && projectPath.trim()) handleAddProject(); }}
					/>
				</div>
			</div>

			<div class="create-modal-actions">
				<button class="modal-btn cancel" onclick={onClose}>
					Cancel
				</button>
				<button
					class="modal-btn create"
					disabled={!projectId.trim() || !projectName.trim() || !projectPath.trim() || adding}
					onclick={handleAddProject}
				>
					{#if adding}Adding...{:else}Add Project{/if}
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.create-modal-backdrop {
		position: fixed;
		inset: 0;
		background: color-mix(in oklch, var(--background) 70%, transparent);
		backdrop-filter: blur(4px);
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: 1000;
	}

	.create-modal {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 12px;
		padding: 20px;
		width: 380px;
		max-width: 90vw;
		box-shadow: var(--shadow-l);
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.add-project-modal {
		width: 500px;
	}

	.create-modal-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.create-modal-title {
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
	}

	.create-modal-close {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.create-modal-close:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.create-modal-error {
		padding: 8px 12px;
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--destructive) 25%, transparent);
		border-radius: 8px;
		color: var(--destructive);
		font-size: 0.8125rem;
	}

	.create-modal-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.create-modal-field--half {
		flex: 1;
		min-width: 0;
	}

	.create-modal-row {
		display: flex;
		gap: 12px;
	}

	.create-modal-label {
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--muted-foreground);
	}

	.create-modal-input {
		width: 100%;
		padding: 8px 12px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 8px;
		color: var(--foreground);
		font-size: 0.8125rem;
		outline: none;
		transition: border-color 150ms ease;
	}

	.create-modal-input--mono {
		font-family: var(--font-mono, 'JetBrains Mono', 'SF Mono', monospace);
		font-size: 0.75rem;
	}

	.create-modal-input:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.path-hint {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		opacity: 0.7;
	}

	.create-modal-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 4px;
	}

	.modal-btn {
		padding: 6px 14px;
		font-size: 0.8125rem;
		font-weight: 500;
		border-radius: 8px;
		border: none;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.modal-btn.cancel {
		background: var(--secondary);
		color: var(--muted-foreground);
	}

	.modal-btn.cancel:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.modal-btn.create {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.modal-btn.create:hover {
		opacity: 0.9;
	}

	.modal-btn.create:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	@media (max-width: 400px) {
		.create-modal-row {
			flex-direction: column;
		}
	}
</style>
