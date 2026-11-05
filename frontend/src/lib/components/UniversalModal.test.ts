import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import UniversalModal from './UniversalModal.svelte';
afterEach(cleanup);
describe('universal dialog chrome', () => {
 it('uses the shared surface and neutral primary action, preserving Escape', async () => {
  const onClose = vi.fn(), onSave = vi.fn();
  render(UniversalModal, {open:true,title:'Edit project',onClose,onSave,saveLabel:'Save project'});
  const dialog = screen.getByRole('dialog', {name:'Edit project'});
  expect(dialog.classList.contains('ui-surface')).toBe(true);
  expect(dialog.querySelector('.ui-dialog')).not.toBeNull();
  const save = screen.getByRole('button', {name:'Save project'});
  expect(save.classList.contains('primary')).toBe(true);
  await fireEvent.click(save); expect(onSave).toHaveBeenCalledOnce();
  await fireEvent.keyDown(window,{key:'Escape'});expect(onClose).toHaveBeenCalledOnce();
 });
 it('retains tabs, loading and disabled states', () => {
  render(UniversalModal,{open:true,title:'Project',onClose:vi.fn(),onSave:vi.fn(),saving:true,
   tabs:[{id:'general',label:'General'},{id:'workers',label:'Workers'}],activeTab:'general'});
  expect(screen.getByRole('tab',{name:'General'}).getAttribute('aria-selected')).toBe('true');
  const save=screen.getByRole('button',{name:'Save'}) as HTMLButtonElement;
  expect(save.disabled).toBe(true); expect(save.getAttribute('aria-busy')).toBe('true');
 });
});
