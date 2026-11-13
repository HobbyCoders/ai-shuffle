import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { flushSync } from 'svelte';
import DangerZone from './DangerZone.svelte';
import { FEATURE_PANEL_CONTEXT } from '../../layout/feature-panel-context';

afterEach(cleanup);

const panel = $state({ active: true });

function renderZone() {
	panel.active = true;
	const items = [
		{ label: 'Sign out of the CLI', button: 'Sign out', question: 'Sign out?', onConfirm: vi.fn(async () => { throw new Error('The CLI is busy'); }) },
		{ label: 'Remove this key', button: 'Remove', question: 'Remove it?', onConfirm: vi.fn(async () => { throw new Error('Database is busy'); }) }
	];
	render(DangerZone, { props: { items }, context: new Map([[FEATURE_PANEL_CONTEXT, panel]]) });
}

/** Arms and confirms one action; the confirm reuses the trigger's label. */
async function fail(button: string) {
	await fireEvent.click(screen.getByRole('button', { name: button }));
	const confirm = screen.getByRole('button', { name: button });
	confirm.focus();
	await fireEvent.click(confirm);
	return screen.findByRole('alert');
}

describe('DangerZone failures (#1523)', () => {
	it('shows why under the row and gives focus back to its button', async () => {
		renderZone();
		const alert = await fail('Remove');
		expect(alert.textContent).toBe('Database is busy');
		expect(alert.previousElementSibling?.textContent).toContain('Remove this key');
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Remove' })));
	});

	it('clears an earlier failure as soon as another confirm opens', async () => {
		renderZone();
		await fail('Remove');
		await fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
		expect(screen.queryByRole('alert')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
		const alerts = await screen.findAllByRole('alert');
		expect(alerts.map((a) => a.textContent)).toEqual(['The CLI is busy']);
	});

	it('clears a failure when Settings is left (it stays mounted while hidden)', async () => {
		renderZone();
		await fail('Remove');
		panel.active = false;
		flushSync();
		expect(screen.queryByRole('alert')).toBeNull();
	});
});
