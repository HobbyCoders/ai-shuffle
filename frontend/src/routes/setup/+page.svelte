<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth, authLoading, authError } from '$lib/stores/auth';

	let username = '';
	let password = '';
	let confirmPassword = '';
	let localError = '';

	async function handleSubmit() {
		localError = '';

		if (password !== confirmPassword) {
			localError = 'Passwords do not match';
			return;
		}

		if (password.length < 8) {
			localError = 'Password must be at least 8 characters';
			return;
		}

		try {
			await auth.setup(username, password);
			goto('/');
		} catch (e) {
			// Error is handled by the store
		}
	}
</script>

<svelte:head>
	<title>Setup - AI Hub</title>
</svelte:head>

<div class="min-h-screen flex items-center justify-center p-4">
	<div class="card p-8 w-full max-w-md">
		<div class="text-center mb-8">
			<h1 class="text-2xl font-bold text-white mb-2">Welcome to AI Hub</h1>
			<p class="text-gray-400">Create your admin account to get started</p>
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
					placeholder="admin"
					required
					minlength="3"
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
					placeholder="Enter password"
					required
					minlength="8"
					autocomplete="new-password"
				/>
			</div>

			<div>
				<label for="confirmPassword" class="block text-sm font-medium text-gray-300 mb-1">
					Confirm Password
				</label>
				<input
					type="password"
					id="confirmPassword"
					bind:value={confirmPassword}
					class="input"
					placeholder="Confirm password"
					required
					autocomplete="new-password"
				/>
			</div>

			{#if localError || $authError}
				<div class="bg-red-900/50 border border-red-500 text-red-300 px-4 py-3 rounded-lg">
					{localError || $authError}
				</div>
			{/if}

			<button
				type="submit"
				class="btn btn-primary w-full"
				disabled={$authLoading}
			>
				{#if $authLoading}
					<span class="inline-block animate-spin mr-2">&#9696;</span>
				{/if}
				Create Account
			</button>
		</form>
	</div>
</div>
