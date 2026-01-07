<script lang="ts">
	import { createEventDispatcher } from 'svelte';

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
	}

	let { data }: Props = $props();

	const dispatch = createEventDispatcher<{
		respond: {
			request_id: string;
			tool_use_id: string;
			answers: Record<string, string | string[]>;
		};
	}>();

	// Track selected answers for each question
	// Key is question index, value is selected option label(s) or custom text
	let answers = $state<Record<number, string | string[]>>({});
	let showOther = $state<Record<number, boolean>>({});
	let otherText = $state<Record<number, string>>({});

	// Initialize answers
	$effect(() => {
		const initial: Record<number, string | string[]> = {};
		data.questions.forEach((q, idx) => {
			if (q.multiSelect) {
				initial[idx] = [];
			} else {
				initial[idx] = '';
			}
		});
		answers = initial;
	});

	function toggleOption(questionIdx: number, optionLabel: string) {
		const question = data.questions[questionIdx];

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
		const question = data.questions[questionIdx];
		showOther[questionIdx] = true;

		if (question.multiSelect) {
			// In multi-select, "Other" clears other selections
			answers[questionIdx] = [];
		} else {
			answers[questionIdx] = '';
		}
	}

	function isSelected(questionIdx: number, optionLabel: string): boolean {
		const question = data.questions[questionIdx];
		const answer = answers[questionIdx];

		if (question.multiSelect) {
			return (answer as string[]).includes(optionLabel);
		} else {
			return answer === optionLabel;
		}
	}

	function canSubmit(): boolean {
		return data.questions.every((q, idx) => {
			if (showOther[idx]) {
				return (otherText[idx] || '').trim().length > 0;
			}
			if (q.multiSelect) {
				return (answers[idx] as string[]).length > 0;
			}
			return (answers[idx] as string).length > 0;
		});
	}

	function handleSubmit() {
		const finalAnswers: Record<string, string | string[]> = {};

		data.questions.forEach((q, idx) => {
			const key = q.header || `question_${idx}`;
			if (showOther[idx]) {
				finalAnswers[key] = otherText[idx].trim();
			} else {
				finalAnswers[key] = answers[idx];
			}
		});

		dispatch('respond', {
			request_id: data.request_id,
			tool_use_id: data.tool_use_id,
			answers: finalAnswers
		});
	}
</script>

<!-- User Question Card -->
<div class="user-question-card rounded-xl border-2 border-blue-500/40 bg-gradient-to-b from-blue-500/5 to-transparent shadow-lg shadow-blue-500/5 overflow-hidden">
	<!-- Header -->
	<div class="flex items-center gap-3 px-4 py-3 bg-blue-500/10 border-b border-blue-500/20">
		<div class="flex-shrink-0 w-10 h-10 rounded-lg bg-blue-500/10 border border-blue-500/30 flex items-center justify-center">
			<svg class="w-5 h-5 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
			</svg>
		</div>
		<div class="flex-1 min-w-0">
			<div class="flex items-center gap-2 flex-wrap">
				<span class="font-semibold text-foreground">Claude has a question</span>
				<span class="px-2 py-0.5 text-xs rounded-full bg-blue-500/20 text-blue-400 font-medium">
					Input Required
				</span>
			</div>
			<div class="text-xs text-muted-foreground mt-0.5">
				Please answer to continue
			</div>
		</div>
	</div>

	<!-- Questions -->
	<div class="p-4 space-y-6">
		{#each data.questions as question, qIdx}
			<div class="space-y-3">
				<!-- Question Header & Text -->
				<div>
					{#if question.header}
						<span class="inline-block px-2 py-0.5 text-xs font-medium rounded bg-blue-500/20 text-blue-400 mb-2">
							{question.header}
						</span>
					{/if}
					<p class="text-sm font-medium text-foreground">
						{question.question}
					</p>
					{#if question.multiSelect}
						<p class="text-xs text-muted-foreground mt-1">Select all that apply</p>
					{/if}
				</div>

				<!-- Options Grid -->
				<div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
					{#each question.options as option}
						<button
							type="button"
							onclick={() => toggleOption(qIdx, option.label)}
							class="text-left p-3 rounded-lg border transition-all duration-150
								{isSelected(qIdx, option.label)
									? 'bg-blue-500/20 border-blue-500/50 ring-1 ring-blue-500/30'
									: 'bg-muted/30 border-border/50 hover:bg-muted/50 hover:border-border'
								}"
						>
							<div class="flex items-start gap-2">
								<!-- Checkbox/Radio indicator -->
								<div class="flex-shrink-0 mt-0.5">
									{#if question.multiSelect}
										<div class="w-4 h-4 rounded border-2 flex items-center justify-center
											{isSelected(qIdx, option.label)
												? 'bg-blue-500 border-blue-500'
												: 'border-muted-foreground/50'
											}">
											{#if isSelected(qIdx, option.label)}
												<svg class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
												</svg>
											{/if}
										</div>
									{:else}
										<div class="w-4 h-4 rounded-full border-2 flex items-center justify-center
											{isSelected(qIdx, option.label)
												? 'border-blue-500'
												: 'border-muted-foreground/50'
											}">
											{#if isSelected(qIdx, option.label)}
												<div class="w-2 h-2 rounded-full bg-blue-500"></div>
											{/if}
										</div>
									{/if}
								</div>
								<div class="flex-1 min-w-0">
									<div class="text-sm font-medium text-foreground">{option.label}</div>
									{#if option.description}
										<div class="text-xs text-muted-foreground mt-0.5">{option.description}</div>
									{/if}
								</div>
							</div>
						</button>
					{/each}

					<!-- Other Option -->
					<button
						type="button"
						onclick={() => selectOther(qIdx)}
						class="text-left p-3 rounded-lg border transition-all duration-150
							{showOther[qIdx]
								? 'bg-blue-500/20 border-blue-500/50 ring-1 ring-blue-500/30'
								: 'bg-muted/30 border-border/50 hover:bg-muted/50 hover:border-border'
							}"
					>
						<div class="flex items-start gap-2">
							<div class="flex-shrink-0 mt-0.5">
								{#if question.multiSelect}
									<div class="w-4 h-4 rounded border-2 flex items-center justify-center
										{showOther[qIdx]
											? 'bg-blue-500 border-blue-500'
											: 'border-muted-foreground/50'
										}">
										{#if showOther[qIdx]}
											<svg class="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
											</svg>
										{/if}
									</div>
								{:else}
									<div class="w-4 h-4 rounded-full border-2 flex items-center justify-center
										{showOther[qIdx]
											? 'border-blue-500'
											: 'border-muted-foreground/50'
										}">
										{#if showOther[qIdx]}
											<div class="w-2 h-2 rounded-full bg-blue-500"></div>
										{/if}
									</div>
								{/if}
							</div>
							<div class="flex-1 min-w-0">
								<div class="text-sm font-medium text-foreground">Other</div>
								<div class="text-xs text-muted-foreground mt-0.5">Provide custom input</div>
							</div>
						</div>
					</button>
				</div>

				<!-- Custom Text Input (when "Other" is selected) -->
				{#if showOther[qIdx]}
					<div class="animate-in slide-in-from-top-2">
						<textarea
							bind:value={otherText[qIdx]}
							placeholder="Enter your answer..."
							rows="2"
							class="w-full px-3 py-2 text-sm bg-input border border-border rounded-lg
								focus:outline-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500/50
								resize-none"
						></textarea>
					</div>
				{/if}
			</div>

			<!-- Divider between questions -->
			{#if qIdx < data.questions.length - 1}
				<hr class="border-border/50" />
			{/if}
		{/each}
	</div>

	<!-- Submit Button -->
	<div class="px-4 py-3 border-t border-border/50 bg-muted/5">
		<button
			type="button"
			onclick={handleSubmit}
			disabled={!canSubmit()}
			class="w-full sm:w-auto px-6 py-2.5 text-sm font-medium rounded-lg transition-all duration-150
				{canSubmit()
					? 'bg-blue-500 text-white hover:bg-blue-600 active:bg-blue-700'
					: 'bg-muted text-muted-foreground cursor-not-allowed'
				}"
		>
			Submit Answer{data.questions.length > 1 ? 's' : ''}
		</button>
	</div>
</div>

<style>
	.user-question-card {
		animation: slide-up 0.3s ease-out;
	}

	@keyframes slide-up {
		from {
			opacity: 0;
			transform: translateY(10px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.animate-in {
		animation: animate-in 0.2s ease-out;
	}

	.slide-in-from-top-2 {
		--tw-enter-translate-y: -0.5rem;
	}

	@keyframes animate-in {
		from {
			opacity: 0;
			transform: translateY(var(--tw-enter-translate-y, 0));
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}
</style>
