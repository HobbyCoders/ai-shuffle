<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * RateLimitManager - Admin component for managing rate limit configurations
	 *
	 * Allows admins to:
	 * - View all rate limit configurations
	 * - Create new configurations for users/API keys
	 * - Edit existing configurations
	 * - Delete configurations
	 */
	import { onMount } from 'svelte';
	import Banner from '$lib/components/Banner.svelte';
	import { api } from '$lib/api/client';

	interface RateLimitConfig {
		id: string;
		user_id: string | null;
		api_key_id: string | null;
		requests_per_minute: number;
		requests_per_hour: number;
		requests_per_day: number;
		concurrent_requests: number;
		priority: number;
		is_unlimited: boolean;
		created_at: string;
		updated_at: string;
	}

	let configs: RateLimitConfig[] = $state([]);
	let loading = $state(true);
	let error = $state('');
	let success = $state('');

	// Form state
	let showCreateForm = $state(false);
	let editingConfig: RateLimitConfig | null = $state(null);
	let formData = $state({
		target_type: 'default' as 'default' | 'user' | 'api_key',
		user_id: '',
		api_key_id: '',
		requests_per_minute: 20,
		requests_per_hour: 200,
		requests_per_day: 1000,
		concurrent_requests: 3,
		priority: 0,
		is_unlimited: false
	});

	// Queue status
	let queueInfo = $state<{ queue_size: number; max_size: number; process_time_estimate: number } | null>(null);

	async function loadData(quiet = false) {
		if (!quiet) loading = true;
		error = '';
		try {
			const [configsRes, queueRes] = await Promise.all([
				api.get<RateLimitConfig[]>('/rate-limits/config'),
				api.get<{ queue_size: number; max_size: number; process_time_estimate: number }>('/rate-limits/queue/admin')
			]);
			configs = configsRes;
			queueInfo = queueRes;
		} catch (e: any) {
			error = e.detail || 'Failed to load rate limit configurations';
		} finally {
			loading = false;
		}
	}

	async function handleCreate() {
		error = '';
		success = '';

		try {
			const payload: any = {
				requests_per_minute: formData.requests_per_minute,
				requests_per_hour: formData.requests_per_hour,
				requests_per_day: formData.requests_per_day,
				concurrent_requests: formData.concurrent_requests,
				priority: formData.priority,
				is_unlimited: formData.is_unlimited
			};

			if (formData.target_type === 'user') {
				payload.user_id = formData.user_id;
			} else if (formData.target_type === 'api_key') {
				payload.api_key_id = formData.api_key_id;
			}

			await api.post('/rate-limits/config', payload);
			success = 'Rate limit configuration created successfully';
			showCreateForm = false;
			resetForm();
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to create rate limit configuration';
		}
	}

	async function handleUpdate() {
		if (!editingConfig) return;
		error = '';
		success = '';

		try {
			await api.patch(`/rate-limits/config/${editingConfig.id}`, {
				requests_per_minute: formData.requests_per_minute,
				requests_per_hour: formData.requests_per_hour,
				requests_per_day: formData.requests_per_day,
				concurrent_requests: formData.concurrent_requests,
				priority: formData.priority,
				is_unlimited: formData.is_unlimited
			});
			success = 'Rate limit configuration updated successfully';
			editingConfig = null;
			resetForm();
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to update rate limit configuration';
		}
	}

	async function handleDelete(config: RateLimitConfig) {
		if (!confirm('Are you sure you want to delete this rate limit configuration?')) return;
		error = '';
		success = '';

		try {
			await api.delete(`/rate-limits/config/${config.id}`);
			success = 'Rate limit configuration deleted';
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to delete rate limit configuration';
		}
	}

	async function handleClearQueue() {
		if (!confirm('Are you sure you want to clear all queued requests? This cannot be undone.')) return;
		error = '';
		success = '';

		try {
			await api.post('/rate-limits/queue/clear');
			success = 'Queue cleared successfully';
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to clear queue';
		}
	}

	function startEdit(config: RateLimitConfig) {
		editingConfig = config;
		formData = {
			target_type: config.api_key_id ? 'api_key' : config.user_id ? 'user' : 'default',
			user_id: config.user_id || '',
			api_key_id: config.api_key_id || '',
			requests_per_minute: config.requests_per_minute,
			requests_per_hour: config.requests_per_hour,
			requests_per_day: config.requests_per_day,
			concurrent_requests: config.concurrent_requests,
			priority: config.priority,
			is_unlimited: config.is_unlimited
		};
		remoteBaseline = remoteSnapshot();
		remoteChange = '';
	}
	function resetForm() {
		formData = {
			target_type: 'default',
			user_id: '',
			api_key_id: '',
			requests_per_minute: 20,
			requests_per_hour: 200,
			requests_per_day: 1000,
			concurrent_requests: 3,
			priority: 0,
			is_unlimited: false
		};
	}

	function cancelEdit() {
		editingConfig = null;
		showCreateForm = false;
		resetForm();
	}

	function getTargetLabel(config: RateLimitConfig): string {
		if (config.api_key_id) {
			return `API Key: ${config.api_key_id}`;
		}
		if (config.user_id) {
			return `User: ${config.user_id}`;
		}
		return 'Default (all users)';
	}

	onMount(() => {
		loadData();
	});
	let remoteChange = $state('');
	let remoteBaseline = '';
	function remoteSnapshot() { return JSON.stringify([formData]); }
	watchResources(['settings', 'agents', 'projects'], async () => {
		const selected = editingConfig;
		const before = remoteSnapshot();
		await loadData(true);
		if (!selected || editingConfig !== selected) return;
		const fresh = configs.find((item) => item.id === selected.id);
		if (false || before !== remoteSnapshot() || remoteSnapshot() !== remoteBaseline) {
			remoteChange = fresh ? 'This item changed elsewhere. Your unsaved fields are kept here.' : 'This item was deleted elsewhere. Copy your unsaved fields before leaving.';
			return;
		}
		if (fresh) startEdit(fresh);
		else { editingConfig = null; remoteChange = 'This item was deleted elsewhere.'; }
	});
</script>

{#if remoteChange}<p role="status" style="flex: none; height: auto;" class="px-4 py-2 text-sm text-muted-foreground">{remoteChange}</p>{/if}

<div class="rate-limit-manager">
	<div class="header">
		<h3>Rate Limit Settings</h3>
		<p class="description">Configure request rate limits for users and API keys</p>
	</div>

	{#if error}
		<Banner severity="error" message={error} />
	{/if}

	{#if success}
		<Banner severity="success" message={success} />
	{/if}

	{#if loading}
		<div class="loading">Loading...</div>
	{:else}
		<!-- Queue Status -->
		{#if queueInfo}
			<div class="queue-status">
				<h4>Request Queue</h4>
				<div class="queue-info">
					<div class="queue-stat">
						<span class="label">Queued requests:</span>
						<span class="value">{queueInfo.queue_size} / {queueInfo.max_size}</span>
					</div>
					<div class="queue-stat">
						<span class="label">Avg process time:</span>
						<span class="value">{queueInfo.process_time_estimate}s</span>
					</div>
					{#if queueInfo.queue_size > 0}
						<button class="btn-danger btn-sm" onclick={handleClearQueue}>
							Clear Queue
						</button>
					{/if}
				</div>
			</div>
		{/if}

		<!-- Create Button -->
		{#if !showCreateForm && !editingConfig}
			<button class="btn-primary" onclick={() => { showCreateForm = true; resetForm(); }}>
				<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
					<line x1="12" y1="5" x2="12" y2="19"/>
					<line x1="5" y1="12" x2="19" y2="12"/>
				</svg>
				Add Rate Limit Configuration
			</button>
		{/if}

		<!-- Create/Edit Form -->
		{#if showCreateForm || editingConfig}
			<div class="form-card">
				<h4>{editingConfig ? 'Edit Configuration' : 'New Configuration'}</h4>

				<form onsubmit={(e) => { e.preventDefault(); editingConfig ? handleUpdate() : handleCreate(); }}>
					{#if !editingConfig}
						<div class="form-group">
							<label for="target_type">Apply to</label>
							<select id="target_type" bind:value={formData.target_type}>
								<option value="default">Default (all users)</option>
								<option value="api_key">Specific API Key</option>
								<option value="user">Specific User ID</option>
							</select>
						</div>

						{#if formData.target_type === 'api_key'}
							<div class="form-group">
								<label for="api_key_id">API Key</label>
								<input type="text" id="api_key_id" bind:value={formData.api_key_id} placeholder="API Key ID" required />
							</div>
						{:else if formData.target_type === 'user'}
							<div class="form-group">
								<label for="user_id">User ID</label>
								<input type="text" id="user_id" bind:value={formData.user_id} placeholder="e.g., admin" required />
							</div>
						{/if}
					{/if}

					<div class="form-row">
						<div class="form-group">
							<label for="requests_per_minute">Requests per minute</label>
							<input type="number" id="requests_per_minute" bind:value={formData.requests_per_minute} min="1" max="10000" />
						</div>
						<div class="form-group">
							<label for="requests_per_hour">Requests per hour</label>
							<input type="number" id="requests_per_hour" bind:value={formData.requests_per_hour} min="1" max="100000" />
						</div>
					</div>

					<div class="form-row">
						<div class="form-group">
							<label for="requests_per_day">Requests per day</label>
							<input type="number" id="requests_per_day" bind:value={formData.requests_per_day} min="1" max="1000000" />
						</div>
						<div class="form-group">
							<label for="concurrent_requests">Concurrent requests</label>
							<input type="number" id="concurrent_requests" bind:value={formData.concurrent_requests} min="1" max="100" />
						</div>
					</div>

					<div class="form-row">
						<div class="form-group">
							<label for="priority">Priority</label>
							<input type="number" id="priority" bind:value={formData.priority} min="-100" max="100" />
							<span class="help-text">Higher priority users are processed first when queued</span>
						</div>
						<div class="form-group checkbox-group">
							<label>
								<input type="checkbox" bind:checked={formData.is_unlimited} />
								<span>Unlimited (bypass all limits)</span>
							</label>
						</div>
					</div>

					<div class="form-actions">
						<button type="button" class="btn-secondary" onclick={cancelEdit}>Cancel</button>
						<button type="submit" class="btn-primary">
							{editingConfig ? 'Update' : 'Create'}
						</button>
					</div>
				</form>
			</div>
		{/if}

		<!-- Configurations List -->
		<div class="configs-list">
			<h4>Configured Limits</h4>
			{#if configs.length === 0}
				<p class="empty-state">No rate limit configurations. Default limits will apply to all users.</p>
			{:else}
				<div class="configs-table">
					<table>
						<thead>
							<tr>
								<th>Target</th>
								<th>Limits</th>
								<th>Priority</th>
								<th>Status</th>
								<th>Actions</th>
							</tr>
						</thead>
						<tbody>
							{#each configs as config}
								<tr>
									<td class="target-cell">
										<span class="target-label">{getTargetLabel(config)}</span>
									</td>
									<td class="limits-cell">
										{#if config.is_unlimited}
											<span class="unlimited-badge">Unlimited</span>
										{:else}
											<div class="limits-info">
												<span>{config.requests_per_minute}/min</span>
												<span>{config.requests_per_hour}/hr</span>
												<span>{config.requests_per_day}/day</span>
												<span>{config.concurrent_requests} concurrent</span>
											</div>
										{/if}
									</td>
									<td class="priority-cell">
										<span class="priority-badge" class:high={config.priority > 0} class:low={config.priority < 0}>
											{config.priority}
										</span>
									</td>
									<td class="status-cell">
										{#if config.is_unlimited}
											<span class="status unlimited">Unlimited</span>
										{:else}
											<span class="status active">Active</span>
										{/if}
									</td>
									<td class="actions-cell">
										<button class="btn-icon" title="Edit" onclick={() => startEdit(config)}>
											<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
												<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
												<path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
											</svg>
										</button>
										<button class="btn-icon danger" title="Delete" onclick={() => handleDelete(config)}>
											<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
												<polyline points="3 6 5 6 21 6"/>
												<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
											</svg>
										</button>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>

		<!-- Default limits info -->
		<div class="defaults-info">
			<h4>Default Limits</h4>
			<p>When no specific configuration exists, these limits apply:</p>
			<div class="defaults-grid">
				<div class="default-item">
					<span class="label">Per minute:</span>
					<span class="value">20</span>
				</div>
				<div class="default-item">
					<span class="label">Per hour:</span>
					<span class="value">200</span>
				</div>
				<div class="default-item">
					<span class="label">Per day:</span>
					<span class="value">1,000</span>
				</div>
				<div class="default-item">
					<span class="label">Concurrent:</span>
					<span class="value">3</span>
				</div>
			</div>
			<p class="note">Admin users bypass rate limits by default.</p>
		</div>
	{/if}
</div>

<style>
	.rate-limit-manager {
		display: flex;
		flex-direction: column;
		gap: 1.5rem;
	}

	.header h3 {
		margin: 0 0 0.25rem 0;
		font-size: 1.125rem;
		font-weight: 600;
		color: var(--text-primary);
	}

	.header .description {
		margin: 0;
		font-size: 0.875rem;
		color: var(--text-secondary);
	}

	.loading {
		text-align: center;
		padding: 2rem;
		color: var(--text-secondary);
	}

	.queue-status {
		padding: 1rem;
		background: var(--bg-secondary);
		border-radius: 0.5rem;
		border: 1px solid var(--border-color);
	}

	.queue-status h4 {
		margin: 0 0 0.75rem 0;
		font-size: 0.875rem;
		font-weight: 600;
	}

	.queue-info {
		display: flex;
		align-items: center;
		gap: 1.5rem;
		flex-wrap: wrap;
	}

	.queue-stat {
		display: flex;
		gap: 0.5rem;
		font-size: 0.875rem;
	}

	.queue-stat .label {
		color: var(--text-secondary);
	}

	.queue-stat .value {
		font-weight: 500;
	}

	.btn-primary {
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.5rem 1rem;
		background: var(--primary);
		color: var(--primary-foreground);
		border: none;
		border-radius: 0.375rem;
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: filter 0.2s;
	}

	.btn-primary:hover {
		filter: brightness(1.1);
	}

	.btn-primary:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.btn-primary svg {
		width: 1rem;
		height: 1rem;
	}

	.btn-secondary {
		padding: 0.5rem 1rem;
		background: var(--secondary);
		color: var(--secondary-foreground);
		border: 1px solid var(--border);
		border-radius: 0.375rem;
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.btn-secondary:hover {
		background: var(--hover-overlay);
		border-color: var(--accent);
	}

	.btn-secondary:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.btn-danger {
		padding: 0.5rem 1rem;
		background: var(--destructive);
		color: var(--destructive-foreground, white);
		border: none;
		border-radius: 0.375rem;
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: filter 0.2s;
	}

	.btn-danger:hover {
		filter: brightness(1.1);
	}

	.btn-danger:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.btn-sm {
		padding: 0.25rem 0.5rem;
		font-size: 0.75rem;
	}

	.form-card {
		padding: 1.5rem;
		background: var(--bg-secondary);
		border-radius: 0.5rem;
		border: 1px solid var(--border-color);
	}

	.form-card h4 {
		margin: 0 0 1rem 0;
		font-size: 1rem;
		font-weight: 600;
	}

	.form-group {
		margin-bottom: 1rem;
	}

	.form-group label {
		display: block;
		margin-bottom: 0.375rem;
		font-size: 0.875rem;
		font-weight: 500;
		color: var(--text-primary);
	}

	.form-group input[type="text"],
	.form-group input[type="number"],
	.form-group select {
		width: 100%;
		padding: 0.5rem 0.75rem;
		background: var(--bg-primary);
		border: 1px solid var(--border-color);
		border-radius: 0.375rem;
		font-size: 0.875rem;
		color: var(--text-primary);
	}

	.form-group input:focus,
	.form-group select:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 20%, transparent);
	}

	.form-row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 1rem;
	}

	.checkbox-group label {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		cursor: pointer;
	}

	.checkbox-group input[type="checkbox"] {
		width: auto;
	}

	.help-text {
		display: block;
		margin-top: 0.25rem;
		font-size: 0.75rem;
		color: var(--text-secondary);
	}

	.form-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.75rem;
		margin-top: 1.5rem;
		padding-top: 1rem;
		border-top: 1px solid var(--border-color);
	}

	.configs-list h4 {
		margin: 0 0 0.75rem 0;
		font-size: 0.875rem;
		font-weight: 600;
	}

	.empty-state {
		padding: 1.5rem;
		text-align: center;
		color: var(--text-secondary);
		background: var(--bg-secondary);
		border-radius: 0.5rem;
		border: 1px dashed var(--border-color);
	}

	.configs-table {
		overflow-x: auto;
	}

	.configs-table table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.875rem;
	}

	.configs-table th,
	.configs-table td {
		padding: 0.75rem;
		text-align: left;
		border-bottom: 1px solid var(--border-color);
	}

	.configs-table th {
		font-weight: 600;
		color: var(--text-secondary);
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.target-label {
		font-weight: 500;
	}

	.limits-info {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
	}

	.limits-info span {
		padding: 0.125rem 0.375rem;
		background: var(--bg-tertiary);
		border-radius: 0.25rem;
		font-size: 0.75rem;
	}

	.unlimited-badge {
		padding: 0.125rem 0.5rem;
		background: color-mix(in oklch, var(--info) 15%, transparent);
		color: var(--info);
		border: 1px solid color-mix(in oklch, var(--info) 30%, transparent);
		border-radius: 0.25rem;
		font-size: 0.75rem;
		font-weight: 500;
	}

	.priority-badge {
		display: inline-block;
		padding: 0.125rem 0.5rem;
		background: var(--muted);
		border-radius: 0.25rem;
		font-size: 0.75rem;
		font-weight: 500;
	}

	.priority-badge.high {
		background: color-mix(in oklch, var(--success) 15%, transparent);
		color: var(--success);
		border: 1px solid color-mix(in oklch, var(--success) 30%, transparent);
	}

	.priority-badge.low {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		border: 1px solid color-mix(in oklch, var(--destructive) 30%, transparent);
	}

	.status {
		font-size: 0.75rem;
		font-weight: 500;
	}

	.status.active {
		color: var(--success);
	}

	.status.unlimited {
		color: var(--info);
	}

	.btn-icon {
		padding: 0.375rem;
		background: transparent;
		border: none;
		border-radius: 0.25rem;
		cursor: pointer;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.btn-icon:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.btn-icon:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.btn-icon.danger:hover {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
	}

	.btn-icon svg {
		width: 1rem;
		height: 1rem;
	}

	.defaults-info {
		padding: 1rem;
		background: var(--bg-secondary);
		border-radius: 0.5rem;
		border: 1px solid var(--border-color);
	}

	.defaults-info h4 {
		margin: 0 0 0.5rem 0;
		font-size: 0.875rem;
		font-weight: 600;
	}

	.defaults-info p {
		margin: 0 0 0.75rem 0;
		font-size: 0.875rem;
		color: var(--text-secondary);
	}

	.defaults-grid {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 0.75rem;
	}

	.default-item {
		display: flex;
		flex-direction: column;
		gap: 0.125rem;
	}

	.default-item .label {
		font-size: 0.75rem;
		color: var(--text-secondary);
	}

	.default-item .value {
		font-size: 1rem;
		font-weight: 600;
		color: var(--text-primary);
	}

	.defaults-info .note {
		margin: 0.75rem 0 0 0;
		padding-top: 0.75rem;
		border-top: 1px solid var(--border-color);
		font-size: 0.75rem;
		color: var(--text-tertiary);
	}

	@media (max-width: 640px) {
		.form-row {
			grid-template-columns: 1fr;
		}

		.defaults-grid {
			grid-template-columns: repeat(2, 1fr);
		}

		.configs-table {
			font-size: 0.75rem;
		}

		.configs-table th,
		.configs-table td {
			padding: 0.5rem;
		}
	}
</style>
