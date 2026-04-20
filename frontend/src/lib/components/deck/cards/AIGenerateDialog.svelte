<script lang="ts">
	import {
		generateProfile,
		generateSubagent,
		continueGeneration,
		type GenerationResponse,
		type ClarificationQuestion,
		type AgentExport,
		type SubagentExport
	} from '$lib/api/client';
	import { MODELS, DEFAULT_MODEL } from '$lib/constants/models';

	type GenerationType = 'profile' | 'subagent';
	type DialogState = 'initial' | 'generating' | 'clarifying' | 'continuing';

	interface Props {
		type: GenerationType;
		isOpen: boolean;
		onClose: () => void;
		onGenerated: (data: AgentExport | SubagentExport) => void;
	}

	let { type, isOpen, onClose, onGenerated }: Props = $props();

	// Form state
	let description = $state('');
	let referenceMaterial = $state('');
	let showReferenceMaterial = $state(false);
	let selectedModel = $state(DEFAULT_MODEL);
	let dialogState = $state<DialogState>('initial');
	let error = $state('');

	// Clarification state
	let clarificationQuestions: ClarificationQuestion[] = $state([]);
	let clarificationAnswers: Record<string, string> = $state({});

	// Get title based on type and state
	const title = $derived(() => {
		if (dialogState === 'clarifying') {
			return 'A few quick questions...';
		}
		return type === 'profile' ? 'Generate Profile with AI' : 'Generate Sub-agent with AI';
	});

	const placeholder = $derived(
		type === 'profile'
			? 'Describe the profile you want, e.g., "A Python development profile with extended thinking for complex debugging tasks"'
			: 'Describe the sub-agent you want, e.g., "A code review agent that checks for security vulnerabilities and follows best practices"'
	);

	const isLoading = $derived(dialogState === 'generating' || dialogState === 'continuing');

	// Reset state when dialog opens
	$effect(() => {
		if (isOpen) {
			description = '';
			referenceMaterial = '';
			showReferenceMaterial = false;
			selectedModel = DEFAULT_MODEL;
			dialogState = 'initial';
			error = '';
			clarificationQuestions = [];
			clarificationAnswers = {};
		}
	});

	function handleResponse(response: GenerationResponse) {
		if (response.status === 'needs_clarification' && response.questions) {
			// Show clarification questions
			clarificationQuestions = response.questions;
			clarificationAnswers = {};
			// Pre-fill with empty strings
			for (const q of response.questions) {
				clarificationAnswers[q.id] = '';
			}
			dialogState = 'clarifying';
		} else if (response.status === 'complete' && response.result) {
			// Generation complete - pass to parent
			onGenerated(response.result as AgentExport | SubagentExport);
			onClose();
		} else {
			throw new Error('Unexpected response from generation API');
		}
	}

	function formatApiError(e: unknown): string {
		const err = e as { detail?: string | Array<{ msg?: string }> };
		if (!err.detail) {
			return 'Failed to generate. Please try again.';
		}
		// Handle FastAPI validation error format (array of errors)
		if (Array.isArray(err.detail)) {
			const messages = err.detail.map((d) => d.msg || 'Validation error').join('; ');
			return messages || 'Validation error';
		}
		return err.detail;
	}

	async function handleGenerate() {
		if (!description.trim() || description.length < 10) {
			error = 'Please provide a description of at least 10 characters.';
			return;
		}

		dialogState = 'generating';
		error = '';

		try {
			let response: GenerationResponse;
			const refMaterial = referenceMaterial.trim() || undefined;
			if (type === 'profile') {
				response = await generateProfile(description, selectedModel, false, refMaterial);
			} else {
				response = await generateSubagent(description, selectedModel, false, refMaterial);
			}
			handleResponse(response);
		} catch (e) {
			error = formatApiError(e);
			dialogState = 'initial';
		}
	}

	async function handleClarificationSubmit() {
		// Validate all questions are answered
		for (const q of clarificationQuestions) {
			if (!clarificationAnswers[q.id]?.trim()) {
				error = 'Please answer all questions.';
				return;
			}
		}

		dialogState = 'continuing';
		error = '';

		try {
			const refMaterial = referenceMaterial.trim() || undefined;
			const response = await continueGeneration(
				type,
				description,
				clarificationAnswers,
				selectedModel,
				refMaterial
			);
			handleResponse(response);
		} catch (e) {
			error = formatApiError(e);
			dialogState = 'clarifying';
		}
	}

	function handleBackToDescription() {
		dialogState = 'initial';
		clarificationQuestions = [];
		clarificationAnswers = {};
		error = '';
	}

	function handleClose() {
		if (!isLoading) {
			onClose();
		}
	}

	function handleBackdropClick(e: MouseEvent) {
		if (e.target === e.currentTarget && !isLoading) {
			onClose();
		}
	}

	function selectOption(questionId: string, option: string) {
		clarificationAnswers[questionId] = option;
	}
</script>

{#if isOpen}
	<div class="dialog-overlay" role="dialog" aria-modal="true">
		<button class="dialog-overlay-close" onclick={handleBackdropClick} tabindex="-1" aria-label="Close dialog"></button>
		<div class="dialog-container" class:clarifying={dialogState === 'clarifying'}>
			<div class="dialog-header">
				<div class="dialog-title">
					<svg class="title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						{#if dialogState === 'clarifying'}
							<circle cx="12" cy="12" r="10" />
							<path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
							<path d="M12 17h.01" />
						{:else}
							<path
								d="M12 2a2 2 0 0 1 2 2c0 .74-.4 1.39-1 1.73V7h1a7 7 0 0 1 7 7h1a2 2 0 0 1 0 4h-1.27a7 7 0 0 1-13.46 0H6a2 2 0 0 1 0-4h1a7 7 0 0 1 7-7h1V5.73A2 2 0 0 1 12 2z"
							/>
							<circle cx="12" cy="14" r="1" />
						{/if}
					</svg>
					<span>{title()}</span>
				</div>
				<button class="close-btn" onclick={handleClose} disabled={isLoading} title="Close">
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
						<path d="M18 6L6 18M6 6l12 12" />
					</svg>
				</button>
			</div>

			<div class="dialog-content">
				{#if dialogState === 'clarifying'}
					<!-- Clarification Questions UI -->
					<div class="clarification-intro">
						<p>To generate the best configuration, I need a bit more information:</p>
					</div>

					{#each clarificationQuestions as question (question.id)}
						<div class="question-section">
							<label class="question-label" for="question-{question.id}">{question.question}</label>
							{#if question.context}
								<p class="question-context">{question.context}</p>
							{/if}

							{#if question.options && question.options.length > 0}
								<div class="options-grid">
									{#each question.options as option}
										<button
											class="option-btn"
											class:selected={clarificationAnswers[question.id] === option}
											onclick={() => selectOption(question.id, option)}
											disabled={isLoading}
										>
											{option}
										</button>
									{/each}
								</div>
								<div class="custom-answer">
									<input
										id="question-{question.id}"
										type="text"
										class="custom-input"
										placeholder="Or type a custom answer..."
										value={clarificationAnswers[question.id] || ''}
										oninput={(e) => (clarificationAnswers[question.id] = e.currentTarget.value)}
										disabled={isLoading}
									/>
								</div>
							{:else}
								<textarea
									class="answer-input"
									placeholder="Your answer..."
									value={clarificationAnswers[question.id] || ''}
									oninput={(e) => (clarificationAnswers[question.id] = e.currentTarget.value)}
									disabled={isLoading}
									rows="2"
								></textarea>
							{/if}
						</div>
					{/each}

					{#if error}
						<div class="error-message">
							<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
								<circle cx="12" cy="12" r="10" />
								<path d="M12 8v4M12 16h.01" />
							</svg>
							<span>{error}</span>
						</div>
					{/if}

					<div class="dialog-actions">
						<button class="btn btn-secondary" onclick={handleBackToDescription} disabled={isLoading}>
							Back
						</button>
						<button class="btn btn-primary" onclick={handleClarificationSubmit} disabled={isLoading}>
							{#if isLoading}
								<span class="spinner"></span>
								<span>Generating...</span>
							{:else}
								<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
								</svg>
								<span>Generate</span>
							{/if}
						</button>
					</div>
				{:else}
					<!-- Initial Form UI -->
					<div class="form-section">
						<label class="form-label" for="description"> Describe what you want </label>
						<textarea
							id="description"
							class="description-input"
							bind:value={description}
							placeholder={placeholder}
							rows="4"
							disabled={isLoading}
						></textarea>
						<div class="char-count" class:error={description.length > 0 && description.length < 10}>
							{description.length}/5000 characters
							{#if description.length > 0 && description.length < 10}(min 10){/if}
						</div>
					</div>

					<!-- Reference Material Section (Collapsible) -->
					<div class="form-section">
						<button
							type="button"
							class="reference-toggle"
							onclick={() => (showReferenceMaterial = !showReferenceMaterial)}
							disabled={isLoading}
						>
							<svg
								class="toggle-icon"
								class:expanded={showReferenceMaterial}
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2"
							>
								<polyline points="9 18 15 12 9 6"></polyline>
							</svg>
							<span>Reference Material</span>
							<span class="toggle-hint">(optional - paste existing prompts or examples)</span>
						</button>

						{#if showReferenceMaterial}
							<div class="reference-content">
								<textarea
									id="reference-material"
									class="reference-input"
									bind:value={referenceMaterial}
									placeholder="Paste an existing system prompt, examples, or detailed specifications here. The AI will analyze this and incorporate relevant patterns into the generated configuration."
									rows="6"
									disabled={isLoading}
								></textarea>
								<div class="char-count">
									{referenceMaterial.length}/15000 characters
								</div>
							</div>
						{/if}
					</div>

					<div class="form-section">
						<label class="form-label" for="model"> Generation Model </label>
						<select id="model" class="model-select" bind:value={selectedModel} disabled={isLoading}>
							{#each MODELS as model (model.value)}
								<option value={model.value}>{model.label}</option>
							{/each}
						</select>
						<div class="model-hint">
							Sonnet 4.6 is recommended for comprehensive generation. Use Opus 4.7 for complex configurations.
						</div>
					</div>

					{#if error}
						<div class="error-message">
							<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
								<circle cx="12" cy="12" r="10" />
								<path d="M12 8v4M12 16h.01" />
							</svg>
							<span>{error}</span>
						</div>
					{/if}

					<div class="dialog-actions">
						<button class="btn btn-secondary" onclick={handleClose} disabled={isLoading}> Cancel </button>
						<button
							class="btn btn-primary"
							onclick={handleGenerate}
							disabled={isLoading || description.length < 10}
						>
							{#if isLoading}
								<span class="spinner"></span>
								<span>Generating...</span>
							{:else}
								<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
									<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
								</svg>
								<span>Generate</span>
							{/if}
						</button>
					</div>
				{/if}
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
	}

	.dialog-overlay-close {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		background: color-mix(in oklch, var(--background) 85%, transparent);
		backdrop-filter: blur(8px);
		-webkit-backdrop-filter: blur(8px);
		border: none;
		cursor: default;
		padding: 0;
	}

	.dialog-container {
		position: relative;
		z-index: 1;
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

	.dialog-container.clarifying {
		max-width: 550px;
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

	/* Reference Material Toggle */
	.reference-toggle {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 10px 12px;
		font-size: 0.875rem;
		font-family: inherit;
		color: var(--foreground);
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 8px;
		cursor: pointer;
		transition: all 150ms ease;
		text-align: left;
	}

	.reference-toggle:hover:not(:disabled) {
		background: var(--hover-overlay);
		border-color: var(--primary);
	}

	.reference-toggle:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.toggle-icon {
		width: 16px;
		height: 16px;
		transition: transform 150ms ease;
		flex-shrink: 0;
	}

	.toggle-icon.expanded {
		transform: rotate(90deg);
	}

	.toggle-hint {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin-left: auto;
	}

	.reference-content {
		margin-top: 12px;
		animation: slideDown 150ms ease;
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

	.reference-input {
		width: 100%;
		padding: 12px;
		font-size: 0.8rem;
		font-family: var(--font-mono);
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 8px;
		resize: vertical;
		min-height: 120px;
		transition: border-color 150ms ease;
	}

	.reference-input:focus {
		outline: none;
		border-color: var(--primary);
	}

	.reference-input:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.reference-input::placeholder {
		color: var(--muted-foreground);
		font-family: inherit;
	}

	/* Clarification UI */
	.clarification-intro {
		margin-bottom: 20px;
		padding: 12px 16px;
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		border-radius: 8px;
	}

	.clarification-intro p {
		font-size: 0.875rem;
		color: var(--foreground);
		margin: 0;
	}

	.question-section {
		margin-bottom: 24px;
	}

	.question-label {
		display: block;
		font-size: 0.9rem;
		font-weight: 500;
		color: var(--foreground);
		margin-bottom: 6px;
	}

	.question-context {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin: 0 0 12px 0;
	}

	.options-grid {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-bottom: 8px;
	}

	.option-btn {
		padding: 8px 14px;
		font-size: 0.8rem;
		font-family: inherit;
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 6px;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.option-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		border-color: var(--primary);
	}

	.option-btn.selected {
		background: var(--primary);
		color: var(--primary-foreground);
		border-color: var(--primary);
	}

	.option-btn:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.custom-answer {
		margin-top: 8px;
	}

	.custom-input,
	.answer-input {
		width: 100%;
		padding: 10px 12px;
		font-size: 0.875rem;
		font-family: inherit;
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 8px;
		transition: border-color 150ms ease;
	}

	.custom-input:focus,
	.answer-input:focus {
		outline: none;
		border-color: var(--primary);
	}

	.custom-input:disabled,
	.answer-input:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.answer-input {
		resize: vertical;
		min-height: 60px;
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
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}
</style>
