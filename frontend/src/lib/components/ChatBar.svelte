<script lang="ts">
	/**
	 * ChatBar - Unified chat input component
	 *
	 * A responsive, mobile-friendly chat input bar with:
	 * - Collapsible file attachments area
	 * - Compact model/mode selectors
	 * - Auto-growing textarea
	 * - Command (/) and file (@) autocomplete
	 */
	import CommandAutocomplete from './CommandAutocomplete.svelte';
	import FileAutocomplete from './FileAutocomplete.svelte';
	import QuickActions from './QuickActions.svelte';
	import type { FileUploadResponse } from '$lib/api/client';

	interface Props {
		tabId: string;
		profileId: string;
		projectId?: string;
		inputValue: string;
		uploadedFiles: FileUploadResponse[];
		isStreaming: boolean;
		isAuthenticated: boolean;
		isUploading: boolean;
		// Model/Mode overrides
		modelOverride: string | null;
		permissionModeOverride: string | null;
		profileModel: string;
		profilePermissionMode: string;
		// Callbacks
		onSubmit: () => void;
		onStop: () => void;
		onInputChange: (value: string) => void;
		onFileUpload: () => void;
		onRemoveFile: (index: number) => void;
		onCommandSelect: (cmd: any) => void;
		onFileSelect: (file: any) => void;
		onModelChange: (model: string | null) => void;
		onModeChange: (mode: string | null) => void;
		onQuickAction: (prompt: string) => void;
	}

	let {
		tabId,
		profileId,
		projectId,
		inputValue = $bindable(),
		uploadedFiles,
		isStreaming,
		isAuthenticated,
		isUploading,
		modelOverride,
		permissionModeOverride,
		profileModel,
		profilePermissionMode,
		onSubmit,
		onStop,
		onInputChange,
		onFileUpload,
		onRemoveFile,
		onCommandSelect,
		onFileSelect,
		onModelChange,
		onModeChange,
		onQuickAction
	}: Props = $props();

	// Component refs
	let textarea: HTMLTextAreaElement;
	let commandAutocomplete: CommandAutocomplete;
	let fileAutocomplete: FileAutocomplete;

	// Local state
	let showCommandAutocomplete = $state(false);
	let showFileAutocomplete = $state(false);
	let showModelPopup = $state(false);
	let showModePopup = $state(false);
	let showAllFiles = $state(false);

	// Computed values
	const effectiveModel = $derived(modelOverride || profileModel);
	const effectiveMode = $derived(permissionModeOverride || profilePermissionMode);
	const hasOverrides = $derived(!!modelOverride || !!permissionModeOverride);

	const modelLabels: Record<string, string> = {
		sonnet: 'Sonnet',
		opus: 'Opus',
		haiku: 'Haiku'
	};

	const modeLabels: Record<string, string> = {
		default: 'Ask',
		acceptEdits: 'Auto',
		plan: 'Plan',
		bypassPermissions: 'Bypass'
	};

	// File chip display logic - show first 2 on mobile, first 4 on desktop
	const visibleFileCount = $derived(
		typeof window !== 'undefined' && window.innerWidth < 640 ? 2 : 4
	);
	const visibleFiles = $derived(
		showAllFiles ? uploadedFiles : uploadedFiles.slice(0, visibleFileCount)
	);
	const hiddenFileCount = $derived(
		showAllFiles ? 0 : Math.max(0, uploadedFiles.length - visibleFileCount)
	);

	// Check for active @ mention (same logic as parent component)
	function hasActiveAtMention(input: string): boolean {
		for (let i = input.length - 1; i >= 0; i--) {
			if (input[i] === '@') {
				if (i === 0 || /\s/.test(input[i - 1])) {
					return true;
				}
			}
		}
		return false;
	}

	// Handle input changes
	function handleInput() {
		// Notify parent of input change
		onInputChange(inputValue);

		// Check for autocomplete triggers
		if (inputValue.startsWith('/')) {
			showCommandAutocomplete = true;
			showFileAutocomplete = false;
		} else if (hasActiveAtMention(inputValue) && projectId) {
			showFileAutocomplete = true;
			showCommandAutocomplete = false;
		} else {
			showCommandAutocomplete = false;
			showFileAutocomplete = false;
		}
	}

	// Handle keyboard events
	function handleKeyDown(e: KeyboardEvent) {
		// Let autocomplete handle it first
		if (showCommandAutocomplete && commandAutocomplete?.handleKeyDown(e)) {
			return;
		}
		if (showFileAutocomplete && fileAutocomplete?.handleKeyDown(e)) {
			return;
		}

		// Handle Enter to submit (Shift+Enter for newline)
		if (e.key === 'Enter' && !e.shiftKey) {
			// On mobile, always allow newlines
			const isMobile = typeof window !== 'undefined' &&
				window.matchMedia('(max-width: 640px)').matches &&
				('ontouchstart' in window);

			if (!isMobile) {
				e.preventDefault();
				if (inputValue.trim()) {
					onSubmit();
				}
			}
		}
	}

	// Handle command selection
	function handleCommandSelection(cmd: any) {
		showCommandAutocomplete = false;
		onCommandSelect(cmd);
	}

	// Handle file selection
	function handleFileSelection(file: any) {
		showFileAutocomplete = false;
		onFileSelect(file);
	}

	// Close popups
	function closePopups() {
		showModelPopup = false;
		showModePopup = false;
	}

	// Export textarea for focus management
	export function focus() {
		textarea?.focus();
	}

	export function getTextarea() {
		return textarea;
	}
</script>

<div class="border-t border-border bg-background">
	<div class="max-w-5xl mx-auto p-3 sm:p-4 pb-[calc(env(safe-area-inset-bottom)+3rem)] sm:pb-4 space-y-2">

		<!-- Uploaded Files - Compact horizontal scroll on mobile, wrap on desktop -->
		{#if uploadedFiles.length > 0}
			<div class="flex items-center gap-2 overflow-hidden">
				<!-- File chips container -->
				<div class="flex items-center gap-1.5 flex-1 min-w-0 overflow-x-auto sm:overflow-visible sm:flex-wrap scrollbar-none">
					{#each visibleFiles as file, index}
						<div class="flex-shrink-0 flex items-center gap-1 bg-card border border-border text-xs px-2 py-1 rounded-md group hover:border-primary/50 transition-colors">
							<svg class="w-3.5 h-3.5 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
							</svg>
							<span class="text-foreground truncate max-w-[80px] sm:max-w-[120px]" title={file.path}>
								{file.filename}
							</span>
							<button
								type="button"
								onclick={() => onRemoveFile(showAllFiles ? index : index)}
								class="opacity-60 hover:opacity-100 hover:text-destructive transition-opacity"
								title="Remove file"
							>
								<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
								</svg>
							</button>
						</div>
					{/each}

					<!-- Show more/less button -->
					{#if uploadedFiles.length > visibleFileCount}
						<button
							type="button"
							onclick={() => showAllFiles = !showAllFiles}
							class="flex-shrink-0 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground px-2 py-1 rounded-md hover:bg-accent transition-colors"
						>
							{#if showAllFiles}
								<span>Show less</span>
								<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 15l7-7 7 7" />
								</svg>
							{:else}
								<span>+{hiddenFileCount} more</span>
								<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							{/if}
						</button>
					{/if}
				</div>

				<!-- Clear all button (only when multiple files) -->
				{#if uploadedFiles.length > 1}
					<button
						type="button"
						onclick={() => {
							for (let i = uploadedFiles.length - 1; i >= 0; i--) {
								onRemoveFile(i);
							}
						}}
						class="flex-shrink-0 text-xs text-muted-foreground hover:text-destructive transition-colors"
						title="Clear all files"
					>
						Clear all
					</button>
				{/if}
			</div>
		{/if}

		<!-- Main Input Row -->
		<div class="flex items-end gap-2">
			<!-- Left action buttons (stacked on mobile for more textarea space) -->
			<div class="flex sm:flex-row flex-col gap-1 flex-shrink-0">
				<!-- Quick Actions -->
				<QuickActions
					{profileId}
					onAction={onQuickAction}
					disabled={isStreaming || !isAuthenticated}
				/>

				<!-- File Upload -->
				<button
					type="button"
					onclick={onFileUpload}
					class="w-9 h-9 sm:w-10 sm:h-10 flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent rounded-lg transition-colors disabled:opacity-50"
					disabled={isStreaming || !isAuthenticated || isUploading || !projectId}
					title={projectId ? 'Attach file' : 'Select a project to upload files'}
				>
					{#if isUploading}
						<svg class="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
					{:else}
						<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
						</svg>
					{/if}
				</button>
			</div>

			<!-- Textarea Container with Autocomplete -->
			<div class="flex-1 relative min-w-0">
				<!-- Command Autocomplete -->
				<CommandAutocomplete
					bind:this={commandAutocomplete}
					inputValue={inputValue}
					{projectId}
					visible={showCommandAutocomplete}
					onSelect={handleCommandSelection}
					onClose={() => showCommandAutocomplete = false}
				/>

				<!-- File Autocomplete -->
				<FileAutocomplete
					bind:this={fileAutocomplete}
					inputValue={inputValue}
					{projectId}
					visible={showFileAutocomplete}
					onSelect={handleFileSelection}
					onClose={() => showFileAutocomplete = false}
				/>

				<!-- Textarea with Model/Mode indicators -->
				<div class="relative">
					<textarea
						bind:this={textarea}
						bind:value={inputValue}
						oninput={handleInput}
						onkeydown={handleKeyDown}
						placeholder="Message Claude... (/ commands, @ files)"
						class="w-full bg-card border border-border rounded-xl px-3 py-2.5 pr-24 text-foreground placeholder-muted-foreground resize-none focus:outline-none focus:ring-2 focus:ring-ring min-h-[44px] max-h-[200px] leading-normal shadow-s overflow-y-auto text-sm sm:text-base"
						rows="1"
						disabled={isStreaming || !isAuthenticated}
					></textarea>

					<!-- Inline Model/Mode badges (inside textarea, top-right) -->
					<div class="absolute right-2 top-2 flex items-center gap-1">
						<!-- Model selector -->
						<div class="relative">
							<button
								type="button"
								onclick={(e) => {
									e.stopPropagation();
									showModelPopup = !showModelPopup;
									showModePopup = false;
								}}
								class="flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] sm:text-xs rounded-md transition-colors {modelOverride ? 'bg-primary/20 text-primary font-medium' : 'bg-muted text-muted-foreground hover:text-foreground'}"
								disabled={isStreaming}
								title="Select model"
							>
								<span>{modelLabels[effectiveModel] || effectiveModel}</span>
								<svg class="w-2.5 h-2.5 transition-transform {showModelPopup ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>

							{#if showModelPopup}
								<div class="fixed inset-0 z-40" onclick={closePopups}></div>
								<div class="absolute bottom-full right-0 mb-1 bg-popover border border-border rounded-lg shadow-lg overflow-hidden z-50 min-w-[90px]">
									{#each [['sonnet', 'Sonnet'], ['opus', 'Opus'], ['haiku', 'Haiku']] as [value, label]}
										<button
											type="button"
											onclick={() => {
												onModelChange(value === profileModel ? null : value);
												showModelPopup = false;
											}}
											class="w-full px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors flex items-center justify-between gap-2 {effectiveModel === value ? 'bg-accent text-foreground font-medium' : 'text-muted-foreground'}"
										>
											<span>{label}</span>
											{#if effectiveModel === value}
												<svg class="w-3 h-3 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
												</svg>
											{/if}
										</button>
									{/each}
								</div>
							{/if}
						</div>

						<!-- Mode selector -->
						<div class="relative">
							<button
								type="button"
								onclick={(e) => {
									e.stopPropagation();
									showModePopup = !showModePopup;
									showModelPopup = false;
								}}
								class="flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] sm:text-xs rounded-md transition-colors {permissionModeOverride ? 'bg-primary/20 text-primary font-medium' : 'bg-muted text-muted-foreground hover:text-foreground'}"
								disabled={isStreaming}
								title="Select permission mode"
							>
								<span>{modeLabels[effectiveMode] || effectiveMode}</span>
								<svg class="w-2.5 h-2.5 transition-transform {showModePopup ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
								</svg>
							</button>

							{#if showModePopup}
								<div class="fixed inset-0 z-40" onclick={closePopups}></div>
								<div class="absolute bottom-full right-0 mb-1 bg-popover border border-border rounded-lg shadow-lg overflow-hidden z-50 min-w-[100px]">
									{#each [['default', 'Ask'], ['acceptEdits', 'Auto'], ['plan', 'Plan'], ['bypassPermissions', 'Bypass']] as [value, label]}
										<button
											type="button"
											onclick={() => {
												onModeChange(value === profilePermissionMode ? null : value);
												showModePopup = false;
											}}
											class="w-full px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors flex items-center justify-between gap-2 {effectiveMode === value ? 'bg-accent text-foreground font-medium' : 'text-muted-foreground'}"
										>
											<span>{label}</span>
											{#if effectiveMode === value}
												<svg class="w-3 h-3 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
												</svg>
											{/if}
										</button>
									{/each}
								</div>
							{/if}
						</div>

						<!-- Reset button -->
						{#if hasOverrides}
							<button
								type="button"
								onclick={() => {
									onModelChange(null);
									onModeChange(null);
								}}
								class="p-0.5 text-muted-foreground hover:text-foreground transition-colors"
								title="Reset to defaults"
								disabled={isStreaming}
							>
								<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
								</svg>
							</button>
						{/if}
					</div>
				</div>
			</div>

			<!-- Send/Stop Button -->
			{#if isStreaming}
				<button
					type="button"
					onclick={onStop}
					class="flex-shrink-0 w-10 h-10 flex items-center justify-center bg-destructive/20 text-destructive hover:bg-destructive/30 rounded-xl transition-colors shadow-s"
					title="Stop generation"
				>
					<svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
						<rect x="6" y="6" width="12" height="12" rx="1" />
					</svg>
				</button>
			{:else}
				<button
					type="button"
					onclick={onSubmit}
					class="flex-shrink-0 w-10 h-10 flex items-center justify-center bg-primary hover:brightness-110 text-primary-foreground rounded-xl transition-all disabled:opacity-50 shadow-s"
					disabled={!inputValue.trim() || !isAuthenticated}
					title="Send message"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 10l7-7m0 0l7 7m-7-7v18" />
					</svg>
				</button>
			{/if}
		</div>

		<!-- Keyboard hint (desktop only) -->
		<div class="hidden sm:flex items-center justify-center gap-4 text-[10px] text-muted-foreground pt-1">
			<span><kbd class="px-1 py-0.5 bg-muted rounded text-[9px]">Enter</kbd> to send</span>
			<span><kbd class="px-1 py-0.5 bg-muted rounded text-[9px]">Shift+Enter</kbd> for new line</span>
			<span><kbd class="px-1 py-0.5 bg-muted rounded text-[9px]">/</kbd> commands</span>
			<span><kbd class="px-1 py-0.5 bg-muted rounded text-[9px]">@</kbd> files</span>
		</div>
	</div>
</div>

<style>
	/* Hide scrollbar for file chips on mobile */
	.scrollbar-none {
		-ms-overflow-style: none;
		scrollbar-width: none;
	}
	.scrollbar-none::-webkit-scrollbar {
		display: none;
	}

	/* Auto-grow textarea - with fallback for browsers that don't support field-sizing */
	@supports (field-sizing: content) {
		textarea {
			field-sizing: content;
		}
	}
</style>
