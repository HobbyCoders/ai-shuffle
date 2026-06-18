<script lang="ts">
	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import { theme, themePreference, currentPresetId, themePresets } from '$lib/stores/theme.svelte';
	import { timezonePreference } from '$lib/stores/timezonePreference.svelte';
	import type { WorkspaceValidation } from '$lib/api/client';
	import './settings-shared.css';

	interface Props {
		expanded: boolean;
		onToggle: () => void;
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
		expanded,
		onToggle,
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
</script>

<section class="section">
	<button class="section-header" onclick={onToggle}>
		<h3>General</h3>
		<svg class="chevron" class:expanded viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
			<polyline points="6 9 12 15 18 9"/>
		</svg>
	</button>
	{#if expanded}
		<div class="section-content" transition:slide={{ duration: 200, easing: quintOut }}>
			<!-- Theme -->
			<div class="setting-row">
				<div class="setting-info">
					<span>Theme</span>
					<span class="setting-desc">Choose your preferred appearance</span>
				</div>
				<div class="theme-switcher">
					<button
						class="theme-option"
						class:active={$themePreference === 'light'}
						onclick={() => theme.setTheme('light')}
						aria-label="Light theme"
					>
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<circle cx="12" cy="12" r="5"/>
							<path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
						</svg>
					</button>
					<button
						class="theme-option"
						class:active={$themePreference === 'dark'}
						onclick={() => theme.setTheme('dark')}
						aria-label="Dark theme"
					>
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
						</svg>
					</button>
					<button
						class="theme-option"
						class:active={$themePreference === 'system'}
						onclick={() => theme.setTheme('system')}
						aria-label="System theme"
					>
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
							<line x1="8" y1="21" x2="16" y2="21"/>
							<line x1="12" y1="17" x2="12" y2="21"/>
						</svg>
					</button>
				</div>
			</div>

			<!-- Theme Preset -->
			<div class="setting-row">
				<div class="setting-info">
					<label for="theme-preset">Theme Style</label>
					<span class="setting-desc">Choose a color palette</span>
				</div>
				<select
					id="theme-preset"
					class="input"
					value={$currentPresetId ?? ''}
					onchange={(e) => theme.setPreset(e.currentTarget.value || null)}
				>
					<option value="">Default</option>
					{#each themePresets as preset}
						<option value={preset.id}>{preset.label}</option>
					{/each}
				</select>
			</div>

			<!-- Workspace Path -->
			<div class="setting-row vertical">
				<div class="setting-info">
					<label for="settings-workspace-path">Workspace Path</label>
					<span class="setting-desc">Base directory for projects</span>
				</div>
				<div class="input-group">
					<input
						id="settings-workspace-path"
						type="text"
						value={workspacePath}
						oninput={(e) => onWorkspacePathChange(e.currentTarget.value)}
						placeholder="/path/to/workspace"
						class="input"
					/>
					<button
						onclick={onValidateWorkspace}
						disabled={validatingWorkspace || !workspacePath.trim()}
						class="btn secondary"
					>
						{validatingWorkspace ? '...' : 'Validate'}
					</button>
					<button
						onclick={onSaveWorkspace}
						disabled={savingWorkspace || !workspacePath.trim()}
						class="btn primary"
					>
						{savingWorkspace ? '...' : 'Save'}
					</button>
				</div>
				{#if workspaceValidation}
					<div class="validation-result" class:valid={workspaceValidation.valid}>
						{workspaceValidation.valid ? '✓ Valid path' : `✗ ${workspaceValidation.error}`}
					</div>
				{/if}
				{#if workspaceSuccess}
					<div class="success-msg">{workspaceSuccess}</div>
				{/if}
				{#if workspaceError}
					<div class="error-msg">{workspaceError}</div>
				{/if}
			</div>

			<!-- Timezone -->
			<div class="setting-row">
				<div class="setting-info">
					<label for="settings-timezone">Timezone</label>
					<span class="setting-desc">Used for timestamps across the UI</span>
				</div>
				<select
					id="settings-timezone"
					class="input"
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
	{/if}
</section>
