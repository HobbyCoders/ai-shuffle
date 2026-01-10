<script lang="ts">
	import { generateProfile, generateSubagent } from '$lib/api/client';
	import type { AgentExport, SubagentExport } from '$lib/api/client';

	type GenerationType = 'profile' | 'subagent';

	interface Props {
		type: GenerationType;
		isOpen: boolean;
		onClose: () => void;
		onGenerated: (data: AgentExport | SubagentExport) => void;
	}

	let { type, isOpen, onClose, onGenerated }: Props = $props();

	// Form state
	let description = $state('');
	let selectedModel = $state('haiku');
	let isGenerating = $state(false);
	let error = $state('');

	// Get title based on type
	const title = $derived(type === 'profile' ? 'Generate Profile with AI' : 'Generate Sub-agent with AI');
	const placeholder = $derived(
		type === 'profile'
			? 'Describe the profile you want, e.g., "A Python development profile with extended thinking for complex debugging tasks"'
			: 'Describe the sub-agent you want, e.g., "A code review agent that checks for security vulnerabilities and follows best practices"'
	);

	// Reset state when dialog opens
	$effect(() => {
		if (isOpen) {
			description = '';
			selectedModel = 'haiku';
			error = '';
			isGenerating = false;
		}
	});

	async function handleGenerate() {
		if (!description.trim() || description.length < 10) {
			error = 'Please provide a description of at least 10 characters.';
			return;
		}

		isGenerating = true;
		error = '';

		try {
			let generatedData: AgentExport | SubagentExport;
			if (type === 'profile') {
				generatedData = await generateProfile(description, selectedModel);
			} else {
				generatedData = await generateSubagent(description, selectedModel);
			}
			// On success, pass data to parent and close - parent will open edit form
			onGenerated(generatedData);
			onClose();
		} catch (e) {
			const err = e as { detail?: string };
			error = err.detail || 'Failed to generate. Please try again.';
		} finally {
			isGenerating = false;
		}
	}

	function handleClose() {
		if (!isGenerating) {
			onClose();
		}
	}

	function handleBackdropClick(e: MouseEvent) {
		if (e.target === e.currentTarget && !isGenerating) {
			onClose();
		}
	}
</script>

{#if isOpen}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div class="dialog-overlay" onclick={handleBackdropClick}>
		<div class="dialog-container">
			<div class="dialog-header">
				<div class="dialog-title">
					<svg class="title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<path d="M12 2a2 2 0 0 1 2 2c0 .74-.4 1.39-1 1.73V7h1a7 7 0 0 1 7 7h1a2 2 0 0 1 0 4h-1.27a7 7 0 0 1-13.46 0H6a2 2 0 0 1 0-4h1a7 7 0 0 1 7-7h1V5.73A2 2 0 0 1 12 2z"/>
						<circle cx="12" cy="14" r="1"/>
					</svg>
					<span>{title}</span>
				</div>
				<button class="close-btn" onclick={handleClose} disabled={isGenerating} title="Close">
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<path d="M18 6L6 18M6 6l12 12"/>
					</svg>
				</button>
			</div>

			<div class="dialog-content">
				<div class="form-section">
					<label class="form-label" for="description">
						Describe what you want
					</label>
					<textarea
						id="description"
						class="description-input"
						bind:value={description}
						placeholder={placeholder}
						rows="4"
						disabled={isGenerating}
					></textarea>
					<div class="char-count" class:error={description.length > 0 && description.length < 10}>
						{description.length}/2000 characters {#if description.length > 0 && description.length < 10}(min 10){/if}
					</div>
				</div>

				<div class="form-section">
					<label class="form-label" for="model">
						Generation Model
					</label>
					<select id="model" class="model-select" bind:value={selectedModel} disabled={isGenerating}>
						<option value="haiku">Haiku (Fast)</option>
						<option value="sonnet">Sonnet (Balanced)</option>
						<option value="opus">Opus (Best Quality)</option>
					</select>
					<div class="model-hint">
						Haiku is recommended for quick generation. Use Sonnet or Opus for more detailed configurations.
					</div>
				</div>

				{#if error}
					<div class="error-message">
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<circle cx="12" cy="12" r="10"/>
							<path d="M12 8v4M12 16h.01"/>
						</svg>
						<span>{error}</span>
					</div>
				{/if}

				<div class="dialog-actions">
					<button class="btn btn-secondary" onclick={handleClose} disabled={isGenerating}>
						Cancel
					</button>
					<button
						class="btn btn-primary"
						onclick={handleGenerate}
						disabled={isGenerating || description.length < 10}
					>
						{#if isGenerating}
							<span class="spinner"></span>
							<span>Generating...</span>
						{:else}
							<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
								<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
							</svg>
							<span>Generate</span>
						{/if}
					</button>
				</div>
			</div>
		</div>
	</div>
{/if}

<style>
	.dialog-overlay {
		position: absolute;
		inset: 0;
		z-index: 100;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 16px;
		background: color-mix(in oklch, var(--background) 85%, transparent);
		backdrop-filter: blur(8px);
		-webkit-backdrop-filter: blur(8px);
	}

	.dialog-container {
		width: 100%;
		max-width: 500px;
		max-height: calc(100% - 32px);
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 12px;
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
		overflow: hidden;
		display: flex;
		flex-direction: column;
	}

	.dialog-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 16px 20px;
		border-bottom: 1px solid var(--border);
		background: var(--muted);
	}

	.dialog-title {
		display: flex;
		align-items: center;
		gap: 10px;
		font-size: 1rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.title-icon {
		width: 20px;
		height: 20px;
		color: var(--primary);
	}

	.close-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		padding: 0;
		background: transparent;
		border: none;
		border-radius: 6px;
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 150ms ease;
	}

	.close-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.close-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.close-btn svg {
		width: 18px;
		height: 18px;
	}

	.dialog-content {
		flex: 1;
		overflow-y: auto;
		padding: 20px;
	}

	.form-section {
		margin-bottom: 20px;
	}

	.form-label {
		display: block;
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--foreground);
		margin-bottom: 8px;
	}

	.description-input {
		width: 100%;
		padding: 12px;
		font-size: 0.875rem;
		font-family: inherit;
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 8px;
		resize: vertical;
		min-height: 100px;
		transition: border-color 150ms ease;
	}

	.description-input:focus {
		outline: none;
		border-color: var(--primary);
	}

	.description-input:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.description-input::placeholder {
		color: var(--muted-foreground);
	}

	.char-count {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin-top: 4px;
		text-align: right;
	}

	.char-count.error {
		color: var(--destructive);
	}

	.model-select {
		width: 100%;
		padding: 10px 12px;
		font-size: 0.875rem;
		font-family: inherit;
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 8px;
		cursor: pointer;
		transition: border-color 150ms ease;
	}

	.model-select:focus {
		outline: none;
		border-color: var(--primary);
	}

	.model-select:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.model-hint {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin-top: 6px;
	}

	.error-message {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		padding: 12px;
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: 1px solid var(--destructive);
		border-radius: 8px;
		color: var(--destructive);
		font-size: 0.875rem;
		margin-bottom: 20px;
	}

	.error-message svg {
		width: 18px;
		height: 18px;
		flex-shrink: 0;
		margin-top: 1px;
	}

	.dialog-actions {
		display: flex;
		gap: 12px;
		justify-content: flex-end;
		padding-top: 8px;
	}

	.btn {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		padding: 10px 16px;
		font-size: 0.875rem;
		font-weight: 500;
		font-family: inherit;
		border-radius: 8px;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.btn svg {
		width: 16px;
		height: 16px;
	}

	.btn:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.btn-primary {
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
	}

	.btn-primary:hover:not(:disabled) {
		background: color-mix(in oklch, var(--primary) 90%, white);
	}

	.btn-secondary {
		background: var(--muted);
		color: var(--foreground);
		border: 1px solid var(--border);
	}

	.btn-secondary:hover:not(:disabled) {
		background: var(--hover-overlay);
	}

	.spinner {
		width: 16px;
		height: 16px;
		border: 2px solid currentColor;
		border-right-color: transparent;
		border-radius: 50%;
		animation: spin 0.75s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>
