import { afterEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { attachComposerOpen } from './mobile-panel-gesture';
const cleanups: (() => void)[] = [];
afterEach(() => { cleanups.splice(0).forEach(fn => fn()); document.body.replaceChildren(); });
function setup() {
 const form = document.createElement('form'), button = document.createElement('button'), editor = document.createElement('div');
 button.type = 'button'; editor.contentEditable = 'true'; editor.setAttribute('contenteditable', 'true'); editor.tabIndex = 0;
 form.append(button, editor); document.body.append(form);
 const state = { enabled: true, allowEditor: true }; const open = vi.fn(), click = vi.fn(); button.onclick = click;
 const action = attachComposerOpen(form, { enabled: () => state.enabled, allowEditor: () => state.allowEditor, onOpen: open });
 cleanups.push(action.destroy); return { form, button, editor, state, open, click, action };
}
function touch(target: Element, type: string, y: number, x = 100, count = 1, cancelable = true) {
 const item = { identifier: 1, clientX: x, clientY: y }; const event = new Event(type, { bubbles: true, cancelable });
 Object.assign(event, { touches: type === 'touchend' ? [] : Array.from({length: count}, (_, i) => ({...item, identifier: i + 1})), changedTouches: [item] });
 target.dispatchEvent(event); return event;
}
function swipe(target: Element, y = 50, x = 100) { touch(target, 'touchstart', 100); const move = touch(target, 'touchmove', y, x); touch(target, 'touchend', y, x); return move; }
describe('composer opening touch arbitration', () => {
 it('opens upward and suppresses the trailing physical click, not keyboard activation', () => { const h = setup(); expect(swipe(h.button).defaultPrevented).toBe(true); expect(h.open).toHaveBeenCalledOnce(); h.button.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, detail: 1 })); expect(h.click).not.toHaveBeenCalled(); h.button.click(); expect(h.click).toHaveBeenCalledOnce(); });
 it.each([[150, 100], [95, 180], [100, 100], [93, 100]])('does not open for downward/horizontal/tap/slop %s %s', (y,x) => { const h = setup(); swipe(h.button,y,x); expect(h.open).not.toHaveBeenCalled(); });
 it('uses 24px distance or existing flick threshold', () => { const h = setup(); const now = vi.spyOn(performance, 'now').mockReturnValue(0); touch(h.button,'touchstart',100); now.mockReturnValue(1000); touch(h.button,'touchmove',77); touch(h.button,'touchend',77); expect(h.open).not.toHaveBeenCalled(); swipe(h.button,76); expect(h.open).toHaveBeenCalledOnce(); now.mockRestore(); });
 it('does not suppress a new intentional touch after the sheet opens', () => {
  const h = setup(); swipe(h.button); h.state.enabled = false;
  const down = new Event('pointerdown', { bubbles: true }); Object.assign(down, { pointerType: 'touch', isPrimary: true }); h.button.dispatchEvent(down);
  h.button.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 })); expect(h.click).toHaveBeenCalledOnce();
 });
 it('preserves ordinary taps and restores consumed tiny button taps once', () => { const h=setup(); swipe(h.button,99); expect(h.click).toHaveBeenCalledOnce(); expect(h.open).not.toHaveBeenCalled(); });
 it.each(['touchcancel','pointercancel','blur','multi','native','disabled'])('cancels %s', kind => { const h=setup(); touch(h.button,'touchstart',100); touch(h.button,'touchmove',70); if(kind==='multi') touch(document.body,'touchstart',70,100,2); else if(kind==='native') touch(h.button,'touchmove',60,100,1,false); else if(kind==='disabled') h.state.enabled=false; else window.dispatchEvent(new Event(kind)); touch(h.button,'touchend',40); expect(h.open).not.toHaveBeenCalled(); });
 it('never takes horizontal native scrolling over later', () => { const h=setup(); touch(h.button,'touchstart',100); expect(touch(h.button,'touchmove',99,130).defaultPrevented).toBe(false); expect(touch(h.button,'touchmove',20).defaultPrevented).toBe(false); touch(h.button,'touchend',20); expect(h.open).not.toHaveBeenCalled(); });
 it('gates editor but not expanded non-editable surfaces', () => { const h=setup(); h.state.allowEditor=false; expect(swipe(h.editor).defaultPrevented).toBe(false); swipe(h.button); expect(h.open).toHaveBeenCalledOnce(); });
 it('does not take focused editor, selection, or panel descendants', () => { const h=setup(); h.editor.focus(); swipe(h.editor); h.editor.blur(); h.editor.textContent='draft'; const range=document.createRange(); range.selectNodeContents(h.editor); window.getSelection()?.addRange(range); swipe(h.editor); window.getSelection()?.removeAllRanges(); h.button.className='mobile-composer-controls'; swipe(h.button); expect(h.open).not.toHaveBeenCalled(); });
 it('snapshots empty editor before pointerdown reflow', () => { const h=setup(); const event=new Event('pointerdown',{bubbles:true}); Object.assign(event,{pointerType:'touch',isPrimary:true}); h.editor.addEventListener('pointerdown',()=>{h.state.allowEditor=false;}); h.editor.dispatchEvent(event); swipe(h.editor); expect(h.open).toHaveBeenCalledOnce(); });
 it('leaves scrollable surfaces native even at top', () => { const h=setup(); h.form.style.overflowY='auto'; Object.defineProperties(h.form,{scrollHeight:{value:300},clientHeight:{value:100}}); expect(swipe(h.button).defaultPrevented).toBe(false); expect(h.open).not.toHaveBeenCalled(); });
 it('cleans up listeners and cancels a pending gesture', () => { const h=setup(); touch(h.button,'touchstart',100); h.action.destroy(); swipe(h.button); expect(h.open).not.toHaveBeenCalled(); });
 it('wires a mobile-only closed-sheet action through the existing setter without focusing', () => { const chat=readFileSync('src/lib/components/chat/ChatInput.svelte','utf8'), dock=readFileSync('src/lib/components/chat/ControlsDock.svelte','utf8'); expect(chat).toContain('use:composerOpenGesture'); expect(chat).toContain('bind:this={controlsDockRef}'); expect(chat).toContain('enabled: () => isMobileComposerViewport() && !mobileControlsOpen'); expect(chat).toContain('allowEditor: () => mobileCollapsed && !inputValue.trim() && !composerFocused'); expect(dock).toMatch(/export function openMobileSheet\(\) \{\s*setMobileSheetOpen\(true\);\s*\}/); });
});
