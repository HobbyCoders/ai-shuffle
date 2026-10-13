import { Menu, type BrowserWindow, type MenuItemConstructorOptions } from 'electron';

/** Native text editing for every document loaded in an application window. */
export function attachTextContextMenu(win: BrowserWindow): void {
	win.webContents.on('context-menu', (_event, params) => {
		const { editFlags, isEditable, selectionText } = params;
		if (!isEditable && !selectionText && !editFlags.canSelectAll) return;

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
