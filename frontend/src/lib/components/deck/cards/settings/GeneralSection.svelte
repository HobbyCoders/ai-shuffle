<script lang="ts">
	import { Palette, FolderOpen, RefreshCw, Info, ShieldAlert } from 'lucide-svelte';
	import { desktop } from '$lib/services/desktop';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import ApplicationUpdates from './ApplicationUpdates.svelte';
	import { theme, themePreference, currentPresetId, themePresets } from '$lib/stores/theme.svelte';
	import { timezonePreference } from '$lib/stores/timezonePreference.svelte';
	import type { WorkspaceValidation } from '$lib/api/client';

	interface Props {
		workspacePath: string;
		onWorkspacePathChange: (value: string) => void;
		workspaceValidation: WorkspaceValidation | null;
		validatingWorkspace: boolean;
		savingWorkspace: boolean;
		workspaceSuccess: string;
		workspaceError: string;
		currentTimezone: string;
		onValidateWorkspace: () => void;
		onSaveWorkspace: () => void;
	}

	let {
		workspacePath,
		onWorkspacePathChange,
		workspaceValidation,
		validatingWorkspace,
		savingWorkspace,
		workspaceSuccess,
		workspaceError,
		currentTimezone,
		onValidateWorkspace,
		onSaveWorkspace,
	}: Props = $props();

	// Desktop only: agents run on this computer as the signed-in user.
	const agentAccess = desktop?.agentAccess ?? null;
</script>

{#if agentAccess}
	<div class="set-group">
		<div class="set-grouphead"><h3><ShieldAlert />{agentAccess.title}</h3></div>
		<div class="set-row stack">
			<div class="ctl agent-access">
				<p>{agentAccess.message}</p>
				<ul>
					{#each agentAccess.points as point}
						<li>{point}</li>
					{/each}
				</ul>
				<p class="footer">{agentAccess.footer}</p>
			</div>
		</div>
	</div>
{/if}

<!-- Appearance -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Palette />Appearance</h3>
	</div>
	<div class="set-row">
		<div class="lab"><h4>Theme</h4></div>
		<div class="ctl">
			<div class="seg">
				<button type="button" class:on={$themePreference === 'light'} onclick={() => theme.setTheme('light')}>Light</button>
				<button type="button" class:on={$themePreference === 'dark'} onclick={() => theme.setTheme('dark')}>Dark</button>
				<button type="button" class:on={$themePreference === 'system'} onclick={() => theme.setTheme('system')}>System</button>
			</div>
		</div>
	</div>
	<div class="set-row">
		<div class="lab"><h4>Theme style</h4></div>
		<div class="ctl">
			<select
				class="sel"
				value={$currentPresetId ?? ''}
				onchange={(e) => theme.setPreset(e.currentTarget.value || null)}
			>
				<option value="">Default</option>
				{#each themePresets as preset}
					<option value={preset.id}>{preset.label}</option>
				{/each}
			</select>
		</div>
	</div>
</div>

{#if $isAdmin}
	<div class="set-group">
		<div class="set-grouphead"><h3><RefreshCw />Application updates</h3></div>
		<ApplicationUpdates />
	</div>
{/if}

<!-- Workspace -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><FolderOpen />Workspace</h3>
	</div>
	<div class="set-row stack">
		<div class="lab"><h4>Workspace directory</h4></div>
		<div class="ctl">
			<div class="ws-row">
				<input
					type="text"
					value={workspacePath}
					oninput={(e) => onWorkspacePathChange(e.currentTarget.value)}
					placeholder="/path/to/workspace"
					class="inp mono ws-input"
				/>
				<button
					type="button"
					class="ed-btn"
					onclick={onValidateWorkspace}
					disabled={validatingWorkspace || !workspacePath.trim()}
				>{validatingWorkspace ? '…' : 'Validate'}</button>
				<button
					type="button"
					class="ed-btn primary"
					onclick={onSaveWorkspace}
					disabled={savingWorkspace || !workspacePath.trim()}
				>{savingWorkspace ? '…' : 'Save'}</button>
			</div>
			{#if workspaceValidation}
				<p class="msg" class:ok={workspaceValidation.valid} class:err={!workspaceValidation.valid}>
					{workspaceValidation.valid ? '✓ Valid path' : `✗ ${workspaceValidation.error}`}
				</p>
			{/if}
			{#if workspaceSuccess}<p class="msg ok">{workspaceSuccess}</p>{/if}
			{#if workspaceError}<p class="msg err">{workspaceError}</p>{/if}
		</div>
	</div>
	<div class="set-row">
		<div class="lab"><h4>Timezone</h4></div>
		<div class="ctl">
			<select
				class="sel"
				value={currentTimezone}
				onchange={(e) => timezonePreference.setTimezone(e.currentTarget.value)}
			>
				<option value="local">Browser Default</option>
				<option value="UTC">UTC</option>
				<optgroup label="Americas">
					<option value="America/New_York">Eastern (New York)</option>
					<option value="America/Chicago">Central (Chicago)</option>
					<option value="America/Denver">Mountain (Denver)</option>
					<option value="America/Los_Angeles">Pacific (Los Angeles)</option>
					<option value="America/Anchorage">Alaska</option>
					<option value="Pacific/Honolulu">Hawaii</option>
					<option value="America/Sao_Paulo">S&atilde;o Paulo</option>
				</optgroup>
				<optgroup label="Europe &amp; Africa">
					<option value="Europe/London">London</option>
					<option value="Europe/Paris">Paris / Berlin</option>
					<option value="Europe/Moscow">Moscow</option>
					<option value="Africa/Cairo">Cairo</option>
				</optgroup>
				<optgroup label="Asia &amp; Pacific">
					<option value="Asia/Dubai">Dubai</option>
					<option value="Asia/Kolkata">India (Kolkata)</option>
					<option value="Asia/Shanghai">China (Shanghai)</option>
					<option value="Asia/Tokyo">Tokyo</option>
					<option value="Asia/Seoul">Seoul</option>
					<option value="Australia/Sydney">Sydney</option>
					<option value="Pacific/Auckland">Auckland</option>
				</optgroup>
			</select>
		</div>
	</div>
</div>

<!-- About -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Info />About</h3>
	</div>
	<div class="set-row">
		<div class="lab"><h4>Version</h4></div>
		<div class="ctl"><span class="version">AI Shuffle {__APP_VERSION__}</span></div>
	</div>
</div>

<style>
	.ws-row {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
	}
	.ws-input {
		flex: 1;
		min-width: 220px;
	}
	.agent-access p,
	.agent-access li {
		font-size: 13px;
		line-height: 1.5;
	}
	.agent-access p {
		margin: 0 0 6px;
	}
	.agent-access ul {
		margin: 0 0 6px;
		padding-left: 18px;
	}
	.agent-access .footer {
		color: var(--muted-foreground);
	}
	.version {
		font-family: var(--font-mono);
		font-size: 12.5px;
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
