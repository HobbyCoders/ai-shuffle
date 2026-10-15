/** Explicit managed-PostgreSQL acceptance: SHUFFLE_TEST_POSTGRES_DIR=<distribution> node --test test/helpers/database-restore.integration.mjs */
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { spawn, execFileSync } from 'node:child_process';
import { generateKeyPairSync, sign } from 'node:crypto';
import { createRequire } from 'node:module';
import { afterRename } from './crash-rename.mjs';
const require = createRequire(import.meta.url);
const { PostgresSupervisor, resolvePostgresBinaries } = require('../../dist/main/postgres.js');
const { SourceUpdates } = require('../../dist/main/source-updates.js');
const { durableJson } = require('../../dist/main/database-restore.js');
const A='a'.repeat(40), B='b'.repeat(40), KEY='c'.repeat(64);
async function fixture(t) {
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-pg-rollback-'));
 const binaries=resolvePostgresBinaries({packaged:false,env:{SHUFFLE_DESKTOP_POSTGRES_DIR:process.env.SHUFFLE_TEST_POSTGRES_DIR}});
 assert.ok(!('error' in binaries),binaries.error);
 let credentials=null;
 const store={load:()=>credentials,save:async config=>{credentials=config;}};
 const pg=new PostgresSupervisor({root:path.join(root,'postgres'),logDir:path.join(root,'logs'),binaries,credentials:store});
 t.after(async()=>{await pg.quiesce();await fs.rm(root,{recursive:true,force:true});});
 assert.equal((await pg.start()).phase,'ready',pg.state.error);
 const sqlEnv=()=>({ ...process.env,PGHOST:credentials.host,PGPORT:String(credentials.port),PGUSER:credentials.user,PGPASSWORD:credentials.password,PGDATABASE:credentials.database });
 const sql=query=>execFileSync(binaries.psql,['-X','-A','-t','-v','ON_ERROR_STOP=1','-c',query],{env:sqlEnv(),encoding:'utf8'}).trim();
 sql("CREATE TABLE rollback_probe (value text NOT NULL); INSERT INTO rollback_probe VALUES ('before update')");
 const {privateKey,publicKey}=generateKeyPairSync('ed25519');
 const options={root:path.join(root,'updates'),publicKey:publicKey.export({type:'spki',format:'pem'}),bootstrapNode:process.execPath};
 async function install(revision,sequence) {
  const dir=path.join(options.root,'versions',revision), resources=path.join(options.root,'runtimes',KEY);
  const electron=path.join(options.root,'dependencies',KEY,'electron');
  const files=[path.join(dir,'source/desktop/dist/main/managed-entry.js'),path.join(dir,'server/app/main.py'),path.join(dir,'server/app/static/index.html'),path.join(resources,process.platform==='win32'?'worker/python/python.exe':'worker/python/bin/python3'),electron];
  for(const file of files){await fs.mkdir(path.dirname(file),{recursive:true});await fs.writeFile(file,'fixture');}
  const payload=Buffer.from(JSON.stringify({format:2,revision,sequence,sha256:KEY,size:1,version:revision===A?'1.0.0':'1.1.0'}));
  await fs.writeFile(path.join(dir,'release.json'),JSON.stringify({payload:payload.toString('base64'),signature:sign(null,payload,privateKey).toString('base64')}));
  await fs.writeFile(path.join(dir,'build.json'),JSON.stringify({revision,pythonKey:null,electron,runtimeKey:KEY}));
  const worker=revision===A
    ? `console.log('previous-build');`
    : `require('node:child_process').execFileSync(process.env.TEST_PSQL,['-X','-v','ON_ERROR_STOP=1','-c',"ALTER TABLE rollback_probe ADD COLUMN migrated integer; UPDATE rollback_probe SET value='after update', migrated=1"],{stdio:'ignore'}); console.log('migrated'); if(process.env.TEST_FAILURE==='crash')process.exit(42); else setInterval(()=>{},1000);`;
  await fs.writeFile(path.join(dir,'server/worker.cjs'),worker);
 }
 await install(A,100);
 await durableJson(path.join(options.root,'state.json'),{schemaVersion:1,pending:A});
 let updates=new SourceUpdates(options);await updates.load();await updates.select();await updates.databaseStarting();await updates.healthy();
 await install(B,200);
 const state=JSON.parse(await fs.readFile(path.join(options.root,'state.json')));state.pending=B;
 await durableJson(path.join(options.root,'state.json'),state);
 updates=new SourceUpdates(options);await updates.load();
 return {root,pg,sql,sqlEnv,binaries,options,updates};
}
for(const failure of ['crash','readiness-timeout','power-loss-during-restore']) test(`real PostgreSQL: ${failure} restores schema, rows, floor and previous executable`,{timeout:600_000},async t=>{
 const f=await fixture(t), database=async()=>f.pg.updateRestorePoint();
 await f.updates.prepareRollback(database);
 assert.equal(f.pg.state.phase,'stopped');
 const selected=await f.updates.select();assert.equal(selected.revision,B);
 await f.updates.requireRestorePoint();await f.updates.databaseStarting();
 assert.equal((await f.pg.start()).phase,'ready',f.pg.state.error);
 const child=spawn(process.execPath,[path.join(selected.serverDir,'worker.cjs')],{env:{...f.sqlEnv(),TEST_PSQL:f.binaries.psql,TEST_FAILURE:failure==='readiness-timeout'?'timeout':'crash'},stdio:['ignore','pipe','pipe']});
 t.after(()=>{if(child.exitCode===null)child.kill('SIGKILL');});
 const exit=new Promise(resolve=>child.once('exit',code=>resolve(code)));
 await new Promise((resolve,reject)=>{child.stdout.once('data',resolve);child.once('error',reject);child.once('exit',code=>{if(code!==42)reject(Error('migration worker exited early'));});});
 assert.equal(f.sql('SELECT value FROM rollback_probe'),'after update');
 if(failure==='readiness-timeout') child.kill('SIGKILL');
 await exit;
 if(failure!=='power-loss-during-restore')await f.updates.failed();
 // New manager is a launcher restart: load notices an unconfirmed boot after a power loss.
 let restart=new SourceUpdates(f.options);await restart.load();
 if(failure==='power-loss-during-restore'){
  const undo=afterRename(async(from)=>{if(from===path.join(f.root,'postgres/data'))throw Error('power loss');});
  try{await assert.rejects(restart.recoverRollback(database),/power loss/);}finally{undo();}
  assert.equal(JSON.parse(await fs.readFile(path.join(f.options.root,'database-floor.json'))).revision,B);
  restart=new SourceUpdates(f.options);await restart.load();
 }
 await restart.recoverRollback(database);
 const previous=await restart.select();assert.equal(previous.revision,A);
 assert.equal(execFileSync(process.execPath,[path.join(previous.serverDir,'worker.cjs')],{encoding:'utf8'}).trim(),'previous-build');
 assert.equal((await f.pg.start()).phase,'ready',f.pg.state.error);
 assert.equal(f.sql('SELECT value FROM rollback_probe'),'before update');
 assert.equal(f.sql("SELECT count(*) FROM information_schema.columns WHERE table_name='rollback_probe' AND column_name='migrated'"),'0');
 assert.equal(JSON.parse(await fs.readFile(path.join(f.options.root,'database-floor.json'))).revision,A);
 assert.equal(restart.hasMigrationRisk,false);
 await assert.rejects(fs.stat(path.join(f.root,'postgres/update-snapshot')),{code:'ENOENT'});
});
