<script lang="ts">
	/**
	 * ProviderList — the unified, store-driven provider list (plan §1.1).
	 *
	 * Every row is CONDITIONAL — it appears only once configured, so the page reads
	 * as one mechanism where everything is added via "+ Add provider".
	 *
	 * Maps the state the `providers` store owns into one ordered list of rows:
	 *   1. Claude subscription (native-auth) — rendered only when authenticated OR
	 *      there is a saved account. Status pill + "Manage accounts" expander that
	 *      reveals `ClaudeAccountsPanel` (switch / add / rename / delete). No toggle.
	 *   2. Anthropic API key (native-auth) — rendered only when a key is stored
	 *      (`s.anthropicKeySet`), as a uniform `ProviderRow`. Edit expands the
	 *      replace-key panel (`ClaudeApiKeyRow`, now panel-only → `setAnthropicKey`);
	 *      Remove inline-confirms → `clearAnthropicKey`. When set, `claudeCodeAuthMethod`
	 *      is 'api_key', which row 1 shows as "Overridden by API key".
	 *   3. model_providers rows, ordered ChatGPT → OpenRouter → OpenAI → custom:
	 *        - ChatGPT subscription (auth_mode='chatgpt'): native-auth, status pill,
	 *          no toggle, "Sign in / Reconnect" (opens the `ChatGptDeviceFlow` modal
	 *          in reconnect mode against the existing row), removable.
	 *        - gateway rows (OpenRouter / OpenAI / custom): "Manage models" (no-op
	 *          this chunk — Chunk 7 wires the drawer), Edit (opens the real
	 *          `ProviderEditForm`), and Remove.
	 *
	 * All mutations funnel through the store so the model registry re-hydrates
	 * (landmine L4). Row presentation lives in `ProviderRow`; this component owns
	 * only the row model + transient view state (which row is expanded / editing /
	 * confirming delete).
	 */
	import { onMount, tick } from 'svelte';
	import { X } from 'lucide-svelte';
	import { isPanelFullscreenWidth } from '$lib/utils/viewport';
	import { claudeAuthenticated, claudeTokenValid } from '$lib/stores/auth.svelte';
	import type { ModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';
	import ProviderRow from './ProviderRow.svelte';
	import ClaudeApiKeyRow from './ClaudeApiKeyRow.svelte';
	import ProviderEditForm from './ProviderEditForm.svelte';
	import ManageModelsDrawer from './ManageModelsDrawer.svelte';
	import BankedResetsDialog from './BankedResetsDialog.svelte';
	import ChatGptDeviceFlow from './flows/ChatGptDeviceFlow.svelte';
	import ClaudeSubscriptionFlow from './flows/ClaudeSubscriptionFlow.svelte';

	const s = $derived(providers.current);

	// Which auth method Claude Code is using, straight from the store's integration
	// status. Drives the Claude-subscription row's "overridden by API key" status.
	// API-key login wins whenever a key is set — no silent billing switch.
	let usingApiKeyAuth = $derived(s.claudeCodeAuthMethod === 'api_key');

	// ----- Claude subscription row visibility -----
	// Only render the subscription row once there is something to show: an
	// authenticated account OR at least one saved account.
	let claudeRowVisible = $derived($claudeAuthenticated || s.claudeAccounts.length > 0);

	// The single Claude account backing the subscription row — the active one, or
	// the sole saved account when none is flagged active. Multiple accounts are no
	// longer allowed; this is the one row we render.
	const claudeAccount = $derived(s.claudeAccounts.find((a) => a.is_active) ?? s.claudeAccounts[0]);

	// ----- Transient view state (component-local) -----
	// Whether the Claude sign-in modal is open.
	let claudeSignInOpen = $state(false);
	// Whether the Claude subscription row is armed for delete (inline confirm).
	let confirmDeleteClaude = $state(false);
	// Whether the Anthropic-key row's replace-key panel is expanded.
	let keyEditOpen = $state(false);
	// Whether the Anthropic-key row is armed for delete (inline confirm).
	let confirmDeleteKey = $state(false);
	// Which gateway row's inline edit form is open (its id), or null.
	let editingId: string | null = $state(null);
	// Which provider row is armed for delete (inline confirm), or null.
	let confirmDeleteId: string | null = $state(null);
	// Which gateway row's Manage-models drawer is open (its id), or null.
	let manageModelsId = $state<string | null>(null);
	// Which ChatGPT row is mid-reconnect (its id, hosts the device-flow modal), or null.
	let reconnectId = $state<string | null>(null);
	// True while the reconnect device flow has an operation in flight — guards close.
	let reconnectBusy = $state(false);
	// True while the Claude sign-in flow has an operation in flight — guards close.
	let claudeSignInBusy = $state(false);
	// Connected ChatGPT provider whose banked reset inventory is open, plus the
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

	// Token present but expired/invalid: the credential exists (so
	// $claudeAuthenticated is true) yet the OAuth token can't be used. Mirrors
	// ConnectionsSection's derivation.
	let tokenExpired = $derived($claudeAuthenticated && !$claudeTokenValid);

	// ----- model_providers classification & ordering (ruling PR1) -----
	// Key on auth_mode FIRST (the Codex row is provider_type='custom' +
	// auth_mode='chatgpt'), THEN provider_type.
	type SubKind = 'chatgpt' | 'grok' | 'openrouter' | 'openai' | 'custom';
	function classifyProvider(p: ModelProvider): SubKind {
		if (p.auth_mode === 'chatgpt') return 'chatgpt'; // auth_mode wins (PR1)
		if (p.provider_type === 'chatgpt') return 'chatgpt';
		if (p.auth_mode === 'grok') return 'grok';
		if (p.provider_type === 'grok') return 'grok';
		if (p.provider_type === 'openrouter') return 'openrouter';
		if (p.provider_type === 'openai') return 'openai';
		return 'custom';
	}
	const SUBKIND_ORDER: Record<SubKind, number> = {
		chatgpt: 0,
		grok: 1,
		openrouter: 2,
		openai: 3,
		custom: 4
	};

	function badgeFor(kind: SubKind): string {
		if (kind === 'chatgpt') return 'ChatGPT';
		if (kind === 'grok') return 'Grok';
		if (kind === 'openrouter') return 'OpenRouter';
		if (kind === 'openai') return 'OpenAI';
		return 'Custom';
	}

	function providerMeta(p: ModelProvider, kind: SubKind): string {
		const n = p.models.length;
		const modelsTxt = `${n} ${n === 1 ? 'model' : 'models'}`;
		// Subscription rows authenticate via OAuth tokens, not a gateway auth
		// token — "no token" there would contradict the Connected pill.
		const noToken = kind !== 'chatgpt' && kind !== 'grok' && !p.auth_token_set ? ' · no token' : '';
		return `${p.base_url} · ${modelsTxt}${noToken}`;
	}

	// Providers sorted into their display order (stable within a subkind).
	let sortedProviders = $derived(
		[...s.providers].sort((a, b) => SUBKIND_ORDER[classifyProvider(a)] - SUBKIND_ORDER[classifyProvider(b)])
	);

	// ----- Claude subscription row status (mirrors ConnectionsSection) -----
	let claudeStatus = $derived.by((): { label: string; tone: 'ok' | 'warn' | 'off' } => {
		if (tokenExpired) return { label: 'Token expired — reconnect', tone: 'warn' };
		if ($claudeAuthenticated && !usingApiKeyAuth) return { label: 'Active', tone: 'ok' };
		if ($claudeAuthenticated) return { label: 'Overridden by API key', tone: 'off' };
		return { label: 'Not connected', tone: 'off' };
	});
	let claudeHighlight = $derived($claudeAuthenticated && !usingApiKeyAuth && !tokenExpired);

	// ----- ChatGPT (Codex) native row status -----
	function chatgptStatus(p: ModelProvider): { label: string; tone: 'ok' | 'warn' | 'off' } {
		if (p.needs_reauth) return { label: 'Reconnect needed', tone: 'warn' };
		if (p.codex_access_token_set) return { label: 'Connected', tone: 'ok' };
		return { label: 'Not connected', tone: 'off' };
	}

	// ----- Actions -----
	// Claude subscription delete: inline-confirm (parity with the ChatGPT rows),
	// then fully disconnect via the store (deletes every saved account).
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

	// Anthropic-key row: Edit toggles the replace-key panel; delete is inline-confirmed.
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
			// The row's remove failure is non-fatal here; the reload leaves the row
			// as-is and the user can retry. (Panel-level save owns its own surface.)
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
</script>

<ul class="prov-list">
	<!-- 1. Claude subscription (native-auth) — only once there's an account to show. -->
	{#if claudeRowVisible}
		<li class="prov-item">
			<ProviderRow
				title="Claude"
				badge="Subscription"
				iconKind="claude"
				status={claudeStatus}
				highlight={claudeHighlight}
				meta={claudeAccount?.email
					? `${claudeAccount.email}${claudeAccount.sync_pending ? ' · sync pending' : ''}`
					: undefined}
				onSignIn={() => (claudeSignInOpen = true)}
				signInLabel={tokenExpired ? 'Reconnect' : 'Sign in'}
				onRemove={s.claudeAccounts.length > 0 ? armDeleteClaude : undefined}
				confirmingDelete={confirmDeleteClaude}
				onConfirmDelete={confirmDeleteClaude_}
				onCancelDelete={cancelDeleteClaude}
			/>
			{#if !s.accountsEncryptionReady}
				<p class="prov-note warn">
					Encryption is locked — log in again as admin to manage the Claude account.
				</p>
			{:else if s.accountsError}
				<p class="prov-note err">{s.accountsError}</p>
			{/if}
		</li>
	{/if}

	<!-- 2. Anthropic API key (native-auth) — only once a key is stored. It overrides
	     the subscription, which the Claude row's "Overridden by API key" pill shows. -->
	{#if s.anthropicKeySet}
		<li class="prov-item">
			<ProviderRow
				title="Anthropic API key"
				badge="API key"
				iconKind="claude"
				status={{ label: 'Active', tone: 'ok' }}
				highlight={true}
				meta={s.anthropicKeyMasked ?? undefined}
				onEdit={toggleKeyEdit}
				onRemove={armDeleteKey}
				confirmingDelete={confirmDeleteKey}
				onConfirmDelete={confirmDeleteKey_}
				onCancelDelete={cancelDeleteKey}
			/>
			{#if keyEditOpen}
				<div class="prov-sub">
					<ClaudeApiKeyRow onClose={() => (keyEditOpen = false)} />
				</div>
			{/if}
		</li>
	{/if}

	<!-- 3. model_providers rows (ChatGPT → OpenRouter → OpenAI → custom) -->
	{#if s.providersError}
		<li class="prov-item"><div class="prov-err" role="alert">{s.providersError}</div></li>
	{/if}

	{#if s.providersLoading && s.providers.length === 0}
		<li class="prov-item"><div class="prov-empty">Loading providers…</div></li>
	{:else if !claudeRowVisible && !s.anthropicKeySet && s.providers.length === 0}
		<li class="prov-item"><div class="prov-empty">No providers yet. Use “+ Add provider” to connect your Claude account, an Anthropic API key, or an OpenRouter / OpenAI / custom gateway.</div></li>
	{/if}

	{#each sortedProviders as p (p.id)}
		{@const kind = classifyProvider(p)}
		<li class="prov-item">
			{#if kind === 'chatgpt' || kind === 'grok'}
				<!-- Subscription: native-auth, no toggle/edit, manage models + banked resets. -->
				<ProviderRow
					title={p.name}
					badge={badgeFor(kind)}
					iconKind="server"
					status={chatgptStatus(p)}
					highlight={!!p.codex_access_token_set && !p.needs_reauth}
					meta={providerMeta(p, kind)}
					onManageModels={() => (manageModelsId = p.id)}
					onBankedResets={p.codex_access_token_set && !p.needs_reauth
						? (opener) => openBankedResets(p, opener)
						: undefined}
					onSignIn={kind === 'chatgpt' ? () => (reconnectId = p.id) : undefined}
					signInLabel={p.needs_reauth ? 'Reconnect' : 'Sign in'}
					onRemove={() => armDelete(p.id)}
					confirmingDelete={confirmDeleteId === p.id}
					onConfirmDelete={() => confirmDelete(p.id)}
					onCancelDelete={cancelDelete}
				/>
				{#if manageModelsId === p.id}
					<ManageModelsDrawer provider={p} onClose={() => (manageModelsId = null)} />
				{/if}
			{:else}
				<!-- Gateway row: manage-models(no-op) + edit + remove. -->
				<ProviderRow
					title={p.name}
					badge={badgeFor(kind)}
					iconKind="server"
					status={p.enabled ? { label: 'Active', tone: 'ok' } : { label: 'Disabled', tone: 'off' }}
					highlight={p.enabled}
					meta={providerMeta(p, kind)}
					onManageModels={() => (manageModelsId = p.id)}
					onEdit={() => openEdit(p.id)}
					onRemove={() => armDelete(p.id)}
					confirmingDelete={confirmDeleteId === p.id}
					onConfirmDelete={() => confirmDelete(p.id)}
					onCancelDelete={cancelDelete}
				/>
				{#if editingId === p.id}
					<div class="prov-sub">
						<ProviderEditForm provider={p} onClose={closeEdit} />
					</div>
				{/if}
				{#if manageModelsId === p.id}
					<ManageModelsDrawer provider={p} onClose={() => (manageModelsId = null)} />
				{/if}
			{/if}
		</li>
	{/each}
</ul>

{#if bankedResetsProvider}
	<BankedResetsDialog provider={bankedResetsProvider} onClose={closeBankedResets} />
{/if}

{#if reconnectId !== null}
	<!-- Reconnect ChatGPT: hosts ChatGptDeviceFlow in reconnect mode against the
	     existing row. Modal chrome copies AddProviderModal (theme-token classes,
	     backdrop/Esc close both guarded while busy, fullscreen at handheld width). -->
	<div
		bind:this={reconnectDialogEl}
		class="fixed inset-0 z-50 flex items-center justify-center {reconnectFullscreen ? 'p-0' : 'p-4'}"
		role="dialog"
		aria-modal="true"
		aria-label="Reconnect ChatGPT"
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
				<h2 class="flex-1 text-lg font-semibold text-foreground">Reconnect ChatGPT</h2>
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
				<ChatGptDeviceFlow
					existingProviderId={reconnectId}
					onDone={() => (reconnectId = null)}
					onCancel={() => (reconnectId = null)}
					onBusyChange={(b) => (reconnectBusy = b)}
				/>
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
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.prov-item {
		display: flex;
		flex-direction: column;
		gap: 0;
	}
	/* Expanded sub-content (accounts panel / edit form) sits indented under its row. */
	.prov-sub {
		margin: 2px 0 4px;
		padding: 4px 4px 4px 14px;
		border-left: 2px solid var(--border);
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
	/* One-line status notice under the Claude subscription row. */
	.prov-note {
		margin: 4px 2px 0;
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
		.prov-sub {
			padding-left: 8px;
		}
	}
</style>
