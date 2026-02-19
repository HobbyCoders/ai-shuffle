<script lang="ts">
	/**
	 * SecurityInstructionsEditor - System prompt instructions editor (admin only)
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { api, getErrorMessage } from '$lib/api/client';
	import { onMount } from 'svelte';

	let expanded = $state(false);

	// State
	let securityInstructions = $state('');
	let defaultSecurityInstructions = $state('');
	let originalSecurityInstructions = $state('');
	let isSecurityInstructionsCustomized = $state(false);
	let loadingSecurityInstructions = $state(false);
	let savingSecurityInstructions = $state(false);
	let securityInstructionsSuccess = $state('');
	let securityInstructionsError = $state('');

	// Derived: check if instructions have changed
	let securityInstructionsChanged = $derived(securityInstructions !== originalSecurityInstructions);

	onMount(() => {
		loadSecurityInstructions();
	});

	async function loadSecurityInstructions() {
		loadingSecurityInstructions = true;
		securityInstructionsError = '';
		try {
			const response = await api.get<{
				current: string;
				default: string;
				is_customized: boolean;
			}>('/settings/security-instructions');
			securityInstructions = response.current;
			defaultSecurityInstructions = response.default;
			originalSecurityInstructions = response.current;
			isSecurityInstructionsCustomized = response.is_customized;
		} catch (e: unknown) {
			securityInstructionsError = getErrorMessage(e, 'Failed to load security instructions');
			console.error('Failed to load security instructions:', e);
		} finally {
			loadingSecurityInstructions = false;
		}
	}

	async function saveSecurityInstructions() {
		if (!securityInstructions.trim()) {
			securityInstructionsError = 'Instructions cannot be empty. Use Revert to restore defaults.';
			return;
		}
		savingSecurityInstructions = true;
		securityInstructionsError = '';
		securityInstructionsSuccess = '';
		try {
			await api.put('/settings/security-instructions', {
				instructions: securityInstructions
			});
			originalSecurityInstructions = securityInstructions;
			isSecurityInstructionsCustomized = true;
			securityInstructionsSuccess = 'Saved successfully';
			setTimeout(() => securityInstructionsSuccess = '', 3000);
		} catch (e: unknown) {
			securityInstructionsError = getErrorMessage(e, 'Failed to save security instructions');
		} finally {
			savingSecurityInstructions = false;
		}
	}

	async function revertSecurityInstructions() {
		if (!confirm('Are you sure you want to revert to the default security instructions? This will discard any customizations.')) {
			return;
		}
		savingSecurityInstructions = true;
		securityInstructionsError = '';
		securityInstructionsSuccess = '';
		try {
			await api.delete('/settings/security-instructions');
			securityInstructions = defaultSecurityInstructions;
			originalSecurityInstructions = defaultSecurityInstructions;
			isSecurityInstructionsCustomized = false;
			securityInstructionsSuccess = 'Reverted to default';
			setTimeout(() => securityInstructionsSuccess = '', 3000);
		} catch (e: unknown) {
			securityInstructionsError = getErrorMessage(e, 'Failed to revert security instructions');
		} finally {
			savingSecurityInstructions = false;
		}
	}
</script>

<section class="section">
	<button class="section-header" onclick={() => expanded = !expanded}>
		<h3>System Prompt Instructions</h3>
		<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if expanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<p class="setting-desc mb-3">
				These instructions are prepended to all system prompts sent to Claude.
				Use this to add custom instructions that apply across all profiles.
			</p>

			{#if loadingSecurityInstructions}
				<div class="loading-text">Loading...</div>
			{:else}
				<div class="security-instructions-editor">
					<textarea
						bind:value={securityInstructions}
						class="input security-textarea"
						placeholder="Enter security instructions..."
						rows="12"
					></textarea>

					<div class="security-instructions-footer">
						<div class="security-status">
							{#if isSecurityInstructionsCustomized}
								<span class="badge primary">Customized</span>
							{:else}
								<span class="badge muted">Using Default</span>
							{/if}
							{#if securityInstructionsChanged}
								<span class="badge warning">Unsaved changes</span>
							{/if}
						</div>

						<div class="security-actions">
							<button
								onclick={revertSecurityInstructions}
								class="btn secondary compact"
								disabled={!isSecurityInstructionsCustomized || savingSecurityInstructions}
								title="Revert to default instructions"
							>
								Revert to Default
							</button>
							<button
								onclick={saveSecurityInstructions}
								class="btn primary compact"
								disabled={!securityInstructionsChanged || savingSecurityInstructions}
							>
								{savingSecurityInstructions ? 'Saving...' : 'Save Changes'}
							</button>
						</div>
					</div>

					{#if securityInstructionsError}
						<p class="status error">{securityInstructionsError}</p>
					{/if}
					{#if securityInstructionsSuccess}
						<p class="status success">{securityInstructionsSuccess}</p>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</section>

<style>
	.section {
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 12px 16px;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: background 150ms ease;
	}

	.section-header:hover { background: var(--hover-overlay); }
	.section-header:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.section-header h3 {
		margin: 0;
		font-size: 0.9375rem;
		font-weight: 600;
		color: var(--foreground);
	}

	@media (min-width: 640px) {
		.section-header {
			justify-content: center;
			position: relative;
		}
		.section-header .chevron {
			position: absolute;
			right: 16px;
		}
	}

	.chevron {
		width: 20px;
		height: 20px;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}

	.chevron.expanded { transform: rotate(180deg); }

	.section-content {
		padding: 8px 16px 16px;
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.setting-desc {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.mb-3 { margin-bottom: 12px; }

	.security-instructions-editor {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.input {
		padding: 8px 12px;
		background-color: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		color: var(--foreground);
		font-size: 0.875rem;
		transition: border-color 150ms ease, box-shadow 150ms ease;
	}

	.input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px var(--primary-20);
	}

	.input::placeholder { color: var(--muted-foreground); }

	.security-textarea {
		width: 100%;
		min-height: 200px;
		font-family: var(--font-mono);
		font-size: 0.8125rem;
		line-height: 1.5;
		resize: vertical;
	}

	.security-instructions-footer {
		display: flex;
		justify-content: space-between;
		align-items: center;
		flex-wrap: wrap;
		gap: 12px;
	}

	.security-status {
		display: flex;
		gap: 8px;
		align-items: center;
	}

	.security-actions {
		display: flex;
		gap: 8px;
	}

	.badge {
		display: inline-flex;
		align-items: center;
		padding: 2px 8px;
		font-size: 0.6875rem;
		font-weight: 600;
		border-radius: 9999px;
		text-transform: uppercase;
		letter-spacing: 0.025em;
	}

	.badge.primary {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		border: 1px solid color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.badge.muted {
		background: var(--muted);
		color: var(--muted-foreground);
		border: 1px solid var(--border);
	}

	.badge.warning {
		background: color-mix(in oklch, var(--warning) 15%, transparent);
		color: var(--warning);
		border: 1px solid color-mix(in oklch, var(--warning) 30%, transparent);
	}

	.btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 8px 16px;
		border: none;
		border-radius: calc(var(--radius) - 2px);
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	.btn.primary {
		background: var(--primary);
		color: var(--primary-foreground);
	}

	.btn.primary:hover:not(:disabled) { filter: brightness(1.1); }

	.btn.secondary {
		background: var(--secondary);
		color: var(--secondary-foreground);
		border: 1px solid var(--border);
	}

	.btn.secondary:hover:not(:disabled) { background: var(--hover-overlay); }

	.btn.compact {
		padding: 6px 12px;
		font-size: 0.8125rem;
	}

	.status {
		font-size: 0.8125rem;
		margin: 0;
	}

	.status.error { color: var(--destructive); }
	.status.success { color: var(--success); }

	.loading-text {
		text-align: center;
		padding: 20px;
		color: var(--muted-foreground);
	}
</style>
