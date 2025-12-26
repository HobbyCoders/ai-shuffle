<script lang="ts">
	import { onMount } from 'svelte';
	import {
		Folder,
		File,
		ChevronRight,
		Upload,
		FolderPlus,
		Download,
		Trash2,
		Edit3,
		LayoutGrid,
		LayoutList,
		ArrowUp,
		RefreshCw,
		Eye,
		EyeOff,
		MoreVertical,
		Check,
		X,
		ChevronDown
	} from 'lucide-svelte';
	import { files, sortedFiles, breadcrumbs, hasSelection, selectionCount } from '$lib/stores/files';
	import type { FileItem } from '$lib/stores/files';
	import { api } from '$lib/api/client';
	import type { Project } from '$lib/stores/chat';

	// State
	let projects: Project[] = $state([]);
	let loadingProjects = $state(true);
	let showNewFolderInput = $state(false);
	let newFolderName = $state('');
	let renameTarget: FileItem | null = $state(null);
	let renameValue = $state('');
	let contextMenu: { x: number; y: number; item: FileItem } | null = $state(null);
	let uploadInput: HTMLInputElement;
	let isMobile = $state(false);

	// Derived state using $derived
	let selectedProjectId = $derived($files.selectedProjectId);
	let currentPath = $derived($files.currentPath);
	let loading = $derived($files.loading);
	let uploading = $derived($files.uploading);
	let uploadProgress = $derived($files.uploadProgress);
	let error = $derived($files.error);
	let viewMode = $derived($files.viewMode);
	let showHidden = $derived($files.showHidden);
	let selectedFiles = $derived($files.selectedFiles);

	// Load projects on mount
	onMount(async () => {
		// Check mobile
		function checkMobile() {
			isMobile = window.innerWidth < 768;
		}
		checkMobile();
		window.addEventListener('resize', checkMobile);

		// Load projects
		try {
			projects = await api.get<Project[]>('/projects');
		} catch (e) {
			console.error('[Files] Failed to load projects:', e);
		} finally {
			loadingProjects = false;
		}

		// If there's a persisted project selection, load its files
		if ($files.selectedProjectId) {
			files.loadFiles();
		}

		// Close context menu on click outside
		function handleClickOutside(e: MouseEvent) {
			if (contextMenu) {
				contextMenu = null;
			}
		}
		document.addEventListener('click', handleClickOutside);

		return () => {
			window.removeEventListener('resize', checkMobile);
			document.removeEventListener('click', handleClickOutside);
		};
	});

	// Handlers
	function handleProjectSelect(projectId: string) {
		files.selectProject(projectId);
	}

	function handleNavigate(item: FileItem) {
		if (item.type === 'directory') {
			files.navigateTo(item.path);
		}
	}

	function handleBreadcrumbClick(path: string) {
		files.navigateTo(path);
	}

	function handleUploadClick() {
		uploadInput?.click();
	}

	async function handleFileUpload(e: Event) {
		const input = e.target as HTMLInputElement;
		if (input.files && input.files.length > 0) {
			await files.uploadFiles(input.files);
			input.value = '';
		}
	}

	function handleNewFolder() {
		showNewFolderInput = true;
		newFolderName = '';
	}

	async function confirmNewFolder() {
		if (newFolderName.trim()) {
			await files.createFolder(newFolderName.trim());
		}
		showNewFolderInput = false;
		newFolderName = '';
	}

	function cancelNewFolder() {
		showNewFolderInput = false;
		newFolderName = '';
	}

	function handleRename(item: FileItem) {
		renameTarget = item;
		renameValue = item.name;
		contextMenu = null;
	}

	async function confirmRename() {
		if (renameTarget && renameValue.trim() && renameValue !== renameTarget.name) {
			await files.rename(renameTarget.path, renameValue.trim());
		}
		renameTarget = null;
		renameValue = '';
	}

	function cancelRename() {
		renameTarget = null;
		renameValue = '';
	}

	async function handleDelete(item: FileItem) {
		if (confirm(`Delete "${item.name}"?`)) {
			await files.delete(item.path);
		}
		contextMenu = null;
	}

	async function handleDeleteSelected() {
		const count = $selectionCount;
		if (confirm(`Delete ${count} selected item${count > 1 ? 's' : ''}?`)) {
			await files.deleteSelected();
		}
	}

	function handleDownload(item: FileItem) {
		if (item.type === 'file') {
			files.downloadFile(item.path, item.name);
		}
		contextMenu = null;
	}

	function handleContextMenu(e: MouseEvent, item: FileItem) {
		e.preventDefault();
		e.stopPropagation();
		contextMenu = { x: e.clientX, y: e.clientY, item };
	}

	function handleItemClick(e: MouseEvent, item: FileItem) {
		if (e.ctrlKey || e.metaKey) {
			files.toggleSelection(item.path);
		} else if (item.type === 'directory') {
			handleNavigate(item);
		}
	}

	function getFileIcon(item: FileItem) {
		if (item.type === 'directory') return Folder;
		return File;
	}

	function formatDate(dateStr: string) {
		const date = new Date(dateStr);
		return date.toLocaleDateString(undefined, {
			year: 'numeric',
			month: 'short',
			day: 'numeric'
		});
	}
</script>

<div class="files-view" class:mobile={isMobile}>
	<!-- Header with project selector -->
	<div class="files-header">
		<div class="project-selector">
			{#if loadingProjects}
				<div class="loading-projects">
					<RefreshCw class="w-4 h-4 animate-spin" />
					<span>Loading projects...</span>
				</div>
			{:else if projects.length === 0}
				<div class="no-projects">
					<span class="text-muted-foreground">No projects available</span>
				</div>
			{:else}
				<div class="select-wrapper">
					<select
						class="project-select"
						value={selectedProjectId || ''}
						onchange={(e) => handleProjectSelect((e.target as HTMLSelectElement).value)}
					>
						<option value="" disabled>Select a project</option>
						{#each projects as project}
							<option value={project.id}>{project.name}</option>
						{/each}
					</select>
					<ChevronDown class="select-icon" />
				</div>
			{/if}
		</div>

		{#if selectedProjectId}
			<div class="header-actions">
				<button
					type="button"
					class="icon-btn"
					onclick={handleUploadClick}
					title="Upload files"
				>
					<Upload class="w-4 h-4" />
				</button>
				<button
					type="button"
					class="icon-btn"
					onclick={handleNewFolder}
					title="New folder"
				>
					<FolderPlus class="w-4 h-4" />
				</button>
				<button
					type="button"
					class="icon-btn"
					onclick={() => files.toggleShowHidden()}
					title={showHidden ? 'Hide hidden files' : 'Show hidden files'}
				>
					{#if showHidden}
						<Eye class="w-4 h-4" />
					{:else}
						<EyeOff class="w-4 h-4" />
					{/if}
				</button>
				<button
					type="button"
					class="icon-btn"
					onclick={() => files.setViewMode(viewMode === 'list' ? 'grid' : 'list')}
					title={viewMode === 'list' ? 'Grid view' : 'List view'}
				>
					{#if viewMode === 'list'}
						<LayoutGrid class="w-4 h-4" />
					{:else}
						<LayoutList class="w-4 h-4" />
					{/if}
				</button>
				<button
					type="button"
					class="icon-btn"
					onclick={() => files.loadFiles()}
					title="Refresh"
				>
					<RefreshCw class="w-4 h-4 {loading ? 'animate-spin' : ''}" />
				</button>
			</div>
		{/if}
	</div>

	<!-- Breadcrumbs -->
	{#if selectedProjectId}
		<div class="breadcrumbs">
			{#each $breadcrumbs as crumb, i}
				<button
					type="button"
					class="breadcrumb"
					class:current={i === $breadcrumbs.length - 1}
					onclick={() => handleBreadcrumbClick(crumb.path)}
				>
					{crumb.name}
				</button>
				{#if i < $breadcrumbs.length - 1}
					<ChevronRight class="breadcrumb-sep" />
				{/if}
			{/each}
		</div>
	{/if}

	<!-- Selection bar -->
	{#if $hasSelection}
		<div class="selection-bar">
			<span>{$selectionCount} selected</span>
			<div class="selection-actions">
				<button type="button" class="action-btn danger" onclick={handleDeleteSelected}>
					<Trash2 class="w-4 h-4" />
					Delete
				</button>
				<button type="button" class="action-btn" onclick={() => files.clearSelection()}>
					<X class="w-4 h-4" />
					Clear
				</button>
			</div>
		</div>
	{/if}

	<!-- Upload progress -->
	{#if uploading}
		<div class="upload-progress">
			<div class="progress-bar">
				<div class="progress-fill" style="width: {uploadProgress}%"></div>
			</div>
			<span class="progress-text">Uploading... {uploadProgress}%</span>
		</div>
	{/if}

	<!-- Error display -->
	{#if error}
		<div class="error-banner">
			<span>{error}</span>
			<button type="button" onclick={() => files.clearError()}>
				<X class="w-4 h-4" />
			</button>
		</div>
	{/if}

	<!-- Main content -->
	<div class="files-content">
		{#if !selectedProjectId}
			<div class="empty-state">
				<Folder class="w-12 h-12 text-muted-foreground/50" />
				<p class="text-muted-foreground">Select a project to browse files</p>
			</div>
		{:else if loading && $sortedFiles.length === 0}
			<div class="loading-state">
				<RefreshCw class="w-8 h-8 animate-spin text-muted-foreground" />
				<p class="text-muted-foreground">Loading files...</p>
			</div>
		{:else if $sortedFiles.length === 0 && !showNewFolderInput}
			<div class="empty-state">
				<Folder class="w-12 h-12 text-muted-foreground/50" />
				<p class="text-muted-foreground">This folder is empty</p>
				<div class="empty-actions">
					<button type="button" class="action-btn primary" onclick={handleUploadClick}>
						<Upload class="w-4 h-4" />
						Upload files
					</button>
					<button type="button" class="action-btn" onclick={handleNewFolder}>
						<FolderPlus class="w-4 h-4" />
						New folder
					</button>
				</div>
			</div>
		{:else}
			<!-- File list/grid -->
			<div class="file-list" class:grid-view={viewMode === 'grid'}>
				<!-- New folder input -->
				{#if showNewFolderInput}
					<div class="file-item new-folder-input">
						<Folder class="file-icon folder" />
						<input
							type="text"
							class="rename-input"
							bind:value={newFolderName}
							placeholder="Folder name"
							onkeydown={(e) => {
								if (e.key === 'Enter') confirmNewFolder();
								if (e.key === 'Escape') cancelNewFolder();
							}}
						/>
						<button type="button" class="inline-btn" onclick={confirmNewFolder}>
							<Check class="w-4 h-4" />
						</button>
						<button type="button" class="inline-btn" onclick={cancelNewFolder}>
							<X class="w-4 h-4" />
						</button>
					</div>
				{/if}

				<!-- File items -->
				{#each $sortedFiles as item}
					{@const isSelected = selectedFiles.has(item.path)}
					{@const isRenaming = renameTarget?.path === item.path}
					{@const ItemIcon = getFileIcon(item)}

					<div
						class="file-item"
						class:selected={isSelected}
						class:renaming={isRenaming}
						onclick={(e) => handleItemClick(e, item)}
						ondblclick={() => handleNavigate(item)}
						oncontextmenu={(e) => handleContextMenu(e, item)}
						role="button"
						tabindex="0"
					>
						<ItemIcon class="file-icon {item.type === 'directory' ? 'folder' : 'file'}" />

						{#if isRenaming}
							<input
								type="text"
								class="rename-input"
								bind:value={renameValue}
								onkeydown={(e) => {
									if (e.key === 'Enter') confirmRename();
									if (e.key === 'Escape') cancelRename();
								}}
							/>
							<button type="button" class="inline-btn" onclick={confirmRename}>
								<Check class="w-4 h-4" />
							</button>
							<button type="button" class="inline-btn" onclick={cancelRename}>
								<X class="w-4 h-4" />
							</button>
						{:else}
							<span class="file-name">{item.name}</span>
							{#if viewMode === 'list'}
								<span class="file-size">{item.sizeFormatted || '-'}</span>
								<span class="file-date">{formatDate(item.modifiedAt)}</span>
							{/if}
							<button
								type="button"
								class="item-menu-btn"
								onclick={(e) => {
									e.stopPropagation();
									handleContextMenu(e, item);
								}}
							>
								<MoreVertical class="w-4 h-4" />
							</button>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Hidden file input -->
	<input
		type="file"
		multiple
		bind:this={uploadInput}
		onchange={handleFileUpload}
		class="hidden"
	/>

	<!-- Context menu -->
	{#if contextMenu}
		<div
			class="context-menu"
			style="left: {contextMenu.x}px; top: {contextMenu.y}px;"
			onclick={(e) => e.stopPropagation()}
		>
			{#if contextMenu.item.type === 'file'}
				<button type="button" onclick={() => handleDownload(contextMenu!.item)}>
					<Download class="w-4 h-4" />
					Download
				</button>
			{/if}
			<button type="button" onclick={() => handleRename(contextMenu!.item)}>
				<Edit3 class="w-4 h-4" />
				Rename
			</button>
			<button type="button" class="danger" onclick={() => handleDelete(contextMenu!.item)}>
				<Trash2 class="w-4 h-4" />
				Delete
			</button>
		</div>
	{/if}
</div>

<style>
	.files-view {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: var(--background);
	}

	/* Header */
	.files-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 1rem;
		border-bottom: 1px solid var(--border);
		background: hsl(var(--card));
		gap: 1rem;
		flex-shrink: 0;
	}

	.project-selector {
		flex: 1;
		min-width: 0;
	}

	.loading-projects,
	.no-projects {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.875rem;
		color: hsl(var(--muted-foreground));
	}

	.select-wrapper {
		position: relative;
		display: flex;
		align-items: center;
	}

	.project-select {
		width: 100%;
		padding: 0.5rem 2rem 0.5rem 0.75rem;
		font-size: 0.875rem;
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		background: hsl(var(--background));
		color: hsl(var(--foreground));
		cursor: pointer;
		appearance: none;
	}

	.project-select:focus {
		outline: none;
		border-color: hsl(var(--primary));
	}

	.select-icon {
		position: absolute;
		right: 0.5rem;
		width: 1rem;
		height: 1rem;
		pointer-events: none;
		color: hsl(var(--muted-foreground));
	}

	.header-actions {
		display: flex;
		gap: 0.25rem;
		flex-shrink: 0;
	}

	.icon-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 2rem;
		height: 2rem;
		border: none;
		border-radius: 0.5rem;
		background: transparent;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
		transition: all 0.15s;
	}

	.icon-btn:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}

	/* Breadcrumbs */
	.breadcrumbs {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		padding: 0.75rem 1rem;
		border-bottom: 1px solid var(--border);
		background: hsl(var(--card) / 0.5);
		overflow-x: auto;
		flex-shrink: 0;
	}

	.breadcrumb {
		padding: 0.25rem 0.5rem;
		font-size: 0.8125rem;
		color: hsl(var(--muted-foreground));
		background: transparent;
		border: none;
		border-radius: 0.25rem;
		cursor: pointer;
		white-space: nowrap;
		transition: all 0.15s;
	}

	.breadcrumb:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}

	.breadcrumb.current {
		color: hsl(var(--foreground));
		font-weight: 500;
	}

	.breadcrumb-sep {
		width: 0.875rem;
		height: 0.875rem;
		color: hsl(var(--muted-foreground) / 0.5);
		flex-shrink: 0;
	}

	/* Selection bar */
	.selection-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.5rem 1rem;
		background: hsl(var(--primary) / 0.1);
		border-bottom: 1px solid hsl(var(--primary) / 0.2);
		font-size: 0.875rem;
		flex-shrink: 0;
	}

	.selection-actions {
		display: flex;
		gap: 0.5rem;
	}

	.action-btn {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.375rem 0.75rem;
		font-size: 0.8125rem;
		border: 1px solid var(--border);
		border-radius: 0.375rem;
		background: hsl(var(--background));
		color: hsl(var(--foreground));
		cursor: pointer;
		transition: all 0.15s;
	}

	.action-btn:hover {
		background: hsl(var(--muted));
	}

	.action-btn.primary {
		background: hsl(var(--primary));
		color: hsl(var(--primary-foreground));
		border-color: hsl(var(--primary));
	}

	.action-btn.primary:hover {
		opacity: 0.9;
	}

	.action-btn.danger {
		color: hsl(var(--destructive));
		border-color: hsl(var(--destructive) / 0.3);
	}

	.action-btn.danger:hover {
		background: hsl(var(--destructive) / 0.1);
	}

	/* Upload progress */
	.upload-progress {
		padding: 0.75rem 1rem;
		background: hsl(var(--muted) / 0.3);
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.progress-bar {
		height: 4px;
		background: hsl(var(--muted));
		border-radius: 2px;
		overflow: hidden;
		margin-bottom: 0.5rem;
	}

	.progress-fill {
		height: 100%;
		background: hsl(var(--primary));
		transition: width 0.3s;
	}

	.progress-text {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
	}

	/* Error banner */
	.error-banner {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.75rem 1rem;
		background: hsl(var(--destructive) / 0.1);
		border-bottom: 1px solid hsl(var(--destructive) / 0.2);
		color: hsl(var(--destructive));
		font-size: 0.875rem;
		flex-shrink: 0;
	}

	.error-banner button {
		background: transparent;
		border: none;
		color: inherit;
		cursor: pointer;
		padding: 0.25rem;
	}

	/* Content */
	.files-content {
		flex: 1;
		overflow-y: auto;
		min-height: 0;
	}

	.empty-state,
	.loading-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		height: 100%;
		gap: 1rem;
		padding: 2rem;
		text-align: center;
	}

	.empty-actions {
		display: flex;
		gap: 0.75rem;
		margin-top: 0.5rem;
	}

	/* File list */
	.file-list {
		padding: 0.5rem;
	}

	.file-list.grid-view {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
		gap: 0.75rem;
		padding: 1rem;
	}

	.file-item {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		padding: 0.625rem 0.75rem;
		border-radius: 0.5rem;
		cursor: pointer;
		transition: background-color 0.15s;
		position: relative;
	}

	.file-item:hover {
		background: hsl(var(--muted) / 0.5);
	}

	.file-item.selected {
		background: hsl(var(--primary) / 0.15);
	}

	.grid-view .file-item {
		flex-direction: column;
		padding: 1rem 0.75rem;
		text-align: center;
		gap: 0.5rem;
	}

	.file-icon {
		width: 1.25rem;
		height: 1.25rem;
		flex-shrink: 0;
	}

	.file-icon.folder {
		color: hsl(var(--primary));
	}

	.file-icon.file {
		color: hsl(var(--muted-foreground));
	}

	.grid-view .file-icon {
		width: 2.5rem;
		height: 2.5rem;
	}

	.file-name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.875rem;
	}

	.grid-view .file-name {
		font-size: 0.8125rem;
		overflow: hidden;
		text-overflow: ellipsis;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		white-space: normal;
		word-break: break-word;
	}

	.file-size,
	.file-date {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		flex-shrink: 0;
		width: 80px;
		text-align: right;
	}

	.file-date {
		width: 100px;
	}

	.item-menu-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.5rem;
		height: 1.5rem;
		border: none;
		border-radius: 0.25rem;
		background: transparent;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
		opacity: 0;
		transition: all 0.15s;
	}

	.file-item:hover .item-menu-btn {
		opacity: 1;
	}

	.item-menu-btn:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}

	/* Rename input */
	.rename-input {
		flex: 1;
		padding: 0.25rem 0.5rem;
		font-size: 0.875rem;
		border: 1px solid hsl(var(--primary));
		border-radius: 0.25rem;
		background: hsl(var(--background));
		color: hsl(var(--foreground));
	}

	.rename-input:focus {
		outline: none;
	}

	.inline-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.5rem;
		height: 1.5rem;
		border: none;
		border-radius: 0.25rem;
		background: transparent;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
	}

	.inline-btn:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}

	/* Context menu */
	.context-menu {
		position: fixed;
		min-width: 160px;
		padding: 0.5rem;
		background: hsl(var(--popover));
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
		z-index: 100;
	}

	.context-menu button {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		width: 100%;
		padding: 0.5rem 0.75rem;
		font-size: 0.875rem;
		border: none;
		border-radius: 0.25rem;
		background: transparent;
		color: hsl(var(--foreground));
		cursor: pointer;
		text-align: left;
	}

	.context-menu button:hover {
		background: hsl(var(--muted));
	}

	.context-menu button.danger {
		color: hsl(var(--destructive));
	}

	.context-menu button.danger:hover {
		background: hsl(var(--destructive) / 0.1);
	}

	/* Hidden */
	.hidden {
		display: none;
	}

	/* Mobile */
	.mobile .files-header {
		flex-direction: column;
		align-items: stretch;
		gap: 0.75rem;
	}

	.mobile .header-actions {
		justify-content: flex-end;
	}

	.mobile .file-size,
	.mobile .file-date {
		display: none;
	}

	/* Animation */
	@keyframes spin {
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}

	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}
</style>
