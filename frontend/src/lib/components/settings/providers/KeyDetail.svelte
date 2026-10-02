<script lang="ts">
	/**
	 * An API key (OpenAI, Grok, OpenRouter) or a custom gateway's key.
	 * Overview is the connection (name, endpoint, token, dialect, context
	 * window, max output); fields save on blur or Enter, the token with Replace.
	 * Every save passes the curated models back with their metadata.
	 */
	import { onMount } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import type { ModelProvider, ModelProviderCreate } from '$lib/api/model-providers';
	import { getOpenRouterUsage, supportsOpenRouterBilling, type OpenRouterUsageStatus } from '$lib/api/openrouter-billing';
	import { supportsOpenAIBilling } from '$lib/api/openai-billing';
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
	import Switch from '../rows/Switch.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import ModelCuration from './ModelCuration.svelte';
	import KeyUsage from './tabs/KeyUsage.svelte';
	import { passthroughModels } from './curation';
	import { hostOf, supportsNativeImages, type CredentialView, type ProviderView } from './providers-model';
	import { setCredentialEnabled } from './provider-actions';

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
	const dialect = $derived<Dialect>(row.api_dialect === 'openai' ? 'openai' : 'anthropic');

	let token = $state('');
	let saving = $state(false);
	let error = $state('');
	let credits = $state<OpenRouterUsageStatus | null>(null);

	const openRouter = $derived(supportsOpenRouterBilling(row));
	const openAI = $derived(supportsOpenAIBilling(row));
	const balance = $derived(
		openRouter && credits?.credits
			? `$${credits.credits.balance_remaining.toFixed(2)}`
			: openAI
				? 'Not available for this key'
				: null
	);

	const facts = $derived(
		gateway
			? [
					{ label: 'Endpoint', value: hostOf(row.base_url), mono: true },
					{ label: 'Dialect', value: dialect === 'openai' ? 'OpenAI chat' : 'Anthropic Messages' },
					{ label: 'Models', value: `${row.models.length} in picker` },
					{ label: 'Context', value: row.context_window ? `${Math.round(row.context_window / 1024)}k` : 'Model default' }
				]
			: [
					{ label: 'Key', value: row.auth_token_set ? row.auth_token_masked || 'Stored' : 'None', mono: true },
					{ label: 'Endpoint', value: hostOf(row.base_url), mono: true },
					{ label: 'Models', value: `${row.models.length} in picker` },
					{ label: 'Balance', value: balance, muted: !!balance && !balance.startsWith('$') }
				]
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
			const body: ModelProviderCreate = {
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

	async function replaceToken() {
		const value = token.trim();
		if (!value) return;
		if (await save({ auth_token: value })) token = '';
	}

	const danger = $derived<DangerAction[]>([
		{
			label: 'Remove this credential',
			hint: 'Deletes the stored key',
			button: 'Remove',
			question: `Remove ${credential.name}?`,
			onConfirm: async () => {
				await providers.deleteProvider(row.id);
				if (gateway) settingsNav.clearSelection('providers');
				else settingsNav.selectItem(provider.item, 'providers');
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
	name={credential.name}
	badge={credential.kind}
	status={credential.status}
	statusText={credential.statusText}
	subtitle={row.enabled ? `${row.models.length} models in picker` : 'Not in the model picker'}
	{facts}
	tabs={gateway ? ['Overview', 'Models'] : ['Overview', 'Models', 'Usage']}
	{danger}
	item={credential.item}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" status={credential.status} />{/snippet}
	{#snippet primary()}
		<span class="onoff">
			{row.enabled ? 'On' : 'Off'}
			<Switch checked={row.enabled} label="{credential.name} on" onchange={(v) => setCredentialEnabled(credential, v)} />
		</span>
	{/snippet}
	{#snippet children(tab: string | null)}
		{#if tab === 'Models'}
			{#key row.id}<ModelCuration {row} />{/key}
		{:else if tab === 'Usage'}
			<KeyUsage {credits} openRouter={openRouter} openAI={openAI} />
		{:else}
			<RowGroup title="Connection" id="providers-connection">
				<SettingRow label="Name">
					<TextField value={row.name} width={220} disabled={saving} onchange={(v) => save({ name: v })} />
				</SettingRow>
				<SettingRow label="Endpoint">
					<TextField value={row.base_url} type="url" mono width={260} disabled={saving} onchange={(v) => save({ base_url: v })} />
				</SettingRow>
				<SettingRow label="Auth token" hint="Blank keeps current">
					<TextField
						bind:value={token}
						type="password"
						mono
						width={180}
						placeholder={row.auth_token_set ? `${row.auth_token_masked || '••••'} kept` : 'Provider API key'}
						disabled={saving}
					/>
					<button type="button" class="st-btn" disabled={saving || !token.trim()} onclick={replaceToken}>
						{row.auth_token_set ? 'Replace' : 'Save'}
					</button>
				</SettingRow>
				{#if gateway}
					<SettingRow label="API dialect">
						<Segmented options={DIALECTS} value={dialect} disabled={saving} onchange={(v: Dialect) => save({ api_dialect: v })} />
					</SettingRow>
				{/if}
				<SettingRow label="Context window" hint="Tokens, blank for auto">
					<TextField
						value={rowValue(row, 'context_window')}
						placeholder="Auto"
						width={110}
						disabled={saving}
						onchange={(v) => saveNumber('context_window', v)}
					/>
				</SettingRow>
				<SettingRow label="Max output" hint="Tokens, blank for auto">
					<TextField
						value={rowValue(row, 'max_output_tokens')}
						placeholder="Auto"
						width={110}
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

<style>
	.onoff {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		font-size: 13px;
		color: var(--st-mfg);
	}
</style>
