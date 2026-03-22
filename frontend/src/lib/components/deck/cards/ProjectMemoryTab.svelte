<script lang="ts">
	/**
	 * ProjectMemoryTab - CLAUDE.md memory file management tab
	 *
	 * Features:
	 * - View/edit CLAUDE.md at project root and .claude/CLAUDE.md
	 * - Create new memory files
	 * - Delete memory files with confirmation
	 * - Inline markdown editor with save/cancel
	 */
	import {
		projectMemory,
		allMemoryFiles,
		memoryLoading,
		memorySaving,
		memoryError
	} from '$lib/stores/projectMemory.svelte';
	import type { MemoryFile, MemoryLocation } from '$lib/api/projectMemory';
	import { FileText, Plus, Trash2, X, Save, RotateCcw, FolderOpen, Lock } from 'lucide-svelte';
	import './card-design-system.css';

	interface Props {
		projectId: string;
	}

	let { projectId }: Props = $props();

	// Editing state per location
	let editingLocation = $state<MemoryLocation | null>(null);
	let editContent = $state('');
	let isDirty = $state(false);

	// Delete confirmation
	let deletingLocation = $state<MemoryLocation | null>(null);
	let deleteLoading = $state(false);

	// Creating state (for files that don't exist yet)
	let creatingLocation = $state<MemoryLocation | null>(null);

	// Load files when project changes
	$effect(() => {
		if (projectId) {
			projectMemory.loadFiles(projectId);
			// Reset editing state on project change
			editingLocation = null;
			creatingLocation = null;
			deletingLocation = null;
		}
	});

	// Location metadata
	const LOCATION_META: Record<MemoryLocation, { label: string; description: string; icon: typeof FileText }> = {
		project: {
			label: 'Project Memory',
			description: 'CLAUDE.md — Shared project instructions, checked into version control',
			icon: FolderOpen
		},
		local: {
			label: 'Local Memory',
			description: '.claude/CLAUDE.md — Private local overrides, typically gitignored',
			icon: Lock
		}
	};

	function startEdit(file: MemoryFile) {
		editingLocation = file.location;
		editContent = file.content;
		isDirty = false;
	}

	function startCreate(location: MemoryLocation) {
		creatingLocation = location;
		editingLocation = location;
		editContent = '';
		isDirty = true;
	}

	function cancelEdit() {
		editingLocation = null;
		creatingLocation = null;
		editContent = '';
		isDirty = false;
	}

	function handleInput(e: Event) {
		const target = e.target as HTMLTextAreaElement;
		editContent = target.value;
		isDirty = true;
	}

	async function handleSave() {
		if (!editingLocation) return;

		try {
			await projectMemory.saveFile(projectId, editingLocation, editContent);
			editingLocation = null;
			creatingLocation = null;
			editContent = '';
			isDirty = false;
		} catch (e) {
			console.error('Failed to save memory file:', e);
		}
	}

	async function handleDelete(location: MemoryLocation) {
		deleteLoading = true;
		try {
			await projectMemory.deleteFile(projectId, location);
			deletingLocation = null;
			// If we were editing this file, close the editor
			if (editingLocation === location) {
				editingLocation = null;
				editContent = '';
				isDirty = false;
			}
		} catch (e) {
			console.error('Failed to delete memory file:', e);
		} finally {
			deleteLoading = false;
		}
	}

	function formatDate(dateStr: string | null): string {
		if (!dateStr) return '';
		const date = new Date(dateStr);
		return date.toLocaleDateString(undefined, {
			month: 'short',
			day: 'numeric',
			year: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	function formatSize(bytes: number): string {
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
		return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
	}
</script>

<div class="card-system memory-tab">
	{#if $memoryLoading}
		<div class="card-content">
			<div class="loading-container">
				<div class="card-spinner"></div>
			</div>
		</div>
	{:else}
		<div class="card-content">
			<div class="centered-content">
				{#if $memoryError}
					<div class="card-error-banner">
						<span class="flex-1">{$memoryError}</span>
						<button onclick={() => projectMemory.clearError()}>
							<X size={14} />
						</button>
					</div>
				{/if}

				{#each $allMemoryFiles as file (file.location)}
					{@const meta = LOCATION_META[file.location]}
					{@const isEditing = editingLocation === file.location}
					{@const isDeleting = deletingLocation === file.location}

					<div class="memory-section">
						<!-- Section Header -->
						<div class="memory-header">
							<div class="memory-header-left">
								<div class="memory-icon" class:memory-icon--active={file.exists}>
									{#if meta.icon}
										{@const MetaIcon = meta.icon}
										<MetaIcon size={16} />
									{/if}
								</div>
								<div class="memory-header-info">
									<div class="memory-header-title">
										<span class="memory-label">{meta.label}</span>
										{#if file.exists}
											<span class="card-badge card-badge--success">Active</span>
										{:else}
											<span class="card-badge card-badge--muted">Not created</span>
										{/if}
									</div>
									<span class="memory-path">{file.file_path}</span>
								</div>
							</div>
							<div class="memory-header-actions">
								{#if file.exists && !isEditing}
									<button
										class="card-action-btn"
										onclick={() => startEdit(file)}
										title="Edit"
									>
										<FileText size={14} />
									</button>
									<button
										class="card-action-btn card-action-btn--destructive"
										onclick={() => (deletingLocation = file.location)}
										title="Delete"
									>
										<Trash2 size={14} />
									</button>
								{:else if !file.exists && !isEditing}
									<button
										class="memory-create-btn"
										onclick={() => startCreate(file.location)}
									>
										<Plus size={14} /> Create
									</button>
								{/if}
							</div>
						</div>

						<!-- Delete Confirmation -->
						{#if isDeleting && !isEditing}
							<div class="memory-delete-confirm">
								<p class="delete-confirm-text">
									Delete <strong>{file.file_path}</strong>? This cannot be undone.
								</p>
								<div class="delete-confirm-actions">
									<button
										onclick={() => (deletingLocation = null)}
										disabled={deleteLoading}
										class="card-btn-secondary"
									>
										Cancel
									</button>
									<button
										onclick={() => handleDelete(file.location)}
										disabled={deleteLoading}
										class="card-btn-destructive"
									>
										{#if deleteLoading}
											<div class="card-spinner card-spinner--small"></div>
										{/if}
										Delete
									</button>
								</div>
							</div>
						{/if}

						<!-- Editor -->
						{#if isEditing}
							<div class="memory-editor">
								<textarea
									class="memory-textarea"
									value={editContent}
									oninput={handleInput}
									placeholder="Enter markdown instructions for Claude..."
									rows={12}
									spellcheck={false}
								></textarea>
								<div class="memory-editor-footer">
									<span class="memory-editor-hint">
										Markdown supported
									</span>
									<div class="memory-editor-actions">
										<button
											class="card-btn-secondary"
											onclick={cancelEdit}
											disabled={$memorySaving}
										>
											<RotateCcw size={12} /> Cancel
										</button>
										<button
											class="card-btn-primary"
											onclick={handleSave}
											disabled={$memorySaving || !isDirty}
										>
											{#if $memorySaving}
												<div class="card-spinner card-spinner--small"></div>
											{:else}
												<Save size={12} />
											{/if}
											Save
										</button>
									</div>
								</div>
							</div>
						{:else if file.exists}
							<!-- File Info / Preview -->
							<div class="memory-preview" onclick={() => startEdit(file)} role="button" tabindex="0" onkeydown={(e) => e.key === 'Enter' && startEdit(file)}>
								{#if file.content.trim()}
									<pre class="memory-preview-text">{file.content.length > 500 ? file.content.slice(0, 500) + '...' : file.content}</pre>
								{:else}
									<p class="memory-preview-empty">File is empty. Click to edit.</p>
								{/if}
								<div class="memory-meta">
									<span class="memory-meta-item">{formatSize(file.size)}</span>
									{#if file.updated_at}
										<span class="memory-meta-item">Modified {formatDate(file.updated_at)}</span>
									{/if}
								</div>
							</div>
						{:else if !isEditing}
							<!-- Empty state for non-existent file -->
							<div class="memory-empty">
								<p class="memory-empty-text">{meta.description}</p>
							</div>
						{/if}
					</div>
				{/each}

				<!-- Show empty state if no files returned at all -->
				{#if $allMemoryFiles.length === 0 && !$memoryLoading}
					<div class="card-empty-state">
						<FileText class="card-empty-icon" />
						<p class="card-empty-title">No memory files</p>
						<p class="card-empty-description">
							CLAUDE.md files provide persistent instructions and context for Claude Code sessions.
						</p>
					</div>
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.memory-tab {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: var(--card);
		overflow: hidden;
	}

	.centered-content {
		width: 100%;
		display: flex;
		flex-direction: column;
		gap: var(--space-5, 20px);
	}

	.flex-1 {
		flex: 1;
	}

	.loading-container {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: var(--space-8);
	}

	/* -----------------------------------------------------------------------
	   MEMORY SECTION
	   ----------------------------------------------------------------------- */

	.memory-section {
		border: 1px solid var(--border-subtle, color-mix(in oklch, var(--border) 60%, transparent));
		border-radius: var(--radius-md, 8px);
		overflow: hidden;
		background: var(--surface-0, var(--card));
		transition: border-color 0.15s ease;
	}

	.memory-section:hover {
		border-color: var(--border-default, var(--border));
	}

	/* -----------------------------------------------------------------------
	   MEMORY HEADER
	   ----------------------------------------------------------------------- */

	.memory-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3, 12px);
		padding: var(--space-3, 12px) var(--space-4, 16px);
		background: var(--surface-1, color-mix(in oklch, var(--muted) 60%, transparent));
		border-bottom: 1px solid var(--border-subtle, color-mix(in oklch, var(--border) 60%, transparent));
	}

	.memory-header-left {
		display: flex;
		align-items: center;
		gap: var(--space-3, 12px);
		flex: 1;
		min-width: 0;
	}

	.memory-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		border-radius: var(--radius, 6px);
		background: var(--surface-2, color-mix(in oklch, var(--muted) 80%, transparent));
		color: var(--text-tertiary, color-mix(in oklch, var(--muted-foreground) 70%, transparent));
		flex-shrink: 0;
	}

	.memory-icon--active {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
	}

	.memory-header-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}

	.memory-header-title {
		display: flex;
		align-items: center;
		gap: var(--space-2, 8px);
	}

	.memory-label {
		font-weight: 600;
		font-size: var(--text-base, 0.8125rem);
		color: var(--text-primary, var(--foreground));
	}

	.memory-path {
		font-size: var(--text-xs, 0.6875rem);
		font-family: var(--card-font-mono, 'JetBrains Mono', 'SF Mono', monospace);
		color: var(--text-tertiary, color-mix(in oklch, var(--muted-foreground) 70%, transparent));
	}

	.memory-header-actions {
		display: flex;
		align-items: center;
		gap: var(--space-1, 4px);
		flex-shrink: 0;
	}

	/* -----------------------------------------------------------------------
	   CREATE BUTTON
	   ----------------------------------------------------------------------- */

	.memory-create-btn {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1, 4px);
		padding: var(--space-1, 4px) var(--space-3, 12px);
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		border-radius: var(--radius, 6px);
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		border: 1px solid color-mix(in oklch, var(--primary) 30%, transparent);
		cursor: pointer;
		transition: all 0.15s ease;
		white-space: nowrap;
	}

	.memory-create-btn:hover {
		background: color-mix(in oklch, var(--primary) 25%, transparent);
		border-color: color-mix(in oklch, var(--primary) 50%, transparent);
		box-shadow: 0 0 12px color-mix(in oklch, var(--primary) 20%, transparent);
	}

	/* -----------------------------------------------------------------------
	   DELETE CONFIRMATION
	   ----------------------------------------------------------------------- */

	.memory-delete-confirm {
		padding: var(--space-3, 12px) var(--space-4, 16px);
		background: color-mix(in oklch, var(--destructive) 5%, var(--card));
		border-bottom: 1px solid color-mix(in oklch, var(--destructive) 15%, transparent);
	}

	/* -----------------------------------------------------------------------
	   EDITOR
	   ----------------------------------------------------------------------- */

	.memory-editor {
		display: flex;
		flex-direction: column;
	}

	.memory-textarea {
		width: 100%;
		min-height: 200px;
		max-height: 500px;
		padding: var(--space-4, 16px);
		font-family: var(--card-font-mono, 'JetBrains Mono', 'SF Mono', monospace);
		font-size: var(--text-sm, 0.75rem);
		line-height: 1.6;
		color: var(--text-primary, var(--foreground));
		background: var(--card);
		border: none;
		outline: none;
		resize: vertical;
		tab-size: 2;
	}

	.memory-textarea::placeholder {
		color: var(--text-tertiary, color-mix(in oklch, var(--muted-foreground) 50%, transparent));
	}

	.memory-editor-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3, 12px);
		padding: var(--space-2, 8px) var(--space-4, 16px);
		background: var(--surface-1, color-mix(in oklch, var(--muted) 60%, transparent));
		border-top: 1px solid var(--border-subtle, color-mix(in oklch, var(--border) 60%, transparent));
	}

	.memory-editor-hint {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-tertiary, color-mix(in oklch, var(--muted-foreground) 70%, transparent));
	}

	.memory-editor-actions {
		display: flex;
		align-items: center;
		gap: var(--space-2, 8px);
	}

	/* -----------------------------------------------------------------------
	   PREVIEW
	   ----------------------------------------------------------------------- */

	.memory-preview {
		padding: var(--space-3, 12px) var(--space-4, 16px);
		cursor: pointer;
		transition: background 0.15s ease;
	}

	.memory-preview:hover {
		background: var(--surface-hover, color-mix(in oklch, var(--muted) 40%, transparent));
	}

	.memory-preview:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.memory-preview-text {
		font-family: var(--card-font-mono, 'JetBrains Mono', 'SF Mono', monospace);
		font-size: var(--text-xs, 0.6875rem);
		line-height: 1.6;
		color: var(--text-secondary, var(--muted-foreground));
		white-space: pre-wrap;
		word-break: break-word;
		margin: 0;
		max-height: 150px;
		overflow: hidden;
		mask-image: linear-gradient(to bottom, black 80%, transparent 100%);
		-webkit-mask-image: linear-gradient(to bottom, black 80%, transparent 100%);
	}

	.memory-preview-empty {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-tertiary, color-mix(in oklch, var(--muted-foreground) 50%, transparent));
		font-style: italic;
		margin: 0;
	}

	.memory-meta {
		display: flex;
		align-items: center;
		gap: var(--space-3, 12px);
		margin-top: var(--space-2, 8px);
		padding-top: var(--space-2, 8px);
		border-top: 1px solid var(--border-subtle, color-mix(in oklch, var(--border) 40%, transparent));
	}

	.memory-meta-item {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-tertiary, color-mix(in oklch, var(--muted-foreground) 70%, transparent));
	}

	/* -----------------------------------------------------------------------
	   EMPTY STATE
	   ----------------------------------------------------------------------- */

	.memory-empty {
		padding: var(--space-3, 12px) var(--space-4, 16px);
	}

	.memory-empty-text {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-tertiary, color-mix(in oklch, var(--muted-foreground) 70%, transparent));
		margin: 0;
	}

	/* -----------------------------------------------------------------------
	   BUTTON OVERRIDES
	   ----------------------------------------------------------------------- */

	.card-btn-secondary {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1, 4px);
		padding: var(--space-1, 4px) var(--space-3, 12px);
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		border-radius: var(--radius, 6px);
		white-space: nowrap;
	}

	.card-btn-primary {
		display: inline-flex;
		align-items: center;
		gap: var(--space-1, 4px);
		padding: var(--space-1, 4px) var(--space-3, 12px);
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		border-radius: var(--radius, 6px);
		background: var(--primary);
		color: var(--primary-foreground);
		border: 1px solid var(--primary);
		cursor: pointer;
		transition: all 0.15s ease;
		white-space: nowrap;
	}

	.card-btn-primary:hover:not(:disabled) {
		filter: brightness(1.1);
		box-shadow: 0 0 12px color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.card-btn-primary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* -----------------------------------------------------------------------
	   RESPONSIVE
	   ----------------------------------------------------------------------- */

	@media (max-width: 639px) {
		.memory-header {
			flex-wrap: wrap;
		}

		.memory-header-actions {
			width: 100%;
			justify-content: flex-end;
		}

		.memory-textarea {
			min-height: 150px;
		}

		.memory-editor-footer {
			flex-direction: column;
			align-items: stretch;
			gap: var(--space-2, 8px);
		}

		.memory-editor-actions {
			justify-content: flex-end;
		}
	}
</style>
