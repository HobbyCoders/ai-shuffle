<script lang="ts">
	/**
	 * What waits on the owner in the open chat, docked above the composer
	 * (issue #1816): permission prompts, private-access requests and the
	 * agent's questions. It replaces the permission modal and the question
	 * tray on the phone and never renders inside the controls panel.
	 *
	 * Permission and one-shot access approvals are answered by sliding the
	 * knob right (approve) or left (deny), or by tapping Deny / Approve, with
	 * "Always do this" beside them. An access request that needs private
	 * inputs opens the existing access dialog through Review. A question shows
	 * its options as large buttons plus "Type an answer"; a multi-part or
	 * multi-select question falls back to the shared question form. Several
	 * waiting items show "1 of N", which cycles.
	 */
	import { untrack } from 'svelte';
	import { ChevronRight, KeyRound, MessageCircleQuestionMark, Pencil, SendHorizontal, Terminal, Check, X } from 'lucide-svelte';
	import UserQuestion from '$lib/components/UserQuestion.svelte';
	import type { UserQuestionData } from '$lib/components/UserQuestion.svelte';
	import type { PermissionRequestData } from '$lib/components/PermissionRequest.svelte';
	import { chatAccessSummary, type PendingAccessItem } from '$lib/stores/chat-access';
	import { notify } from '$lib/stores/notifications';
	import type { PromptDockProps } from '$lib/components/chat/composer-shell';
	import SlideToAnswer from './SlideToAnswer.svelte';

	let { sessionId, permissions, questions, working = false, connected = true, onPermission, onQuestion }: PromptDockProps = $props();

	type Item =
		| { kind: 'permission'; id: string; request: PermissionRequestData }
		| { kind: 'question'; id: string; request: UserQuestionData }
		| { kind: 'access'; id: string; item: PendingAccessItem };

	const summary = $derived($chatAccessSummary);
	const accessItems = $derived<PendingAccessItem[]>(
		summary && sessionId && summary.sessionId === sessionId && summary.pending > 0 ? summary.items ?? [] : []
	);

	const queue = $derived<Item[]>([
		...permissions.map((request) => ({ kind: 'permission' as const, id: `p:${request.request_id}`, request })),
		...accessItems.map((item) => ({ kind: 'access' as const, id: `a:${item.id}`, item })),
		...questions.map((request) => ({ kind: 'question' as const, id: `q:${request.request_id}`, request }))
	]);

	/** The item on show; falls back to the first when it is answered or gone. */
	let shownId = $state<string | null>(null);
	const current = $derived(queue.find((item) => item.id === shownId) ?? queue[0] ?? null);
	const position = $derived(current ? queue.indexOf(current) + 1 : 0);

	function next() {
		if (queue.length < 2 || !current) return;
		shownId = queue[(queue.indexOf(current) + 1) % queue.length].id;
	}

	// ── Per-item state: arming, answered-once, "always", typed answer ─────
	const ARM_MS = 300;
	const RETRY_MS = 5000;
	let armed = $state(false);
	let answered = $state(false);
	let always = $state(false);
	let typing = $state(false);
	let typed = $state('');
	let flash = $state<{ ok: boolean; text: string } | null>(null);
	let typeRef = $state<HTMLInputElement | null>(null);

	$effect(() => {
		const id = current?.id;
		untrack(() => {
			armed = false;
			answered = false;
			always = false;
			typing = false;
			typed = '';
		});
		if (!id) return;
		const timer = setTimeout(() => (armed = true), ARM_MS);
		return () => clearTimeout(timer);
	});

	$effect(() => {
		if (!answered) return;
		const retry = setTimeout(() => (answered = false), RETRY_MS);
		return () => clearTimeout(retry);
	});

	function showResult(ok: boolean, text: string) {
		flash = { ok, text };
		setTimeout(() => { if (flash?.text === text) flash = null; }, 1000);
	}

	// ── Permission ────────────────────────────────────────────────────────
	function relayedFrom(request: PermissionRequestData): string | null {
		const tag = request.provider_agent;
		if (!tag || !tag.remote_approval_id) return null;
		return (tag.worker_name ?? '').trim() || 'another worker';
	}

	function preview(request: PermissionRequestData): string {
		const input = request.tool_input ?? {};
		switch (request.tool_name) {
			case 'Bash': return String(input.command ?? '');
			case 'Read': case 'Write': case 'Edit': return String(input.file_path ?? '');
			case 'Glob': case 'Grep': return String(input.pattern ?? input.path ?? '');
			case 'WebFetch': return String(input.url ?? '');
			case 'WebSearch': return String(input.query ?? '');
			case 'Task': case 'Agent': return String(input.description ?? input.prompt ?? '');
			default: return request.description?.trim() || JSON.stringify(input);
		}
	}

	function verb(request: PermissionRequestData): string {
		if (request.tool_name === 'Bash') return 'wants to run';
		if (['Write', 'Edit', 'NotebookEdit'].includes(request.tool_name)) return 'wants to edit';
		if (request.tool_name === 'Read') return 'wants to read';
		return 'wants to use';
	}

	function answerPermission(request: PermissionRequestData, decision: 'allow' | 'deny') {
		if (answered) return;
		answered = true;
		onPermission({
			request_id: request.request_id,
			decision,
			remember: always && !relayedFrom(request) ? 'profile' : 'none'
		});
		showResult(decision === 'allow', decision === 'allow' ? 'Approved' : 'Denied');
	}

	// ── Private access ────────────────────────────────────────────────────
	const KIND_LABEL: Record<PendingAccessItem['kind'], string> = {
		machine: 'SSH machine',
		folder: 'Folder',
		service: 'HTTPS service',
		action: 'One-shot approval'
	};

	async function answerAccess(item: PendingAccessItem, allow: boolean) {
		if (answered || !summary?.decide) return;
		answered = true;
		const ok = await summary.decide(item.id, allow);
		if (ok) showResult(allow, allow ? 'Allowed once' : 'Denied');
		else {
			answered = false;
			notify.error('Could not answer this request. Try again or open Review.');
		}
	}

	// ── Question ──────────────────────────────────────────────────────────
	/** One single-choice question with options: answered by a tap. */
	function simple(request: UserQuestionData): boolean {
		const qs = request.questions ?? [];
		return qs.length === 1 && !qs[0].multiSelect;
	}

	function answerQuestion(request: UserQuestionData, answer: string) {
		const text = answer.trim();
		if (!text || answered || !connected) return;
		answered = true;
		const q = request.questions[0];
		onQuestion({
			request_id: request.request_id,
			tool_use_id: request.tool_use_id,
			answers: { [q.question || q.header || 'question_0']: text },
			skipped: []
		});
		showResult(true, 'Answer sent');
	}

	function startTyping() {
		typing = true;
		queueMicrotask(() => typeRef?.focus());
	}
</script>

{#if current}
	<div class="prompt-dock" data-no-drawer-swipe>
		<section class="prompt" aria-label="Waiting on you" aria-live="polite">
			{#if flash}
				<div class="p-result" class:ok={flash.ok} class:no={!flash.ok} role="status">
					{#if flash.ok}<Check size={18} aria-hidden="true" />{:else}<X size={18} aria-hidden="true" />{/if}{flash.text}
				</div>
			{:else if current.kind === 'permission'}
				{@const request = current.request}
				{@const command = preview(request)}
				<div class="p-head">
					<Terminal size={17} class="t-ico" aria-hidden="true" />
					<strong>{request.display_name?.trim() || request.tool_name}</strong>
					<span class="what">{verb(request)}</span>
					<code class="inline-cmd">{command}</code>
					{#if queue.length > 1}
						<button type="button" class="count" onclick={next} aria-label={`${position} of ${queue.length}. Show the next request`}>{position} of {queue.length}<ChevronRight size={14} aria-hidden="true" /></button>
					{/if}
				</div>
				{#if command}<pre class="cmd">{#if request.tool_name === 'Bash'}<span class="p">$ </span>{/if}{command}</pre>{/if}
				{#if relayedFrom(request)}
					<p class="p-note">From {relayedFrom(request)}. Answer each time.</p>
				{:else}
					<button type="button" class="check-row" role="checkbox" aria-checked={always} onclick={() => (always = !always)}>
						<span class="box" aria-hidden="true"><Check size={14} /></span>Always do this
					</button>
				{/if}
				<SlideToAnswer approveLabel="Approve" disabled={answered} {armed}
					onApprove={() => answerPermission(request, 'allow')}
					onDeny={() => answerPermission(request, 'deny')} />
			{:else if current.kind === 'access'}
				{@const item = current.item}
				<div class="p-head">
					<KeyRound size={17} class="t-ico keyc" aria-hidden="true" />
					<strong>{item.kind === 'action' ? 'Approval' : 'Access request'}</strong>
					<span class="what">{KIND_LABEL[item.kind]}</span>
					{#if queue.length > 1}
						<button type="button" class="count" onclick={next} aria-label={`${position} of ${queue.length}. Show the next request`}>{position} of {queue.length}<ChevronRight size={14} aria-hidden="true" /></button>
					{/if}
				</div>
				<p class="p-body">{item.detail}</p>
				{#if item.kind === 'action' && summary?.decide}
					<SlideToAnswer approveLabel="Allow" disabled={answered} {armed}
						onApprove={() => answerAccess(item, true)}
						onDeny={() => answerAccess(item, false)} />
				{:else}
					<button type="button" class="review-btn" onclick={() => summary?.open()}>Review</button>
				{/if}
			{:else}
				{@const request = current.request}
				{@const question = request.questions[0]}
				<div class="p-head">
					<MessageCircleQuestionMark size={17} class="t-ico q" aria-hidden="true" />
					<strong>Question</strong>
					<span class="what">{!connected ? 'Reconnect to answer' : request.nonblocking && working ? 'Still working' : 'Waiting for you'}</span>
					{#if queue.length > 1}
						<button type="button" class="count" onclick={next} aria-label={`${position} of ${queue.length}. Show the next request`}>{position} of {queue.length}<ChevronRight size={14} aria-hidden="true" /></button>
					{/if}
				</div>
				{#if simple(request) && question}
					<p class="p-body q-text">{question.question}</p>
					{#if typing || !question.options?.length}
						<form class="q-type" onsubmit={(event) => { event.preventDefault(); answerQuestion(request, typed); }}>
							<input bind:this={typeRef} bind:value={typed} type="text" placeholder="Type your answer" aria-label="Your answer" disabled={!connected || answered} enterkeyhint="send" />
							<button type="submit" class="q-send" aria-label="Send answer" disabled={!typed.trim() || !connected || answered}><SendHorizontal size={18} aria-hidden="true" /></button>
						</form>
					{:else}
						<div class="q-opts">
							{#each question.options as option, i (i)}
								<button type="button" class="q-opt" disabled={!connected || answered || !armed} onclick={() => answerQuestion(request, option.label)}>
									<span class="q-label">{option.label}</span>
									{#if option.description}<small>{option.description}</small>{/if}
								</button>
							{/each}
							<button type="button" class="q-opt type" disabled={!connected || answered} onclick={startTyping}><Pencil size={15} aria-hidden="true" />Type an answer</button>
						</div>
					{/if}
				{:else}
					<div class="q-form">
						<UserQuestion data={request} onRespond={onQuestion} disabled={!connected} />
					</div>
				{/if}
			{/if}
		</section>
	</div>
{/if}

<style>
	/* Sits on the composer's top edge, the same width as its box. */
	.prompt-dock { position: relative; z-index: 2; padding: 8px 0.5rem 0; margin-bottom: -10px; }
	.prompt {
		max-width: 48rem;
		margin: 0 auto;
		padding: 10px 12px 20px;
		border: 1px solid var(--border-strong);
		border-bottom: 0;
		border-radius: calc(var(--radius) * 1.6) calc(var(--radius) * 1.6) 0 0;
		background: color-mix(in oklch, var(--card) 55%, var(--muted));
		color: var(--foreground);
		max-height: min(46dvh, 26rem);
		overflow-y: auto;
		overscroll-behavior: contain;
		animation: dock-in 0.24s cubic-bezier(0.22, 1, 0.36, 1);
	}
	@keyframes dock-in { from { opacity: 0; transform: translateY(8px); } }
	.p-head { display: flex; align-items: center; gap: 8px; min-width: 0; min-height: 30px; padding: 0 2px; font-size: 13.5px; }
	.p-head :global(.t-ico) { flex-shrink: 0; color: var(--tool-bash, var(--success)); }
	.p-head :global(.t-ico.keyc) { color: var(--warning); }
	.p-head :global(.t-ico.q) { color: var(--primary); }
	.p-head strong { flex-shrink: 0; font-weight: 650; }
	.what { flex: none; white-space: nowrap; color: var(--muted-foreground); }
	.inline-cmd { display: none; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono); font-size: 12px; }
	.count {
		display: flex;
		align-items: center;
		gap: 2px;
		flex-shrink: 0;
		min-height: 44px;
		margin: -7px -6px -7px auto;
		padding: 0 6px 0 10px;
		border: 0;
		border-radius: 999px;
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 12px;
		font-variant-numeric: tabular-nums;
		cursor: pointer;
	}
	.count:hover { background: var(--hover-overlay); color: var(--foreground); }
	.cmd {
		max-height: 5.8em;
		margin: 8px 0 2px;
		padding: 8px 10px;
		overflow: auto;
		border: 1px solid var(--border-subtle);
		border-radius: calc(var(--radius) - 2px);
		background: var(--code-bg, var(--background));
		color: var(--foreground);
		font-family: var(--font-mono);
		font-size: 12.5px;
		line-height: 1.45;
		white-space: pre-wrap;
		word-break: break-all;
		user-select: text;
	}
	.cmd .p { color: var(--subtle-foreground); }
	.p-body { margin: 4px 2px 6px; font-size: 13.5px; line-height: 1.45; overflow-wrap: anywhere; }
	.q-text { font-weight: 550; }
	.p-note { margin: 6px 2px 0; font-size: 12.5px; color: var(--muted-foreground); }
	.check-row { display: flex; align-items: center; gap: 10px; width: 100%; min-height: 44px; padding: 0 2px; border: 0; background: transparent; color: var(--foreground); font: inherit; font-size: 13px; text-align: left; cursor: pointer; }
	.box { display: grid; place-items: center; flex: none; width: 20px; height: 20px; border: 1.5px solid var(--border-strong); border-radius: 6px; color: var(--primary-foreground); }
	.check-row[aria-checked='true'] .box { background: var(--primary); border-color: var(--primary); }
	.check-row:not([aria-checked='true']) .box :global(svg) { visibility: hidden; }
	.p-result { display: flex; align-items: center; justify-content: center; gap: 8px; min-height: 56px; font-size: 14px; font-weight: 600; }
	.p-result.ok { color: var(--success, var(--primary)); }
	.p-result.no { color: var(--destructive); }
	.review-btn { width: 100%; min-height: 48px; margin-top: 8px; border: 1px solid var(--border-strong); border-radius: var(--radius); background: var(--muted); color: var(--foreground); font: inherit; font-size: 14px; font-weight: 600; cursor: pointer; }
	.review-btn:hover { background: var(--accent); }
	.q-opts { display: flex; flex-direction: column; gap: 4px; margin-top: 8px; }
	.q-opt {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		justify-content: center;
		gap: 2px;
		min-height: 48px;
		padding: 6px 12px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--card);
		color: var(--foreground);
		font: inherit;
		font-size: 14px;
		text-align: left;
		cursor: pointer;
	}
	.q-opt small { font-size: 12px; color: var(--muted-foreground); }
	.q-opt:hover:not(:disabled) { background: var(--hover-overlay); }
	.q-opt:disabled { cursor: default; opacity: 0.7; }
	.q-opt.type { flex-direction: row; align-items: center; justify-content: flex-start; gap: 8px; border-style: dashed; color: var(--muted-foreground); }
	.q-type { display: flex; gap: 6px; margin-top: 8px; }
	.q-type input {
		flex: 1;
		min-width: 0;
		min-height: 44px;
		padding: 0 12px;
		border: 1px solid var(--border-strong);
		border-radius: var(--radius);
		background: var(--card);
		color: var(--foreground);
		font: inherit;
		font-size: 16px;
	}
	.q-type input:focus { outline: 2px solid var(--ring); outline-offset: 1px; }
	.q-send { display: grid; place-items: center; width: 44px; height: 44px; border: 0; border-radius: var(--radius); background: var(--primary); color: var(--primary-foreground); cursor: pointer; }
	.q-send:disabled { background: var(--muted); color: var(--subtle-foreground); cursor: default; }
	.q-form { margin-top: 6px; }
	.count:focus-visible, .check-row:focus-visible, .review-btn:focus-visible, .q-opt:focus-visible, .q-send:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	/* Typing with the keyboard up: keep the decision, drop to one line of context. */
	:global(.input-slot:has(.ProseMirror-focused)) .prompt .cmd,
	:global(.input-slot:has(.ProseMirror-focused)) .prompt .check-row,
	:global(.input-slot:has(.ProseMirror-focused)) .prompt .p-note,
	:global(.input-slot:has(.ProseMirror-focused)) .prompt .q-opts { display: none; }
	:global(.input-slot:has(.ProseMirror-focused)) .prompt .inline-cmd { display: block; }
	:global(.input-slot:has(.ProseMirror-focused)) .prompt .what { display: none; }

	@media (prefers-reduced-motion: reduce) {
		.prompt { animation: none; }
	}
</style>
