<script lang="ts">
	/**
	 * UserSettingsCard - Settings management for API users
	 *
	 * This is a dedicated settings card for API users to manage:
	 * - Profile information (name, description)
	 * - Password changes
	 * - API credentials (based on policy)
	 * - GitHub connection and configuration
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { onMount } from 'svelte';
	import { auth, apiUser, isAdmin } from '$lib/stores/auth';
	import {
		getMyProfile,
		updateMyProfile,
		changeMyPassword,
		getMyCredentials,
		setMyCredential,
		deleteMyCredential,
		getMyGitHubConfig,
		connectGitHub,
		disconnectGitHub,
		listMyGitHubRepos,
		listGitHubBranches,
		setGitHubDefaults,
		type UserProfile,
		type CredentialStatus,
		type GitHubConfig,
		type GitHubRepo,
		type GitHubBranch
	} from '$lib/api/userSelfService';

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let {
		card,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false
	}: Props = $props();

	// Navigation
	type Section = 'profile' | 'credentials' | 'github';
	let activeSection: Section = $state('profile');

	const sections: { id: Section; label: string; icon: string }[] = [
		{
			id: 'profile',
			label: 'Profile',
			icon: 'M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z'
		},
		{
			id: 'credentials',
			label: 'API Keys',
			icon: 'M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z'
		},
		{
			id: 'github',
			label: 'GitHub',
			icon: 'M12 0C5.374 0 0 5.373 0 12c0 5.302 3.438 9.8 8.207 11.387.6.11.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23A11.509 11.509 0 0112 5.803c1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576C20.566 21.797 24 17.3 24 12c0-6.627-5.373-12-12-12z'
		}
	];

	// Data state
	let profile: UserProfile | null = $state(null);
	let credentials: CredentialStatus[] = $state([]);
	let hasMissingRequired = $state(false);
	let githubConfig: GitHubConfig | null = $state(null);
	let githubRepos: GitHubRepo[] = $state([]);
	let selectedRepo: string = $state('');
	let branches: GitHubBranch[] = $state([]);
	let selectedBranch: string = $state('');

	// Loading states
	let loading = $state(true);
	let loadingCredentials = $state(false);
	let loadingGitHub = $state(false);
	let loadingRepos = $state(false);
	let loadingBranches = $state(false);

	// Form states
	let editingName = $state(false);
	let editingDescription = $state(false);
	let nameInput = $state('');
	let descriptionInput = $state('');
	let savingProfile = $state(false);
	let profileSuccess = $state('');
	let profileError = $state('');

	// Password change
	let showPasswordChange = $state(false);
	let currentPassword = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let changingPassword = $state(false);
	let passwordSuccess = $state('');
	let passwordError = $state('');

	// Credential editing
	let editingCredential: string | null = $state(null);
	let credentialInput = $state('');
	let savingCredential = $state(false);
	let credentialSuccess = $state('');
	let credentialError = $state('');

	// GitHub connection
	let showGitHubConnect = $state(false);
	let gitHubPAT = $state('');
	let connectingGitHub = $state(false);
	let gitHubError = $state('');
	let gitHubSuccess = $state('');
	let savingGitHubDefaults = $state(false);

	// Load data on mount
	onMount(async () => {
		await loadProfile();
		await loadCredentials();
		await loadGitHubConfig();
		loading = false;
	});

	async function loadProfile() {
		try {
			profile = await getMyProfile();
			nameInput = profile.name;
			descriptionInput = profile.description || '';
		} catch (e: any) {
			console.error('Failed to load profile:', e);
		}
	}

	async function loadCredentials() {
		loadingCredentials = true;
		try {
			const result = await getMyCredentials();
			credentials = result.credentials;
			hasMissingRequired = result.has_missing_required;
		} catch (e: any) {
			console.error('Failed to load credentials:', e);
		}
		loadingCredentials = false;
	}

	async function loadGitHubConfig() {
		loadingGitHub = true;
		try {
			githubConfig = await getMyGitHubConfig();
			if (githubConfig?.connected && githubConfig.default_repo) {
				selectedRepo = githubConfig.default_repo;
				selectedBranch = githubConfig.default_branch || '';
			}
		} catch (e: any) {
			console.error('Failed to load GitHub config:', e);
		}
		loadingGitHub = false;
	}

	async function handleSaveProfile() {
		if (!profile) return;
		savingProfile = true;
		profileError = '';
		profileSuccess = '';

		try {
			const updated = await updateMyProfile({
				name: nameInput,
				description: descriptionInput
			});
			profile = updated;
			editingName = false;
			editingDescription = false;
			profileSuccess = 'Profile updated successfully';
			setTimeout(() => profileSuccess = '', 3000);
		} catch (e: any) {
			profileError = e.detail || 'Failed to update profile';
		}
		savingProfile = false;
	}

	async function handleChangePassword() {
		if (newPassword !== confirmPassword) {
			passwordError = 'Passwords do not match';
			return;
		}
		if (newPassword.length < 8) {
			passwordError = 'Password must be at least 8 characters';
			return;
		}

		changingPassword = true;
		passwordError = '';
		passwordSuccess = '';

		try {
			await changeMyPassword(currentPassword, newPassword);
			passwordSuccess = 'Password changed successfully';
			currentPassword = '';
			newPassword = '';
			confirmPassword = '';
			showPasswordChange = false;
			setTimeout(() => passwordSuccess = '', 3000);
		} catch (e: any) {
			passwordError = e.detail || 'Failed to change password';
		}
		changingPassword = false;
	}

	async function handleSaveCredential(credentialType: string) {
		if (!credentialInput.trim()) {
			credentialError = 'Please enter a value';
			return;
		}

		savingCredential = true;
		credentialError = '';
		credentialSuccess = '';

		try {
			await setMyCredential(credentialType, credentialInput);
			await loadCredentials();
			editingCredential = null;
			credentialInput = '';
			credentialSuccess = 'Credential saved successfully';
			setTimeout(() => credentialSuccess = '', 3000);
		} catch (e: any) {
			credentialError = e.detail || 'Failed to save credential';
		}
		savingCredential = false;
	}

	async function handleDeleteCredential(credentialType: string) {
		if (!confirm('Are you sure you want to remove this credential?')) return;

		try {
			await deleteMyCredential(credentialType);
			await loadCredentials();
			credentialSuccess = 'Credential removed';
			setTimeout(() => credentialSuccess = '', 3000);
		} catch (e: any) {
			credentialError = e.detail || 'Failed to remove credential';
		}
	}

	async function handleConnectGitHub() {
		if (!gitHubPAT.trim()) {
			gitHubError = 'Please enter a Personal Access Token';
			return;
		}

		connectingGitHub = true;
		gitHubError = '';
		gitHubSuccess = '';

		try {
			await connectGitHub(gitHubPAT);
			await loadGitHubConfig();
			await loadCredentials();
			showGitHubConnect = false;
			gitHubPAT = '';
			gitHubSuccess = 'GitHub connected successfully';
			setTimeout(() => gitHubSuccess = '', 3000);
		} catch (e: any) {
			gitHubError = e.detail || 'Failed to connect GitHub';
		}
		connectingGitHub = false;
	}

	async function handleDisconnectGitHub() {
		if (!confirm('Are you sure you want to disconnect GitHub?')) return;

		try {
			await disconnectGitHub();
			await loadGitHubConfig();
			await loadCredentials();
			githubRepos = [];
			branches = [];
			selectedRepo = '';
			selectedBranch = '';
			gitHubSuccess = 'GitHub disconnected';
			setTimeout(() => gitHubSuccess = '', 3000);
		} catch (e: any) {
			gitHubError = e.detail || 'Failed to disconnect GitHub';
		}
	}

	async function handleLoadRepos() {
		if (!githubConfig?.connected) return;

		loadingRepos = true;
		try {
			const result = await listMyGitHubRepos();
			githubRepos = result.repos;
		} catch (e: any) {
			gitHubError = e.detail || 'Failed to load repositories';
		}
		loadingRepos = false;
	}

	async function handleRepoChange() {
		if (!selectedRepo) {
			branches = [];
			return;
		}

		loadingBranches = true;
		try {
			const [owner, repo] = selectedRepo.split('/');
			const result = await listGitHubBranches(owner, repo);
			branches = result.branches;
			selectedBranch = result.default_branch;
		} catch (e: any) {
			gitHubError = e.detail || 'Failed to load branches';
		}
		loadingBranches = false;
	}

	async function handleSaveGitHubDefaults() {
		savingGitHubDefaults = true;
		gitHubError = '';
		gitHubSuccess = '';

		try {
			await setGitHubDefaults(selectedRepo, selectedBranch);
			gitHubSuccess = 'Default repository and branch saved';
			setTimeout(() => gitHubSuccess = '', 3000);
		} catch (e: any) {
			gitHubError = e.detail || 'Failed to save defaults';
		}
		savingGitHubDefaults = false;
	}

	function getCredentialIcon(credentialType: string): string {
		switch (credentialType) {
			case 'openai_api_key':
				return 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z';
			case 'gemini_api_key':
				return 'M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z';
			case 'meshy_api_key':
				// 3D cube icon for Meshy
				return 'M21 16.5c0 .38-.21.71-.53.88l-7.9 4.44c-.36.2-.8.2-1.14 0l-7.9-4.44A.991.991 0 013 16.5v-9c0-.38.21-.71.53-.88l7.9-4.44c.36-.2.8-.2 1.14 0l7.9 4.44c.32.17.53.5.53.88v9zM12 4.15L5 8.09v7.82l7 3.94 7-3.94V8.09l-7-3.94z';
			case 'github_pat':
				return 'M12 0C5.374 0 0 5.373 0 12c0 5.302 3.438 9.8 8.207 11.387.6.11.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23A11.509 11.509 0 0112 5.803c1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576C20.566 21.797 24 17.3 24 12c0-6.627-5.373-12-12-12z';
			default:
				return 'M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z';
		}
	}

	function getPolicyBadge(policy: string): { text: string; class: string } {
		switch (policy) {
			case 'admin_provided':
				return { text: 'Admin Provides', class: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200' };
			case 'user_provided':
				return { text: 'Required', class: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200' };
			case 'optional':
				return { text: 'Optional', class: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200' };
			default:
				return { text: policy, class: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200' };
		}
	}
</script>

<BaseCard
	{card}
	{onClose}
	{onMaximize}
	{onFocus}
	{onMove}
	{onResize}
	{onDragEnd}
	{onResizeEnd}
	{mobile}
>
	<svelte:fragment slot="icon">
		<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
		</svg>
	</svelte:fragment>

	<div class="flex h-full">
		<!-- Sidebar Navigation -->
		<div class="w-48 border-r border-gray-200 dark:border-gray-700 p-2 flex-shrink-0">
			<div class="space-y-1">
				{#each sections as section}
					<button
						onclick={() => activeSection = section.id}
						class="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors
							{activeSection === section.id
								? 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900 dark:text-indigo-200'
								: 'text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800'}"
					>
						<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
							<path d={section.icon} />
						</svg>
						{section.label}
						{#if section.id === 'credentials' && hasMissingRequired}
							<span class="ml-auto w-2 h-2 bg-red-500 rounded-full"></span>
						{/if}
					</button>
				{/each}
			</div>
		</div>

		<!-- Content Area -->
		<div class="flex-1 overflow-y-auto p-4">
			{#if loading}
				<div class="flex items-center justify-center h-full">
					<div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-600"></div>
				</div>
			{:else if activeSection === 'profile'}
				<!-- Profile Section -->
				<div class="space-y-6">
					<h3 class="text-lg font-semibold text-gray-900 dark:text-gray-100">Profile</h3>

					{#if profileSuccess}
						<div class="p-3 bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200 rounded-lg text-sm">
							{profileSuccess}
						</div>
					{/if}
					{#if profileError}
						<div class="p-3 bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200 rounded-lg text-sm">
							{profileError}
						</div>
					{/if}

					{#if profile}
						<!-- Name -->
						<div class="space-y-2">
							<label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Name</label>
							{#if editingName}
								<div class="flex gap-2">
									<input
										type="text"
										bind:value={nameInput}
										class="flex-1 px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600"
									/>
									<button
										onclick={handleSaveProfile}
										disabled={savingProfile}
										class="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50"
									>
										{savingProfile ? 'Saving...' : 'Save'}
									</button>
									<button
										onclick={() => { editingName = false; nameInput = profile!.name; }}
										class="px-4 py-2 border rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
									>
										Cancel
									</button>
								</div>
							{:else}
								<div class="flex items-center gap-2">
									<span class="text-gray-900 dark:text-gray-100">{profile.name}</span>
									<button
										onclick={() => editingName = true}
										class="text-indigo-600 hover:text-indigo-800 text-sm"
									>
										Edit
									</button>
								</div>
							{/if}
						</div>

						<!-- Username -->
						<div class="space-y-2">
							<label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Username</label>
							<span class="text-gray-900 dark:text-gray-100">{profile.username || 'Not set'}</span>
						</div>

						<!-- Description -->
						<div class="space-y-2">
							<label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Description</label>
							{#if editingDescription}
								<div class="space-y-2">
									<textarea
										bind:value={descriptionInput}
										rows="3"
										class="w-full px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600"
									></textarea>
									<div class="flex gap-2">
										<button
											onclick={handleSaveProfile}
											disabled={savingProfile}
											class="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50"
										>
											{savingProfile ? 'Saving...' : 'Save'}
										</button>
										<button
											onclick={() => { editingDescription = false; descriptionInput = profile!.description || ''; }}
											class="px-4 py-2 border rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
										>
											Cancel
										</button>
									</div>
								</div>
							{:else}
								<div class="flex items-center gap-2">
									<span class="text-gray-900 dark:text-gray-100">{profile.description || 'No description'}</span>
									<button
										onclick={() => editingDescription = true}
										class="text-indigo-600 hover:text-indigo-800 text-sm"
									>
										Edit
									</button>
								</div>
							{/if}
						</div>

						<!-- Password Change -->
						<div class="pt-4 border-t border-gray-200 dark:border-gray-700">
							<h4 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">Security</h4>

							{#if passwordSuccess}
								<div class="p-3 bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200 rounded-lg text-sm mb-3">
									{passwordSuccess}
								</div>
							{/if}

							{#if showPasswordChange}
								<div class="space-y-3">
									{#if passwordError}
										<div class="p-3 bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200 rounded-lg text-sm">
											{passwordError}
										</div>
									{/if}
									<input
										type="password"
										bind:value={currentPassword}
										placeholder="Current password"
										class="w-full px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600"
									/>
									<input
										type="password"
										bind:value={newPassword}
										placeholder="New password"
										class="w-full px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600"
									/>
									<input
										type="password"
										bind:value={confirmPassword}
										placeholder="Confirm new password"
										class="w-full px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600"
									/>
									<div class="flex gap-2">
										<button
											onclick={handleChangePassword}
											disabled={changingPassword}
											class="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50"
										>
											{changingPassword ? 'Changing...' : 'Change Password'}
										</button>
										<button
											onclick={() => { showPasswordChange = false; currentPassword = ''; newPassword = ''; confirmPassword = ''; passwordError = ''; }}
											class="px-4 py-2 border rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
										>
											Cancel
										</button>
									</div>
								</div>
							{:else}
								<button
									onclick={() => showPasswordChange = true}
									class="px-4 py-2 border rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
								>
									Change Password
								</button>
							{/if}
						</div>
					{/if}
				</div>

			{:else if activeSection === 'credentials'}
				<!-- Credentials Section -->
				<div class="space-y-6">
					<div class="flex items-center justify-between">
						<h3 class="text-lg font-semibold text-gray-900 dark:text-gray-100">API Keys</h3>
						{#if hasMissingRequired}
							<span class="px-2 py-1 bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200 rounded text-xs">
								Missing required keys
							</span>
						{/if}
					</div>

					{#if credentialSuccess}
						<div class="p-3 bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200 rounded-lg text-sm">
							{credentialSuccess}
						</div>
					{/if}
					{#if credentialError}
						<div class="p-3 bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200 rounded-lg text-sm">
							{credentialError}
						</div>
					{/if}

					{#if loadingCredentials}
						<div class="flex justify-center py-8">
							<div class="animate-spin rounded-full h-6 w-6 border-b-2 border-indigo-600"></div>
						</div>
					{:else}
						<div class="space-y-4">
							{#each credentials as cred}
								<div class="p-4 border rounded-lg dark:border-gray-700">
									<div class="flex items-start gap-3">
										<svg class="w-5 h-5 text-gray-500 mt-0.5" fill="currentColor" viewBox="0 0 24 24">
											<path d={getCredentialIcon(cred.credential_type)} />
										</svg>
										<div class="flex-1">
											<div class="flex items-center gap-2 mb-1">
												<h4 class="font-medium text-gray-900 dark:text-gray-100">{cred.name}</h4>
												<span class="px-2 py-0.5 text-xs rounded-full {getPolicyBadge(cred.policy).class}">
													{getPolicyBadge(cred.policy).text}
												</span>
											</div>
											<p class="text-sm text-gray-500 dark:text-gray-400 mb-2">{cred.description}</p>

											{#if cred.policy === 'admin_provided'}
												<p class="text-sm text-blue-600 dark:text-blue-400">
													This key is provided by the administrator.
												</p>
											{:else if editingCredential === cred.credential_type}
												<div class="space-y-2">
													<input
														type="password"
														bind:value={credentialInput}
														placeholder="Enter {cred.name}"
														class="w-full px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600 text-sm"
													/>
													<div class="flex gap-2">
														<button
															onclick={() => handleSaveCredential(cred.credential_type)}
															disabled={savingCredential}
															class="px-3 py-1.5 bg-indigo-600 text-white rounded text-sm hover:bg-indigo-700 disabled:opacity-50"
														>
															{savingCredential ? 'Saving...' : 'Save'}
														</button>
														<button
															onclick={() => { editingCredential = null; credentialInput = ''; credentialError = ''; }}
															class="px-3 py-1.5 border rounded text-sm hover:bg-gray-100 dark:hover:bg-gray-700"
														>
															Cancel
														</button>
													</div>
												</div>
											{:else if cred.is_set}
												<div class="flex items-center gap-2">
													<span class="text-sm text-gray-600 dark:text-gray-400 font-mono">{cred.masked_value}</span>
													<span class="text-green-600 text-xs">✓ Set</span>
													<button
														onclick={() => { editingCredential = cred.credential_type; credentialInput = ''; }}
														class="text-indigo-600 hover:text-indigo-800 text-sm ml-2"
													>
														Update
													</button>
													<button
														onclick={() => handleDeleteCredential(cred.credential_type)}
														class="text-red-600 hover:text-red-800 text-sm"
													>
														Remove
													</button>
												</div>
											{:else}
												<div class="flex items-center gap-2">
													{#if cred.admin_available && cred.policy === 'optional'}
														<span class="text-sm text-gray-500">Using admin key (you can override)</span>
													{:else}
														<span class="text-sm text-yellow-600 dark:text-yellow-400">Not set</span>
													{/if}
													<button
														onclick={() => { editingCredential = cred.credential_type; credentialInput = ''; }}
														class="text-indigo-600 hover:text-indigo-800 text-sm"
													>
														{cred.is_set ? 'Update' : 'Set'}
													</button>
												</div>
											{/if}
										</div>
									</div>
								</div>
							{/each}
						</div>
					{/if}
				</div>

			{:else if activeSection === 'github'}
				<!-- GitHub Section -->
				<div class="space-y-6">
					<h3 class="text-lg font-semibold text-gray-900 dark:text-gray-100">GitHub Connection</h3>

					{#if gitHubSuccess}
						<div class="p-3 bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200 rounded-lg text-sm">
							{gitHubSuccess}
						</div>
					{/if}
					{#if gitHubError}
						<div class="p-3 bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200 rounded-lg text-sm">
							{gitHubError}
						</div>
					{/if}

					{#if loadingGitHub}
						<div class="flex justify-center py-8">
							<div class="animate-spin rounded-full h-6 w-6 border-b-2 border-indigo-600"></div>
						</div>
					{:else if githubConfig?.connected}
						<!-- Connected State -->
						<div class="p-4 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg">
							<div class="flex items-center gap-3">
								{#if githubConfig.github_avatar_url}
									<img src={githubConfig.github_avatar_url} alt="" class="w-10 h-10 rounded-full" />
								{/if}
								<div class="flex-1">
									<p class="font-medium text-green-800 dark:text-green-200">
										Connected as @{githubConfig.github_username}
									</p>
								</div>
								<button
									onclick={handleDisconnectGitHub}
									class="px-3 py-1.5 text-sm text-red-600 hover:text-red-800 border border-red-300 rounded hover:bg-red-50 dark:hover:bg-red-900/20"
								>
									Disconnect
								</button>
							</div>
						</div>

						<!-- Default Repository Selection -->
						<div class="space-y-4">
							<h4 class="text-sm font-medium text-gray-700 dark:text-gray-300">Default Repository</h4>

							{#if githubRepos.length === 0}
								<button
									onclick={handleLoadRepos}
									disabled={loadingRepos}
									class="px-4 py-2 border rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
								>
									{loadingRepos ? 'Loading...' : 'Load Repositories'}
								</button>
							{:else}
								<div class="space-y-3">
									<select
										bind:value={selectedRepo}
										onchange={handleRepoChange}
										class="w-full px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600"
									>
										<option value="">Select a repository</option>
										{#each githubRepos as repo}
											<option value={repo.full_name}>
												{repo.full_name} {repo.private ? '🔒' : ''}
											</option>
										{/each}
									</select>

									{#if selectedRepo && branches.length > 0}
										<select
											bind:value={selectedBranch}
											class="w-full px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600"
										>
											{#each branches as branch}
												<option value={branch.name}>
													{branch.name} {branch.is_default ? '(default)' : ''}
												</option>
											{/each}
										</select>
									{/if}

									{#if selectedRepo}
										<button
											onclick={handleSaveGitHubDefaults}
											disabled={savingGitHubDefaults}
											class="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50"
										>
											{savingGitHubDefaults ? 'Saving...' : 'Save as Default'}
										</button>
									{/if}
								</div>
							{/if}
						</div>
					{:else}
						<!-- Not Connected State -->
						<div class="p-4 bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg">
							<p class="text-gray-600 dark:text-gray-400 mb-4">
								Connect your GitHub account to access your repositories.
							</p>

							{#if showGitHubConnect}
								<div class="space-y-3">
									<div>
										<label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
											Personal Access Token
										</label>
										<input
											type="password"
											bind:value={gitHubPAT}
											placeholder="ghp_..."
											class="w-full px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600"
										/>
										<p class="text-xs text-gray-500 mt-1">
											Create a token at <a href="https://github.com/settings/tokens" target="_blank" class="text-indigo-600 hover:underline">github.com/settings/tokens</a> with 'repo' scope.
										</p>
									</div>
									<div class="flex gap-2">
										<button
											onclick={handleConnectGitHub}
											disabled={connectingGitHub}
											class="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50"
										>
											{connectingGitHub ? 'Connecting...' : 'Connect'}
										</button>
										<button
											onclick={() => { showGitHubConnect = false; gitHubPAT = ''; gitHubError = ''; }}
											class="px-4 py-2 border rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
										>
											Cancel
										</button>
									</div>
								</div>
							{:else}
								<button
									onclick={() => showGitHubConnect = true}
									class="px-4 py-2 bg-gray-900 text-white rounded-lg hover:bg-gray-800 flex items-center gap-2"
								>
									<svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
										<path d="M12 0C5.374 0 0 5.373 0 12c0 5.302 3.438 9.8 8.207 11.387.6.11.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23A11.509 11.509 0 0112 5.803c1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576C20.566 21.797 24 17.3 24 12c0-6.627-5.373-12-12-12z"/>
									</svg>
									Connect GitHub
								</button>
							{/if}
						</div>
					{/if}
				</div>
			{/if}
		</div>
	</div>
</BaseCard>
