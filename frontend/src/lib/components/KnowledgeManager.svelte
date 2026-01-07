<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { KnowledgeDocumentSummary, KnowledgeStats, KnowledgeSearchResult } from '$lib/api/client';
	import {
		getKnowledgeDocuments,
		getKnowledgeStats,
		uploadKnowledgeDocument,
		deleteKnowledgeDocument,
		searchKnowledge,
		getKnowledgeDocumentPreview
	} from '$lib/api/client';

	export let projectId: string;

	const dispatch = createEventDispatcher<{
		close: void;
	}>();

	// Supported file extensions
	const SUPPORTED_EXTENSIONS = ['.txt', '.md', '.markdown', '.json', '.yaml', '.yml', '.py', '.js', '.ts', '.html', '.css', '.xml', '.csv', '.log'];

	let documents: KnowledgeDocumentSummary[] = [];
	let stats: KnowledgeStats = { document_count: 0, total_size: 0, total_chunks: 0 };
	let loading = true;
	let error = '';
	let uploading = false;

	// Preview modal state
	let previewDocument: { filename: string; preview: string; truncated: boolean } | null = null;

	// Search state
	let searchQuery = '';
	let searchResults: KnowledgeSearchResult[] = [];
	let searching = false;
	let showSearchResults = false;

	// Drag and drop state
	let isDragging = false;

	// Delete confirmation state
	let confirmDelete: string | null = null;

	// Load documents and stats
	async function loadDocuments() {
		loading = true;
		error = '';
		try {
			[documents, stats] = await Promise.all([
				getKnowledgeDocuments(projectId),
				getKnowledgeStats(projectId)
			]);
		} catch (err: any) {
			error = err.detail || 'Failed to load knowledge base';
		} finally {
			loading = false;
		}
	}

	// Handle file upload
	async function handleUpload(files: FileList | null) {
		if (!files || files.length === 0) return;

		uploading = true;
		error = '';

		for (const file of files) {
			// Check file extension
			const ext = '.' + file.name.split('.').pop()?.toLowerCase();
			if (!SUPPORTED_EXTENSIONS.includes(ext)) {
				error = `Unsupported file type: ${ext}. Supported: ${SUPPORTED_EXTENSIONS.join(', ')}`;
				continue;
			}

			// Check file size (5MB limit)
			if (file.size > 5 * 1024 * 1024) {
				error = `File too large: ${file.name}. Maximum size is 5MB.`;
				continue;
			}

			try {
				await uploadKnowledgeDocument(projectId, file);
			} catch (err: any) {
				error = err.detail || `Failed to upload ${file.name}`;
			}
		}

		uploading = false;
		await loadDocuments();
	}

	// Handle drag and drop
	function handleDragOver(e: DragEvent) {
		e.preventDefault();
		isDragging = true;
	}

	function handleDragLeave(e: DragEvent) {
		e.preventDefault();
		isDragging = false;
	}

	async function handleDrop(e: DragEvent) {
		e.preventDefault();
		isDragging = false;
		const files = e.dataTransfer?.files;
		if (files) {
			await handleUpload(files);
		}
	}

	// Handle file input change
	async function handleFileInputChange(e: Event) {
		const input = e.target as HTMLInputElement;
		await handleUpload(input.files);
		input.value = ''; // Reset for re-uploading same file
	}

	// Delete document
	async function handleDelete(docId: string) {
		try {
			await deleteKnowledgeDocument(projectId, docId);
			confirmDelete = null;
			await loadDocuments();
		} catch (err: any) {
			error = err.detail || 'Failed to delete document';
		}
	}

	// Show document preview
	async function showPreview(doc: KnowledgeDocumentSummary) {
		try {
			const preview = await getKnowledgeDocumentPreview(projectId, doc.id, 2000);
			previewDocument = {
				filename: preview.filename,
				preview: preview.preview,
				truncated: preview.truncated
			};
		} catch (err: any) {
			error = err.detail || 'Failed to load preview';
		}
	}

	// Search knowledge base
	async function handleSearch() {
		if (!searchQuery.trim()) {
			searchResults = [];
			showSearchResults = false;
			return;
		}

		searching = true;
		try {
			searchResults = await searchKnowledge(projectId, searchQuery, 10);
			showSearchResults = true;
		} catch (err: any) {
			error = err.detail || 'Search failed';
		} finally {
			searching = false;
		}
	}

	// Format file size
	function formatSize(bytes: number): string {
		if (bytes < 1024) return bytes + ' B';
		if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
		return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
	}

	// Get file icon based on content type
	function getFileIcon(contentType: string): string {
		if (contentType.includes('markdown')) return 'M';
		if (contentType.includes('json')) return '{}';
		if (contentType.includes('yaml')) return 'Y';
		if (contentType.includes('python')) return 'Py';
		if (contentType.includes('javascript')) return 'JS';
		if (contentType.includes('typescript')) return 'TS';
		if (contentType.includes('html')) return '<>';
		if (contentType.includes('css')) return '#';
		if (contentType.includes('xml')) return 'XML';
		if (contentType.includes('csv')) return 'CSV';
		return 'TXT';
	}

	// Initialize
	loadDocuments();
</script>

<div class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm">
	<div class="bg-background border border-border rounded-2xl shadow-2xl w-full max-w-2xl max-h-[85vh] max-sm:max-h-full flex flex-col">
		<!-- Header -->
		<div class="flex items-center justify-between px-5 py-4 border-b border-border">
			<div>
				<h2 class="text-lg font-semibold text-foreground">Knowledge Base</h2>
				<p class="text-sm text-muted-foreground mt-0.5">
					{stats.document_count} documents, {formatSize(stats.total_size)}, {stats.total_chunks} chunks
				</p>
			</div>
			<button
				on:click={() => dispatch('close')}
				class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-hover-overlay transition-colors"
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		</div>

		<!-- Content -->
		<div class="flex-1 overflow-y-auto p-5 space-y-5">
			{#if error}
				<div class="p-3 rounded-lg bg-destructive/10 text-destructive text-sm">
					{error}
					<button on:click={() => error = ''} class="ml-2 underline">Dismiss</button>
				</div>
			{/if}

			<!-- Upload area -->
			<div
				class="relative border-2 border-dashed rounded-xl p-6 text-center transition-colors
					{isDragging ? 'border-primary bg-primary/5' : 'border-border hover:border-muted-foreground'}"
				on:dragover={handleDragOver}
				on:dragleave={handleDragLeave}
				on:drop={handleDrop}
			>
				{#if uploading}
					<div class="flex items-center justify-center gap-2">
						<div class="animate-spin rounded-full h-5 w-5 border-2 border-primary border-t-transparent"></div>
						<span class="text-muted-foreground">Uploading...</span>
					</div>
				{:else}
					<div class="space-y-2">
						<svg class="w-10 h-10 mx-auto text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
						</svg>
						<p class="text-foreground font-medium">Drop files here or click to upload</p>
						<p class="text-sm text-muted-foreground">
							Supported: {SUPPORTED_EXTENSIONS.join(', ')}
						</p>
						<p class="text-xs text-muted-foreground">Max 5MB per file</p>
					</div>
					<input
						type="file"
						multiple
						accept={SUPPORTED_EXTENSIONS.join(',')}
						on:change={handleFileInputChange}
						class="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
					/>
				{/if}
			</div>

			<!-- Search -->
			<div class="space-y-3">
				<div class="flex items-center gap-2">
					<div class="relative flex-1">
						<svg class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
						</svg>
						<input
							type="text"
							bind:value={searchQuery}
							placeholder="Search knowledge base..."
							class="w-full pl-10 pr-4 py-2 rounded-lg bg-muted/50 border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
							on:keypress={(e) => e.key === 'Enter' && handleSearch()}
						/>
					</div>
					<button
						on:click={handleSearch}
						disabled={!searchQuery.trim() || searching}
						class="px-4 py-2 rounded-lg bg-primary text-primary-foreground font-medium text-sm disabled:opacity-50 disabled:cursor-not-allowed hover:bg-primary/90 transition-colors"
					>
						{searching ? 'Searching...' : 'Search'}
					</button>
				</div>

				{#if showSearchResults}
					<div class="bg-muted/50 rounded-lg border border-border p-4 space-y-3">
						<div class="flex items-center justify-between">
							<span class="text-sm text-muted-foreground">
								{searchResults.length} result{searchResults.length !== 1 ? 's' : ''} for "{searchQuery}"
							</span>
							<button
								on:click={() => { showSearchResults = false; searchResults = []; searchQuery = ''; }}
								class="text-sm text-muted-foreground hover:text-foreground"
							>
								Clear
							</button>
						</div>
						{#if searchResults.length > 0}
							<div class="space-y-2 max-h-48 overflow-y-auto">
								{#each searchResults as result}
									<div class="p-3 rounded-lg bg-muted/50 border border-border">
										<div class="flex items-center gap-2 mb-1">
											<span class="text-xs font-mono px-1.5 py-0.5 rounded bg-primary/20 text-primary">
												{result.filename}
											</span>
											<span class="text-xs text-muted-foreground">
												Chunk {result.chunk_index + 1}
											</span>
											<span class="text-xs text-success">
												Score: {result.relevance_score}
											</span>
										</div>
										<p class="text-sm text-foreground line-clamp-3">
											{result.content}
										</p>
									</div>
								{/each}
							</div>
						{:else}
							<p class="text-sm text-muted-foreground text-center py-4">
								No matching content found
							</p>
						{/if}
					</div>
				{/if}
			</div>

			<!-- Document list -->
			<div class="space-y-2">
				<label class="text-sm font-medium text-muted-foreground">Documents</label>
				{#if loading}
					<div class="flex items-center justify-center py-8">
						<div class="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent"></div>
					</div>
				{:else if documents.length === 0}
					<div class="text-center py-8 text-muted-foreground text-sm">
						No documents yet. Upload files to add context to your conversations.
					</div>
				{:else}
					<div class="space-y-2">
						{#each documents as doc (doc.id)}
							<div class="flex items-center gap-3 p-3 rounded-lg bg-muted/50 border border-border group">
								<span class="w-8 h-8 rounded-lg bg-primary/20 text-primary text-xs font-mono flex items-center justify-center flex-shrink-0">
									{getFileIcon(doc.content_type)}
								</span>
								<div class="flex-1 min-w-0">
									<p class="text-sm font-medium text-foreground truncate">{doc.filename}</p>
									<p class="text-xs text-muted-foreground">
										{formatSize(doc.file_size)} | {doc.chunk_count} chunk{doc.chunk_count !== 1 ? 's' : ''}
									</p>
								</div>
								<div class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
									<button
										on:click={() => showPreview(doc)}
										class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-hover-overlay transition-colors"
										title="Preview"
									>
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
										</svg>
									</button>
									{#if confirmDelete === doc.id}
										<span class="text-xs text-destructive">Delete?</span>
										<button
											on:click={() => handleDelete(doc.id)}
											class="p-1.5 rounded-lg text-destructive hover:bg-destructive/10 transition-colors"
											title="Confirm delete"
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
											</svg>
										</button>
										<button
											on:click={() => confirmDelete = null}
											class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-hover-overlay transition-colors"
											title="Cancel"
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
											</svg>
										</button>
									{:else}
										<button
											on:click={() => confirmDelete = doc.id}
											class="p-1.5 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
											title="Delete"
										>
											<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
											</svg>
										</button>
									{/if}
								</div>
							</div>
						{/each}
					</div>
				{/if}
			</div>

			<!-- Info box -->
			<div class="p-4 rounded-lg bg-blue-500/10 border border-blue-500/20">
				<h4 class="text-sm font-medium text-blue-400 mb-1">How it works</h4>
				<p class="text-xs text-blue-300/80">
					Documents you upload are automatically chunked and indexed. When you send a message,
					relevant content from your knowledge base is automatically included as context,
					helping Claude provide more accurate and informed responses.
				</p>
			</div>
		</div>

		<!-- Footer -->
		<div class="flex justify-end px-5 py-4 border-t border-border">
			<button
				on:click={() => dispatch('close')}
				class="px-4 py-2 rounded-lg bg-muted/50 text-foreground font-medium text-sm hover:bg-hover-overlay transition-colors"
			>
				Close
			</button>
		</div>
	</div>
</div>

<!-- Preview modal -->
{#if previewDocument}
	<div class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] z-[60] flex items-center justify-center bg-black/50 backdrop-blur-sm">
		<div class="bg-background border border-border rounded-xl shadow-2xl w-full max-w-lg max-h-[70vh] max-sm:max-h-full flex flex-col">
			<div class="flex items-center justify-between px-4 py-3 border-b border-border">
				<h3 class="font-medium text-foreground">{previewDocument.filename}</h3>
				<button
					on:click={() => previewDocument = null}
					class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-hover-overlay transition-colors"
				>
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>
			<div class="flex-1 overflow-y-auto p-4">
				<pre class="text-sm text-foreground whitespace-pre-wrap font-mono">{previewDocument.preview}</pre>
				{#if previewDocument.truncated}
					<p class="mt-2 text-xs text-muted-foreground italic">Content truncated for preview...</p>
				{/if}
			</div>
		</div>
	</div>
{/if}
