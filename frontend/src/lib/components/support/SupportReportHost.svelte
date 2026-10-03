<script lang="ts">
	/**
	 * Mounts the support report dialog once for the whole app (RootLayout, both
	 * shells) and loads the support capabilities so error actions know whether
	 * to offer "Fix with AI". Open it with `openSupportReport(...)`.
	 */
	import { onMount } from 'svelte';
	import SupportReportDialog from './SupportReportDialog.svelte';
	import {
		SupportReportFlow,
		supportCaps,
		supportReportDialog,
		type ReportRequest
	} from '$lib/stores/support-report.svelte';

	onMount(() => {
		void supportCaps.load();
	});

	const request = $derived(supportReportDialog.request);

	function makeFlow(req: ReportRequest): SupportReportFlow {
		const flow = new SupportReportFlow(req.source, {
			defaultModel: () => supportCaps.current?.default_model ?? null
		});
		void flow.start();
		return flow;
	}
</script>

{#if request}
	{#key request.key}
		<SupportReportDialog flow={makeFlow(request)} onClose={() => supportReportDialog.close()} />
	{/key}
{/if}
