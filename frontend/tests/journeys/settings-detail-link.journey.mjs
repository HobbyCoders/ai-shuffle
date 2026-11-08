// The actual Automation notice deep-links to Claude in Settings, with every phone Back step.
import { runJourney } from './runner.mjs';
import { expect, gotoApp } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
const mobileShell = process.argv.includes('--mobile-shell');
await runJourney({ name: 'settings-detail-link', allow: DEV_INSTANCE_NOISE, cases: [{
 // Responsive desktop only: the real /mobile/ shell has no window to shrink.
 name: 'keeps-the-open-page-at-phone-width', viewports: mobileShell ? [] : ['desktop'],
 allow: [{kind:'layout_shift',reason:'known bug #1301: Settings > Security adds rows after a reload, unrelated to which page is kept'}],
 async setup({page,status}) { await gotoApp(page,status); },
 async act({page,mark}) {
  const onSecurity=async why=>{
   await page.getByRole('heading',{name:'Security',exact:true}).first().waitFor({timeout:15000});
   expect(await page.locator('.settings-shell[data-view="page"]').count()===1,`${why}: still on the page, not the list`);
  };
  await mark('open Settings > Security at desktop width');
  await page.getByRole('navigation',{name:'Workspace',exact:true}).getByRole('button',{name:'Settings',exact:true}).click();
  await page.getByRole('navigation',{name:'Settings',exact:true}).getByRole('button',{name:'Security',exact:true}).click();
  await page.getByRole('heading',{name:'Security',exact:true}).first().waitFor();
  await mark('shrink to phone width');
  await page.setViewportSize({width:390,height:844});
  await onSecurity('after the resize');
  await mark('reload at phone width');
  await page.reload();
  await onSecurity('after the reload');
  await mark('Back reaches the list, and a reload then stays on the list');
  await page.locator('.topbar').getByRole('button',{name:'Back to Settings',exact:true}).click();
  await page.getByRole('navigation',{name:'Settings',exact:true}).waitFor();
  await page.reload();
  await page.getByRole('navigation',{name:'Settings',exact:true}).waitFor({timeout:15000});
  expect(await page.locator('.settings-shell[data-view="page"]').count()===0,'the list stays the list after Back');
 }
},{
 name: mobileShell ? 'native-cold-and-retained-target' : 'cold-and-retained-target', viewports: ['phone'],
 async setup({page,status}) {
  if (mobileShell) { await gotoApp(page,status,'/mobile/'); expect(new URL(page.url()).pathname === '/mobile/', 'real mobile bundle'); }
  else await gotoApp(page,status);
 },
 async act({page,mark}) {
  for (const visit of ['cold','retained']) {
   await mark(`${visit} Settings target from an Automation detail`);
   const nav=page.getByRole('navigation',{name:'Workspace',exact:true});
   if (!await nav.isVisible()) await page.getByRole('button',{name:/^(Open menu|Chats)( \(|$)/}).first().click();
   await nav.getByRole('button',{name:'More',exact:true}).click();
   await page.getByRole('menuitem',{name:'Automations',exact:true}).click();
   const create=page.getByRole('button',{name:'New automation',exact:true}).first();
   const notice=page.getByRole('button',{name:'Open Claude settings',exact:true});
   await notice.or(create).filter({visible:true}).first().waitFor();
   if(!await notice.isVisible()) await create.click();
   await notice.click();
   const target=page.locator('[data-setting-anchor="providers.claudeMode"]');
   await target.waitFor({state:'visible',timeout:15000});
   expect(await page.locator('.settings-shell[data-view="detail"]').count()===1,'target detail is not the hidden Settings index');
   expect(await target.isVisible(),'Claude mode row is actually rendered');
   const bar=page.locator('.topbar');
   await bar.getByRole('button',{name:'Back to Providers',exact:true}).click();
   await page.getByRole('heading',{name:'Providers',exact:true}).waitFor();
   expect(await bar.getByRole('button',{name:/^(Chats|Open menu)/}).count()===0,'no jumping global menu on the detail');
   await bar.getByRole('button',{name:'Back to Settings',exact:true}).click();
   await page.getByRole('navigation',{name:'Settings',exact:true}).waitFor();
   expect(await bar.getByRole('button',{name:/^(Chats|Open menu)/}).count()===1,'menu is one Back away at the index');
  }
 }
}]});
