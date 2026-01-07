<script lang="ts">
	/**
	 * WebhookManager - Manage webhooks for external integrations
	 *
	 * Admin-only component for creating, editing, testing, and deleting webhooks.
	 */

	import { onMount } from 'svelte';

	// Types
	interface Webhook {
		id: string;
		url: string;
		events: string[];
		secret?: string;
		is_active: boolean;
		created_at: string;
		last_triggered_at?: string;
		failure_count: number;
	}

	interface WebhookTestResult {
		success: boolean;
		status_code?: number;
		response_time_ms?: number;
		error?: string;
	}

	// State
	let webhooks: Webhook[] = $state([]);
	let eventTypes: string[] = $state([]);
	let loading = $state(true);
	let error = $state('');
	let showCreateForm = $state(false);
	let editingWebhook: Webhook | null = $state(null);
	let testingWebhookId: string | null = $state(null);
	let testResult: WebhookTestResult | null = $state(null);

	// Form state
	let formUrl = $state('');
	let formSecret = $state('');
	let formEvents: string[] = $state([]);
	let formIsActive = $state(true);
	let saving = $state(false);
	let formError = $state('');

	// API functions
	async function fetchWebhooks() {
		try {
			const response = await fetch('/api/v1/webhooks', {
				credentials: 'include'
			});
			if (response.ok) {
				webhooks = await response.json();
			} else {
				error = 'Failed to load webhooks';
			}
		} catch (e) {
			error = 'Failed to load webhooks';
		}
	}

	async function fetchEventTypes() {
		try {
			const response = await fetch('/api/v1/webhooks/events', {
				credentials: 'include'
			});
			if (response.ok) {
				eventTypes = await response.json();
			}
		} catch (e) {
			// Fallback to hardcoded list
			eventTypes = ['session.complete', 'session.error', 'session.started'];
		}
	}

	async function createWebhook() {
		if (!formUrl) {
			formError = 'URL is required';
			return;
		}
		if (formEvents.length === 0) {
			formError = 'At least one event must be selected';
			return;
		}

		saving = true;
		formError = '';

		try {
			const response = await fetch('/api/v1/webhooks', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({
					url: formUrl,
					events: formEvents,
					secret: formSecret || null
				})
			});

			if (response.ok) {
				await fetchWebhooks();
				resetForm();
			} else {
				const data = await response.json();
				formError = data.detail || 'Failed to create webhook';
			}
		} catch (e) {
			formError = 'Failed to create webhook';
		} finally {
			saving = false;
		}
	}

	async function updateWebhook() {
		if (!editingWebhook) return;

		saving = true;
		formError = '';

		try {
			const response = await fetch(`/api/v1/webhooks/${editingWebhook.id}`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({
					url: formUrl,
					events: formEvents,
					secret: formSecret || null,
					is_active: formIsActive
				})
			});

			if (response.ok) {
				await fetchWebhooks();
				resetForm();
			} else {
				const data = await response.json();
				formError = data.detail || 'Failed to update webhook';
			}
		} catch (e) {
			formError = 'Failed to update webhook';
		} finally {
			saving = false;
		}
	}

	async function deleteWebhook(id: string) {
		if (!confirm('Are you sure you want to delete this webhook?')) return;

		try {
			const response = await fetch(`/api/v1/webhooks/${id}`, {
				method: 'DELETE',
				credentials: 'include'
			});

			if (response.ok) {
				await fetchWebhooks();
			} else {
				error = 'Failed to delete webhook';
			}
		} catch (e) {
			error = 'Failed to delete webhook';
		}
	}

	async function testWebhook(id: string) {
		testingWebhookId = id;
		testResult = null;

		try {
			const response = await fetch(`/api/v1/webhooks/${id}/test`, {
				method: 'POST',
				credentials: 'include'
			});

			if (response.ok) {
				testResult = await response.json();
			} else {
				testResult = { success: false, error: 'Failed to send test' };
			}
		} catch (e) {
			testResult = { success: false, error: 'Failed to send test' };
		} finally {
			testingWebhookId = null;
		}
	}

	function startEdit(webhook: Webhook) {
		editingWebhook = webhook;
		formUrl = webhook.url;
		formSecret = webhook.secret || '';
		formEvents = [...webhook.events];
		formIsActive = webhook.is_active;
		showCreateForm = true;
	}

	function resetForm() {
		showCreateForm = false;
		editingWebhook = null;
		formUrl = '';
		formSecret = '';
		formEvents = [];
		formIsActive = true;
		formError = '';
		testResult = null;
	}

	function toggleEvent(event: string) {
		if (formEvents.includes(event)) {
			formEvents = formEvents.filter(e => e !== event);
		} else {
			formEvents = [...formEvents, event];
		}
	}

	function formatDate(dateStr: string | undefined): string {
		if (!dateStr) return 'Never';
		return new Date(dateStr).toLocaleString();
	}

	onMount(async () => {
		await Promise.all([fetchWebhooks(), fetchEventTypes()]);
		loading = false;
	});
</script>

<div class="space-y-4">
	{#if loading}
		<div class="text-center py-4 text-zinc-400">Loading webhooks...</div>
	{:else if error}
		<div class="bg-red-500/10 border border-red-500/20 rounded-lg p-3 text-red-400 text-sm">
			{error}
		</div>
	{:else}
		<!-- Header -->
		<div class="flex items-center justify-between">
			<div>
				<h3 class="text-sm font-medium text-zinc-200">Webhooks</h3>
				<p class="text-xs text-zinc-500 mt-0.5">Send events to external services when sessions complete</p>
			</div>
			{#if !showCreateForm}
				<button
					onclick={() => { showCreateForm = true; }}
					class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white text-sm rounded-md transition-colors"
				>
					Add Webhook
				</button>
			{/if}
		</div>

		<!-- Create/Edit Form -->
		{#if showCreateForm}
			<div class="bg-zinc-800/50 rounded-lg p-4 border border-zinc-700/50">
				<h4 class="text-sm font-medium text-zinc-200 mb-3">
					{editingWebhook ? 'Edit Webhook' : 'New Webhook'}
				</h4>

				{#if formError}
					<div class="bg-red-500/10 border border-red-500/20 rounded-md p-2 text-red-400 text-xs mb-3">
						{formError}
					</div>
				{/if}

				<div class="space-y-3">
					<!-- URL -->
					<div>
						<label class="block text-xs text-zinc-400 mb-1">Webhook URL</label>
						<input
							type="url"
							bind:value={formUrl}
							placeholder="https://example.com/webhook"
							class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-md text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-indigo-500"
						/>
					</div>

					<!-- Secret -->
					<div>
						<label class="block text-xs text-zinc-400 mb-1">Secret (optional)</label>
						<input
							type="password"
							bind:value={formSecret}
							placeholder="For HMAC signature verification"
							class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-md text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-indigo-500"
						/>
						<p class="text-xs text-zinc-500 mt-1">Used to sign webhook payloads with HMAC-SHA256</p>
					</div>

					<!-- Events -->
					<div>
						<label class="block text-xs text-zinc-400 mb-2">Events</label>
						<div class="space-y-2">
							{#each eventTypes as event}
								<label class="flex items-center gap-2 cursor-pointer">
									<input
										type="checkbox"
										checked={formEvents.includes(event)}
										onchange={() => toggleEvent(event)}
										class="w-4 h-4 rounded border-zinc-600 bg-zinc-800 text-indigo-600 focus:ring-indigo-500 focus:ring-offset-zinc-900"
									/>
									<span class="text-sm text-zinc-300">{event}</span>
								</label>
							{/each}
						</div>
					</div>

					<!-- Active toggle (only for edit) -->
					{#if editingWebhook}
						<div>
							<label class="flex items-center gap-2 cursor-pointer">
								<input
									type="checkbox"
									bind:checked={formIsActive}
									class="w-4 h-4 rounded border-zinc-600 bg-zinc-800 text-indigo-600 focus:ring-indigo-500 focus:ring-offset-zinc-900"
								/>
								<span class="text-sm text-zinc-300">Active</span>
							</label>
						</div>
					{/if}

					<!-- Actions -->
					<div class="flex items-center gap-2 pt-2">
						<button
							onclick={editingWebhook ? updateWebhook : createWebhook}
							disabled={saving}
							class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-indigo-600/50 text-white text-sm rounded-md transition-colors"
						>
							{saving ? 'Saving...' : (editingWebhook ? 'Update' : 'Create')}
						</button>
						<button
							onclick={resetForm}
							class="px-3 py-1.5 bg-zinc-700 hover:bg-zinc-600 text-zinc-200 text-sm rounded-md transition-colors"
						>
							Cancel
						</button>
					</div>
				</div>
			</div>
		{/if}

		<!-- Test Result -->
		{#if testResult}
			<div class={`rounded-lg p-3 text-sm ${testResult.success ? 'bg-green-500/10 border border-green-500/20 text-green-400' : 'bg-red-500/10 border border-red-500/20 text-red-400'}`}>
				{#if testResult.success}
					Test successful! Status: {testResult.status_code}, Response time: {testResult.response_time_ms}ms
				{:else}
					Test failed: {testResult.error}
					{#if testResult.status_code}
						(Status: {testResult.status_code})
					{/if}
				{/if}
				<button
					onclick={() => { testResult = null; }}
					class="ml-2 text-xs underline opacity-70 hover:opacity-100"
				>
					Dismiss
				</button>
			</div>
		{/if}

		<!-- Webhooks List -->
		{#if webhooks.length === 0}
			<div class="text-center py-8 text-zinc-500 text-sm">
				No webhooks configured. Add one to receive notifications when sessions complete.
			</div>
		{:else}
			<div class="space-y-2">
				{#each webhooks as webhook}
					<div class="bg-zinc-800/30 rounded-lg p-3 border border-zinc-700/30">
						<div class="flex items-start justify-between gap-4">
							<div class="flex-1 min-w-0">
								<div class="flex items-center gap-2">
									<span class={`w-2 h-2 rounded-full ${webhook.is_active ? 'bg-green-500' : 'bg-zinc-500'}`}></span>
									<code class="text-sm text-zinc-200 truncate">{webhook.url}</code>
								</div>
								<div class="flex flex-wrap items-center gap-2 mt-1.5">
									{#each webhook.events as event}
										<span class="px-1.5 py-0.5 bg-indigo-500/20 text-indigo-300 text-xs rounded">
											{event}
										</span>
									{/each}
								</div>
								<div class="flex items-center gap-4 mt-2 text-xs text-zinc-500">
									<span>Last triggered: {formatDate(webhook.last_triggered_at)}</span>
									{#if webhook.failure_count > 0}
										<span class="text-amber-400">Failures: {webhook.failure_count}</span>
									{/if}
								</div>
							</div>
							<div class="flex items-center gap-1">
								<button
									onclick={() => testWebhook(webhook.id)}
									disabled={testingWebhookId === webhook.id}
									class="p-1.5 text-zinc-400 hover:text-indigo-400 hover:bg-zinc-700/50 rounded transition-colors disabled:opacity-50"
									title="Test webhook"
								>
									{#if testingWebhookId === webhook.id}
										<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
											<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
											<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
										</svg>
									{:else}
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
										</svg>
									{/if}
								</button>
								<button
									onclick={() => startEdit(webhook)}
									class="p-1.5 text-zinc-400 hover:text-zinc-200 hover:bg-zinc-700/50 rounded transition-colors"
									title="Edit webhook"
								>
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
									</svg>
								</button>
								<button
									onclick={() => deleteWebhook(webhook.id)}
									class="p-1.5 text-zinc-400 hover:text-red-400 hover:bg-zinc-700/50 rounded transition-colors"
									title="Delete webhook"
								>
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
									</svg>
								</button>
							</div>
						</div>
					</div>
				{/each}
			</div>
		{/if}
	{/if}
</div>
