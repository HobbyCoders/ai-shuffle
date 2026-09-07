<script lang="ts">
	import './deck/cards/card-design-system.css';

	export interface QuestionOption {
		label: string;
		description: string;
	}

	export interface Question {
		question: string;
		header: string;
		options: QuestionOption[];
		multiSelect: boolean;
	}

	export interface UserQuestionData {
		request_id: string;
		tool_use_id: string;
		questions: Question[];
	}

	interface Props {
		data: UserQuestionData;
		onRespond?: (detail: {
			request_id: string;
			tool_use_id: string;
			answers: Record<string, string | string[]>;
			/** The user closed the dialog ("X") without answering anything. */
			dismissed?: boolean;
			/** Question keys the user explicitly chose to skip. */
			skipped?: string[];
		}) => void;
	}

	let { data, onRespond }: Props = $props();

	// Wizard state
	let currentStep = $state(0);
	let isReviewStep = $state(false);
	let slideDirection = $state<'forward' | 'backward'>('forward');

	// Track selected answers for each question
	// Key is question index, value is selected option label(s) or custom text
	let answers = $state<Record<number, string | string[]>>({});
	let showOther = $state<Record<number, boolean>>({});
	let otherText = $state<Record<number, string>>({});
	// Per-question "skip": the user declines to answer THIS question but still
	// submits the form. Mutually exclusive with having a selection — picking an
	// option (or "Other") clears the skip, and skipping clears the selection.
	let skipped = $state<Record<number, boolean>>({});

	// Initialize answers when a NEW question arrives (tracked by request_id).
	// IMPORTANT: Do NOT depend on `data` directly — only on `data.request_id`.
	// If the store replaces the data object reference (e.g., heartbeat triggers
	// get_pending_questions → pending_questions replaces array), the $effect
	// would refire and wipe the user's in-progress answers/typed "Other" text.
	let initializedRequestId = $state<string | null>(null);
	$effect(() => {
		const requestId = data.request_id;
		if (requestId === initializedRequestId) return;
		initializedRequestId = requestId;

		const questions = data.questions ?? [];
		const initial: Record<number, string | string[]> = {};
		questions.forEach((q, idx) => {
			if (q.multiSelect) {
				initial[idx] = [];
			} else {
				initial[idx] = '';
			}
		});
		answers = initial;
		showOther = {};
		otherText = {};
		skipped = {};
		currentStep = 0;
		isReviewStep = false;
	});

	// Computed values
	const totalSteps = $derived(data.questions?.length ?? 0);
	const isSingleQuestion = $derived(totalSteps === 1);
	const currentQuestion = $derived(data.questions?.[currentStep] ?? { question: '', header: '', options: [], multiSelect: false });
	const currentOptions = $derived(currentQuestion.options ?? []);
	const isLastQuestion = $derived(currentStep === totalSteps - 1);
	const isFirstQuestion = $derived(currentStep === 0);

	function toggleOption(questionIdx: number, optionLabel: string) {
		const question = data.questions?.[questionIdx];
		if (!question) return;

		// Making a selection is an implicit un-skip.
		skipped[questionIdx] = false;

		if (question.multiSelect) {
			const current = answers[questionIdx] as string[];
			if (current.includes(optionLabel)) {
				answers[questionIdx] = current.filter(l => l !== optionLabel);
			} else {
				answers[questionIdx] = [...current, optionLabel];
			}
			// Clear "other" if selecting regular options
			if (showOther[questionIdx]) {
				showOther[questionIdx] = false;
				otherText[questionIdx] = '';
			}
		} else {
			answers[questionIdx] = optionLabel;
			showOther[questionIdx] = false;
			otherText[questionIdx] = '';
		}
	}

	function selectOther(questionIdx: number) {
		const question = data.questions?.[questionIdx];
		if (!question) return;
		showOther[questionIdx] = true;
		skipped[questionIdx] = false;

		if (question.multiSelect) {
			answers[questionIdx] = [];
		} else {
			answers[questionIdx] = '';
		}
	}

	/** Toggle "skip this question". Skipping discards any in-progress answer. */
	function toggleSkip(questionIdx: number) {
		const question = data.questions?.[questionIdx];
		if (!question) return;
		const next = !skipped[questionIdx];
		skipped[questionIdx] = next;
		if (next) {
			showOther[questionIdx] = false;
			otherText[questionIdx] = '';
			answers[questionIdx] = question.multiSelect ? [] : '';
		}
	}

	function isSkipped(questionIdx: number): boolean {
		return skipped[questionIdx] === true;
	}

	function isSelected(questionIdx: number, optionLabel: string): boolean {
		const question = data.questions?.[questionIdx];
		if (!question) return false;
		const answer = answers[questionIdx];

		if (question.multiSelect) {
			return Array.isArray(answer) && answer.includes(optionLabel);
		} else {
			return answer === optionLabel;
		}
	}

	function canProceed(questionIdx: number): boolean {
		// A skipped question is "resolved" for navigation/submit purposes.
		if (skipped[questionIdx]) return true;
		if (showOther[questionIdx]) {
			return (otherText[questionIdx] || '').trim().length > 0;
		}
		const question = data.questions?.[questionIdx];
		if (!question) return false;
		if (question.multiSelect) {
			const answer = answers[questionIdx];
			return Array.isArray(answer) && answer.length > 0;
		}
		return ((answers[questionIdx] as string) || '').length > 0;
	}

	function canSubmit(): boolean {
		const questions = data.questions ?? [];
		return questions.length > 0 && questions.every((_, idx) => canProceed(idx));
	}

	function goNext() {
		if (!canProceed(currentStep)) return;

		slideDirection = 'forward';
		if (isLastQuestion) {
			// Go to review step (if multi-question)
			if (!isSingleQuestion) {
				isReviewStep = true;
			} else {
				handleSubmit();
			}
		} else {
			currentStep++;
		}
	}

	function goBack() {
		slideDirection = 'backward';
		if (isReviewStep) {
			isReviewStep = false;
		} else if (currentStep > 0) {
			currentStep--;
		}
	}

	function goToQuestion(idx: number) {
		slideDirection = idx > currentStep ? 'forward' : 'backward';
		isReviewStep = false;
		currentStep = idx;
	}

	function getAnswerDisplay(questionIdx: number): string {
		if (skipped[questionIdx]) {
			return 'Skipped';
		}
		if (showOther[questionIdx]) {
			return otherText[questionIdx];
		}
		const answer = answers[questionIdx];
		if (Array.isArray(answer)) {
			return answer.join(', ');
		}
		return answer || '';
	}

	function handleSubmit() {
		const questions = data.questions ?? [];
		// The SDK's AskUserQuestion tool matches answers by the full question
		// TEXT (not the short header) and expects each answer as a plain string;
		// multi-select selections must be comma-separated. Sending header-keyed
		// answers or raw arrays makes the tool report "did not answer".
		const finalAnswers: Record<string, string> = {};
		// Skipped questions carry no answer of their own — they ride out-of-band
		// as keys so the backend can fill in its own "user skipped this question"
		// text. Sending an empty string instead would make the tool report the
		// whole form as unanswered.
		const skippedKeys: string[] = [];

		questions.forEach((q, idx) => {
			const key = q.question || q.header || `question_${idx}`;
			if (skipped[idx]) {
				skippedKeys.push(key);
				return;
			}
			if (showOther[idx]) {
				finalAnswers[key] = (otherText[idx] || '').trim();
			} else {
				const answer = answers[idx];
				finalAnswers[key] = Array.isArray(answer) ? answer.join(', ') : (answer ?? '');
			}
		});

		onRespond?.({
			request_id: data.request_id,
			tool_use_id: data.tool_use_id,
			answers: finalAnswers,
			skipped: skippedKeys
		});
	}

	/**
	 * Close the dialog without answering. Resolves the pending AskUserQuestion
	 * tool call with a "user dismissed" outcome rather than leaving it hanging
	 * until the question TTL expires.
	 */
	function handleDismiss() {
		onRespond?.({
			request_id: data.request_id,
			tool_use_id: data.tool_use_id,
			answers: {},
			dismissed: true
		});
	}

	// Keyboard navigation
	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			if (isReviewStep && canSubmit()) {
				handleSubmit();
			} else if (canProceed(currentStep)) {
				goNext();
			}
		} else if (e.key === 'Escape' && !isFirstQuestion) {
			e.preventDefault();
			goBack();
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- User Question Panel -->
<div
	class="card-system question-panel rounded-2xl border shadow-lg overflow-hidden backdrop-blur-xs"
	style="
		border-color: var(--border);
		background: var(--card);
	"
	role="dialog"
	aria-labelledby="question-title"
>
	<!-- Header -->
	<div class="flex items-center gap-3 px-4 py-3 border-b border-border/40">
		<!-- Icon -->
		<div
			class="shrink-0 w-9 h-9 rounded-xl border flex items-center justify-center"
			style="
				background: color-mix(in oklch, var(--muted) 50%, var(--card));
				border-color: var(--border);
			"
		>
			<svg class="w-4.5 h-4.5 text-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
			</svg>
		</div>

		<!-- Title -->
		<div class="flex-1 min-w-0">
			<h3 id="question-title" class="text-sm font-semibold text-foreground flex items-center gap-2">
				Claude has a question
				<span
					class="inline-flex items-center px-1.5 py-0.5 text-[10px] font-medium rounded-md border"
					style="
						background: color-mix(in oklch, var(--muted-foreground) 15%, transparent);
						color: var(--muted-foreground);
						border-color: color-mix(in oklch, var(--muted-foreground) 20%, transparent);
					"
				>
					Input Required
				</span>
			</h3>
			<p class="text-xs text-muted-foreground">
				{#if isSingleQuestion}
					Please answer to continue
				{:else if isReviewStep}
					Review your answers
				{:else}
					Step {currentStep + 1} of {totalSteps}
				{/if}
			</p>
		</div>

		<!-- Progress Indicator (for multi-question) -->
		{#if !isSingleQuestion}
			<div class="shrink-0 flex items-center gap-1">
				{#each data.questions as _, idx}
					<button
						type="button"
						onclick={() => goToQuestion(idx)}
						disabled={!canProceed(idx) && idx > currentStep}
						class="w-2 h-2 rounded-full transition-all duration-200"
						style={
							isReviewStep ? 'background: var(--primary);' :
							idx === currentStep ? 'background: var(--primary); transform: scale(1.25);' :
							idx < currentStep ? 'background: color-mix(in oklch, var(--primary) 60%, transparent);' :
							'background: color-mix(in oklch, var(--muted-foreground) 30%, transparent);'
						}
						aria-label="Go to question {idx + 1}"
					></button>
				{/each}
				{#if !isSingleQuestion}
					<button
						type="button"
						onclick={() => { if (canSubmit()) isReviewStep = true; }}
						disabled={!canSubmit()}
						class="w-2 h-2 rounded-full transition-all duration-200 ml-1"
						style={
							isReviewStep ? 'background: var(--success); transform: scale(1.25);' :
							canSubmit() ? 'background: color-mix(in oklch, var(--success) 60%, transparent);' :
							'background: color-mix(in oklch, var(--muted-foreground) 30%, transparent);'
						}
						aria-label="Review answers"
					></button>
				{/if}
			</div>
		{/if}

		<!-- Dismiss: closes the dialog and resolves the tool call as unanswered. -->
		<button
			type="button"
			onclick={handleDismiss}
			class="uq-close-btn shrink-0"
			aria-label="Dismiss question without answering"
			title="Dismiss without answering"
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
			</svg>
		</button>
	</div>

	<!-- Content Area with Slide Animation -->
	<div class="question-content-wrapper overflow-hidden">
		{#if isReviewStep}
			<!-- Review Step -->
			<div class="p-4 space-y-3 animate-slide-{slideDirection}">
				<p class="text-sm text-muted-foreground mb-4">
					Review and confirm your answers before submitting:
				</p>

				{#each data.questions as question, idx}
					<div class="review-item group">
						<div class="flex items-start justify-between gap-3">
							<div class="flex-1 min-w-0">
								{#if question.header}
									<span
										class="inline-block px-2 py-0.5 text-[10px] font-medium rounded-md mb-1.5"
										style="background: color-mix(in oklch, var(--primary) 15%, transparent); color: var(--primary);"
									>
										{question.header}
									</span>
								{/if}
								<p class="text-xs text-muted-foreground">{question.question}</p>
								<p
									class="text-sm font-medium mt-1 wrap-break-word"
									class:text-foreground={!isSkipped(idx)}
									class:uq-skipped-answer={isSkipped(idx)}
								>
									{getAnswerDisplay(idx) || '—'}
								</p>
							</div>
							<button
								type="button"
								onclick={() => goToQuestion(idx)}
								class="uq-edit-btn shrink-0 p-1.5 rounded-lg text-muted-foreground hover:text-foreground transition-colors opacity-0 group-hover:opacity-100 hover-actions"
								aria-label="Edit answer"
							>
								<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
								</svg>
							</button>
						</div>
					</div>
				{/each}
			</div>
		{:else}
			<!-- Question Step -->
			{#key currentStep}
			<div class="p-4 animate-slide-{slideDirection}">
				<!-- Question Header & Text -->
				<div class="mb-4">
					{#if currentQuestion.header}
						<span
							class="inline-block px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider rounded-md mb-2"
							style="background: color-mix(in oklch, var(--primary) 15%, transparent); color: var(--primary);"
						>
							{currentQuestion.header}
						</span>
					{/if}
					<p class="text-sm font-medium text-foreground leading-relaxed">
						{currentQuestion.question}
					</p>
					{#if currentQuestion.multiSelect}
						<p class="text-xs text-muted-foreground mt-1">Select all that apply</p>
					{/if}
				</div>

				<!-- Options -->
				<div class="space-y-2">
					{#each currentOptions as option}
						<button
							type="button"
							onclick={() => toggleOption(currentStep, option.label)}
							class="option-card {isSelected(currentStep, option.label) ? 'selected' : ''}"
						>
							<div class="flex items-start gap-3">
								<!-- Checkbox/Radio indicator -->
								<div class="shrink-0 mt-0.5">
									{#if currentQuestion.multiSelect}
										<div class="checkbox-indicator {isSelected(currentStep, option.label) ? 'checked' : ''}">
											{#if isSelected(currentStep, option.label)}
												<svg class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
												</svg>
											{/if}
										</div>
									{:else}
										<div class="radio-indicator {isSelected(currentStep, option.label) ? 'checked' : ''}">
											{#if isSelected(currentStep, option.label)}
												<div class="radio-dot"></div>
											{/if}
										</div>
									{/if}
								</div>
								<div class="flex-1 min-w-0 text-left">
									<div class="text-sm font-medium text-foreground">{option.label}</div>
									{#if option.description}
										<div class="text-xs text-muted-foreground mt-0.5 leading-relaxed">{option.description}</div>
									{/if}
								</div>
							</div>
						</button>
					{/each}

					<!-- Other Option -->
					<button
						type="button"
						onclick={() => selectOther(currentStep)}
						class="option-card {showOther[currentStep] ? 'selected' : ''}"
					>
						<div class="flex items-start gap-3">
							<div class="shrink-0 mt-0.5">
								{#if currentQuestion.multiSelect}
									<div class="checkbox-indicator {showOther[currentStep] ? 'checked' : ''}">
										{#if showOther[currentStep]}
											<svg class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
											</svg>
										{/if}
									</div>
								{:else}
									<div class="radio-indicator {showOther[currentStep] ? 'checked' : ''}">
										{#if showOther[currentStep]}
											<div class="radio-dot"></div>
										{/if}
									</div>
								{/if}
							</div>
							<div class="flex-1 min-w-0 text-left">
								<div class="text-sm font-medium text-foreground">Other</div>
								<div class="text-xs text-muted-foreground mt-0.5">Provide custom input</div>
							</div>
						</div>
					</button>

					<!-- Custom Text Input (when "Other" is selected) -->
					{#if showOther[currentStep]}
						<div class="mt-3 animate-expand">
							<textarea
								bind:value={otherText[currentStep]}
								placeholder="Enter your answer..."
								rows="2"
								class="other-textarea"
							></textarea>
						</div>
					{/if}
				</div>

				<!-- Per-question skip: a lightweight opt-out, deliberately not
				     styled as a primary action. Reported to the tool as
				     "user skipped this question" so the rest of the form can
				     still submit. -->
				<div class="mt-3 flex justify-end">
					<button
						type="button"
						onclick={() => toggleSkip(currentStep)}
						class="uq-skip-btn"
						class:active={isSkipped(currentStep)}
						aria-pressed={isSkipped(currentStep)}
					>
						{#if isSkipped(currentStep)}
							Skipped — undo
						{:else}
							Skip this question
						{/if}
					</button>
				</div>
			</div>
			{/key}
		{/if}
	</div>

	<!-- Footer / Actions -->
	<div class="px-4 py-3 border-t border-border/40 bg-card/20">
		<div class="flex items-center justify-between gap-3">
			<!-- Back Button -->
			<div class="shrink-0">
				{#if !isFirstQuestion || isReviewStep}
					<button
						type="button"
						onclick={goBack}
						class="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
						</svg>
						Back
					</button>
				{:else}
					<div></div>
				{/if}
			</div>

			<!-- Next/Submit Button -->
			<div class="shrink-0">
				{#if isReviewStep}
					<button
						type="button"
						onclick={handleSubmit}
						disabled={!canSubmit()}
						class="submit-btn"
						class:disabled={!canSubmit()}
					>
						Submit
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>
					</button>
				{:else if isSingleQuestion}
					<button
						type="button"
						onclick={handleSubmit}
						disabled={!canProceed(currentStep)}
						class="submit-btn"
						class:disabled={!canProceed(currentStep)}
					>
						Submit
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
						</svg>
					</button>
				{:else}
					<button
						type="button"
						onclick={goNext}
						disabled={!canProceed(currentStep)}
						class="submit-btn"
						class:disabled={!canProceed(currentStep)}
					>
						{isLastQuestion ? 'Review' : 'Next'}
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
						</svg>
					</button>
				{/if}
			</div>
		</div>

		<!-- Keyboard Hints -->
		<div class="mt-2 flex items-center justify-center gap-3 text-[10px] text-muted-foreground">
			<span class="flex items-center gap-1">
				<kbd class="px-1.5 py-0.5 bg-muted/50 rounded-sm text-[9px] font-mono">Enter</kbd>
				<span>{isReviewStep ? 'Submit' : isSingleQuestion ? 'Submit' : isLastQuestion ? 'Review' : 'Next'}</span>
			</span>
			{#if !isFirstQuestion || isReviewStep}
				<span class="flex items-center gap-1">
					<kbd class="px-1.5 py-0.5 bg-muted/50 rounded-sm text-[9px] font-mono">Esc</kbd>
					<span>Back</span>
				</span>
			{/if}
		</div>
	</div>
</div>

<style>
	.question-panel {
		animation: panel-enter 0.25s cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes panel-enter {
		from {
			opacity: 0;
			transform: translateY(8px) scale(0.98);
		}
		to {
			opacity: 1;
			transform: translateY(0) scale(1);
		}
	}

	/* Slide animations for wizard steps */
	.animate-slide-forward {
		animation: slide-forward 0.25s cubic-bezier(0.16, 1, 0.3, 1);
	}

	.animate-slide-backward {
		animation: slide-backward 0.25s cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes slide-forward {
		from {
			opacity: 0;
			transform: translateX(20px);
		}
		to {
			opacity: 1;
			transform: translateX(0);
		}
	}

	@keyframes slide-backward {
		from {
			opacity: 0;
			transform: translateX(-20px);
		}
		to {
			opacity: 1;
			transform: translateX(0);
		}
	}

	.animate-expand {
		animation: expand 0.2s ease-out;
	}

	@keyframes expand {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	/* Option Card Styling */
	.option-card {
		display: block;
		width: 100%;
		padding: 0.75rem;
		border-radius: var(--radius, 0.75rem);
		border: 1px solid var(--border);
		background: var(--card);
		cursor: pointer;
		transition: all 0.15s ease;
		text-align: left;
	}

	.option-card:hover {
		border-color: var(--accent);
		box-shadow: 0 0 12px color-mix(in oklch, var(--accent) 25%, transparent);
	}

	.option-card.selected {
		border-color: var(--selection-ring);
		background: color-mix(in oklch, var(--selection) 8%, var(--card));
		box-shadow: var(--selection-shadow-subtle);
	}

	.option-card:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Checkbox Indicator */
	.checkbox-indicator {
		width: 1.125rem;
		height: 1.125rem;
		border-radius: 0.375rem;
		border: 2px solid var(--muted-foreground);
		display: flex;
		align-items: center;
		justify-content: center;
		transition: all 0.15s ease;
	}

	.checkbox-indicator.checked {
		background: var(--primary);
		border-color: var(--primary);
	}

	/* Radio Indicator */
	.radio-indicator {
		width: 1.125rem;
		height: 1.125rem;
		border-radius: 50%;
		border: 2px solid var(--muted-foreground);
		display: flex;
		align-items: center;
		justify-content: center;
		transition: all 0.15s ease;
	}

	.radio-indicator.checked {
		border-color: var(--primary);
	}

	.radio-dot {
		width: 0.5rem;
		height: 0.5rem;
		border-radius: 50%;
		background: var(--primary);
		animation: dot-pop 0.15s ease;
	}

	/* Submit Button */
	.submit-btn {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.625rem 1.25rem;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: var(--radius, 0.75rem);
		background: var(--button-primary-gradient, var(--primary));
		color: var(--primary-foreground);
		box-shadow: var(--shadow-s);
		transition: all 0.15s ease;
	}

	.submit-btn:hover:not(.disabled) {
		filter: brightness(1.1);
		box-shadow: var(--shadow-m), 0 0 20px var(--glow-color-soft);
	}

	.submit-btn:active:not(.disabled) {
		transform: scale(0.98);
	}

	.submit-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.submit-btn.disabled {
		background: var(--muted);
		color: var(--muted-foreground);
		cursor: not-allowed;
		box-shadow: none;
	}

	/* Other Textarea */
	.other-textarea {
		width: 100%;
		padding: 0.625rem 0.75rem;
		font-size: 0.875rem;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		color: var(--foreground);
		resize: none;
		transition: all 0.15s ease;
	}

	.other-textarea:focus {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
		border-color: var(--ring);
	}

	@keyframes dot-pop {
		from {
			transform: scale(0);
		}
		to {
			transform: scale(1);
		}
	}

	/* Review Item */
	.review-item {
		padding: 0.75rem;
		border-radius: 0.875rem;
		border: 1px solid var(--border);
		background: var(--card);
		transition: all 0.15s ease;
	}

	.review-item:hover {
		border-color: color-mix(in oklch, var(--border) 60%, var(--foreground));
		background: color-mix(in oklch, var(--card) 95%, var(--foreground));
	}

	/* Mobile optimizations */
	@media (max-width: 639px) {
		.question-panel {
			border-radius: 1.25rem 1.25rem 0 0;
			border-bottom: none;
		}

		.option-card {
			padding: 1rem;
		}
	}

	/* Dismiss ("X") button — ghost, matching .uq-edit-btn's translucent hover
	   so it never paints an opaque fill that sticks after a tap on touch. */
	.uq-close-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 1.75rem;
		height: 1.75rem;
		border-radius: 0.5rem;
		color: var(--muted-foreground);
		background: transparent;
		transition: color 0.15s ease, background 0.15s ease;
	}

	.uq-close-btn:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	.uq-close-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Per-question skip — a quiet text control, never a primary button. */
	.uq-skip-btn {
		padding: 0.25rem 0.5rem;
		font-size: 0.75rem;
		line-height: 1rem;
		border-radius: 0.5rem;
		color: var(--muted-foreground);
		background: transparent;
		border: 1px solid transparent;
		cursor: pointer;
		transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
	}

	.uq-skip-btn:hover {
		color: var(--foreground);
		background: var(--hover-overlay);
	}

	.uq-skip-btn.active {
		color: var(--foreground);
		border-color: var(--border);
	}

	.uq-skip-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.uq-skipped-answer {
		color: var(--muted-foreground);
		font-style: italic;
	}

	/* Touch devices (the mobile shell included): tap targets stay comfortable and
	   no hover fill is left stuck after a tap. */
	@media (hover: none) {
		.uq-close-btn {
			width: 2rem;
			height: 2rem;
		}

		.uq-close-btn:hover,
		.uq-skip-btn:hover {
			color: var(--muted-foreground);
			background: transparent;
		}

		.uq-skip-btn {
			padding: 0.375rem 0.625rem;
		}

		.uq-skip-btn.active {
			color: var(--foreground);
		}
	}

	/* Edit button hover */
	.uq-edit-btn:hover {
		background: var(--hover-overlay);
	}

	.uq-edit-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Touch devices: always show action buttons since hover is not available */
	@media (hover: none) {
		.hover-actions {
			opacity: 1 !important;
		}
	}
</style>
