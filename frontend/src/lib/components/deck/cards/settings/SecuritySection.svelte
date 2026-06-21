<script lang="ts">
	import { Lock } from 'lucide-svelte';

	interface Props {
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

<div class="set-group">
	<div class="set-grouphead">
		<h3><Lock />Security</h3>
		<p>Changing your password re-derives the key that encrypts stored secrets.</p>
	</div>
	<div class="set-row stack">
		<div class="lab"><h4>Change password</h4><p>Enter your current password, then a new one (8+ characters).</p></div>
		<div class="ctl">
			<div class="pw-form">
				<input
					type="password"
					autocomplete="off"
					value={currentPassword}
					oninput={(e) => onCurrentPasswordChange(e.currentTarget.value)}
					placeholder="Current password"
					class="inp pw"
				/>
				<input
					type="password"
					autocomplete="new-password"
					value={newPassword}
					oninput={(e) => onNewPasswordChange(e.currentTarget.value)}
					placeholder="New password (8+ chars)"
					class="inp pw"
				/>
				<input
					type="password"
					autocomplete="new-password"
					value={confirmPassword}
					oninput={(e) => onConfirmPasswordChange(e.currentTarget.value)}
					placeholder="Confirm password"
					class="inp pw"
				/>
				<button
					type="button"
					class="ed-btn primary"
					onclick={onChangePassword}
					disabled={changingPassword || !currentPassword || !newPassword || !confirmPassword}
				>{changingPassword ? 'Changing…' : 'Change password'}</button>
			</div>
			{#if passwordChangeSuccess}<p class="msg ok">{passwordChangeSuccess}</p>{/if}
			{#if passwordChangeError}<p class="msg err">{passwordChangeError}</p>{/if}
		</div>
	</div>
</div>

<style>
	.pw-form {
		display: flex;
		flex-direction: column;
		gap: 8px;
		max-width: 360px;
	}
	.pw {
		width: 100%;
		min-width: 0;
	}
	.pw-form .ed-btn {
		align-self: flex-start;
	}
	.msg {
		margin: 8px 0 0;
		font-size: 12.5px;
	}
	.msg.ok {
		color: var(--success, #16a34a);
	}
	.msg.err {
		color: var(--destructive);
	}
</style>
