<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth, authLoading, authError } from '$lib/stores/auth';

	let loginMode: 'admin' | 'apikey' = 'admin';
	let username = '';
	let password = '';
	let apiKey = '';

	async function handleAdminLogin() {
		try {
			await auth.login(username, password);
			goto('/');
		} catch (e) {
			// Error is handled by the store
		}
	}

	async function handleApiKeyLogin() {
		try {
			await auth.loginWithApiKey(apiKey);
			goto('/');
		} catch (e) {
			// Error is handled by the store
		}
	}

	function switchMode(mode: 'admin' | 'apikey') {
		loginMode = mode;
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
			<p class="text-gray-400 text-sm sm:text-base">Sign in to continue</p>
		</div>

		<!-- Login mode tabs -->
		<div class="flex mb-6 border-b border-[var(--color-border)]">
			<button
				class="flex-1 py-2 text-sm font-medium transition-colors {loginMode === 'admin'
					? 'text-white border-b-2 border-[var(--color-primary)]'
					: 'text-gray-500 hover:text-gray-300'}"
				on:click={() => switchMode('admin')}
			>
				Admin Login
			</button>
			<button
				class="flex-1 py-2 text-sm font-medium transition-colors {loginMode === 'apikey'
					? 'text-white border-b-2 border-[var(--color-primary)]'
					: 'text-gray-500 hover:text-gray-300'}"
				on:click={() => switchMode('apikey')}
			>
				API Key Login
			</button>
		</div>

		{#if loginMode === 'admin'}
			<form on:submit|preventDefault={handleAdminLogin} class="space-y-4">
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
						placeholder="Password"
						required
						autocomplete="current-password"
					/>
				</div>

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
					Sign In
				</button>
			</form>
		{:else}
			<form on:submit|preventDefault={handleApiKeyLogin} class="space-y-4">
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
						Enter your API key to access with restricted permissions
					</p>
				</div>

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
					Sign In with API Key
				</button>
			</form>
		{/if}
	</div>
</div>
