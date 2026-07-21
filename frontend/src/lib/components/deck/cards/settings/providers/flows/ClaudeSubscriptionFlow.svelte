<script lang="ts">
	/**
	 * ClaudeSubscriptionFlow — the "Claude Code subscription" Add-provider flow
	 * (plan §1.2 `claude-sub`).
	 *
	 * The existing Claude popup + paste-code OAuth flow, relocated into the modal
	 * and driven ENTIRELY by the `providers` store's existing actions (mechanics
	 * unchanged, D8). The store decides the endpoints; this component is only UI.
	 *
	 * Two action pairs, chosen once at mount from the auth state:
	 *   - NOT authenticated → PRIMARY connect pair:
	 *       `startClaudeOAuth` → `setClaudeAuthCode` → `completeClaudeOAuth`.
	 *   - already authenticated → ADD-ACCOUNT pair (leaves the current login
	 *     untouched): `startAddAccount` → `setAddAccount*` → `completeAddAccount`
	 *     (`cancelAddAccount` on back).
	 *
	 * Success is detected off the store's own post-action state (its actions return
	 * void): primary succeeds when `claudeAuthError` is empty and `claudeOAuthUrl`
	 * was cleared; add-account succeeds when `accountsError` is empty after the
	 * complete resolves.
	 *
	 * Rendered inside `AddProviderModal` (outside `.ed-content`) → self-contained
	 * Tailwind utility classes only (landmine L3).
	 */
	import { Loader2, ExternalLink } from 'lucide-svelte';
	import { claudeAuthenticated } from '$lib/stores/auth.svelte';
	import { providers } from '$lib/stores/providers.svelte';

	interface Props {
		onDone: () => void;
		onCancel: () => void;
		onBusyChange?: (busy: boolean) => void;
	}
	let { onDone, onCancel, onBusyChange }: Props = $props();

	// Capture the mode ONCE — mid-flow the auth state shouldn't flip the pair.
	const addMode = claudeAuthenticated.current;

	const s = $derived(providers.current);

	// Whether the sign-in popup has been opened (the paste-code step is unlocked).
	let started = $derived(addMode ? !!s.addAccountOAuthUrl : !!s.claudeOAuthUrl);
	let oauthUrl = $derived(addMode ? s.addAccountOAuthUrl : s.claudeOAuthUrl);
	let code = $derived(addMode ? s.addAccountCode : s.claudeAuthCode);
	let error = $derived(addMode ? s.accountsError : s.claudeAuthError);
	let starting = $derived(addMode ? s.addAccountLoading : s.claudeLoginLoading);
	let completing = $derived(addMode ? s.addAccountCompleting : s.claudeCompletingLogin);

	$effect(() => {
		onBusyChange?.(starting || completing);
	});

	function setCode(value: string) {
		if (addMode) providers.setAddAccountCode(value);
		else providers.setClaudeAuthCode(value);
	}

	async function start() {
		if (addMode) await providers.startAddAccount();
		else await providers.startClaudeOAuth();
	}

	async function complete() {
		if (!code.trim() || completing) return;
		if (addMode) {
			await providers.completeAddAccount();
			// completeAddAccount clears the flow on success and only sets
			// accountsError on failure.
			if (!providers.current.accountsError && !providers.current.addAccountOAuthUrl) onDone();
		} else {
			await providers.completeClaudeOAuth();
			// completeClaudeOAuth clears claudeOAuthUrl ONLY on success, and never
			// clears a stale claudeAuthError from an earlier failed attempt — so the
			// url is the sole reliable success signal here.
			if (!providers.current.claudeOAuthUrl) onDone();
		}
	}

	function back() {
		// Reset the transient add-account flow so a re-entry starts clean; the
		// primary flow has no store reset action (existing behavior — its url is
		// re-issued by a fresh startClaudeOAuth).
		if (addMode) providers.cancelAddAccount();
		onCancel();
	}
</script>

<div class="space-y-4">
	<p class="text-sm text-muted-foreground leading-relaxed">
		{#if addMode}
			You're already signed in. Add another Claude account — your current login stays active
			unless you make the new one active.
		{:else}
			Sign in with your Claude account. A sign-in page opens in a new window; paste the code it
			gives you back here.
		{/if}
	</p>

	{#if error}
		<div class="p-3 bg-destructive/10 border border-destructive/20 rounded-lg text-sm text-destructive" role="alert">
			{error}
		</div>
	{/if}

	{#if !started}
		<div class="flex items-center justify-end gap-3 pt-1">
			<button
				type="button"
				onclick={back}
				disabled={starting}
				class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
			>
				Back
			</button>
			<button
				type="button"
				onclick={start}
				disabled={starting}
				class="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center gap-2"
			>
				{#if starting}<Loader2 class="w-4 h-4 animate-spin" />Opening…{:else}Open sign-in{/if}
			</button>
		</div>
	{:else}
		{#if oauthUrl}
			<a
				href={oauthUrl}
				target="_blank"
				rel="noopener"
				class="inline-flex items-center gap-1.5 text-sm font-medium text-primary hover:underline"
			>
				<ExternalLink class="w-4 h-4" />Reopen sign-in page
			</a>
		{/if}

		{#if addMode}
			<label class="flex items-center gap-2 text-sm text-foreground cursor-pointer">
				<input
					type="checkbox"
					checked={s.addAccountMakeActive}
					onchange={(e) => providers.setAddAccountMakeActive(e.currentTarget.checked)}
					disabled={completing}
					class="cursor-pointer"
				/>
				Make this the active account
			</label>
		{/if}

		<div>
			<label for="claude-code" class="block text-sm font-medium text-foreground mb-1.5">Authorization code</label>
			<input
				id="claude-code"
				type="text"
				autocomplete="off"
				spellcheck="false"
				value={code}
				oninput={(e) => setCode(e.currentTarget.value)}
				placeholder="Paste the code from the sign-in page"
				disabled={completing}
				class="w-full px-3 py-2 bg-background border border-border rounded-lg text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
			/>
		</div>

		<div class="flex items-center justify-end gap-3 pt-1">
			<button
				type="button"
				onclick={back}
				disabled={completing}
				class="px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
			>
				Back
			</button>
			<button
				type="button"
				onclick={complete}
				disabled={completing || !code.trim()}
				class="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 flex items-center gap-2"
			>
				{#if completing}<Loader2 class="w-4 h-4 animate-spin" />Completing…{:else}Complete{/if}
			</button>
		</div>
	{/if}
</div>
