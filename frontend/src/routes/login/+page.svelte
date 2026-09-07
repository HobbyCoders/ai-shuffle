<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth } from '$lib/stores/auth.svelte';
	import OwnerSignIn from '$lib/components/OwnerSignIn.svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	let recoveryCode = $state('');
	let recovering = $state(false);
	let recoveryError = $state('');
	async function recover(event: SubmitEvent) {
		event.preventDefault();
		if (recovering) return;
		recovering = true;
		recoveryError = '';
		try {
			await api.post('/auth/recovery', { code: recoveryCode.trim() });
			recoveryCode = '';
			await auth.checkAuth();
			if (!auth.current.status?.authenticated) throw new Error('The worker could not verify local recovery.');
			await goto(auth.current.status.onboarding_complete ? '/' : '/setup');
		} catch (e) {
			recoveryError = getErrorMessage(e, 'Local recovery could not be completed.');
		} finally { recovering = false; }
	}
</script>

<svelte:head>
	<title>Sign in - AI Shuffle</title>
</svelte:head>

<div class="min-h-screen flex items-center justify-center p-4">
	<div class="card p-6 sm:p-8 w-full max-w-md">
		<div class="mb-8">
			<h1 class="text-2xl font-bold text-foreground mb-2">AI Shuffle</h1>
			<p class="text-muted-foreground">Sign in with the account that owns this worker.</p>
		</div>
		{#if $auth.status?.account_sign_in_available}
			<OwnerSignIn onAuthenticated={() => goto($auth.status?.onboarding_complete ? '/' : '/setup')} />
		{:else if $auth.status?.credential_storage?.ready === false}
			<p role="alert" class="text-sm text-muted-foreground">This worker’s credential storage is unavailable. Check its local console for recovery instructions, then reload this page.</p>
		{:else}
			<p class="text-sm text-muted-foreground">Remote access is not connected to an account yet. Open Shuffle on your local network, then link this worker in Devices.</p>
		{/if}
		{#if $auth.status?.local_recovery_available}
			<details class="mt-6 text-sm">
				<summary class="cursor-pointer">Local recovery</summary>
				<p class="my-3 text-muted-foreground">Enter the one-use recovery code generated in this worker’s local console.</p>
				<form onsubmit={recover} class="space-y-3">
					<label for="recovery-code">Recovery code</label>
					<input id="recovery-code" class="input" bind:value={recoveryCode} required minlength={43} maxlength={43} autocomplete="off" spellcheck={false} />
					<button type="submit" class="btn w-full" disabled={recovering}>{recovering ? 'Verifying…' : 'Recover local access'}</button>
					{#if recoveryError}<p role="alert" class="text-destructive">{recoveryError}</p>{/if}
				</form>
			</details>
		{/if}
	</div>
</div>
