import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const layout = vi.hoisted(() => ({ openFeaturePage: vi.fn() }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout }));
import CodeSandboxRow from './CodeSandboxRow.svelte';

beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

it('says the sandbox is active when the self-test passed', () => {
	render(CodeSandboxRow, { status: { mode: 'auto', state: 'active', active: true, reason: '' } });
	expect(screen.getByTestId('code-sandbox-state').textContent).toBe('Code sandbox: active');
	expect(screen.queryByTestId('code-sandbox-reason')).toBeNull();
});

it('distinguishes an unavailable sandbox and explains Ask versus Bypass', () => {
	const reason = 'bwrap: No permissions to create a new namespace (the container needs --security-opt seccomp=unconfined)';
	render(CodeSandboxRow, { status: { mode: 'auto', state: 'unavailable', active: false, reason } });
	expect(screen.getByTestId('code-sandbox-state').textContent).toBe('Code sandbox: unavailable');
	expect(screen.getByTestId('code-sandbox-permissions').textContent).toContain('Bypass skips ordinary tool approvals');
	expect(screen.getByTestId('code-sandbox-permissions').textContent).toContain('explicit consent requests still require your approval');
	expect(screen.getByTestId('code-sandbox-state').closest('.set-row')?.classList.contains('stack')).toBe(true);
	expect(screen.getByTestId('code-sandbox-reason').textContent).toBe(reason);
});

it('shows a checking state until the worker answers or its pre-warm finishes', () => {
	const { unmount } = render(CodeSandboxRow, { status: null });
	expect(screen.getByTestId('code-sandbox-state').textContent).toBe('Code sandbox: checking…');
	unmount();
	render(CodeSandboxRow, { status: { mode: 'auto', state: 'checking', active: false, reason: 'the self-test has not finished yet' } });
	expect(screen.getByTestId('code-sandbox-state').textContent).toBe('Code sandbox: checking…');
	expect(screen.queryByTestId('code-sandbox-reason')).toBeNull();
});

it('distinguishes an explicitly disabled sandbox', () => {
	render(CodeSandboxRow, { status: { mode: 'off', state: 'off', active: false, reason: 'CODE_MODE_OS_SANDBOX is off' } });
	expect(screen.getByTestId('code-sandbox-state').textContent).toBe('Code sandbox: off');
});

it('opens the install guide in the docs panel', async () => {
	render(CodeSandboxRow, { status: { mode: 'off', state: 'off', active: false, reason: 'CODE_MODE_OS_SANDBOX is off' } });
	await fireEvent.click(screen.getByRole('button', { name: /install guide/ }));
	expect(layout.openFeaturePage).toHaveBeenCalledWith('docs', 'getting-started/install-docker-compose');
});
