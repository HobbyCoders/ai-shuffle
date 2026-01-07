<script lang="ts">
	import { connectionState, deviceId, connectedDevices } from '$lib/stores/chat';
	import { claudeAuthenticated } from '$lib/stores/auth';

	interface Props {
		wsConnected?: boolean; // Per-tab connection status (for tabs-based UI)
	}

	let { wsConnected }: Props = $props();

	// Determine connection status:
	// 1. If Claude is not authenticated, show 'unauthorized'
	// 2. If wsConnected prop is explicitly provided (tabs-based UI), use it
	// 3. Otherwise fall back to global connectionState from chat store
	let connectionStatus = $derived.by(() => {
		// Check Claude authentication first
		if (!$claudeAuthenticated) {
			return 'unauthorized';
		}
		// If wsConnected prop is provided, derive status from it
		if (wsConnected !== undefined) {
			return wsConnected ? 'connected' : 'disconnected';
		}
		// Fall back to global connection state
		return $connectionState || 'disconnected';
	});
	let devices = $derived($connectedDevices || 1);
	let shortDeviceId = $derived($deviceId ? $deviceId.substring(0, 8) : '');

	// Mobile: toggle on tap
	let showMobilePanel = $state(false);

	function toggleMobilePanel() {
		showMobilePanel = !showMobilePanel;
	}

	function closeMobilePanel() {
		showMobilePanel = false;
	}

	// Color and icon based on connection state
	const statusConfig = $derived.by(() => {
		switch (connectionStatus) {
			case 'connected':
				return {
					color: 'text-success',
					bgColor: 'bg-success',
					label: 'Connected',
					icon: 'dot',
					pulse: false
				};
			case 'connecting':
				return {
					color: 'text-warning',
					bgColor: 'bg-warning',
					label: 'Connecting...',
					icon: 'spinner',
					pulse: true
				};
			case 'reconnecting':
				return {
					color: 'text-warning',
					bgColor: 'bg-warning',
					label: 'Reconnecting...',
					icon: 'spinner',
					pulse: true
				};
			case 'disconnected':
				return {
					color: 'text-destructive',
					bgColor: 'bg-destructive',
					label: 'Disconnected',
					icon: 'dot',
					pulse: false
				};
			case 'unauthorized':
				return {
					color: 'text-warning',
					bgColor: 'bg-warning',
					label: 'Unauthorized',
					icon: 'lock',
					pulse: false
				};
			default:
				return {
					color: 'text-muted-foreground',
					bgColor: 'bg-muted-foreground',
					label: 'Unknown',
					icon: 'dot',
					pulse: false
				};
		}
	});
</script>

<!-- Connection Status Indicator -->
<div class="relative group">
	<!-- Desktop: hover to show, Mobile: tap to show -->
	<button
		onclick={toggleMobilePanel}
		class="floating-pill flex items-center gap-1.5 px-3 py-1.5 text-xs {statusConfig.color} hover:bg-hover-overlay transition-colors"
		title={statusConfig.label}
	>
		{#if statusConfig.icon === 'spinner'}
			<!-- Spinner for connecting/reconnecting -->
			<svg class="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24">
				<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
				<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
			</svg>
		{:else if statusConfig.icon === 'lock'}
			<!-- Lock icon for unauthorized -->
			<svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
				<path stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
			</svg>
		{:else}
			<!-- Dot for connected/disconnected -->
			<span class="w-2 h-2 {statusConfig.bgColor} rounded-full {statusConfig.pulse ? 'animate-pulse' : ''}"></span>
		{/if}
	</button>

	<!-- Desktop: Hover dropdown (hidden on mobile) - positioned absolute to right edge -->
	<div class="hidden sm:block absolute top-full right-0 mt-1 w-52 min-w-[13rem] bg-card border border-border rounded-lg shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50">
		<div class="py-2 px-3 space-y-2">
			<!-- Status -->
			<div class="flex items-center justify-between text-xs pb-1 border-b border-border">
				<span class="text-muted-foreground">Status</span>
				<span class="flex items-center gap-1.5 {statusConfig.color} font-medium">
					{#if statusConfig.icon === 'spinner'}
						<svg class="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
					{:else if statusConfig.icon === 'lock'}
						<svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
							<path stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
						</svg>
					{:else}
						<span class="w-2 h-2 {statusConfig.bgColor} rounded-full"></span>
					{/if}
					{statusConfig.label}
				</span>
			</div>

			<!-- Device ID -->
			<div class="flex items-center justify-between text-xs">
				<span class="text-muted-foreground">Device ID</span>
				<span class="text-foreground font-mono text-[10px]">{shortDeviceId}</span>
			</div>

			<!-- Connected Devices -->
			{#if devices > 1}
				<div class="flex items-center justify-between text-xs">
					<span class="text-muted-foreground">Devices</span>
					<span class="text-foreground font-medium">{devices} viewing</span>
				</div>
			{/if}

			<!-- Info text -->
			<div class="text-xs text-muted-foreground pt-1 border-t border-border">
				{#if connectionStatus === 'unauthorized'}
					Claude Code not connected. Go to Settings to authenticate.
				{:else if connectionStatus === 'connected'}
					Real-time sync enabled
				{:else if connectionStatus === 'reconnecting'}
					Attempting to reconnect...
				{:else if connectionStatus === 'connecting'}
					Establishing connection...
				{:else}
					Connection lost. Check your network.
				{/if}
			</div>
		</div>
	</div>
</div>

<!-- Mobile: Click-to-open panel (outside relative container for proper fixed positioning) -->
{#if showMobilePanel}
	<!-- Backdrop - tap anywhere to close -->
	<button
		class="sm:hidden fixed inset-0 z-[60] bg-black/20"
		onclick={closeMobilePanel}
		aria-label="Close details"
	></button>

	<!-- Panel - centered with margins -->
	<div class="sm:hidden fixed left-4 right-4 top-16 z-[70] bg-card border border-border rounded-lg shadow-lg">
		<div class="p-4 space-y-3">
			<!-- Status -->
			<div class="flex items-center justify-between text-sm pb-2 border-b border-border">
				<span class="text-muted-foreground">Status</span>
				<span class="flex items-center gap-2 {statusConfig.color} font-medium">
					{#if statusConfig.icon === 'spinner'}
						<svg class="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
							<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
							<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
						</svg>
					{:else if statusConfig.icon === 'lock'}
						<svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
							<path stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
						</svg>
					{:else}
						<span class="w-2.5 h-2.5 {statusConfig.bgColor} rounded-full"></span>
					{/if}
					{statusConfig.label}
				</span>
			</div>

			<!-- Device ID -->
			<div class="flex items-center justify-between text-sm">
				<span class="text-muted-foreground">Device ID</span>
				<span class="text-foreground font-mono text-xs">{shortDeviceId}</span>
			</div>

			<!-- Connected Devices -->
			{#if devices > 1}
				<div class="flex items-center justify-between text-sm">
					<span class="text-muted-foreground">Devices</span>
					<span class="text-foreground font-medium">{devices} viewing</span>
				</div>
			{/if}

			<!-- Info text -->
			<div class="text-sm text-muted-foreground pt-2 border-t border-border">
				{#if connectionStatus === 'unauthorized'}
					Claude Code not connected. Go to Settings to authenticate.
				{:else if connectionStatus === 'connected'}
					Real-time sync enabled
				{:else if connectionStatus === 'reconnecting'}
					Attempting to reconnect...
				{:else if connectionStatus === 'connecting'}
					Establishing connection...
				{:else}
					Connection lost. Check your network.
				{/if}
			</div>
		</div>
	</div>
{/if}
