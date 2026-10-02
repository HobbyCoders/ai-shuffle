<script lang="ts">
	/**
	 * CallPromptsEditor: the voice-call prompts, opened under Voice > Call
	 * prompts. Long text, so it keeps an explicit Save.
	 *
	 * The prompts and their factory text come from the server
	 * (`model_details.prompts`). A prompt saved equal to its factory text is
	 * stored empty, so it keeps following future improvements to the default.
	 * While edits are unsaved, a change from elsewhere does not replace them;
	 * the editor says so and offers "Load latest".
	 */
	import { RotateCcw } from 'lucide-svelte';
	import type { VoiceModelDetails, VoiceOptions } from '$lib/api/voice';
	import InlineBanner from '../../rows/InlineBanner.svelte';

	type PromptKey = 'instructions' | 'delegation_instructions';

	interface Props {
		options: VoiceOptions;
		details: VoiceModelDetails;
		saving: boolean;
		onSave: (patch: VoiceOptions) => Promise<boolean>;
		/** Closes the editor (after Discard or a successful Save). */
		onClose?: () => void;
	}

	let { options, details, saving, onSave, onClose }: Props = $props();

	const uid = $props.id();
	const prompts = $derived(details.prompts.filter((p): p is typeof p & { key: PromptKey } => !!p.key));

	function savedDraft(): Partial<Record<PromptKey, string>> {
		const value: Partial<Record<PromptKey, string>> = {};
		for (const prompt of prompts) value[prompt.key] = options[prompt.key] || prompt.default_text || prompt.text;
		return value;
	}

	let draft = $state<Partial<Record<PromptKey, string>>>({});
	let dirty = $state(false);
	/** Saved text when editing started; a different saved text means it changed elsewhere. */
	let base = $state('');
	const savedKey = $derived(JSON.stringify(savedDraft()));
	const changedElsewhere = $derived(dirty && base !== '' && savedKey !== base);

	$effect(() => {
		if (!dirty) {
			draft = savedDraft();
			base = savedKey;
		}
	});

	function change(key: PromptKey, value: string) {
		if (!dirty) base = savedKey;
		draft = { ...draft, [key]: value };
		dirty = true;
	}

	function discard() {
		dirty = false;
		draft = savedDraft();
		base = savedKey;
	}

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		const patch: VoiceOptions = {};
		for (const prompt of prompts) {
			const value = draft[prompt.key] ?? '';
			patch[prompt.key] = value === prompt.default_text ? '' : value;
		}
		if (await onSave(patch)) {
			dirty = false;
			onClose?.();
		}
	}
</script>

<form onsubmit={submit} aria-label="Call prompts">
	{#if changedElsewhere}
		<InlineBanner tone="warn">
			Changed elsewhere, your edits kept
			{#snippet actions()}
				<button type="button" class="st-btn sm" onclick={discard}>Load latest</button>
			{/snippet}
		</InlineBanner>
	{/if}
	{#each prompts as prompt (prompt.key)}
		<div class="st-xh">
			<label for="{uid}-{prompt.key}">{prompt.label}</label>
			<span class="st-sp"></span>
			<button
				type="button"
				class="st-btn ghost sm"
				aria-label="Restore default {prompt.label}"
				disabled={saving || draft[prompt.key] === prompt.default_text}
				onclick={() => change(prompt.key, prompt.default_text ?? '')}><RotateCcw />Restore default</button
			>
		</div>
		<textarea
			id="{uid}-{prompt.key}"
			class="st-area"
			rows={prompt.key === 'instructions' ? 7 : 5}
			maxlength="16000"
			value={draft[prompt.key] ?? ''}
			disabled={saving}
			oninput={(e) => change(prompt.key, e.currentTarget.value)}
		></textarea>
	{/each}
	{#if details.prompt_note}<p class="st-note">{details.prompt_note}</p>{/if}
	<div class="st-acts">
		<button type="button" class="st-btn ghost" disabled={saving || !dirty} onclick={discard}>Discard changes</button>
		<button type="submit" class="st-btn primary" disabled={saving || !dirty}>{saving ? 'Saving…' : 'Save prompts'}</button>
	</div>
</form>
