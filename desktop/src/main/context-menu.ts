import { clipboard, Menu, type BrowserWindow, type MenuItemConstructorOptions } from 'electron';

/** A web link worth copying: http(s) only, never script, data or app-internal schemes. */
export function copyableLink(url: string): string | null {
	if (!url) return null;
	try {
		const { protocol } = new URL(url);
		return protocol === 'http:' || protocol === 'https:' ? url : null;
	} catch {
		return null;
	}
}

/** Native text editing and link copying for every document loaded in an application window. */
export function attachTextContextMenu(win: BrowserWindow): void {
	win.webContents.on('context-menu', (_event, params) => {
		const { editFlags, isEditable, selectionText } = params;
		const link = copyableLink(params.linkURL);
		if (!link && !isEditable && !selectionText && !editFlags.canSelectAll) return;

		const template: MenuItemConstructorOptions[] = isEditable
			? [
					{ role: 'undo', enabled: editFlags.canUndo },
					{ role: 'redo', enabled: editFlags.canRedo },
					{ type: 'separator' },
					{ role: 'cut', enabled: editFlags.canCut },
					{ role: 'copy', enabled: editFlags.canCopy },
					{ role: 'paste', enabled: editFlags.canPaste },
					{ role: 'pasteAndMatchStyle', enabled: editFlags.canPaste },
					{ role: 'delete', enabled: editFlags.canDelete },
					{ type: 'separator' },
					{ role: 'selectAll', enabled: editFlags.canSelectAll }
				]
			: [
					{ role: 'copy', enabled: editFlags.canCopy },
					{ type: 'separator' },
					{ role: 'selectAll', enabled: editFlags.canSelectAll }
				];

		if (link) {
			template.unshift(
				{ label: 'Copy Link Address', click: () => clipboard.writeText(link) },
				{ type: 'separator' }
			);
		}

		// Electron draws the underline, but custom menus must expose Chromium's
		// suggestions explicitly. Keep these tied to the word that was clicked.
		if (isEditable && params.misspelledWord) {
			const spelling: MenuItemConstructorOptions[] = params.dictionarySuggestions.length
				? params.dictionarySuggestions.map((suggestion) => ({
						label: suggestion,
						click: () => win.webContents.replaceMisspelling(suggestion)
					}))
				: [{ label: 'No spelling suggestions', enabled: false }];
			spelling.push(
				{ type: 'separator' },
				{
					label: 'Add to dictionary',
					click: () => win.webContents.session.addWordToSpellCheckerDictionary(params.misspelledWord)
				},
				{ type: 'separator' }
			);
			template.unshift(...spelling);
		}

		Menu.buildFromTemplate(template).popup({
			window: win,
			frame: params.frame ?? undefined,
			x: params.x,
			y: params.y,
			sourceType: params.menuSourceType
		});
	});
}
