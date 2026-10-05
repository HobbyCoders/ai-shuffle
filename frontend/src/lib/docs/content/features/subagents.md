<!--
title: Subagents (Folded into Agents)
description: Subagents are now ordinary agents
order: 4
-->

# Subagents (Folded into Agents)

Subagents are no longer a separate concept. There is no more standalone `/api/v1/subagents` API and no dedicated "Subagents" section of the app — everything here now lives under [Agents](/docs/features/profiles).

## What changed

- A former subagent's `prompt` is now an agent's `config.system_prompt.content` (with `config.system_prompt.type` set to `"custom"`).
- A former subagent's `tools` is now `config.allowed_tools`, read together with `config.tool_selection_mode` (`all`, `none`, `allow`, `disallow`). See [Tool Configuration](/docs/features/profiles#tool-configuration).
- A former subagent's `model` is now the agent's `config.provider_models` entry: the model it runs on when another agent dispatches it.
- A former subagent's `memory` is now `config.memory`.
- Delegation from one agent to a specialist during a session still works the same way, but the specialist is now managed through the owning agent's nested-agent endpoints (`/api/v1/agents/{agent_id}/agents`) rather than a standalone router.
- Orchestration and multi-step delegation during a session go through the app's Task tooling, not a separate subagent-dispatch mechanism.
- A Task worker started from an API key's session runs as that key (its agents, tools and credentials) and cannot use an agent outside the key's assigned agents. See [API Users](/docs/api-reference/api-users#own-sessions).

## Models the user asks for

Task, Swarm tasks and `StartSession` take an optional `model`. Leave it out and
a subagent runs on its pinned model, else on the subagent default; a chat
started with `StartSession` runs on the caller's model. That is the normal case.

`model` is for when you name a model yourself, for example "spin up a general
agent on GPT-6 Astra" when that agent is pinned to Opus. Any model the app can
run is accepted: a Claude model (`opus`) or a model on an enabled provider's
list (`prov:<providerId>:<modelId>`). A model on another provider moves that one
subagent or chat to that provider.

The app, not the agent, decides whether a different model is used:

- **You named it.** The app reads every message you typed in the calling chat,
  including the first message of a chat started on a new branch, messages you
  sent while it was working and messages sent through the API, so "use Opus
  sub-agents" at the start of a chat keeps counting for the whole chat. For a
  subagent it reads the chat at the top of the chain, and for a chat another
  chat started, the chat where you typed the request. A model counts as named
  when its id, Claude name or display name appears, ignoring case and
  separators ("GPT-6 Astra", "gpt 6 astra" and "gpt-6-astra" all match), or one
  distinctive word of its name ("astra") that no other available model shares.
  Messages from other agents, tool output and the agent's own text never count.
- **Otherwise you are asked.** A prompt asks whether to start that subagent or
  chat on the model instead of the usual one. It appears in every permission
  mode, Bypass included, and a remembered answer never skips it. If you decline,
  do not answer, or the chat is in Away mode, nothing is started and the agent
  is told to leave `model` out. A yes covers that model for the rest of the
  chat, its subagents included, and several starts that need the same answer
  at once share one prompt. After an app restart you are asked once more.

A model equal to the one that would run anyway needs no check. The choice
covers one start only: it never changes an agent's pin or the subagent default.
Effort, context window and fast mode settings the new model does not support
fall back to that model's defaults. The receipt names the provider and model
used and how it was asked for (`named_by_user` or `approved_by_owner`), and the
running-tasks list marks such a subagent's model "your choice".

Limits of the check: it reads only messages you typed into the chats on this
worker. Work begun from another worker, from an automation or from a connector
always gets the prompt. When you answer a question the agent asked, only your
answer counts, not the question, and a request a voice call hands to the chat
does not count because the voice model wrote it. A model you mentioned only to rule it out ("not Opus")
still counts as named. A name that appears in a document you pasted counts as
named.

## Task workers across an app restart

Background Task workers and Monitor commands stop when the app restarts. On the
next start, the chat that dispatched them gets one automatic turn listing which
tasks were interrupted. An interrupted worker keeps its task id and transcript:
`TaskList`, `TaskGet` and `ListAgents` show it as `interrupted`, and
`SendMessage` to its task id or name continues it from its saved transcript.
Interrupted Monitor commands are not restarted; run them again if they are still
needed.

## Running a subagent on another worker

When your account has more than one worker in its
[worker network](/docs/features/worker-network), a Task subagent can run on
another worker. Ask for it in plain words ("run this on Unraid"); the agent
names the worker in its Task call. Each Swarm task can name a worker too. The
subagent is still part of the chat that started it: it reports back there, you
can message and stop it from there, and its approval prompts appear there.

### Setting a default for the assistant

The **Run work on** row in the Limits section of the Assistant page sets where
the personal assistant's background jobs run. It applies to Task and Swarm work
started by the assistant's conversation and by the jobs it starts, on the worker
whose page you set it on. Ordinary chats never use it. A worker that is offline
or needs an update is shown but cannot be picked.

If the chosen worker cannot take a job (it went offline, runs an older release,
or refused before anything started), the job runs on the assistant's own worker
instead and the agent is told why. A worker the agent names itself is never replaced: if it
cannot take the job, the call is refused.

### What it needs

- **Both workers online and up to date.** A worker on an older release is
  refused with a request to update it.
- **The project checked out on that worker,** from the same Git remote. The
  subagent's branch moves between workers through that remote, so both workers
  need to be able to push and fetch there with their own Git credentials.
  Nothing is cloned for you.
- **A login on that worker** for the subagent's provider, and a model that
  worker can serve. If one is missing, the start is refused. It never switches
  to another provider or model.
- **An agent stored in the app.** Agents defined in files or plugins run only on
  the worker that started them.
- **A chat you own.** Sessions started with an API key, and the assistant's
  background observation, cannot run a subagent on another worker.

A subagent running on another worker can start its own subagents only on that
worker.

### What travels and what does not

- **The brief** travels, up to 50,000 characters. A longer one is refused, never
  cut.
- **The permission mode** is a ceiling, not a grant. The subagent runs at the
  strictest of the parent's mode, the mode set on its agent, and the most that
  worker allows (`REMOTE_TASK_MAX_PERMISSION_MODE`, default
  `bypassPermissions`). The start receipt names the mode it got.
- **Credentials** do not travel. The subagent uses the logins and provider keys
  on the worker that runs it.
- **Private-access grants** do not travel. The subagent has none, and the
  parent's grants are not passed on.
- **Memory and skills** are the copies on that worker. The network syncs saved
  facts and skills between workers, so they match once sync has caught up.
- **The worktree** does not travel. Your committed work does: the subagent gets
  its own checkout on that worker, on a `task/<id>` branch at the parent's last
  commit. Uncommitted changes in the parent's checkout are not copied.
  Read-only research agents use that worker's checkout of the project
  read-only. Without a project, the subagent runs in that worker's workspace.
  Paths never travel.

### While it runs

A subagent on another worker always runs in the background, even when the agent
asked to wait for it. Its progress card follows its transcript as it syncs from
that worker, and `TaskList`, `TaskGet` and `ListAgents` show which worker runs
it. When it finishes, the chat that started it is woken with its report, the
same as for a local subagent.

**Commits come back.** When it finishes with commits, the worker that ran it
pushes its `task/<id>` branch to the Git remote, never forced. The parent's
worker then fetches it, so `git log task/<id>` works there. The parent's working
tree, index and current branch are not touched. Uncommitted changes the subagent
left behind are counted in the report, never committed. The report ends with a
short "Git:" note saying where the work is, or why it was not pushed or fetched.

**Stop.** Stopping the subagent reaches it on the worker that runs it: its stop
button in the chat's background tasks, Stop on the parent chat, Swarm cancel and
deleting the parent chat all do. If that worker is unreachable, the stop is kept and sent again when it
answers. A worker on an older release cannot be stopped this way and says so. A
stopped subagent refuses later messages, so it is never restarted by accident.

**Messages both ways.** `SendMessage` reaches a subagent on another worker the
same way it reaches a local one. A busy subagent reads the message at its next
tool result. One that is waiting on background work holds it until it next
wakes, unless the message is urgent. A finished subagent is resumed over its own
transcript and reports again. The subagent can also message `main`, the chat
that started it. A message is refused, with the reason, when the subagent was
stopped, when its worker is unreachable or runs an older release, or, for the
assistant's subagents, when its daily spending limit has been reached. "Queued"
and "held" are never reported as delivered.

### Approvals

When the subagent needs your permission, the prompt appears in the parent chat
on the parent's worker, with the worker's name on the card (for example "Task ·
Default on Unraid"). When the parent is the assistant's conversation, the
assistant asks you in plain words instead, as for its other work.

- Only "allow once" or "deny" goes back to the subagent. If you choose
  "always allow", that call is allowed once and the rule is saved on the
  parent's side only. Rules saved on the parent never answer the subagent's
  prompts: a person decides each one.
- Anything that goes wrong denies, never allows: no answer in time, the parent
  unreachable or on an older release, or Stop on either worker. A prompt that
  nobody answered in time is listed in the subagent's report.
- Questions and private-access requests from a subagent on another worker are
  not passed on. They are held back and listed in its report. A subagent on
  this worker asks in its main chat instead (see
  [A subagent's chat](#a-subagents-chat)).

### Spend limits

The assistant's **Daily spending limit** and **Longest background job** count
work on other workers: Task subagents and chats the assistant started there.
The parent's worker asks the other worker for each job's running cost about once
a minute, so remote cost can show up a little late. When a limit is reached, a
remote subagent is stopped the same way Stop stops it, and a new remote Task is
refused before anything is sent. Cost spent while a worker was unreachable is
counted when it answers again.

### When a worker goes quiet or restarts

- **The 30-minute notice.** If the worker running a subagent has not answered
  for 30 minutes, the chat that started it gets one notice: it stopped waiting,
  and the subagent may still be running there. Nothing is sent to that worker.
  Time while the parent's own worker was down does not count, and after a parent
  restart the 30 minutes start again. Open approval prompts from that subagent
  are withdrawn; if its worker comes back with the subagent still running, its
  next prompts are shown again.
- **Late reports.** If the worker comes back and the subagent finishes, its
  report is still kept, and the chat is woken once more with the real outcome.
  Messages to it are delivered again once its worker answers.
- **Stop after the notice.** Stop in the parent chat still reaches the
  subagent: the stop is kept and sent when its worker answers again, and the
  subagent's later report does not wake the chat.
- **The subagent's worker restarts.** The run is cut off. When that worker
  starts again, it reports the run as failed, says it restarted, and includes
  the subagent's last words marked "(unfinished)". Continue it with
  `SendMessage` once that worker is back.
- **The parent's worker restarts.** The parent keeps its record of the subagent,
  and its card, prompts and status checks come back at startup. A report sent
  while the parent was down is sent again until the parent accepts it.

## Independent top-level orchestration

Agents can coordinate relevant work without a parent/subagent relationship:

- **ListSessions** discovers active (open, including idle) top-level chats owned
  by the same user on this worker. It returns titles, projects, models and status,
  not conversation contents. The default scope is the current project; `all`
  permits cross-project discovery. Follow `next_offset` to paginate.
- **MessageSession** queues an attributed peer message using an exact discovered
  session ID. A peer can reply with the same tool. Delivery is asynchronous:
  `queued` is not a delivery or completion acknowledgement.
- **StartSession** creates a visible, independent chat with a self-contained peer
  brief. It inherits the caller's agent, model/provider, permissions, project and
  workspace, but not its transcript, private access grants or subagent lifecycle.
  Its optional `model` starts the chat on another model or provider when you
  asked for one (see [Models the user asks for](#models-the-user-asks-for)).
  `MessageSession` works between chats on different providers.
  Coordinate file ownership or select a separate branch before concurrent edits.

These tools do not require an explicit request to use subagents. Models should
judge relevance, prefer existing peers when appropriate, and avoid courtesy
acknowledgements or message loops. They remain subject to the agent's tool policy
and normal write permission gates. Peer text is not a user instruction and never
permits bypassing a denial or delegating prohibited work.

Stopped, archived, hidden, voice, subagent, other-owner and other-worker sessions
are not eligible. Starting independent sessions is available only from eligible
top-level chats. Outside the personal assistant's conversation no automatic
parent completion notification is registered: ask a peer to report back with
MessageSession when needed.

Use a stable `request_id` for each start/message and reuse it on retries. Durable
limits bound each autonomous chain to **4 new sessions, 32 total actions and 8
hops**; only new user input starts a fresh budget. Existing `Task`, `Swarm`,
`ListAgents` and `SendMessage` retain their parent/subagent semantics.

### Work the personal assistant starts

Chats that the personal assistant starts from its own conversation behave
differently in four ways. Chats started from any other conversation are
unchanged.

- **They report back.** When a started chat finishes, fails or is stopped, the
  assistant is woken with its report, the same way a finished `Task` wakes its
  parent; if the assistant is mid-reply, the report joins that reply. The
  assistant is asked to answer in at most three short lines. Work you type into
  the started chat yourself is not reported. A report never grants a fresh
  start budget: the 4-session and 32-action limits still reset only when you
  send a message.
- **They can run in a named project.** `StartSession` takes an optional
  `project` (a registered project's name, id or checkout path) in the
  assistant's conversation only. The chat runs on this worker when the project
  is checked out here, otherwise on an online worker that has it, using that
  checkout. An unknown project is refused with the list of registered names, and
  a project nobody has checked out is refused without starting anything. Naming
  a project grants no access.
- **Questions don't stall them.** A question the started chat asks is answered
  with its recommended option and recorded, as in Away mode.
- **Approvals are asked in the conversation.** When the assistant, a subagent
  under it, or a chat it started needs your approval, the assistant asks one
  plain question ("Do you authorize me to run `git push` for “Fix the flaky
  test”?"). Only your own next message typed in the assistant's conversation
  answers it; work a voice call hands to the chat never does. A plain yes
  approves that one request; a plain no declines it. A hedged, delegating or unrelated reply is not an answer: the
  assistant asks once more, and a second unclear reply declines. One question is
  asked at a time, each yes covers only its own request, and an unanswered
  question lapses after 15 minutes. Peer messages, reports and tool output can
  never approve anything. What needs approval at all is unchanged. Entering a
  secret still uses the private access card.

A chat started on another worker cannot send its approvals back, so it runs in
Away mode there: approvals are deferred, never granted, and listed in its report
for you to approve in that chat. Both workers need this release; an older worker
starts the chat but it will not report back. Task subagents on another worker
work differently: see
[Running a subagent on another worker](#running-a-subagent-on-another-worker).

## A subagent's chat

Opening a subagent's transcript (Open transcript on its Task or Swarm card)
shows its chat for reading and for messaging the subagent. It has no Git panel,
branch control or private access control. The worker refuses owner-driven Git
requests made for a subagent's session (commit, push, pull, branches, checkout,
pull requests, Finish, Ship It, enabling auto-merge and moving it to another
worker). Project-level Git controls outside that session remain available to
the owner. The subagent's own Git work and hand-off through its tools are unaffected.

When a subagent on this worker asks for private access, or a grant it holds
needs a one-shot approval, the card shows in its main chat (the top-level chat
at the root of the delegation, also for a nested subagent), headed "Asked by
subagent" and the subagent's title. Your answer goes to the subagent, and its
grants are listed there so you can revoke them. Approval rules, scope and expiry
are unchanged, and a subagent's grant is never usable by the main chat. A chat
started with StartSession is a top-level chat and keeps both its Git panel and
its private access control.

## Session organization

When asked to organize chats, agents can use **SettleSession** to move a chat out
of Active, **UnsettleSession** to put a settled chat back in Active, and
**ArchiveSession** to archive it without deleting the conversation. Each takes an
exact `session_id`, or a `session_ids` list of up to 500 chats in one call; the
current chat is also allowed. `ArchiveSession(all_settled=true)` archives every
settled chat in the project (or everywhere with `scope="all"`) without listing
first, never including the current chat. Bulk calls check each chat on its own,
keep going past refusals, and return counts plus the refused or failed chats.
These are write tools and use the same permission gates and sidebar updates as
the app's controls.

`ListSessions(state="settled")` finds settled chats. `state="archived"` finds
archives, and `state="all"` includes both, plus active and stopped chats. These
management views include same-owner chats on other workers; they do not make
those chats eligible for messaging. Project scope and pagination still apply.

None of these actions starts or stops work, clears a user stop, or deletes a
transcript. Settling and unsettling work even when the execution worker is
offline; archiving requires that worker to be reachable. Archived chats must be
restored in the app before they can be unsettled. Hidden, voice and subagent
chats are excluded, and persistent personal assistant chats cannot be settled or
archived. Other users' chats are never accessible.

## Where to look now

- [Agents](/docs/features/profiles) — the unified configuration model (tools, prompt, memory, the agents it can call).
- [Agents API reference](/docs/api-reference/agents) — full endpoint list, including the nested per-agent delegate endpoints and a field-by-field migration table for anyone still calling the old `/api/v1/subagents` paths.
