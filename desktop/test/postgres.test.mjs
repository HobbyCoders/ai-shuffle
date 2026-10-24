import assert from 'node:assert/strict';
import net from 'node:net';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const {
	PostgresSupervisor,
	resolvePostgresBinaries,
	initdbArgs,
	pgCtlStartArgs,
	walCompressionFor,
	pgCtlStopArgs,
	chooseSocketDir,
	parsePidFile,
	planPidFile,
	planCluster,
	pickPort,
	POSTGRES_HOST,
	POSTGRES_USER,
	POSTGRES_DATABASE
} = require('../dist/main/postgres.js');

const ROOT = '/data/postgres';
const DATA = path.join(ROOT, 'data');
const PID_FILE = path.join(DATA, 'postmaster.pid');
const IDENTITY_FILE = path.join(ROOT, 'postmaster-identity.json');
const identityRecord = (pid=4242) => JSON.stringify({schemaVersion:1,dataDir:DATA,startTime:1700000000,identity:{pid,executable:'/opt/pg/bin/postgres',started:`linux:boot:${pid}`,startedAt:1700000000}});
const LOGS = '/data/logs';

const bin = (name) => `/opt/pg/bin/${name}`;
const binaries = { root: '/opt/pg', initdb: bin('initdb'), pgCtl: bin('pg_ctl'), postgres: bin('postgres'), createdb: bin('createdb'), psql: bin('psql'), pgIsReady: bin('pg_isready') };

/**
 * A runtime with an in-memory filesystem and a scripted process table.
 * `files` maps paths to contents; directories are the paths in `dirs`.
 * `results` answers exec calls by binary name (a function or a fixed reply).
 */
function fakeRuntime({ files = {}, dirs = [], alive = [], results = {}, platform = 'linux', freePort = 54321, missingBinaries = [] } = {}) {
	const fs = new Map(Object.entries(files));
	// The whole distribution is present unless a test removes a tool.
	for (const [name, file] of Object.entries(binaries)) {
		if (name !== 'root' && !missingBinaries.includes(name)) fs.set(file, '');
	}
	const dirSet = new Set([binaries.root, ...dirs]);
	const calls = [];
	const rt = {
		platform,
		env: { PATH: '/usr/bin', PGHOST: 'leak', DATABASE_URL: 'leak', POSTGRES_PASSWORD: 'leak', HOME: '/home/u' },
		tmpDir: '/tmp',
		existsSync: (file) => fs.has(file) || dirSet.has(file),
		readFile: async (file) => {
			if (!fs.has(file)) throw new Error(`ENOENT ${file}`);
			return fs.get(file);
		},
		writeFile: async (file, text, mode) => {
			calls.push({ op: 'write', file, mode });
			fs.set(file, text);
		},
		mkdir: async (dir) => {
			dirSet.add(dir);
		},
		remove: async (target) => {
			calls.push({ op: 'remove', target });
			fs.delete(target);
			dirSet.delete(target);
			for (const key of [...fs.keys()]) if (key.startsWith(`${target}/`)) fs.delete(key);
		},
		exec: async (file, args, opts) => {
			calls.push({ op: 'exec', file, args, env: opts?.env, input: opts?.input });
			const name = path.basename(file);
			const reply = results[name];
			const result = typeof reply === 'function' ? reply(args, opts, rt) : reply;
			if (name === 'pg_ctl' && (result?.code ?? 0) === 0 && args.at(-1) === 'start') {
				if (!fs.has(PID_FILE)) pgCtlWritingPid(fs)(args);
				alive.push(parsePidFile(fs.get(PID_FILE)).pid);
			}
			return { code: 0, stdout: '', stderr: '', ...(result ?? {}) };
		},
		pidAlive: (pid) => alive.includes(pid),
		signal: async (identity,signal) => {calls.push({op:'signal',identity,signal});fs.delete(PID_FILE);alive.splice(0);},
		processIdentity: async pid => ({pid,executable:binaries.postgres,started:`linux:boot:${pid}`,startedAt:1700000000}),
		realpath: async file => file,
		saveIdentity: async (file,value) => fs.set(file,JSON.stringify(value)),
		freePort: async (preferred) => {
			calls.push({ op: 'freePort', preferred });
			return preferred === 55555 ? preferred : freePort;
		},
		randomPassword: () => 'generated-secret'
	};
	return { rt, calls, fs, dirs: dirSet, execs: (name) => calls.filter((c) => c.op === 'exec' && path.basename(c.file) === name) };
}

/** pg_ctl start writes the pid file the way the real postmaster does. */
function pgCtlWritingPid(fs, pid = 4242) {
	return (args) => {
		if (args.at(-1) === 'start') fs.set(PID_FILE, `${pid}\n${DATA}\n1700000000\n${args[args.indexOf('-o') + 1].match(/-p (\d+)/)[1]}\n/tmp\n127.0.0.1\n  5432001         0\nready\n`);
		if (args.at(-1) === 'stop') fs.delete(PID_FILE);
		return { code: 0 };
	};
}

function credentialsStore(initial = null) {
	const store = { saved: [], current: initial };
	store.credentials = {
		load: () => store.current,
		save: async (config) => {
			store.saved.push(config);
			store.current = config;
		}
	};
	return store;
}

function supervisor(rt, credentials, extra = {}) {
	return new PostgresSupervisor({ binaries, root: ROOT, logDir: LOGS, credentials, runtime: rt, ...extra });
}

test('update snapshot requires a stopped cluster and a clean control file; restore tolerates crash recovery state', async () => {
 const {rt,execs}=fakeRuntime({files:{[path.join(DATA,'PG_VERSION')]:'16'},results:{pg_ctl:{code:3},pg_controldata:{stdout:'Database cluster state:               shut down\n'}}});
 const pg=supervisor(rt,credentialsStore().credentials);
 await pg.quiesce(true);
 assert.equal(execs('pg_controldata').length,1);
 assert.equal(execs('pg_controldata')[0].env.LC_ALL,'C');
 assert.equal(execs('pg_controldata')[0].env.PGHOST,undefined);
 await pg.quiesce(false); assert.equal(execs('pg_controldata').length,1);
});
test('an update restore point can reap a leftover worker before it stops the cluster for a copy',async()=>{
 const {rt,execs}=fakeRuntime({files:{[path.join(DATA,'PG_VERSION')]:'16'},results:{pg_ctl:{code:3},pg_controldata:{stdout:'Database cluster state:               shut down\n'}}});
 const order=[];const point=supervisor(rt,credentialsStore().credentials).updateRestorePoint(async()=>{order.push(execs('pg_ctl').length);});
 await point.quiesce(true);
 assert.deepEqual(order,[0],'before any pg_ctl call');assert.equal(execs('pg_controldata').length,1);
});
test('update snapshot refuses live PostgreSQL, unclean shutdown and missing control tools',async()=>{
 for(const results of [{pg_ctl:{code:0}},{pg_ctl:{code:3},pg_controldata:{stdout:'Database cluster state: in production'}},{pg_ctl:{code:3},pg_controldata:{code:-2}}]){
  const {rt}=fakeRuntime({files:{[path.join(DATA,'PG_VERSION')]:'16'},results});
  await assert.rejects(supervisor(rt,credentialsStore().credentials).quiesce(true),/PostgreSQL/);
 }
});
test('update quiescence reaps an authenticated PostgreSQL orphan before accepting the cluster',async()=>{
 const {rt,fs,execs,calls}=fakeRuntime({files:{[IDENTITY_FILE]:identityRecord(),[PID_FILE]:`4242\n${DATA}\n1700000000\n54321\n`,[path.join(DATA,'PG_VERSION')]:'16'},alive:[4242],results:{pg_isready:{code:0},pg_ctl:args=>{
  if(args.at(-1)==='stop'){fs.delete(PID_FILE);return {code:0};}return {code:3};
 },pg_controldata:{stdout:'Database cluster state: shut down\n'}}});
 await supervisor(rt,credentialsStore().credentials).quiesce(true);
 assert.equal(execs('pg_ctl').some(call=>call.args.at(-1)==='stop'),false);assert.equal(calls.find(call=>call.op==='signal').signal,'SIGINT');
 assert.equal(fs.has(PID_FILE),false);
});
test('a fresh data directory gets initdb with a generated password, then a start, the database and saved managed settings', async () => {
	const { rt, calls, fs, execs } = fakeRuntime({ results: { pg_ctl: null } });
	rt.exec = ((original) => async (file, args, opts) => {
		if (path.basename(file) === 'pg_ctl') pgCtlWritingPid(fs)(args);
		return original(file, args, opts);
	})(rt.exec);
	const store = credentialsStore();
	const pg = supervisor(rt, store.credentials);
	const phases = [];
	pg.on('state', (s) => phases.push(s.phase));

	const state = await pg.start();
	assert.equal(state.phase, 'ready', state.error);
	assert.equal(state.port, 54321);
	assert.equal(state.pid, 4242);
	assert.deepEqual(phases, ['starting', 'starting', 'ready']);

	const [initdb] = execs('initdb');
	assert.ok(initdb, 'initdb must run for a fresh cluster');
	const pwfile = initdb.args.find((a) => a.startsWith('--pwfile=')).slice('--pwfile='.length);
	assert.deepEqual(initdb.args, ['-D', DATA, '-U', 'ai_shuffle', `--pwfile=${pwfile}`, '--auth=scram-sha-256', '-E', 'UTF8', '--locale=C']);
	const written = calls.find((c) => c.op === 'write' && c.file === pwfile);
	assert.equal(written.mode, 0o600);
	assert.ok(calls.some((c) => c.op === 'remove' && c.target === pwfile), 'the password file is removed after initdb');
	assert.equal(fs.has(pwfile), false);
	assert.equal(Object.keys(initdb.env).some((key) => /^(PG|DATABASE_|POSTGRES_)/i.test(key)), false, 'inherited DB variables must not reach the tools');
	assert.equal(initdb.env.HOME, '/home/u');

	const starts = execs('pg_ctl').filter((c) => c.args.at(-1) === 'start');
	assert.equal(starts.length, 1);
	assert.deepEqual(starts[0].args, ['-D', DATA, '-l', path.join(LOGS, 'postgres.log'), '-w', '-t', '60', '-o', `-p 54321 -c listen_addresses=127.0.0.1 -c max_wal_size=4GB -c checkpoint_timeout=15min -c checkpoint_completion_target=0.9 -c wal_compression=pglz -c statement_timeout=30s -c lock_timeout=5s -c idle_in_transaction_session_timeout=30s -k "${ROOT}"`, 'start']);

	const [createdb] = execs('createdb');
	assert.deepEqual(createdb.args, ['-h', '127.0.0.1', '-p', '54321', '-U', 'ai_shuffle', 'ai_shuffle']);
	assert.equal(createdb.env.PGPASSWORD, 'generated-secret');

	assert.equal(store.saved.length, 1);
	assert.deepEqual(store.saved[0], { host: '127.0.0.1', port: 54321, user: 'ai_shuffle', database: 'ai_shuffle', password: 'generated-secret', managed: true });
	assert.deepEqual(pg.connection(), store.saved[0]);
	assert.ok(pg.tail().some((l) => /creating the database cluster/.test(l)));
});

test('an existing cluster with stored settings skips initdb, keeps its port and does not rewrite the settings', async () => {
	const { fs, rt, execs } = fakeRuntime({ files: { [path.join(DATA, 'PG_VERSION')]: '16\n' }, dirs: [DATA] });
	rt.exec = ((original) => async (file, args, opts) => {
		if (path.basename(file) === 'pg_ctl') pgCtlWritingPid(fs, 77)(args);
		return original(file, args, opts);
	})(rt.exec);
	const stored = { host: '127.0.0.1', port: 55555, user: 'ai_shuffle', database: 'ai_shuffle', password: 'kept', managed: true };
	const store = credentialsStore(stored);
	const pg = supervisor(rt, store.credentials);

	const state = await pg.start();
	assert.equal(state.phase, 'ready', state.error);
	assert.equal(state.port, 55555, 'the previous port is preferred while it is free');
	assert.equal(state.pid, 77);
	assert.equal(execs('initdb').length, 0);
	assert.equal(execs('postgres').length, 0, 'no password reset when the password is known');
	assert.equal(store.saved.length, 0);
	assert.equal(pg.connection().password, 'kept');
	// createdb answering "already exists" is fine.
	assert.equal(execs('createdb').length, 1);
	// #591: an existing cluster gets the session timeouts on every start, without editing postgresql.conf.
	const [start] = execs('pg_ctl').filter((c) => c.args.at(-1) === 'start');
	const options = start.args[start.args.indexOf('-o') + 1].split(' ');
	for (const setting of ['statement_timeout=30s', 'lock_timeout=5s', 'idle_in_transaction_session_timeout=30s']) {
		assert.equal(options[options.indexOf(setting) - 1], '-c', `${setting} is passed as a server option`);
	}
});

test('an existing cluster tolerates a database that already exists and fails on other createdb errors', async () => {
	const base = { files: { [path.join(DATA, 'PG_VERSION')]: '16\n' }, dirs: [DATA] };
	const stored = { host: '127.0.0.1', port: 1, user: 'ai_shuffle', database: 'ai_shuffle', password: 'kept', managed: true };
	{
		const { rt } = fakeRuntime({ ...base, results: { createdb: { code: 1, stderr: 'createdb: error: database creation failed: ERROR:  database "ai_shuffle" already exists' } } });
		assert.equal((await supervisor(rt, credentialsStore(stored).credentials).start()).phase, 'ready');
	}
	{
		const { rt } = fakeRuntime({ ...base, results: { createdb: { code: 1, stderr: 'createdb: error: connection failed: FATAL: password authentication failed' } } });
		const state = await supervisor(rt, credentialsStore(stored).credentials).start();
		assert.equal(state.phase, 'failed');
		assert.match(state.error, /Could not create the ai_shuffle database/);
		assert.match(state.error, /password authentication failed/);
	}
});

test('without createdb the database is created through psql instead', async () => {
	const { rt, execs } = fakeRuntime({ files: { [path.join(DATA, 'PG_VERSION')]: '16\n' }, dirs: [DATA], missingBinaries: ['createdb'] });
	const stored = { host: '127.0.0.1', port: 1, user: 'ai_shuffle', database: 'ai_shuffle', password: 'kept', managed: true };
	const state = await supervisor(rt, credentialsStore(stored).credentials).start();
	assert.equal(state.phase, 'ready', state.error);
	assert.equal(execs('createdb').length, 0);
	const [psql] = execs('psql');
	assert.deepEqual(psql.args, ['-h', '127.0.0.1', '-p', '54321', '-U', 'ai_shuffle', '-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-c', 'CREATE DATABASE "ai_shuffle"']);
	assert.equal(psql.env.PGPASSWORD, 'kept');
});

test('an existing cluster without a known password gets a new one in single-user mode', async () => {
	const { rt, execs } = fakeRuntime({ files: { [path.join(DATA, 'PG_VERSION')]: '16\n' }, dirs: [DATA] });
	const store = credentialsStore(null);
	const state = await supervisor(rt, store.credentials).start();
	assert.equal(state.phase, 'ready', state.error);
	assert.equal(execs('initdb').length, 0, 'never initdb over an existing cluster');
	const [single] = execs('postgres');
	assert.deepEqual(single.args, ['--single', '-D', DATA, 'postgres']);
	assert.equal(single.input, `ALTER ROLE "ai_shuffle" WITH PASSWORD 'generated-secret';\n`);
	assert.equal(store.saved.length, 1);
	assert.equal(store.saved[0].password, 'generated-secret');
	assert.equal(store.saved[0].managed, true);
});

test('an incomplete existing data directory is preserved, never auto-cleaned on restart', async () => {
	const marker = path.join(DATA, 'preserved-data');
	const { rt, calls, execs, fs } = fakeRuntime({ files: { [marker]: 'keep' }, dirs: [DATA] });
	const pg = supervisor(rt, credentialsStore().credentials);
	for (let run = 0; run < 2; run++) {
		const state = await pg.start();
		assert.equal(state.phase, 'failed'); assert.match(state.error, /preserved/);
	}
	assert.equal(fs.get(marker), 'keep');
	assert.equal(calls.some((c) => c.op === 'remove' && c.target === DATA), false);
	assert.equal(execs('initdb').length, 0);
});

test('a stale postmaster.pid whose process is gone is removed before starting', async () => {
	const { rt, calls, execs } = fakeRuntime({
		files: { [path.join(DATA, 'PG_VERSION')]: '16\n', [PID_FILE]: `9999\n${DATA}\n1700000000\n50000\n/tmp\n127.0.0.1\n` },
		dirs: [DATA],
		alive: []
	});
	const stored = { host: '127.0.0.1', port: 50000, user: 'ai_shuffle', database: 'ai_shuffle', password: 'kept', managed: true };
	const state = await supervisor(rt, credentialsStore(stored).credentials).start();
	assert.equal(state.phase, 'ready', state.error);
	assert.ok(calls.some((c) => c.op === 'remove' && c.target === PID_FILE));
	assert.equal(execs('pg_isready').length, 0, 'a dead pid needs no protocol check');
	assert.equal(execs('pg_ctl').filter((c) => c.args.at(-1) === 'stop').length, 0);
});

test('a live pid with matching OS and cluster identity is stopped through pg_ctl', async () => {
	const { rt, fs, calls, execs } = fakeRuntime({
		platform: 'win32',
		files: { [IDENTITY_FILE]:identityRecord(4321), [path.join(DATA, 'PG_VERSION')]: '16\n', [PID_FILE]: `4321\n${DATA}\n1700000000\n50000\n/tmp\n127.0.0.1\n` },
		dirs: [DATA],
		alive: [4321],
		results: { pg_isready: { code: 0 } }
	});
	rt.exec = ((original) => async (file, args, opts) => {
		if (path.basename(file) === 'pg_ctl') pgCtlWritingPid(fs)(args);
		return original(file, args, opts);
	})(rt.exec);
	const stored = { host: '127.0.0.1', port: 50000, user: 'ai_shuffle', database: 'ai_shuffle', password: 'kept', managed: true };
	const state = await supervisor(rt, credentialsStore(stored).credentials).start();
	assert.equal(state.phase, 'ready', state.error);
	assert.deepEqual(execs('pg_isready')[0].args, ['-h', '127.0.0.1', '-p', '50000', '-t', '5']);
	const stops = execs('pg_ctl').filter((c) => c.args.at(-1) === 'stop');
	assert.equal(stops.length, 1);
	assert.deepEqual(stops[0].args, ['-D', DATA, '-m', 'fast', '-w', '-t', '30', 'stop']);
	assert.ok(!calls.some((c) => c.op === 'remove' && c.target === PID_FILE), 'the postmaster removes its own pid file');
});

for(const mismatch of ['start','executable','cluster','pid-time','missing']) test(`a responding PG port cannot authorize stopping a mismatched ${mismatch} identity`,async()=>{
 const saved=JSON.parse(identityRecord(4321));
 const {rt,execs}=fakeRuntime({files:{[IDENTITY_FILE]:identityRecord(4321),[PID_FILE]:`4321\n${mismatch==='cluster'?'/other-cluster':DATA}\n${mismatch==='pid-time'?1700000001:1700000000}\n50000\n`,[path.join(DATA,'PG_VERSION')]:'16'},alive:[4321],results:{pg_isready:{code:0}}});
 if(mismatch==='missing')await rt.remove(IDENTITY_FILE);
 const identity=await rt.processIdentity(4321);rt.processIdentity=async()=>({...identity,...(mismatch==='start'?{started:'reused-pid'}:{}),...(mismatch==='executable'?{executable:'/unrelated/program'}:{})});
 const pg=supervisor(rt,credentialsStore().credentials);await assert.rejects(pg.quiesce(),/identity|cluster/i);
 assert.equal(execs('pg_ctl').filter(c=>c.args.at(-1)==='stop').length,0);
 assert.equal(rt.existsSync(PID_FILE),true);
});
test('a live pid that is not PostgreSQL is left alone and reported', async () => {
	const { rt, calls, execs } = fakeRuntime({
		files: { [path.join(DATA, 'PG_VERSION')]: '16\n', [PID_FILE]: `4321\n${DATA}\n1700000000\n50000\n/tmp\n127.0.0.1\n` },
		dirs: [DATA],
		alive: [4321],
		results: { pg_isready: { code: 2 } }
	});
	const stored = { host: '127.0.0.1', port: 50000, user: 'ai_shuffle', database: 'ai_shuffle', password: 'kept', managed: true };
	const pg = supervisor(rt, credentialsStore(stored).credentials);
	const state = await pg.start();
	assert.equal(state.phase, 'failed');
	assert.match(state.error, /Another process \(4321\) holds/);
	assert.ok(!calls.some((c) => c.op === 'remove' && c.target === PID_FILE));
	assert.equal(execs('pg_ctl').length, 0, 'nothing is signalled');
	assert.equal(execs('initdb').length, 0);
	// Quitting afterwards must not hand that pid file to pg_ctl stop either.
	await pg.stop();
	assert.equal(pg.state.phase, 'stopped');
	assert.equal(execs('pg_ctl').length, 0, 'nothing is signalled on quit');
});

test('a failed pg_ctl start reports the server log tail and stop cleans up', async () => {
	const logPath = path.join(LOGS, 'postgres.log');
	const { rt, fs, execs } = fakeRuntime({
		files: { [path.join(DATA, 'PG_VERSION')]: '16\n', [logPath]: 'LOG:  starting\nFATAL:  could not create lock file\n' },
		dirs: [DATA],
		results: {
			pg_ctl: (args) => {
				if (args.at(-1) === 'start') {
					fs.set(PID_FILE, '31\n');
					return { code: 1, stderr: 'pg_ctl: could not start server' };
				}
				if (args.at(-1) === 'stop') fs.delete(PID_FILE);
				return { code: 0 };
			}
		}
	});
	const stored = { host: '127.0.0.1', port: 1, user: 'ai_shuffle', database: 'ai_shuffle', password: 'kept', managed: true };
	const pg = supervisor(rt, credentialsStore(stored).credentials);
	const state = await pg.start();
	assert.equal(state.phase, 'failed');
	assert.match(state.error, /PostgreSQL did not start \(pg_ctl exit code 1\)/);
	assert.match(state.error, /could not create lock file/);
	assert.equal(pg.connection(), null);
	await pg.stop();
	assert.equal(pg.state.phase, 'stopped');
	assert.equal(execs('pg_ctl').filter((c) => c.args.at(-1) === 'stop').length, 0, 'an unverified PID from a failed start is never signalled');
	assert.equal(fs.has(PID_FILE), false, 'a dead PID file is removed without signalling');
});

test('Windows stop is a no-op before the first start and uses fast shutdown afterwards', async () => {
	const { rt, fs, execs } = fakeRuntime({platform:'win32'});
	rt.exec = ((original) => async (file, args, opts) => {
		if (path.basename(file) === 'pg_ctl') pgCtlWritingPid(fs)(args);
		return original(file, args, opts);
	})(rt.exec);
	const pg = supervisor(rt, credentialsStore().credentials);
	await pg.stop();
	assert.equal(pg.state.phase, 'idle');
	assert.equal(execs('pg_ctl').length, 0);
	await pg.start();
	await pg.stop();
	assert.equal(pg.state.phase, 'stopped');
	assert.equal(pg.state.pid, null);
	assert.equal(pg.connection(), null);
	assert.deepEqual(execs('pg_ctl').at(-1).args, pgCtlStopArgs(DATA));
	// A start after a stop is a full start again.
	assert.equal((await pg.start()).phase, 'ready');
});

test('start while ready only checks the status when the server still runs', async () => {
	const { rt, fs, execs } = fakeRuntime({ results: { pg_ctl: null } });
	rt.exec = ((original) => async (file, args, opts) => {
		if (path.basename(file) === 'pg_ctl') pgCtlWritingPid(fs)(args);
		return original(file, args, opts);
	})(rt.exec);
	const pg = supervisor(rt, credentialsStore().credentials);
	await pg.start();
	const before = execs('pg_ctl').length;
	await pg.start();
	const after = execs('pg_ctl');
	assert.equal(after.length, before + 1);
	assert.deepEqual(after.at(-1).args, ['-D', DATA, 'status']);
	assert.equal(execs('initdb').length, 1);
});

test('pure helpers: pg_ctl arguments, socket directory, pid file, plans', () => {
	assert.deepEqual(initdbArgs({ dataDir: 'D', pwfile: 'P' }), ['-D', 'D', '-U', POSTGRES_USER, '--pwfile=P', '--auth=scram-sha-256', '-E', 'UTF8', '--locale=C']);
	assert.deepEqual(pgCtlStartArgs({ dataDir: 'D', logPath: 'L', port: 5000, socketDir: null }), ['-D', 'D', '-l', 'L', '-w', '-t', '60', '-o', '-p 5000 -c listen_addresses=127.0.0.1 -c max_wal_size=4GB -c checkpoint_timeout=15min -c checkpoint_completion_target=0.9 -c wal_compression=pglz -c statement_timeout=30s -c lock_timeout=5s -c idle_in_transaction_session_timeout=30s', 'start']);
	assert.deepEqual(pgCtlStartArgs({ dataDir: 'D', logPath: 'L', port: 5000, socketDir: '/a b', timeoutSec: 9, walCompression: 'lz4' }), ['-D', 'D', '-l', 'L', '-w', '-t', '9', '-o', '-p 5000 -c listen_addresses=127.0.0.1 -c max_wal_size=4GB -c checkpoint_timeout=15min -c checkpoint_completion_target=0.9 -c wal_compression=lz4 -c statement_timeout=30s -c lock_timeout=5s -c idle_in_transaction_session_timeout=30s -k "/a b"', 'start']);
	assert.equal(walCompressionFor('win32'), 'lz4', 'the Windows bundle is built with lz4');
	assert.equal(walCompressionFor('darwin'), 'pglz', 'unverified bundles get the codec every build accepts');
	assert.equal(walCompressionFor('linux'), 'pglz');
	assert.deepEqual(pgCtlStopArgs('D'), ['-D', 'D', '-m', 'fast', '-w', '-t', '30', 'stop']);

	assert.equal(chooseSocketDir('win32', 'C:\\x', 'C:\\tmp'), null);
	assert.equal(chooseSocketDir('darwin', '/Users/me/Library/Application Support/AI Shuffle/postgres', '/var/tmp'), '/Users/me/Library/Application Support/AI Shuffle/postgres');
	assert.equal(chooseSocketDir('linux', `/${'x'.repeat(120)}`, '/tmp'), '/tmp');

	assert.deepEqual(parsePidFile('123\n/d\n1\n5433\n/tmp\n127.0.0.1\n'), { pid: 123, dataDir: '/d', startTime: 1, port: 5433 });
	assert.deepEqual(parsePidFile('123\r\n'), { pid: 123, dataDir: null, startTime: null, port: null });
	assert.equal(parsePidFile(''), null);
	assert.equal(parsePidFile('abc\n'), null);

	assert.equal(planPidFile(null, false, false), 'remove');
	assert.equal(planPidFile({ pid: 1, dataDir: null, port: 1 }, false, true), 'remove');
	assert.equal(planPidFile({ pid: 1, dataDir: null, port: 1 }, true, true), 'stop');
	assert.equal(planPidFile({ pid: 1, dataDir: null, port: 1 }, true, false), 'fail');

	assert.equal(planCluster(false, null), 'initdb');
	assert.equal(planCluster(false, { password: 'x' }), 'initdb');
	assert.equal(planCluster(true, null), 'reset-password');
	assert.equal(planCluster(true, { password: '' }), 'reset-password');
	assert.equal(planCluster(true, { password: 'x' }), 'reuse');
	assert.equal(POSTGRES_HOST, '127.0.0.1');
	assert.equal(POSTGRES_DATABASE, 'ai_shuffle');
});

test('pickPort prefers a free stored port and never hands out a bound one', async () => {
	const server = net.createServer();
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	const bound = server.address().port;
	try {
		const chosen = await pickPort(bound);
		assert.notEqual(chosen, bound);
		assert.ok(chosen > 0 && chosen < 65536);
	} finally {
		await new Promise((resolve) => server.close(resolve));
	}
	assert.equal(await pickPort(bound), bound);
	const ephemeral = await pickPort(null);
	assert.ok(ephemeral > 1024);
});

test('resolvePostgresBinaries finds the bundle, the development override, and explains what is missing', () => {
	const root = path.join('/res', 'postgres');
	const present = new Set(['initdb', 'pg_ctl', 'postgres'].map(name => path.join(root, 'bin', name)));
	const existsSync = (file) => present.has(file);
	const bundled = resolvePostgresBinaries({ packaged: true, resourcesPath: '/res', platform: 'linux', env: {}, existsSync });
	assert.equal(bundled.root, root);
	assert.equal(bundled.initdb, path.join(root, 'bin', 'initdb'));
	assert.equal(bundled.pgCtl, path.join(root, 'bin', 'pg_ctl'));
	assert.equal(bundled.createdb, path.join(root, 'bin', 'createdb'));

	assert.match(resolvePostgresBinaries({ packaged: false, resourcesPath: '/res', platform: 'linux', env: {}, existsSync }).error, /SHUFFLE_DESKTOP_POSTGRES_DIR/);
	assert.match(resolvePostgresBinaries({ packaged: false, resourcesPath: '/res', platform: 'linux', env: { SHUFFLE_DESKTOP_POSTGRES_DIR: '/dev/pg' }, existsSync }).error, /No PostgreSQL initdb at [\\/]dev[\\/]pg[\\/]bin[\\/]initdb/);
	const dev = resolvePostgresBinaries({ packaged: false, resourcesPath: '/res', platform: 'linux', env: { SHUFFLE_DESKTOP_POSTGRES_DIR: '/res/postgres' }, existsSync });
	assert.equal(dev.root, '/res/postgres');

	present.delete(path.join(root, 'bin', 'postgres'));
	assert.match(resolvePostgresBinaries({ packaged: true, resourcesPath: '/res', platform: 'linux', env: {}, existsSync }).error, /bundled PostgreSQL is missing.*postgres\)/);

	const win = new Set(['initdb.exe', 'pg_ctl.exe', 'postgres.exe'].map((name) => path.join('C:\\res', 'postgres', 'bin', name)));
	const onWindows = resolvePostgresBinaries({ packaged: true, resourcesPath: 'C:\\res', platform: 'win32', env: {}, existsSync: (f) => win.has(f) });
	assert.equal(onWindows.initdb, path.join('C:\\res', 'postgres', 'bin', 'initdb.exe'), onWindows.error);
	assert.equal(onWindows.pgIsReady, path.join('C:\\res', 'postgres', 'bin', 'pg_isready.exe'));
});

test('generated supervisor connection is handed off atomically BEFORE worker spawn, never via env or config', async () => {
	const { mkdtemp, readFile, rm, writeFile } = await import('node:fs/promises');
	const { readFileSync } = await import('node:fs');
	const { tmpdir } = await import('node:os');
	const { EventEmitter } = await import('node:events');
	const vm = await import('node:vm');
	const { fs, rt, execs } = fakeRuntime();
	rt.exec = ((original) => async (file, args, opts) => {
		if (path.basename(file) === 'pg_ctl') pgCtlWritingPid(fs)(args);
		return original(file, args, opts);
	})(rt.exec);
	const store = credentialsStore(); const pg = supervisor(rt, store.credentials);
	assert.equal((await pg.start()).phase, 'ready');
	assert.equal(execs('initdb').length, 1, 'generated through real supervisor startup decisions');
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-handoff-'));
	const envFile = path.join(dir, 'worker.env'); const descriptor = path.join(dir, 'managed-postgres.json');
	const codeFile = new URL('../dist/main/worker.js', import.meta.url); const localRequire = createRequire(codeFile);
	const module = { exports: {} }; let spawns = 0;
	const child = new EventEmitter(); child.pid = 424242; child.exitCode = null; child.signalCode = null; child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
	const replacements = {
		'./process-identity': { readProcessIdentity: async pid => ({pid,executable:'/fake/python',started:'fixture-child',startedAt:1700000000}) },
		'node:child_process': { spawn: (_file, _args, options) => {
			spawns++;
			const actual = JSON.parse(readFileSync(descriptor, 'utf8'));
			const { managed, ...generated } = pg.connection();
			assert.deepEqual(actual, { version: 1, ...generated });
			assert.equal(Object.keys(options.env).some((key) => /^(DATABASE_|POSTGRES_|PG)/i.test(key)), false);
			assert.equal(readFileSync(envFile, 'utf8'), 'MODEL=kept\n');
			return child;
		} },
		'node:net': { createServer: () => ({ once() {}, listen(_options, callback) { callback(); }, close(callback) { callback(); } }) },
		'node:http': { request: (_options, callback) => {
			const request = new EventEmitter(); request.destroy = () => {};
			request.end = () => queueMicrotask(() => {
				const response = new EventEmitter(); response.statusCode = 200; response.setEncoding = () => {};
				callback(response); response.emit('data', JSON.stringify({ status: 'synced' })); response.emit('end');
			});
			return request;
		}, get: (_options, callback) => {
			const request = new EventEmitter(); request.destroy = () => {};
			queueMicrotask(() => {
				const response = new EventEmitter(); response.statusCode = 200; response.setEncoding = () => {};
				callback(response); response.emit('data', JSON.stringify({ service: 'ai-shuffle' })); response.emit('end');
			});
			return request;
		} }
	};
	// worker.js takes its port and health probes from worker-process.js; give it the same fakes.
	const processFile = new URL('../dist/main/worker-process.js', import.meta.url); const processModule = { exports: {} };
	vm.runInNewContext(readFileSync(processFile, 'utf8'), { module: processModule, exports: processModule.exports, Buffer, process, setTimeout, clearTimeout, console, require: (id) => id in replacements ? replacements[id] : createRequire(processFile)(id) });
	replacements['./worker-process'] = processModule.exports;
	vm.runInNewContext(readFileSync(codeFile, 'utf8'), { module, exports: module.exports, Buffer, process, setTimeout, clearTimeout, console, require: (id) => id in replacements ? replacements[id] : localRequire(id) });
	try {
		await writeFile(envFile, 'MODEL=kept\nPOSTGRES_PASSWORD=old-test\nDATABASE_URL=old-test\n'.replaceAll('\\n', '\n'));
		const worker = new module.exports.WorkerSupervisor({ runtime: { mode: 'bundled', root: dir, python: '/fake/python', serverDir: dir, nodeBinDir: null, toolsDir: dir, browserMcpEntry: 'unused' }, logDir: dir, envFile, dataDir: dir, postgres: () => pg.connection(), allowAttach: false });
		assert.equal((await worker.start()).phase, 'ready'); assert.equal(spawns, 1);
		// Simulate an actual child exit before cleanup; no process tree is killed in this test.
		child.exitCode = 0; await worker.stop(); child.emit('exit', 0); await Promise.resolve();
		await pg.stop(); await worker.clearConnectionFile();
		await assert.rejects(readFile(descriptor), { code: 'ENOENT' });
		assert.equal(store.current.password, 'generated-secret', 'restart custody remains');
		assert.equal(fs.has(path.join(DATA, 'PG_VERSION')), false, 'fake runtime does not pretend to be a real database');
	} finally { await pg.stop(); await rm(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }); }
});
