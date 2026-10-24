<!--
title: Subagents
description: Let an agent hand work to other agents, in the background or on another worker
order: 4
-->

# Subagents

An agent can hand parts of a job to other agents, called subagents. A subagent works on its own task, in the background, and reports back to the chat that started it. This lets one chat run several pieces of work at once.

Subagents are ordinary [agents](/docs/features/profiles). There is nothing separate to set up. An agent's **Subagents** section decides which other agents it may call: all of them, only the ones you select, or none.

## Using subagents

Ask in plain words, for example "have a reviewer check this change while you write the tests". The agent starts the subagents it needs.

- Each subagent shows a card in the chat with its progress. **Open transcript** shows everything it did, and you can message it from there.
- The chat's background tasks list shows what is running. You can stop any subagent from there.
- When a subagent finishes, the chat that started it gets its report and carries on.
- An agent can also wait for a subagent's report before it carries on, but only for a limited time (15 minutes by default). A subagent still working then keeps running in the background and reports back the same way.
- Approval prompts from a subagent appear in the chat that started it.

A subagent working on code gets its own copy of the project on its own branch, so it never edits the files you are working on. Its commits come back for you or the main agent to review.

## Checkout setup and review commits

Local coding workers receive a receipt with their actual base commit, an existing Python interpreter (the project's `.venv`/`venv`, otherwise the worker interpreter), and ready-to-run `Monitor` inputs for `npm ci` in each checkout-local package folder with a lockfile. SvelteKit packages also run `npx svelte-kit sync`. Dependencies and credentials are never copied or linked. An interpreter's presence is not proof that every test dependency is installed; do not install packages into the parent's environment.

For automatic setup, the project owner can opt in once in that repository's **local Git config**:

```sh
git config --local fluid.workerSetupCommand 'python -m venv .venv && .venv/bin/python -m pip install -r requirements.txt'
```

Use the project's documented provisioning command instead where appropriate (including its development requirements). The example uses POSIX paths; on Windows use the project's shell-compatible setup command and `.venv/Scripts/python.exe`. `fluid.workerPython` may record an absolute existing interpreter path for the receipt. These settings are local to the repository, not accepted from tracked files or the task brief. `fluid.workerSetupCommand` is trusted local config: setting it authorizes running the command and the install hooks of the chosen commit (including an explicit review commit) in future local worker checkouts. Enable it only for trusted code. Remove the opt-in with `git config --local --unset fluid.workerSetupCommand`.

Setup runs in that worker's own checkout as a tracked Monitor task with a ten-minute maximum, not a minutes-long dispatch wait. The receipt says whether setup is configured. Setup starts only after the initial assignment and its full restrictions are saved; find its task ID in TaskList or the completion/failure Monitor wake. Failed or cancelled initial submission does not start setup. Wait for completion before using the environment or starting another install. Automatic setup is skipped when the agent's shell tools are restricted. Delegated worker sessions with Bash also receive Monitor and its task-management tools unless explicitly blocked; non-worker sessions and file-read-only agents keep their existing tools. Tool availability does not pre-approve shell commands.

For a local review of a different commit, the delegating agent can supply Task's `base_commit`: a full commit SHA already fetched into the caller's repository. The host validates it and checks the resulting checkout SHA before launching. It never guesses a SHA from prose. Without it, the host briefly fetches the default origin branch; a successful fetch is reused for ten seconds across worktrees of the same repository. A caller that is only behind forks from that fresh tip; a caller with unique commits keeps its committed HEAD, with the distance behind origin noted in the receipt. A failed or timed-out fetch keeps the caller's HEAD and is noted too. New chat branches also fetch their base first, keeping the cached default branch when offline. Fetches stop interactive credential prompts and kill helper processes on timeout, including on Windows. Git older than 2.29 uses compatible fetch flags (which may update `FETCH_HEAD`), with a one-time compatibility warning. Remote workers supporting this policy make the same decision with their own fetch; older peers retain caller-HEAD behavior. The explicit override is not supported with a remote `worker` request.

## Choosing the model

A subagent runs on the model set in its agent's **Models** section. If that is empty, it uses your subagent default. The first time an agent starts a subagent with no default set, AI Shuffle asks **Which model should subagents use?** Your answer becomes the default, which you can change on the **Agents** page.

## Models the user asks for

You can ask for a specific model, for example "run the reviewer on a faster model". The agent then starts that subagent on the model you named.

An agent cannot switch to a model on its own. If it wants a model you did not name, AI Shuffle asks you first, in every permission mode. A yes covers that model for the rest of the chat. Unknown model names are refused with a list of the models you can use, and nothing falls back to another model.

## After a restart

Background subagents stop when AI Shuffle restarts. On the next start, the chat that started them gets a note listing what was interrupted. You can ask the agent to continue an interrupted subagent, and it picks up from where it stopped.

## Running a subagent on another worker

When your account has more than one worker in its [worker network](/docs/features/worker-network), a subagent can run on another worker. Ask for it in plain words, for example "run this on my desktop". The subagent still belongs to the chat that started it: it reports back there, and you can message it and stop it from there.

### What it needs

- Both workers online and on the current release.
- Normally, the project checked out on both workers from the same Git remote, with push/fetch access. A no-project owner conversation can also name a project checked out only on the target (remote Task protocol 7 or newer).
- A sign-in on that worker for the subagent's AI provider.
- An agent saved in the app. Agents from files or plugins run only on their own worker.
- A chat you own. Chats started with an API key cannot use other workers.

### What to expect

- The subagent uses that worker's own sign-ins and keys. Nothing is copied over.
- It starts from your last commit. Uncommitted changes are not copied.
- It never gets more permission than the chat that started it.
- When it finishes, its branch is pushed to your Git remote and fetched on your worker. Your own working files are not touched.
- Approval prompts appear in the chat that started it, labelled with the worker's name. Questions and private-access requests are not passed on. They are listed in its report instead.
- If the other worker goes quiet for 30 minutes, the chat gets a notice. A late report still arrives if the worker comes back.

## Agents working together

Besides subagents, agents can coordinate with your other open chats:

- find your other active chats, including on your other workers;
- send a message to another chat;
- start a new, separate chat with a short brief.

A message from another chat is never treated as your instruction, and cannot approve anything. Each chain of work is limited to 32 new chats and 256 actions until you send a new message. One direct reply to each incoming request does not use the action budget, even if the request is still queued. Replies do not earn another free reply, so acknowledgement loops still run into the action limit. Stop, archive and ownership checks always apply.

### Coordinating without a project

An owner conversation without a project uses your selected agent and effort. It can coordinate sessions, start work in any registered project on an eligible worker, use subagents and app tools, browse the web, and use resources you grant through the access card. It can read registered project folders with Read, Glob and Grep, but cannot edit worker files, run a shell or Python kernel, or write skills or plugins. Start a session in the project for code changes; code-capable subagents must name a project and receive their own checkout there. App configuration writes still ask for your approval. API-key and non-owner conversations do not gain this surface. When a named project is checked out only on the target, Task fetches that worker's own configured upstream/default branch and creates a fresh isolated checkout without switching or changing its main checkout's tracking. An explicit source `branch` wins. If fetching fails, it uses the local source-branch tip and says so in the report. Older peers require an update; no silent local fallback occurs. Read-only remote Tasks from a no-project chat also require protocol 7 to request the chat ceiling explicitly; older senders keep their previous worker mode. StartSession on another worker from a no-project chat must also name a project. When the calling worker has a checkout, committed-branch transfer is unchanged.

Chats started by an owner conversation report back automatically. If the starting conversation is settled, it stays settled: the report remains on the child chat and you receive a notification instead. Approval prompts can be answered in the starting conversation while it is open; only your own reply there counts. Each request shows once there, as a card where it was asked. It keeps its place, shows whether it was authorized, declined or expired, and a repeat of an earlier request is marked as asked again. Your yes or no answers the oldest request still waiting. If it was settled, archived or deleted, prompts use the normal approval path. The daily spending limit covers unattended no-project coordination and the work it starts; set it in **Settings → Usage → Spending**, together with the time limit per job. Turns you type yourself are not counted or refused by these job limits. The job time limit applies to each session or subagent such a conversation starts, not to the conversation itself. Independent project work started from a project conversation is unchanged. Subagents, Swarm tasks and started chats begin in the starting chat's current permission mode, on this worker or another one, also when that chat has no project: a chat in Bypass starts them in Bypass, one in Auto in Auto, and one in Ask in Ask. They never start above a limit the starting chat runs under, and the receiving worker can lower a remote subagent's mode further. Paid generation, spending limits and new private access still ask. Report-back does not grant Bypass. A started chat whose approvals go to its starting conversation does not stop to ask you questions: each question gets its recommended answer, and the assumptions are listed in its report. Conversations run subagents here unless they name a worker. Remote StartSession requires the destination to advertise separate report-back permissions; older workers refuse with an update message. Existing code-mode conversations keep their code tools during this first step.

## Durable watches

Ask an owner chat to watch Gmail, Slack, Discord, Telegram, GitHub issues/PRs/CI, session states (stuck, waiting on you, finished), usage/credit thresholds, or a plain interval schedule. Messaging watches name an explicit channel/chat ID within the connector's allowlist. Slack reads channel history with a saved page/high-water cursor; Discord reads oldest-first message history after a saved message ID. Both require the bot's history permissions. Telegram polls a durable journal populated by the existing bot receiver: the Bot API exposes no chat-history API, so messages that Telegram never delivered to the receiver (including updates expired during downtime) cannot be recovered. Telegram's journal is worker-local; moving those watches is explicitly refused rather than skipping messages.

The agent asks one confirmation sentence covering the source/filter, executing worker, read-only access, action and daily cap; your yes authorizes it. Placement prefers an available worker advertising `always_on`, then an available Linux server. You can override `worker_id` when creating a watch. `PUT /api/v1/watches/preference` with `{"worker_id":"<network worker ID>"}` designates your preferred worker; `null` clears it. This preference and display-only watch metadata sync across your worker network. An unavailable designated worker fails explicitly without silently falling back to a sleeping desktop. Connectors and grants must already belong to the executing worker; credentials are never moved by watch placement.

Use `CreateWatch` with `move_watch_id` and `worker_id`, or owner-only `PATCH /api/v1/watches/{id}` with `worker_id`, to move a watch. Moving freezes the old executor before transferring the saved cursor, pending events and dedupe/accounting history. In-flight deliveries must finish first. A failed or ambiguous network transfer stays fenced; retry the same move, never resume the old executor. Transfers over 10,000 events or 3 MiB fail explicitly. Pause/Stop and listing route to the executing worker; synced rows remain display-only and never create a second executor.

The app polls with plain code and saves bookmarks; it does not run a model while nothing matches. A live creating chat on the executing worker receives a real turn. For a creator on another worker, delivery currently uses the executing worker's alert/investigation path instead of remotely waking the creator; notifications are stored on that executing worker. After it is settled or archived, an alert uses a tool-free cheap-model summary and a notification; an investigation starts a short no-project chat and notifies you. Notifications stay in the bell even when no device supports push. Push transport is best-effort, not an exactly-once delivery guarantee.

Ask “what are you watching?”, “pause that watch”, or “stop it” to list and control watches. Stop is permanent. Settling the creating chat does not stop its watches. Daily admission caps include in-flight reservations; actual reported costs are recorded after execution, so these are not provider-side billing caps. Floods, repeated failures and end dates pause watches with a reason. Read-only scope is enforced even in Bypass; requests for more access are left as questions for you in the resulting chat.
### Working record and fresh coordinating chats

AI Shuffle keeps an owner working record outside any one conversation: goals, decisions, pending items, what needs you, and finished reports. Ask the agent to add a decision or pending item, list the record, or resolve an item using **WorkingRecord**. An open report means finished but not yet acknowledged in the record; simply reading a summary does not acknowledge it, and this is separate from the notification bell's read state. Records and acknowledgements follow your owner account across paired workers. Remote session state is the last known snapshot and can be stale while a worker is offline.

A fresh conversation without a project receives a compact summary (at most 3,200 UTF-8 bytes) plus live session state. You can settle the old coordinator and ask a new one “what's going on?”. Child report-backs still enter the record without waking a settled starter. A genuine or richer report replaces an earlier provisional report without reopening an acknowledged item. Acknowledgements sync as separate durable receipts, so acknowledging on one worker while a report arrives on another preserves both the acknowledgement and the real result.

At a safe completed-turn boundary, a no-project owner coordinator using at least 80% of its measured context is automatically replaced by a fresh conversation on the same worker. The old chat is settled, not deleted; its transcript records the successor and a notification opens the new chat. Agent, model, effort and access choice carry over with the record, not the whole transcript. No hand-off happens while messages are queued, a question or approval is pending, or background Tasks are still running; Stop and settled chats are respected. The successor's display clock starts fresh, but any job time already consumed carries over across repeated hand-offs. Private-access grants remain session-scoped and are not copied to the successor. This is separate from **HandOffSession**, which moves the same conversation between workers.

## A subagent's chat

A subagent's chat is for reading and for messaging the subagent. It has no Git controls of its own. An approved private-access grant is also usable by that session's Task descendants on the same worker, with the same permissions and expiry. Revocation ends access for all of them. A subagent's own confirmation mode still applies; it does not borrow its parent's Bypass. StartSession chats, ordinary forks and unrelated chats do not inherit grants, and grants never cross to another worker. When a subagent needs separate private access, the card appears in the main chat on its worker, marked **Asked by subagent**; a remote child without a local main chat follows Away handling instead.

## Organizing chats

When you ask, an agent can settle chats (move them out of Active), bring them back, or archive them. It never deletes a conversation. Settling dismisses a pending prompt.
