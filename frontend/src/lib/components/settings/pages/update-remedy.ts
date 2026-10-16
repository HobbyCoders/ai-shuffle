/**
 * A worker whose update only the latest installer can finish: the desktop
 * cannot update itself, or its update needs a newer installer or launcher
 * (`remedy` and `data_kept` from app/core/update_workers.py remedy()). Shared
 * by System > Updates and the sidebar update button; no imports, so the
 * sidebar does not pull in settings state.
 */

/** Where every installer is published; the newest release is always at this address. */
export const INSTALLER_URL = 'https://github.com/HobbyCoders/ai-shuffle/releases/latest';
export const INSTALLER_ACTION = 'Download the latest installer';
export const INSTALLER_LINK = { label: INSTALLER_ACTION, href: INSTALLER_URL };
/** Said only when the launcher kept the previous data (`data_kept`). */
export const DATA_KEPT = 'Your data is kept as it was before the update.';

/** Plain words for a worker only the latest installer can update, or undefined. */
export function installerHint(worker: { remedy?: 'installer' | null; data_kept?: boolean; phase: string }): string | undefined {
	if (worker.remedy !== 'installer') return undefined;
	const why = worker.phase === 'unsupported' ? 'This app cannot update itself.' : 'This update cannot finish inside the app.';
	return `${why} Run the latest installer to finish it.${worker.data_kept ? ` ${DATA_KEPT}` : ''}`;
}
