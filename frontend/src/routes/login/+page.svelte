<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth, authLoading, authError } from '$lib/stores/auth';
	import type { LoginResponse } from '$lib/api/auth';

	// Login fields
	let username = '';
	let password = '';

	// Password visibility
	let showPassword = false;

	async function handleLogin() {
		// Clear any previous errors at the start
		auth.clearError();

		const response = await fetch('/api/v1/auth/login', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ username, password }),
			credentials: 'include'
		});
		const data: LoginResponse = await response.json();

		if (response.ok) {
			// Admin login successful, refresh auth status and redirect
			await auth.checkAuth();
			goto('/');
			return;
		}

		// Login failed
		auth.setError(data.detail || data.message || 'Login failed');
	}
</script>

<svelte:head>
	<title>Login - AI Shuffle</title>
	<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
</svelte:head>

<div class="min-h-screen flex items-center justify-center p-4">
	<div class="card p-6 sm:p-8 w-full max-w-md">
		<div class="text-center mb-6 sm:mb-8">
			<div class="text-4xl mb-3">🤖</div>
			<h1 class="text-xl sm:text-2xl font-bold text-foreground mb-2">AI Shuffle</h1>
			<p class="text-muted-foreground text-sm sm:text-base">
				Sign in to continue
			</p>
		</div>

		<!-- Login Form -->
		<form on:submit|preventDefault={handleLogin} class="space-y-4">
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
				/>
			</div>

			<div>
				<label for="password" class="block text-sm font-medium text-muted-foreground mb-1">
					Password
				</label>
				<div class="relative">
					<input
						type={showPassword ? "text" : "password"}
						id="password"
						bind:value={password}
						class="input pr-10"
						placeholder="Password"
						required
						autocomplete="current-password"
					/>
					<button
						type="button"
						class="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
						on:click={() => showPassword = !showPassword}
						tabindex="-1"
						aria-label={showPassword ? "Hide password" : "Show password"}
					>
						{#if showPassword}
							<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
								<path stroke-linecap="round" stroke-linejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
							</svg>
						{:else}
							<svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
								<path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
								<path stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
							</svg>
						{/if}
					</button>
				</div>
			</div>

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
			Sign In
		</button>
		</form>
	</div>
</div>
