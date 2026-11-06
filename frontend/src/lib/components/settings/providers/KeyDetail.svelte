<script lang="ts">
	/**
	 * An API key (OpenAI, Grok, OpenRouter) or a custom gateway's key.
	 * Overview is the connection (name, endpoint, token, dialect, context
	 * window, max output); fields save on blur or Enter, the token through
	 * Replace. The stored token is write-only: it reads "Saved", never a mask.
	 * Every save passes the curated models back with their metadata, including
	 * per-model local tuning. A custom gateway speaking OpenAI chat also gets a
	 * Tuning tab (LocalTuning) for a declared llama.cpp or vLLM server.
	 */
	import { onMount } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { checkProviderKey, isNativeProvider, type ModelProvider, type ModelProviderCreate } from '$lib/api/model-providers';
	import { nativeOption } from './native-options.svelte';
	import {
		getOpenRouterUsage,
		supportsOpenRouterBilling,
		type OpenRouterUsageStatus
	} from '$lib/api/openrouter-billing';
	import { supportsXAIBalance } from '$lib/api/provider-balances';
	import { providers } from '$lib/stores/providers.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { NATIVE_IMAGE_HELP } from '$lib/settings/entries/providers';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { DangerAction } from '../shell/DangerZone.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import TextField from '../rows/TextField.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import ModelCuration from './ModelCuration.svelte';
	import LocalTuning from './LocalTuning.svelte';
	import KeyUsage from './tabs/KeyUsage.svelte';
	import { passthroughModels } from './curation';
	import { supportsLocalTuning, tunedModels } from './local-tuning';
	import { hostOf, supportsNativeImages, type CredentialView, type ProviderView } from './providers-model';
	import CredentialControl from './CredentialControl.svelte';
	import SavedSecretRow from './SavedSecretRow.svelte';
	import { detailLine } from './credential-display';

	interface Props {
		credential: CredentialView;
		provider: ProviderView;
	}

	let { credential, provider }: Props = $props();

	type Dialect = 'anthropic' | 'openai';
	const DIALECTS: { value: Dialect; label: string }[] = [
		{ value: 'anthropic', label: 'Anthropic Messages' },
		{ value: 'openai', label: 'OpenAI chat' }
	];

	const row = $derived(credential.row!);
	const gateway = $derived(credential.flavor === 'gateway');
	const native = $derived(isNativeProvider(row.provider_type));
	const option = $derived(nativeOption(row.provider_type, row.auth_mode === 'plan_key') ?? row.native_provider);
	let keyStatus = $state('');
	let checking = $state(false);
	async function checkKey() {
		if (checking) return;
		checking = true;
		keyStatus = '';
		error = '';
		try {
			const result = await checkProviderKey(row.id);
			keyStatus = result.status === 'valid' ? 'Key is valid' : 'Saved, but unverified. Try checking again later.';
		} catch (e) { error = getErrorMessage(e, 'Could not check key'); }
		finally { checking = false; }
	}
	const dialect = $derived<Dialect>(row.api_dialect === 'openai' ? 'openai' : 'anthropic');
	const tuning = $derived(gateway && supportsLocalTuning(row));

	let saving = $state(false);
	let error = $state('');
	let credits = $state<OpenRouterUsageStatus | null>(null);

	const openRouter = $derived(supportsOpenRouterBilling(row));
	const balance = $derived(
		openRouter && credits?.credits
			? `$${credits.credits.balance_remaining.toFixed(2)}`
			: null
	);

	const title = $derived(row.name?.trim() || credential.name);
	const line = $derived(
		detailLine(
			`${row.models.length} ${row.models.length === 1 ? 'model' : 'models'}`,
			gateway ? (dialect === 'openai' ? 'OpenAI chat' : 'Anthropic Messages') : hostOf(row.base_url),
			balance && `${balance} left`,
			!row.auth_token_set && 'No token'
		)
	);

	/** Positive integer or null; '' is null; anything else is invalid. */
	function parseOptionalInt(value: string): number | null | 'invalid' {
		const t = value.trim();
		if (!t) return null;
		const n = Number(t);
		return Number.isFinite(n) && n > 0 ? Math.floor(n) : 'invalid';
	}

	/** Save one change; the rest of the row passes through unchanged. */
	async function save(patch: Partial<ModelProviderCreate>) {
		if (saving) return;
		const next = {
			name: (patch.name ?? row.name).trim(),
			base_url: (patch.base_url ?? row.base_url).trim()
		};
		if (!next.name || !next.base_url) {
			error = 'Name and endpoint are both required.';
			return;
		}
		saving = true;
		error = '';
		try {
			const body: ModelProviderCreate = native ? { name: next.name, provider_type: row.provider_type, ...patch } : {
				name: next.name,
				provider_type: row.provider_type,
				base_url: next.base_url,
				// Only a real custom gateway authors its dialect.
				...(gateway ? { api_dialect: patch.api_dialect ?? dialect } : {}),
				models: passthroughModels(row.models),
				default_model: row.default_model,
				small_fast_model: row.small_fast_model,
				context_window: 'context_window' in patch ? patch.context_window : row.context_window,
				max_output_tokens: 'max_output_tokens' in patch ? patch.max_output_tokens : row.max_output_tokens,
				...(patch.auth_token ? { auth_token: patch.auth_token } : {})
			};
			await providers.saveProvider(body, row.id);
			keyStatus = '';
			flashSaved();
			return true;
		} catch (e) {
			error = getErrorMessage(e, 'Save failed');
		} finally {
			saving = false;
		}
	}

	function saveNumber(field: 'context_window' | 'max_output_tokens', raw: string) {
		const value = parseOptionalInt(raw);
		if (value === 'invalid') {
			error = 'Use a whole number of tokens, or leave it empty.';
			return;
		}
		if (value === (row[field] ?? null)) return;
		void save({ [field]: value });
	}

	/** The API refuses local tuning on an Anthropic endpoint: say so before sending. */
	function changeDialect(next: Dialect) {
		const tuned = tunedModels(row.models);
		if (next !== 'openai' && tuned.length) {
			const names = tuned.length === 1 ? tuned[0].id : `${tuned.length} models`;
			error = `Clear the local tuning on ${names} in the Tuning tab first. Anthropic Messages endpoints do not take it.`;
			return Promise.resolve(false);
		}
		return save({ api_dialect: next });
	}

	const danger = $derived<DangerAction[]>([
		{
			label: 'Remove this credential',
			hint: 'Deletes the stored key',
			button: 'Remove',
			question: `Remove ${credential.name}?`,
			onConfirm: async () => {
				// Deleting closes this detail; capture its props first.
				const back = gateway ? null : provider.item;
				await providers.deleteProvider(row.id);
				if (!back) settingsNav.clearSelection('providers');
				else settingsNav.selectItem(back, 'providers');
			}
		}
	]);

	onMount(() => {
		if (!openRouter) return;
		let disposed = false;
		getOpenRouterUsage(row.id)
			.then((value) => {
				if (!disposed) credits = value;
			})
			.catch(() => {});
		return () => {
			disposed = true;
		};
	});

	const rowValue = (r: ModelProvider, f: 'context_window' | 'max_output_tokens') => (r[f] != null ? String(r[f]) : '');
</script>

<DetailPane
	name={title}
	subtitle={line}
	tabs={gateway ? (tuning ? ['Overview', 'Models', 'Tuning'] : ['Overview', 'Models']) : row.provider_type === 'openai' || native ? ['Overview', 'Models'] : ['Overview', 'Models', 'Usage']}
	{danger}
	item={credential.item}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" />{/snippet}
	{#snippet primary()}
		<CredentialControl {credential} showState />
	{/snippet}
	{#snippet children(tab: string | null)}
		{#if tab === 'Models'}
			{#key row.id}<ModelCuration {row} />{/key}
		{:else if tab === 'Tuning' && tuning}
			{#key row.id}<LocalTuning
					{row}
					onOpenModels={() => settingsNav.setTab('providers', credential.item, 'Models')}
				/>{/key}
		{:else if tab === 'Usage'}
			{#key row.id}<KeyUsage providerId={row.id} {credits} {openRouter} xai={supportsXAIBalance(row)} />{/key}
		{:else}
			<RowGroup title="Connection" id="providers-connection">
				<SettingRow label="Name">
					<TextField value={row.name} width={260} disabled={saving} onchange={(v) => save({ name: v })} />
				</SettingRow>
				<SettingRow label="Endpoint">
					<TextField
						value={row.base_url}
						type="url"
						mono
						width={260}
						disabled={saving}
						onchange={(v) => save({ base_url: v })}
					/>
				</SettingRow>
				<SavedSecretRow
					label="Auth token"
					saved={row.auth_token_set}
					placeholder="Provider API key"
					busy={saving}
					onsave={(value) => save({ auth_token: value }).then(Boolean)}
				/>
				{#if native}
					<SettingRow label={option?.credential_label ?? credential.kind} hint="Saving a replacement checks the key; a blank key leaves it unchanged">
						<button type="button" class="st-btn" disabled={checking || saving || !row.auth_token_set} onclick={checkKey}>{checking ? 'Checking…' : 'Check key'}</button>
					</SettingRow>
					{#if keyStatus}<p class="pv-msg" role="status">{keyStatus}</p>{/if}
				{/if}
				{#if gateway}
					<SettingRow label="API dialect">
						<Segmented
							options={DIALECTS}
							value={dialect}
							disabled={saving}
							onchange={(v: Dialect) => changeDialect(v)}
						/>
					</SettingRow>
				{/if}
				<SettingRow label="Context window" hint="Tokens, blank for auto">
					<TextField
						value={rowValue(row, 'context_window')}
						placeholder="Auto"
						inputmode="numeric"
						width={260}
						disabled={saving}
						onchange={(v) => saveNumber('context_window', v)}
					/>
				</SettingRow>
				<SettingRow label="Max output" hint="Tokens, blank for auto">
					<TextField
						value={rowValue(row, 'max_output_tokens')}
						placeholder="Auto"
						inputmode="numeric"
						width={260}
						disabled={saving}
						onchange={(v) => saveNumber('max_output_tokens', v)}
					/>
				</SettingRow>
			</RowGroup>
			{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
			{#if !gateway && supportsNativeImages(row)}
				<RowGroup title="Use">
					<SettingRow label="Native image generation" help={NATIVE_IMAGE_HELP}>
						<ValueText status="ok" value="Supported" />
					</SettingRow>
				</RowGroup>
			{/if}
		{/if}
	{/snippet}
</DetailPane>
