<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { auth, username, claudeAuthenticated } from '$lib/stores/auth';
	import { chat, messages, isStreaming, chatError, profiles, selectedProfile } from '$lib/stores/chat';
	import { marked } from 'marked';

	let prompt = '';
	let messagesContainer: HTMLElement;

	onMount(async () => {
		await chat.loadProfiles();
	});

	$: if ($messages.length && messagesContainer) {
		// Scroll to bottom when new messages arrive
		setTimeout(() => {
			messagesContainer.scrollTop = messagesContainer.scrollHeight;
		}, 10);
	}

	async function handleSubmit() {
		if (!prompt.trim() || $isStreaming) return;

		const userPrompt = prompt;
		prompt = '';
		await chat.sendMessage(userPrompt);
	}

	function handleKeyDown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			handleSubmit();
		}
	}

	async function handleLogout() {
		await auth.logout();
		goto('/login');
	}

	function formatCost(cost: number | undefined): string {
		if (cost === undefined) return '';
		return `$${cost.toFixed(4)}`;
	}

	function renderMarkdown(content: string): string {
		return marked(content, { breaks: true }) as string;
	}
</script>

<svelte:head>
	<title>AI Hub</title>
</svelte:head>

<div class="min-h-screen flex flex-col">
	<!-- Header -->
	<header class="bg-[var(--color-surface)] border-b border-[var(--color-border)] px-4 py-3">
		<div class="max-w-6xl mx-auto flex items-center justify-between">
			<div class="flex items-center gap-4">
				<h1 class="text-xl font-bold text-white">AI Hub</h1>

				<!-- Profile selector -->
				<select
					value={$selectedProfile}
					on:change={(e) => chat.setProfile(e.currentTarget.value)}
					class="input !w-auto !py-1.5 text-sm"
				>
					{#each $profiles as profile}
						<option value={profile.id}>{profile.name}</option>
					{/each}
				</select>
			</div>

			<div class="flex items-center gap-4">
				{#if !$claudeAuthenticated}
					<span class="text-yellow-500 text-sm">Claude not authenticated</span>
				{/if}

				<span class="text-gray-400 text-sm">{$username}</span>

				<button on:click={() => chat.startNewChat()} class="btn btn-secondary text-sm">
					New Chat
				</button>

				<button on:click={handleLogout} class="btn btn-secondary text-sm">
					Logout
				</button>
			</div>
		</div>
	</header>

	<!-- Chat area -->
	<main class="flex-1 flex flex-col max-w-4xl mx-auto w-full">
		<!-- Messages -->
		<div
			bind:this={messagesContainer}
			class="flex-1 overflow-y-auto p-4 space-y-4"
		>
			{#if $messages.length === 0}
				<div class="h-full flex items-center justify-center">
					<div class="text-center">
						<p class="text-gray-400 mb-2">Start a conversation with Claude</p>
						<p class="text-gray-500 text-sm">
							Using profile: <span class="text-gray-300">{$profiles.find(p => p.id === $selectedProfile)?.name || $selectedProfile}</span>
						</p>
					</div>
				</div>
			{:else}
				{#each $messages as message}
					<div class="flex gap-3 {message.role === 'user' ? 'justify-end' : ''}">
						<div class="max-w-[80%] {message.role === 'user' ? 'order-2' : ''}">
							<!-- Role label -->
							<div class="text-xs text-gray-500 mb-1 {message.role === 'user' ? 'text-right' : ''}">
								{message.role === 'user' ? 'You' : 'Claude'}
							</div>

							<!-- Message content -->
							<div class="card p-4 {message.role === 'user' ? 'bg-primary-900/30 border-primary-800' : ''}">
								{#if message.role === 'assistant'}
									<div class="prose prose-invert prose-sm max-w-none">
										{@html renderMarkdown(message.content)}
									</div>

									{#if message.streaming && !message.content}
										<div class="flex gap-1">
											<span class="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style="animation-delay: 0ms"></span>
											<span class="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style="animation-delay: 150ms"></span>
											<span class="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style="animation-delay: 300ms"></span>
										</div>
									{/if}

									<!-- Tool uses -->
									{#if message.toolUses && message.toolUses.length > 0}
										<div class="mt-3 space-y-2">
											{#each message.toolUses as tool}
												<details class="bg-[var(--color-bg)] rounded-lg overflow-hidden">
													<summary class="px-3 py-2 cursor-pointer text-sm text-gray-300 hover:bg-[var(--color-surface-hover)]">
														Tool: {tool.name}
													</summary>
													<div class="px-3 py-2 border-t border-[var(--color-border)]">
														<div class="text-xs text-gray-500 mb-1">Input:</div>
														<pre class="text-xs overflow-x-auto">{JSON.stringify(tool.input, null, 2)}</pre>
														{#if tool.output}
															<div class="text-xs text-gray-500 mt-2 mb-1">Output:</div>
															<pre class="text-xs overflow-x-auto max-h-40">{tool.output}</pre>
														{/if}
													</div>
												</details>
											{/each}
										</div>
									{/if}

									<!-- Metadata -->
									{#if message.metadata && !message.streaming}
										<div class="mt-3 pt-2 border-t border-[var(--color-border)] text-xs text-gray-500 flex gap-4">
											{#if message.metadata.total_cost_usd}
												<span>Cost: {formatCost(message.metadata.total_cost_usd as number)}</span>
											{/if}
											{#if message.metadata.duration_ms}
												<span>Time: {((message.metadata.duration_ms as number) / 1000).toFixed(1)}s</span>
											{/if}
										</div>
									{/if}
								{:else}
									<p class="whitespace-pre-wrap">{message.content}</p>
								{/if}
							</div>
						</div>
					</div>
				{/each}
			{/if}

			{#if $chatError}
				<div class="bg-red-900/50 border border-red-500 text-red-300 px-4 py-3 rounded-lg">
					{$chatError}
					<button on:click={() => chat.clearError()} class="ml-2 text-red-400 hover:text-red-300">
						&times;
					</button>
				</div>
			{/if}
		</div>

		<!-- Input area -->
		<div class="p-4 border-t border-[var(--color-border)]">
			<form on:submit|preventDefault={handleSubmit} class="flex gap-2">
				<textarea
					bind:value={prompt}
					on:keydown={handleKeyDown}
					placeholder="Type a message..."
					class="input flex-1 resize-none"
					rows="1"
					disabled={$isStreaming || !$claudeAuthenticated}
				></textarea>
				{#if $isStreaming}
					<button
						type="button"
						class="btn btn-danger"
						on:click={() => chat.stopGeneration()}
					>
						Stop
					</button>
				{:else}
					<button
						type="submit"
						class="btn btn-primary"
						disabled={!prompt.trim() || !$claudeAuthenticated}
					>
						Send
					</button>
				{/if}
			</form>

			{#if !$claudeAuthenticated}
				<p class="mt-2 text-sm text-yellow-500">
					Claude CLI is not authenticated. Run <code class="bg-[var(--color-surface)] px-1 rounded">docker exec -it ai-hub claude login</code> to authenticate.
				</p>
			{/if}
		</div>
	</main>
</div>

<style>
	.prose :global(pre) {
		@apply bg-[var(--color-bg)] rounded-lg p-3 overflow-x-auto;
	}

	.prose :global(code) {
		@apply bg-[var(--color-bg)] px-1 rounded;
	}

	.prose :global(p) {
		@apply mb-2;
	}

	.prose :global(ul), .prose :global(ol) {
		@apply mb-2 pl-4;
	}
</style>
