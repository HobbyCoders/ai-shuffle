<script lang="ts">
	/**
	 * FilesStudio — the Studio-v2 Files surface.
	 *
	 * Two-column layout per the mockup: a lazy-loading folder tree on the left and
	 * a columnar file list (Name / Size / Modified / download) on the right, under a
	 * Studio page header (New folder / Upload) and a breadcrumb + hidden-files
	 * toolbar. Reuses the `files` store for all data + operations. Theme tokens only.
	 */
	import { onMount } from 'svelte';
	import {
		Folder, File as FileIcon, FileCode, FileText, FileImage, FileVideo, FileAudio,
		FileArchive, FileSpreadsheet, Database, Settings, Package,
		FolderPlus, Upload, Download, Trash2, Edit3, Home, ChevronRight, Check, X, Loader2, AlertCircle
	} from 'lucide-svelte';
	import { files, sortedFiles, breadcrumbs, type FileItem } from '$lib/stores/files.svelte';
	import StudioPageHeader from '$lib/components/layout/StudioPageHeader.svelte';
	import FilesTreeNode from './FilesTreeNode.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';

	let uploadInput = $state<HTMLInputElement | null>(null);
	let showNewFolder = $state(false);
	let newFolderName = $state('');
	let creatingFolder = $state(false);
	let renameTarget = $state<FileItem | null>(null);
	let renameValue = $state('');
	let pendingDelete = $state<FileItem | null>(null);
	let isDragging = $state(false);

	// Root-level directories for the tree sidebar.
	let rootDirs = $state<{ name: string; path: string }[]>([]);

	const currentPath = $derived($files.currentPath);
	const loading = $derived($files.loading);
	const uploading = $derived($files.uploading);
	const uploadProgress = $derived($files.uploadProgress);
	const error = $derived($files.error);
	const showHidden = $derived($files.showHidden);

	async function loadRootDirs() {
		try {
			const params = new URLSearchParams({ path: '', show_hidden: String($files.showHidden) });
			const res = await fetch(`/api/v1/workspace/files?${params}`, { credentials: 'include' });
			if (res.ok) {
				const data = await res.json();
				rootDirs = (data.files as FileItem[])
					.filter((f) => f.type === 'directory')
					.map((f) => ({ name: f.name, path: f.path }));
			}
		} catch { /* tree is best-effort */ }
	}

	onMount(() => {
		files.loadFiles();
		loadRootDirs();
	});

	// Keep the tree's root list in sync when the hidden-files toggle flips.
	$effect(() => {
		$files.showHidden;
		loadRootDirs();
	});

	function navigate(path: string) {
		files.navigateTo(path);
	}

	// ── New folder ──
	function openNewFolder() {
		showNewFolder = true;
		newFolderName = '';
	}
	async function confirmNewFolder() {
		const name = newFolderName.trim();
		if (!name) { showNewFolder = false; return; }
		creatingFolder = true;
		try {
			await files.createFolder(name);
			await loadRootDirs();
			showNewFolder = false;
			newFolderName = '';
		} finally {
			creatingFolder = false;
		}
	}

	// ── Upload ──
	function triggerUpload() { uploadInput?.click(); }
	async function onUploadChange(e: Event) {
		const input = e.target as HTMLInputElement;
		if (input.files && input.files.length > 0) {
			await files.uploadFiles(input.files);
			input.value = '';
		}
	}

	// ── Drag & drop ──
	function onDragOver(e: DragEvent) { e.preventDefault(); isDragging = true; }
	function onDragLeave(e: DragEvent) { e.preventDefault(); isDragging = false; }
	async function onDrop(e: DragEvent) {
		e.preventDefault();
		isDragging = false;
		if (e.dataTransfer?.files?.length) await files.uploadFiles(e.dataTransfer.files);
	}

	// ── Rename ──
	function startRename(item: FileItem, e: Event) {
		e.stopPropagation();
		renameTarget = item;
		renameValue = item.name;
	}
	async function confirmRename() {
		if (renameTarget && renameValue.trim() && renameValue.trim() !== renameTarget.name) {
			await files.rename(renameTarget.path, renameValue.trim());
			await loadRootDirs();
		}
		renameTarget = null;
		renameValue = '';
	}
	function cancelRename() { renameTarget = null; renameValue = ''; }

	// ── Delete ──
	async function confirmDelete() {
		const t = pendingDelete;
		pendingDelete = null;
		if (!t) return;
		await files.delete(t.path);
		await loadRootDirs();
	}

	function rowClick(item: FileItem) {
		if (renameTarget?.path === item.path) return;
		if (item.type === 'directory') files.navigateTo(item.path);
	}

	function download(item: FileItem, e: Event) {
		e.stopPropagation();
		if (item.type === 'file') files.downloadFile(item.path, item.name);
	}

	// ── Presentation helpers ──
	function fileIcon(item: FileItem) {
		if (item.type === 'directory') return Folder;
		const ext = item.extension?.toLowerCase() || '';
		if (['js','ts','jsx','tsx','py','rb','go','rs','java','c','cpp','h','cs','php','swift','kt','scala','vue','svelte'].includes(ext)) return FileCode;
		if (['md','txt','doc','docx','pdf','rtf','odt'].includes(ext)) return FileText;
		if (['jpg','jpeg','png','gif','svg','webp','ico','bmp','tiff'].includes(ext)) return FileImage;
		if (['mp4','mov','avi','mkv','webm','flv'].includes(ext)) return FileVideo;
		if (['mp3','wav','ogg','flac','aac','m4a'].includes(ext)) return FileAudio;
		if (['zip','rar','7z','tar','gz','bz2'].includes(ext)) return FileArchive;
		if (['json','xml','yaml','yml','toml','csv','sql'].includes(ext)) return Database;
		if (['xls','xlsx','ods'].includes(ext)) return FileSpreadsheet;
		if (['env','ini','cfg','conf','config'].includes(ext) || item.name.startsWith('.')) return Settings;
		if (['package.json','package-lock.json','yarn.lock','Cargo.toml','go.mod','requirements.txt'].includes(item.name)) return Package;
		return FileIcon;
	}

	function relTime(dateStr: string): string {
		const t = new Date(dateStr).getTime();
		if (Number.isNaN(t)) return '—';
		const min = Math.floor((Date.now() - t) / 60000);
		if (min < 1) return 'just now';
		if (min < 60) return `${min}m ago`;
		const h = Math.floor(min / 60);
		if (h < 24) return `${h}h ago`;
		const d = Math.floor(h / 24);
		if (d < 30) return `${d}d ago`;
		return new Date(dateStr).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}
</script>

<div class="fs">
	<div class="fs-wrap">
	<div class="fs-head">
		<StudioPageHeader eyebrow="Browse, upload & manage your workspace" title="Project" accent="files">
			{#snippet actions()}
				<button class="fs-btn" onclick={openNewFolder}>
					<FolderPlus size={15} strokeWidth={1.8} /><span>New folder</span>
				</button>
				<button class="fs-btn fs-btn--primary" onclick={triggerUpload} disabled={uploading}>
					{#if uploading}<Loader2 size={15} class="fs-spin" />{:else}<Upload size={15} strokeWidth={1.8} />{/if}
					<span>{uploading ? `Uploading ${uploadProgress}%` : 'Upload'}</span>
				</button>
			{/snippet}
		</StudioPageHeader>
	</div>

	<!-- Toolbar: breadcrumbs + hidden toggle -->
	<div class="fs-toolbar">
		<nav class="fs-crumbs" aria-label="Breadcrumb">
			{#each $breadcrumbs as crumb, i (crumb.path)}
				{#if i > 0}<span class="fs-crumb-sep">/</span>{/if}
				<button
					class="fs-crumb"
					class:last={i === $breadcrumbs.length - 1}
					onclick={() => navigate(crumb.path)}
				>
					{#if i === 0}<Home size={13} strokeWidth={1.9} />{/if}
					{crumb.name}
				</button>
			{/each}
		</nav>
		<div class="fs-toolbar-spacer"></div>
		<label class="fs-toggle">
			<button
				class="fs-switch"
				class:on={showHidden}
				role="switch"
				aria-checked={showHidden}
				aria-label="Toggle hidden files"
				onclick={() => files.toggleShowHidden()}
			><span class="fs-switch-thumb"></span></button>
			Hidden files
		</label>
	</div>

	{#if showNewFolder}
		<div class="fs-newfolder">
			<FolderPlus size={15} strokeWidth={1.8} />
			<!-- svelte-ignore a11y_autofocus -->
			<input
				type="text"
				placeholder="Folder name…"
				bind:value={newFolderName}
				autofocus
				onkeydown={(e) => { if (e.key === 'Enter') confirmNewFolder(); else if (e.key === 'Escape') { showNewFolder = false; } }}
			/>
			<button class="fs-btn fs-btn--sm fs-btn--primary" onclick={confirmNewFolder} disabled={creatingFolder || !newFolderName.trim()}>Create</button>
			<button class="fs-btn fs-btn--sm" onclick={() => (showNewFolder = false)}>Cancel</button>
		</div>
	{/if}

	{#if error}
		<div class="fs-error">
			<AlertCircle size={15} strokeWidth={1.7} />
			<span>{error}</span>
			<button class="fs-error-close" onclick={() => files.clearError()} aria-label="Dismiss"><X size={14} strokeWidth={2} /></button>
		</div>
	{/if}

	<!-- Two-column layout -->
	<div class="fs-layout">
		<div class="fs-side">
			<aside class="fs-tree">
				<button class="fs-tree-root" class:on={currentPath === ''} onclick={() => navigate('')}>
					<Home size={14} strokeWidth={1.8} /><span>Workspace</span>
				</button>
				{#each rootDirs as dir (dir.path)}
					<FilesTreeNode name={dir.name} path={dir.path} depth={0} {currentPath} {showHidden} onNavigate={navigate} />
				{/each}
			</aside>
		</div>

		<div
			class="fs-main"
			class:drag={isDragging}
			role="region"
			aria-label="File list"
			ondragover={onDragOver}
			ondragleave={onDragLeave}
			ondrop={onDrop}
		>
			<div class="fs-list">
				<div class="fs-row fs-row--head">
					<span>Name</span>
					<span>Size</span>
					<span>Modified</span>
					<span></span>
				</div>

				{#if loading && $sortedFiles.length === 0}
					<div class="fs-state"><Loader2 size={24} class="fs-spin" /><p>Loading files…</p></div>
				{:else if $sortedFiles.length === 0}
					<div class="fs-state">
						<Folder size={30} strokeWidth={1.25} />
						<p>This folder is empty</p>
						<button class="fs-link" onclick={triggerUpload}>Upload a file</button>
					</div>
				{:else}
					{#each $sortedFiles as item (item.path)}
						{#if renameTarget?.path === item.path}
							<div class="fs-row fs-row--rename">
								<!-- svelte-ignore a11y_autofocus -->
								<input
									type="text"
									bind:value={renameValue}
									autofocus
									onkeydown={(e) => { if (e.key === 'Enter') confirmRename(); else if (e.key === 'Escape') cancelRename(); }}
								/>
								<button class="fs-icon-btn" title="Save" onclick={confirmRename}><Check size={15} strokeWidth={2} /></button>
								<button class="fs-icon-btn" title="Cancel" onclick={cancelRename}><X size={15} strokeWidth={2} /></button>
							</div>
						{:else}
							{@const Ic = fileIcon(item)}
							<div
								class="fs-row"
								class:dir={item.type === 'directory'}
								role="button"
								tabindex="0"
								onclick={() => rowClick(item)}
								onkeydown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && item.type === 'directory') { e.preventDefault(); rowClick(item); } }}
							>
								<span class="fs-nm">
									<Ic size={16} strokeWidth={1.8} class={item.type === 'directory' ? 'fs-fold' : 'fs-file'} />
									<span class="fs-nm-text">{item.name}</span>
								</span>
								<span class="fs-sz">{item.type === 'directory' ? '—' : (item.sizeFormatted ?? '—')}</span>
								<span class="fs-md">{relTime(item.modifiedAt)}</span>
								<span class="fs-actions">
									<button class="fs-icon-btn" title="Rename" onclick={(e) => startRename(item, e)}><Edit3 size={14} strokeWidth={1.8} /></button>
									<button class="fs-icon-btn fs-icon-btn--danger" title="Delete" onclick={(e) => { e.stopPropagation(); pendingDelete = item; }}><Trash2 size={14} strokeWidth={1.8} /></button>
									{#if item.type === 'file'}
										<button class="fs-icon-btn" title="Download" onclick={(e) => download(item, e)}><Download size={14} strokeWidth={1.8} /></button>
									{/if}
								</span>
							</div>
						{/if}
					{/each}
				{/if}
			</div>

			{#if isDragging}
				<div class="fs-droplay"><Upload size={26} strokeWidth={1.6} /><span>Drop files to upload</span></div>
			{/if}
		</div>
	</div>
	</div>
</div>

<input type="file" multiple bind:this={uploadInput} onchange={onUploadChange} style="display:none" />

<ConfirmDialog
	open={!!pendingDelete}
	title="Delete {pendingDelete?.type === 'directory' ? 'folder' : 'file'}"
	message={`Delete "${pendingDelete?.name}"? This cannot be undone.`}
	confirmLabel="Delete"
	danger
	onConfirm={confirmDelete}
	onCancel={() => (pendingDelete = null)}
/>

<style>
	.fs {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
		background: var(--background);
	}
	/* Centered, max-width container — matches the mockup's .wrap (not full-bleed). */
	.fs-wrap {
		width: 100%;
		max-width: 1240px;
		margin: 0 auto;
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
	.fs-head { flex-shrink: 0; padding: 22px 28px 0; }

	/* Toolbar */
	.fs-toolbar {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
		padding: 16px 28px 14px;
	}
	.fs-crumbs { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; min-width: 0; }
	.fs-crumb {
		display: inline-flex; align-items: center; gap: 5px;
		padding: 3px 7px; border-radius: 6px;
		background: transparent; border: none; cursor: pointer;
		font-family: var(--font-mono, monospace); font-size: 0.78rem;
		color: var(--muted-foreground);
		transition: background-color 110ms ease, color 110ms ease;
	}
	.fs-crumb:hover { background: var(--hover-overlay); color: var(--foreground); }
	.fs-crumb.last { color: var(--foreground); font-weight: 600; }
	.fs-crumb :global(svg) { flex-shrink: 0; }
	.fs-crumb-sep { color: var(--muted-foreground); opacity: 0.5; font-size: 0.78rem; }
	.fs-toolbar-spacer { flex: 1; }

	.fs-toggle {
		display: flex; align-items: center; gap: 8px;
		font-size: 0.78rem; font-weight: 500; color: var(--muted-foreground); cursor: pointer;
	}
	.fs-switch {
		position: relative; width: 34px; height: 19px; border-radius: 10px;
		border: none; background: var(--muted); cursor: pointer; flex-shrink: 0; transition: background 180ms ease;
	}
	.fs-switch.on { background: var(--primary); }
	.fs-switch-thumb {
		position: absolute; top: 2px; left: 2px; width: 15px; height: 15px; border-radius: 50%;
		background: var(--foreground); transition: transform 180ms ease;
	}
	.fs-switch.on .fs-switch-thumb { transform: translateX(15px); background: var(--primary-foreground); }

	/* New folder inline */
	.fs-newfolder {
		flex-shrink: 0; display: flex; align-items: center; gap: 8px;
		margin: 0 28px 12px; padding: 9px 12px;
		border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 2px);
		background: var(--card);
	}
	.fs-newfolder :global(svg) { color: var(--muted-foreground); flex-shrink: 0; }
	.fs-newfolder input {
		flex: 1; min-width: 0; height: 28px; padding: 0 8px;
		background: var(--input-bg); border: 1px solid var(--border); border-radius: 6px;
		color: var(--foreground); font-size: 0.8125rem; outline: none;
	}
	.fs-newfolder input:focus { border-color: var(--ring); }

	.fs-error {
		flex-shrink: 0; display: flex; align-items: center; gap: 8px;
		margin: 0 28px 12px; padding: 10px 12px;
		border-radius: calc(var(--radius, 0.75rem) - 2px);
		background: color-mix(in oklch, var(--destructive) 12%, transparent); color: var(--destructive); font-size: 0.8125rem;
	}
	.fs-error-close { margin-left: auto; background: transparent; border: none; color: var(--destructive); cursor: pointer; padding: 2px; display: grid; place-items: center; }

	/* Buttons */
	.fs-btn {
		display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 13px;
		background: var(--card); color: var(--foreground);
		border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 2px);
		font-size: 0.8125rem; font-weight: 500; cursor: pointer; white-space: nowrap;
		transition: background-color 120ms ease, filter 120ms ease;
	}
	.fs-btn:hover { background: var(--hover-overlay); }
	.fs-btn:disabled { opacity: 0.55; cursor: not-allowed; }
	.fs-btn--sm { height: 30px; padding: 0 11px; font-size: 0.78rem; }
	.fs-btn--primary { background: var(--primary); color: var(--primary-foreground); border-color: transparent; }
	.fs-btn--primary:hover { filter: brightness(1.08); background: var(--primary); }

	/* Two-column layout */
	.fs-layout {
		flex: 1; min-height: 0;
		display: grid; grid-template-columns: 240px 1fr; gap: 18px;
		padding: 0 28px 28px; overflow: hidden;
	}
	/* Left column: a full-height scroll container (no chrome) wrapping a
	   content-sized bordered panel — mirrors .fs-main + .fs-list on the right. */
	.fs-side { min-height: 0; overflow-y: auto; }
	.fs-tree {
		background: var(--card); border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem); padding: 8px;
	}
	.fs-tree-root {
		display: flex; align-items: center; gap: 7px; width: 100%;
		padding: 6px 8px; border-radius: 7px; margin-bottom: 2px;
		background: transparent; border: none; cursor: pointer;
		font-size: 0.8125rem; font-weight: 600; color: var(--foreground);
		transition: background-color 110ms ease;
	}
	.fs-tree-root:hover { background: var(--hover-overlay); }
	.fs-tree-root.on { background: var(--primary); color: var(--primary-foreground); }
	.fs-tree-root :global(svg) { color: var(--muted-foreground); flex-shrink: 0; }
	.fs-tree-root.on :global(svg) { color: var(--primary-foreground); }

	.fs-main { position: relative; min-height: 0; overflow-y: auto; }
	.fs-main.drag { outline: 2px dashed var(--primary); outline-offset: -4px; border-radius: var(--radius, 0.75rem); }

	.fs-list {
		border: 1px solid var(--border); border-radius: var(--radius, 0.75rem);
		overflow: hidden; background: var(--card);
	}
	.fs-row {
		display: grid; grid-template-columns: 1fr 92px 116px 84px; align-items: center; gap: 12px;
		padding: 10px 16px; border-bottom: 1px solid var(--border-subtle, var(--border)); font-size: 0.8125rem;
		transition: background-color 110ms ease;
	}
	.fs-row:last-child { border-bottom: none; }
	.fs-row--head {
		font-family: var(--font-mono, monospace); font-size: 0.66rem; text-transform: uppercase;
		letter-spacing: 0.08em; color: var(--muted-foreground); font-weight: 600;
		background: color-mix(in oklch, var(--muted) 35%, transparent);
	}
	.fs-row.dir { cursor: pointer; }
	.fs-row:not(.fs-row--head):hover { background: var(--hover-overlay); }
	.fs-row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	.fs-nm { display: flex; align-items: center; gap: 10px; font-weight: 500; min-width: 0; }
	.fs-nm :global(svg) { flex-shrink: 0; color: var(--muted-foreground); }
	.fs-nm :global(.fs-fold) { color: var(--warning, #d97706); }
	.fs-nm-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.fs-sz, .fs-md { font-family: var(--font-mono, monospace); font-size: 0.7rem; color: var(--muted-foreground); }

	.fs-actions { display: flex; align-items: center; justify-content: flex-end; gap: 2px; opacity: 0; transition: opacity 110ms ease; }
	.fs-row:hover .fs-actions, .fs-row:focus-within .fs-actions { opacity: 1; }
	.fs-icon-btn {
		display: grid; place-items: center; width: 26px; height: 26px;
		background: transparent; border: none; border-radius: 6px;
		color: var(--muted-foreground); cursor: pointer; transition: color 110ms ease, background-color 110ms ease;
	}
	.fs-icon-btn:hover { color: var(--foreground); background: var(--hover-overlay); }
	.fs-icon-btn--danger:hover { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }

	.fs-row--rename { grid-template-columns: 1fr auto auto; }
	.fs-row--rename input {
		height: 28px; padding: 0 8px; background: var(--input-bg);
		border: 1px solid var(--ring); border-radius: 6px; color: var(--foreground); font-size: 0.8125rem; outline: none;
	}

	.fs-state {
		display: flex; flex-direction: column; align-items: center; gap: 8px;
		padding: 44px 24px; text-align: center; color: var(--muted-foreground); font-size: 0.85rem;
	}
	.fs-state :global(svg) { color: var(--muted-foreground); opacity: 0.7; }
	.fs-link { background: none; border: none; color: var(--primary); cursor: pointer; font-size: 0.8125rem; }
	.fs-link:hover { text-decoration: underline; }

	.fs-droplay {
		position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px;
		background: color-mix(in oklch, var(--primary) 10%, transparent); color: var(--primary);
		font-size: 0.9rem; font-weight: 600; border-radius: var(--radius, 0.75rem); pointer-events: none;
	}

	:global(.fs-spin) { animation: fs-spin 1s linear infinite; }
	@keyframes fs-spin { to { transform: rotate(360deg); } }

	@media (max-width: 767px) {
		.fs-head { padding: 16px 16px 0; }
		.fs-toolbar { padding: 12px 16px 10px; }
		.fs-newfolder, .fs-error { margin: 0 16px 10px; }
		.fs-layout { grid-template-columns: 1fr; padding: 0 16px 20px; overflow-y: auto; }
		.fs-side { max-height: 220px; }
		.fs-row { grid-template-columns: 1fr 70px 64px; }
		.fs-md { display: none; }
		.fs-actions { opacity: 1; }
	}
</style>
