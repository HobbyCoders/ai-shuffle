<script lang="ts">
	import { createEventDispatcher } from 'svelte';

	const dispatch = createEventDispatcher();

	// Props
	export let show = false;

	// State
	let fileInput: HTMLInputElement;
	let selectedFile: File | null = null;
	let previewData: {
		title: string;
		messageCount: number;
		createdAt: string;
		exportedAt: string;
		totalCost: number;
		turnCount: number;
	} | null = null;
	let parseError: string | null = null;
	let isImporting = false;
	let importError: string | null = null;
	let isDragOver = false;

	function close() {
		show = false;
		resetState();
		dispatch('close');
	}

	function resetState() {
		selectedFile = null;
		previewData = null;
		parseError = null;
		importError = null;
		isImporting = false;
		isDragOver = false;
	}

	function handleFileSelect(event: Event) {
		const input = event.target as HTMLInputElement;
		if (input.files && input.files.length > 0) {
			processFile(input.files[0]);
		}
	}

	function handleDrop(event: DragEvent) {
		event.preventDefault();
		isDragOver = false;

		if (event.dataTransfer?.files && event.dataTransfer.files.length > 0) {
			const file = event.dataTransfer.files[0];
			if (file.name.endsWith('.json')) {
				processFile(file);
			} else {
				parseError = 'Please select a JSON file.';
			}
		}
	}

	function handleDragOver(event: DragEvent) {
		event.preventDefault();
		isDragOver = true;
	}

	function handleDragLeave() {
		isDragOver = false;
	}

	async function processFile(file: File) {
		selectedFile = file;
		parseError = null;
		previewData = null;

		if (!file.name.endsWith('.json')) {
			parseError = 'Please select a JSON file.';
			return;
		}

		try {
			const content = await file.text();
			const data = JSON.parse(content);

			// Validate structure
			if (!data.session || !data.messages) {
				parseError = 'Invalid export format. Missing session or messages data.';
				return;
			}

			if (!Array.isArray(data.messages)) {
				parseError = 'Invalid export format. Messages must be an array.';
				return;
			}

			// Extract preview data
			const session = data.session;
			previewData = {
				title: session.title || 'Untitled Session',
				messageCount: data.messages.length,
				createdAt: session.created_at ? new Date(session.created_at).toLocaleString() : 'Unknown',
				exportedAt: data.exported_at ? new Date(data.exported_at).toLocaleString() : 'Unknown',
				totalCost: session.total_cost_usd || 0,
				turnCount: session.turn_count || data.messages.filter((m: any) => m.role === 'user').length
			};
		} catch (e) {
			parseError = 'Failed to parse JSON file. Please ensure it is a valid AI Hub export.';
			console.error('Parse error:', e);
		}
	}

	async function handleImport() {
		if (!selectedFile || !previewData) return;

		isImporting = true;
		importError = null;

		try {
			const formData = new FormData();
			formData.append('file', selectedFile);

			const response = await fetch('/api/v1/sessions/import', {
				method: 'POST',
				credentials: 'include',
				body: formData
			});

			if (!response.ok) {
				const error = await response.json().catch(() => ({ detail: 'Import failed' }));
				throw new Error(error.detail || 'Import failed');
			}

			const result = await response.json();

			dispatch('imported', {
				sessionId: result.session_id,
				title: result.title,
				messageCount: result.message_count
			});

			close();
		} catch (e) {
			importError = e instanceof Error ? e.message : 'Import failed. Please try again.';
			console.error('Import error:', e);
		} finally {
			isImporting = false;
		}
	}

	function triggerFileSelect() {
		fileInput?.click();
	}
</script>

{#if show}
	<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
	<div
		class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] bg-black/50 z-50 flex items-center justify-center p-4"
		on:click={close}
		on:keydown={(e) => e.key === 'Escape' && close()}
		role="dialog"
		aria-modal="true"
		tabindex="-1"
	>
		<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
		<div
			class="bg-card rounded-xl w-full max-w-md overflow-hidden shadow-xl border border-border"
			on:click|stopPropagation
		>
			<!-- Header -->
			<div class="px-4 py-3 border-b border-border flex items-center justify-between">
				<h2 class="text-lg font-semibold text-foreground">Import Session</h2>
				<button
					class="p-1.5 text-muted-foreground hover:text-foreground rounded-lg hover:bg-muted transition-colors"
					on:click={close}
					aria-label="Close"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<!-- Content -->
			<div class="p-4 space-y-4">
				<input
					type="file"
					accept=".json"
					bind:this={fileInput}
					on:change={handleFileSelect}
					class="hidden"
				/>

				{#if !selectedFile || parseError}
					<!-- Dropzone -->
					<!-- svelte-ignore a11y_no_static_element_interactions -->
					<div
						class="border-2 border-dashed rounded-xl p-8 text-center transition-colors cursor-pointer {isDragOver ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/50'}"
						on:click={triggerFileSelect}
						on:drop={handleDrop}
						on:dragover={handleDragOver}
						on:dragleave={handleDragLeave}
						on:keydown={(e) => e.key === 'Enter' && triggerFileSelect()}
						role="button"
						tabindex="0"
					>
						<svg class="w-12 h-12 mx-auto mb-3 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
						</svg>
						<p class="text-sm text-foreground mb-1">Drop your export file here</p>
						<p class="text-xs text-muted-foreground">or click to browse</p>
						<p class="text-xs text-muted-foreground mt-2">Supports AI Hub JSON exports (.json)</p>
					</div>

					{#if parseError}
						<div class="flex items-start gap-2 p-3 bg-destructive/10 border border-destructive/20 rounded-lg">
							<svg class="w-5 h-5 text-destructive flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
							</svg>
							<div>
								<p class="text-sm text-destructive font-medium">Invalid File</p>
								<p class="text-xs text-destructive/80 mt-0.5">{parseError}</p>
							</div>
						</div>
					{/if}
				{:else if previewData}
					<!-- Preview -->
					<div class="space-y-4">
						<div class="flex items-center gap-3 p-3 bg-muted/50 rounded-lg">
							<svg class="w-10 h-10 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
							</svg>
							<div class="flex-1 min-w-0">
								<p class="text-sm font-medium text-foreground truncate">{selectedFile.name}</p>
								<p class="text-xs text-muted-foreground">{(selectedFile.size / 1024).toFixed(1)} KB</p>
							</div>
							<button
								on:click={() => { selectedFile = null; previewData = null; }}
								class="p-1 text-muted-foreground hover:text-foreground transition-colors"
								title="Remove file"
							>
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
								</svg>
							</button>
						</div>

						<div class="border border-border rounded-lg overflow-hidden">
							<div class="px-3 py-2 bg-muted/30 border-b border-border">
								<p class="text-xs text-muted-foreground uppercase tracking-wider font-medium">Preview</p>
							</div>
							<div class="p-3 space-y-2">
								<div class="flex justify-between">
									<span class="text-sm text-muted-foreground">Title</span>
									<span class="text-sm text-foreground font-medium truncate ml-4 max-w-[200px]">{previewData.title}</span>
								</div>
								<div class="flex justify-between">
									<span class="text-sm text-muted-foreground">Messages</span>
									<span class="text-sm text-foreground">{previewData.messageCount}</span>
								</div>
								<div class="flex justify-between">
									<span class="text-sm text-muted-foreground">Turns</span>
									<span class="text-sm text-foreground">{previewData.turnCount}</span>
								</div>
								{#if previewData.totalCost > 0}
									<div class="flex justify-between">
										<span class="text-sm text-muted-foreground">Cost</span>
										<span class="text-sm text-foreground">${previewData.totalCost.toFixed(4)}</span>
									</div>
								{/if}
								<div class="flex justify-between">
									<span class="text-sm text-muted-foreground">Created</span>
									<span class="text-sm text-foreground">{previewData.createdAt}</span>
								</div>
								<div class="flex justify-between">
									<span class="text-sm text-muted-foreground">Exported</span>
									<span class="text-sm text-foreground">{previewData.exportedAt}</span>
								</div>
							</div>
						</div>

						{#if importError}
							<div class="flex items-start gap-2 p-3 bg-destructive/10 border border-destructive/20 rounded-lg">
								<svg class="w-5 h-5 text-destructive flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
								</svg>
								<div>
									<p class="text-sm text-destructive font-medium">Import Failed</p>
									<p class="text-xs text-destructive/80 mt-0.5">{importError}</p>
								</div>
							</div>
						{/if}
					</div>
				{/if}
			</div>

			<!-- Footer -->
			<div class="px-4 py-3 border-t border-border flex justify-end gap-2 bg-muted/30">
				<button
					on:click={close}
					class="px-4 py-2 text-sm bg-muted text-foreground rounded-lg hover:bg-accent transition-colors"
				>
					Cancel
				</button>
				<button
					on:click={handleImport}
					disabled={!previewData || isImporting}
					class="px-4 py-2 text-sm bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
				>
					{#if isImporting}
						<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
						Importing...
					{:else}
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
						</svg>
						Import Session
					{/if}
				</button>
			</div>
		</div>
	</div>
{/if}
