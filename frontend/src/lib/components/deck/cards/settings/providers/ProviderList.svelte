<script lang="ts">
	/**
	 * ProviderList — the static catalog of every provider the app supports.
	 *
	 * The list is FIXED: each supported provider holds a slot whether or not it is
	 * configured, always in the same order, so the page reads as a catalog rather
	 * than a log of what happens to be connected. Slots, top to bottom:
	 *
	 *   1. Claude              — native subscription auth (no toggle)
	 *   2. Anthropic API key   — overrides the subscription while it is set
	 *   3. ChatGPT             — Codex subscription (auth_mode='chatgpt')
	 *   4. OpenAI API key      — gateway row, provider_type='openai'
	 *   5. Grok                — SuperGrok subscription
	 *   6. Grok API key        — gateway row, provider_type='xai'
	 *   7. OpenRouter          — gateway row
	 *   8. Custom gateways     — one row per remaining custom row (the only
	 *                            dynamic group; renders nothing when empty)
	 *
	 * A slot backed by no `model_providers` row still renders, reading "Not
	 * connected", and its panel offers the sign-in / add-key action, which opens
	 * `AddProviderModal` straight on that provider's step. When more than one row
	 * matches a slot the first fills it and the rest render directly beneath with
	 * the same glyph — a row is never dropped.
	 *
	 * Clicking a row header expands its panel; only one panel is open at a time.
	 * The panel holds the ai-shuffle controls for that provider (reconnect, remove,
	 * banked resets, manage models, default model, replace key). All mutations
	 * funnel through the `providers` store so the model registry re-hydrates
	 * (landmine L4); this component owns only the transient view state.
	 */
	import { onMount, tick } from 'svelte';
	import { X } from 'lucide-svelte';
	import { isPanelFullscreenWidth } from '$lib/utils/viewport';
	import { claudeAuthenticated, claudeTokenValid } from '$lib/stores/auth.svelte';
	import { updateModelProvider, type ModelProvider } from '$lib/api/model-providers';
	import { getErrorMessage } from '$lib/api/client';
	import { providers } from '$lib/stores/providers.svelte';
	import { toast } from '$lib/stores/toast';
	import type { ProviderGlyphKind } from '$lib/components/settings/ProviderGlyph.svelte';
	import ProviderRow from './ProviderRow.svelte';
	import ClaudeApiKeyRow from './ClaudeApiKeyRow.svelte';
	import ProviderEditForm from './ProviderEditForm.svelte';
	import ManageModelsDrawer from './ManageModelsDrawer.svelte';
	import BankedResetsDialog from './BankedResetsDialog.svelte';
	import AddProviderModal, { type Step } from './AddProviderModal.svelte';
	import ChatGptDeviceFlow from './flows/ChatGptDeviceFlow.svelte';
	import GrokDeviceFlow from './flows/GrokDeviceFlow.svelte';
	import ClaudeSubscriptionFlow from './flows/ClaudeSubscriptionFlow.svelte';

	type Tone = 'ok' | 'warn' | 'off';

	const s = $derived(providers.current);

	// Which auth method Claude Code is using, straight from the store's integration
	// status. API-key login wins whenever a key is set — no silent billing switch.
	let usingApiKeyAuth = $derived(s.claudeCodeAuthMethod === 'api_key');

	// The single Claude account backing the subscription row — the active one, or
	// the sole saved account when none is flagged active.
	const claudeAccount = $derived(s.claudeAccounts.find((a) => a.is_active) ?? s.claudeAccounts[0]);

	// True once there is a Claude credential to manage (authenticated session or a
	// saved account) — drives which panel the Claude slot shows.
	let claudeConnected = $derived($claudeAuthenticated || s.claudeAccounts.length > 0);

	// Token present but expired/invalid: the credential exists (so
	// $claudeAuthenticated is true) yet the OAuth token can't be used.
	let tokenExpired = $derived($claudeAuthenticated && !$claudeTokenValid);

	// ----- Transient view state (component-local) -----
	// Which row's panel is open (`claude`, `openai-key`, `custom:{id}`, …), or null.
	let expandedKey = $state<string | null>(null);
	// Which step the add-provider modal is pinned to, or null when it is closed.
	let addStep = $state<Step | null>(null);
	// Whether the Claude sign-in modal is open.
	let claudeSignInOpen = $state(false);
	// Whether the Claude row is armed for disconnect (inline confirm).
	let confirmDeleteClaude = $state(false);
	// Whether the Anthropic-key row's replace-key panel is expanded.
	let keyEditOpen = $state(false);
	// Whether the Anthropic-key row is armed for delete (inline confirm).
	let confirmDeleteKey = $state(false);
	// Which gateway row's inline edit form is open (its id), or null.
	let editingId: string | null = $state(null);
	// Which provider row is armed for delete (inline confirm), or null.
	let confirmDeleteId: string | null = $state(null);
	// Which row's Manage-models drawer is open (its id), or null.
	let manageModelsId = $state<string | null>(null);
	// Which row's default-model save is in flight (its id), or null.
	let savingDefaultId = $state<string | null>(null);
	// Which ChatGPT/Grok row is mid-reconnect (its id, hosts the device-flow modal), or null.
	let reconnectId = $state<string | null>(null);
	// True while the reconnect device flow has an operation in flight — guards close.
	let reconnectBusy = $state(false);
	// True while the Claude sign-in flow has an operation in flight — guards close.
	let claudeSignInBusy = $state(false);
	// Connected subscription row whose banked reset inventory is open, plus the
	// exact row button that must regain focus after the dialog unmounts.
	let bankedResetsProvider = $state<ModelProvider | null>(null);
	let bankedResetsOpener = $state<HTMLButtonElement | null>(null);

	// Fullscreen at handheld widths for the reconnect modal (mirrors AddProviderModal).
	let viewportWidth = $state(typeof window !== 'undefined' ? window.innerWidth : 1280);
	let reconnectFullscreen = $derived(isPanelFullscreenWidth(viewportWidth));

	// Esc fires on keydown within the dialog container, which only receives events
	// while focus is inside it — so focus it on open (AddProviderModal precedent).
	let reconnectDialogEl = $state<HTMLDivElement | null>(null);
	let claudeSignInDialogEl = $state<HTMLDivElement | null>(null);

	onMount(() => {
		const onResize = () => (viewportWidth = window.innerWidth);
		window.addEventListener('resize', onResize);
		return () => window.removeEventListener('resize', onResize);
	});

	// Focus the dialog container once it mounts so Esc works before the first click.
	$effect(() => {
		if (reconnectId !== null) reconnectDialogEl?.focus();
	});
	$effect(() => {
		if (claudeSignInOpen) claudeSignInDialogEl?.focus();
	});

	// The drawer is rendered once, outside the rows, so it survives its row
	// collapsing and there is never more than one mounted.
	const manageModelsProvider = $derived(
		manageModelsId === null ? null : (s.providers.find((p) => p.id === manageModelsId) ?? null)
	);

	// ----- Expansion -----
	// One panel at a time. Any armed confirm or open inline edit belongs to the
	// panel that was showing, so switching rows drops it.
	function toggleExpand(key: string) {
		expandedKey = expandedKey === key ? null : key;
		confirmDeleteClaude = false;
		confirmDeleteKey = false;
		keyEditOpen = false;
		editingId = null;
		confirmDeleteId = null;
	}

	function closeReconnect() {
		if (!reconnectBusy) reconnectId = null;
	}
	function handleReconnectKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') closeReconnect();
	}

	function closeClaudeSignIn() {
		if (!claudeSignInBusy) claudeSignInOpen = false;
	}
	function handleClaudeSignInKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') closeClaudeSignIn();
	}

	function openBankedResets(provider: ModelProvider, opener: HTMLButtonElement) {
		bankedResetsProvider = provider;
		bankedResetsOpener = opener;
	}

	async function closeBankedResets() {
		const opener = bankedResetsOpener;
		bankedResetsProvider = null;
		bankedResetsOpener = null;
		await tick();
		opener?.focus();
	}

	// ----- model_providers classification (ruling PR1) -----
	// Key on auth_mode FIRST (the Codex row is provider_type='custom' +
	// auth_mode='chatgpt'), THEN provider_type. Every row lands in exactly one
	// bucket, and every bucket has a slot — so nothing can go missing.
	type SubKind = 'chatgpt' | 'grok' | 'openrouter' | 'openai' | 'xai' | 'custom';
	function classifyProvider(p: ModelProvider): SubKind {
		if (p.auth_mode === 'chatgpt') return 'chatgpt'; // auth_mode wins (PR1)
		if (p.auth_mode === 'grok' || p.provider_type === 'grok') return 'grok';
		if (p.provider_type === 'chatgpt') return 'chatgpt';
		if (p.provider_type === 'openrouter') return 'openrouter';
		if (p.provider_type === 'openai') return 'openai';
		if (p.provider_type === 'xai') return 'xai';
		return 'custom';
	}

	const byKind = $derived.by(() => {
		const buckets: Record<SubKind, ModelProvider[]> = {
			chatgpt: [],
			grok: [],
			openrouter: [],
			openai: [],
			xai: [],
			custom: []
		};
		for (const p of s.providers) buckets[classifyProvider(p)].push(p);
		return buckets;
	});

	let reconnectKind = $derived.by((): SubKind | null => {
		if (reconnectId === null) return null;
		const p = s.providers.find((x) => x.id === reconnectId);
		return p ? classifyProvider(p) : null;
	});
	let reconnectTitle = $derived(reconnectKind === 'grok' ? 'Reconnect Grok' : 'Reconnect ChatGPT');

	// ----- Status lines -----
	/** Human name for a Claude plan code. Unknown or missing → the plain label. */
	function subscriptionLabel(type: string | null | undefined): string {
		const value = (type ?? '').trim().toLowerCase();
		if (value === 'max') return 'Claude Max Subscription';
		if (value === 'pro') return 'Claude Pro Subscription';
		return 'Claude Subscription';
	}

	let claudeStatus = $derived.by((): { subtitle: string; tone: Tone } => {
		if (!$claudeAuthenticated) {
			return { subtitle: 'Not connected — sign in with your Claude account.', tone: 'off' };
		}
		if (tokenExpired) return { subtitle: 'Token expired — reconnect', tone: 'warn' };
		if (usingApiKeyAuth) return { subtitle: 'Overridden by Anthropic API key', tone: 'off' };
		const label = subscriptionLabel(claudeAccount?.subscription_type);
		const pending = claudeAccount?.sync_pending ? ' · sync pending' : '';
		const email = claudeAccount?.email;
		return {
			subtitle: email ? `Authenticated as ${email} · ${label}${pending}` : `${label}${pending}`,
			tone: 'ok'
		};
	});

	let anthropicKeyStatus = $derived.by((): { subtitle: string; tone: Tone } => {
		if (!s.anthropicKeySet) {
			return {
				subtitle: 'Not connected — paste an Anthropic key to use it instead of the subscription.',
				tone: 'off'
			};
		}
		const masked = s.anthropicKeyMasked;
		return { subtitle: masked ? `Key set · ${masked}` : 'Key set', tone: 'ok' };
	});

	/** Subscription rows (ChatGPT / Grok) authenticate via OAuth blobs, not a token. */
	function subStatus(p: ModelProvider): { subtitle: string; tone: Tone } {
		const n = p.models.length;
		let subtitle: string;
		let tone: Tone;
		if (p.needs_reauth) {
			subtitle = 'Reconnect needed';
			tone = 'warn';
		} else if (p.codex_access_token_set) {
			subtitle = `Connected · ${n} ${n === 1 ? 'model' : 'models'}`;
			tone = 'ok';
		} else {
			subtitle = 'Not connected';
			tone = 'off';
		}
		if (!p.enabled) return { subtitle: `Disabled · ${subtitle}`, tone: 'off' };
		return { subtitle, tone };
	}

	function gatewayStatus(p: ModelProvider): { subtitle: string; tone: Tone } {
		const noToken = p.auth_token_set ? '' : ' · no token';
		if (!p.enabled) return { subtitle: `Disabled${noToken}`, tone: 'off' };
		return { subtitle: `Active · ${p.base_url}${noToken}`, tone: 'ok' };
	}

	// ----- Actions -----
	function armDeleteClaude() {
		confirmDeleteClaude = true;
	}
	function cancelDeleteClaude() {
		confirmDeleteClaude = false;
	}
	async function confirmDeleteClaude_() {
		confirmDeleteClaude = false;
		await providers.disconnectClaude();
	}

	function toggleKeyEdit() {
		confirmDeleteKey = false;
		keyEditOpen = !keyEditOpen;
	}
	function armDeleteKey() {
		keyEditOpen = false;
		confirmDeleteKey = true;
	}
	function cancelDeleteKey() {
		confirmDeleteKey = false;
	}
	async function confirmDeleteKey_() {
		confirmDeleteKey = false;
		try {
			await providers.clearAnthropicKey();
		} catch {
			// Non-fatal: the row stays as-is and the user can retry.
		}
	}

	function openEdit(id: string) {
		confirmDeleteId = null;
		editingId = id;
	}
	function closeEdit() {
		editingId = null;
	}

	function armDelete(id: string) {
		editingId = null;
		confirmDeleteId = id;
	}
	function cancelDelete() {
		confirmDeleteId = null;
	}
	async function confirmDelete(id: string) {
		confirmDeleteId = null;
		if (editingId === id) editingId = null;
		await providers.deleteProvider(id);
	}

	// Empty string = "provider default", stored as null.
	async function saveDefaultModel(p: ModelProvider, value: string) {
		savingDefaultId = p.id;
		try {
			await updateModelProvider(p.id, { default_model: value || null });
			await providers.refreshProviders();
		} catch (e) {
			toast.push(getErrorMessage(e, 'Could not save the default model'), { tone: 'error' });
		} finally {
			savingDefaultId = null;
		}
	}
</script>

{#snippet subPanel(p: ModelProvider)}
	<div class="prov-actrow">
		{#if confirmDeleteId === p.id}
			<span class="acct-confirm">Delete?</span>
			<button type="button" class="ed-btn danger acct-sm" onclick={() => confirmDelete(p.id)}>
				Delete
			</button>
			<button type="button" class="ed-btn acct-sm" onclick={cancelDelete}>Cancel</button>
		{:else}
			<button type="button" class="ed-btn acct-sm" onclick={() => (manageModelsId = p.id)}>
				Manage models
			</button>
			{#if p.codex_access_token_set && !p.needs_reauth}
				<button
					type="button"
					class="ed-btn acct-sm"
					onclick={(event) => openBankedResets(p, event.currentTarget)}
				>
					Banked resets
				</button>
			{/if}
			<button type="button" class="ed-btn acct-sm" onclick={() => (reconnectId = p.id)}>
				{p.needs_reauth ? 'Reconnect' : 'Sign in'}
			</button>
			<button type="button" class="ed-btn danger acct-sm" onclick={() => armDelete(p.id)}>
				Remove
			</button>
		{/if}
	</div>
{/snippet}

{#snippet gatewayPanel(p: ModelProvider)}
	<dl class="prov-dl">
		<dt>Endpoint</dt>
		<dd>{p.base_url}</dd>
		<dt>Token</dt>
		<dd>{p.auth_token_masked || 'not set'}</dd>
		<dt>Models</dt>
		<dd>{p.models.length} enabled</dd>
	</dl>

	<div class="prov-field">
		<label class="prov-label" for="prov-default-{p.id}">Default model</label>
		<select
			id="prov-default-{p.id}"
			class="sel prov-sel"
			value={p.default_model ?? ''}
			disabled={savingDefaultId === p.id}
			onchange={(event) => saveDefaultModel(p, event.currentTarget.value)}
		>
			<option value="">Provider default</option>
			{#each p.models as model (model.id)}
				<option value={model.id}>{model.label || model.id}</option>
			{/each}
		</select>
	</div>

	<div class="prov-actrow">
		{#if confirmDeleteId === p.id}
			<span class="acct-confirm">Delete?</span>
			<button type="button" class="ed-btn danger acct-sm" onclick={() => confirmDelete(p.id)}>
				Delete
			</button>
			<button type="button" class="ed-btn acct-sm" onclick={cancelDelete}>Cancel</button>
		{:else}
			<button type="button" class="ed-btn acct-sm" onclick={() => (manageModelsId = p.id)}>
				Manage models
			</button>
			<button type="button" class="ed-btn acct-sm" onclick={() => openEdit(p.id)}>Edit</button>
			<button type="button" class="ed-btn danger acct-sm" onclick={() => armDelete(p.id)}>
				Remove
			</button>
		{/if}
	</div>

	{#if editingId === p.id}
		<div class="prov-sub"><ProviderEditForm provider={p} onClose={closeEdit} /></div>
	{/if}
{/snippet}

<!-- One subscription slot (ChatGPT / Grok): the row when configured, otherwise a
     "Not connected" placeholder whose panel opens the sign-in flow. -->
{#snippet subSlot(
	key: string,
	slotTitle: string,
	glyph: ProviderGlyphKind,
	rows: ModelProvider[],
	emptySubtitle: string,
	emptyStep: Step
)}
	{#if rows.length === 0}
		<ProviderRow
			title={slotTitle}
			badge="Subscription"
			{glyph}
			tone="off"
			subtitle={emptySubtitle}
			expanded={expandedKey === key}
			onToggleExpand={() => toggleExpand(key)}
		>
			<div class="prov-actrow">
				<button type="button" class="ed-btn primary acct-sm" onclick={() => (addStep = emptyStep)}>
					Sign in
				</button>
			</div>
		</ProviderRow>
	{:else}
		{#each rows as p, i (p.id)}
			{@const rowKey = i === 0 ? key : `${key}:${p.id}`}
			{@const status = subStatus(p)}
			<ProviderRow
				title={i === 0 ? slotTitle : p.name}
				badge="Subscription"
				{glyph}
				tone={status.tone}
				subtitle={status.subtitle}
				expanded={expandedKey === rowKey}
				onToggleExpand={() => toggleExpand(rowKey)}
				enabled={p.enabled}
				onToggleEnabled={() => providers.toggleProvider(p.id)}
				toggling={s.togglingProviderId === p.id}
			>
				{@render subPanel(p)}
			</ProviderRow>
		{/each}
	{/if}
{/snippet}

<!-- One gateway slot (OpenAI / Grok / OpenRouter API keys). -->
{#snippet gatewaySlot(
	key: string,
	slotTitle: string,
	badge: string,
	glyph: ProviderGlyphKind,
	rows: ModelProvider[],
	emptySubtitle: string,
	emptyStep: Step
)}
	{#if rows.length === 0}
		<ProviderRow
			title={slotTitle}
			{badge}
			{glyph}
			tone="off"
			subtitle={emptySubtitle}
			expanded={expandedKey === key}
			onToggleExpand={() => toggleExpand(key)}
		>
			<div class="prov-actrow">
				<button type="button" class="ed-btn primary acct-sm" onclick={() => (addStep = emptyStep)}>
					Add API key
				</button>
			</div>
		</ProviderRow>
	{:else}
		{#each rows as p, i (p.id)}
			{@const rowKey = i === 0 ? key : `${key}:${p.id}`}
			{@const status = gatewayStatus(p)}
			<ProviderRow
				title={i === 0 ? slotTitle : p.name}
				{badge}
				{glyph}
				tone={status.tone}
				subtitle={status.subtitle}
				expanded={expandedKey === rowKey}
				onToggleExpand={() => toggleExpand(rowKey)}
				enabled={p.enabled}
				onToggleEnabled={() => providers.toggleProvider(p.id)}
				toggling={s.togglingProviderId === p.id}
			>
				{@render gatewayPanel(p)}
			</ProviderRow>
		{/each}
	{/if}
{/snippet}

{#if s.providersError}
	<div class="prov-err" role="alert">{s.providersError}</div>
{/if}

{#if s.providersLoading && s.providers.length === 0}
	<div class="prov-empty">Loading providers…</div>
{/if}

<div class="prov-list">
	<!-- 1. Claude subscription (native auth, no toggle). -->
	<ProviderRow
		title="Claude"
		badge="Subscription"
		glyph="claude"
		tone={claudeStatus.tone}
		subtitle={claudeStatus.subtitle}
		expanded={expandedKey === 'claude'}
		onToggleExpand={() => toggleExpand('claude')}
	>
		{#if claudeConnected}
			<div class="prov-actrow">
				{#if confirmDeleteClaude}
					<span class="acct-confirm">Disconnect?</span>
					<button type="button" class="ed-btn danger acct-sm" onclick={confirmDeleteClaude_}>
						Disconnect
					</button>
					<button type="button" class="ed-btn acct-sm" onclick={cancelDeleteClaude}>Cancel</button>
				{:else}
					<button
						type="button"
						class="ed-btn acct-sm"
						onclick={() => (claudeSignInOpen = true)}
					>
						Reconnect
					</button>
					{#if s.claudeAccounts.length > 0}
						<button type="button" class="ed-btn danger acct-sm" onclick={armDeleteClaude}>
							Disconnect
						</button>
					{/if}
				{/if}
			</div>
		{:else}
			<div class="prov-actrow">
				<button
					type="button"
					class="ed-btn primary acct-sm"
					onclick={() => (claudeSignInOpen = true)}
				>
					Sign in
				</button>
			</div>
		{/if}

		{#if !s.accountsEncryptionReady}
			<p class="prov-note warn">
				Encryption is locked — log in again as admin to manage the Claude account.
			</p>
		{:else if s.accountsError}
			<p class="prov-note err">{s.accountsError}</p>
		{/if}
		{#if s.claudeAuthError}
			<p class="prov-note err">{s.claudeAuthError}</p>
		{/if}
	</ProviderRow>

	<!-- 2. Anthropic API key — overrides the subscription while it is set. -->
	<ProviderRow
		title="Anthropic API key"
		badge="API key"
		glyph="anthropic"
		tone={anthropicKeyStatus.tone}
		subtitle={anthropicKeyStatus.subtitle}
		expanded={expandedKey === 'anthropic-key'}
		onToggleExpand={() => toggleExpand('anthropic-key')}
	>
		{#if s.anthropicKeySet}
			<div class="prov-actrow">
				{#if confirmDeleteKey}
					<span class="acct-confirm">Delete?</span>
					<button type="button" class="ed-btn danger acct-sm" onclick={confirmDeleteKey_}>
						Delete
					</button>
					<button type="button" class="ed-btn acct-sm" onclick={cancelDeleteKey}>Cancel</button>
				{:else}
					<button type="button" class="ed-btn acct-sm" onclick={toggleKeyEdit}>Replace key</button>
					<button type="button" class="ed-btn danger acct-sm" onclick={armDeleteKey}>Remove</button>
				{/if}
			</div>
			{#if keyEditOpen}
				<div class="prov-sub"><ClaudeApiKeyRow onClose={() => (keyEditOpen = false)} /></div>
			{/if}
		{:else}
			<div class="prov-actrow">
				<button
					type="button"
					class="ed-btn primary acct-sm"
					onclick={() => (addStep = 'claude-key')}
				>
					Add API key
				</button>
			</div>
		{/if}
	</ProviderRow>

	<!-- 3. ChatGPT (Codex subscription) -->
	{@render subSlot(
		'chatgpt',
		'ChatGPT',
		'openai',
		byKind.chatgpt,
		'Not connected — sign in with ChatGPT.',
		'openai-sub'
	)}

	<!-- 4. OpenAI API key -->
	{@render gatewaySlot(
		'openai-key',
		'OpenAI API key',
		'API key',
		'openai',
		byKind.openai,
		'Not connected — paste an OpenAI API key.',
		'openai-key'
	)}

	<!-- 5. Grok (SuperGrok subscription) -->
	{@render subSlot(
		'grok',
		'Grok',
		'grok',
		byKind.grok,
		'Not connected — sign in with SuperGrok.',
		'grok-sub'
	)}

	<!-- 6. Grok API key -->
	{@render gatewaySlot(
		'grok-key',
		'Grok API key',
		'API key',
		'grok',
		byKind.xai,
		'Not connected — paste a Grok API key.',
		'grok-key'
	)}

	<!-- 7. OpenRouter -->
	{@render gatewaySlot(
		'openrouter',
		'OpenRouter',
		'Gateway',
		'openrouter',
		byKind.openrouter,
		'Not connected — paste an OpenRouter API key.',
		'openrouter-key'
	)}

	<!-- 8. Custom gateways — the only dynamic group; nothing renders when empty. -->
	{#each byKind.custom as p (p.id)}
		{@const status = gatewayStatus(p)}
		<ProviderRow
			title={p.name}
			badge="Gateway"
			glyph="custom"
			tone={status.tone}
			subtitle={status.subtitle}
			expanded={expandedKey === `custom:${p.id}`}
			onToggleExpand={() => toggleExpand(`custom:${p.id}`)}
			enabled={p.enabled}
			onToggleEnabled={() => providers.toggleProvider(p.id)}
			toggling={s.togglingProviderId === p.id}
		>
			{@render gatewayPanel(p)}
		</ProviderRow>
	{/each}
</div>

{#if manageModelsProvider}
	<ManageModelsDrawer provider={manageModelsProvider} onClose={() => (manageModelsId = null)} />
{/if}

{#if bankedResetsProvider}
	<BankedResetsDialog provider={bankedResetsProvider} onClose={closeBankedResets} />
{/if}

{#if addStep !== null}
	<AddProviderModal initialStep={addStep} onClose={() => (addStep = null)} />
{/if}

{#if reconnectId !== null}
	<!-- Reconnect ChatGPT/Grok: hosts the matching device flow in reconnect mode
	     against the existing row. Modal chrome copies AddProviderModal. -->
	<div
		bind:this={reconnectDialogEl}
		class="fixed inset-0 z-50 flex items-center justify-center {reconnectFullscreen ? 'p-0' : 'p-4'}"
		role="dialog"
		aria-modal="true"
		aria-label={reconnectTitle}
		onkeydown={handleReconnectKeydown}
		tabindex="-1"
	>
		<!-- Backdrop -->
		<button
			class="absolute inset-0 bg-background/80 backdrop-blur-sm"
			onclick={closeReconnect}
			aria-label="Close"
		></button>

		<!-- Modal card -->
		<div
			class="relative flex flex-col bg-card border border-border shadow-xl {reconnectFullscreen
				? 'w-full h-full max-w-none rounded-none'
				: 'w-full max-w-lg max-h-[90vh] rounded-xl'}"
		>
			<!-- Header -->
			<div class="flex items-center gap-2 px-6 py-4 border-b border-border">
				<h2 class="flex-1 text-lg font-semibold text-foreground">{reconnectTitle}</h2>
				<button
					type="button"
					onclick={closeReconnect}
					disabled={reconnectBusy}
					class="p-2 -mr-2 hover:bg-muted rounded-lg transition-colors disabled:opacity-50"
					aria-label="Close"
				>
					<X class="w-5 h-5 text-muted-foreground" />
				</button>
			</div>

			<!-- Content -->
			<div class="flex-1 overflow-y-auto p-6">
				{#if reconnectKind === 'grok'}
					<GrokDeviceFlow
						existingProviderId={reconnectId}
						onDone={() => (reconnectId = null)}
						onCancel={() => (reconnectId = null)}
						onBusyChange={(b) => (reconnectBusy = b)}
					/>
				{:else}
					<ChatGptDeviceFlow
						existingProviderId={reconnectId}
						onDone={() => (reconnectId = null)}
						onCancel={() => (reconnectId = null)}
						onBusyChange={(b) => (reconnectBusy = b)}
					/>
				{/if}
			</div>
		</div>
	</div>
{/if}

{#if claudeSignInOpen}
	<!-- Claude subscription sign-in: hosts ClaudeSubscriptionFlow (primary connect
	     when signed out, add-account reconnect when signed in). Modal chrome copies
	     the ChatGPT reconnect modal above verbatim. -->
	<div
		bind:this={claudeSignInDialogEl}
		class="fixed inset-0 z-50 flex items-center justify-center {reconnectFullscreen ? 'p-0' : 'p-4'}"
		role="dialog"
		aria-modal="true"
		aria-label="Claude subscription"
		onkeydown={handleClaudeSignInKeydown}
		tabindex="-1"
	>
		<!-- Backdrop -->
		<button
			class="absolute inset-0 bg-background/80 backdrop-blur-sm"
			onclick={closeClaudeSignIn}
			aria-label="Close"
		></button>

		<!-- Modal card -->
		<div
			class="relative flex flex-col bg-card border border-border shadow-xl {reconnectFullscreen
				? 'w-full h-full max-w-none rounded-none'
				: 'w-full max-w-lg max-h-[90vh] rounded-xl'}"
		>
			<!-- Header -->
			<div class="flex items-center gap-2 px-6 py-4 border-b border-border">
				<h2 class="flex-1 text-lg font-semibold text-foreground">Claude subscription</h2>
				<button
					type="button"
					onclick={closeClaudeSignIn}
					disabled={claudeSignInBusy}
					class="p-2 -mr-2 hover:bg-muted rounded-lg transition-colors disabled:opacity-50"
					aria-label="Close"
				>
					<X class="w-5 h-5 text-muted-foreground" />
				</button>
			</div>

			<!-- Content -->
			<div class="flex-1 overflow-y-auto p-6">
				<ClaudeSubscriptionFlow
					onDone={() => (claudeSignInOpen = false)}
					onCancel={() => (claudeSignInOpen = false)}
					onBusyChange={(b) => (claudeSignInBusy = b)}
				/>
			</div>
		</div>
	</div>
{/if}

<style>
	.prov-list {
		display: flex;
		flex-direction: column;
	}
	.prov-err {
		font-size: 12.5px;
		color: var(--destructive);
		padding: 4px 2px;
	}
	.prov-empty {
		font-size: 12px;
		color: var(--muted-foreground);
		padding: 6px 2px 4px;
	}

	/* ----- Panel contents (rendered into ProviderRow's panel slot) ----- */
	.prov-actrow {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}
	.acct-sm {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 5px 10px;
		font-size: 12px;
	}
	.acct-confirm {
		font-size: 12px;
		color: var(--warning, #d97706);
		font-weight: 600;
	}
	/* Endpoint / token / model counts. */
	.prov-dl {
		margin: 0 0 10px;
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 2px 12px;
		font-size: 12px;
	}
	.prov-dl dt {
		color: var(--muted-foreground);
	}
	.prov-dl dd {
		margin: 0;
		min-width: 0;
		color: var(--foreground);
		font-family: var(--font-mono, monospace);
		font-size: 11.5px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.prov-field {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-bottom: 10px;
	}
	.prov-label {
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: var(--muted-foreground);
	}
	.prov-sel {
		width: 220px;
		max-width: 100%;
	}
	/* Inline edit / replace-key panel sits under the panel's action row. */
	.prov-sub {
		margin-top: 4px;
	}
	/* One-line status notice inside the Claude panel. */
	.prov-note {
		margin: 8px 0 0;
		font-size: 11.5px;
		line-height: 1.4;
	}
	.prov-note.warn {
		color: var(--warning, #d97706);
	}
	.prov-note.err {
		color: var(--destructive);
	}

	@media (max-width: 560px) {
		.prov-field {
			align-items: flex-start;
			flex-direction: column;
			gap: 4px;
		}
		.prov-sel {
			width: 100%;
		}
	}
</style>
