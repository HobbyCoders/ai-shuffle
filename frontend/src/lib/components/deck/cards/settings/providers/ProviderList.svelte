<script lang="ts">
	/**
	 * ProviderList — the static catalog of every provider the app supports.
	 *
	 * The list is FIXED: each supported provider holds a slot whether or not it is
	 * configured, always in the same order, so the page reads as a catalog rather
	 * than a log of what happens to be connected. Slots, top to bottom:
	 *
	 *   1. Claude              — the stock CLI's own login (no toggle); signing
	 *                            in and out lives in the Claude Code CLI group
	 *   2. Anthropic API key   — used only in API-key mode (Claude Code mode above)
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
	 * EVERY panel is a `ProviderPanel`, so the structure (two actions, settings,
	 * models, banked resets) is identical on every row and cannot drift. This
	 * component keeps the parts that reach outside one row: the modals, the delete
	 * confirms, and the `providers` store calls (which re-hydrate the model
	 * registry — landmine L4).
	 */
	import { untrack } from 'svelte';
	import { X } from 'lucide-svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import { auth, claudeAuthenticated } from '$lib/stores/auth.svelte';
	import { claudePlanLabel } from '$lib/api/claude-cli';
	import { type ModelProvider } from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';
	import type { ProviderGlyphKind } from '$lib/components/settings/ProviderGlyph.svelte';
	import ProviderRow from './ProviderRow.svelte';
	import ProviderPanel from './ProviderPanel.svelte';
	import ClaudeApiKeyRow from './ClaudeApiKeyRow.svelte';
	import AddProviderModal, { type Step } from './AddProviderModal.svelte';
	import ChatGptDeviceFlow from './flows/ChatGptDeviceFlow.svelte';
	import GrokDeviceFlow from './flows/GrokDeviceFlow.svelte';

	type Tone = 'ok' | 'warn' | 'off';

	const s = $derived(providers.current);

	// Which auth method Claude Code is using, straight from the store's integration
	// status. API-key login wins whenever a key is set — no silent billing switch.
	let usingApiKeyAuth = $derived(s.claudeCodeAuthMethod === 'api_key');

	// What the Claude Code CLI reports about its own login (`claude auth status`).
	const claudeCli = $derived($auth.status?.claude_cli ?? null);

	// ----- Transient view state (component-local) -----
	// Which row's panel is open (`claude`, `openai-key`, `custom:{id}`, …), or null.
	let expandedKey = $state<string | null>(null);
	// Which step the add-provider modal is pinned to, or null when it is closed.
	let addStep = $state<Step | null>(null);
	// Whether the Anthropic-key row's replace-key panel is expanded.
	let keyEditOpen = $state(false);
	// Whether the Anthropic-key row is armed for delete (inline confirm).
	let confirmDeleteKey = $state(false);
	// Which provider row is armed for delete (inline confirm), or null.
	let confirmDeleteId: string | null = $state(null);
	// Which ChatGPT/Grok row is mid-reconnect (its id, hosts the device-flow modal), or null.
	let reconnectId = $state<string | null>(null);
	// True while the reconnect device flow has an operation in flight — guards close.
	let reconnectBusy = $state(false);

	// Fullscreen on the mobile shell for the reconnect modal (mirrors AddProviderModal).
	const reconnectFullscreen = $derived(platform.isMobile);

	// Esc fires on keydown within the dialog container, which only receives events
	// while focus is inside it — so focus it on open (AddProviderModal precedent).
	let reconnectDialogEl = $state<HTMLDivElement | null>(null);

	// Focus the dialog container once it mounts so Esc works before the first click.
	$effect(() => {
		if (reconnectId !== null) reconnectDialogEl?.focus();
	});

	// ----- Expansion -----
	// One panel at a time. Any armed confirm or open inline form belongs to the
	// panel that was showing, so switching rows drops it.
	function toggleExpand(key: string) {
		expandedRows = [];
		expandedKey = expandedKey === key ? null : key;
		confirmDeleteKey = false;
		keyEditOpen = false;
		confirmDeleteId = null;
	}

	function closeReconnect() {
		if (!reconnectBusy) reconnectId = null;
	}
	function handleReconnectKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') closeReconnect();
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

	let expandedRows = $state<ModelProvider[]>([]);
	$effect(() => {
		const key = expandedKey;
		const live = s.providers;
		untrack(() => {
			if (!key) { expandedRows = []; return; }
			// Keep the expanded bucket's row identities until collapse, including a
			// remotely deleted row whose child may hold a write-only token.
			const kind = key.split(':')[0].replace('-key', '') as SubKind;
			const relevant = live.filter((row) => classifyProvider(row) === kind);
			const retained = expandedRows.filter((row) => classifyProvider(row) === kind);
			expandedRows = retained.map((row) => relevant.find((fresh) => fresh.id === row.id) ?? row)
				.concat(relevant.filter((row) => !retained.some((old) => old.id === row.id)));
		});
	});
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
		if (expandedRows.length) {
			const kind = classifyProvider(expandedRows[0]);
			buckets[kind] = expandedRows.map((row) => s.providers.find((fresh) => fresh.id === row.id) ?? row)
				.concat(buckets[kind].filter((row) => !expandedRows.some((old) => old.id === row.id)));
		}
		return buckets;
	});

	let reconnectKind = $derived.by((): SubKind | null => {
		if (reconnectId === null) return null;
		const p = s.providers.find((x) => x.id === reconnectId);
		return p ? classifyProvider(p) : null;
	});
	let reconnectTitle = $derived(reconnectKind === 'grok' ? 'Reconnect Grok' : 'Reconnect ChatGPT');

	// ----- Status lines -----
	let claudeStatus = $derived.by((): { subtitle: string; tone: Tone } => {
		if (!$claudeAuthenticated) {
			return { subtitle: 'Not signed in — sign in to the Claude Code CLI above.', tone: 'off' };
		}
		if (usingApiKeyAuth) return { subtitle: 'Not in use: Claude Code mode is API key', tone: 'off' };
		const plan = claudePlanLabel(claudeCli?.plan);
		const label = plan ? `Claude ${plan} subscription` : 'Claude subscription';
		const email = claudeCli?.email;
		return { subtitle: email ? `Signed in as ${email} · ${label}` : label, tone: 'ok' };
	});

	/** The Claude row's "Sign in" goes to the Claude Code CLI group. */
	function showClaudeCli() {
		document.getElementById('claude-cli-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
	}

	let anthropicKeyStatus = $derived.by((): { subtitle: string; tone: Tone } => {
		if (!s.anthropicKeySet) {
			return {
				subtitle: 'Not connected — paste an Anthropic key to use it instead of the subscription.',
				tone: 'off'
			};
		}
		const masked = s.anthropicKeyMasked;
		const set = masked ? `Key set · ${masked}` : 'Key set';
		if (!usingApiKeyAuth) return { subtitle: `${set} · not in use: Claude Code mode is subscription`, tone: 'off' };
		return { subtitle: set, tone: 'ok' };
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

	function armDelete(id: string) {
		confirmDeleteId = id;
	}
	function cancelDelete() {
		confirmDeleteId = null;
	}
	async function confirmDelete(id: string) {
		expandedRows = [];
		expandedKey = null;
		confirmDeleteId = null;
		await providers.deleteProvider(id);
	}

	/** `Sign in` on a ChatGPT/Grok row: reconnect the existing credential. */
	function signInSub(p: ModelProvider) {
		reconnectId = p.id;
	}
</script>

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
			<ProviderPanel kind="subscription" onPrimary={() => (addStep = emptyStep)} />
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
				<ProviderPanel
					kind="subscription"
					provider={p}
					configured
					bankedResets={!!p.codex_access_token_set && !p.needs_reauth}
					onPrimary={() => signInSub(p)}
					confirming={confirmDeleteId === p.id}
					onArmRemove={() => armDelete(p.id)}
					onConfirmRemove={() => confirmDelete(p.id)}
					onCancelRemove={cancelDelete}
				/>
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
			<ProviderPanel kind="key" onPrimary={() => (addStep = emptyStep)} />
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
				<ProviderPanel
					kind="key"
					provider={p}
					configured
					showDialect={classifyProvider(p) === 'custom'}
					confirming={confirmDeleteId === p.id}
					onArmRemove={() => armDelete(p.id)}
					onConfirmRemove={() => confirmDelete(p.id)}
					onCancelRemove={cancelDelete}
				/>
			</ProviderRow>
		{/each}
	{/if}
{/snippet}

<!-- Anthropic-key row extras: the replace-key form `Add key` toggles. -->
{#snippet anthropicKeyForm()}
	{#if keyEditOpen}
		<div class="prov-sub"><ClaudeApiKeyRow onClose={() => (keyEditOpen = false)} /></div>
	{/if}
{/snippet}

{#if s.providersError}
	<div class="prov-err" role="alert">{s.providersError}</div>
{/if}

{#if s.providersLoading && s.providers.length === 0}
	<div class="prov-empty">Loading providers…</div>
{/if}

{#if expandedRows.some((row) => !s.providers.some((fresh) => fresh.id === row.id))}
	<p class="prov-note" role="status">This provider was removed elsewhere. Your open settings and any typed token remain here until you close the row.</p>
{/if}
<div class="prov-list">
	<!-- 1. Claude subscription: the CLI's own login, no toggle. -->
	<ProviderRow
		title="Claude"
		badge="Subscription"
		glyph="claude"
		tone={claudeStatus.tone}
		subtitle={claudeStatus.subtitle}
		expanded={expandedKey === 'claude'}
		onToggleExpand={() => toggleExpand('claude')}
	>
		<ProviderPanel kind="subscription" nativeModels onPrimary={showClaudeCli} />
	</ProviderRow>

	<!-- 2. Anthropic API key — used only when Claude Code mode is API key. -->
	<ProviderRow
		title="Anthropic API key"
		badge="API key"
		glyph="anthropic"
		tone={anthropicKeyStatus.tone}
		subtitle={anthropicKeyStatus.subtitle}
		expanded={expandedKey === 'anthropic-key'}
		onToggleExpand={() => toggleExpand('anthropic-key')}
	>
		<ProviderPanel
			kind="key"
			nativeModels
			configured={s.anthropicKeySet}
			onPrimary={() => (s.anthropicKeySet ? toggleKeyEdit() : (addStep = 'claude-key'))}
			confirming={confirmDeleteKey}
			onArmRemove={armDeleteKey}
			onConfirmRemove={confirmDeleteKey_}
			onCancelRemove={cancelDeleteKey}
			afterActions={anthropicKeyForm}
		/>
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
			<ProviderPanel
				kind="key"
				provider={p}
				configured
				showDialect
				confirming={confirmDeleteId === p.id}
				onArmRemove={() => armDelete(p.id)}
				onConfirmRemove={() => confirmDelete(p.id)}
				onCancelRemove={cancelDelete}
			/>
		</ProviderRow>
	{/each}
</div>

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
			class="absolute inset-0 bg-background/80 backdrop-blur-xs"
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

	/* Inline replace-key panel sits under the Anthropic row's action row. */
	.prov-sub {
		margin-top: 4px;
	}
	/* One-line status notice. */
	.prov-note {
		margin: 8px 0 0;
		font-size: 11.5px;
		line-height: 1.4;
	}
</style>
