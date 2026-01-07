<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth, authLoading, authError } from '$lib/stores/auth';

	let isRegistering = false;

	// Login fields (works for both admin and API users)
	let username = '';
	let password = '';

	// Registration only
	let apiKey = '';

	async function handleLogin() {
		try {
			// Try admin login first
			try {
				await auth.login(username, password);
				goto('/');
				return;
			} catch (adminError: any) {
				// If admin login fails, try API user login
				if (adminError.detail === 'Invalid credentials') {
					await auth.loginApiUser(username, password);
					goto('/');
					return;
				}
				throw adminError;
			}
		} catch (e) {
			// Error is handled by the store
		}
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
			<h1 class="text-xl sm:text-2xl font-bold text-white mb-2">AI Hub</h1>
			<p class="text-gray-400 text-sm sm:text-base">
				{isRegistering ? 'Create your account' : 'Sign in to continue'}
			</p>
		</div>

		<form on:submit|preventDefault={isRegistering ? handleRegister : handleLogin} class="space-y-4">
			<div>
				<label for="username" class="block text-sm font-medium text-gray-300 mb-1">
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
				<label for="password" class="block text-sm font-medium text-gray-300 mb-1">
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
					<label for="apiKey" class="block text-sm font-medium text-gray-300 mb-1">
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
					<p class="text-xs text-gray-500 mt-1">
						Enter the API key provided by your administrator
					</p>
				</div>
			{/if}

			{#if $authError}
				<div class="bg-red-900/50 border border-red-500 text-red-300 px-3 sm:px-4 py-3 rounded-lg text-sm">
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
	</div>
</div>
