<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth, authLoading, authError } from '$lib/stores/auth';
	import { verify2FALogin, type LoginResponse } from '$lib/api/auth';

	let isRegistering = false;
	let requires2FA = false;
	let pending2FAToken = '';

	// Login fields (works for both admin and API users)
	let username = '';
	let password = '';

	// Registration only
	let apiKey = '';

	// 2FA fields
	let twoFactorCode = '';
	let useRecoveryCode = false;
	let verifying2FA = false;
	let twoFactorError = '';

	// Password visibility
	let showPassword = false;
	let showApiKey = false;

	async function handleLogin() {
		// Clear any previous errors at the start
		auth.clearError();

		// Try admin login first
		const response = await fetch('/api/v1/auth/login', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ username, password }),
			credentials: 'include'
		});
		const data: LoginResponse = await response.json();

		if (response.ok) {
			// Check if 2FA is required
			if (data.requires_2fa && data.pending_2fa_token) {
				requires2FA = true;
				pending2FAToken = data.pending_2fa_token;
				return;
			}

			// Admin login successful, refresh auth status and redirect
			await auth.checkAuth();
			goto('/');
			return;
		}

		// Admin login failed - if "Invalid credentials", try API user login
		if (data.message === 'Invalid credentials') {
			try {
				// This sets error in auth store on failure
				await auth.loginApiUser(username, password);
				goto('/');
			} catch {
				// Error already set in auth store by loginApiUser
			}
		} else {
			// Some other admin login error (rate limit, 2FA issue, etc)
			auth.setError(data.message || 'Login failed');
		}
	}

	async function handle2FAVerify() {
		if (!twoFactorCode) {
			twoFactorError = 'Please enter a verification code';
			return;
		}

		verifying2FA = true;
		twoFactorError = '';

		try {
			await verify2FALogin(twoFactorCode, pending2FAToken, useRecoveryCode);
			await auth.checkAuth();
			goto('/');
		} catch (e: any) {
			twoFactorError = e.detail || 'Invalid verification code';
		} finally {
			verifying2FA = false;
		}
	}

	function cancel2FA() {
		requires2FA = false;
		pending2FAToken = '';
		twoFactorCode = '';
		twoFactorError = '';
		useRecoveryCode = false;
	}

	async function handleRegister() {
		try {
			await auth.registerApiUser(apiKey, username, password);
			goto('/');
		} catch (e) {
			// Error is handled by the store
		}
	}

	function toggleMode() {
		isRegistering = !isRegistering;
		auth.clearError();
	}
</script>

<svelte:head>
	<title>Login - AI Hub</title>
	<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
</svelte:head>

<div class="min-h-screen flex items-center justify-center p-4">
	<div class="card p-6 sm:p-8 w-full max-w-md">
		<div class="text-center mb-6 sm:mb-8">
			<div class="text-4xl mb-3">🤖</div>
			<h1 class="text-xl sm:text-2xl font-bold text-foreground mb-2">AI Hub</h1>
			<p class="text-muted-foreground text-sm sm:text-base">
				{#if requires2FA}
					Enter your verification code
				{:else if isRegistering}
					Create your account
				{:else}
					Sign in to continue
				{/if}
			</p>
		</div>

		{#if requires2FA}
			<!-- 2FA Verification Form -->
			<form on:submit|preventDefault={handle2FAVerify} class="space-y-4">
				<div>
					<label for="twoFactorCode" class="block text-sm font-medium text-muted-foreground mb-1">
						{useRecoveryCode ? 'Recovery Code' : 'Authenticator Code'}
					</label>
					<input
						type="text"
						id="twoFactorCode"
						bind:value={twoFactorCode}
						class="input text-center text-xl tracking-widest font-mono"
						placeholder={useRecoveryCode ? "XXXX-XXXX-XXXX" : "000000"}
						maxlength={useRecoveryCode ? 14 : 6}
						autocomplete="one-time-code"
						inputmode={useRecoveryCode ? "text" : "numeric"}
					/>
				</div>

				<div class="flex items-center gap-2">
					<input
						type="checkbox"
						id="useRecoveryCode"
						bind:checked={useRecoveryCode}
						class="rounded border-border bg-input text-primary"
					/>
					<label for="useRecoveryCode" class="text-sm text-muted-foreground">
						Use a recovery code instead
					</label>
				</div>

				{#if twoFactorError}
					<div class="bg-destructive/10 border border-destructive text-destructive px-3 sm:px-4 py-3 rounded-lg text-sm">
						{twoFactorError}
					</div>
				{/if}

				<button
					type="submit"
					class="btn btn-primary w-full py-3"
					disabled={verifying2FA}
				>
					{#if verifying2FA}
						<span class="inline-block animate-spin mr-2">&#9696;</span>
					{/if}
					Verify
				</button>

				<div class="text-center">
					<button
						type="button"
						class="text-sm text-[var(--color-primary)] hover:underline"
						on:click={cancel2FA}
					>
						Back to login
					</button>
				</div>
			</form>
		{:else}
			<!-- Regular Login/Register Form -->
			<form on:submit|preventDefault={isRegistering ? handleRegister : handleLogin} class="space-y-4">
				<div>
					<label for="username" class="block text-sm font-medium text-muted-foreground mb-1">
						Username
					</label>
					<input
						type="text"
						id="username"
						bind:value={username}
						class="input"
						placeholder="Username"
						required
						autocomplete="username"
						minlength={isRegistering ? 3 : undefined}
					/>
				</div>

				<div>
					<label for="password" class="block text-sm font-medium text-muted-foreground mb-1">
						Password
					</label>
					<input
						type="password"
						id="password"
						bind:value={password}
						class="input"
						placeholder={isRegistering ? "Create a password (min 8 characters)" : "Password"}
						required
						autocomplete={isRegistering ? "new-password" : "current-password"}
						minlength={isRegistering ? 8 : undefined}
					/>
				</div>

				{#if isRegistering}
				<div>
					<label for="apiKey" class="block text-sm font-medium text-muted-foreground mb-1">
						API Key
					</label>
					<input
						type="password"
						id="apiKey"
						bind:value={apiKey}
						class="input"
						placeholder="aih_..."
						required
						autocomplete="off"
					/>
					<p class="text-xs text-muted-foreground mt-1">
						Enter the API key provided by your administrator
					</p>
				</div>
			{/if}

			{#if $authError}
				<div class="bg-destructive/10 border border-destructive text-destructive px-3 sm:px-4 py-3 rounded-lg text-sm">
					{$authError}
				</div>
			{/if}

			<button
				type="submit"
				class="btn btn-primary w-full py-3"
				disabled={$authLoading}
			>
				{#if $authLoading}
					<span class="inline-block animate-spin mr-2">&#9696;</span>
				{/if}
				{isRegistering ? 'Create Account' : 'Sign In'}
			</button>

			<div class="text-center">
				<button
					type="button"
					class="text-sm text-[var(--color-primary)] hover:underline"
					on:click={toggleMode}
				>
					{isRegistering ? 'Already have an account? Sign in' : "New user? Register with API Key"}
				</button>
			</div>
			</form>
		{/if}
	</div>
</div>
