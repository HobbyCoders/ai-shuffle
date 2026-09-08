import { afterEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { attachComposerOpen } from './mobile-panel-gesture';
const cleanups: (() => void)[] = [];
afterEach(() => { cleanups.splice(0).forEach(fn => fn()); document.body.replaceChildren(); window.getSelection()?.removeAllRanges(); vi.restoreAllMocks(); });
function setup() {
 const form = document.createElement('form'), button = document.createElement('button'), editor = document.createElement('div');
 button.type = 'button'; editor.contentEditable = 'true'; editor.setAttribute('contenteditable', 'true'); editor.tabIndex = 0;
 form.append(button, editor); document.body.append(form);
 const state = { enabled: true, allowEditor: true, open: false, compact: true }; const open = vi.fn(), click = vi.fn(); button.onclick = click;
 const options: Parameters<typeof attachComposerOpen>[1] = { enabled: () => state.enabled, allowEditor: () => state.allowEditor, open: () => state.open, openDirection: () => state.compact ? 'up' : 'down', onOpenChange: value => { state.open = value; open(value); } };
 const action = attachComposerOpen(form, options);
 cleanups.push(action.destroy); return { form, button, editor, state, open, click, action, options };
}
function touch(target: Element, type: string, y: number, x = 100, count = 1, cancelable = true) {
 const item = { identifier: 1, clientX: x, clientY: y }; const event = new Event(type, { bubbles: true, cancelable });
 Object.assign(event, { touches: type === 'touchend' ? [] : Array.from({length: count}, (_, i) => ({...item, identifier: i + 1})), changedTouches: [item] });
 target.dispatchEvent(event); return event;
}
function swipe(target: Element, y = 50, x = 100) { touch(target, 'touchstart', 100); const move = touch(target, 'touchmove', y, x); touch(target, 'touchend', y, x); return move; }

const states = [
 { layout: 'existing chat', compact: true, initialOpen: false, dy: -60 },
 { layout: 'existing chat', compact: true, initialOpen: true, dy: 60 },
 { layout: 'centered new session', compact: false, initialOpen: false, dy: 60 },
 { layout: 'centered new session', compact: false, initialOpen: true, dy: -60 }
];
describe.each(states)('$layout, initially open=$initialOpen', ({ compact, initialOpen, dy }) => {
 function arrange() { const h=setup(); Object.assign(h.state, { compact, open: initialOpen }); return h; }
 it('opens and closes from the same empty, unfocused editor without focus theft', () => {
  const h=arrange(); expect(swipe(h.editor,100+dy).defaultPrevented).toBe(true);
  expect(h.open).toHaveBeenLastCalledWith(!initialOpen); expect(h.state.open).toBe(!initialOpen);
  expect(document.activeElement).not.toBe(h.editor);
  swipe(h.editor,100-dy); expect(h.open).toHaveBeenLastCalledWith(initialOpen); expect(h.open).toHaveBeenCalledTimes(2);
 });
 it('rejects the wrong direction permanently even if the gesture reverses', () => {
  const h=arrange(); touch(h.editor,'touchstart',100);
  expect(touch(h.editor,'touchmove',100-dy).defaultPrevented).toBe(false);
  expect(touch(h.editor,'touchmove',100+dy).defaultPrevented).toBe(false);
  touch(h.editor,'touchend',100+dy); expect(h.open).not.toHaveBeenCalled();
 });
 it('snapshots the requested state and layout rather than toggling a later value', () => {
  const h=arrange(); touch(h.editor,'touchstart',100);
  h.state.open=!initialOpen; h.state.compact=!compact;
  expect(touch(h.editor,'touchmove',100+dy).defaultPrevented).toBe(true);
  touch(h.editor,'touchend',100+dy); expect(h.open).toHaveBeenCalledExactlyOnceWith(!initialOpen);
 });
 it('snapshots state and layout before pointerdown handlers reflow the composer', () => {
  const h=arrange(); h.editor.addEventListener('pointerdown',()=>{ h.state.open=!initialOpen; h.state.compact=!compact; });
  const down=new Event('pointerdown',{bubbles:true}); Object.assign(down,{pointerType:'touch',isPrimary:true}); h.editor.dispatchEvent(down);
  swipe(h.editor,100+dy); expect(h.open).toHaveBeenCalledExactlyOnceWith(!initialOpen);
 });
 it.each(['touchcancel','pointercancel','blur','multi','native','disabled'])('does not change state after %s', kind => {
  const h=arrange(); touch(h.button,'touchstart',100); touch(h.button,'touchmove',100+dy);
  if(kind==='multi') touch(document.body,'touchstart',100+dy,100,2);
  else if(kind==='native') touch(h.button,'touchmove',100+dy,100,1,false);
  else if(kind==='disabled') h.state.enabled=false;
  else window.dispatchEvent(new Event(kind));
  touch(h.button,'touchend',100+dy); expect(h.open).not.toHaveBeenCalled();
 });
 it('keeps draft editing, focus, selection and scrollable editors native in either state', () => {
  const h=arrange(); h.state.allowEditor=false;
  expect(swipe(h.editor,100+dy).defaultPrevented).toBe(false);
  h.state.allowEditor=true; h.editor.focus(); expect(swipe(h.editor,100+dy).defaultPrevented).toBe(false); h.editor.blur();
  h.editor.textContent='Selected draft'; const range=document.createRange(); range.selectNodeContents(h.editor); window.getSelection()?.addRange(range);
  expect(swipe(h.editor,100+dy).defaultPrevented).toBe(false); window.getSelection()?.removeAllRanges();
  h.editor.style.overflowY='auto'; Object.defineProperties(h.editor,{scrollHeight:{value:400},clientHeight:{value:100}});
  expect(swipe(h.editor,100+dy).defaultPrevented).toBe(false); expect(h.open).not.toHaveBeenCalled();
  swipe(h.button,100+dy); expect(h.open).toHaveBeenCalledExactlyOnceWith(!initialOpen);
 });
 it('uses the callback captured at gesture start', () => {
  const h=arrange(), replacement=vi.fn();
  touch(h.editor,'touchstart',100); h.options.onOpenChange=replacement;
  touch(h.editor,'touchmove',100+dy); touch(h.editor,'touchend',100+dy);
  expect(h.open).toHaveBeenCalledExactlyOnceWith(!initialOpen); expect(replacement).not.toHaveBeenCalled();
 });
 it('preserves the 24px distance and quick-flick thresholds in each direction', () => {
  const h=arrange(), sign=Math.sign(dy), now=vi.spyOn(performance,'now').mockReturnValue(0);
  touch(h.button,'touchstart',100); now.mockReturnValue(1000);
  touch(h.button,'touchmove',100+23*sign); touch(h.button,'touchend',100+23*sign); expect(h.open).not.toHaveBeenCalled();
  swipe(h.button,100+24*sign); expect(h.open).toHaveBeenCalledExactlyOnceWith(!initialOpen);
  h.state.open=initialOpen; now.mockReturnValue(2000); touch(h.button,'touchstart',100); now.mockReturnValue(2020);
  touch(h.button,'touchmove',100+17*sign); touch(h.button,'touchend',100+17*sign); expect(h.open).toHaveBeenCalledTimes(2);
  now.mockRestore();
 });
 it.each(['focus', 'selection', 'scroll'])('surrenders if %s takes ownership after touch start', kind => {
  const h=arrange(); touch(h.editor,'touchstart',100);
  if(kind==='focus') h.editor.focus();
  else if(kind==='selection') { h.editor.textContent='draft'; const range=document.createRange(); range.selectNodeContents(h.editor); window.getSelection()?.addRange(range); }
  else { h.editor.style.overflowY='auto'; Object.defineProperties(h.editor,{scrollHeight:{value:400},clientHeight:{value:100}}); }
  expect(touch(h.editor,'touchmove',100+dy).defaultPrevented).toBe(false);
  h.editor.blur(); window.getSelection()?.removeAllRanges(); h.editor.style.overflowY='visible';
  expect(touch(h.editor,'touchmove',100+dy).defaultPrevented).toBe(false);
  touch(h.editor,'touchend',100+dy); expect(h.open).not.toHaveBeenCalled();
 });
 it('leaves panel descendants native in both directions, including model list options', () => {
  const h=arrange(), panel=document.createElement('section'), option=document.createElement('button');
  panel.className='mobile-composer-controls'; panel.append(option); h.form.append(panel);
  expect(swipe(option,100+dy).defaultPrevented).toBe(false); expect(swipe(option,100-dy).defaultPrevented).toBe(false);
  expect(h.open).not.toHaveBeenCalled();
 });
 it('preserves taps, restores tiny consumed button taps once, and suppresses only consequential clicks', () => {
  const h=arrange(); swipe(h.button,100); h.button.dispatchEvent(new MouseEvent('click',{bubbles:true,detail:1}));
  expect(h.click).toHaveBeenCalledOnce();
  swipe(h.button,100+Math.sign(dy)); expect(h.click).toHaveBeenCalledTimes(2); expect(h.open).not.toHaveBeenCalled();
  h.button.dispatchEvent(new MouseEvent('click',{bubbles:true,detail:1})); expect(h.click).toHaveBeenCalledTimes(2);
  swipe(h.button,100+dy); h.button.dispatchEvent(new MouseEvent('click',{bubbles:true,detail:1})); expect(h.click).toHaveBeenCalledTimes(2);
  h.button.click(); expect(h.click).toHaveBeenCalledTimes(3);
 });
});

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
 it('wires both mobile sheet states through the focus-aware setter using the visible layout', () => {
  const chat=readFileSync('src/lib/components/chat/ChatInput.svelte','utf8'), dock=readFileSync('src/lib/components/chat/ControlsDock.svelte','utf8');
  expect(chat).toContain('use:composerOpenGesture');
  expect(chat).toContain('bind:this={controlsDockRef}');
  expect(chat).toContain('enabled: () => isMobileComposerViewport()');
  expect(chat.includes('isMobileComposerViewport() && !mobileControlsOpen')).toBe(false);
  expect(chat).toContain('allowEditor: () => !inputValue.trim() && !composerFocused');
  expect(chat).toContain('open: () => mobileControlsOpen');
  expect(chat).toContain("openDirection: () => compact ? 'up' : 'down'");
  expect(chat).toContain('onOpenChange: (open) => controlsDockRef?.setMobileSheetOpen(open)');
  expect(dock).toContain('export function setMobileSheetOpen(open: boolean)');
 });
});
