<script lang="ts">
	/**
	 * The connect flow for OpenAI, xAI Grok, OpenRouter and custom gateways
	 * inside setup's "AI provider" step. It reuses Settings > Providers' own
	 * setup components (KeySetup, DeviceCodeSetup, GatewaySetup, ModelCuration)
	 * the way DraftDetail drives them: one draft in `providerDrafts`, its setup
	 * inline, then the models step. Only the Settings detail pane around them is
	 * replaced, and a `.settings-shell` wrapper gives them their styles.
	 *
	 * The models step runs in wizard mode (see ModelCuration): a model is
	 * required, a rejected key comes back to the key form with the worker's
	 * reason and is replaced on the same row, a timeout or server error offers
	 * Retry, and a gateway with no model list offers Remove. A row saved
	 * earlier that still has no model (`pending`) is resumed, never doubled.
	 *
	 * `onConnected` fires with the provider id once models are saved; whether
	 * the step then says "Connected" is the worker's call (`connected`).
	 */
	import { onDestroy, untrack } from 'svelte';
	import { KeyRound, LogIn, Plug } from 'lucide-svelte';
	import { deleteModelProvider, type ModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';
	import '$lib/components/settings/shell/settings.css';
	import '$lib/components/settings/providers/providers.css';
	import DeviceCodeSetup from '$lib/components/settings/providers/setup/DeviceCodeSetup.svelte';
	import KeySetup from '$lib/components/settings/providers/setup/KeySetup.svelte';
	import GatewaySetup from '$lib/components/settings/providers/setup/GatewaySetup.svelte';
	import ModelCuration from '$lib/components/settings/providers/ModelCuration.svelte';
	import { providerDrafts } from '$lib/components/settings/providers/drafts.svelte';
	import {
		kindsOf, type CredentialFlavor, type CredentialView, type ProviderId
	} from '$lib/components/settings/providers/providers-model';

	interface Props {
		/** `openai`, `grok`, `openrouter`, or `gateway` for a new custom gateway. */
		provider: 'openai' | 'grok' | 'openrouter' | 'gateway';
		/** Credentials this provider already has (names the new draft "API key 2"). */
		existing?: Pick<CredentialView, 'name'>[];
		/** A key or gateway row saved earlier that has no model yet: resumed at its models step. */
		pending?: ModelProvider | null;
		/** The worker reports this provider ready and it has a model (the step's own check). */
		connected?: boolean;
		onConnected?: (providerId: ProviderId) => void;
		/** A row was removed (a gateway with no model list). */
		onChanged?: () => void;
	}
	let { provider, existing = [], pending = null, connected = false, onConnected, onChanged }: Props = $props();

	const kinds = $derived(provider === 'gateway' ? [] : kindsOf(provider));
	let draftItem = $state<string | null>(null);
	const draft = $derived(providerDrafts.get(draftItem));
	let done = $state(false);
	/** Why the models step sent the owner back to the key form. */
	let rejection = $state('');

	const DEVICE_LABEL: Record<string, string> = { openai: 'Sign in with ChatGPT', grok: 'Sign in with SuperGrok' };
	const BRAND: Record<string, string> = { openai: 'OpenAI', grok: 'xAI', openrouter: 'OpenRouter' };
	const brandName = $derived.by(() => {
		if (provider !== 'gateway') return BRAND[provider];
		const rowId = draft?.rowId;
		const row = rowId ? providers.current.providers?.find((p) => p.id === rowId) : undefined;
		return row?.name?.trim() || 'The gateway';
	});

	function start(flavor: CredentialFlavor) {
		drop();
		done = false;
		rejection = '';
		const d = provider === 'gateway' ? providerDrafts.addGateway() : providerDrafts.add(provider, flavor, existing);
		draftItem = d.item;
	}

	/** Pick up a saved row that has no model yet, at its models step. */
	function resume(row: ModelProvider) {
		drop();
		done = false;
		rejection = '';
		const d = provider === 'gateway' ? providerDrafts.addGateway() : providerDrafts.add(provider, 'key', existing);
		providerDrafts.update(d.item, { rowId: row.id, ownsRow: false, step: 'models', name: row.name });
		if (provider === 'gateway') providerDrafts.renameGateway(d.providerId, row.name);
		draftItem = d.item;
	}

	/** A provider with one way to connect (OpenRouter, a gateway) opens it at once. */
	const onlyFlavor = $derived<CredentialFlavor | null>(
		provider === 'gateway' ? 'gateway' : kinds.length === 1 ? kinds[0].flavor : null
	);
	$effect(() => {
		const only = onlyFlavor;
		const stuck = pending;
		if (untrack(() => draftItem || done)) return;
		if (stuck) untrack(() => resume(stuck));
		else if (only) untrack(() => start(only));
	});

	function again() {
		done = false;
		if (onlyFlavor) start(onlyFlavor);
	}

	/** Forget the draft; a device sign-in's own placeholder row is deleted. */
	function drop() {
		const d = providerDrafts.get(draftItem);
		if (d?.ownsRow && d.rowId) void deleteModelProvider(d.rowId).catch(() => {});
		if (draftItem) providerDrafts.remove(draftItem);
		draftItem = null;
	}

	/** The row exists but has no model: nothing can use it, so it is not kept. */
	function isDeadRow(rowId: string | null | undefined): boolean {
		if (!rowId) return false;
		const row = providers.current.providers?.find((p) => p.id === rowId);
		return !row || (row.models?.length ?? 0) === 0;
	}

	async function finish() {
		const d = draft;
		if (!d) return;
		const id: ProviderId = provider === 'gateway' && d.rowId ? `gw:${d.rowId}` : (d.providerId as ProviderId);
		providerDrafts.remove(d.item);
		draftItem = null;
		done = true;
		rejection = '';
		await providers.refreshProviders().catch(() => {});
		onConnected?.(id);
	}

	/** The provider refused the key: back to the key form, same row. */
	function rejected(message: string) {
		const d = draft;
		if (!d) return;
		rejection = message;
		providerDrafts.update(d.item, { step: 'start' });
	}

	/** Delete the row (a gateway with no model list) and start over. */
	async function remove() {
		const d = draft;
		const rowId = d?.rowId;
		if (d) providerDrafts.update(d.item, { rowId: null, ownsRow: false });
		drop();
		if (rowId) await deleteModelProvider(rowId).catch(() => {});
		await providers.refreshProviders().catch(() => {});
		onChanged?.();
		if (onlyFlavor) start(onlyFlavor);
	}

	/** Cancel: a row left with no model (e.g. a rejected key) is deleted too. */
	function cancel() {
		const d = draft;
		if (d?.rowId && !d.ownsRow && isDeadRow(d.rowId)) {
			const rowId = d.rowId;
			providerDrafts.update(d.item, { rowId: null });
			void deleteModelProvider(rowId).catch(() => {}).then(() => providers.refreshProviders().catch(() => {})).then(() => onChanged?.());
		}
		rejection = '';
		drop();
	}

	onDestroy(drop);
</script>

<div class="settings-shell setup-embed" data-testid="setup-provider-connect">
	{#if done}
		<div class="again" role="status" data-testid="setup-provider-connect-done">
			<span class="text-sm text-muted-foreground">
				{connected
					? 'Connected. You can add another below or continue.'
					: 'Saved. The worker does not report it ready yet. Choose Check again below.'}
			</span>
			<button type="button" class="btn btn-secondary text-sm" onclick={again}>Add another</button>
		</div>
	{:else if !draft}
		<div class="kinds" role="group" aria-label="How to connect">
			{#each kinds as k (k.flavor)}
				<button type="button" class="kind" onclick={() => start(k.flavor)}>
					{#if k.flavor === 'device'}<LogIn size={18} strokeWidth={1.75} />{:else if k.flavor === 'gateway'}<Plug size={18} strokeWidth={1.75} />{:else}<KeyRound size={18} strokeWidth={1.75} />{/if}
					<span>
						<span class="kind-title"
							>{k.flavor === 'device' ? DEVICE_LABEL[provider] ?? k.label : k.label}</span
						>
						<span class="kind-hint">{k.hint}</span>
					</span>
				</button>
			{/each}
		</div>
	{:else if draft.step === 'models' && draft.rowId}
		{#key draft.rowId}
			<ModelCuration
				mode="setup"
				wizard
				providerName={brandName}
				providerId={draft.rowId}
				onDone={finish}
				onRejected={draft.flavor === 'device' ? undefined : rejected}
				onRemove={draft.flavor === 'gateway' ? () => void remove() : undefined}
			/>
		{/key}
	{:else if draft.flavor === 'device'}
		<DeviceCodeSetup
			kind={provider === 'openai' ? 'codex' : 'xai'}
			draftRowId={draft.rowId}
			onRow={(id) => id && id !== draft.rowId && providerDrafts.update(draft.item, { rowId: id, ownsRow: true })}
			onConnected={(id) => providerDrafts.update(draft.item, { rowId: id, ownsRow: false, step: 'models' })}
			onCancel={cancel}
		/>
	{:else}
		{#if rejection}
			<p class="pv-msg err rejection" role="alert" data-testid="setup-provider-rejected">{rejection}</p>
		{/if}
		{#if draft.flavor === 'gateway'}
			<GatewaySetup {draft} onFinish={finish} onCancel={cancel} />
		{:else}
			<KeySetup {draft} submitLabel="Save key" onFinish={finish} onCancel={cancel} />
		{/if}
	{/if}
</div>

<style>
	/* Settings' shell is a two-column page; here it is just a styled block. */
	.settings-shell.setup-embed {
		display: block;
		height: auto;
		background: transparent;
	}
	.setup-embed :global(.pv-acts) { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; }
	.setup-embed :global(.st-card + .pv-acts),
	.setup-embed :global(.pv-msg) { margin-top: 10px; }
	.kinds { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.5rem; }
	.kinds:has(> :only-child) { grid-template-columns: 1fr; }
	@media (max-width: 440px) { .kinds { grid-template-columns: 1fr; } }
	.kind {
		display: flex;
		gap: 0.625rem;
		align-items: flex-start;
		text-align: left;
		padding: 0.75rem 0.875rem;
		min-height: 44px;
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		background: color-mix(in srgb, var(--muted) 30%, transparent);
		color: var(--primary);
		transition: border-color 0.18s ease-out, background-color 0.18s ease-out;
	}
	.kind:hover { border-color: color-mix(in srgb, var(--primary) 45%, var(--border)); background: color-mix(in srgb, var(--primary) 6%, transparent); }
	.kind:active { background: color-mix(in srgb, var(--primary) 12%, transparent); }
	.kind-title { display: block; font-size: 0.875rem; font-weight: 600; color: var(--foreground); }
	.kind-hint { display: block; font-size: 0.75rem; color: var(--muted-foreground); }
	.rejection { margin: 0 0 10px; }
	.again { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap; }
	@media (prefers-reduced-motion: reduce) { .kind { transition: none; } }
</style>
