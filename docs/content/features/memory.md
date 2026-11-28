<!--
title: Memory
description: See, change and delete what agents remember between chats
order: 13
-->

# Memory

Agents can save short facts to remember between chats, such as how you like updates written or how a project is deployed. You can read and manage all of them in **Settings → Memory**.

Each memory shows who saved it (you, an agent, or learned automatically), which project it belongs to, and when it was last used. Search finds words in the memory or in its **When this applies** line. The filter switches between **Active**, **Archived**, **Needs review** and **All**.

## Changing a memory

Open a memory, edit it, and choose **Save**. **When this applies** tells agents when to load the memory, for example "when writing release notes".

When a chat reaches the situation a memory describes, its full text is added to the chat once: with your message when your words match it, or with the result of a matching command, file or tool. An agent saving a memory can name the commands (such as `git push`), file paths or tools it applies to. Set `FLUID_MEMORY_JIT=false` on the worker to turn this off. Agents still see the list of memories either way.

A command's result arrives after the command has run, so a rule about a command, file or tool also needs to reach the agent beforehand. For these memories the list agents see at the start of a chat includes the rule itself, as one short line after **When this applies**. In Settings it's shown as **Rule before acting**, and you can edit it like the rest of the memory. If nobody wrote one, the memory's first sentence is used until you do. These lines share about 2,400 characters per chat. Rules you saved yourself get that room first, then the ones used most recently. A rule that doesn't fit is listed by its **When this applies** line alone, and **Memory health** says how many didn't fit.

Agents don't save where a piece of work stands, such as "continuing the fix in PR #123", as a memory: that goes in the chat's working notes. If you ask for it anyway, it's saved. When an agent saves a memory that looks like one it already has, it's shown the existing one so it can update it instead of keeping two. Facts about a worker's setup, such as which Python has pytest, are checked fresh at the start of each chat rather than saved as memories; ones saved before expire on their own.

- Saving a memory an agent wrote makes it yours, and it's kept until you change it.
- Saving an archived memory makes it active again.
- A memory marked **Needs review** conflicts with another one, or a newer one replaced it. Saving your version settles it.

If the memory changed somewhere else while you were editing, your text stays on screen and you can choose **Load latest** to replace it with the saved version.

## Learned behaviour

A background learner looks back over each finished chat and saves two things: facts (the memories on this page) and behaviour rules, short "When X: do Y" instructions about how you want agents to work. Rules come from your corrections, for example "stop opening draft PRs" or "lead with the result". Every rule must quote words you actually wrote, and a rule can never grant permission for something; it only describes how to work.

Open **Settings → Learned behaviour** to see them. Each rule applies to every model or to one model family (Claude, GPT, Gemini, Grok or other models), and new chats with a matching model get the rule in full. A rule the learner just wrote shows **New** for three days. **Undo** removes a new rule, or brings back the text it had before the learner changed it. Saving a rule makes it yours, and the learner never rewrites it. **Delete** removes it on all your workers. You can also add a rule yourself with **Add rule**. Rules sync to all your workers like memories.

The learner reflects on a chat once, when the chat settles or goes quiet, in a single model call. It only does that when the chat gave it something to learn from:

- you corrected the agent ("no, use the other branch") or sounded frustrated: swearing, words like "useless" or "this sucks", phrases like "I told you" or "how many times", or a message in capitals;
- you asked it to remember something ("remember this: …");
- the same friction kept happening, such as a path that kept not existing or work done twice.

Words inside code don't count. A chat with none of these is skipped without any model call, and **Recent activity** says so. If you come back to a chat and it settles again, the learner reads only the new part.

It reads your messages, the agent's replies, the friction it noticed and the memories already saved for that place. It doesn't read the tool calls. It saves a preference at once, as a rule under **Learned behaviour**, only when it can quote your own words for it. Anything else it learns waits in **Proposed memories**.

### Proposed memories

**Settings → Memory → Proposed memories** lists what the learner noticed but wasn't sure enough to save, such as "the project deploys with `make release`" or "the backup NAS is at 192.168.1.20". Each one shows its kind (an owner preference, an owner fact or project knowledge), the words of yours it was based on, the chats it came from and when it expires.

- **Accept** saves it as a memory, and agents can use it from the next chat.
- **Dismiss** drops it without saving anything.
- Otherwise it disappears after 14 days. Nothing is saved unless you accept it.
- A proposal you dismiss doesn't come back, even if a later chat finds the same thing.

If several chats noticed the same thing, the proposal says how many. That's for your information only.

Agents never see a proposal: it isn't in the memory list, isn't added to chats and Recall can't find it. Proposals are shared with all your workers, and accepting or dismissing one anywhere decides it everywhere.

### Cost

The learner runs on the same model, provider and effort as the chat it learns from, so a chat on a strong model gets lessons written by that model. Each reflection is billed like a message in that chat, and a chat's text only ever goes to that chat's own model. A chat with no learner (for example, one on a sign-in that refuses background calls) is skipped. A reflection reads at most about 28,000 characters, less if that chat's model has a smaller limit.

**Setup** on the Learned behaviour page shows the last reflection (whether it ran or was skipped, and why), the model and billing it used, and today's calls against the daily limit. By default the limit is 48 calls a day on a pay-per-use provider and 200 on a flat-rate subscription. Set **Daily learning calls** to choose your own (0 picks the default). Turn off **Learn from chats** to stop the learner completely. While it's off it makes no model calls. **Recent activity** lists the latest reflections and what each one saved, and **Built-in guidance** shows the instructions that ship with AI Shuffle for each model family. They can't be edited.

Until the next memory update, a worker can go back to the earlier learner, which read new activity in every chat as it happened, by setting `FLUID_MEMORY_LEARNER=continuous`.

## Deleting a memory

**Delete** is permanent and removes the memory on all your workers. It also stops agents from saving that same fact again. Past chats are not erased, so anything said in them stays there.
