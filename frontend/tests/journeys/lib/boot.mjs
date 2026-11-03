// Observe the app's reconciliation milestone, not fetch delivery or a sleep.
// Boot clears the two bounded, content-free marks before each initialization.
import { traceNote } from './trace.mjs';

function bootState() {
	return {
		view: JSON.parse(localStorage.getItem('ai-shuffle-layout') || 'null')?.activeView,
		session: JSON.parse(sessionStorage.getItem('ai-shuffle-active-session') || 'null')?.sessionId,
		milestones: ['shuffle:boot.init.end', 'shuffle:boot.reconciled'].flatMap(name =>
			performance.getEntriesByName(name).map(entry => ({ name, t: entry.startTime }))),
		bootstrap: performance.getEntriesByType('measure')
			.filter(entry => entry.name.startsWith('shuffle:app.bootstrap:'))
			.map(entry => ({ name: entry.name, end: entry.startTime + entry.duration }))
	};
}

async function waitForBootState(page, label, predicate, timeout) {
	try {
		await page.waitForFunction(predicate, null, { timeout });
	} catch (error) {
		const state = await page.evaluate(bootState);
		throw new Error(`${label}: ${JSON.stringify(state)} (${error.message.split('\n')[0]})`);
	}
	traceNote(page, `${label}: ${JSON.stringify(await page.evaluate(bootState))}`);
}

// Preserve the first-send check's historical two-second post-load bound. A
// stuck/slow reconciliation fails with its view and bootstrap timing attached.
export async function waitForBootReconciled(page, { timeout = 2000 } = {}) {
	await waitForBootState(page, 'boot reconciliation', () =>
		performance.getEntriesByName('shuffle:boot.reconciled').length > 0, timeout);
}

export async function waitForCreatedChat(page, { timeout = 2000 } = {}) {
	await waitForBootState(page, 'layout follows the created chat', () => {
		const view = JSON.parse(localStorage.getItem('ai-shuffle-layout') || 'null')?.activeView;
		const session = JSON.parse(sessionStorage.getItem('ai-shuffle-active-session') || 'null')?.sessionId;
		return typeof session === 'string' && view?.type === 'session' && view.sessionId === session;
	}, timeout);
}
