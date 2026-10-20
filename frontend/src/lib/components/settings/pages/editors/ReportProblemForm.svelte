<script lang="ts" module>
	import { EXCERPT_MAX, type ReportKind } from '$lib/api/support';

	/** Longest description. The report API takes up to 4,000; this is the title, so keep it short. */
	export const PROBLEM_MAX = 1000;
	/** Longest reason: the report's note. */
	export const REASON_MAX = 2000;

	export type ReportFormResult =
		| { kind: 'problem'; description: string }
		| { kind: 'ai_content'; excerpt: string; reason: string; sessionId: string | null };
</script>

<script lang="ts">
	import { untrack } from 'svelte';
	/**
	 * ReportProblemForm: what to report, opened under System > Help.
	 *
	 * Opened from the row it asks what went wrong with the app (a description).
	 * The Android app's "Report message" (a chat reply's ⋯ menu) opens it as an
	 * AI content report instead (`kind: 'ai_content'`): the AI's output, filled
	 * in, and why it is reported. There is no picker between the two, so offensive
	 * or harmful AI content is reported only from the message itself, and only
	 * in the Android app (Google Play generative-AI policy).
	 * Continue hands the input to the support report dialog, which shows exactly
	 * what will be sent; nothing is sent from here.
	 * Enter continues from the one-line-ish fields, Shift+Enter starts a new
	 * line; the excerpt takes Enter as a new line (pasted text).
	 */
	interface Props {
		kind?: ReportKind;
		description: string;
		excerpt?: string;
		reason?: string;
		/** The session a prefilled excerpt came from. */
		sessionId?: string | null;
		onContinue: (input: ReportFormResult) => void;
		onCancel: () => void;
	}

	let {
		kind = 'problem',
		description = $bindable(''),
		excerpt = $bindable(''),
		reason = $bindable(''),
		sessionId = null,
		onContinue,
		onCancel
	}: Props = $props();

	const uid = $props.id();
	const ids = {
		text: `${uid}-text`,
		err: `${uid}-err`,
		count: `${uid}-count`,
		excerpt: `${uid}-excerpt`,
		excerptErr: `${uid}-excerpt-err`,
		reason: `${uid}-reason`,
		reasonErr: `${uid}-reason-err`
	};

	let form = $state<HTMLFormElement | null>(null);
	let field = $state<HTMLTextAreaElement | null>(null);
	let excerptField = $state<HTMLTextAreaElement | null>(null);
	let reasonField = $state<HTMLTextAreaElement | null>(null);
	let missing = $state(false);
	let missingExcerpt = $state(false);
	let missingReason = $state(false);
	const length = $derived(description.length);

	function submit(event?: SubmitEvent) {
		event?.preventDefault();
		if (kind === 'ai_content') {
			const text = excerpt.trim();
			const why = reason.trim();
			missingExcerpt = !text;
			missingReason = !why;
			if (missingExcerpt || missingReason) {
				(missingExcerpt ? excerptField : reasonField)?.focus();
				return;
			}
			onContinue({ kind: 'ai_content', excerpt: text, reason: why, sessionId: sessionId ?? null });
			return;
		}
		const text = description.trim();
		if (!text) {
			missing = true;
			field?.focus();
			return;
		}
		missing = false;
		onContinue({ kind: 'problem', description: text });
	}

	function keydown(event: KeyboardEvent) {
		if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
			event.preventDefault();
			submit();
		}
	}

	function focusFirst() {
		if (kind === 'ai_content') (excerpt.trim() ? reasonField : excerptField)?.focus({ preventScroll: true });
		else field?.focus({ preventScroll: true });
	}

	// Opened from the row: type straight away, with the buttons in view.
	// Only on open: typing must not move focus between fields.
	$effect(() => {
		if (!form) return;
		untrack(focusFirst);
		form.scrollIntoView?.({ block: 'nearest' });
	});
</script>

<form class="rp" bind:this={form} novalidate aria-label={kind === 'ai_content' ? 'Report AI content' : 'Report a problem'} onsubmit={submit}>
	{#if kind === 'ai_content'}
		<label class="st-xh" for={ids.excerpt}>The AI output</label>
		<textarea
			bind:this={excerptField}
			id={ids.excerpt}
			class="st-area rp-text rp-excerpt"
			rows="4"
			maxlength={EXCERPT_MAX}
			aria-required="true"
			aria-invalid={missingExcerpt}
			aria-describedby={missingExcerpt ? ids.excerptErr : undefined}
			placeholder="Paste the part of the reply you are reporting"
			bind:value={excerpt}
			oninput={() => {
				if (missingExcerpt && excerpt.trim()) missingExcerpt = false;
			}}
		></textarea>
		{#if missingExcerpt}
			<p class="st-msg err" id={ids.excerptErr} role="alert">Paste the AI output you are reporting.</p>
		{/if}
		{#if sessionId}
			<p class="st-note rp-ref">From session <code>{sessionId.slice(0, 12)}</code>; the report includes this reference.</p>
		{/if}

		<label class="st-xh" for={ids.reason}>Why are you reporting it?</label>
		<textarea
			bind:this={reasonField}
			id={ids.reason}
			class="st-area rp-text"
			rows="2"
			maxlength={REASON_MAX}
			aria-required="true"
			aria-invalid={missingReason}
			aria-describedby={missingReason ? ids.reasonErr : undefined}
			placeholder="For example: it is hateful, sexual, violent or dangerous"
			bind:value={reason}
			oninput={() => {
				if (missingReason && reason.trim()) missingReason = false;
			}}
			onkeydown={keydown}
		></textarea>
		{#if missingReason}
			<p class="st-msg err" id={ids.reasonErr} role="alert">Say why you are reporting it.</p>
		{/if}
		<div class="st-acts">
			<button type="button" class="st-btn ghost" onclick={onCancel}>Cancel</button>
			<button type="submit" class="st-btn primary">Continue</button>
		</div>
		<p class="st-note">
			Next you see exactly what will be sent. Nothing leaves this worker until you press Send; without a Shuffle
			account you can save it and email it instead.
		</p>
	{:else}
		<label class="st-xh" for={ids.text}>What went wrong?</label>
		<textarea
			bind:this={field}
			id={ids.text}
			class="st-area rp-text"
			rows="3"
			maxlength={PROBLEM_MAX}
			aria-required="true"
			aria-invalid={missing}
			aria-describedby={missing ? `${ids.err} ${ids.count}` : ids.count}
			placeholder="For example: the voice call dropped after a minute"
			bind:value={description}
			oninput={() => {
				if (missing && description.trim()) missing = false;
			}}
			onkeydown={keydown}
		></textarea>
		{#if missing}
			<p class="st-msg err" id={ids.err} role="alert">Describe the problem first.</p>
		{/if}
		<div class="st-acts">
			<span class="st-left st-note rp-count" id={ids.count} class:full={length >= PROBLEM_MAX}>
				{length} / {PROBLEM_MAX}
			</span>
			<button type="button" class="st-btn ghost" onclick={onCancel}>Cancel</button>
			<button type="submit" class="st-btn primary">Continue</button>
		</div>
		<p class="st-note">Next you see exactly what will be sent. Nothing leaves this worker until you press Send.</p>
	{/if}
</form>

<style>
	.rp {
		min-width: 0;
	}

	/* A description is prose: the shared editor area is sized and set for
	   prompts (mono, 160px), and its selector outranks a single class. */
	.rp textarea.rp-text {
		min-height: 84px;
		font-family: inherit;
		font-size: 13.5px;
		line-height: 1.5;
		overflow-wrap: anywhere;
	}
	.rp textarea.rp-excerpt {
		min-height: 110px;
		max-height: 240px;
	}
	.rp textarea.rp-text[aria-invalid='true'] {
		border-color: var(--st-destructive);
	}
	.rp textarea.rp-text[aria-invalid='true']:focus {
		box-shadow: 0 0 0 3px var(--st-destructive-soft);
	}
	.rp textarea.rp-text::placeholder {
		color: var(--st-subtle);
	}
	.rp-ref {
		margin: 6px 0 0;
	}
	.rp-ref code {
		font-family: var(--st-mono);
		font-size: 12px;
	}
	.st-acts > .rp-count {
		margin: 0 auto 0 0;
		font-variant-numeric: tabular-nums;
	}
	.rp-count.full {
		color: var(--st-warning);
	}
</style>
