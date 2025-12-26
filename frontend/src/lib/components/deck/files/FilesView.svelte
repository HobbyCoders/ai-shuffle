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
		RefreshCw,
		Eye,
		EyeOff,
		MoreVertical,
		Check,
		X,
		ChevronDown,
		FileText,
		FileCode,
		FileImage,
		FileVideo,
		FileAudio,
		FileArchive,
		FileSpreadsheet,
		Database,
		Settings,
		Search,
		Home,
		ArrowUpRight,
		Package
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
	let isDragging = $state(false);
	let searchQuery = $state('');
	let showProjectDropdown = $state(false);
	let itemsLoaded = $state(false);

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

	// Filtered files based on search
	let displayedFiles = $derived(
		searchQuery.trim()
			? $sortedFiles.filter((f) =>
					f.name.toLowerCase().includes(searchQuery.toLowerCase())
				)
			: $sortedFiles
	);

	// Get selected project name
	let selectedProject = $derived(projects.find((p) => p.id === selectedProjectId));

	// Trigger staggered animation when files load
	$effect(() => {
		if ($sortedFiles.length > 0 && !loading) {
			itemsLoaded = false;
			setTimeout(() => {
				itemsLoaded = true;
			}, 50);
		}
	});

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

		// Close context menu and dropdowns on click outside
		function handleClickOutside(e: MouseEvent) {
			if (contextMenu) {
				contextMenu = null;
			}
			// Check if click is outside project dropdown
			const target = e.target as HTMLElement;
			if (!target.closest('.project-selector-container')) {
				showProjectDropdown = false;
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
		showProjectDropdown = false;
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

	// File type detection and styling
	function getFileTypeInfo(item: FileItem): { icon: any; color: string; bgColor: string } {
		if (item.type === 'directory') {
			return { icon: Folder, color: 'var(--file-folder)', bgColor: 'var(--file-folder-bg)' };
		}

		const ext = item.extension?.toLowerCase() || '';

		// Code files
		if (['js', 'ts', 'jsx', 'tsx', 'py', 'rb', 'go', 'rs', 'java', 'c', 'cpp', 'h', 'cs', 'php', 'swift', 'kt', 'scala', 'vue', 'svelte'].includes(ext)) {
			return { icon: FileCode, color: 'var(--file-code)', bgColor: 'var(--file-code-bg)' };
		}

		// Documents
		if (['md', 'txt', 'doc', 'docx', 'pdf', 'rtf', 'odt'].includes(ext)) {
			return { icon: FileText, color: 'var(--file-doc)', bgColor: 'var(--file-doc-bg)' };
		}

		// Images
		if (['jpg', 'jpeg', 'png', 'gif', 'svg', 'webp', 'ico', 'bmp', 'tiff'].includes(ext)) {
			return { icon: FileImage, color: 'var(--file-image)', bgColor: 'var(--file-image-bg)' };
		}

		// Video
		if (['mp4', 'mov', 'avi', 'mkv', 'webm', 'flv'].includes(ext)) {
			return { icon: FileVideo, color: 'var(--file-video)', bgColor: 'var(--file-video-bg)' };
		}

		// Audio
		if (['mp3', 'wav', 'ogg', 'flac', 'aac', 'm4a'].includes(ext)) {
			return { icon: FileAudio, color: 'var(--file-audio)', bgColor: 'var(--file-audio-bg)' };
		}

		// Archives
		if (['zip', 'rar', '7z', 'tar', 'gz', 'bz2'].includes(ext)) {
			return { icon: FileArchive, color: 'var(--file-archive)', bgColor: 'var(--file-archive-bg)' };
		}

		// Data files
		if (['json', 'xml', 'yaml', 'yml', 'toml', 'csv', 'sql'].includes(ext)) {
			return { icon: Database, color: 'var(--file-data)', bgColor: 'var(--file-data-bg)' };
		}

		// Spreadsheets
		if (['xls', 'xlsx', 'ods'].includes(ext)) {
			return { icon: FileSpreadsheet, color: 'var(--file-spreadsheet)', bgColor: 'var(--file-spreadsheet-bg)' };
		}

		// Config files
		if (['env', 'ini', 'cfg', 'conf', 'config'].includes(ext) || item.name.startsWith('.')) {
			return { icon: Settings, color: 'var(--file-config)', bgColor: 'var(--file-config-bg)' };
		}

		// Package files
		if (['package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'Cargo.toml', 'go.mod', 'requirements.txt', 'Gemfile'].includes(item.name)) {
			return { icon: Package, color: 'var(--file-package)', bgColor: 'var(--file-package-bg)' };
		}

		return { icon: File, color: 'var(--file-default)', bgColor: 'var(--file-default-bg)' };
	}

	function formatDate(dateStr: string) {
		const date = new Date(dateStr);
		return date.toLocaleDateString(undefined, {
			year: 'numeric',
			month: 'short',
			day: 'numeric'
		});
	}

	function formatSize(size: number | null): string {
		if (size === null) return '-';
		if (size < 1024) return `${size} B`;
		if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
		if (size < 1024 * 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
		return `${(size / (1024 * 1024 * 1024)).toFixed(1)} GB`;
	}

	// Drag and drop handlers
	function handleDragEnter(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
		if (selectedProjectId) {
			isDragging = true;
		}
	}

	function handleDragLeave(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
		// Check if we're leaving the drop zone entirely
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		if (
			e.clientX < rect.left ||
			e.clientX > rect.right ||
			e.clientY < rect.top ||
			e.clientY > rect.bottom
		) {
			isDragging = false;
		}
	}

	function handleDragOver(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
	}

	async function handleDrop(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
		isDragging = false;

		if (!selectedProjectId || !e.dataTransfer?.files?.length) return;

		await files.uploadFiles(e.dataTransfer.files);
	}
</script>

<div
	class="files-view"
	class:mobile={isMobile}
	class:dragging={isDragging}
	ondragenter={handleDragEnter}
	ondragleave={handleDragLeave}
	ondragover={handleDragOver}
	ondrop={handleDrop}
>
	<!-- Drag overlay -->
	{#if isDragging}
		<div class="drag-overlay">
			<div class="drag-content">
				<Upload class="drag-icon" />
				<span class="drag-text">Drop files to upload</span>
				<span class="drag-subtext">Files will be uploaded to current folder</span>
			</div>
		</div>
	{/if}

	<!-- Header -->
	<header class="files-header">
		<div class="header-top">
			<!-- Project Selector -->
			<div class="project-selector-container">
				{#if loadingProjects}
					<div class="project-loading">
						<RefreshCw class="spin-icon" />
						<span>Loading...</span>
					</div>
				{:else if projects.length === 0}
					<div class="project-empty">
						<span>No projects</span>
					</div>
				{:else}
					<button
						class="project-trigger"
						onclick={(e) => {
							e.stopPropagation();
							showProjectDropdown = !showProjectDropdown;
						}}
					>
						<div class="project-icon">
							<Folder />
						</div>
						<div class="project-info">
							<span class="project-label">Project</span>
							<span class="project-name">{selectedProject?.name || 'Select project'}</span>
						</div>
						<ChevronDown class="chevron {showProjectDropdown ? 'open' : ''}" />
					</button>

					{#if showProjectDropdown}
						<div class="project-dropdown">
							<div class="dropdown-header">
								<span>Switch Project</span>
							</div>
							<div class="dropdown-list">
								{#each projects as project}
									<button
										class="dropdown-item {project.id === selectedProjectId ? 'active' : ''}"
										onclick={() => handleProjectSelect(project.id)}
									>
										<Folder class="item-icon" />
										<span class="item-name">{project.name}</span>
										{#if project.id === selectedProjectId}
											<Check class="check-icon" />
										{/if}
									</button>
								{/each}
							</div>
						</div>
					{/if}
				{/if}
			</div>

			<!-- Actions -->
			{#if selectedProjectId}
				<div class="header-actions">
					<button
						class="action-btn primary-action"
						onclick={handleUploadClick}
						title="Upload files"
					>
						<Upload />
						<span class="action-label">Upload</span>
					</button>
					<button
						class="action-btn"
						onclick={handleNewFolder}
						title="New folder"
					>
						<FolderPlus />
					</button>
					<div class="action-divider"></div>
					<button
						class="action-btn"
						onclick={() => files.toggleShowHidden()}
						title={showHidden ? 'Hide hidden files' : 'Show hidden files'}
					>
						{#if showHidden}
							<Eye />
						{:else}
							<EyeOff />
						{/if}
					</button>
					<button
						class="action-btn {viewMode === 'grid' ? 'active' : ''}"
						onclick={() => files.setViewMode('grid')}
						title="Grid view"
					>
						<LayoutGrid />
					</button>
					<button
						class="action-btn {viewMode === 'list' ? 'active' : ''}"
						onclick={() => files.setViewMode('list')}
						title="List view"
					>
						<LayoutList />
					</button>
					<button
						class="action-btn"
						onclick={() => files.loadFiles()}
						title="Refresh"
					>
						<RefreshCw class={loading ? 'spin-icon' : ''} />
					</button>
				</div>
			{/if}
		</div>

		<!-- Path Navigator -->
		{#if selectedProjectId}
			<nav class="path-nav">
				<div class="path-track">
					{#each $breadcrumbs as crumb, i}
						<button
							class="path-segment {i === $breadcrumbs.length - 1 ? 'current' : ''}"
							onclick={() => handleBreadcrumbClick(crumb.path)}
						>
							{#if i === 0}
								<Home class="home-icon" />
							{/if}
							<span>{crumb.name}</span>
						</button>
						{#if i < $breadcrumbs.length - 1}
							<ChevronRight class="path-sep" />
						{/if}
					{/each}
				</div>

				<!-- Search -->
				<div class="path-search">
					<Search class="search-icon" />
					<input
						type="text"
						placeholder="Filter files..."
						bind:value={searchQuery}
						class="search-input"
					/>
					{#if searchQuery}
						<button class="search-clear" onclick={() => (searchQuery = '')}>
							<X />
						</button>
					{/if}
				</div>
			</nav>
		{/if}
	</header>

	<!-- Selection bar -->
	{#if $hasSelection}
		<div class="selection-bar">
			<div class="selection-info">
				<span class="selection-count">{$selectionCount}</span>
				<span>selected</span>
			</div>
			<div class="selection-actions">
				<button class="sel-action danger" onclick={handleDeleteSelected}>
					<Trash2 />
					<span>Delete</span>
				</button>
				<button class="sel-action" onclick={() => files.clearSelection()}>
					<X />
					<span>Clear</span>
				</button>
			</div>
		</div>
	{/if}

	<!-- Upload progress -->
	{#if uploading}
		<div class="upload-bar">
			<div class="upload-track">
				<div class="upload-fill" style="width: {uploadProgress}%"></div>
			</div>
			<span class="upload-text">Uploading... {uploadProgress}%</span>
		</div>
	{/if}

	<!-- Error display -->
	{#if error}
		<div class="error-bar">
			<span>{error}</span>
			<button onclick={() => files.clearError()}>
				<X />
			</button>
		</div>
	{/if}

	<!-- Main content -->
	<main class="files-content">
		{#if !selectedProjectId}
			<div class="empty-state">
				<div class="empty-icon-wrap">
					<Folder class="empty-icon" />
				</div>
				<h3 class="empty-title">No Project Selected</h3>
				<p class="empty-desc">Choose a project from the dropdown above to browse its files</p>
			</div>
		{:else if loading && $sortedFiles.length === 0}
			<div class="loading-state">
				<div class="loader">
					<RefreshCw class="spin-icon large" />
				</div>
				<p>Loading files...</p>
			</div>
		{:else if displayedFiles.length === 0 && !showNewFolderInput}
			<div class="empty-state">
				<div class="empty-icon-wrap">
					{#if searchQuery}
						<Search class="empty-icon" />
					{:else}
						<Folder class="empty-icon" />
					{/if}
				</div>
				<h3 class="empty-title">
					{searchQuery ? 'No matches found' : 'This folder is empty'}
				</h3>
				<p class="empty-desc">
					{searchQuery
						? `No files matching "${searchQuery}"`
						: 'Drop files here or use the upload button'}
				</p>
				{#if !searchQuery}
					<div class="empty-actions">
						<button class="empty-btn primary" onclick={handleUploadClick}>
							<Upload />
							Upload files
						</button>
						<button class="empty-btn" onclick={handleNewFolder}>
							<FolderPlus />
							New folder
						</button>
					</div>
				{/if}
			</div>
		{:else}
			<!-- File list/grid -->
			<div class="file-grid {viewMode}" class:loaded={itemsLoaded}>
				<!-- New folder input -->
				{#if showNewFolderInput}
					<div class="file-card new-folder">
						<div class="file-icon-wrap" style="--icon-color: var(--file-folder); --icon-bg: var(--file-folder-bg);">
							<Folder />
						</div>
						<input
							type="text"
							class="folder-input"
							bind:value={newFolderName}
							placeholder="Folder name"
							onkeydown={(e) => {
								if (e.key === 'Enter') confirmNewFolder();
								if (e.key === 'Escape') cancelNewFolder();
							}}
						/>
						<div class="folder-actions">
							<button class="folder-btn confirm" onclick={confirmNewFolder}>
								<Check />
							</button>
							<button class="folder-btn cancel" onclick={cancelNewFolder}>
								<X />
							</button>
						</div>
					</div>
				{/if}

				<!-- File items -->
				{#each displayedFiles as item, index}
					{@const isSelected = selectedFiles.has(item.path)}
					{@const isRenaming = renameTarget?.path === item.path}
					{@const typeInfo = getFileTypeInfo(item)}
					{@const ItemIcon = typeInfo.icon}

					<div
						class="file-card"
						class:selected={isSelected}
						class:renaming={isRenaming}
						style="--anim-delay: {Math.min(index * 30, 300)}ms;"
						onclick={(e) => handleItemClick(e, item)}
						ondblclick={() => handleNavigate(item)}
						oncontextmenu={(e) => handleContextMenu(e, item)}
						role="button"
						tabindex="0"
					>
						<div class="file-icon-wrap" style="--icon-color: {typeInfo.color}; --icon-bg: {typeInfo.bgColor};">
							<ItemIcon />
						</div>

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
							<div class="rename-actions">
								<button class="folder-btn confirm" onclick={confirmRename}>
									<Check />
								</button>
								<button class="folder-btn cancel" onclick={cancelRename}>
									<X />
								</button>
							</div>
						{:else}
							<div class="file-info">
								<span class="file-name" title={item.name}>{item.name}</span>
								{#if viewMode === 'list'}
									<span class="file-meta">{formatSize(item.size)}</span>
									<span class="file-meta date">{formatDate(item.modifiedAt)}</span>
								{/if}
							</div>
							<button
								class="file-menu"
								onclick={(e) => {
									e.stopPropagation();
									handleContextMenu(e, item);
								}}
							>
								<MoreVertical />
							</button>
						{/if}

						{#if isSelected}
							<div class="selected-indicator">
								<Check />
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	</main>

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
				<button class="ctx-btn" onclick={() => handleDownload(contextMenu!.item)}>
					<Download />
					<span>Download</span>
					<ArrowUpRight class="external" />
				</button>
			{/if}
			<button class="ctx-btn" onclick={() => handleRename(contextMenu!.item)}>
				<Edit3 />
				<span>Rename</span>
			</button>
			<div class="ctx-divider"></div>
			<button class="ctx-btn danger" onclick={() => handleDelete(contextMenu!.item)}>
				<Trash2 />
				<span>Delete</span>
			</button>
		</div>
	{/if}
</div>

<style>
	/* ============================================
	   CSS Custom Properties - File Type Colors
	   ============================================ */
	.files-view {
		--file-folder: oklch(0.75 0.15 85);
		--file-folder-bg: oklch(0.75 0.15 85 / 0.15);
		--file-code: oklch(0.70 0.18 280);
		--file-code-bg: oklch(0.70 0.18 280 / 0.15);
		--file-doc: oklch(0.65 0.15 240);
		--file-doc-bg: oklch(0.65 0.15 240 / 0.15);
		--file-image: oklch(0.68 0.18 330);
		--file-image-bg: oklch(0.68 0.18 330 / 0.15);
		--file-video: oklch(0.65 0.20 25);
		--file-video-bg: oklch(0.65 0.20 25 / 0.15);
		--file-audio: oklch(0.70 0.16 160);
		--file-audio-bg: oklch(0.70 0.16 160 / 0.15);
		--file-archive: oklch(0.60 0.12 50);
		--file-archive-bg: oklch(0.60 0.12 50 / 0.15);
		--file-data: oklch(0.72 0.14 180);
		--file-data-bg: oklch(0.72 0.14 180 / 0.15);
		--file-spreadsheet: oklch(0.65 0.18 145);
		--file-spreadsheet-bg: oklch(0.65 0.18 145 / 0.15);
		--file-config: oklch(0.55 0.08 260);
		--file-config-bg: oklch(0.55 0.08 260 / 0.15);
		--file-package: oklch(0.68 0.16 20);
		--file-package-bg: oklch(0.68 0.16 20 / 0.15);
		--file-default: oklch(0.60 0.02 260);
		--file-default-bg: oklch(0.60 0.02 260 / 0.12);
	}

	/* ============================================
	   Layout
	   ============================================ */
	.files-view {
		display: flex;
		flex-direction: column;
		height: 100%;
		background: var(--background);
		position: relative;
		overflow: hidden;
	}

	.files-view.dragging {
		pointer-events: none;
	}

	.files-view.dragging .drag-overlay {
		pointer-events: auto;
	}

	/* ============================================
	   Drag Overlay
	   ============================================ */
	.drag-overlay {
		position: absolute;
		inset: 0;
		z-index: 50;
		background: oklch(0.14 0.008 260 / 0.95);
		backdrop-filter: blur(8px);
		display: flex;
		align-items: center;
		justify-content: center;
		animation: fadeIn 0.2s ease;
	}

	.drag-content {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 1rem;
		padding: 3rem;
		border: 2px dashed var(--primary);
		border-radius: 1.5rem;
		background: oklch(0.72 0.14 180 / 0.08);
		animation: pulse 2s ease-in-out infinite;
	}

	.drag-icon {
		width: 3rem;
		height: 3rem;
		color: var(--primary);
	}

	.drag-text {
		font-size: 1.25rem;
		font-weight: 600;
		color: hsl(var(--foreground));
	}

	.drag-subtext {
		font-size: 0.875rem;
		color: hsl(var(--muted-foreground));
	}

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.7; }
	}

	@keyframes fadeIn {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	/* ============================================
	   Header
	   ============================================ */
	.files-header {
		flex-shrink: 0;
		background: hsl(var(--card));
		border-bottom: 1px solid var(--border);
	}

	.header-top {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.875rem 1.25rem;
		gap: 1rem;
	}

	/* Project Selector */
	.project-selector-container {
		position: relative;
		flex: 1;
		max-width: 320px;
	}

	.project-loading,
	.project-empty {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.625rem 1rem;
		font-size: 0.875rem;
		color: hsl(var(--muted-foreground));
		background: hsl(var(--muted) / 0.3);
		border-radius: 0.75rem;
	}

	.project-trigger {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		width: 100%;
		padding: 0.5rem 0.75rem;
		background: hsl(var(--muted) / 0.4);
		border: 1px solid transparent;
		border-radius: 0.75rem;
		cursor: pointer;
		transition: all 0.2s ease;
	}

	.project-trigger:hover {
		background: hsl(var(--muted) / 0.6);
		border-color: var(--border);
	}

	.project-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 2.25rem;
		height: 2.25rem;
		background: var(--file-folder-bg);
		border-radius: 0.5rem;
		color: var(--file-folder);
	}

	.project-icon :global(svg) {
		width: 1.125rem;
		height: 1.125rem;
	}

	.project-info {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.125rem;
	}

	.project-label {
		font-size: 0.6875rem;
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: hsl(var(--muted-foreground));
	}

	.project-name {
		font-size: 0.9375rem;
		font-weight: 600;
		color: hsl(var(--foreground));
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 100%;
	}

	.chevron {
		width: 1rem;
		height: 1rem;
		color: hsl(var(--muted-foreground));
		transition: transform 0.2s ease;
	}

	.chevron.open {
		transform: rotate(180deg);
	}

	/* Project Dropdown */
	.project-dropdown {
		position: absolute;
		top: calc(100% + 0.5rem);
		left: 0;
		right: 0;
		background: hsl(var(--popover));
		border: 1px solid var(--border);
		border-radius: 0.75rem;
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.25);
		z-index: 100;
		overflow: hidden;
		animation: slideDown 0.15s ease;
	}

	@keyframes slideDown {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.dropdown-header {
		padding: 0.75rem 1rem;
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: hsl(var(--muted-foreground));
		border-bottom: 1px solid var(--border);
	}

	.dropdown-list {
		max-height: 280px;
		overflow-y: auto;
		padding: 0.375rem;
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		gap: 0.625rem;
		width: 100%;
		padding: 0.625rem 0.75rem;
		background: transparent;
		border: none;
		border-radius: 0.5rem;
		cursor: pointer;
		text-align: left;
		transition: background 0.15s ease;
	}

	.dropdown-item:hover {
		background: hsl(var(--muted));
	}

	.dropdown-item.active {
		background: hsl(var(--primary) / 0.15);
	}

	.dropdown-item :global(.item-icon) {
		width: 1.125rem;
		height: 1.125rem;
		color: var(--file-folder);
	}

	.dropdown-item .item-name {
		flex: 1;
		font-size: 0.875rem;
		color: hsl(var(--foreground));
	}

	.dropdown-item :global(.check-icon) {
		width: 1rem;
		height: 1rem;
		color: var(--primary);
	}

	/* Header Actions */
	.header-actions {
		display: flex;
		align-items: center;
		gap: 0.25rem;
	}

	.action-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 0.375rem;
		height: 2.25rem;
		padding: 0 0.625rem;
		background: transparent;
		border: none;
		border-radius: 0.5rem;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.action-btn :global(svg) {
		width: 1.125rem;
		height: 1.125rem;
	}

	.action-btn:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}

	.action-btn.active {
		background: hsl(var(--primary) / 0.15);
		color: var(--primary);
	}

	.action-btn.primary-action {
		background: var(--primary);
		color: hsl(var(--primary-foreground));
		padding: 0 0.875rem;
	}

	.action-btn.primary-action:hover {
		filter: brightness(1.1);
	}

	.action-label {
		font-size: 0.8125rem;
		font-weight: 500;
	}

	.action-divider {
		width: 1px;
		height: 1.5rem;
		background: var(--border);
		margin: 0 0.375rem;
	}

	/* Path Navigator */
	.path-nav {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 0.625rem 1.25rem;
		background: hsl(var(--muted) / 0.25);
		border-top: 1px solid var(--border);
	}

	.path-track {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		overflow-x: auto;
		flex: 1;
		min-width: 0;
		-ms-overflow-style: none;
		scrollbar-width: none;
	}

	.path-track::-webkit-scrollbar {
		display: none;
	}

	.path-segment {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.375rem 0.625rem;
		background: transparent;
		border: none;
		border-radius: 0.375rem;
		font-size: 0.8125rem;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
		white-space: nowrap;
		transition: all 0.15s ease;
	}

	.path-segment:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}

	.path-segment.current {
		color: hsl(var(--foreground));
		font-weight: 600;
	}

	.home-icon {
		width: 0.875rem;
		height: 0.875rem;
	}

	.path-sep {
		width: 0.875rem;
		height: 0.875rem;
		color: hsl(var(--muted-foreground) / 0.5);
		flex-shrink: 0;
	}

	/* Path Search */
	.path-search {
		position: relative;
		display: flex;
		align-items: center;
		width: 200px;
		flex-shrink: 0;
	}

	.search-icon {
		position: absolute;
		left: 0.625rem;
		width: 0.875rem;
		height: 0.875rem;
		color: hsl(var(--muted-foreground));
		pointer-events: none;
	}

	.search-input {
		width: 100%;
		height: 2rem;
		padding: 0 2rem 0 2rem;
		background: hsl(var(--muted) / 0.5);
		border: 1px solid transparent;
		border-radius: 0.5rem;
		font-size: 0.8125rem;
		color: hsl(var(--foreground));
		transition: all 0.15s ease;
	}

	.search-input::placeholder {
		color: hsl(var(--muted-foreground));
	}

	.search-input:focus {
		outline: none;
		background: hsl(var(--background));
		border-color: var(--border);
	}

	.search-clear {
		position: absolute;
		right: 0.375rem;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.25rem;
		height: 1.25rem;
		background: transparent;
		border: none;
		border-radius: 0.25rem;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
	}

	.search-clear :global(svg) {
		width: 0.75rem;
		height: 0.75rem;
	}

	.search-clear:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}

	/* ============================================
	   Selection Bar
	   ============================================ */
	.selection-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.625rem 1.25rem;
		background: linear-gradient(135deg, oklch(0.72 0.14 180 / 0.12), oklch(0.72 0.14 180 / 0.08));
		border-bottom: 1px solid oklch(0.72 0.14 180 / 0.2);
		flex-shrink: 0;
	}

	.selection-info {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.875rem;
		color: hsl(var(--foreground));
	}

	.selection-count {
		display: flex;
		align-items: center;
		justify-content: center;
		min-width: 1.5rem;
		height: 1.5rem;
		padding: 0 0.5rem;
		background: var(--primary);
		color: hsl(var(--primary-foreground));
		border-radius: 1rem;
		font-size: 0.75rem;
		font-weight: 600;
	}

	.selection-actions {
		display: flex;
		gap: 0.5rem;
	}

	.sel-action {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		padding: 0.375rem 0.75rem;
		background: hsl(var(--background));
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		font-size: 0.8125rem;
		color: hsl(var(--foreground));
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.sel-action :global(svg) {
		width: 0.875rem;
		height: 0.875rem;
	}

	.sel-action:hover {
		background: hsl(var(--muted));
	}

	.sel-action.danger {
		color: hsl(var(--destructive));
		border-color: hsl(var(--destructive) / 0.3);
	}

	.sel-action.danger:hover {
		background: hsl(var(--destructive) / 0.1);
	}

	/* ============================================
	   Upload Bar
	   ============================================ */
	.upload-bar {
		padding: 0.75rem 1.25rem;
		background: hsl(var(--muted) / 0.3);
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	.upload-track {
		height: 4px;
		background: hsl(var(--muted));
		border-radius: 2px;
		overflow: hidden;
		margin-bottom: 0.5rem;
	}

	.upload-fill {
		height: 100%;
		background: linear-gradient(90deg, var(--primary), oklch(0.72 0.14 180));
		border-radius: 2px;
		transition: width 0.3s ease;
	}

	.upload-text {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
	}

	/* ============================================
	   Error Bar
	   ============================================ */
	.error-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 0.75rem 1.25rem;
		background: hsl(var(--destructive) / 0.1);
		border-bottom: 1px solid hsl(var(--destructive) / 0.2);
		color: hsl(var(--destructive));
		font-size: 0.875rem;
		flex-shrink: 0;
	}

	.error-bar button {
		display: flex;
		padding: 0.25rem;
		background: transparent;
		border: none;
		color: inherit;
		cursor: pointer;
		border-radius: 0.25rem;
	}

	.error-bar button:hover {
		background: hsl(var(--destructive) / 0.15);
	}

	.error-bar button :global(svg) {
		width: 1rem;
		height: 1rem;
	}

	/* ============================================
	   Main Content
	   ============================================ */
	.files-content {
		flex: 1;
		overflow-y: auto;
		overflow-x: hidden;
		min-height: 0;
	}

	/* Empty States */
	.empty-state,
	.loading-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		height: 100%;
		gap: 1rem;
		padding: 3rem;
		text-align: center;
	}

	.empty-icon-wrap {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 5rem;
		height: 5rem;
		background: hsl(var(--muted) / 0.5);
		border-radius: 1.25rem;
		margin-bottom: 0.5rem;
	}

	.empty-icon {
		width: 2.5rem;
		height: 2.5rem;
		color: hsl(var(--muted-foreground) / 0.6);
	}

	.empty-title {
		font-size: 1.125rem;
		font-weight: 600;
		color: hsl(var(--foreground));
		margin: 0;
	}

	.empty-desc {
		font-size: 0.875rem;
		color: hsl(var(--muted-foreground));
		max-width: 280px;
		margin: 0;
	}

	.empty-actions {
		display: flex;
		gap: 0.75rem;
		margin-top: 1rem;
	}

	.empty-btn {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.625rem 1rem;
		background: hsl(var(--muted));
		border: 1px solid var(--border);
		border-radius: 0.625rem;
		font-size: 0.875rem;
		font-weight: 500;
		color: hsl(var(--foreground));
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.empty-btn :global(svg) {
		width: 1rem;
		height: 1rem;
	}

	.empty-btn:hover {
		background: hsl(var(--muted) / 0.8);
		border-color: hsl(var(--muted-foreground) / 0.3);
	}

	.empty-btn.primary {
		background: var(--primary);
		color: hsl(var(--primary-foreground));
		border-color: var(--primary);
	}

	.empty-btn.primary:hover {
		filter: brightness(1.1);
	}

	.loader {
		margin-bottom: 0.5rem;
	}

	.loading-state p {
		font-size: 0.875rem;
		color: hsl(var(--muted-foreground));
		margin: 0;
	}

	/* ============================================
	   File Grid/List
	   ============================================ */
	.file-grid {
		padding: 1rem;
	}

	.file-grid.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
		gap: 0.75rem;
	}

	.file-grid.list {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	/* File Card */
	.file-card {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		padding: 0.625rem 0.75rem;
		background: transparent;
		border: 1px solid transparent;
		border-radius: 0.625rem;
		cursor: pointer;
		position: relative;
		transition: all 0.15s ease;
		opacity: 0;
		transform: translateY(8px);
	}

	.file-grid.loaded .file-card {
		animation: fadeInUp 0.25s ease forwards;
		animation-delay: var(--anim-delay, 0ms);
	}

	@keyframes fadeInUp {
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.file-card:hover {
		background: hsl(var(--muted) / 0.5);
		border-color: var(--border);
	}

	.file-card.selected {
		background: hsl(var(--primary) / 0.1);
		border-color: hsl(var(--primary) / 0.3);
	}

	.file-card.new-folder {
		background: hsl(var(--muted) / 0.3);
		border: 1px dashed var(--border);
		opacity: 1;
		transform: none;
	}

	/* Grid view card */
	.file-grid.grid .file-card {
		flex-direction: column;
		padding: 1rem 0.75rem;
		text-align: center;
		gap: 0.625rem;
		min-height: 120px;
	}

	.file-grid.grid .file-card:hover {
		transform: translateY(-2px);
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
	}

	/* File Icon */
	.file-icon-wrap {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 2.25rem;
		height: 2.25rem;
		background: var(--icon-bg);
		border-radius: 0.5rem;
		flex-shrink: 0;
		transition: transform 0.15s ease;
	}

	.file-icon-wrap :global(svg) {
		width: 1.25rem;
		height: 1.25rem;
		color: var(--icon-color);
	}

	.file-grid.grid .file-icon-wrap {
		width: 3rem;
		height: 3rem;
		border-radius: 0.75rem;
	}

	.file-grid.grid .file-icon-wrap :global(svg) {
		width: 1.5rem;
		height: 1.5rem;
	}

	.file-card:hover .file-icon-wrap {
		transform: scale(1.05);
	}

	/* File Info */
	.file-info {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 1rem;
	}

	.file-grid.grid .file-info {
		flex-direction: column;
		gap: 0.25rem;
	}

	.file-name {
		flex: 1;
		min-width: 0;
		font-size: 0.875rem;
		font-weight: 500;
		color: hsl(var(--foreground));
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.file-grid.grid .file-name {
		font-size: 0.8125rem;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
		white-space: normal;
		word-break: break-word;
		text-align: center;
	}

	.file-meta {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		white-space: nowrap;
		width: 80px;
		text-align: right;
		flex-shrink: 0;
	}

	.file-meta.date {
		width: 100px;
	}

	/* File Menu Button */
	.file-menu {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.75rem;
		height: 1.75rem;
		background: transparent;
		border: none;
		border-radius: 0.375rem;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
		opacity: 0;
		transition: all 0.15s ease;
	}

	.file-menu :global(svg) {
		width: 1rem;
		height: 1rem;
	}

	.file-card:hover .file-menu {
		opacity: 1;
	}

	.file-menu:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}

	.file-grid.grid .file-menu {
		position: absolute;
		top: 0.5rem;
		right: 0.5rem;
	}

	/* Selected Indicator */
	.selected-indicator {
		position: absolute;
		top: 0.375rem;
		left: 0.375rem;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.25rem;
		height: 1.25rem;
		background: var(--primary);
		border-radius: 50%;
		color: hsl(var(--primary-foreground));
	}

	.selected-indicator :global(svg) {
		width: 0.75rem;
		height: 0.75rem;
	}

	.file-grid.list .selected-indicator {
		position: static;
		margin-left: auto;
	}

	/* Folder Input */
	.folder-input,
	.rename-input {
		flex: 1;
		min-width: 0;
		height: 2rem;
		padding: 0 0.625rem;
		background: hsl(var(--background));
		border: 1px solid var(--primary);
		border-radius: 0.375rem;
		font-size: 0.875rem;
		color: hsl(var(--foreground));
	}

	.folder-input:focus,
	.rename-input:focus {
		outline: none;
		box-shadow: 0 0 0 2px hsl(var(--primary) / 0.2);
	}

	.folder-actions,
	.rename-actions {
		display: flex;
		gap: 0.25rem;
	}

	.folder-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.75rem;
		height: 1.75rem;
		background: transparent;
		border: none;
		border-radius: 0.375rem;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.folder-btn :global(svg) {
		width: 1rem;
		height: 1rem;
	}

	.folder-btn.confirm {
		color: hsl(var(--success));
	}

	.folder-btn.confirm:hover {
		background: hsl(var(--success) / 0.15);
	}

	.folder-btn.cancel {
		color: hsl(var(--muted-foreground));
	}

	.folder-btn.cancel:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}

	/* ============================================
	   Context Menu
	   ============================================ */
	.context-menu {
		position: fixed;
		min-width: 180px;
		background: hsl(var(--popover));
		border: 1px solid var(--border);
		border-radius: 0.75rem;
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.25);
		z-index: 100;
		overflow: hidden;
		padding: 0.375rem;
		animation: contextIn 0.12s ease;
	}

	@keyframes contextIn {
		from {
			opacity: 0;
			transform: scale(0.95);
		}
		to {
			opacity: 1;
			transform: scale(1);
		}
	}

	.ctx-btn {
		display: flex;
		align-items: center;
		gap: 0.625rem;
		width: 100%;
		padding: 0.625rem 0.75rem;
		background: transparent;
		border: none;
		border-radius: 0.5rem;
		font-size: 0.875rem;
		color: hsl(var(--foreground));
		cursor: pointer;
		text-align: left;
		transition: background 0.15s ease;
	}

	.ctx-btn:hover {
		background: hsl(var(--muted));
	}

	.ctx-btn :global(svg) {
		width: 1rem;
		height: 1rem;
		flex-shrink: 0;
	}

	.ctx-btn span {
		flex: 1;
	}

	.ctx-btn :global(.external) {
		width: 0.75rem;
		height: 0.75rem;
		color: hsl(var(--muted-foreground));
	}

	.ctx-btn.danger {
		color: hsl(var(--destructive));
	}

	.ctx-btn.danger:hover {
		background: hsl(var(--destructive) / 0.1);
	}

	.ctx-divider {
		height: 1px;
		background: var(--border);
		margin: 0.375rem 0;
	}

	/* ============================================
	   Utilities
	   ============================================ */
	.hidden {
		display: none;
	}

	.spin-icon {
		animation: spin 1s linear infinite;
	}

	.spin-icon.large {
		width: 2rem;
		height: 2rem;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	/* ============================================
	   Mobile Styles
	   ============================================ */
	.mobile .header-top {
		flex-direction: column;
		align-items: stretch;
		gap: 0.75rem;
	}

	.mobile .project-selector-container {
		max-width: none;
	}

	.mobile .header-actions {
		justify-content: flex-end;
	}

	.mobile .action-label {
		display: none;
	}

	.mobile .path-nav {
		flex-direction: column;
		align-items: stretch;
		gap: 0.625rem;
	}

	.mobile .path-search {
		width: 100%;
	}

	.mobile .file-meta {
		display: none;
	}

	.mobile .selection-bar {
		flex-direction: column;
		gap: 0.75rem;
		align-items: stretch;
	}

	.mobile .selection-actions {
		justify-content: flex-end;
	}
</style>
