// Files + the real plugin installer -> command API -> composer -> harness -> provider.
// Only the devloop's disposable home is populated; no command responses are mocked.
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { runJourney } from './runner.mjs';
import { PROJECT, composer, expect, openNewChat, pick, scriptTag, sendButton, typeAndSend, uniq, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const checkout = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const produce = String.raw`
import json, sys
from pathlib import Path
from app.core.plugin_service import PluginService
scratch, name = Path(sys.argv[1]), sys.argv[2]
assert (scratch / '.branch-preview').is_file()
assert Path.home() == scratch / 'home'
svc = PluginService()
assert svc.FLUID_DIR == scratch / 'home' / '.fluid'
def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
body = 'Fluid journey expanded $1 / $2. $ARGUMENTS'
write(svc.FLUID_DIR / 'commands' / (name + '.md'), '---\ndescription: Journey direct command\n---\n' + body)
write(svc.FLUID_DIR / 'skills' / (name + '-skill') / 'SKILL.md', '---\ndescription: Journey direct skill\n---\n' + body)
market = svc.MARKETPLACES_DIR / name
package = market / name
write(package / '.claude-plugin' / 'plugin.json', json.dumps({'name': name, 'version': '1.0.0'}))
write(package / 'commands' / 'run.md', '---\ndescription: Journey installed command\n---\n' + body)
write(package / 'skills' / 'guide' / 'SKILL.md', '---\ndescription: Journey installed skill\n---\n' + body)
write(market / '.claude-plugin' / 'marketplace.json', json.dumps({'name': name, 'plugins': [{'name': name, 'source': './' + name}]}))
known = svc._read_json_file(svc.MARKETPLACES_FILE, {})
known[name] = {'installLocation': str(market)}
svc._write_json_file(svc.MARKETPLACES_FILE, known)
installed = svc.install_plugin(name + '@' + name, enable=False)
assert not svc.get_enabled_plugins().get(installed.id)
assert svc.enable_plugin(installed.id)
assert svc.get_enabled_plugins()[installed.id]
`;

function seed(status, name) {
	const scratch = status.scratch_dir;
	expect(existsSync(join(scratch, '.branch-preview')), 'producer only runs in a disposable devloop');
	execFileSync(process.env.PYTHON || 'python3', ['-c', produce, scratch, name], {
		cwd: checkout,
		env: { PATH: process.env.PATH, HOME: join(scratch, 'home'), DATA_DIR: status.data_dir,
			LOCAL_MODE: 'true', AI_SHUFFLE_PREVIEW: '1', AI_SHUFFLE_ENV_FILE: join(scratch, 'empty.env'),
			PYTHONPATH: `${checkout}:${join(checkout, 'fluid')}` },
		stdio: 'pipe'
	});
}

function requests(path) {
	return existsSync(path) ? readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [];
}

await runJourney({
	name: 'fluid-commands',
	allow: DEV_INSTANCE_NOISE,
	cases: ['native', 'delegated'].flatMap(runtime => [{
		name: `${runtime}-code`,
		async setup(ctx) {
			const name = `fj${uniq()}`;
			seed(ctx.status, name);
			const hero = await openNewChat(ctx, { project: PROJECT });
			if (runtime === 'delegated') {
				const menu = ctx.page.getByRole('menu', { name: 'Agent selection' });
				await pick(hero.getByRole('button', { name: /^Agent:/ }), menu,
					menu.getByRole('button', { name: 'Dev (Claude Code)', exact: true }),
					hero.getByRole('button', { name: 'Agent: Dev (Claude Code)' }));
			}
			ctx.state = { name };
		},
		async act({ page, status, variant, mark, log, state: { name } }) {
			for (const [index, command] of [name, `${name}-skill`, `${name}:run`, `${name}:guide`].entries()) {
				await mark(`find and select /${command}`);
				const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/commands/' && r.ok());
				await composer(page).fill(`/${command}`);
				const catalogResponse = await response;
				const catalog = await catalogResponse.json();
				expect(catalog.commands.some(c => c.name === command), `${runtime} catalog contains /${command}`);
				const option = page.getByRole('listbox', { name: 'Commands' }).getByRole('button', { name: new RegExp(`^/${command}(?:\\s|$)`) });
				await option.waitFor();
				await option.click();
				expect((await composer(page).innerText()).trim() === `/${command}`, 'selection keeps the slash invocation');
				// Reuse the actual composer context (including the real, not recorder-hashed,
				// session id after the first send) for the details endpoint.
				const query = new URL(catalogResponse.url()).searchParams.toString();
				const detail = await page.request.get(new URL(`/api/v1/commands/${encodeURIComponent(command)}?${query}`, status.urls.ui).toString());
				expect(detail.ok() && (await detail.json()).content.includes('Fluid journey expanded $1'), 'real detail endpoint returns the discovered body');
				const path = runtime === 'native' ? status.fake_ai.request_log : status.claude_cli.log;
				const offset = requests(path).length;
				await mark(`invoke /${command}`);
				await composer(page).fill(`/${command} "two words" release ${scriptTag(variant, 'default')}`);
				await sendButton(page).click();
				await waitTurnSettled(page, { text: 'You said:', messagesAtLeast: 2 * (index + 1) });
				const sent = requests(path).slice(offset).filter(r => runtime === 'native' ? r.method === 'POST' : r.kind === 'turn');
				expect(sent.some(r => r.prompt.includes('Fluid journey expanded two words / release.')), 'provider receives expanded body and quoted arguments, not an unknown slash command');
				log(`${runtime}: /${command} selected, detail loaded, expanded prompt reached provider`);
			}
		}
	}, {
		name: `${runtime}-chat`,
		async setup(ctx) {
			const name = `fj${uniq()}`;
			seed(ctx.status, name);
			const hero = await openNewChat(ctx);
			if (runtime === 'delegated') {
				const menu = ctx.page.getByRole('menu', { name: 'Agent selection' });
				await pick(hero.getByRole('button', { name: /^Agent:/ }), menu,
					menu.getByRole('button', { name: 'Dev (Claude Code)', exact: true }),
					hero.getByRole('button', { name: 'Agent: Dev (Claude Code)' }));
			}
			ctx.state = { name };
		},
		async act({ page, status, variant, mark, log, state: { name } }) {
			for (const persisted of [false, true]) {
				await mark(`Chat excludes file commands ${persisted ? 'after' : 'before'} session creation`);
				const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/commands/' && r.ok());
				await composer(page).fill(`/${name}`);
				const catalogResponse = await response;
				const catalog = await catalogResponse.json();
				expect(!catalog.commands.some(c => c.name.startsWith(name)), 'Chat does not advertise isolated Fluid files or installed plugin entries');
				await page.getByText('No matching commands', { exact: true }).waitFor();
				const query = new URL(catalogResponse.url()).searchParams;
				if (persisted) query.set('mode', 'code'); // A query cannot escape a saved Chat's scopes.
				for (const command of [name, `${name}-skill`, `${name}:run`, `${name}:guide`]) {
					const detail = await page.request.get(new URL(`/api/v1/commands/${encodeURIComponent(command)}?${query}`, status.urls.ui).toString());
					expect(detail.status() === 404, `Chat detail also excludes /${command}`);
				}
				await composer(page).fill('');
				if (!persisted) {
					await mark('create a plain Chat');
					await typeAndSend(page, `Check Chat isolation ${scriptTag(variant, 'default')}`);
					await waitTurnSettled(page, { text: 'You said:' });
				}
			}
			log(`${runtime}: Chat hides file/plugin commands before and after creation, including details with a mode override`);
		}
	}])
});
