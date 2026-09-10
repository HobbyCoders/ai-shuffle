<script lang="ts">
	import { ChevronDown, CircleHelp } from 'lucide-svelte';
	import UserQuestion, { type UserQuestionData, type QuestionResponse } from '$lib/components/UserQuestion.svelte';

	interface Props {
		requests: UserQuestionData[];
		working?: boolean;
		connected?: boolean;
		onRespond: (detail: QuestionResponse) => void;
	}
	let { requests, working = false, connected = true, onRespond }: Props = $props();
	const id = $props.id();
	let expanded = $state(true);
	let selectedId = $state<string | null>(null);
	const selected = $derived(requests.find(request => request.request_id === selectedId) ?? requests[0]);
	const count = $derived(requests.reduce((total, request) => total + request.questions.length, 0));
	const status = $derived(!connected ? 'Reconnect to answer' : selected?.nonblocking && working ? 'Still working' : selected?.nonblocking ? 'Answer to continue' : 'Waiting for your answer');
</script>

{#if requests.length > 0}
	<div class="question-tray-dock">
		<section class="question-tray" aria-label="Questions for you">
			<button type="button" class="tray-toggle" aria-expanded={expanded} aria-controls={`${id}-content`} onclick={() => expanded = !expanded}>
				<CircleHelp size={14} /><span class="title">Questions for you</span><span class="count">{count}</span>
				<span class="status">{status}</span><ChevronDown size={14} class={expanded ? 'expanded' : ''} />
			</button>
			<!-- Hide rather than unmount: collapse and request switching retain drafts. -->
			<div id={`${id}-content`} hidden={!expanded} class="tray-content">
				{#if requests.length > 1}
					<div class="request-tabs" role="group" aria-label="Pending requests">
						{#each requests as request, index (request.request_id)}
							<button type="button" aria-pressed={selected?.request_id === request.request_id} onclick={() => selectedId = request.request_id}>
								{request.questions[0]?.header || `Request ${index + 1}`}{request.questions.length > 1 ? ` · ${request.questions.length}` : ''}
							</button>
						{/each}
					</div>
				{/if}
				{#each requests as request (request.request_id)}
					<div hidden={selected?.request_id !== request.request_id}>
						<UserQuestion data={request} {onRespond} disabled={!connected} />
					</div>
				{/each}
			</div>
		</section>
	</div>
{/if}

<style>
	.question-tray-dock { padding: 0 0.75rem 0.625rem; }
	:global(html[data-platform='desktop']) .question-tray-dock { padding-right: 1.5rem; padding-left: 1.5rem; }
	.question-tray { max-width: 48rem; margin: 0 auto; border: 1px solid var(--border); border-radius: var(--radius, 10px); background: var(--card); color: var(--foreground); overflow: hidden; }
	.tray-toggle { width: 100%; display: flex; align-items: center; gap: 0.5rem; min-height: 38px; padding: 0.625rem 0.75rem; border: 0; background: none; color: var(--muted-foreground); cursor: pointer; text-align: left; font: inherit; font-size: 0.6875rem; }
	.tray-toggle:hover { background: var(--muted); }
	.title { font-weight: 500; color: var(--foreground); }
	.count { padding: 1px 5px; border-radius: 4px; background: var(--muted); color: var(--foreground); font-size: 0.625rem; }
	.status { margin-left: auto; font-size: 0.625rem; }
	.tray-toggle :global(svg) { flex-shrink: 0; }
	.tray-toggle :global(.expanded) { transform: rotate(180deg); }
	.tray-content { border-top: 1px solid var(--border); max-height: min(40dvh, 22rem); overflow-y: auto; overscroll-behavior: contain; }
	[hidden] { display: none !important; }
	.request-tabs { display: flex; gap: 6px; padding: 0.625rem 0.75rem 0; flex-wrap: wrap; }
	.request-tabs button { border: 1px solid transparent; border-radius: 4px; padding: 4px 7px; background: none; color: var(--muted-foreground); font: inherit; font-size: 0.6875rem; cursor: pointer; }
	.request-tabs button[aria-pressed='true'] { border-color: var(--border); background: var(--muted); color: var(--foreground); }
	button:focus-visible { outline: 2px solid var(--ring, var(--foreground)); outline-offset: -2px; }
	@media (max-width: 380px) { .status { max-width: 7rem; text-align: right; } }
</style>
