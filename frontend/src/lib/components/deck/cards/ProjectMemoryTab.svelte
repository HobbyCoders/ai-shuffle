<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/** Project instruction files and provider-memory import. */
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
	import { RowGroup, SettingRow, EmptyDetail, InlineBanner, SelectField } from '$lib/components/layout/view';
	import { formatBytes } from '$lib/utils/format-bytes';
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
			// A failed load is shown through $memoryError.
			projectMemory.loadFiles(projectId).catch(() => {});
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
			label: 'Project memory',
			description: 'AGENTS.md — Shared project instructions, checked into version control',
			icon: FolderOpen
		},
		local: {
			label: 'Local memory',
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
{#if remoteChange}<InlineBanner>{remoteChange}</InlineBanner>{/if}
{#if $memoryLoading}<p class="project-muted" role="status">Loading memory…</p>
{:else}
	{#if $memoryError}<InlineBanner>{$memoryError}{#snippet actions()}<button class="st-btn" onclick={() => projectMemory.clearError()}>Dismiss</button>{/snippet}</InlineBanner>{/if}
	{#each $allMemoryFiles as file (file.location)}
		{@const meta = LOCATION_META[file.location]}
		{@const isEditing = editingLocation === file.location}
		<RowGroup title={meta.label}>
			<SettingRow label={file.exists ? 'Instructions' : 'Not created'} hint={meta.description}>
				{#if file.exists && !isEditing}
					<button class="st-btn icon" onclick={() => startEdit(file)} title="Edit" aria-label="Edit"><FileText /></button>
					<button class="st-btn icon danger" onclick={() => (deletingLocation = file.location)} title="Delete" aria-label="Delete"><Trash2 /></button>
				{:else if !file.exists && !isEditing}<button class="st-btn" onclick={() => startCreate(file.location)}><Plus />Create</button>{/if}
				{#snippet expanded()}
					<p class="project-path">{file.file_path}</p>
					{#if deletingLocation === file.location && !isEditing}
						<InlineBanner>Delete {file.file_path}? This cannot be undone.
							{#snippet actions()}<button class="st-btn" onclick={() => (deletingLocation = null)} disabled={deleteLoading}>Cancel</button><button class="st-btn danger" onclick={() => handleDelete(file.location)} disabled={deleteLoading}>{deleteLoading ? 'Deleting…' : 'Delete'}</button>{/snippet}
						</InlineBanner>
					{/if}
					{#if isEditing}
						<textarea class="project-memory-editor" value={editContent} oninput={handleInput} aria-label={`Edit ${meta.label}`} placeholder="Enter markdown instructions for the agent…" rows={12} spellcheck={false}></textarea>
						<div class="project-toolbar"><span class="project-muted">Markdown supported</span><button class="st-btn" onclick={cancelEdit} disabled={$memorySaving}><RotateCcw />Cancel</button><button class="st-btn primary" onclick={handleSave} disabled={$memorySaving || !isDirty}><Save />{$memorySaving ? 'Saving…' : 'Save'}</button></div>
					{:else if file.exists}
						<button class="project-memory-preview" onclick={() => startEdit(file)}>
							{#if file.content.trim()}<pre>{file.content.length > 500 ? file.content.slice(0, 500) + '…' : file.content}</pre>{:else}<span>File is empty. Click to edit.</span>{/if}
						</button>
						<p class="project-muted">{formatBytes(file.size)}{file.updated_at ? ` · Modified ${formatDate(file.updated_at)}` : ''}</p>
					{/if}
				{/snippet}
			</SettingRow>
		</RowGroup>
	{/each}
	<RowGroup title="Import agent memory">
		<SettingRow label="Memory provider" hint="Copy saved memory into this project. Existing files are never overwritten.">
			<SelectField value={importProvider} options={IMPORT_PROVIDERS} disabled={importBusy} label="Memory provider" onchange={(v) => { importProvider = v as MemoryImportProvider; onProviderChange(); }} />
			<button class="st-btn" onclick={handleImportPreview} disabled={importBusy}>{importBusy ? 'Working…' : 'Preview'}</button>
			<button class="st-btn primary" onclick={handleImportApply} disabled={importBusy || !importPreview || importPreview.ready_count === 0}><Download />Import {importPreview?.ready_count ?? 0}</button>
		</SettingRow>
		{#if importError || importResult || importPreview}
			<SettingRow label="Import preview" hint={importPreview ? `${importPreview.ready_count} ready · ${importPreview.conflict_count} conflict` : undefined}>
				{#snippet expanded()}
					{#if importError}<InlineBanner>{importError}{#snippet actions()}<button class="st-btn" onclick={() => (importError = null)}>Dismiss</button>{/snippet}</InlineBanner>{/if}
					{#if importResult}<p class="project-muted" role="status">Imported {importResult.imported.length} files{importResult.skipped.length ? `, skipped ${importResult.skipped.length}` : ''}.</p>{/if}
					{#if importPreview}<p class="project-path">{importPreview.source_dir ?? 'No source directory'}</p>{#if !importPreview.items.length}<p class="project-muted">Nothing found for this provider.</p>{/if}{/if}
				{/snippet}
			</SettingRow>
			{#each importPreview?.items ?? [] as item (item.relative_path)}<SettingRow label={item.relative_path} hint={formatBytes(item.size)}><span class="project-muted">{item.status === 'ready' ? 'Ready' : 'Already exists'}</span></SettingRow>{/each}
		{:else}<SettingRow label="Preview to see what would be copied." />{/if}
	</RowGroup>
	{#if !$allMemoryFiles.length}<EmptyDetail title="No memory files" message="AGENTS.md files provide persistent instructions and context for agent chats. Existing CLAUDE.md files are still read." />{/if}
{/if}
