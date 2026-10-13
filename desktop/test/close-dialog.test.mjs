import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../src/pages/close.js', import.meta.url), 'utf8');
function page() {
    let dialog;
    const document = {};
    class Element extends EventTarget {
        isConnected = true;
        children = [];
        textContent = '';
        returnValue = '';
        setAttribute() {}
        append(child) { this.children.push(child); }
        remove() { this.isConnected = false; }
        focus() { document.activeElement = this; }
        set innerHTML(_html) {
            this.parts = Object.fromEntries(['h2', 'p', '.quit-cancel', '.quit-confirm'].map(key => [key, new Element()]));
        }
        querySelector(selector) { return this.parts[selector]; }
        showModal() { this.open = true; }
        close(value = '') { this.returnValue = value; this.dispatchEvent(new Event('close')); }
    }
    document.head = new Element();
    document.body = new Element();
    document.activeElement = new Element();
    const previous = document.activeElement;
    document.createElement = tag => {
        const element = new Element();
        if (tag === 'dialog') dialog = element;
        return element;
    };
    const show = vm.runInNewContext(source, { document });
    return { document, previous, show, get dialog() { return dialog; } };
}

test('close dialog inherits theme tokens, focuses Keep Open and restores focus', async () => {
    const p = page();
    const pending = p.show({activeCount: 2});
    assert.equal(p.document.activeElement, p.dialog.querySelector('.quit-cancel'));
    assert.match(p.document.head.children[0].textContent, /var\(--card/);
    p.dialog.querySelector('.quit-cancel').dispatchEvent(new Event('click'));
    assert.equal(await pending, false);
    assert.equal(p.document.activeElement, p.previous);
    assert.equal(p.dialog.isConnected, false);
    assert.equal(p.document.head.children[0].isConnected, false);
});

test('only the explicit destructive button returns confirmation', async () => {
    const p = page();
    const pending = p.show({activeCount: 1});
    assert.match(p.dialog.querySelector('p').textContent, /1 active session\./);
    assert.match(p.dialog.querySelector('p').textContent, /stop it/);
    p.dialog.querySelector('.quit-confirm').dispatchEvent(new Event('click'));
    assert.equal(await pending, true);
});

test('workers that keep running subagents are named, and the local stop is still said (#1808)', async () => {
    const p = page();
    const pending = p.show({activeCount: 1, remoteWorkers: ['ServerWorker']});
    const text = p.dialog.querySelector('p').textContent;
    assert.match(text, /stop work on this computer/);
    assert.match(text, /Subagents on ServerWorker keep running there/);
    p.dialog.close();
    assert.equal(await pending, false);
    const q = page();
    const two = q.show({activeCount: 2, remoteWorkers: ['NAS', 'ServerWorker', 'Laptop']});
    assert.match(q.dialog.querySelector('p').textContent, /Subagents on NAS, ServerWorker and Laptop keep running/);
    q.dialog.close();
    await two;
});

test('native Escape dismissal never confirms', async () => {
    const p = page();
    const pending = p.show({activeCount: 2});
    p.dialog.close();
    assert.equal(await pending, false);
});

test('unavailable activity still warns rather than silently quitting', async () => {
    const p = page();
    const pending = p.show({activeCount: null});
    assert.match(p.dialog.querySelector('p').textContent, /Unable to check/);
    p.dialog.close();
    await pending;
});

test('cleanup error has no destructive action', async () => {
    const p = page();
    const pending = p.show({error: true});
    assert.equal(p.dialog.querySelector('h2').textContent, 'Sessions are still stopping');
    assert.equal(p.dialog.querySelector('.quit-confirm').isConnected, false);
    p.dialog.querySelector('.quit-cancel').dispatchEvent(new Event('click'));
    assert.equal(await pending, false);
});

test('NC-11: a repeated cleanup error offers Quit Anyway, and only that button confirms', async () => {
    const p = page();
    const pending = p.show({error: true, offerForce: true});
    const confirm = p.dialog.querySelector('.quit-confirm');
    assert.equal(confirm.isConnected, true);
    assert.equal(confirm.textContent, 'Quit Anyway');
    assert.match(p.dialog.querySelector('p').textContent, /may be interrupted/);
    assert.equal(p.document.activeElement, p.dialog.querySelector('.quit-cancel'), 'Keep Open stays the default');
    confirm.dispatchEvent(new Event('click'));
    assert.equal(await pending, true);
    const q = page();
    const kept = q.show({error: true, offerForce: true});
    q.dialog.close();
    assert.equal(await kept, false, 'Escape never quits');
});
