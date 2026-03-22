<script lang="ts">
	/**
	 * SecuritySettings - Password change
	 */
	import '../deck/cards/card-design-system.css';
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { changePassword } from '$lib/api/auth';
	import { getErrorMessage } from '$lib/api/client-core';

	let expanded = $state(false);

	// Password change state
	let currentPassword = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let changingPassword = $state(false);
	let passwordChangeError = $state('');
	let passwordChangeSuccess = $state('');

	async function handleChangePassword() {
		if (newPassword !== confirmPassword) {
			passwordChangeError = 'Passwords do not match';
			return;
		}
		if (newPassword.length < 8) {
			passwordChangeError = 'Password must be at least 8 characters';
			return;
		}
		changingPassword = true;
		passwordChangeError = '';
		passwordChangeSuccess = '';
		try {
			await changePassword(currentPassword, newPassword);
			passwordChangeSuccess = 'Password changed';
			currentPassword = '';
			newPassword = '';
			confirmPassword = '';
			setTimeout(() => passwordChangeSuccess = '', 3000);
		} catch (e: unknown) {
			passwordChangeError = getErrorMessage(e, 'Failed to change password');
		} finally {
			changingPassword = false;
		}
	}
</script>

<section class="section">
	<button class="section-header" onclick={() => expanded = !expanded}>
		<h3>Security</h3>
		<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if expanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<!-- Password Change -->
			<div class="setting-row vertical">
				<div class="setting-info">
					<span>Change Password</span>
				</div>
				<div class="password-form">
					<input
						type="password"
						bind:value={currentPassword}
						placeholder="Current password"
						class="input"
					/>
					<input
						type="password"
						bind:value={newPassword}
						placeholder="New password (8+ chars)"
						class="input"
					/>
					<input
						type="password"
						bind:value={confirmPassword}
						placeholder="Confirm password"
						class="input"
					/>
					<button
						onclick={handleChangePassword}
						disabled={changingPassword || !currentPassword || !newPassword || !confirmPassword}
						class="btn primary"
					>
						{changingPassword ? 'Changing...' : 'Change Password'}
					</button>
				</div>
				{#if passwordChangeSuccess}
					<div class="success-msg">{passwordChangeSuccess}</div>
				{/if}
				{#if passwordChangeError}
					<div class="error-msg">{passwordChangeError}</div>
				{/if}
			</div>
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

	.setting-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 8px 0;
	}

	.setting-row.vertical {
		flex-direction: column;
		align-items: stretch;
	}

	.setting-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
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

	.password-form {
		display: flex;
		flex-direction: column;
		gap: 8px;
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

	.success-msg {
		font-size: 0.8125rem;
		color: var(--success);
	}

	.error-msg {
		font-size: 0.8125rem;
		color: var(--destructive);
	}
</style>
