<script lang="ts">
	/**
	 * One behaviour rule, or a new one (`id` null). Saving makes a rule yours:
	 * the learner can read it but never rewrites it, and it stops fading. A
	 * rule the learner wrote can be undone: its earlier wording comes back, or
	 * a rule it just added is removed and not learned again.
	 */
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import {
		agoText,
		compact,
		dateText,
		validateRule,
		type AppliesTo,
		type LearningModel
	} from './learning-model.svelte';

	interface Props {
		model: LearningModel;
		id: string | null;
	}

	let { model, id }: Props = $props();

	const CATEGORY = 'learning' as const;
	const uid = $props.id();
	const rule = $derived(model.rule(id));
	// The detail is keyed by id, so the form starts from the rule it opened.
	// svelte-ignore state_referenced_locally
	let text = $state(rule?.text ?? '');
	// svelte-ignore state_referenced_locally
	let appliesTo = $state<AppliesTo>(rule?.applies_to ?? 'all');
	let touched = $state(false);

	const limit = $derived(model.data?.limits.rule_chars ?? 300);
	const options = $derived(model.data?.applies_to ?? [{ value: 'all' as AppliesTo, label: 'All models' }]);
	const dirty = $derived(!rule || compact(text) !== rule.text || appliesTo !== rule.applies_to);
	const problem = $derived(validateRule(text, limit));
	const busy = $derived(model.busy === (rule?.id ?? 'create'));
	const length = $derived(compact(text).length);

	const status = $derived.by(() => {
		if (!rule) return { tone: 'off', text: 'Not saved' } as const;
		if (rule && dirty) return { tone: 'warn', text: 'Unsaved changes' } as const;
		if (rule.new) return { tone: 'warn', text: 'New' } as const;
		return { tone: 'ok', text: rule.kept ? 'Kept' : 'Active' } as const;
	});

	function close() {
		const pop = settingsNav.stacked && settingsNav.view === 'detail' && settingsNav.depth >= 2;
		settingsNav.clearSelection(CATEGORY);
		if (pop) settingsNav.back();
	}

	async function save() {
		touched = true;
		if (problem) return;
		if (!rule) {
			const made = await model.create(text, appliesTo);
			if (made) {
				flashSaved();
				// Same Back step as the draft: a phone's Back then returns to the
				// list, not to an empty New rule form.
				settingsNav.replaceItem(made.id, CATEGORY);
			}
			return;
		}
		const saved = await model.save(rule, text, appliesTo);
		if (saved) {
			flashSaved();
			text = saved.text;
			appliesTo = saved.applies_to;
		}
	}

	async function undo() {
		if (!rule) return;
		const out = await model.undo(rule);
		if (out?.deleted) close();
		else if (out?.restored) {
			text = out.restored.text;
			appliesTo = out.restored.applies_to;
		}
	}

	async function remove() {
		if (rule && (await model.remove(rule))) close();
	}

	const notes = $derived.by(() => {
		if (!rule) return ['Rules you add are kept until you change them, and apply from the next chat.'];
		const lines: string[] = [];
		if (!rule.kept) lines.push('The learner wrote this from one of your chats. Saving it makes it yours, so it stays and the learner won’t change it.');
		if (!rule.kept && rule.expires_at) lines.push(`If it isn’t learned again, it fades on ${dateText(rule.expires_at)}.`);
		if (rule.kept) lines.push('You kept this rule. The learner can read it but never changes it.');
		return lines;
	});
</script>

<div class="lb-detail">
	<DetailPane
		name={rule ? 'Behaviour rule' : 'New rule'}
		status={status.tone}
		statusText={status.text}
		badge={rule?.applies_label}
		subtitle={rule ? `${rule.kept ? 'Kept by you' : 'Learned'}${rule.updated_at ? ` · ${agoText(rule.updated_at)}` : ''}` : 'Write a short “When X: do Y” rule'}
		category={CATEGORY}
		item={id ?? 'new-rule'}
		phonePrimary="inline"
		facts={rule
			? [
					{ label: 'Applies to', value: rule.applies_label },
					{ label: 'Where', value: rule.scope_label },
					{ label: 'Written', value: dateText(rule.created_at) || 'Unknown' },
					{ label: 'Changed', value: dateText(rule.updated_at) || 'Unknown' }
				]
			: []}
		danger={rule
			? [
					{
						label: 'Delete this rule',
						hint: 'Removed on all your workers',
						button: 'Delete',
						question: 'Delete this rule on all your workers? The learner won’t write the same rule again.',
						onConfirm: remove
					}
				]
			: []}
	>
		{#snippet primary()}
			<button type="button" class="st-btn primary" disabled={busy || (!!rule && !dirty && rule.kept) || (touched && !!problem)} onclick={save}>
				{busy ? 'Saving…' : !rule ? 'Add rule' : dirty ? 'Save' : rule.kept ? 'Kept' : 'Keep'}
			</button>
		{/snippet}
		{#snippet banner()}
			{#if rule?.new && !rule.kept}
				<InlineBanner tone="info">
					{rule.previous_text ? 'The learner changed this rule' : 'The learner added this rule'} {agoText(rule.updated_at)}.
					{#snippet actions()}
						<button type="button" class="st-btn sm" disabled={busy} onclick={undo}>Undo</button>
					{/snippet}
				</InlineBanner>
			{/if}
			{#if model.error}
				<InlineBanner tone="warn"><span role="alert">{model.error}</span></InlineBanner>
			{/if}
		{/snippet}
		{#snippet children()}
			<RowGroup title="Rule">
				<SettingRow label="What the agent should do">
					{#snippet expanded()}
						<textarea
							class="st-area lb-text"
							aria-label="Rule text"
							placeholder="For example: When a test fails, read the whole log before retrying."
							aria-invalid={touched && problem ? 'true' : undefined}
							aria-describedby="{uid}-text"
							bind:value={text}
						></textarea>
						<div class="lb-foot" id="{uid}-text">
							<span class="lb-problem" role={touched && problem ? 'alert' : undefined}>{touched ? (problem ?? '') : ''}</span>
							<span class="lb-len" class:over={length > limit}>{length.toLocaleString()} / {limit.toLocaleString()}</span>
						</div>
					{/snippet}
				</SettingRow>
				<SettingRow label="Applies to" hint="Every model, or one model family">
					<SelectField label="Applies to" value={appliesTo} {options} width={200} onchange={(v) => (appliesTo = v as AppliesTo)} />
				</SettingRow>
				{#if rule?.previous_text && !rule.kept}
					<SettingRow label="Before the learner’s change" hint={rule.previous_text} wrapHint />
				{/if}
			</RowGroup>
			{#if notes.length}
				<div class="lb-notes"><InlineBanner tone="info" compact>{notes.join(' ')}</InlineBanner></div>
			{/if}
		{/snippet}
	</DetailPane>
</div>

<style>
	:global(.settings-shell) textarea.st-area.lb-text {
		min-height: 96px;
		max-height: 320px;
		field-sizing: content;
		overflow-wrap: anywhere;
	}
	.lb-foot {
		display: flex;
		justify-content: space-between;
		gap: 12px;
		margin-top: 6px;
		font-size: 12px;
		color: var(--st-mfg);
	}
	.lb-problem {
		color: var(--st-destructive);
		min-width: 0;
	}
	.lb-len {
		margin-left: auto;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}
	.lb-len.over {
		color: var(--st-destructive);
		font-weight: 500;
	}
	.lb-notes {
		margin-top: 8px;
	}
	.lb-detail :global(.st-ask) {
		white-space: normal;
		text-align: right;
	}
</style>
