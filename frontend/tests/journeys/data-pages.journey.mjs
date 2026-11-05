// Content-first data destinations must expose the real content and every moved
// control without taking a panel/detail history step on responsive and native phone.
import { runJourney } from './runner.mjs';
import { expect, gotoApp } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
const native = process.argv.includes('--mobile-shell');
await runJourney({
 name: 'data-pages',
 viewports: ['desktop', 'phone'],
 throttles: ['fast'],
 allow: [...DEV_INSTANCE_NOISE, {
  kind: 'failed_requests', status: 401, url: /\/github\/inbox(\?|$)/,
  reason: 'the disposable dev instance has no GitHub sign-in; the rendered sign-in error is expected here'
 }],
 cases: [{
  name: 'content-and-controls-first',
  async setup({page,status,variant}) {
   await gotoApp(page,status,native && variant.viewport === 'phone' ? '/mobile/' : '/');
  },
  async act({page,mark,variant}) {
   const phone=variant.viewport==='phone';
   async function checkSingleTitle(label) {
    const heading=page.getByRole('heading',{level:2,name:label,exact:true});
    expect(await heading.count()===1,label+' keeps its semantic detail heading');
    const size=await heading.boundingBox();
    expect(size.width>1 && size.height>1,
     label+' has a visible range/state title distinct from the host destination');
   }
   const workspace=page.getByRole('navigation',{name:'Workspace',exact:true});
   async function open(label) {
    if(!await workspace.isVisible())await page.getByRole('button',{name:native&&phone?/^Open menu/:/^Chats(?: \(|$)|Show sidebar/}).first().click();
    const target=workspace.getByRole('button',{name:label,exact:true});
    if(await target.count())await target.click();
    else {await workspace.getByRole('button',{name:'More',exact:true}).click();await page.getByRole('menuitem',{name:label,exact:true}).click();}
   }
   await mark('open Stats directly on content');
   await open('Stats');
   await page.locator('[data-view-shell="Stats"] .metrics').waitFor();
   await checkSingleTitle('Last 30 days');
   if(!phone) {
    await mark('align Settings and data-page titles on the shared top line');
    const statsTitle=await page.getByRole('heading',{level:2,name:'Last 30 days',exact:true}).boundingBox();
    await open('Settings');
    await page.locator('nav[aria-label=Settings]').getByRole('button',{name:/^General/}).filter({visible:true}).first().click();
    const general=page.locator('.st-page-h h1').filter({visible:true});
    await general.waitFor();
    expect((await general.boundingBox()).y===statsTitle.y,'Settings and data-page title text share the exact top line');
    const settingsFrame=await page.locator('.st-page-h').filter({visible:true}).boundingBox();
    await open('Notes');
    await page.getByRole('button',{name:'New note',exact:true}).filter({visible:true}).first().click();
    await page.getByRole('textbox',{name:'Note title',exact:true}).waitFor();
    const noteFrame=await page.locator('.st-dh').filter({visible:true}).boundingBox();
    expect(noteFrame.x===settingsFrame.x&&noteFrame.width===settingsFrame.width,
     'short Notes and long Settings reserve the same centered reading frame '+JSON.stringify({noteFrame,settingsFrame}));
    await mark('preserve real avatar and provider-logo header columns');
    await open('Agents');
    await page.locator('.st-it').filter({visible:true}).first().click();
    const avatarHeader=page.locator('.st-dh').filter({has:page.locator('.st-identity > .agent-avatar')});
    const avatar=await avatarHeader.locator('.st-identity > *').boundingBox();
    const agentTitle=await avatarHeader.locator('h2').boundingBox();
    expect(Math.abs(agentTitle.x-avatar.x-avatar.width-14)<1,'Agent avatar keeps the original 14px title gap '+JSON.stringify({avatar,agentTitle}));
    await open('Settings');
    await page.getByRole('navigation',{name:'Settings',exact:true}).getByRole('button',{name:'Providers',exact:true}).click();
    await page.locator('.pgn').first().click();
    const providerHeader=page.locator('[data-view-shell=Settings] .st-dh').filter({has:page.locator('.st-identity > *')});
    const logo=await providerHeader.locator('.st-identity > *').boundingBox();
    const providerTitle=await providerHeader.locator('h2').boundingBox();
    expect(Math.abs(providerTitle.x-logo.x-logo.width-14)<1,'Provider logo keeps the original 14px title gap '+JSON.stringify({logo,providerTitle}));
    await open('Stats');
    await page.locator('[data-view-shell="Stats"] .metrics').waitFor();
   }
   if(phone) {
    expect(await page.locator('[data-view-shell="Stats"]').getAttribute('data-pane')==='detail','Stats opens directly on KPIs');
    expect(!await page.locator('[data-view-shell="Stats"] .st-back-row').count(),'Stats adds no Back row');
    expect(!await page.getByRole('button',{name:'Usage by provider',exact:true}).count(),'phone has no jump-link menu');
   }
   await mark('change range and keep export available');
   await page.getByRole('button',{name:'7 days',exact:true}).filter({visible:true}).click();
   await page.locator('[data-view-shell="Stats"] .metrics').waitFor();
   expect(await page.getByRole('button',{name:'Export usage by provider as CSV',exact:true}).filter({visible:true}).isEnabled(),'export remains reachable');
   await mark('open Pull requests directly on content');
   await open('Pull requests');
   await page.getByRole('heading',{level:2,name:'Open pull requests',exact:true}).waitFor();
   await checkSingleTitle('Open pull requests');
   if(phone)expect(!await page.locator('[data-view-shell="Pull requests"] .st-back-row').count(),'Pull requests adds no Back row');
   for(const name of ['Merged','Closed','All','Open']) {
    const control=page.getByRole('button',{name,exact:true}).filter({visible:true});
    expect(await control.isVisible(),name+' remains reachable');
    if(phone)expect((await control.boundingBox()).height>=44,name+' has a 44px phone target');
    await control.click();
   }
   await mark('open filters, change one and close the sheet');
   await page.getByRole('button',{name:'Filter pull requests',exact:true}).click();
   await page.getByRole('combobox',{name:'Checks',exact:true}).selectOption('failing');
   await page.getByRole('button',{name:'Done',exact:true}).click();
   expect(await page.getByRole('button',{name:'Filter pull requests, 1 active',exact:true}).isVisible(),'active filter count survives closing the sheet');
   await mark('clear filters through the same sheet');
   await page.getByRole('button',{name:'Filter pull requests, 1 active',exact:true}).click();
   await page.getByRole('button',{name:'Clear filters',exact:true}).click();
   await page.getByRole('button',{name:'Done',exact:true}).click();
   expect(await page.getByRole('button',{name:'Filter pull requests',exact:true}).isVisible(),'filters can be cleared');
  }
 }, {
  name: 'files-actions-and-card',
  async setup({page,status,variant}) {
   const phone=variant.viewport==='phone';
   await gotoApp(page,status,native && phone ? '/mobile/' : '/');
   const workspace=page.getByRole('navigation',{name:'Workspace',exact:true});
   if(!await workspace.isVisible())await page.getByRole('button',{name:native&&phone?/^Open menu/:/^Chats(?: \(|$)|Show sidebar/}).first().click();
   await workspace.getByRole('button',{name:'Files',exact:true}).click();
   await page.getByRole('button',{name:'Workspace',exact:true}).click();
   await page.locator('.fs-row:not(.fs-row--head)').first().waitFor();
  },
  async act({page,mark,variant}) {
   const phone=variant.viewport==='phone';
   await mark('Files keeps an edged card with a folder/file count');
   expect(await page.locator('.fs-main.st-card').count()===1,'F1: file list is a card');
   expect((await page.locator('.fs-footer').innerText()).includes('folders'),'F1: footer counts folders');
   if(phone) {
    const actions=page.locator('.fs-phone-actions');
    const upload=await actions.getByRole('button',{name:'Upload',exact:true}).boundingBox();
    const folder=await actions.getByRole('button',{name:'New folder',exact:true}).boundingBox();
    expect(upload&&folder&&upload.y===folder.y&&Math.abs(upload.width-folder.width)<0.1,'F2: primary file actions share one equal-width row '+JSON.stringify({upload,folder}));
    expect(upload.height>=44&&folder.height>=44,'F2: file actions have 44px targets');
    await actions.getByText('More',{exact:true}).click();
    expect(await actions.getByRole('button',{name:'Open terminal here',exact:true}).isEnabled(),'F2: guarded terminal action remains reachable');
    await actions.getByText('More',{exact:true}).click();
   }
   const create=page.getByRole('button',{name:'New folder',exact:true}).filter({visible:true});
   await mark('new folder keeps its original create/cancel path');
   await create.click();
   await page.getByRole('textbox',{name:'Folder name',exact:true}).fill('cancelled-r9-folder');
   await page.locator('.fs-newfolder').getByRole('button',{name:'Cancel',exact:true}).click();
   expect(!await page.locator('.fs-newfolder').count(),'folder draft can be cancelled');
  }
 }, {
  name: 'worker-rows-and-one-local-remove',
  async setup({page,status,variant}) {
   const phone=variant.viewport==='phone';
   await gotoApp(page,status,native && phone ? '/mobile/' : '/');
   const workspace=page.getByRole('navigation',{name:'Workspace',exact:true});
   if(!await workspace.isVisible())await page.getByRole('button',{name:native&&phone?/^Open menu/:/^Chats(?: \(|$)|Show sidebar/}).first().click();
   await workspace.getByRole('button',{name:'Projects',exact:true}).click();
   await page.getByRole('button',{name:'Open Dev sandbox',exact:true}).click();
   await page.getByRole('tab',{name:'Workers',exact:true}).click();
   await page.locator('article[data-worker-state="ready"]').waitFor();
  },
  async act({page,mark}) {
   await mark('ready worker status follows the standard row rhythm');
   const article=page.locator('article[data-worker-state="ready"]').first();
   const status=article.locator('.st-row').filter({has:page.getByText('Status',{exact:true})});
   const folder=article.locator('.st-row').filter({has:page.getByText('Branch',{exact:true})});
   const s=await status.boundingBox(),f=await folder.boundingBox();
   expect(Math.abs(s.height-f.height)<1.1,'S4: ready Status has normal row height');
   expect(!await article.locator('.st-rowx').count(),'S4: ready Status has no empty expansion');
   await page.getByRole('button',{name:'Remove project and chats',exact:true}).waitFor();
   expect(await page.getByRole('button',{name:'Remove project and chats',exact:true}).count()===1,'S4: one removal for this worker');
   expect(!await article.getByRole('button',{name:/^Remove project and chats from/}).count(),'S4: no duplicate self-worker removal');
   const foot=await page.locator('.st-footnote').boundingBox();
   const card=await article.locator('.st-card').boundingBox();
   expect(Math.abs(foot.y-card.y-card.height-12)<1,'S4: footnote is 12px below its card');
  }
 }]
});
