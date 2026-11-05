<script lang="ts">
	import Switch from '$lib/components/settings/rows/Switch.svelte';
	/** Files: shared navigation panel and a full-width workspace table. */
	import { getContext, onMount } from 'svelte';
	import { ViewShell, ViewPanel, Checkbox, DetailPane, EmptyDetail, InlineBanner } from '$lib/components/layout/view';
	import { FEATURE_PANEL_CONTEXT, type FeaturePanelContext } from '$lib/components/layout/feature-panel-context';
	import FilesPreview from './FilesPreview.svelte';
	import { withAuth } from '$lib/api/runtime';
	import { listInstalls, type Install } from '$lib/api/installs';
	import {
		Folder, File as FileIcon, FileCode, FileText, FileImage, FileVideo, FileAudio,
		FileArchive, FileSpreadsheet, Database, Settings, Package,
		FolderPlus, Upload, Download, Trash2, Edit3, Home, Check, X, Loader2,
		ServerOff, FolderX, RotateCw, ChevronDown
	} from 'lucide-svelte';
	import { files, sortedFiles, breadcrumbs, type FileItem } from '$lib/stores/files.svelte';

	import FilesTreeNode from './FilesTreeNode.svelte';
	import FilesWorkerPicker from './FilesWorkerPicker.svelte';
	import { findWorker, workerOptions, type WorkerOption } from './files-workers';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';
	import FilesTerminalDrawer from './FilesTerminalDrawer.svelte';
	import { TerminalSquare } from 'lucide-svelte';
	let terminalDrawer: ReturnType<typeof FilesTerminalDrawer>;
	let terminalBusy = $state(false);
	let phone = $state(false);
	const feature = getContext<FeaturePanelContext | undefined>(FEATURE_PANEL_CONTEXT);
	let detailOpen = $state(false);
	let preview = $state<FileItem | null>(null);
	const folderName = $derived($breadcrumbs.at(-1)?.name || 'Workspace');

	let uploadInput = $state<HTMLInputElement | null>(null);
	let showNewFolder = $state(false);
	let newFolderName = $state('');
	let creatingFolder = $state(false);
	let renameTarget = $state<FileItem | null>(null);
	let renameValue = $state('');
	let pendingDelete = $state<FileItem | null>(null);
	let pendingBulkDelete = $state(false);
	let isDragging = $state(false);
	// Touch/narrow only: which row has its collapsed ⋮ action tray expanded.
	// One open at a time; desktop keeps the always-inline hover-revealed rail.
	let actionsOpenPath = $state<string | null>(null);

	// Root-level directories for the tree sidebar.
	let rootDirs = $state<{ name: string; path: string }[]>([]);
	// Bumped after every folder mutation so expanded tree nodes refetch children.
	let treeVersion = $state(0);
	function refreshTree() { treeVersion++; }

	const currentPath = $derived($files.currentPath);
	const loading = $derived($files.loading);
	const uploading = $derived($files.uploading);
	const uploadProgress = $derived($files.uploadProgress);
	const error = $derived($files.error);
	const loadError = $derived($files.loadError);
	const workerId = $derived($files.workerId);
	// No status = the request never got an answer; 503 = the relay says offline.
	const workerOffline = $derived(
		workerId !== null && loadError !== null && ($files.loadErrorStatus === null || $files.loadErrorStatus === 503)
	);

	// ── Workers ──
	let installs = $state<Install[]>([]);
	let installsLoading = $state(true);
	let installsFailed = $state(false);
	const workerChoices = $derived(workerOptions(installs));
	const selectedWorker = $derived<WorkerOption>(
		findWorker(workerChoices, workerId) ?? {
			// A remembered worker before the list arrives: never show this
			// device's name over another worker's files.
			key: workerId, name: 'Another worker', hint: null, isSelf: false,
			online: true, disabled: false, reason: null
		}
	);
	const workerName = $derived(selectedWorker.isSelf ? 'this device' : selectedWorker.name);

	async function loadWorkers() {
		installsLoading = true;
		installsFailed = false;
		try {
			const res = await listInstalls();
			installs = Array.isArray(res?.installs) ? res.installs : [];
			// A remembered worker that left the network falls back to this one.
			if ($files.workerId !== null && !findWorker(workerOptions(installs), $files.workerId)) {
				files.setWorker(null);
			}
		} catch {
			installsFailed = true;
		} finally {
			installsLoading = false;
		}
	}

	function selectWorker(key: string | null) {
		showNewFolder = false;
		renameTarget = null;
		pendingDelete = null;
		pendingBulkDelete = false;
		actionsOpenPath = null;
		preview = null;
		files.setWorker(key);
	}
	const showHidden = $derived($files.showHidden);
	const selectedCount = $derived($files.selectedFiles.size);
	const allSelected = $derived($sortedFiles.length > 0 && $sortedFiles.every((f) => $files.selectedFiles.has(f.path)));

	let rootSeq = 0;
	async function loadRootDirs() {
		const seq = ++rootSeq;
		try {
			const params = new URLSearchParams({ path: '', show_hidden: String($files.showHidden) });
			const res = await fetch(`${files.base()}/files?${params}`, withAuth({ }));
			// Another worker (or toggle) was chosen meanwhile: its own fetch owns the tree.
			if (seq !== rootSeq) return;
			if (!res.ok) { rootDirs = []; return; }
			const data = await res.json();
			if (seq !== rootSeq) return;
			rootDirs = (data.files as FileItem[])
				.filter((f) => f.type === 'directory')
				.map((f) => ({ name: f.name, path: f.path }));
		} catch {
			/* tree is best-effort */
			if (seq === rootSeq) rootDirs = [];
		}
	}

	onMount(() => {
		const query = window.matchMedia('(max-width: 600px)');
		const update = () => { phone = query.matches; };
		update();
		query.addEventListener('change', update);
		files.loadFiles();
		loadWorkers();
		return () => query.removeEventListener('change', update);
	});

	// Keep the tree's root list in sync with the worker and the hidden-files
	// toggle. The previous worker's folders go at once, never shown under the new one.
	$effect(() => {
		$files.showHidden;
		$files.workerId;
		rootDirs = [];
		loadRootDirs();
	});

	function navigate(path: string) {
		detailOpen = true;
		preview = null;
		files.navigateTo(path);
	}

	// ── New folder ──
	function openNewFolder() {
		showNewFolder = true;
		newFolderName = '';
	}
	async function confirmNewFolder() {
		if (creatingFolder) return;
		const name = newFolderName.trim();
		if (!name) { showNewFolder = false; return; }
		creatingFolder = true;
		try {
			// On failure the store shows the error; keep the typed name for a retry.
			if (!(await files.createFolder(name))) return;
			await loadRootDirs();
			refreshTree();
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
		if (loadError) return;
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
			// On failure the store shows the error; keep the rename open for a retry.
			if (!(await files.rename(renameTarget.path, renameValue.trim()))) return;
			preview = null;
			await loadRootDirs();
			refreshTree();
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
		preview = null;
		await files.delete(t.path);
		await loadRootDirs();
		refreshTree();
	}

	// ── Multi-select delete ──
	function onHeaderSelectAll(checked: boolean) {
		if (checked) files.selectAll();
		else files.clearSelection();
	}
	async function confirmDeleteSelected() {
		pendingBulkDelete = false;
		preview = null;
		await files.deleteSelected();
		await loadRootDirs();
		refreshTree();
	}

	function rowClick(item: FileItem) {
		if (renameTarget?.path === item.path) return;
		if (item.type === 'directory') navigate(item.path);
		else preview = item;
	}

	function download(item: FileItem, e: Event) {
		e.stopPropagation();
		if (item.type === 'file') files.downloadFile(item.path, item.name);
	}

	// ── Collapsed ⋮ action tray (touch / narrow) ──
	// RowActionMenu already stops propagation before calling this, so the row's
	// own click (navigate) never also fires.
	function toggleRowActions(_e: MouseEvent | KeyboardEvent, path: string) {
		actionsOpenPath = actionsOpenPath === path ? null : path;
	}
	// The ⋮ toggle and every tray button stop propagation, so any click that
	// reaches the window is outside an open tray — collapse it.
	function onWindowClick() {
		if (actionsOpenPath !== null) actionsOpenPath = null;
	}
	function onWindowKeydown(e: KeyboardEvent) {
		if (feature && !feature.active) return;
		if (e.key === 'Escape') actionsOpenPath = null;
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

<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />

<ViewShell label="Files" {detailOpen} onBack={() => detailOpen = false}>
	{#snippet panel()}
		<ViewPanel title="Files" listLabel="Folders on {workerName}">
			{#snippet filters()}
				<FilesWorkerPicker options={workerChoices} selected={selectedWorker} loading={installsLoading}
					failed={installsFailed} onSelect={selectWorker} onRetry={loadWorkers} />
			{/snippet}
			<button class="st-btn touch ghost fs-root" class:on={currentPath === ''} onclick={() => navigate('')}>
				<Home size={14} /><span>Workspace</span>
			</button>
			{#key `${workerId}:${showHidden}`}
				{#each rootDirs as dir (dir.path)}
					<FilesTreeNode name={dir.name} path={dir.path} depth={0} {currentPath} {showHidden} onNavigate={navigate} {treeVersion} />
				{/each}
			{/key}
			{#snippet footer()}
				<label class="fs-hidden"><span>Hidden files</span><Switch controlled size="sm" checked={showHidden} label="Toggle hidden files" onchange={() => files.toggleShowHidden()} /></label>
			{/snippet}
		</ViewPanel>
	{/snippet}
	{#snippet main(back)}
		{#snippet desktopActions()}
			{#if workerId === null}<button class="st-btn touch" disabled={loading || !!loadError || terminalBusy} onclick={() => terminalDrawer.open(currentPath)} title="Open terminal in this folder on this worker"><TerminalSquare size={15} /><span>Open terminal here</span></button>{/if}
			<button class="st-btn touch" onclick={openNewFolder} disabled={!!loadError}><FolderPlus size={15} /><span>New folder</span></button>
			<button class="st-btn touch primary" onclick={triggerUpload} disabled={uploading || !!loadError}>{#if uploading}<Loader2 size={15} class="fs-spin" />{:else}<Upload size={15} />{/if}<span>{uploading ? `Uploading ${uploadProgress}%` : 'Upload'}</span></button>
		{/snippet}
		<DetailPane width="data" name={folderName} onBack={back} backLabel="Files" primary={phone ? undefined : desktopActions}>
				{#snippet tile()}{/snippet}
				{#snippet topStatus()}{/snippet}
				{#snippet subtitle()}
					<span>{selectedWorker.name}</span>
					{#if currentPath}<span aria-hidden="true">·</span><nav class="fs-crumbs" aria-label="Breadcrumb">
						{#each $breadcrumbs as crumb, i (crumb.path)}
							{#if i > 0}<span aria-hidden="true">/</span>{/if}
							<button class="st-btn touch ghost dense" aria-current={i === $breadcrumbs.length - 1 ? 'page' : undefined} title={crumb.path || 'Workspace'} onclick={() => navigate(crumb.path)}>{crumb.name}</button>
						{/each}
					</nav>{/if}
				{/snippet}
				{#if phone}
					<div class="fs-phone-actions">
						<button class="st-btn touch primary" onclick={triggerUpload} disabled={uploading || !!loadError}>{#if uploading}<Loader2 size={15} class="fs-spin" />{:else}<Upload size={15} />{/if}<span>{uploading ? `Uploading ${uploadProgress}%` : 'Upload'}</span></button>
						<button class="st-btn touch" onclick={openNewFolder} disabled={!!loadError}><FolderPlus size={15} /><span>New folder</span></button>
						{#if workerId === null}<details class="fs-more"><summary class="st-btn touch" aria-label="More file actions">More<ChevronDown size={14} /></summary><div class="fs-more-menu"><button class="st-btn touch" disabled={loading || !!loadError || terminalBusy} onclick={(e) => { e.currentTarget.closest('details')?.removeAttribute('open'); terminalDrawer.open(currentPath); }}>Open terminal here</button></div></details>{/if}
					</div>
				{/if}
				{#if showNewFolder}
					<form class="fs-newfolder" onsubmit={(e) => { e.preventDefault(); confirmNewFolder(); }}>
						<FolderPlus size={16} />
						<!-- svelte-ignore a11y_autofocus -->
						<input aria-label="Folder name" placeholder="Folder name" bind:value={newFolderName} autofocus onkeydown={(e) => { if (e.key === 'Escape') showNewFolder = false; }} />
						<button class="st-btn touch primary" disabled={creatingFolder || !newFolderName.trim()}>Create</button>
						<button type="button" class="st-btn touch" onclick={() => showNewFolder = false}>Cancel</button>
					</form>
				{/if}
				{#if selectedCount > 0}
					<div class="fs-bulkbar" role="region" aria-label="Selected files">
						<span>{selectedCount} selected</span>
						<button class="st-btn touch" onclick={() => files.clearSelection()}>Clear</button>
						<button class="st-btn touch danger" onclick={() => pendingBulkDelete = true}>Delete selected</button>
					</div>
				{/if}
				{#if error}<div role="alert"><InlineBanner tone="info">{error}{#snippet actions()}<button class="st-btn touch dense" aria-label="Dismiss error" onclick={() => files.clearError()}><X size={14} /></button>{/snippet}</InlineBanner></div>{/if}
				{#if preview}
					{#key `${workerId}:${preview.path}`}<FilesPreview item={preview} base={files.base()} onClose={() => preview = null} />{/key}
				{/if}
				<div class="fs-main st-card" class:drag={isDragging} role="region" aria-label="File list" aria-busy={loading}
					ondragover={onDragOver} ondragleave={onDragLeave} ondrop={onDrop}>
					<div class="fs-row fs-row--head" aria-hidden={!!loadError || ($sortedFiles.length === 0)}>
						<Checkbox isolate label="Select all" checked={allSelected} indeterminate={selectedCount > 0 && !allSelected} onchange={onHeaderSelectAll} disabled={loading || !!loadError || !$sortedFiles.length} />
						<span class="fs-nm">Name</span><span class="fs-sz">Size</span><span class="fs-md">Modified</span><span class="fs-actions-head"></span>
					</div>
					{#if loadError && workerOffline}
						<div role="alert"><EmptyDetail title="{selectedWorker.name} is offline" message="{loadError} Its files will show here once it reconnects." icon={ServerOff}>
							<button class="st-btn touch" onclick={() => files.loadFiles()} disabled={loading}><RotateCw size={14} />Try again</button>
							<button class="st-btn touch primary" onclick={() => selectWorker(null)}>Back to this device</button>
						</EmptyDetail></div>
					{:else if loadError}
						<div role="alert"><EmptyDetail title="This folder could not be opened" message={loadError} icon={FolderX}>
							<button class="st-btn touch" onclick={() => files.loadFiles()} disabled={loading}><RotateCw size={14} />Try again</button>
							{#if currentPath}<button class="st-btn touch" onclick={() => navigate('')}>Go to Workspace</button>{/if}
							{#if workerId !== null}<button class="st-btn touch primary" onclick={() => selectWorker(null)}>Back to this device</button>{/if}
						</EmptyDetail></div>
					{:else if loading && $sortedFiles.length === 0}
						<div class="fs-loading" role="status"><Loader2 size={20} class="fs-spin" />{workerId === null ? 'Loading files…' : `Loading files from ${selectedWorker.name}…`}</div>
					{:else if $sortedFiles.length === 0}
						<EmptyDetail title="This folder is empty" message="Upload a file or create a folder to get started." icon={Folder}>
							<button class="st-btn touch primary" onclick={triggerUpload}>Upload a file</button>
						</EmptyDetail>
					{:else}
						{#each $sortedFiles as item (item.path)}
							{#if renameTarget?.path === item.path}
								<form class="fs-rename" onsubmit={(e) => { e.preventDefault(); confirmRename(); }}>
									<!-- svelte-ignore a11y_autofocus -->
									<input bind:value={renameValue} aria-label="New name for {item.name}" autofocus onkeydown={(e) => { if (e.key === 'Escape') cancelRename(); }} />
									<button class="st-btn touch icon ghost" title="Save" aria-label="Save"><Check size={15} /></button>
									<button type="button" class="st-btn touch icon ghost" title="Cancel" aria-label="Cancel" onclick={cancelRename}><X size={15} /></button>
								</form>
							{:else}
								{@const Ic = fileIcon(item)}
								<div class="fs-row" class:selected={$files.selectedFiles.has(item.path)} role="button" tabindex="0"
									onclick={() => rowClick(item)} onkeydown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) { e.preventDefault(); rowClick(item); } }}>
									<Checkbox isolate label="Select {item.name}" checked={$files.selectedFiles.has(item.path)} onchange={() => files.toggleSelection(item.path)} />
									<span class="fs-nm" title={item.name}><Ic size={16} /><span class="fs-nm-text">{item.name}</span></span>
									<span class="fs-sz">{item.type === 'directory' ? '—' : item.sizeFormatted ?? '—'}</span>
									<span class="fs-md" title={new Date(item.modifiedAt).toLocaleString()}>{relTime(item.modifiedAt)}</span>
									<RowActionMenu open={actionsOpenPath === item.path} onToggle={(e) => toggleRowActions(e, item.path)} label="File actions">
										{#if workerId === null}<button class="st-btn touch icon ghost" disabled={terminalBusy} title={item.type === 'directory' ? 'Open terminal here' : 'Open terminal in parent folder'} aria-label={`Open terminal ${item.type === 'directory' ? 'in' : 'in parent folder of'} ${item.name}`} onclick={(e) => { e.stopPropagation(); actionsOpenPath = null; void terminalDrawer.open(item.type === 'directory' ? item.path : currentPath); }}><TerminalSquare size={14} /></button>{/if}
										<button class="st-btn touch icon ghost" title="Rename" aria-label="Rename {item.name}" onclick={(e) => startRename(item, e)}><Edit3 size={14} /></button>
										<button class="st-btn touch icon danger ghost" title="Delete" aria-label="Delete {item.name}" onclick={(e) => { e.stopPropagation(); pendingDelete = item; }}><Trash2 size={14} /></button>
										{#if item.type === 'file'}<button class="st-btn touch icon ghost" title="Download" aria-label="Download {item.name}" onclick={(e) => download(item, e)}><Download size={14} /></button>{/if}
									</RowActionMenu>
								</div>
							{/if}
						{/each}
						{/if}
					{#if isDragging}<div class="fs-droplay"><Upload size={26} /><span>Drop files to upload</span></div>{/if}
				</div>
				{#if !loading && !loadError && $sortedFiles.length}
					{@const folders = $sortedFiles.filter(item => item.type === 'directory').length}
					{@const countFiles = $sortedFiles.length - folders}
					<div class="fs-footer">{[folders ? `${folders} ${folders === 1 ? 'folder' : 'folders'}` : '', countFiles ? `${countFiles} ${countFiles === 1 ? 'file' : 'files'}` : ''].filter(Boolean).join(', ')}</div>
				{/if}
			</DetailPane>
	{/snippet}
</ViewShell>
<FilesTerminalDrawer bind:this={terminalDrawer} bind:busy={terminalBusy} active={feature?.active ?? true} />
<input type="file" multiple bind:this={uploadInput} onchange={onUploadChange} hidden />
<ConfirmDialog open={!!pendingDelete} title="Delete {pendingDelete?.type === 'directory' ? 'folder' : 'file'}" message={`Delete "${pendingDelete?.name}"? This cannot be undone.`} confirmLabel="Delete" danger onConfirm={confirmDelete} onCancel={() => pendingDelete = null} />
<ConfirmDialog open={pendingBulkDelete} title="Delete selected items" message={`Delete ${selectedCount} selected item${selectedCount === 1 ? '' : 's'}? This cannot be undone.`} confirmLabel="Delete" danger onConfirm={confirmDeleteSelected} onCancel={() => pendingBulkDelete = false} />

<style>
	.fs-root { width: 100%; justify-content: flex-start; }
	.fs-root.on { background: var(--st-accent); }
	.fs-hidden { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 44px; padding: 0 8px; font-size: 13px; color: var(--st-mfg); }
	.fs-crumbs { display: flex; gap: 2px; align-items: center; flex-wrap: wrap; min-width: 0; }
	.fs-crumbs button { max-width: 240px; overflow: hidden; text-overflow: ellipsis; display: block; }
	.fs-crumbs button[aria-current] { color: var(--st-fg); }
	.fs-main { position: relative; min-width: 0; padding: 0 16px 16px; }
	.fs-footer { padding-top: 12px; color: var(--st-mfg); font-size: 12px; }
	.fs-phone-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)) auto; gap: 8px; align-items: center; margin-bottom: 20px; }
	.fs-phone-actions > button { flex: 1 1 0; min-width: 0; padding-inline: 8px; min-height: 44px; }
	.fs-more { position: relative; flex: none; }
	.fs-more summary { list-style: none; cursor: pointer; min-width: 44px; min-height: 44px; padding-inline: 0; gap: 2px; font-size: 12px; }
	.fs-more summary::-webkit-details-marker { display: none; }
	.fs-more summary::before { display: none !important; }
	.fs-more-menu { position: absolute; top: calc(100% + 4px); right: 0; z-index: 20; padding: 8px; min-width: max-content; border: 1px solid var(--st-border); border-radius: var(--st-radius-md); background: var(--st-popover); box-shadow: var(--st-shadow); }
	.fs-row { display: grid; grid-template-columns: 32px minmax(0, 1fr) 132px 80px 100px; grid-template-areas: "select name actions size modified"; gap: 12px; align-items: center; min-height: 44px; border-bottom: 1px solid var(--st-border); font-size: 13px; cursor: pointer; --ram-reveal: 0; --ram-touch-size: 44px; }
	.fs-row:hover, .fs-row:focus-within { background: var(--st-hover); --ram-reveal: 1; }
	.fs-row.selected { background: var(--st-accent); }
	.fs-row--head { color: var(--st-mfg); cursor: default; font-size: 12px; }
	.fs-row--head:hover { background: transparent; }
	.fs-row > :global(:first-child) { grid-area: select; }
	.fs-row > :global(.ram), .fs-actions-head { grid-area: actions; }
	.fs-nm { grid-area: name; display: flex; align-items: center; gap: 10px; min-width: 0; }
	.fs-nm :global(svg) { color: currentColor; flex: none; }
	.fs-nm-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.fs-sz, .fs-md { color: var(--st-mfg); font-variant-numeric: tabular-nums; white-space: nowrap; }
	.fs-sz { grid-area: size; text-align: right; }
	.fs-md { grid-area: modified; padding-left: 12px; text-align: right; }
	.fs-newfolder, .fs-rename, .fs-bulkbar { display: flex; gap: 8px; align-items: center; padding: 12px 0; flex-wrap: wrap; }
	.fs-newfolder input, .fs-rename input { flex: 1; min-width: 100px; height: 32px; border: 1px solid var(--st-input); border-radius: var(--st-radius-md); background: var(--st-field); color: var(--st-fg); padding: 0 10px; font-size: 13px; }
	.fs-bulkbar { font-size: 13px; font-variant-numeric: tabular-nums; }
	.fs-loading { display: flex; align-items: center; gap: 12px; padding: 48px 0; color: var(--st-mfg); font-size: 13px; }
	.fs-droplay { position: absolute; inset: 0; display: flex; justify-content: center; align-items: center; gap: 12px; min-height: 160px; background: var(--st-card); border: 1px dashed var(--st-border-strong); pointer-events: none; }
	:global(.fs-spin) { animation: fs-spin 1s linear infinite; }
	@keyframes fs-spin { to { transform: rotate(360deg); } }
	@container settings (max-width: 850px) { .fs-row { grid-template-columns: 32px minmax(0, 1fr) 112px 64px 100px; gap: 8px; } }
	@media (max-width: 600px), (pointer: coarse) { .fs-root { min-height: 44px; } .fs-row { grid-template-columns: 32px minmax(0, 1fr) 44px 44px; grid-template-areas: "select name size actions"; gap: 6px; min-height: 52px; } .fs-md { display: none; } .fs-crumbs button { max-width: 180px; } .fs-newfolder input, .fs-rename input { height: 44px; font-size: 16px; } }
	@media (prefers-reduced-motion: reduce) { :global(.fs-spin) { animation: none; } }
</style>
