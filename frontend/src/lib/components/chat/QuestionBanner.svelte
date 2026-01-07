<script lang="ts">
	/**
	 * QuestionBanner - User question queue from AskUserQuestion tool
	 *
	 * Wraps UserQuestion in a styled banner container that appears
	 * in the chat area when Claude asks questions that need user input.
	 */
	import UserQuestion, { type UserQuestionData } from '$lib/components/UserQuestion.svelte';

	interface Props {
		questions: UserQuestionData[];
		onRespond: (event: CustomEvent<{
			request_id: string;
			tool_use_id: string;
			answers: Record<string, string | string[]>;
		}>) => void;
	}

	let { questions, onRespond }: Props = $props();
</script>

{#if questions && questions.length > 0}
	<div class="question-banner">
		<div class="max-w-5xl mx-auto px-4 sm:px-8 py-3 space-y-3">
			{#each questions as question (question.request_id)}
				<UserQuestion data={question} on:respond={onRespond} />
			{/each}
		</div>
	</div>
{/if}

<style>
	.question-banner {
		animation: banner-enter 0.25s cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes banner-enter {
		from {
			opacity: 0;
			transform: translateY(-8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	/* Mobile: Bottom sheet style */
	@media (max-width: 639px) {
		.question-banner {
			position: fixed;
			bottom: 0;
			left: 0;
			right: 0;
			z-index: 50;
			padding-bottom: env(safe-area-inset-bottom, 0);
			background: var(--background);
			border-radius: 1.25rem 1.25rem 0 0;
			box-shadow: 0 -4px 20px rgba(0, 0, 0, 0.15);
			animation: sheet-enter 0.3s cubic-bezier(0.16, 1, 0.3, 1);
			max-height: 80vh;
			overflow-y: auto;
		}

		@keyframes sheet-enter {
			from {
				opacity: 0;
				transform: translateY(100%);
			}
			to {
				opacity: 1;
				transform: translateY(0);
			}
		}
	}
</style>
