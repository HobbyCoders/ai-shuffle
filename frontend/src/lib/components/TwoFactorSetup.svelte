<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import {
		get2FAStatus,
		setup2FA,
		verify2FA,
		disable2FA,
		regenerateRecoveryCodes,
		type TwoFactorStatusResponse,
		type TwoFactorSetupResponse
	} from '$lib/api/auth';

	const dispatch = createEventDispatcher();

	// State
	let status: TwoFactorStatusResponse | null = null;
	let setupData: TwoFactorSetupResponse | null = null;
	let recoveryCodes: string[] = [];

	let loading = false;
	let error = '';
	let step: 'status' | 'setup' | 'verify' | 'recovery' | 'disable' = 'status';

	// Form inputs
	let verifyCode = '';
	let disableCode = '';
	let disablePassword = '';

	// Load initial status
	async function loadStatus() {
		loading = true;
		error = '';
		try {
			status = await get2FAStatus();
		} catch (e: any) {
			error = e.detail || 'Failed to load 2FA status';
		} finally {
			loading = false;
		}
	}

	// Start setup process
	async function startSetup() {
		loading = true;
		error = '';
		try {
			setupData = await setup2FA();
			step = 'setup';
		} catch (e: any) {
			error = e.detail || 'Failed to start 2FA setup';
		} finally {
			loading = false;
		}
	}

	// Verify and enable 2FA
	async function verifyAndEnable() {
		if (verifyCode.length !== 6) {
			error = 'Please enter a 6-digit code';
			return;
		}

		loading = true;
		error = '';
		try {
			const result = await verify2FA(verifyCode);
			recoveryCodes = result.recovery_codes;
			step = 'recovery';
			verifyCode = '';
			// Refresh status
			status = await get2FAStatus();
			dispatch('enabled');
		} catch (e: any) {
			error = e.detail || 'Invalid code. Please try again.';
		} finally {
			loading = false;
		}
	}

	// Disable 2FA
	async function handleDisable() {
		if (disableCode.length !== 6) {
			error = 'Please enter a 6-digit code';
			return;
		}
		if (!disablePassword) {
			error = 'Please enter your password';
			return;
		}

		loading = true;
		error = '';
		try {
			await disable2FA(disableCode, disablePassword);
			status = await get2FAStatus();
			step = 'status';
			disableCode = '';
			disablePassword = '';
			dispatch('disabled');
		} catch (e: any) {
			error = e.detail || 'Failed to disable 2FA';
		} finally {
			loading = false;
		}
	}

	// Regenerate recovery codes
	async function handleRegenerateCodes() {
		loading = true;
		error = '';
		try {
			const result = await regenerateRecoveryCodes();
			recoveryCodes = result.codes;
			step = 'recovery';
			status = await get2FAStatus();
		} catch (e: any) {
			error = e.detail || 'Failed to regenerate recovery codes';
		} finally {
			loading = false;
		}
	}

	// Copy codes to clipboard
	async function copyRecoveryCodes() {
		try {
			await navigator.clipboard.writeText(recoveryCodes.join('\n'));
		} catch {
			// Fallback for older browsers
			const textarea = document.createElement('textarea');
			textarea.value = recoveryCodes.join('\n');
			document.body.appendChild(textarea);
			textarea.select();
			document.execCommand('copy');
			document.body.removeChild(textarea);
		}
	}

	// Initialize
	loadStatus();
</script>

<div class="two-factor-setup">
	{#if loading && !status}
		<div class="loading">
			<span class="spinner"></span>
			Loading...
		</div>
	{:else if step === 'status' && status}
		<!-- Status View -->
		<div class="status-view">
			<div class="status-header">
				<div class="status-icon" class:enabled={status.enabled}>
					{status.enabled ? '🔒' : '🔓'}
				</div>
				<div class="status-text">
					<h3>Two-Factor Authentication</h3>
					<p class="status-label" class:enabled={status.enabled}>
						{status.enabled ? 'Enabled' : 'Disabled'}
					</p>
				</div>
			</div>

			{#if status.enabled}
				<div class="recovery-info">
					<p>
						<strong>{status.recovery_codes_count}</strong> recovery codes remaining
					</p>
					{#if status.recovery_codes_count <= 3}
						<p class="warning">
							Warning: You have few recovery codes left. Consider generating new ones.
						</p>
					{/if}
				</div>

				<div class="actions">
					<button class="btn btn-secondary" on:click={handleRegenerateCodes} disabled={loading}>
						Generate New Recovery Codes
					</button>
					<button class="btn btn-danger" on:click={() => (step = 'disable')} disabled={loading}>
						Disable 2FA
					</button>
				</div>
			{:else}
				<p class="description">
					Add an extra layer of security to your account by requiring a verification code
					from your authenticator app when you sign in.
				</p>

				<div class="actions">
					<button class="btn btn-primary" on:click={startSetup} disabled={loading}>
						{loading ? 'Setting up...' : 'Enable Two-Factor Authentication'}
					</button>
				</div>
			{/if}
		</div>
	{:else if step === 'setup' && setupData}
		<!-- Setup View - QR Code -->
		<div class="setup-view">
			<h3>Scan QR Code</h3>
			<p class="instructions">
				Scan this QR code with your authenticator app (Google Authenticator, Authy, 1Password, etc.)
			</p>

			<div class="qr-container">
				<img src={setupData.qr_code} alt="2FA QR Code" class="qr-code" />
			</div>

			<details class="manual-entry">
				<summary>Can't scan? Enter code manually</summary>
				<div class="secret-key">
					<code>{setupData.secret}</code>
					<button
						class="btn-copy"
						on:click={() => navigator.clipboard.writeText(setupData?.secret || '')}
					>
						Copy
					</button>
				</div>
			</details>

			<div class="verify-section">
				<h4>Enter verification code</h4>
				<p>Enter the 6-digit code from your authenticator app to verify setup.</p>

				<input
					type="text"
					class="input code-input"
					bind:value={verifyCode}
					placeholder="000000"
					maxlength="6"
					pattern="[0-9]*"
					inputmode="numeric"
					autocomplete="one-time-code"
				/>

				{#if error}
					<p class="error">{error}</p>
				{/if}

				<div class="actions">
					<button class="btn btn-secondary" on:click={() => (step = 'status')} disabled={loading}>
						Cancel
					</button>
					<button
						class="btn btn-primary"
						on:click={verifyAndEnable}
						disabled={loading || verifyCode.length !== 6}
					>
						{loading ? 'Verifying...' : 'Verify and Enable'}
					</button>
				</div>
			</div>
		</div>
	{:else if step === 'recovery'}
		<!-- Recovery Codes View -->
		<div class="recovery-view">
			<h3>Save Your Recovery Codes</h3>
			<p class="warning-box">
				<strong>Important:</strong> Save these recovery codes in a safe place. If you lose access
				to your authenticator app, you can use these codes to sign in. Each code can only be used once.
			</p>

			<div class="codes-container">
				{#each recoveryCodes as code, i}
					<div class="code-item">
						<span class="code-number">{i + 1}.</span>
						<code>{code}</code>
					</div>
				{/each}
			</div>

			<div class="actions">
				<button class="btn btn-secondary" on:click={copyRecoveryCodes}>
					Copy All Codes
				</button>
				<button class="btn btn-primary" on:click={() => (step = 'status')}>
					I've Saved My Codes
				</button>
			</div>
		</div>
	{:else if step === 'disable'}
		<!-- Disable 2FA View -->
		<div class="disable-view">
			<h3>Disable Two-Factor Authentication</h3>
			<p class="warning-box">
				<strong>Warning:</strong> Disabling 2FA will make your account less secure.
				Enter your current authenticator code and password to confirm.
			</p>

			<div class="form-group">
				<label for="disable-code">Authenticator Code</label>
				<input
					type="text"
					id="disable-code"
					class="input code-input"
					bind:value={disableCode}
					placeholder="000000"
					maxlength="6"
					pattern="[0-9]*"
					inputmode="numeric"
					autocomplete="one-time-code"
				/>
			</div>

			<div class="form-group">
				<label for="disable-password">Password</label>
				<input
					type="password"
					id="disable-password"
					class="input"
					bind:value={disablePassword}
					placeholder="Enter your password"
					autocomplete="current-password"
				/>
			</div>

			{#if error}
				<p class="error">{error}</p>
			{/if}

			<div class="actions">
				<button class="btn btn-secondary" on:click={() => (step = 'status')} disabled={loading}>
					Cancel
				</button>
				<button
					class="btn btn-danger"
					on:click={handleDisable}
					disabled={loading || disableCode.length !== 6 || !disablePassword}
				>
					{loading ? 'Disabling...' : 'Disable 2FA'}
				</button>
			</div>
		</div>
	{/if}
</div>

<style>
	.two-factor-setup {
		max-width: 500px;
	}

	.loading {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		color: var(--color-text-secondary);
		padding: 1rem;
	}

	.spinner {
		display: inline-block;
		width: 1rem;
		height: 1rem;
		border: 2px solid currentColor;
		border-radius: 50%;
		border-top-color: transparent;
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	.status-header {
		display: flex;
		align-items: center;
		gap: 1rem;
		margin-bottom: 1rem;
	}

	.status-icon {
		font-size: 2rem;
		width: 3rem;
		height: 3rem;
		display: flex;
		align-items: center;
		justify-content: center;
		border-radius: 0.5rem;
		background: var(--color-bg-tertiary);
	}

	.status-icon.enabled {
		background: rgba(34, 197, 94, 0.2);
	}

	.status-text h3 {
		margin: 0;
		font-size: 1.125rem;
	}

	.status-label {
		margin: 0.25rem 0 0;
		font-size: 0.875rem;
		color: var(--color-text-secondary);
	}

	.status-label.enabled {
		color: #22c55e;
	}

	.description {
		color: var(--color-text-secondary);
		margin-bottom: 1.5rem;
	}

	.recovery-info {
		background: var(--color-bg-tertiary);
		padding: 1rem;
		border-radius: 0.5rem;
		margin-bottom: 1rem;
	}

	.recovery-info p {
		margin: 0;
	}

	.recovery-info .warning {
		color: #f59e0b;
		margin-top: 0.5rem;
		font-size: 0.875rem;
	}

	.actions {
		display: flex;
		gap: 0.75rem;
		margin-top: 1.5rem;
	}

	.btn {
		padding: 0.5rem 1rem;
		border-radius: 0.375rem;
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: opacity 0.2s;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn-primary {
		background: var(--color-primary);
		color: white;
		border: none;
	}

	.btn-secondary {
		background: var(--color-bg-tertiary);
		color: var(--color-text);
		border: 1px solid var(--color-border);
	}

	.btn-danger {
		background: #ef4444;
		color: white;
		border: none;
	}

	.qr-container {
		display: flex;
		justify-content: center;
		margin: 1.5rem 0;
	}

	.qr-code {
		border-radius: 0.5rem;
		background: white;
		padding: 0.5rem;
	}

	.manual-entry {
		margin: 1rem 0;
	}

	.manual-entry summary {
		cursor: pointer;
		color: var(--color-primary);
		font-size: 0.875rem;
	}

	.secret-key {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-top: 0.75rem;
		padding: 0.75rem;
		background: var(--color-bg-tertiary);
		border-radius: 0.375rem;
	}

	.secret-key code {
		flex: 1;
		font-family: monospace;
		font-size: 0.875rem;
		word-break: break-all;
	}

	.btn-copy {
		padding: 0.25rem 0.5rem;
		font-size: 0.75rem;
		background: var(--color-bg-secondary);
		border: 1px solid var(--color-border);
		border-radius: 0.25rem;
		cursor: pointer;
	}

	.verify-section {
		margin-top: 1.5rem;
		padding-top: 1.5rem;
		border-top: 1px solid var(--color-border);
	}

	.verify-section h4 {
		margin: 0 0 0.5rem;
	}

	.verify-section p {
		color: var(--color-text-secondary);
		font-size: 0.875rem;
		margin-bottom: 1rem;
	}

	.code-input {
		font-family: monospace;
		font-size: 1.5rem;
		text-align: center;
		letter-spacing: 0.25rem;
		max-width: 200px;
	}

	.form-group {
		margin-bottom: 1rem;
	}

	.form-group label {
		display: block;
		margin-bottom: 0.5rem;
		font-size: 0.875rem;
		font-weight: 500;
	}

	.input {
		width: 100%;
		padding: 0.5rem 0.75rem;
		background: var(--color-bg-tertiary);
		border: 1px solid var(--color-border);
		border-radius: 0.375rem;
		color: var(--color-text);
		font-size: 0.875rem;
	}

	.input:focus {
		outline: none;
		border-color: var(--color-primary);
	}

	.error {
		color: #ef4444;
		font-size: 0.875rem;
		margin-top: 0.5rem;
	}

	.warning-box {
		background: rgba(245, 158, 11, 0.1);
		border: 1px solid rgba(245, 158, 11, 0.3);
		border-radius: 0.5rem;
		padding: 1rem;
		margin-bottom: 1.5rem;
		font-size: 0.875rem;
	}

	.codes-container {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 0.5rem;
		margin: 1.5rem 0;
	}

	.code-item {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.5rem 0.75rem;
		background: var(--color-bg-tertiary);
		border-radius: 0.375rem;
		font-family: monospace;
	}

	.code-number {
		color: var(--color-text-secondary);
		font-size: 0.75rem;
	}

	.code-item code {
		font-size: 0.875rem;
	}

	.instructions {
		color: var(--color-text-secondary);
		font-size: 0.875rem;
		margin-bottom: 1rem;
	}
</style>
