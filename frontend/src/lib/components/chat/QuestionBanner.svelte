<script lang="ts">
	/**
	 * QuestionBanner - User question queue from AskUserQuestion tool
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
	<div class="border-t border-info/30 bg-info/5 p-3 sm:p-4">
		<div class="max-w-5xl mx-auto">
			{#each questions as question (question.request_id)}
				<UserQuestion data={question} on:respond={onRespond} />
			{/each}
		</div>
	</div>
{/if}
