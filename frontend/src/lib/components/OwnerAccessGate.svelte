<script lang="ts">
	/**
	 * OwnerAccessGate — what a browser sees before it is the owner, or before
	 * the owner has a password.
	 *
	 * - "create": no owner password yet, and this browser may create one (the
	 *   first-run step, or the blocking step after an upgrade).
	 * - "login": a password exists and this browser is not signed in.
	 * - otherwise a notice that this address cannot sign in.
	 */
	import { KeyRound, Loader2, ShieldCheck } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { createOwnerPassword, ownerLogin, type OwnerStatus } from '$lib/api/owner';

	interface Props {
		status: OwnerStatus | null;
		authenticated: boolean;
		/** Called after a successful sign-in or password creation. */
		onDone: () => Promise<void> | void;
		/** Try again (re-check the connection). */
		onRetry: () => void;
	}
	let { status, authenticated, onDone, onRetry }: Props = $props();

	const mode = $derived<'create' | 'login' | 'notice'>(
		status && !status.password_set && (authenticated || status.setup_allowed)
			? 'create'
			: status && status.login_available && !authenticated
				? 'login'
				: 'notice'
	);
	const minLength = $derived(status?.min_length ?? 12);

	let password = $state('');
	let confirm = $state('');
	let busy = $state(false);
	let error = $state('');

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (busy) return;
		error = '';
		if (mode === 'create') {
			if (password.length < minLength) { error = `Use at least ${minLength} characters.`; return; }
			if (password !== confirm) { error = 'The two passwords are different.'; return; }
		}
		busy = true;
		try {
			if (mode === 'create') await createOwnerPassword(password);
			else await ownerLogin(password);
			password = ''; confirm = '';
			await onDone();
		} catch (e: unknown) {
			error = getErrorMessage(e, mode === 'create' ? 'The password could not be saved.' : 'Sign-in failed.');
		} finally {
			busy = false;
		}
	}
</script>

<div class="min-h-screen flex items-center justify-center p-4">
	<div class="card w-full max-w-md p-6 sm:p-8">
		{#if mode === 'create'}
			<form onsubmit={submit} class="space-y-4" aria-labelledby="owner-create-heading">
				<div class="text-center">
					<ShieldCheck size={28} strokeWidth={1.5} class="mx-auto mb-3 text-primary" />
					<h1 id="owner-create-heading" class="text-xl font-bold text-foreground mb-2">Set an owner password</h1>
					<p class="text-muted-foreground text-sm">
						Every browser on your network signs in with it once and then stays signed in on that device.
						Choose a long password you use nowhere else and keep it in a password manager: anyone who has it controls your agents.
					</p>
				</div>
				<div>
					<label for="owner-new-password" class="block text-xs text-muted-foreground mb-1">New password (at least {minLength} characters)</label>
					<input id="owner-new-password" type="password" class="input" autocomplete="new-password" bind:value={password} required minlength={minLength} />
				</div>
				<div>
					<label for="owner-confirm-password" class="block text-xs text-muted-foreground mb-1">Repeat it</label>
					<input id="owner-confirm-password" type="password" class="input" autocomplete="new-password" bind:value={confirm} required />
				</div>
				{#if error}<p role="alert" class="text-destructive text-sm">{error}</p>{/if}
				<button type="submit" class="btn btn-primary w-full py-3" disabled={busy}>
					{#if busy}<Loader2 size={16} class="inline-block animate-spin mr-2" />{/if}Save password
				</button>
				<p class="text-xs text-muted-foreground text-center">Phones and browsers linked through your HobbyCoders account keep using their own device sign-in.</p>
			</form>
		{:else if mode === 'login'}
			<form onsubmit={submit} class="space-y-4" aria-labelledby="owner-login-heading">
				<div class="text-center">
					<KeyRound size={28} strokeWidth={1.5} class="mx-auto mb-3 text-primary" />
					<h1 id="owner-login-heading" class="text-xl font-bold text-foreground mb-2">Sign in to AI Shuffle</h1>
					<p class="text-muted-foreground text-sm">Enter the owner password of this install.</p>
				</div>
				<div>
					<label for="owner-password" class="block text-xs text-muted-foreground mb-1">Owner password</label>
					<input id="owner-password" type="password" class="input" autocomplete="current-password" bind:value={password} required />
				</div>
				{#if error}<p role="alert" class="text-destructive text-sm">{error}</p>{/if}
				<button type="submit" class="btn btn-primary w-full py-3" disabled={busy || !password}>
					{#if busy}<Loader2 size={16} class="inline-block animate-spin mr-2" />{/if}Sign in
				</button>
				<p class="text-xs text-muted-foreground text-center">Forgot it? The Troubleshooting guide explains how to reset it from the server.</p>
			</form>
		{:else}
			<div class="text-center">
				<p role="status" class="text-foreground mb-2">This browser cannot sign in to AI Shuffle at this address.</p>
				<p class="text-muted-foreground mb-4 text-sm">Open it from your local network or your Tailscale network, using the server's address, localhost, or a hostname allowed by SHUFFLE_LOCAL_HOSTNAMES. Phones and remote browsers connect through your HobbyCoders account instead.</p>
				<button type="button" class="btn" onclick={onRetry}>Try again</button>
			</div>
		{/if}
	</div>
</div>
