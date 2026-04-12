<script lang="ts">
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import './settings-shared.css';

	interface Props {
		expanded: boolean;
		onToggle: () => void;
		currentPassword: string;
		onCurrentPasswordChange: (value: string) => void;
		newPassword: string;
		onNewPasswordChange: (value: string) => void;
		confirmPassword: string;
		onConfirmPasswordChange: (value: string) => void;
		changingPassword: boolean;
		passwordChangeSuccess: string;
		passwordChangeError: string;
		onChangePassword: () => void;
	}

	let {
		expanded,
		onToggle,
		currentPassword,
		onCurrentPasswordChange,
		newPassword,
		onNewPasswordChange,
		confirmPassword,
		onConfirmPasswordChange,
		changingPassword,
		passwordChangeSuccess,
		passwordChangeError,
		onChangePassword,
	}: Props = $props();
</script>

<section class="section">
	<button class="section-header" onclick={onToggle}>
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
						autocomplete="off"
						value={currentPassword}
						oninput={(e) => onCurrentPasswordChange(e.currentTarget.value)}
						placeholder="Current password"
						class="input"
					/>
					<input
						type="password"
						autocomplete="new-password"
						value={newPassword}
						oninput={(e) => onNewPasswordChange(e.currentTarget.value)}
						placeholder="New password (8+ chars)"
						class="input"
					/>
					<input
						type="password"
						autocomplete="new-password"
						value={confirmPassword}
						oninput={(e) => onConfirmPasswordChange(e.currentTarget.value)}
						placeholder="Confirm password"
						class="input"
					/>
					<button
						onclick={onChangePassword}
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
