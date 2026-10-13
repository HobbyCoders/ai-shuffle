<script lang="ts">
	/**
	 * Send report to HobbyCoders support.
	 *
	 * Shows the exact text that will be sent (and how many secrets were hidden)
	 * before anything leaves the machine; only the Send button sends. The
	 * known-issue check runs on open (it sends only the error's fingerprint).
	 * The optional AI diagnosis says up front that it uses the user's own model
	 * and usage, names the model (the shared ModelPicker), and starts only after
	 * an explicit acknowledgement.
	 *
	 * An AI content report (offensive or harmful AI output) shows the excerpt
	 * and the reason instead, with no known-issue check and no diagnosis. On a
	 * worker with no Shuffle account, Send saves the report on the worker and
	 * this dialog then offers it as copyable text and an email to support.
	 *
	 * State and API calls live in SupportReportFlow
	 * ($lib/stores/support-report.svelte); this file only renders it.
	 */
	import { onDestroy } from 'svelte';
	import { Sparkles, Check, ShieldCheck, LoaderCircle, RotateCw, Copy, Mail, HardDrive } from 'lucide-svelte';
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	import UniversalModal from '$lib/components/UniversalModal.svelte';
	import Banner from '$lib/components/Banner.svelte';
	import ModelPicker from '$lib/components/settings/ModelPicker.svelte';
	import { openSettings } from '$lib/settings/navigation.svelte';
	import {
		hubNotice,
		knownIssueNotice,
		wasDelivered,
		supportCaps,
		supportMailSubject,
		supportMailto,
		type Notice,
		type SupportReportFlow
	} from '$lib/stores/support-report.svelte';

	interface Props {
		flow: SupportReportFlow;
		onClose: () => void;
	}

	let { flow, onClose }: Props = $props();

	const FLAG_ICON =
		'M3 3v1.5M3 21v-6m0 0 2.77-.693a9 9 0 0 1 6.208.682l.108.054a9 9 0 0 0 6.086.71l3.114-.732a48.524 48.524 0 0 1-.005-10.499l-3.11.732a9 9 0 0 1-6.085-.711l-.108-.054a9 9 0 0 0-6.208-.682L3 4.5M3 15V4.5';

	const preview = $derived(flow.preview);
	const issueNotice = $derived(knownIssueNotice(flow.knownIssue));
	const checkNotice = $derived(flow.checkResult ? hubNotice(flow.checkResult) : null);
	const sendNotice = $derived(flow.sendOutcome ? hubNotice(flow.sendOutcome) : null);
	const sentIssue = $derived(knownIssueNotice(flow.sent?.known_issue));
	const delivered = $derived(wasDelivered(flow.sent?.delivered));
	// With no default text model the server picks one; say so instead of "Select a model".
	const noDefaultModel = $derived(!supportCaps.current?.default_model);

	// Elapsed time while the diagnosis runs.
	let now = $state(Date.now());
	let tick: ReturnType<typeof setInterval> | null = null;
	$effect(() => {
		if (flow.aiStep !== 'running') return;
		now = Date.now();
		tick = setInterval(() => (now = Date.now()), 1000);
		return () => {
			if (tick) clearInterval(tick);
			tick = null;
		};
	});
	const elapsed = $derived.by(() => {
		const secs = flow.aiStartedAt ? Math.max(0, Math.floor((now - flow.aiStartedAt) / 1000)) : 0;
		return `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;
	});

	onDestroy(() => {
		flow.dispose();
		if (copiedTimer) clearTimeout(copiedTimer);
	});

	// Local-only worker: the saved report as text and an email.
	const supportEmail = $derived(flow.savedLocally?.support_email || supportCaps.current?.support_email || 'support@hobbycoders.com');
	const mailto = $derived(preview ? supportMailto(flow.reportKind, preview.body, supportEmail) : '');
	let copied = $state(false);
	let copyError = $state<string | null>(null);
	let copiedTimer: ReturnType<typeof setTimeout> | null = null;

	async function copyReport() {
		if (!preview) return;
		copyError = null;
		try {
			await copyText(preview.body);
			copied = true;
			if (copiedTimer) clearTimeout(copiedTimer);
			copiedTimer = setTimeout(() => (copied = false), 2000);
		} catch {
			copyError = COPY_ERROR_MESSAGE;
		}
	}

	const title = $derived(
		flow.phase === 'sent'
			? 'Report sent'
			: flow.phase === 'saved_locally'
				? 'Report saved'
				: flow.isAiContent
					? 'Report AI content'
					: 'Send report to HobbyCoders support'
	);
	const finished = $derived(flow.phase === 'sent' || flow.phase === 'saved_locally');

	function close() {
		if (flow.phase === 'sending') return;
		onClose();
	}

	/** Signed out: after signing in, "Check again" clears the block. */
	function secondary(notice: Notice) {
		return notice.action === 'account' ? { label: 'Check again', onClick: () => void flow.recheck() } : undefined;
	}

	function act(notice: Notice) {
		if (notice.action === 'account') {
			return { label: 'Open Shuffle account', onClick: () => go({ category: 'devices', item: 'account' }) };
		}
		if (notice.action === 'update') {
			return { label: 'Open updates', onClick: () => go({ category: 'system', anchor: 'system.checkUpdates' }) };
		}
		if (notice.action === 'retry') {
			return { label: 'Check again', onClick: () => void flow.recheck() };
		}
		return undefined;
	}

	function go(target: Parameters<typeof openSettings>[0]) {
		onClose();
		openSettings(target);
	}

	const sendLabel = $derived(
		flow.phase === 'sending'
			? 'Sending…'
			: flow.signedOut
				? 'Save report'
				: flow.kind === 'ai' && flow.aiReport
					? 'Send with diagnosis'
					: 'Send report'
	);
</script>

<UniversalModal
	open
	{title}
	icon={FLAG_ICON}
	size="lg"
	onClose={close}
	onSave={finished || flow.phase === 'build_failed' ? undefined : () => void flow.send()}
	saveLabel={sendLabel}
	saveDisabled={!flow.canSend}
	saving={flow.phase === 'sending'}
	cancelLabel={finished ? 'Done' : 'Cancel'}
>
	<div class="sr" data-phase={flow.phase} data-report-kind={flow.reportKind}>
		{#if flow.phase === 'building'}
			<div class="sr-loading" role="status" aria-live="polite">
				<LoaderCircle size={16} class="sr-spin" aria-hidden="true" />
				<span>Building the report…</span>
			</div>
			<div class="sr-skeleton" aria-hidden="true">
				<span></span><span></span><span></span><span></span>
			</div>
		{:else if flow.phase === 'build_failed'}
			<Banner
				severity="error"
				title="Couldn't build the report"
				message={flow.buildError ?? ''}
				action={{ label: 'Try again', onClick: () => void flow.start() }}
			/>
		{:else if flow.phase === 'sent'}
			<section class="sr-sent" aria-live="polite">
				<span class="sr-sent-icon" aria-hidden="true"><Check size={22} strokeWidth={2.5} /></span>
				{#if flow.alreadySent}
					<h3>Already sent</h3>
					<p>This report was sent before. Nothing new was sent.</p>
				{:else if delivered}
					<h3>Sent to HobbyCoders support</h3>
					<p>They have the report and will look at it.</p>
				{:else}
					<h3>Saved by HobbyCoders</h3>
					<p>The report is stored, but it hasn't reached the support inbox yet. No need to send it again.</p>
				{/if}
				{#if flow.sent?.hub_report_id}
					<p class="sr-ref">Reference <code>{flow.sent.hub_report_id.slice(0, 12)}</code></p>
				{/if}
			</section>
			{#if sentIssue}
				<Banner severity={sentIssue.tone} title={sentIssue.title} message={sentIssue.text} action={act(sentIssue)} />
			{/if}
		{:else if flow.phase === 'saved_locally' && preview}
			<section class="sr-local" aria-labelledby="sr-local-title" data-testid="saved-locally">
				<header class="sr-local-head">
					<span class="sr-local-icon" aria-hidden="true"><HardDrive size={18} /></span>
					<div>
						<h3 id="sr-local-title">Saved on this worker</h3>
						<p>
							This worker isn't signed in to a Shuffle account, so nothing was sent. The report is in
							<code>{flow.savedLocally?.local_record?.file}</code> in the app's data folder. To send it, email it to
							<strong>{supportEmail}</strong>.
						</p>
					</div>
				</header>
				<div class="sr-local-actions">
					<a class="sr-btn sr-btn-primary" href={mailto} data-testid="mailto">
						<Mail size={14} aria-hidden="true" /> Email support
					</a>
					<button type="button" class="sr-btn" onclick={() => void copyReport()}>
						{#if copied}<Check size={14} aria-hidden="true" /> Copied{:else}<Copy size={14} aria-hidden="true" /> Copy report{/if}
					</button>
					<span class="sr-muted sr-subject">Subject: {supportMailSubject(flow.reportKind)}</span>
				</div>
				<span class="sr-only" aria-live="polite">{copied ? 'Report copied' : ''}</span>
				{#if copyError}<p class="sr-error" role="alert">{copyError}</p>{/if}
				<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
				<pre class="sr-body" tabindex="0" aria-label="Report text">{preview.body}</pre>
				<p class="sr-muted sr-foot">
					Long reports may not fit in the email link: copy the report and paste it into the email.
				</p>
			</section>
		{:else if preview}
			<!-- Status notices -->
			<div class="sr-notices" aria-live="polite">
				{#if flow.notice}
					<Banner severity="info" message={flow.notice} />
				{/if}
				{#if flow.checking}
					<p class="sr-checking" role="status">
						<LoaderCircle size={14} class="sr-spin" aria-hidden="true" />
						Checking for known issues…
					</p>
				{:else if issueNotice}
					<div data-testid="known-issue">
						<Banner severity={issueNotice.tone} title={issueNotice.title} message={issueNotice.text} action={act(issueNotice)} />
					</div>
				{:else if checkNotice}
					<Banner
						severity={checkNotice.tone}
						title={checkNotice.title}
						message={checkNotice.text}
						action={act(checkNotice)}
						secondaryAction={secondary(checkNotice)}
					/>
				{:else if flow.checkError}
					<Banner
						severity="warning"
						title="Couldn't check known issues"
						message={flow.checkError}
						action={{ label: 'Check again', onClick: () => void flow.recheck() }}
					/>
				{/if}
				{#if sendNotice}
					<!-- Retrying a send is pressing Send again; only sign-in gets a button. -->
					<Banner
						severity={sendNotice.tone}
						title={sendNotice.title}
						message={sendNotice.text}
						action={sendNotice.action === 'account' ? act(sendNotice) : undefined}
						secondaryAction={secondary(sendNotice)}
					/>
				{:else if flow.sendError}
					<Banner severity="error" title="Not sent" message={flow.sendError} />
				{/if}
			</div>

			{#if flow.isAiContent}
				<p class="sr-policy">
					HobbyCoders support reviews reported AI content by hand against its content policy. The report holds only
					the text below: the reported output, your reason, and a reference to the session.
				</p>
			{/if}

			<!-- Note -->
			<label class="sr-field">
				{#if flow.isAiContent}
					<span class="sr-label">Why are you reporting it?</span>
				{:else}
					<span class="sr-label">Your note <span class="sr-muted">(optional)</span></span>
				{/if}
				<textarea
					rows="2"
					maxlength="2000"
					placeholder={flow.isAiContent ? 'For example: it is hateful, sexual, violent or dangerous' : 'What were you doing?'}
					aria-required={flow.isAiContent}
					aria-invalid={flow.reasonMissing}
					aria-describedby={flow.reasonMissing ? 'sr-reason-err' : undefined}
					value={flow.note}
					oninput={(e) => flow.setNote(e.currentTarget.value)}
					onblur={() => void flow.saveNote()}
					disabled={flow.phase === 'sending'}
				></textarea>
				{#if flow.reasonMissing}
					<span class="sr-error" id="sr-reason-err" role="alert">Say why you are reporting it.</span>
				{/if}
			</label>

			<!-- AI diagnosis (not for AI content reports) -->
			{#if !flow.isAiContent}
			<section class="sr-ai" data-step={flow.aiStep} aria-labelledby="sr-ai-title">
				<header class="sr-ai-head">
					<span class="sr-ai-glyph" aria-hidden="true"><Sparkles size={15} /></span>
					<div class="sr-ai-titles">
						<h3 id="sr-ai-title">AI diagnosis</h3>
						{#if flow.aiStep === 'off'}
							<p class="sr-muted">Optional. Uses your AI usage.</p>
						{/if}
					</div>
					{#if flow.aiStep === 'off'}
						<button type="button" class="sr-btn" onclick={() => flow.openAi()} disabled={flow.phase === 'sending'}>
							Add AI diagnosis
						</button>
					{/if}
				</header>

				{#if flow.aiStep === 'confirm' || flow.aiStep === 'starting'}
					<div class="sr-ai-body">
						<p class="sr-ai-cost">
							This runs on <strong>your own model</strong> and counts against your AI usage or subscription.
						</p>
						<div class="sr-model-row">
							<label class="sr-label" for="sr-model">Model</label>
							<div class="sr-model">
								<ModelPicker
									id="sr-model"
									value={flow.aiModel}
									allowCustom={false}
									allowEmpty={noDefaultModel}
									emptyLabel="Default text model"
									onSelect={(v) => (flow.aiModel = v)}
								/>
							</div>
						</div>
						<label class="sr-ack">
							<input type="checkbox" bind:checked={flow.aiAcknowledged} disabled={flow.aiStep === 'starting'} />
							<span>I understand this uses my AI usage</span>
						</label>
						{#if flow.aiError}
							<p class="sr-error" role="alert">{flow.aiError}</p>
						{/if}
						<div class="sr-ai-actions">
							<button
								type="button"
								class="sr-btn sr-btn-primary"
								disabled={!flow.aiAcknowledged || flow.aiStep === 'starting'}
								onclick={() => void flow.startAi()}
							>
								{#if flow.aiStep === 'starting'}<LoaderCircle size={14} class="sr-spin" aria-hidden="true" />{/if}
								Start diagnosis
							</button>
							<button type="button" class="sr-btn sr-btn-ghost" onclick={() => flow.cancelAi()} disabled={flow.aiStep === 'starting'}>
								Not now
							</button>
						</div>
					</div>
				{:else if flow.aiStep === 'running'}
					<div class="sr-ai-body" role="status" aria-live="polite">
						<div class="sr-progress" aria-hidden="true"><span></span></div>
						<p class="sr-running">
							<LoaderCircle size={14} class="sr-spin" aria-hidden="true" />
							Diagnosing with {flow.aiRun?.model ?? 'your model'}
							<span class="sr-muted sr-tabular">{elapsed}</span>
						</p>
						<p class="sr-muted">Takes up to 3 minutes.</p>
					</div>
				{:else if flow.aiStep === 'done' && flow.aiReport?.diagnosis}
					{@const d = flow.aiReport.diagnosis}
					<div class="sr-ai-body">
						<p class="sr-muted sr-by">By {d.model}</p>
						<div class="sr-diagnosis">{d.text}</div>
						{#if d.workaround}
							<h4>What you can try</h4>
							<div class="sr-diagnosis sr-workaround">{d.workaround}</div>
						{/if}
						<div class="sr-choice" role="radiogroup" aria-label="Report to send">
							<button
								type="button"
								role="radio"
								aria-checked={flow.kind === 'ai'}
								class:active={flow.kind === 'ai'}
								onclick={() => void flow.useKind('ai')}
								disabled={flow.phase === 'sending'}
							>With diagnosis</button>
							<button
								type="button"
								role="radio"
								aria-checked={flow.kind === 'basic'}
								class:active={flow.kind === 'basic'}
								onclick={() => void flow.useKind('basic')}
								disabled={flow.phase === 'sending'}
							>Basic only</button>
						</div>
					</div>
				{:else if flow.aiStep === 'failed'}
					<div class="sr-ai-body">
						<Banner
							severity="error"
							title="Diagnosis failed"
							message={`${flow.aiError ?? 'The diagnosis did not finish.'} You can still send the basic report.`}
							action={{ label: 'Try again', onClick: () => flow.openAi() }}
						/>
					</div>
				{/if}
			</section>
			{/if}

			<!-- Exactly what will be sent -->
			<section class="sr-preview" aria-labelledby="sr-preview-title">
				<header class="sr-preview-head">
					<h3 id="sr-preview-title">{flow.signedOut ? 'What will be saved' : 'What will be sent'}</h3>
					<span class="sr-redactions" class:has={preview.redactions > 0}>
						<ShieldCheck size={13} aria-hidden="true" />
						{#if preview.redactions > 0}
							{preview.redactions} secret{preview.redactions === 1 ? '' : 's'} hidden as ***
						{:else}
							No secrets found
						{/if}
					</span>
				</header>
				<!-- A scrollable region must take focus so keyboard users can scroll it. -->
				<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
				<pre class="sr-body" tabindex="0" aria-label="Report text">{preview.body}</pre>
				<p class="sr-muted sr-foot">
					{#if flow.noteSaving}
						<RotateCw size={11} class="sr-spin" aria-hidden="true" /> Updating preview…
					{:else}
						Read it first. Only this text is sent.
					{/if}
				</p>
			</section>
		{/if}
	</div>
</UniversalModal>

<style>
	.sr {
		display: flex;
		flex-direction: column;
		gap: 16px;
		min-width: 0;
		font-size: 0.875rem;
		color: var(--foreground);
	}

	.sr-muted {
		color: var(--muted-foreground);
		font-weight: 400;
	}

	.sr-tabular {
		font-variant-numeric: tabular-nums;
	}

	.sr :global(.sr-spin) {
		animation: sr-spin 0.9s linear infinite;
		flex-shrink: 0;
	}

	@keyframes sr-spin {
		to {
			transform: rotate(360deg);
		}
	}

	/* ---- loading ---- */
	.sr-loading,
	.sr-checking,
	.sr-running {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0;
		color: var(--muted-foreground);
	}
	.sr-running {
		color: var(--foreground);
		flex-wrap: wrap;
	}

	.sr-skeleton {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.sr-skeleton span {
		height: 12px;
		border-radius: 6px;
		background: linear-gradient(90deg, var(--muted) 0%, var(--hover-overlay) 50%, var(--muted) 100%);
		background-size: 200% 100%;
		animation: sr-shimmer 1.4s ease-in-out infinite;
	}
	.sr-skeleton span:nth-child(2) { width: 82%; }
	.sr-skeleton span:nth-child(3) { width: 64%; }
	.sr-skeleton span:nth-child(4) { width: 90%; height: 96px; }

	@keyframes sr-shimmer {
		from { background-position: 100% 0; }
		to { background-position: -100% 0; }
	}

	.sr-notices {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.sr-notices:empty {
		display: none;
	}

	/* ---- note ---- */
	.sr-field {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.sr-label {
		font-size: 0.8125rem;
		font-weight: 600;
	}
	textarea {
		width: 100%;
		min-height: 3.75rem;
		resize: vertical;
		padding: 0.625rem 0.75rem;
		border: 1px solid var(--input-border, var(--border));
		border-radius: 10px;
		background: var(--input-bg, var(--background));
		color: var(--foreground);
		font: inherit;
		font-size: 0.875rem;
		line-height: 1.45;
		transition: border-color 150ms ease, background-color 150ms ease;
	}
	textarea:hover {
		border-color: var(--input-border-hover, var(--border-strong));
	}
	textarea:focus-visible {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 22%, transparent);
	}
	textarea[aria-invalid='true'] {
		border-color: var(--destructive);
	}
	textarea:disabled {
		opacity: 0.6;
	}

	/* ---- AI diagnosis ---- */
	.sr-ai {
		border: 1px solid var(--border);
		border-radius: 12px;
		background: color-mix(in oklch, var(--muted) 40%, transparent);
		overflow: visible;
	}
	.sr-ai[data-step='running'] {
		border-color: color-mix(in srgb, var(--primary) 45%, var(--border));
	}
	.sr-ai-head {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 12px 14px;
	}
	.sr-ai-glyph {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		flex-shrink: 0;
		border-radius: 8px;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 14%, transparent);
	}
	.sr-ai-titles {
		flex: 1;
		min-width: 0;
	}
	.sr-ai-titles h3,
	.sr-preview-head h3 {
		margin: 0;
		font-size: 0.875rem;
		font-weight: 600;
	}
	.sr-ai-titles p {
		margin: 2px 0 0;
		font-size: 0.75rem;
	}
	.sr-ai-body {
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding: 0 14px 14px;
		min-width: 0;
	}
	.sr-ai-cost {
		margin: 0;
		line-height: 1.5;
	}
	.sr-model-row {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		align-items: center;
		gap: 10px;
	}
	.sr-model {
		min-width: 0;
	}
	/* The picker panel is right-anchored and up to 360px wide; keep it inside
	   the dialog on narrow screens. */
	.sr-model :global(.mpk) {
		min-width: 0;
	}
	.sr-model :global(.mpk-panel) {
		--mpp-width: min(360px, calc(100vw - 72px));
	}
	.sr-ack {
		display: flex;
		align-items: center;
		gap: 10px;
		min-height: 32px;
		cursor: pointer;
		user-select: none;
	}
	.sr-ack input {
		width: 1rem;
		height: 1rem;
		flex-shrink: 0;
		accent-color: var(--primary);
		cursor: pointer;
	}
	.sr-ack input:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
	.sr-ai-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}
	.sr-error {
		margin: 0;
		color: var(--destructive);
		font-size: 0.8125rem;
	}

	.sr-progress {
		height: 3px;
		border-radius: 3px;
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		overflow: hidden;
	}
	.sr-progress span {
		display: block;
		width: 35%;
		height: 100%;
		border-radius: inherit;
		background: var(--primary);
		animation: sr-indeterminate 1.6s cubic-bezier(0.65, 0, 0.35, 1) infinite;
	}
	@keyframes sr-indeterminate {
		from { transform: translateX(-100%); }
		to { transform: translateX(290%); }
	}

	.sr-by {
		margin: 0;
		font-size: 0.75rem;
	}
	.sr-diagnosis {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		line-height: 1.55;
		max-height: 14rem;
		overflow-y: auto;
	}
	.sr-ai-body h4 {
		margin: 0;
		font-size: 0.8125rem;
		font-weight: 600;
	}
	.sr-workaround {
		padding: 10px 12px;
		border-radius: 10px;
		background: var(--status-success-bg, color-mix(in oklch, var(--success) 10%, transparent));
		border: 1px solid var(--status-success-border, color-mix(in oklch, var(--success) 30%, transparent));
	}

	.sr-choice {
		display: inline-flex;
		align-self: flex-start;
		padding: 3px;
		gap: 2px;
		border-radius: 10px;
		background: var(--muted);
		border: 1px solid var(--border);
	}
	.sr-choice button {
		min-height: 32px;
		padding: 4px 12px;
		border: 0;
		border-radius: 7px;
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		transition: background-color 150ms ease, color 150ms ease;
	}
	.sr-choice button:hover:not(:disabled) {
		color: var(--foreground);
	}
	.sr-choice button.active {
		background: var(--card);
		color: var(--foreground);
		box-shadow: var(--shadow-s, 0 1px 2px rgba(0, 0, 0, 0.15));
	}
	.sr-choice button:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	/* ---- buttons ---- */
	.sr-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		min-height: 36px;
		padding: 0 14px;
		flex-shrink: 0;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--card);
		color: var(--foreground);
		font: inherit;
		font-size: 0.8125rem;
		font-weight: 600;
		cursor: pointer;
		transition: background-color 150ms ease, border-color 150ms ease, opacity 150ms ease;
	}
	.sr-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		border-color: color-mix(in oklch, var(--accent) 50%, var(--border));
	}
	.sr-btn:active:not(:disabled) {
		background: var(--active-overlay, var(--hover-overlay));
	}
	.sr-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
	.sr-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.sr-btn-primary {
		border-color: transparent;
		background: var(--primary);
		color: var(--primary-foreground);
	}
	.sr-btn-primary:hover:not(:disabled) {
		background: var(--primary);
		border-color: transparent;
		opacity: 0.9;
	}
	.sr-btn-ghost {
		background: transparent;
	}

	/* ---- preview ---- */
	.sr-preview {
		display: flex;
		flex-direction: column;
		gap: 8px;
		min-width: 0;
	}
	.sr-preview-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		flex-wrap: wrap;
		gap: 6px 12px;
	}
	.sr-redactions {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 2px 8px;
		border-radius: 999px;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		background: var(--muted);
	}
	.sr-redactions.has {
		color: var(--foreground);
		background: var(--status-warning-bg, color-mix(in oklch, var(--warning) 14%, transparent));
		box-shadow: inset 0 0 0 1px var(--status-warning-border, color-mix(in oklch, var(--warning) 35%, transparent));
	}
	.sr-body {
		margin: 0;
		max-height: 18rem;
		overflow: auto;
		padding: 12px 14px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--code-bg, var(--muted));
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: 0.75rem;
		line-height: 1.6;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		tab-size: 2;
		/* Show the text exactly: no "***" or "==" ligatures. */
		font-variant-ligatures: none;
		font-feature-settings: 'liga' 0, 'calt' 0;
	}
	.sr-body:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
	.sr-foot {
		display: flex;
		align-items: center;
		gap: 6px;
		margin: 0;
		font-size: 0.75rem;
	}

	.sr-policy {
		margin: 0;
		padding: 10px 12px;
		border-left: 3px solid var(--primary);
		border-radius: 4px 10px 10px 4px;
		background: color-mix(in oklch, var(--primary) 8%, transparent);
		line-height: 1.5;
	}

	/* ---- saved on this worker ---- */
	.sr-local {
		display: flex;
		flex-direction: column;
		gap: 12px;
		min-width: 0;
		animation: sr-rise 260ms cubic-bezier(0.2, 0.8, 0.2, 1);
	}
	@keyframes sr-rise {
		from { transform: translateY(6px); opacity: 0; }
		to { transform: none; opacity: 1; }
	}
	.sr-local-head {
		display: flex;
		gap: 12px;
		align-items: flex-start;
	}
	.sr-local-head h3 {
		margin: 0 0 4px;
		font-size: 1rem;
		font-weight: 600;
	}
	.sr-local-head p {
		margin: 0;
		color: var(--muted-foreground);
		line-height: 1.5;
		overflow-wrap: anywhere;
	}
	.sr-local-head code {
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: 0.8125rem;
		color: var(--foreground);
	}
	.sr-local-head strong {
		color: var(--foreground);
	}
	.sr-local-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		flex-shrink: 0;
		border-radius: 10px;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 14%, transparent);
	}
	.sr-local-actions {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
	}
	a.sr-btn {
		text-decoration: none;
	}
	.sr-subject {
		font-size: 0.75rem;
	}

	/* ---- sent ---- */
	.sr-sent {
		display: flex;
		flex-direction: column;
		align-items: center;
		text-align: center;
		gap: 6px;
		padding: 12px 8px 4px;
	}
	.sr-sent-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 48px;
		height: 48px;
		margin-bottom: 6px;
		border-radius: 999px;
		color: var(--success);
		background: var(--status-success-bg, color-mix(in srgb, var(--success) 16%, transparent));
		box-shadow: inset 0 0 0 1px var(--status-success-border, color-mix(in srgb, var(--success) 40%, transparent));
		animation: sr-pop 360ms cubic-bezier(0.34, 1.56, 0.64, 1);
	}
	@keyframes sr-pop {
		from { transform: scale(0.6); opacity: 0; }
		to { transform: scale(1); opacity: 1; }
	}
	.sr-sent h3 {
		margin: 0;
		font-size: 1rem;
		font-weight: 600;
	}
	.sr-sent p {
		margin: 0;
		max-width: 28rem;
		color: var(--muted-foreground);
		line-height: 1.5;
	}
	.sr-sent .sr-ref {
		margin-top: 4px;
		font-size: 0.75rem;
	}
	.sr-ref code {
		font-family: var(--font-mono, ui-monospace, monospace);
		color: var(--foreground);
	}

	@media (max-width: 639px) {
		.sr-btn,
		.sr-choice button {
			min-height: 44px;
		}
		.sr-ai-head {
			flex-wrap: wrap;
		}
		.sr-ai-head .sr-btn {
			width: 100%;
		}
		.sr-model-row {
			grid-template-columns: minmax(0, 1fr);
			gap: 6px;
		}
		.sr-ai-actions .sr-btn,
		.sr-local-actions .sr-btn {
			flex: 1 1 auto;
		}
		.sr-body {
			max-height: 14rem;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.sr :global(.sr-spin),
		.sr-skeleton span,
		.sr-progress span,
		.sr-local,
		.sr-sent-icon {
			animation: none;
		}
		.sr-progress span {
			width: 100%;
			opacity: 0.5;
		}
	}
</style>
