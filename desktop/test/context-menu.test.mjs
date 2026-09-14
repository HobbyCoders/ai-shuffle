import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../dist/main/context-menu.js', import.meta.url), 'utf8');

function menu(overrides = {}) {
  const exports = {};
  let handler, template, popup;
  const replacements = [];
  const dictionary = [];
  const win = {
    webContents: {
      on(event, callback) {
        assert.equal(event, 'context-menu');
        handler = callback;
      },
      replaceMisspelling(word) { replacements.push(word); },
      session: { addWordToSpellCheckerDictionary(word) { dictionary.push(word); } },
    },
  };
  vm.runInNewContext(source, {
    exports,
    require(name) {
      assert.equal(name, 'electron');
      return { Menu: { buildFromTemplate(items) {
        template = items;
        return { popup(options) { popup = options; } };
      } } };
    },
  });
  exports.attachTextContextMenu(win);
  const params = {
    isEditable: true,
    selectionText: '',
    misspelledWord: '',
    dictionarySuggestions: [],
    editFlags: {
      canUndo: true, canRedo: false, canCut: true, canCopy: true,
      canPaste: true, canDelete: false, canSelectAll: true,
    },
    frame: {}, x: 120, y: 80, menuSourceType: 'mouse',
    ...overrides,
  };
  handler({}, params);
  return { template, popup, params, win, replacements, dictionary };
}

const editingRoles = ['undo', 'redo', 'cut', 'copy', 'paste', 'pasteAndMatchStyle', 'delete', 'selectAll'];
const roles = m => Array.from(m.template, item => item.role).filter(Boolean);

test('misspelled editable text offers every Chromium suggestion and applies the chosen replacement', () => {
  const m = menu({ misspelledWord: 'teh', dictionarySuggestions: ['the', 'tech'] });
  assert.deepEqual(Array.from(m.template.slice(0, 2), item => item.label), ['the', 'tech']);
  m.template[1].click();
  m.template[0].click();
  assert.deepEqual(m.replacements, ['tech', 'the']);
  assert.equal(m.template[2].type, 'separator');
  assert.deepEqual(roles(m), editingRoles);
});

test('Add to dictionary uses the clicked word and the owning window session', () => {
  const m = menu({ misspelledWord: 'Shuffleword', selectionText: 'different selection', dictionarySuggestions: ['Shuffle word'] });
  m.template.find(item => item.label === 'Add to dictionary').click();
  assert.deepEqual(m.dictionary, ['Shuffleword']);
  assert.deepEqual(m.replacements, []);
});

test('unknown words with no suggestions retain dictionary and editing actions', () => {
  const m = menu({ misspelledWord: 'zzzzqqqq' });
  assert.equal(m.template[0].label, 'No spelling suggestions');
  assert.equal(m.template[0].enabled, false);
  m.template.find(item => item.label === 'Add to dictionary').click();
  assert.deepEqual(m.dictionary, ['zzzzqqqq']);
  assert.deepEqual(roles(m), editingRoles);
});

test('correctly spelled text has only the original editing commands and enabled flags', () => {
  const m = menu({ dictionarySuggestions: ['ignored without a misspelled word'] });
  assert.deepEqual(roles(m), editingRoles);
  assert.equal(m.template.some(item => item.label), false);
  for (const [role, flag] of [
    ['undo', 'canUndo'], ['redo', 'canRedo'], ['cut', 'canCut'], ['copy', 'canCopy'],
    ['paste', 'canPaste'], ['pasteAndMatchStyle', 'canPaste'], ['delete', 'canDelete'], ['selectAll', 'canSelectAll'],
  ]) assert.equal(m.template.find(item => item.role === role).enabled, m.params.editFlags[flag]);
});

test('read-only selections never offer spelling mutations', () => {
  const m = menu({ isEditable: false, selectionText: 'teh', misspelledWord: 'teh', dictionarySuggestions: ['the'] });
  assert.deepEqual(roles(m), ['copy', 'selectAll']);
  assert.equal(m.template.some(item => item.label), false);
});

test('non-text targets do not open a menu', () => {
  assert.equal(menu({ isEditable: false, editFlags: { canSelectAll: false } }).template, undefined);
});

test('popup retains the originating window, frame, coordinates and source', () => {
  const m = menu({ misspelledWord: 'teh', dictionarySuggestions: ['the'] });
  assert.equal(m.popup.window, m.win);
  assert.equal(m.popup.frame, m.params.frame);
  assert.equal(m.popup.x, m.params.x);
  assert.equal(m.popup.y, m.params.y);
  assert.equal(m.popup.sourceType, m.params.menuSourceType);
  assert.equal(menu({ frame: null }).popup.frame, undefined);
});
