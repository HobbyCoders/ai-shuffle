<script lang="ts">
	import { connectionState, deviceId, connectedDevices } from '$lib/stores/chat';

	interface Props {
		wsConnected?: boolean; // Per-tab connection status (for tabs-based UI)
	}

	let { wsConnected }: Props = $props();

	// Determine connection status:
	// 1. If wsConnected prop is explicitly provided (tabs-based UI), use it
	// 2. Otherwise fall back to global connectionState from chat store
	let connectionStatus = $derived.by(() => {
		// If wsConnected prop is provided, derive status from it
		if (wsConnected !== undefined) {
			return wsConnected ? 'connected' : 'disconnected';
		}
		// Fall back to global connection state
		return $connectionState || 'disconnected';
	});
	let devices = $derived($connectedDevices || 1);
	let shortDeviceId = $derived($deviceId ? $deviceId.substring(0, 8) : '');

	// Show more details on hover/click
	let showDetails = $state(false);

	function toggleDetails() {
		showDetails = !showDetails;
	}

	function closeDetails() {
		showDetails = false;
	}

	// Color and icon based on connection state
	const statusConfig = $derived.by(() => {
		switch (connectionStatus) {
			case 'connected':
				return {
					color: 'text-green-500',
					bgColor: 'bg-green-500',
					label: 'Connected',
					icon: 'dot',
					pulse: false
				};
			case 'connecting':
				return {
					color: 'text-yellow-500',
					bgColor: 'bg-yellow-500',
					label: 'Connecting...',
					icon: 'spinner',
					pulse: true
				};
			case 'reconnecting':
				return {
					color: 'text-orange-500',
					bgColor: 'bg-orange-500',
					label: 'Reconnecting...',
					icon: 'spinner',
					pulse: true
				};
			case 'disconnected':
				return {
					color: 'text-red-500',
					bgColor: 'bg-red-500',
					label: 'Disconnected',
					icon: 'dot',
					pulse: false
				};
			default:
				return {
					color: 'text-gray-500',
					bgColor: 'bg-gray-500',
					label: 'Unknown',
					icon: 'dot',
					pulse: false
				};
		}
	});
</script>

<!-- Connection Status Indicator -->
<div class="relative">
	<button
		onclick={toggleDetails}
		class="flex items-center justify-center p-1.5 text-xs {statusConfig.color} hover:bg-accent rounded-md transition-colors"
		title={statusConfig.label}
	>
		{#if statusConfig.icon === 'spinner'}
			<!-- Spinner for connecting/reconnecting -->
			<svg class="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24">
				<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
				<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
			</svg>
		{:else}
			<!-- Dot for connected/disconnected -->
			<span class="w-2 h-2 {statusConfig.bgColor} rounded-full {statusConfig.pulse ? 'animate-pulse' : ''}"></span>
		{/if}
	</button>

	<!-- Details Popover -->
	{#if showDetails}
		<div class="absolute right-0 top-full mt-1 w-56 bg-card border border-border rounded-lg shadow-lg z-50">
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
					{#if connectionStatus === 'connected'}
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

			<!-- Close button overlay -->
			<button
				class="absolute top-2 right-2 text-muted-foreground hover:text-foreground"
				onclick={closeDetails}
				title="Close"
			>
				<svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		</div>
	{/if}
</div>

<!-- Click outside to close -->
{#if showDetails}
	<button
		class="fixed inset-0 z-40"
		onclick={closeDetails}
		aria-label="Close details"
	></button>
{/if}
