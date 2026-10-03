<script lang="ts">
	/**
	 * One hook, or the draft from "Add hook". Edits stay in the form until
	 * Save (or "Add hook"); Enabled saves at once. Test runs the saved hook
	 * with sample event data, so it waits for unsaved edits to be saved.
	 */
	import { Play } from 'lucide-svelte';
	import { projects } from '$lib/stores/app-data.svelte';
	import type { HookEventType } from '$lib/api/hooks';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import TextField from '../rows/TextField.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import NumberField from '../rows/NumberField.svelte';
	import Switch from '../rows/Switch.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { relativeAgo, shortDate } from '../integrations/api-users.svelte';
	import { DRAFT_ID, EVENT_HINTS, EVENT_ORDER, hookIcon, isToolEvent, type HooksModel } from './hooks-model.svelte';

	interface Props {
		model: HooksModel;
		/** A hook id, or DRAFT_ID. */
		id: string;
	}

	let { model, id }: Props = $props();

	const CATEGORY = 'hooks' as const;
	// Forms are created outside rendering; the collection keys this component by id.
	// svelte-ignore state_referenced_locally
	model.ensure(id);

	const TYPE_OPTIONS = [
		{ value: 'bash', label: 'Bash' },
		{ value: 'webhook', label: 'Webhook' }
	] as const;
	const EVENT_OPTIONS = EVENT_ORDER.map((e) => ({ value: e, label: e }));

	const isDraft = $derived(id === DRAFT_ID);
	const hook = $derived(isDraft ? undefined : model.hook(id));
	const form = $derived(model.form(id));
	const dirty = $derived(model.dirty(id));
	const saving = $derived(model.saving === id);
	const testing = $derived(model.testing === id);
	const result = $derived(model.results[id]);
	const errors = $derived(model.errors[id] ?? []);
	const remote = $derived(model.remote[id]);
	const enabled = $derived(hook?.enabled ?? form?.enabled ?? true);

	const scopeOptions = $derived([
		{ value: '', label: 'App (all sessions)' },
		...$projects.map((p) => ({ value: p.id, label: p.name }))
	]);
	const scopeName = $derived.by(() => {
		const pid = form?.projectId;
		if (!pid) return 'App';
		return $projects.find((p) => p.id === pid)?.name ?? 'Project';
	});

	async function save() {
		const saved = await model.save(id);
		if (!saved) return;
		flashSaved();
		if (saved !== id) settingsNav.selectItem(saved, CATEGORY);
	}

	function cancelDraft() {
		model.discardDraft();
		settingsNav.clearSelection(CATEGORY);
	}

	async function remove() {
		if (await model.remove(id)) settingsNav.clearSelection(CATEGORY);
	}

	async function toggle(next: boolean) {
		if (isDraft) {
			if (form) form.enabled = next;
			return;
		}
		if (await model.setEnabled(id, next)) {
			flashSaved();
			return;
		}
		return false;
	}
</script>

{#if form}
	<DetailPane
		name={form.name.trim() || (isDraft ? 'New hook' : 'Untitled hook')}
		icon={hookIcon(form.type)}
		status={dirty && !isDraft ? 'warn' : isDraft ? 'busy' : enabled ? 'ok' : 'off'}
		statusText={isDraft ? 'Not added yet' : dirty ? 'Unsaved changes' : enabled ? 'Enabled' : 'Disabled'}
		badge={isDraft ? 'Hook' : `${scopeName} hook`}
		category={CATEGORY}
		item={id}
		facts={hook
			? [
					{ label: 'Event', value: hook.event },
					{
						label: 'Last run',
						value: hook.last_triggered_at ? relativeAgo(hook.last_triggered_at) || shortDate(hook.last_triggered_at) : 'Never'
					},
					{ label: 'Failures', value: hook.failure_count },
					{ label: 'Created', value: shortDate(hook.created_at) }
				]
			: []}
		danger={hook
			? [
					{
						label: 'Delete this hook',
						hint: 'It stops firing',
						button: 'Delete',
						question: `Delete ${hook.name}?`,
						onConfirm: remove
					}
				]
			: []}
	>
		{#snippet primary()}
			{#if isDraft}
				<button type="button" class="st-btn ghost" onclick={cancelDraft}>Cancel</button>
				<button type="button" class="st-btn primary" disabled={saving} onclick={save}>
					{saving ? 'Adding…' : 'Add hook'}
				</button>
			{:else}
				<button
					type="button"
					class="st-btn"
					disabled={dirty || testing || !hook}
					title={dirty ? 'Save first' : undefined}
					onclick={() => model.test(id)}
				>
					<Play />{testing ? 'Testing…' : 'Test'}
				</button>
				{#if dirty}
					<button type="button" class="st-btn ghost" disabled={saving} onclick={() => model.discard(id)}>Discard</button>
				{/if}
				<button type="button" class="st-btn primary" disabled={!dirty || saving} onclick={save}>
					{saving ? 'Saving…' : 'Save'}
				</button>
			{/if}
		{/snippet}
		{#snippet banner()}
			{#if remote === 'changed'}
				<InlineBanner tone="warn">
					Changed elsewhere, your edits kept
					{#snippet actions()}
						<button type="button" class="st-btn sm" onclick={() => model.loadLatest(id)}>Load latest</button>
					{/snippet}
				</InlineBanner>
			{:else if remote === 'deleted'}
				<InlineBanner tone="warn">Deleted elsewhere. Save adds it back.</InlineBanner>
			{/if}
			{#if errors.length}
				<InlineBanner tone="warn"><span role="alert">{errors.join('. ')}</span></InlineBanner>
			{/if}
			{#if result}
				<InlineBanner tone={result.success ? 'info' : 'warn'}>
					<span role="status">
						{#if result.success}
							Test passed{result.duration_ms != null ? ` in ${result.duration_ms} ms` : ''}
						{:else}
							Test failed: {result.error || 'no details'}
						{/if}
					</span>
					{#snippet actions()}
						<button type="button" class="st-btn ghost sm" onclick={() => model.dismissResult(id)}>Dismiss</button>
					{/snippet}
				</InlineBanner>
				{#if result.output}<pre class="out">{result.output}</pre>{/if}
			{/if}
		{/snippet}
		{#snippet children()}
			<RowGroup title="Trigger">
				<SettingRow label="Name">
					<TextField bind:value={form.name} placeholder="Block rm -rf" maxlength={100} width={260} />
				</SettingRow>
				<SettingRow label="Enabled">
					<Switch checked={enabled} onchange={toggle} />
				</SettingRow>
				<SettingRow label="Scope" hint={isDraft ? 'Fixed after adding' : undefined}>
					{#if isDraft}
						<SelectField
							value={form.projectId ?? ''}
							options={scopeOptions}
							onchange={(v) => (form.projectId = v || null)}
						/>
					{:else}
						<ValueText value={form.projectId ? `Project: ${scopeName}` : 'App (all sessions)'} tone="fg" />
					{/if}
				</SettingRow>
				<SettingRow label="Event" hint={EVENT_HINTS[form.event]}>
					<SelectField
						value={form.event}
						options={EVENT_OPTIONS}
						mono
						onchange={(v) => (form.event = v as HookEventType)}
					/>
				</SettingRow>
				{#if isToolEvent(form.event)}
					<SettingRow label="Tool matcher" hint="Regex; empty matches all">
						<TextField bind:value={form.matcher} placeholder="Bash or Write|Edit" mono width={220} />
					</SettingRow>
				{/if}
			</RowGroup>
			<RowGroup title="Action">
				<SettingRow label="Type">
					<Segmented options={TYPE_OPTIONS} bind:value={form.type} size="sm" />
				</SettingRow>
				{#if form.type === 'bash'}
					<SettingRow label="Command" hint="Event JSON in HOOK_DATA">
						{#snippet expanded()}
							<textarea
								class="st-area cmd"
								aria-label="Command"
								spellcheck="false"
								placeholder="~/.hooks/guard-rm.sh"
								bind:value={form.command}
							></textarea>
						{/snippet}
					</SettingRow>
					<SettingRow label="Timeout">
						<NumberField bind:value={form.timeoutSec} min={1} max={300} unit="s" />
					</SettingRow>
				{:else}
					<SettingRow label="URL" hint="Receives a POST">
						<TextField bind:value={form.url} type="url" placeholder="https://ntfy.sh/my-topic" mono width={300} />
					</SettingRow>
					<SettingRow label="Secret" hint="Signs each request" help="The signature is sent in the X-Hook-Signature header.">
						<TextField bind:value={form.secret} type="password" placeholder="Optional" mono width={220} />
					</SettingRow>
					<SettingRow label="Timeout">
						<NumberField bind:value={form.timeoutSec} min={1} max={300} unit="s" />
					</SettingRow>
				{/if}
			</RowGroup>
		{/snippet}
	</DetailPane>
{/if}

<style>
	.cmd {
		min-height: 88px;
	}
	.out {
		margin: -4px 0 10px;
		max-height: 180px;
		overflow: auto;
		padding: 8px 10px;
		border-radius: var(--st-radius-md);
		background: var(--st-muted);
		font-family: var(--st-mono);
		font-size: 12px;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
</style>
