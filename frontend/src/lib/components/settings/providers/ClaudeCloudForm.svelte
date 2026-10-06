<script lang="ts">
	/**
	 * The "Cloud provider or gateway" form: one method (Amazon Bedrock, Google
	 * Vertex AI, Microsoft Foundry or an LLM gateway), its values, and the
	 * warning the owner must acknowledge before saving or turning it on.
	 *
	 * Secrets are write-only: the inputs start blank, a stored one says
	 * "Stored", and leaving it blank keeps it. Nothing here reads a secret back.
	 */
	import type { ClaudeAuthMode, ClaudeCloudConfig, ClaudeCloudMethodId } from '$lib/api/claude-auth';
	import { getErrorMessage } from '$lib/api/client';
	import { providers } from '$lib/stores/providers.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import TextField from '../rows/TextField.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import '../pages/editors/editors.css';
	import {
		buildValues,
		canSave,
		initialChoice,
		initialInputs,
		methodOf,
		missingFields,
		placeholderFor,
		visibleFields
	} from './claude-cloud';

	interface Props {
		config: ClaudeCloudConfig;
		mode: ClaudeAuthMode;
		/** Show the warnings here (the setup); the detail shows them in its banner instead. */
		showWarning?: boolean;
		onSaved: (config: ClaudeCloudConfig, activated: boolean) => void | Promise<void>;
		onCancel?: () => void;
	}

	let { config, mode, showWarning = true, onSaved, onCancel }: Props = $props();
	const access = getSettingsAccess();

	let methodId = $state<ClaudeCloudMethodId | ''>('');
	let choice = $state<string | null>(null);
	let inputs = $state<Record<string, string>>({});
	let acknowledged = $state(false);
	let busy = $state(false);
	let error = $state('');

	function reset(id: ClaudeCloudMethodId | '', from: ClaudeCloudConfig) {
		methodId = id;
		const m = methodOf(from, id || null);
		choice = m ? initialChoice(from, m) : null;
		inputs = m ? initialInputs(from, m) : {};
		error = '';
	}
	// Start on the saved method, once the config is known.
	let started = false;
	$effect(() => {
		if (started) return;
		started = true;
		reset(config.method ?? '', config);
	});

	const method = $derived(methodOf(config, methodId || null));
	const fields = $derived(method ? visibleFields(method, choice) : []);
	const missing = $derived(method ? missingFields(config, method, choice, inputs) : []);
	const ready = $derived(canSave(config, method, choice, inputs, acknowledged));
	const isActive = $derived(mode === 'cloud');
	const methodOptions = $derived([
		{ value: '', label: 'Choose a method', disabled: true },
		...config.methods.map((m) => ({ value: m.id, label: m.label }))
	]);
	const gatewayNotice = $derived(methodId === 'gateway' && (showWarning || config.method !== 'gateway'));

	async function save(activate: boolean) {
		if (!method || !ready || busy) return;
		busy = true;
		error = '';
		try {
			const next = await providers.saveClaudeCloud(method.id, buildValues(method, choice, inputs), activate);
			reset(method.id, next);
			await onSaved(next, activate);
		} catch (e) {
			error = getErrorMessage(e, 'Save failed');
		} finally {
			busy = false;
		}
	}
</script>

{#if showWarning}
	<InlineBanner tone="warn">{config.warning}</InlineBanner>
{/if}
{#if gatewayNotice}
	<InlineBanner tone="warn">{config.gateway_warning}</InlineBanner>
{/if}

<RowGroup title="Method">
	<SettingRow label="Method" hint="One at a time">
		<SelectField
			value={methodId}
			options={methodOptions}
			disabled={busy}
			label="Cloud method"
			onchange={(v) => reset(v as ClaudeCloudMethodId, config)}
		/>
	</SettingRow>
	{#if method}
		<SettingRow label="Setup guide" hint="Claude Code docs">
			<a class="st-btn ghost sm" href={method.docs} target="_blank" rel="noopener noreferrer">Open</a>
		</SettingRow>
		{#if method.choices.length}
			<SettingRow label="Sign in with">
				<Segmented
					options={method.choices.map((c) => ({ value: c.id, label: c.label }))}
					value={choice ?? method.choices[0].id}
					size="sm"
					disabled={busy}
					onchange={(v: string) => (choice = v)}
				/>
			</SettingRow>
		{/if}
	{/if}
</RowGroup>

{#if method}
	<RowGroup title="Values">
		{#each fields as f (f.name)}
			{#if f.kind === 'json'}
				<SettingRow label={f.label} hint={f.env} hintMono>
					{#snippet expanded()}
						<textarea
							class="st-area"
							aria-label={f.label}
							spellcheck="false"
							autocomplete="off"
							placeholder={placeholderFor(config, method, f)}
							disabled={busy || access.readOnly}
							bind:value={inputs[f.name]}
						></textarea>
					{/snippet}
				</SettingRow>
			{:else}
				<SettingRow label={f.label} hint={f.env} hintMono>
					<TextField
						bind:value={inputs[f.name]}
						type={f.secret ? 'password' : f.kind === 'url' ? 'url' : 'text'}
						mono
						width={240}
						placeholder={placeholderFor(config, method, f)}
						disabled={busy || access.readOnly}
						label={f.label}
					/>
				</SettingRow>
			{/if}
		{/each}
	</RowGroup>

	<div class="st-xlist">
		<label class="st-check">
			<input type="checkbox" bind:checked={acknowledged} disabled={busy || access.readOnly} />
			<span>I have read the warning above</span>
			<span></span>
		</label>
	</div>
	{#if acknowledged && missing.length}
		<p class="pv-msg">Still needed: {missing.join(', ')}</p>
	{/if}
{/if}

{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
<div class="pv-acts">
	{#if onCancel}
		<button type="button" class="st-btn ghost left" disabled={busy} onclick={onCancel}>Cancel</button>
	{/if}
	<!-- Read-only access (another device, an agent preview) gets no way to save. -->
	{#if !access.readOnly}
		<button type="button" class="st-btn" disabled={!ready || busy} onclick={() => save(false)}>Save</button>
		{#if !isActive}
			<button type="button" class="st-btn primary" disabled={!ready || busy} onclick={() => save(true)}>
				Save and use for Claude
			</button>
		{/if}
	{/if}
</div>
