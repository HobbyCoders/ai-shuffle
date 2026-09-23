/**
 * The stock Claude Code CLI: what `claude auth status` reports, and the
 * in-app terminal that runs `claude auth login`.
 *
 * AI Shuffle never handles a Claude sign-in itself. The user signs in to the
 * CLI through Anthropic's own page; these calls only show the CLI's answer and
 * relay the terminal.
 */

import { api } from './client';

export interface ClaudeCliLoginHint {
	/** Where this worker runs: the Docker image, the desktop app, or a local run. */
	surface: 'docker' | 'desktop' | 'local';
	/** Whether this worker offers the in-app login terminal. */
	terminal: boolean;
	command: string;
	logout_command: string;
	status_command: string;
	/** `docker exec` form, with `<container>` for the container name (Docker only). */
	docker_command: string | null;
}

export interface ClaudeCliStatus {
	installed: boolean;
	/** null when the CLI's answer could not be read (see `error`). */
	signed_in: boolean | null;
	auth_method: string | null;
	auth_method_label: string | null;
	email: string | null;
	org_name: string | null;
	plan: string | null;
	api_provider: string | null;
	cli_version: string | null;
	binary_path: string | null;
	error: string | null;
	/** Unix seconds. */
	checked_at: number;
	login: ClaudeCliLoginHint;
}

export interface ClaudeCliLoginState {
	supported: boolean;
	/** A login was started and not yet cleared. */
	active: boolean;
	running: boolean;
	exit_code: number | null;
	/** null while running; true when the CLI reported a successful login. */
	succeeded: boolean | null;
	/** Plain text printed after the requested offset. */
	output: string;
	/** Offset to ask for next. */
	offset: number;
	/** True when the requested offset is no longer available: replace, don't append. */
	reset: boolean;
	/** The sign-in link the CLI printed, once it has. */
	url: string | null;
	error: string | null;
}

export function getClaudeCliStatus(refresh = false): Promise<ClaudeCliStatus> {
	return api.get(`/auth/claude/cli${refresh ? '?refresh=true' : ''}`, { cache: 'no-store' });
}

export function startClaudeCliLogin(): Promise<ClaudeCliLoginState> {
	return api.post('/auth/claude/cli-login/start', {});
}

export function readClaudeCliLogin(offset: number): Promise<ClaudeCliLoginState> {
	return api.get(`/auth/claude/cli-login?offset=${offset}`, { cache: 'no-store' });
}

export function sendClaudeCliLogin(text: string, offset: number): Promise<ClaudeCliLoginState> {
	return api.post('/auth/claude/cli-login/input', { text, offset });
}

export function cancelClaudeCliLogin(): Promise<ClaudeCliLoginState> {
	return api.post('/auth/claude/cli-login/cancel', {});
}

/** Sign the CLI out with its own `claude auth logout`. */
export function logoutClaudeCli(): Promise<{ success: boolean; message?: string; error?: string }> {
	return api.post('/auth/claude/logout', {});
}

/** Human name for the plan code the CLI prints, when it prints one. */
export function claudePlanLabel(plan: string | null | undefined): string | null {
	const value = (plan ?? '').trim().toLowerCase();
	if (!value) return null;
	if (value === 'max') return 'Max';
	if (value === 'pro') return 'Pro';
	if (value === 'team') return 'Team';
	if (value === 'enterprise') return 'Enterprise';
	return plan!.trim();
}
