import { render, cleanup } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import BrowserWorkspaceNotice from './BrowserWorkspaceNotice.svelte';
afterEach(cleanup);

describe('browser workspace notice', () => {
	it('shows the runtime notice as a status, and nothing without one', async () => {
		const view = render(BrowserWorkspaceNotice, { notice: 'This step needs your own browser.' });
		expect(view.getByRole('status').textContent).toBe('This step needs your own browser.');
		await view.rerender({ notice: undefined });
		expect(view.queryByRole('status')).toBeNull();
	});
});
