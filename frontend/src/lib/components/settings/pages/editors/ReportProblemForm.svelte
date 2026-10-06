<script lang="ts" module>
	/** Longest description. The report API takes up to 4,000; this is the title, so keep it short. */
	export const PROBLEM_MAX = 1000;
</script>

<script lang="ts">
	/**
	 * ReportProblemForm: what went wrong, opened under System > Help > Report a
	 * problem. Continue hands the description to the support report dialog,
	 * which shows exactly what will be sent; nothing is sent from here.
	 * Enter continues, Shift+Enter starts a new line.
	 */
	interface Props {
		description: string;
		onContinue: (description: string) => void;
		onCancel: () => void;
	}

	let { description = $bindable(''), onContinue, onCancel }: Props = $props();

	const uid = $props.id();
	const fieldId = `${uid}-text`;
	const errorId = `${uid}-err`;
	const countId = `${uid}-count`;

	let form = $state<HTMLFormElement | null>(null);
	let field = $state<HTMLTextAreaElement | null>(null);
	let missing = $state(false);
	const length = $derived(description.length);

	function submit(event?: SubmitEvent) {
		event?.preventDefault();
		const text = description.trim();
		if (!text) {
			missing = true;
			field?.focus();
			return;
		}
		missing = false;
		onContinue(text);
	}

	function keydown(event: KeyboardEvent) {
		if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
			event.preventDefault();
			submit();
		}
	}

	// Opened from the row: type straight away, with the buttons in view.
	$effect(() => {
		field?.focus({ preventScroll: true });
		form?.scrollIntoView?.({ block: 'nearest' });
	});
</script>

<form class="rp" bind:this={form} novalidate aria-label="Report a problem" onsubmit={submit}>
	<label class="st-xh" for={fieldId}>What went wrong?</label>
	<textarea
		bind:this={field}
		id={fieldId}
		class="st-area rp-text"
		rows="3"
		maxlength={PROBLEM_MAX}
		aria-required="true"
		aria-invalid={missing}
		aria-describedby={missing ? `${errorId} ${countId}` : countId}
		placeholder="For example: the voice call dropped after a minute"
		bind:value={description}
		oninput={() => {
			if (missing && description.trim()) missing = false;
		}}
		onkeydown={keydown}
	></textarea>
	{#if missing}
		<p class="st-msg err" id={errorId} role="alert">Describe the problem first.</p>
	{/if}
	<div class="st-acts">
		<span class="st-left st-note rp-count" id={countId} class:full={length >= PROBLEM_MAX}>
			{length} / {PROBLEM_MAX}
		</span>
		<button type="button" class="st-btn ghost" onclick={onCancel}>Cancel</button>
		<button type="submit" class="st-btn primary">Continue</button>
	</div>
	<p class="st-note">Next you see exactly what will be sent. Nothing leaves this worker until you press Send.</p>
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
	.rp textarea.rp-text[aria-invalid='true'] {
		border-color: var(--st-destructive);
	}
	.rp textarea.rp-text[aria-invalid='true']:focus {
		box-shadow: 0 0 0 3px var(--st-destructive-soft);
	}
	.rp textarea.rp-text::placeholder {
		color: var(--st-subtle);
	}
	.st-acts > .rp-count {
		margin: 0 auto 0 0;
		font-variant-numeric: tabular-nums;
	}
	.rp-count.full {
		color: var(--st-warning);
	}
</style>
