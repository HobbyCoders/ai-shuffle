<script lang="ts">
	import { api } from '$lib/api/client';

	// Export/Import types
	interface ExportedSubagent {
		id: string;
		name: string;
		description: string;
		prompt: string;
		tools?: string[] | null;
		model?: string | null;
	}

	interface ExportedProfileConfig {
		model?: string;
		permission_mode?: string;
		max_turns?: number;
		allowed_tools?: string[];
		disallowed_tools?: string[];
		system_prompt?: Record<string, unknown> | null;
		setting_sources?: string[];
		enabled_agents?: string[];
		cwd?: string;
		add_dirs?: string[];
		env?: Record<string, string>;
		extra_args?: Record<string, unknown>;
	}

	interface ExportedProfile {
		id: string;
		name: string;
		description?: string | null;
		config: ExportedProfileConfig;
	}

	interface ExportData {
		version: string;
		export_type: string;
		exported_at: string;
		profiles?: ExportedProfile[];
		subagents?: ExportedSubagent[];
	}

	interface ImportResult {
		success: boolean;
		profiles_imported: number;
		profiles_skipped: number;
		profiles_updated: number;
		subagents_imported: number;
		subagents_skipped: number;
		subagents_updated: number;
		errors: string[];
		warnings: string[];
	}

	// State
	let exportLoading = false;
	let importLoading = false;
	let error = '';
	let success = '';
	let importResult: ImportResult | null = null;
	let overwriteExisting = false;
	let fileInput: HTMLInputElement;

	// Export functions
	async function exportAll() {
		await doExport('/export-import/all', 'ai-hub-export-all');
	}

	async function exportProfiles() {
		await doExport('/export-import/profiles', 'ai-hub-profiles');
	}

	async function exportSubagents() {
		await doExport('/export-import/subagents', 'ai-hub-subagents');
	}

	async function doExport(endpoint: string, filename: string) {
		exportLoading = true;
		error = '';
		success = '';
		importResult = null;

		try {
			const data = await api.get<ExportData>(endpoint);

			// Create download
			const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `${filename}-${new Date().toISOString().split('T')[0]}.json`;
			document.body.appendChild(a);
			a.click();
			document.body.removeChild(a);
			URL.revokeObjectURL(url);

			success = `Exported successfully!`;
		} catch (e: any) {
			error = e.detail || 'Export failed';
		}
		exportLoading = false;
	}

	// Import functions
	function triggerFileUpload() {
		fileInput?.click();
	}

	async function handleFileSelect(event: Event) {
		const input = event.target as HTMLInputElement;
		const file = input.files?.[0];

		if (!file) return;

		// Reset input for re-selection
		input.value = '';

		importLoading = true;
		error = '';
		success = '';
		importResult = null;

		try {
			// Read and parse file first for validation
			const content = await file.text();
			let data: ExportData;

			try {
				data = JSON.parse(content);
			} catch {
				throw { detail: 'Invalid JSON file' };
			}

			// Validate structure
			if (!data.version) {
				throw { detail: 'Invalid export file: missing version' };
			}

			// Import via JSON endpoint
			const result = await api.post<ImportResult>(
				`/export-import/import/json?overwrite_existing=${overwriteExisting}`,
				data
			);

			importResult = result;

			if (result.success) {
				const imported =
					result.profiles_imported +
					result.subagents_imported +
					result.profiles_updated +
					result.subagents_updated;
				success = `Import completed! ${imported} items imported/updated.`;
			} else {
				error = `Import completed with errors. See details below.`;
			}
		} catch (e: any) {
			error = e.detail || 'Import failed';
		}

		importLoading = false;
	}

	function dismissResult() {
		importResult = null;
		success = '';
		error = '';
	}
</script>

<div class="space-y-6">
	<!-- Export Section -->
	<div>
		<h3 class="text-lg font-medium text-white mb-3">Export</h3>
		<p class="text-sm text-gray-400 mb-4">
			Download your profiles and subagents as JSON files for backup or migration.
		</p>

		<div class="flex flex-wrap gap-3">
			<button
				on:click={exportAll}
				disabled={exportLoading}
				class="btn btn-primary flex items-center gap-2"
			>
				{#if exportLoading}
					<span class="animate-spin inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full"></span>
				{:else}
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
					</svg>
				{/if}
				Export All
			</button>

			<button
				on:click={exportProfiles}
				disabled={exportLoading}
				class="btn btn-secondary flex items-center gap-2"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
				</svg>
				Profiles Only
			</button>

			<button
				on:click={exportSubagents}
				disabled={exportLoading}
				class="btn btn-secondary flex items-center gap-2"
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
				</svg>
				Subagents Only
			</button>
		</div>
	</div>

	<!-- Import Section -->
	<div>
		<h3 class="text-lg font-medium text-white mb-3">Import</h3>
		<p class="text-sm text-gray-400 mb-4">
			Upload a previously exported JSON file to import profiles and subagents.
		</p>

		<div class="flex flex-wrap items-center gap-4">
			<input
				type="file"
				accept=".json,application/json"
				bind:this={fileInput}
				on:change={handleFileSelect}
				class="hidden"
			/>

			<button
				on:click={triggerFileUpload}
				disabled={importLoading}
				class="btn btn-primary flex items-center gap-2"
			>
				{#if importLoading}
					<span class="animate-spin inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full"></span>
				{:else}
					<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
					</svg>
				{/if}
				Import from File
			</button>

			<label class="flex items-center gap-2 text-sm text-gray-400 cursor-pointer">
				<input
					type="checkbox"
					bind:checked={overwriteExisting}
					class="w-4 h-4 rounded border-gray-600 bg-[var(--color-bg)] text-[var(--color-primary)] focus:ring-[var(--color-primary)] focus:ring-offset-0"
				/>
				Overwrite existing items
			</label>
		</div>
	</div>

	<!-- Status Messages -->
	{#if error}
		<div class="bg-red-900/50 border border-red-500 text-red-300 px-4 py-3 rounded-lg flex items-start justify-between">
			<span>{error}</span>
			<button on:click={dismissResult} class="ml-2 text-red-400 hover:text-red-300">&times;</button>
		</div>
	{/if}

	{#if success}
		<div class="bg-green-900/30 border border-green-500/50 text-green-300 px-4 py-3 rounded-lg flex items-start justify-between">
			<span>{success}</span>
			<button on:click={dismissResult} class="ml-2 text-green-400 hover:text-green-300">&times;</button>
		</div>
	{/if}

	<!-- Import Result Details -->
	{#if importResult}
		<div class="bg-[var(--color-bg)] border border-[var(--color-border)] rounded-lg p-4">
			<h4 class="text-sm font-medium text-white mb-3">Import Details</h4>

			<div class="grid grid-cols-2 md:grid-cols-3 gap-4 mb-4">
				<div>
					<div class="text-2xl font-bold text-green-400">{importResult.profiles_imported}</div>
					<div class="text-xs text-gray-500">Profiles Imported</div>
				</div>
				<div>
					<div class="text-2xl font-bold text-blue-400">{importResult.profiles_updated}</div>
					<div class="text-xs text-gray-500">Profiles Updated</div>
				</div>
				<div>
					<div class="text-2xl font-bold text-gray-400">{importResult.profiles_skipped}</div>
					<div class="text-xs text-gray-500">Profiles Skipped</div>
				</div>
				<div>
					<div class="text-2xl font-bold text-green-400">{importResult.subagents_imported}</div>
					<div class="text-xs text-gray-500">Subagents Imported</div>
				</div>
				<div>
					<div class="text-2xl font-bold text-blue-400">{importResult.subagents_updated}</div>
					<div class="text-xs text-gray-500">Subagents Updated</div>
				</div>
				<div>
					<div class="text-2xl font-bold text-gray-400">{importResult.subagents_skipped}</div>
					<div class="text-xs text-gray-500">Subagents Skipped</div>
				</div>
			</div>

			{#if importResult.warnings.length > 0}
				<div class="mb-3">
					<div class="text-xs font-medium text-yellow-400 mb-1">Warnings:</div>
					<ul class="text-xs text-yellow-300/80 list-disc list-inside space-y-0.5">
						{#each importResult.warnings as warning}
							<li>{warning}</li>
						{/each}
					</ul>
				</div>
			{/if}

			{#if importResult.errors.length > 0}
				<div>
					<div class="text-xs font-medium text-red-400 mb-1">Errors:</div>
					<ul class="text-xs text-red-300/80 list-disc list-inside space-y-0.5">
						{#each importResult.errors as err}
							<li>{err}</li>
						{/each}
					</ul>
				</div>
			{/if}
		</div>
	{/if}
</div>
