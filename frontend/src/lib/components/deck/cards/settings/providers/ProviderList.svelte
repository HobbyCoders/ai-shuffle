<script lang="ts">
	/**
	 * ProviderList — the unified, store-driven provider list (plan §1.1).
	 *
	 * Maps the two state clusters the `providers` store owns into one ordered list
	 * of rows:
	 *   1. Claude subscription (native-auth) — status pill + "Manage accounts"
	 *      expander that reveals `ClaudeAccountsPanel` (switch / add / rename /
	 *      delete). No enable toggle.
	 *   2. Claude API key (native-auth) — `ClaudeApiKeyRow`, its own keycard. It
	 *      reports which auth method Claude Code is using so row 1 can show
	 *      "overridden by API key".
	 *   3. model_providers rows, ordered ChatGPT → OpenRouter → OpenAI → custom:
	 *        - ChatGPT subscription (auth_mode='chatgpt'): native-auth, status pill,
	 *          no toggle, "Sign in / Reconnect" (no-op this chunk — Chunk 6 wires
	 *          the device flow), removable.
	 *        - gateway rows (OpenRouter / OpenAI / custom): enable toggle, "Manage
	 *          models" (no-op this chunk — Chunk 7 wires the drawer), Edit (opens
	 *          the real `ProviderEditForm`), and Remove.
	 *
	 * All mutations funnel through the store so the model registry re-hydrates
	 * (landmine L4). Row presentation lives in `ProviderRow`; this component owns
	 * only the row model + transient view state (which row is expanded / editing /
	 * confirming delete).
	 */
	import { claudeAuthenticated, claudeTokenValid } from '$lib/stores/auth.svelte';
	import type { ModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';
	import ProviderRow from './ProviderRow.svelte';
	import ClaudeAccountsPanel from './ClaudeAccountsPanel.svelte';
	import ClaudeApiKeyRow from './ClaudeApiKeyRow.svelte';
	import ProviderEditForm from './ProviderEditForm.svelte';
	import ManageModelsDrawer from './ManageModelsDrawer.svelte';

	const s = $derived(providers.current);

	// Mirror of which auth method Claude Code is using, reported by ClaudeApiKeyRow.
	// Drives the Claude-subscription row's "overridden by API key" status, exactly
	// as ConnectionsSection derived it from the same backend signal today.
	let usingApiKeyAuth = $state(false);

	// ----- Transient view state (component-local) -----
	// Whether the Claude subscription row's saved-accounts panel is expanded.
	let claudeExpanded = $state(false);
	// Which gateway row's inline edit form is open (its id), or null.
	let editingId: string | null = $state(null);
	// Which provider row is armed for delete (inline confirm), or null.
	let confirmDeleteId: string | null = $state(null);
	// Which gateway row's Manage-models drawer is open (its id), or null.
	let manageModelsId = $state<string | null>(null);

	// Token present but expired/invalid: the credential exists (so
	// $claudeAuthenticated is true) yet the OAuth token can't be used. Mirrors
	// ConnectionsSection's derivation.
	let tokenExpired = $derived($claudeAuthenticated && !$claudeTokenValid);

	// ----- model_providers classification & ordering (ruling PR1) -----
	// Key on auth_mode FIRST (the Codex row is provider_type='custom' +
	// auth_mode='chatgpt'), THEN provider_type.
	type SubKind = 'chatgpt' | 'openrouter' | 'openai' | 'custom';
	function classifyProvider(p: ModelProvider): SubKind {
		if (p.auth_mode === 'chatgpt') return 'chatgpt'; // auth_mode wins (PR1)
		if (p.provider_type === 'chatgpt') return 'chatgpt';
		if (p.provider_type === 'openrouter') return 'openrouter';
		if (p.provider_type === 'openai') return 'openai';
		return 'custom';
	}
	const SUBKIND_ORDER: Record<SubKind, number> = { chatgpt: 0, openrouter: 1, openai: 2, custom: 3 };

	function badgeFor(kind: SubKind): string {
		if (kind === 'chatgpt') return 'ChatGPT';
		if (kind === 'openrouter') return 'OpenRouter';
		if (kind === 'openai') return 'OpenAI';
		return 'Custom';
	}

	function providerMeta(p: ModelProvider, kind: SubKind): string {
		const n = p.models.length;
		const modelsTxt = `${n} ${n === 1 ? 'model' : 'models'}`;
		// ChatGPT rows authenticate via the Codex OAuth tokens, not a gateway auth
		// token — "no token" there would contradict the Connected pill.
		const noToken = kind !== 'chatgpt' && !p.auth_token_set ? ' · no token' : '';
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
	function onManageAccounts() {
		claudeExpanded = !claudeExpanded;
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

	// Sign in / Reconnect / Manage models are wired to no-op callbacks this chunk.
	function noop() {
		// Chunk 5/6/7 wires this (Add-provider modal / ChatGPT device flow /
		// Manage-models drawer). Intentionally does nothing for now.
	}
</script>

<ul class="prov-list">
	<!-- 1. Claude subscription (native-auth) -->
	<li class="prov-item">
		<ProviderRow
			title="Claude subscription"
			badge="Subscription"
			iconKind="claude"
			status={claudeStatus}
			highlight={claudeHighlight}
			onManageAccounts={onManageAccounts}
			expanded={claudeExpanded}
		/>
		{#if claudeExpanded}
			<div class="prov-sub">
				<ClaudeAccountsPanel />
			</div>
		{/if}
	</li>

	<!-- 2. Claude API key (native-auth) — reports the active auth method upward. -->
	<li class="prov-item">
		<ClaudeApiKeyRow onAuthMethodChange={(v) => (usingApiKeyAuth = v)} />
	</li>

	<!-- 3. model_providers rows (ChatGPT → OpenRouter → OpenAI → custom) -->
	{#if s.providersError}
		<li class="prov-item"><div class="prov-err" role="alert">{s.providersError}</div></li>
	{/if}

	{#if s.providersLoading && s.providers.length === 0}
		<li class="prov-item"><div class="prov-empty">Loading providers…</div></li>
	{:else if s.providers.length === 0}
		<li class="prov-item"><div class="prov-empty">No additional providers yet. Add OpenRouter, OpenAI or a custom gateway to run non-Anthropic models.</div></li>
	{/if}

	{#each sortedProviders as p (p.id)}
		{@const kind = classifyProvider(p)}
		<li class="prov-item">
			{#if kind === 'chatgpt'}
				<!-- ChatGPT subscription: native-auth, no toggle, sign-in is no-op. -->
				<ProviderRow
					title={p.name}
					badge={badgeFor(kind)}
					iconKind="server"
					status={chatgptStatus(p)}
					highlight={!!p.codex_access_token_set && !p.needs_reauth}
					meta={providerMeta(p, kind)}
					onSignIn={noop}
					signInLabel={p.needs_reauth ? 'Reconnect' : 'Sign in'}
					onRemove={() => armDelete(p.id)}
					confirmingDelete={confirmDeleteId === p.id}
					onConfirmDelete={() => confirmDelete(p.id)}
					onCancelDelete={cancelDelete}
				/>
			{:else}
				<!-- Gateway row: enable toggle + manage-models(no-op) + edit + remove. -->
				<ProviderRow
					title={p.name}
					badge={badgeFor(kind)}
					iconKind="server"
					status={p.enabled ? { label: 'Enabled', tone: 'ok' } : { label: 'Disabled', tone: 'off' }}
					highlight={p.enabled}
					meta={providerMeta(p, kind)}
					toggle={{
						on: p.enabled,
						busy: s.togglingProviderId === p.id,
						onToggle: () => providers.toggleProvider(p.id)
					}}
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

	@media (max-width: 560px) {
		.prov-sub {
			padding-left: 8px;
		}
	}
</style>
