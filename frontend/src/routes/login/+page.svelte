<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth, authLoading, authError } from '$lib/stores/auth';

	let username = '';
	let password = '';

	async function handleSubmit() {
		try {
			await auth.login(username, password);
			goto('/');
		} catch (e) {
			// Error is handled by the store
		}
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

		<form on:submit|preventDefault={handleSubmit} class="space-y-4">
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
	</div>
</div>
