<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { parseAgentExportFile, importAgent, type AgentExport } from '$lib/api/client';

	const dispatch = createEventDispatcher();

	// Props
	export let show = false;

	// State
	let fileInput: HTMLInputElement;
	let dragOver = false;
	let error: string | null = null;
	let importing = false;

	// Parsed agent data
	let parsedData: AgentExport | null = null;
	let overrideName = '';
	let overrideId = '';

	function close() {
		show = false;
		reset();
		dispatch('close');
	}

	function reset() {
		parsedData = null;
		overrideName = '';
		overrideId = '';
		error = null;
		importing = false;
		dragOver = false;
	}

	function triggerFileSelect() {
		fileInput?.click();
	}

	async function handleFileSelect(event: Event) {
		const input = event.target as HTMLInputElement;
		if (input.files?.length) {
			await processFile(input.files[0]);
		}
		// Reset input to allow selecting same file again
		input.value = '';
	}

	async function handleDrop(event: DragEvent) {
		event.preventDefault();
		dragOver = false;

		const files = event.dataTransfer?.files;
		if (files?.length) {
			await processFile(files[0]);
		}
	}

	function handleDragOver(event: DragEvent) {
		event.preventDefault();
		dragOver = true;
	}

	function handleDragLeave() {
		dragOver = false;
	}

	async function processFile(file: File) {
		error = null;
		parsedData = null;

		// Validate file type
		if (!file.name.endsWith('.json')) {
			error = 'Please select a JSON file';
			return;
		}

		try {
			const content = await file.text();
			parsedData = parseAgentExportFile(content);
			overrideName = parsedData.agent.name;
			overrideId = '';
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to parse file';
		}
	}

	async function doImport() {
		if (!parsedData) return;

		importing = true;
		error = null;

		try {
			const importData = {
				...parsedData,
				new_name: overrideName !== parsedData.agent.name ? overrideName : undefined,
				new_id: overrideId || undefined
			};

			const profile = await importAgent(importData);
			dispatch('imported', profile);
			close();
		} catch (e: any) {
			error = e?.detail || e?.message || 'Import failed';
		} finally {
			importing = false;
		}
	}

	function getModelDisplay(model?: string | null): string {
		switch (model) {
			case 'haiku': return 'Haiku';
			case 'sonnet': return 'Sonnet';
			case 'sonnet-1m': return 'Sonnet 1M';
			case 'opus': return 'Opus';
			default: return model || 'Default';
		}
	}

	function getPermissionDisplay(mode?: string | null): string {
		switch (mode) {
			case 'default': return 'Default';
			case 'acceptEdits': return 'Accept Edits';
			case 'plan': return 'Plan';
			case 'bypassPermissions': return 'Bypass';
			default: return mode || 'Default';
		}
	}

	function formatDate(dateStr: string): string {
		try {
			return new Date(dateStr).toLocaleDateString('en-US', {
				year: 'numeric',
				month: 'short',
				day: 'numeric',
				hour: '2-digit',
				minute: '2-digit'
			});
		} catch {
			return dateStr;
		}
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
			class="bg-card rounded-xl w-full max-w-lg max-h-[90vh] overflow-hidden shadow-xl border border-border flex flex-col"
			on:click|stopPropagation
		>
			<!-- Header -->
			<div class="px-4 py-3 border-b border-border flex items-center justify-between shrink-0">
				<h2 class="text-lg font-semibold text-foreground">Import Agent</h2>
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
			<div class="flex-1 overflow-y-auto p-4 space-y-4">
				<!-- Hidden file input -->
				<input
					type="file"
					accept=".json"
					bind:this={fileInput}
					on:change={handleFileSelect}
					class="hidden"
				/>

				{#if !parsedData}
					<!-- File drop zone -->
					<div
						class="border-2 border-dashed rounded-xl p-8 text-center transition-colors cursor-pointer {dragOver ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/50'}"
						on:click={triggerFileSelect}
						on:drop={handleDrop}
						on:dragover={handleDragOver}
						on:dragleave={handleDragLeave}
						role="button"
						tabindex="0"
						on:keydown={(e) => e.key === 'Enter' && triggerFileSelect()}
					>
						<svg class="w-12 h-12 mx-auto mb-3 text-muted-foreground/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
						</svg>
						<p class="text-foreground font-medium mb-1">Drop agent file here</p>
						<p class="text-sm text-muted-foreground">or click to browse</p>
						<p class="text-xs text-muted-foreground mt-2">.json files only</p>
					</div>
				{:else}
					<!-- Preview imported agent -->
					<div class="space-y-4">
						<div class="bg-muted/50 rounded-lg p-4 border border-border">
							<div class="flex items-start justify-between mb-3">
								<div>
									<h3 class="font-medium text-foreground">{parsedData.agent.name}</h3>
									{#if parsedData.agent.description}
										<p class="text-sm text-muted-foreground mt-0.5">{parsedData.agent.description}</p>
									{/if}
								</div>
								<button
									on:click={reset}
									class="text-xs text-muted-foreground hover:text-foreground"
								>
									Change file
								</button>
							</div>

							<div class="grid grid-cols-2 gap-2 text-xs">
								<div class="flex items-center gap-2">
									<span class="text-muted-foreground">Model:</span>
									<span class="text-foreground">{getModelDisplay(parsedData.agent.model)}</span>
								</div>
								<div class="flex items-center gap-2">
									<span class="text-muted-foreground">Permissions:</span>
									<span class="text-foreground">{getPermissionDisplay(parsedData.agent.permission_mode)}</span>
								</div>
								{#if parsedData.agent.allowed_tools?.length}
									<div class="flex items-center gap-2 col-span-2">
										<span class="text-muted-foreground">Allowed tools:</span>
										<span class="text-foreground">{parsedData.agent.allowed_tools.length} tools</span>
									</div>
								{/if}
								{#if parsedData.agent.disallowed_tools?.length}
									<div class="flex items-center gap-2 col-span-2">
										<span class="text-muted-foreground">Blocked tools:</span>
										<span class="text-foreground">{parsedData.agent.disallowed_tools.length} tools</span>
									</div>
								{/if}
								{#if parsedData.agent.system_prompt_type === 'custom'}
									<div class="flex items-center gap-2 col-span-2">
										<span class="text-muted-foreground">System prompt:</span>
										<span class="text-foreground">Custom</span>
									</div>
								{:else if parsedData.agent.system_prompt_append}
									<div class="flex items-center gap-2 col-span-2">
										<span class="text-muted-foreground">System prompt:</span>
										<span class="text-foreground">Claude Code + Appended</span>
									</div>
								{/if}
							</div>

							<div class="mt-3 pt-3 border-t border-border text-xs text-muted-foreground">
								Exported: {formatDate(parsedData.exported_at)} (v{parsedData.version})
							</div>
						</div>

						<!-- Override options -->
						<div class="space-y-3">
							<h4 class="text-sm font-medium text-foreground">Import Options</h4>

							<div>
								<label class="block text-xs text-muted-foreground mb-1.5">Name (required)</label>
								<input
									bind:value={overrideName}
									class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
									placeholder="Agent name"
								/>
							</div>

							<div>
								<label class="block text-xs text-muted-foreground mb-1.5">ID (optional - auto-generated if empty)</label>
								<input
									bind:value={overrideId}
									class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 font-mono"
									placeholder="my-agent-id"
									pattern="^[a-z0-9-]*$"
								/>
								<p class="text-xs text-muted-foreground mt-1">Lowercase letters, numbers, and hyphens only</p>
							</div>
						</div>
					</div>
				{/if}

				<!-- Error display -->
				{#if error}
					<div class="bg-destructive/10 border border-destructive/30 rounded-lg px-4 py-3 text-sm text-destructive">
						{error}
					</div>
				{/if}
			</div>

			<!-- Footer -->
			<div class="px-4 py-3 border-t border-border flex justify-end gap-2 shrink-0">
				<button
					on:click={close}
					class="px-4 py-2 text-sm bg-muted text-foreground rounded-lg hover:bg-accent transition-colors"
				>
					Cancel
				</button>
				{#if parsedData}
					<button
						on:click={doImport}
						disabled={!overrideName || importing}
						class="px-4 py-2 text-sm bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
					>
						{#if importing}
							<span class="animate-spin w-4 h-4 border-2 border-current border-t-transparent rounded-full"></span>
							Importing...
						{:else}
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
							</svg>
							Import Agent
						{/if}
					</button>
				{/if}
			</div>
		</div>
	</div>
{/if}
