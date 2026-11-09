<script lang="ts">
	/**
	 * One saved memory. Text and when-line stay in the form until Save. A save
	 * or delete is checked against the version the form was opened from, so a
	 * newer version is never overwritten and the draft is never lost to one.
	 */
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import TextField from '../rows/TextField.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { agoText, compact, dateText, savedByText, type MemoryModel } from './memory-model.svelte';

	interface Props {
		model: MemoryModel;
		id: string;
	}

	let { model, id }: Props = $props();

	const CATEGORY = 'memory' as const;
	const uid = $props.id();
	// Forms are created outside rendering; the collection keys this component by id.
	// svelte-ignore state_referenced_locally
	model.ensure(id);

	const item = $derived(model.item(id));
	const form = $derived(model.forms[id]);
	const dirty = $derived(model.dirty(id));
	const saving = $derived(model.saving === id);
	const problems = $derived(model.problems(id));
	const error = $derived(model.errors[id]);
	const remote = $derived(model.remote[id]);
	const textLength = $derived(compact(form?.text ?? '').length);
	const whenLength = $derived(compact(form?.when ?? '').length);

	const status = $derived.by(() => {
		if (dirty) return { tone: 'warn', text: 'Unsaved changes' } as const;
		if (item?.needs_review) return { tone: 'warn', text: 'Needs review' } as const;
		if (item?.status === 'archived') return { tone: 'off', text: 'Archived' } as const;
		return { tone: 'ok', text: 'Active' } as const;
	});

	const subtitle = $derived(
		item
			? [
					item.always ? 'Applies to every chat' : '',
					item.this_worker_only ? 'Only on this worker' : '',
					item.status === 'active' && item.expires_at ? `Archived automatically if unused by ${dateText(item.expires_at)}` : ''
				]
					.filter(Boolean)
					.join(' · ')
			: ''
	);

	function lastUsed(uses: number, at: number | null): string {
		if (!uses && !at) return 'Never';
		const times = `${uses.toLocaleString()} ${uses === 1 ? 'time' : 'times'}`;
		const ago = agoText(at);
		return ago ? `${ago}, ${times}` : times;
	}

	const notes = $derived.by(() => {
		if (!item) return [];
		const lines: string[] = [];
		if (item.needs_review) lines.push('This memory conflicts with another one, or a newer one replaced it. Saving your version settles it.');
		if (item.status === 'archived') lines.push('This memory is archived, so agents aren’t using it. Saving makes it active again.');
		if (item.tier === 'agent') lines.push('An agent saved this. Saving makes it yours, and it’s kept until you change it.');
		if (item.tier === 'learned') lines.push('This was learned automatically. Saving makes it yours, and it’s kept until you change it.');
		return lines;
	});

	async function save() {
		if (await model.save(id)) flashSaved();
	}

	async function remove() {
		if (!(await model.remove(id))) return;
		// On a phone the detail is its own Back step; pop it so Back can't reopen a deleted memory.
		const pop = settingsNav.stacked && settingsNav.view === 'detail' && settingsNav.depth >= 2;
		settingsNav.clearSelection(CATEGORY);
		if (pop) settingsNav.back();
	}
</script>

{#if form}
	<div class="mem-detail">
		<DetailPane
			name="Saved memory"
			status={status.tone}
			statusText={status.text}
			badge={item?.scope_label}
			{subtitle}
			category={CATEGORY}
			item={id}
			phonePrimary="inline"
			facts={item
				? [
						{ label: 'Saved by', value: savedByText(item) },
						{ label: 'Project', value: item.scope_label },
						{ label: 'Created', value: dateText(item.created_at) || 'Unknown' },
						{ label: 'Last used', value: lastUsed(item.uses, item.last_recalled_at) }
					]
				: []}
			danger={item
				? [
						{
							label: 'Delete this memory',
							hint: 'Removed on all your workers',
							button: 'Delete',
							question: 'Delete this permanently on all your workers? Agents won’t be able to save this same fact again. Past chats are not erased.',
							onConfirm: remove
						}
					]
				: []}
		>
			{#snippet primary()}
				{#if dirty}
					<button type="button" class="st-btn ghost" disabled={saving} onclick={() => model.discard(id)}>Discard</button>
				{/if}
				<button type="button" class="st-btn primary" disabled={!model.canSave(id)} onclick={save}>
					{saving ? 'Saving…' : 'Save'}
				</button>
			{/snippet}
			{#snippet banner()}
				{#if remote === 'deleted'}
					<InlineBanner tone="warn">This memory was deleted elsewhere, so these edits can’t be saved. Copy anything you want to keep.</InlineBanner>
				{:else if remote === 'changed' && !error}
					<InlineBanner tone="warn">
						Changed elsewhere, your edits kept
						{#snippet actions()}
							<button type="button" class="st-btn sm" onclick={() => model.loadLatest(id)}>Load latest</button>
						{/snippet}
					</InlineBanner>
				{/if}
				{#if error}
					<InlineBanner tone="warn">
						<span role="alert">{error.message}{error.conflict ? ' Your edits are still here; loading the latest version replaces them.' : ''}</span>
						{#snippet actions()}
							{#if error.conflict}
								<button type="button" class="st-btn sm" onclick={() => model.loadLatest(id)}>Load latest</button>
							{/if}
						{/snippet}
					</InlineBanner>
				{/if}
				{#if notes.length}
					<InlineBanner tone="info" compact>{notes.join(' ')}</InlineBanner>
				{/if}
			{/snippet}
			{#snippet children()}
				<RowGroup title="Memory">
					<SettingRow label="What to remember">
						{#snippet expanded()}
							<textarea
								class="st-area mem-text"
								aria-label="What to remember"
								aria-invalid={problems.text ? 'true' : undefined}
								aria-describedby="{uid}-text"
								bind:value={form.text}
							></textarea>
							<div class="mem-foot" id="{uid}-text">
								<span class="mem-problem" role={problems.text ? 'alert' : undefined}>{dirty ? (problems.text ?? '') : ''}</span>
								<span class="mem-count" class:over={textLength > model.limits.text_chars}>{textLength.toLocaleString()} / {model.limits.text_chars.toLocaleString()}</span>
							</div>
						{/snippet}
					</SettingRow>
					<SettingRow label="When this applies" hint="When agents should load this memory">
						{#snippet expanded()}
							<TextField
								bind:value={form.when}
								label="When this applies"
								placeholder="For example: when writing release notes"
								width="full"
								invalid={!!problems.when}
								errorId="{uid}-when"
							/>
							<div class="mem-foot" id="{uid}-when">
								<span class="mem-problem" role={problems.when ? 'alert' : undefined}>{dirty ? (problems.when ?? '') : ''}</span>
								<span class="mem-count" class:over={whenLength > model.limits.when_chars}>{whenLength.toLocaleString()} / {model.limits.when_chars.toLocaleString()}</span>
							</div>
						{/snippet}
					</SettingRow>
				</RowGroup>
			{/snippet}
		</DetailPane>
	</div>
{/if}

<style>
	:global(.settings-shell) textarea.st-area.mem-text {
		min-height: 120px;
		max-height: 420px;
		field-sizing: content;
		overflow-wrap: anywhere;
	}
	.mem-foot {
		display: flex;
		justify-content: space-between;
		gap: 12px;
		margin-top: 6px;
		font-size: 12px;
		color: var(--st-mfg);
	}
	.mem-problem {
		color: var(--st-destructive);
		min-width: 0;
	}
	.mem-count {
		margin-left: auto;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}
	.mem-count.over {
		color: var(--st-destructive);
		font-weight: 500;
	}
	/* The delete question is a full sentence; let it wrap. */
	.mem-detail :global(.st-ask) {
		white-space: normal;
		text-align: right;
	}
	@media (max-width: 600px) {
		.mem-detail :global(.st-ask) {
			text-align: left;
		}
	}
</style>
