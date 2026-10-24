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

## When a worker is busy

When database pressure persists, new turns (including Task, Swarm and StartSession
work) stay **Queued** instead of claiming a run lease. The parent's task summary
also says **Queued**. Already-running turns and lease renewals are independent.
Starts resume automatically, one at a time, when pressure clears. Stop or removing
a queued message cancels it; accepted work is not discarded on queue overflow.
Remote work is admitted on the worker that executes it, not the sending worker.

The defaults hold starts after 2 seconds of pressure: 85% ordinary-pool usage,
pool/event-write waiters, long-held transactions, or recent mean DB hold/acquisition
latency of at least 0.5 seconds. Recovery needs 3 seconds below 75% of that usage
threshold with no waiters or slow operations, then spaces starts by 0.5 seconds.
The worker polls counters every 0.25 seconds, without querying the database.
At most 128 starts wait in memory (FIFO, one per session). Overflow or a 300-second
admission wait leaves the durable turn queued and schedules another attempt after
5 seconds; retried overflow joins the tail. Existing hard job/synchronous wait
limits still apply. The provider first-frame watchdog starts counting only after
a turn leaves the queue. One snapshot checks for any queued work and no live turn,
so a newer removed message cannot hide an older waiting start. Queue reads and
status notices race idle completion and the watchdog budget; unavailable or overdue
observations do not suspend the watchdog or exempt a live provider.

Administrators can tune `DB_START_UTILIZATION` (0.85),
`DB_START_LATENCY_SECONDS` (0.5), `DB_START_SUSTAIN_SECONDS` (2),
`DB_START_RECOVERY_SECONDS` (3), `DB_START_INTERVAL_SECONDS` (0.5),
`DB_START_POLL_SECONDS` (0.25), `DB_START_MAX_WAITERS` (128), and
`DB_START_WAIT_SECONDS` (300), then restart the candidate worker. These do not
increase pool capacity or allow starts to borrow lease-reserved connections.

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

Chats started by an owner conversation report back automatically. If the starting conversation is settled, it stays settled: the report remains on the child chat and you receive a notification instead. Approval prompts can be answered in the starting conversation while it is open; only your own reply there counts. Each request shows once there, as a card where it was asked. It keeps its place, shows whether it was authorized, declined or expired, and a repeat of an earlier request is marked as asked again. Your yes or no answers the oldest request still waiting. Only a plain yes or no answers it: any other message goes to the agent straight away as an ordinary message, and the request stays open. If the conversation itself was waiting on that request, the waiting step is marked not run yet; a later yes lets the agent run exactly that step once without asking again, and a later no or an expired request is reported to the agent. If it was settled, archived or deleted, prompts use the normal approval path. The daily spending limit covers unattended no-project coordination and the work it starts; set it in **Settings → Usage → Spending**, together with the time limit per job. Turns you type yourself are not counted or refused by these job limits. The job time limit applies to each session or subagent such a conversation starts, not to the conversation itself. Independent project work started from a project conversation is unchanged. Subagents, Swarm tasks and started chats begin in the starting chat's current permission mode, on this worker or another one, also when that chat has no project: a chat in Bypass starts them in Bypass, one in Auto in Auto, and one in Ask in Ask. They never start above a limit the starting chat runs under, and the receiving worker can lower a remote subagent's mode further. Paid generation, spending limits and new private access still ask. Report-back does not grant Bypass. A started chat whose approvals go to its starting conversation does not stop to ask you questions: each question gets its recommended answer, and the assumptions are listed in its report. Conversations run subagents here unless they name a worker. Remote StartSession requires the destination to advertise separate report-back permissions; older workers refuse with an update message. Existing code-mode conversations keep their code tools during this first step.

## Durable watches

Gmail watches read history through the installed connector package, keeping OAuth and SDK
state in its isolated process. Update the Gmail package to the bundled watch-capable release
before using watches. The saved `historyId` and page token advance only when matching events
are committed; idle and nonmatching polls use no model. If Gmail expires a history ID, the
watch reads at most one page of 100 recent messages in the configured labels (including read
mail). A durable bell notification reports the missing history interval and any truncated
snapshot before the watch resumes. This is bounded recovery, not a guarantee that all mail
from the expired interval can be reconstructed. Other API failures do not reset the bookmark.

Ask an owner chat to watch Gmail, Slack, Discord, Telegram, GitHub issues/PRs/CI, session states (stuck, waiting on you, finished), usage/credit thresholds, or a plain interval schedule. Messaging watches name an explicit channel/chat ID within the connector's allowlist. Slack reads channel history with a saved page/high-water cursor; Discord reads oldest-first message history after a saved message ID. Both require the bot's history permissions. Telegram polls a durable journal populated by the existing bot receiver. It commits each update to the journal before acknowledging the next one, retrying persistence failures instead of silently dropping messages. Chat execution then runs in a bounded, tracked background task, leaving the receiver free for approval buttons and other chats; stopping the connector cancels those tasks. If chat capacity is full, it replies with a busy notice while preserving the journaled observation. Journal writes and cursor reads are serialized per connector to prevent concurrent commits from skipping messages: the Bot API exposes no chat-history API, so messages that Telegram never delivered to the receiver (including updates expired during downtime) cannot be recovered. Telegram's journal uses the bounded retention and fenced transfer rules below. Installed first-party Slack, Discord and Telegram connector packages must be updated to the bundled watch-capable release before creating watches; older packages fail explicitly. Stop their connections before updating the package, then reconnect.

The agent asks one confirmation sentence covering the source/filter, executing worker, read-only access, action and daily cap; your yes authorizes it. Placement prefers an available worker advertising `always_on`, then an available Linux server. You can override `worker_id` when creating a watch. `PUT /api/v1/watches/preference` with `{"worker_id":"<network worker ID>"}` designates your preferred worker; `null` clears it. This preference and display-only watch metadata sync across your worker network. An unavailable designated worker fails explicitly without silently falling back to a sleeping desktop. Connectors and grants must already belong to the executing worker; credentials are never moved by watch placement.

Use `CreateWatch` with `move_watch_id` and `worker_id`, or owner-only `PATCH /api/v1/watches/{id}` with `worker_id`, to move a watch. Moving freezes the old executor before transferring the saved cursor, pending events and dedupe/accounting history. In-flight deliveries must finish first. Once export commits, a failed or ambiguous network transfer stays fenced; retry the same move, never resume the old executor. Prerequisite rejections before export do not fence the receivers. Transfers over 10,000 events or 3 MiB fail explicitly. Pause/Stop and listing route to the executing worker; synced rows remain display-only and never create a second executor.

In the running-tasks panel, each watch names the worker it runs on, with **Always on**, **Server** or **May sleep**, and whether that worker is online. With a second worker, **Move** lists the other workers; an offline one is shown but cannot be picked. While a move runs, the row says so and Pause, Stop and Move wait. A refused move shows the reason with **Try again**. A move with no answer reads **not confirmed**, never as moved, and offers **Retry move**. If the list then shows the watch on its new worker, the panel asks that worker to confirm the move, so the old worker records that it finished. That confirmation never moves the watch itself: if a newer move has taken it elsewhere, it is refused and the note clears. It reads **Moved** only when the confirmation comes back. A watch left fenced reads **Move unfinished** on every device until you retry it. A watch whose worker is offline shows its last synced state, and its controls wait until that worker is back. The **New watches** control in the Watches heading sets or clears the preferred worker. It stays available with no watches: the list keeps a Watches entry while a worker is chosen, and the full list offers it whenever you have a second worker. It names the worker automatic placement would pick, and says plainly when the chosen worker is offline.

The app polls with plain code and saves bookmarks; it does not run a model while nothing matches. A live creating chat receives a real read-only turn, including when its watch executes on another worker. Authenticated peer delivery retains the same event command across retries and restarts; the creating worker rechecks the executor's current grant, deadline and budget before unattended work. Receipts are not executable watch leases. If the creating worker is offline, chat delivery waits durably rather than running against its mirror, while the bell retains bounded event details without waiting for inference. After the creator is settled or archived, an alert uses a tool-free cheap-model summary and a linked notification; an investigation starts a short no-project chat and notifies you without reactivating the creator. The event's durable bell notification is shared with your network workers using an idempotent outbox, including offline peers after reconnect. Pending notifications and dedupe/accounting history survive watch transfers. Notifications stay in the bell even when no device supports push. Push transport is best-effort, not an exactly-once delivery guarantee.

Ask “what are you watching?”, “pause that watch”, or “stop it” to list and control watches. Stop is permanent. Settling the creating chat does not stop its watches. Daily admission caps include in-flight reservations; actual reported costs are recorded after execution, so these are not provider-side billing caps. Floods, repeated failures and end dates pause watches with a reason. Read-only scope is enforced even in Bypass; requests for more access are left as questions for you in the resulting chat.
### Telegram journal retention and moves

Each connector retains at most 1,000 received journal entries (at most 16,000 bytes each).
Entries are removed only after every active or paused watch cursor has passed them. If a
cursor would hold the journal above that limit, that watch is explicitly paused with
“Telegram journal retention exceeded; create a new watch to opt in again”. Its old cursor
cannot be resumed or silently rebased; a new watch requires a fresh confirmation. Message-ID
high-water marks contain no message text and prevent compacted Telegram replays from creating
new observations. Unwatched content is discarded immediately; hourly maintenance also cleans
old/idle journals. Deleting a connector removes its journal content and high-water marks,
invalidates pending transfer snapshots, expires its watches and keeps only a content-free
receive fence against late callbacks.

Before moving a Telegram watch, stop and disable the connection on **both** workers. Provision
the same connector ID and bot credential independently on the destination, with the watched
chat allowlisted and its original agent grant available there. Credentials are never copied.
The connector must have no other active/paused non-expired watches: moving one bot receiver
cannot silently strand another watch. Do not run two `getUpdates` consumers for the same bot.

Prerequisite checks do not fence either receiver. A rejected destination leaves the source
restartable and allows choosing another target. Export commits the source fence with its
snapshot; only then does import fence the destination. The move exports the unread chat tail with
the cursor, pending events and dedupe history, and rebases that tail to destination sequence
numbers in a single import transaction. Only after import commits does the destination enable
and start its receiver. The old receiver stays fenced across restarts and ambiguous network
failures. Retry the same move after acknowledgement loss; do not re-enable the source or
start a second consumer as recovery. Completed transfer snapshots discard their message
content. Messages Telegram never delivered, including expired updates during downtime,
cannot be reconstructed: Telegram has no chat-history API.

### Working record and fresh coordinating chats

AI Shuffle keeps an owner working record outside any one conversation: goals, decisions, pending items, what needs you, and finished reports. Ask the agent to add a decision or pending item, list the record, or resolve an item using **WorkingRecord**. An open report means finished but not yet acknowledged in the record; simply reading a summary does not acknowledge it, and this is separate from the notification bell's read state. Records and acknowledgements follow your owner account across paired workers. Remote session state is the last known snapshot and can be stale while a worker is offline.

A fresh conversation without a project receives a compact summary (at most 3,200 UTF-8 bytes) plus live session state. You can settle the old coordinator and ask a new one “what's going on?”. Child report-backs still enter the record without waking a settled starter. A genuine or richer report replaces an earlier provisional report without reopening an acknowledged item. Acknowledgements sync as separate durable receipts, so acknowledging on one worker while a report arrives on another preserves both the acknowledgement and the real result.

At a safe completed-turn boundary, a no-project owner coordinator using at least 80% of its measured context is automatically replaced by a fresh conversation on the same worker. The old chat is settled, not deleted; its transcript records the successor and a notification opens the new chat. Agent, model, effort and access choice carry over with the record, not the whole transcript. No hand-off happens while messages are queued, a question or approval is pending, or background Tasks are still running; Stop and settled chats are respected. The successor's display clock starts fresh, but any job time already consumed carries over across repeated hand-offs. Private-access grants remain session-scoped and are not copied to the successor. This is separate from **HandOffSession**, which moves the same conversation between workers.

## A subagent's chat

A subagent's chat is for reading and for messaging the subagent. It has no Git controls of its own. An approved private-access grant is also usable by that session's Task descendants on the same worker, with the same permissions and expiry. Revocation ends access for all of them. A subagent's own confirmation mode still applies; it does not borrow its parent's Bypass. StartSession chats, ordinary forks and unrelated chats do not inherit grants, and grants never cross to another worker. When a subagent needs separate private access, the card appears in the main chat on its worker, marked **Asked by subagent**; a remote child without a local main chat follows Away handling instead.

## Organizing chats

When you ask, an agent can settle chats (move them out of Active), bring them back, or archive them. It never deletes a conversation. Settling dismisses a pending prompt.
