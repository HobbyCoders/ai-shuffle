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
	 * Two kinds: a problem with the app (a description), or offensive or
	 * harmful AI content (the AI's output and why it is reported). A chat
	 * message's Report button opens this form as an AI content report with the
	 * message filled in. Continue hands the input to the support report dialog,
	 * which shows exactly what will be sent; nothing is sent from here.
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
		kind = $bindable('problem'),
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
		reasonErr: `${uid}-reason-err`,
		kind: `${uid}-kind`
	};

	const KINDS: { value: ReportKind; label: string; hint: string }[] = [
		{ value: 'problem', label: 'A problem with the app', hint: 'Something broke or behaves wrong' },
		{ value: 'ai_content', label: 'Offensive or harmful AI content', hint: 'Reviewed by a person at HobbyCoders' }
	];

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

	function pick(value: ReportKind) {
		kind = value;
		missing = missingExcerpt = missingReason = false;
		requestAnimationFrame(focusFirst);
	}
</script>

<form class="rp" bind:this={form} novalidate aria-label="Report a problem or AI content" onsubmit={submit}>
	<fieldset class="rp-kinds">
		<legend class="st-xh rp-legend" id={ids.kind}>What are you reporting?</legend>
		<div class="rp-kind-grid">
			{#each KINDS as option (option.value)}
				<label class="rp-kind" class:on={kind === option.value} data-kind={option.value}>
					<input
						type="radio"
						name={ids.kind}
						value={option.value}
						checked={kind === option.value}
						onchange={() => pick(option.value)}
					/>
					<span class="rp-dot" aria-hidden="true"></span>
					<span class="rp-kind-text">
						<span class="rp-kind-label">{option.label}</span>
						<span class="rp-kind-hint">{option.hint}</span>
					</span>
				</label>
			{/each}
		</div>
	</fieldset>

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

	/* ---- kind picker: two cards, a radio dot each ---- */
	.rp-kinds {
		margin: 0;
		padding: 0;
		border: 0;
		min-width: 0;
	}
	.rp .rp-legend {
		margin-top: 4px;
		padding: 0;
	}
	.rp-kind-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 8px;
	}
	.rp-kind {
		position: relative;
		display: flex;
		align-items: flex-start;
		gap: 10px;
		min-height: 44px;
		padding: 10px 12px;
		border: 1px solid var(--st-border);
		border-radius: var(--st-radius-md);
		background: var(--st-card);
		cursor: pointer;
		transition:
			border-color 160ms ease,
			background-color 160ms ease,
			box-shadow 160ms ease;
	}
	.rp-kind:hover {
		border-color: var(--st-border-strong);
		background: color-mix(in oklch, var(--st-card) 92%, var(--st-fg));
	}
	.rp-kind.on {
		border-color: var(--st-primary-line);
		background: var(--st-primary-soft);
	}
	.rp-kind[data-kind='ai_content'].on {
		border-color: var(--st-destructive-line);
		background: var(--st-destructive-soft);
	}
	/* The native radio stays in the accessibility tree; the dot draws it. */
	.rp-kind input {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		margin: 0;
		opacity: 0;
		cursor: pointer;
	}
	.rp-kind:has(input:focus-visible) {
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--st-primary) 28%, transparent);
	}
	.rp-dot {
		flex-shrink: 0;
		width: 16px;
		height: 16px;
		margin-top: 1px;
		border-radius: 999px;
		border: 1.5px solid var(--st-border-strong);
		background: var(--st-bg);
		transition:
			border-color 160ms ease,
			box-shadow 160ms ease;
	}
	.rp-kind.on .rp-dot {
		border-color: var(--st-primary);
		box-shadow: inset 0 0 0 3.5px var(--st-bg), inset 0 0 0 8px var(--st-primary);
	}
	.rp-kind[data-kind='ai_content'].on .rp-dot {
		border-color: var(--st-destructive);
		box-shadow: inset 0 0 0 3.5px var(--st-bg), inset 0 0 0 8px var(--st-destructive);
	}
	.rp-kind-text {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}
	.rp-kind-label {
		font-size: 13px;
		font-weight: 550;
		color: var(--st-fg);
		line-height: 1.3;
	}
	.rp-kind-hint {
		font-size: 12px;
		color: var(--st-mfg);
		line-height: 1.35;
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

	@media (max-width: 639px) {
		.rp-kind-grid {
			grid-template-columns: minmax(0, 1fr);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.rp-kind,
		.rp-dot {
			transition: none;
		}
	}
</style>
