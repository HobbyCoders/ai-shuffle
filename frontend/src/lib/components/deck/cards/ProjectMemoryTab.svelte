<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * ProjectMemoryTab - AGENTS.md memory file management tab
	 *
	 * Studio-v2 grouped-field design (.set-group), mirroring the Profile
	 * editor. Card shell primitives come from project-tabs.css (.ptab);
	 * the preview/editor bodies are styled locally with theme tokens.
	 *
	 * - View/edit AGENTS.md at project root and .agents/AGENTS.md
	 *   (legacy CLAUDE.md / .claude/CLAUDE.md are still read as a fallback)
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
	import {
		applyMemoryImport,
		previewMemoryImport,
		type MemoryImportPreview,
		type MemoryImportProvider,
		type MemoryImportResult
	} from '$lib/api/projectMemory';
	import {
		FileText,
		Plus,
		Trash2,
		Save,
		RotateCcw,
		FolderOpen,
		Lock,
		Download
	} from 'lucide-svelte';
	import Banner from '$lib/components/Banner.svelte';
	import './project-tabs.css';

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

	// Agent-memory import (provider dir -> this project's memory dir)
	const IMPORT_PROVIDERS: { value: MemoryImportProvider; label: string }[] = [
		{ value: 'claude', label: 'Claude' },
		{ value: 'codex', label: 'Codex' }
	];
	let importProvider = $state<MemoryImportProvider>('claude');
	let importPreview = $state<MemoryImportPreview | null>(null);
	let importResult = $state<MemoryImportResult | null>(null);
	let importBusy = $state(false);
	let importError = $state<string | null>(null);

	// Load files when project changes
	$effect(() => {
		if (projectId) {
			projectMemory.loadFiles(projectId);
			// Reset editing state on project change
			editingLocation = null;
			creatingLocation = null;
			deletingLocation = null;
			importPreview = null;
			importResult = null;
			importError = null;
		}
	});

	// Location metadata
	const LOCATION_META: Record<MemoryLocation, { label: string; description: string; icon: typeof FileText }> = {
		project: {
			label: 'Project Memory',
			description: 'AGENTS.md — Shared project instructions, checked into version control',
			icon: FolderOpen
		},
		local: {
			label: 'Local Memory',
			description: '.agents/AGENTS.md — Private local instructions, typically gitignored',
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

	function errorMessage(e: unknown): string {
		return e instanceof Error ? e.message : String(e);
	}

	async function handleImportPreview() {
		importBusy = true;
		importError = null;
		importResult = null;
		try {
			importPreview = await previewMemoryImport(projectId, importProvider);
		} catch (e) {
			importPreview = null;
			importError = errorMessage(e);
		} finally {
			importBusy = false;
		}
	}

	async function handleImportApply() {
		importBusy = true;
		importError = null;
		try {
			importResult = await applyMemoryImport(projectId, importProvider);
			// Re-preview so every remaining item reads as a conflict, not a
			// stale "ready" the button would happily press again.
			importPreview = await previewMemoryImport(projectId, importProvider);
		} catch (e) {
			importError = errorMessage(e);
		} finally {
			importBusy = false;
		}
	}

	function onProviderChange() {
		importPreview = null;
		importResult = null;
		importError = null;
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
	let remoteChange = $state('');
	watchResources(['projects'], async () => {
		const id = projectId;
		const location = editingLocation;
		const before = editContent;
		await projectMemory.loadFiles(id);
		if (id !== projectId || !location || editingLocation !== location) return;
		const fresh = $allMemoryFiles.find((file) => file.location === location);
		if (isDirty || $memorySaving || before !== editContent) {
			if (!fresh?.exists || fresh.content !== before) remoteChange = fresh?.exists
				? 'This memory file changed elsewhere. Your unsaved text is kept here.'
				: 'This memory file was deleted elsewhere. Your unsaved text is kept here.';
			return;
		}
		if (fresh?.exists) { editContent = fresh.content; remoteChange = ''; }
		else { cancelEdit(); remoteChange = 'This memory file was deleted elsewhere.'; }
	});
</script>

{#if remoteChange}<p role="status" style="flex: none; height: auto;" class="px-4 py-2 text-sm text-muted-foreground">{remoteChange}</p>{/if}

<div class="ptab">
	{#if $memoryLoading}
		<div class="ptab-loading"><div class="ptab-spinner"></div></div>
	{:else}
		{#if $memoryError}
			<Banner severity="error" message={$memoryError} dismissible onDismiss={() => projectMemory.clearError()} />
		{/if}

		{#each $allMemoryFiles as file (file.location)}
			{@const meta = LOCATION_META[file.location]}
			{@const isEditing = editingLocation === file.location}
			{@const isDeleting = deletingLocation === file.location}
			{@const MetaIcon = meta.icon}

			<div class="set-group">
				<!-- Header -->
				<div class="set-grouphead grouphead-row">
					<div>
						<h3>
							<MetaIcon />{meta.label}
							{#if file.exists}<span class="badge success">Active</span>{:else}<span class="badge muted">Not created</span>{/if}
						</h3>
						<p class="mpath">{file.file_path}</p>
					</div>
					<div class="head-acts">
						{#if file.exists && !isEditing}
							<button class="icon-btn" onclick={() => startEdit(file)} title="Edit"><FileText size={14} /></button>
							<button class="icon-btn danger" onclick={() => (deletingLocation = file.location)} title="Delete"><Trash2 size={14} /></button>
						{:else if !file.exists && !isEditing}
							<button class="ed-btn sm primary" onclick={() => startCreate(file.location)}><Plus size={14} /> Create</button>
						{/if}
					</div>
				</div>

				<!-- Delete confirmation -->
				{#if isDeleting && !isEditing}
					<div class="mem-confirm">
						<p>Delete <strong>{file.file_path}</strong>? This cannot be undone.</p>
						<div class="mem-confirm-acts">
							<button class="ed-btn sm" onclick={() => (deletingLocation = null)} disabled={deleteLoading}>Cancel</button>
							<button class="ed-btn sm danger" onclick={() => handleDelete(file.location)} disabled={deleteLoading}>
								{deleteLoading ? 'Deleting…' : 'Delete'}
							</button>
						</div>
					</div>
				{/if}

				<!-- Editor -->
				{#if isEditing}
					<div class="mem-editor">
						<textarea
							class="mem-textarea"
							value={editContent}
							oninput={handleInput}
							placeholder="Enter markdown instructions for the agent…"
							rows={12}
							spellcheck={false}
						></textarea>
						<div class="mem-foot">
							<span class="hint">Markdown supported</span>
							<div class="mem-foot-acts">
								<button class="ed-btn sm" onclick={cancelEdit} disabled={$memorySaving}><RotateCcw size={12} /> Cancel</button>
								<button class="ed-btn sm primary" onclick={handleSave} disabled={$memorySaving || !isDirty}>
									<Save size={12} /> {$memorySaving ? 'Saving…' : 'Save'}
								</button>
							</div>
						</div>
					</div>
				{:else if file.exists}
					<!-- Preview -->
					<div class="mem-preview" onclick={() => startEdit(file)} role="button" tabindex="0" onkeydown={(e) => e.key === 'Enter' && startEdit(file)}>
						{#if file.content.trim()}
							<pre class="mem-pre">{file.content.length > 500 ? file.content.slice(0, 500) + '…' : file.content}</pre>
						{:else}
							<p class="mem-empty-line">File is empty. Click to edit.</p>
						{/if}
						<div class="mem-meta">
							<span>{formatSize(file.size)}</span>
							{#if file.updated_at}<span>Modified {formatDate(file.updated_at)}</span>{/if}
						</div>
					</div>
				{:else}
					<!-- Empty (not created) -->
					<div class="mem-empty"><p>{meta.description}</p></div>
				{/if}
			</div>
		{/each}

		<!-- Import agent memory from a provider's own per-project memory dir -->
		<div class="set-group">
			<div class="set-grouphead grouphead-row">
				<div>
					<h3><Download />Import agent memory</h3>
					<p class="mpath">
						Copy an agent provider's saved memory for this project into the project's
						memory dir. Existing files are never overwritten.
					</p>
				</div>
				<div class="head-acts imp-acts">
					<select
						class="imp-select"
						bind:value={importProvider}
						onchange={onProviderChange}
						disabled={importBusy}
						aria-label="Memory provider"
					>
						{#each IMPORT_PROVIDERS as p (p.value)}
							<option value={p.value}>{p.label}</option>
						{/each}
					</select>
					<button class="ed-btn sm" onclick={handleImportPreview} disabled={importBusy}>
						{importBusy ? 'Working…' : 'Preview'}
					</button>
					<button
						class="ed-btn sm primary"
						onclick={handleImportApply}
						disabled={importBusy || !importPreview || importPreview.ready_count === 0}
					>
						<Download size={12} /> Import {importPreview?.ready_count ?? 0}
					</button>
				</div>
			</div>

			<div class="imp-body">
				{#if importError}
					<Banner severity="error" message={importError} dismissible onDismiss={() => (importError = null)} />
				{/if}

				{#if importResult}
					<p class="imp-note">
						Imported {importResult.imported.length}
						{importResult.imported.length === 1 ? 'file' : 'files'}{importResult.skipped
							.length
							? `, skipped ${importResult.skipped.length}`
							: ''}.
					</p>
				{/if}

				{#if importPreview}
					<p class="imp-note">
						<span class="badge success">{importPreview.ready_count} ready</span>
						<span class="badge muted">{importPreview.conflict_count} conflict</span>
					</p>
					<p class="mpath">{importPreview.source_dir ?? 'no source directory'}</p>
					{#if importPreview.items.length === 0}
						<p class="imp-empty">Nothing found for this provider.</p>
					{:else}
						<ul class="imp-list">
							{#each importPreview.items as item (item.relative_path)}
								<li>
									<span class="imp-file">{item.relative_path}</span>
									<span class="imp-size">{formatSize(item.size)}</span>
									<span class="badge {item.status === 'ready' ? 'success' : 'muted'}">
										{item.status === 'ready' ? 'Ready' : 'Already exists'}
									</span>
								</li>
							{/each}
						</ul>
					{/if}
				{:else if !importError}
					<p class="imp-empty">Preview to see what would be copied.</p>
				{/if}
			</div>
		</div>

		<!-- No files at all -->
		{#if $allMemoryFiles.length === 0 && !$memoryLoading}
			<div class="empty-state">
				<FileText />
				<p class="es-title">No memory files</p>
				<p class="es-desc">AGENTS.md files provide persistent instructions and context for agent sessions. Existing CLAUDE.md files are still read.</p>
			</div>
		{/if}
	{/if}
</div>

<style>
	.mpath {
		margin: 5px 0 0;
		font-family: var(--font-mono, monospace);
		font-size: 11.5px;
		color: var(--muted-foreground);
		word-break: break-all;
	}

	/* Delete confirmation */
	.mem-confirm {
		padding: 13px 18px;
		background: color-mix(in oklch, var(--destructive) 6%, var(--card));
		border-bottom: 1px solid color-mix(in oklch, var(--destructive) 18%, transparent);
	}
	.mem-confirm p {
		margin: 0 0 10px;
		font-size: 12.5px;
		color: var(--foreground);
	}
	.mem-confirm-acts {
		display: flex;
		gap: 8px;
	}

	/* Editor */
	.mem-textarea {
		width: 100%;
		min-height: 220px;
		max-height: 520px;
		padding: 14px 18px;
		font-family: var(--font-mono, monospace);
		font-size: 12.5px;
		line-height: 1.6;
		color: var(--foreground);
		background: var(--background);
		border: none;
		outline: none;
		resize: vertical;
		tab-size: 2;
		display: block;
	}
	.mem-textarea::placeholder {
		color: var(--muted-foreground);
		opacity: 0.6;
	}
	.mem-foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 10px 14px;
		background: var(--muted);
		border-top: 1px solid var(--border);
	}
	.mem-foot-acts {
		display: flex;
		gap: 8px;
	}

	/* Preview */
	.mem-preview {
		padding: 14px 18px;
		cursor: pointer;
		transition: background 0.12s ease;
	}
	.mem-preview:hover {
		background: var(--hover-overlay, color-mix(in oklch, var(--muted) 40%, transparent));
	}
	.mem-preview:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	.mem-pre {
		font-family: var(--font-mono, monospace);
		font-size: 11.5px;
		line-height: 1.6;
		color: var(--muted-foreground);
		white-space: pre-wrap;
		word-break: break-word;
		margin: 0;
		max-height: 150px;
		overflow: hidden;
		mask-image: linear-gradient(to bottom, black 80%, transparent 100%);
		-webkit-mask-image: linear-gradient(to bottom, black 80%, transparent 100%);
	}
	.mem-empty-line {
		font-size: 12.5px;
		color: var(--muted-foreground);
		font-style: italic;
		margin: 0;
	}
	.mem-meta {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-top: 10px;
		padding-top: 10px;
		border-top: 1px solid var(--border);
		font-size: 11px;
		color: var(--muted-foreground);
	}

	/* Import agent memory */
	.imp-acts {
		flex-wrap: wrap;
	}
	.imp-select {
		height: 26px;
		padding: 0 6px;
		font-size: 12px;
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 6px;
	}
	.imp-body {
		padding: 13px 18px;
	}
	.imp-note {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0 0 8px;
		font-size: 12.5px;
		color: var(--muted-foreground);
	}
	.imp-empty {
		margin: 0;
		font-size: 12.5px;
		font-style: italic;
		color: var(--muted-foreground);
	}
	.imp-list {
		list-style: none;
		margin: 10px 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.imp-list li {
		display: flex;
		align-items: center;
		gap: 10px;
		font-size: 12px;
	}
	.imp-file {
		flex: 1;
		font-family: var(--font-mono, monospace);
		color: var(--foreground);
		word-break: break-all;
	}
	.imp-size {
		color: var(--muted-foreground);
	}

	/* Empty (not created) */
	.mem-empty {
		padding: 13px 18px;
	}
	.mem-empty p {
		margin: 0;
		font-size: 12.5px;
		color: var(--muted-foreground);
	}

	@media (max-width: 560px) {
		.mem-foot {
			flex-direction: column;
			align-items: stretch;
			gap: 8px;
		}
		.mem-foot-acts {
			justify-content: flex-end;
		}
	}
</style>
