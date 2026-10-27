<!--
title: Voice Mode
description: Talk to your agents in a voice call and hand them work out loud
order: 10
-->

# Voice Mode

Voice mode adds a spoken conversation to a chat. You talk, the assistant answers out loud, and anything that needs real work is handed to the chat's agent while you keep talking.

## Set up

1. **Add an API key** under **Settings → Providers**: OpenAI for GPT Live, or xAI for Grok Voice. To use a ChatGPT subscription instead, connect your ChatGPT login under **Settings → Providers** and choose **GPT-Live-1.1 · ChatGPT subscription** as the voice engine in **Settings → Voice** (`voice_model: "gpt-live-1-codex"` in the API). It uses your ChatGPT subscription login; it doesn't fall back to an API key. It stays off until you select it, offers the 25 ChatGPT voices (Cove by default), and has no voice samples or automatic provider retry. When your OpenAI API key can use GPT-Live-1.1, it also appears in the engine list as **GPT-Live-1.1**. See the [Voice API](/docs/api-reference/voice). To use a SuperGrok subscription instead of an xAI API key, connect your SuperGrok login under **Settings → Providers** and choose **Grok voice (SuperGrok subscription)**; it never falls back to an xAI API key.
2. **Turn on Voice calls** under **Settings → Voice**. Choose **GPT Live 1**, **Grok Voice** or **Grok voice (SuperGrok subscription)**, then a voice (the play button plays a sample; the SuperGrok engine has no samples), set **Max call length**, and change the **Call prompts** if you like. Both Grok engines default to **Eve**, use server voice activity detection, and delegate work to the same chat agent. Calls keep the engine, provider, voice and prompts they started with.
3. In the voice chat's composer, **choose the model** that does the work, with its effort and permission mode.

GPT Live includes the nine additional API voices Nuri, Juni, Flitz, Brise, Sillage, Nira, Harema, Shida and Noeul (documentation checked 2026-10-18). GPT Live 1.1 is coming soon to the API; no undocumented model ID is offered. Prompt-to-voice/custom voice creation is not implemented here.

The **Dictation** settings on the same page, including the **Voice input hotkey**, are for speaking into the message box. They do not affect calls.

## Start a call

In any chat, press the round **Talk** button (a waveform) to the right of the message box, next to the microphone. Talk shows while the message box is empty; once you type or attach something it turns into **Send**, and clearing the box brings Talk back. In a new chat it reads **Talk to** the agent's name and creates the chat first.

You can also choose **Voice** in the session mode switcher, then **Start a voice call**. To call again on an existing voice chat, use **Start a call on this voice session** in the composer. When you start a call, the voice picks up first with a short hello, using your first name when the app knows it (never a username). On a chat that already has history it welcomes you back instead, and it never greets again after a reconnect. Each new call uses your current voice settings, so changing the engine or voice in Settings applies to the next call in any chat. A call that is already running keeps its engine and voice until you hang up.

During a call, the call dock shows whether it is connected and who is speaking, how long the call has run, and buttons to:

- end the call;
- mute your microphone;
- mute the assistant.

You can still type messages and attach files during a call. They go to the same chat.

If you open another chat or page during a call, a small call bar keeps the controls in reach. Drag it by its handle to anywhere in the window, or focus the handle and use the arrow keys. It stays where you put it on that device. Double-click the handle, press Escape on it, or use its put-back button to return it to its usual place. In the desktop app, the pop-out button moves the call bar into a small window that stays on top of other apps; its put-back button returns it to the app.

**Voice volume** under **Settings → Voice** sets how loud the assistant plays on this device, up to 300% for quiet speakers. Changes apply to a call in progress.

Hanging up ends the audio. Work already handed to the chat keeps running. Stop it separately if you want to cancel it.

## Handing work to the chat

Ask for something in plain words, for example "fix the failing test" or "summarize today's emails". The request goes to the voice chat itself, with its model, agent, tools and approval rules.

- The voice talks as the one doing the work ("I'm running the tests"), not about a separate agent, and only claims what the chat has confirmed.
- Results, failures and approval requests are spoken. When work finishes you hear what happened in plain words: the outcome, what changed, anything you need to do, and any question at the end. Code, tables, logs and links stay in the chat.
- When the chat stops to wait on something, such as a review, CI or a background job, you hear what it has done so far and what it is waiting on. When it finishes, you hear only what is new.
- Routine progress is not announced. Ask "how is it going?" whenever you want an update.
- Edited **Call prompts** replace the defaults for those engines. Calls on a chat also include the start of the chat agent's own instructions, for its persona and tone.
- What you and the assistant say is saved in the chat as text.

## Calling a chat while it works

You can call any of your chats at any time, even while its agent is busy. A call never starts by itself: nothing listens until you press the call button.

- **Ask for status**, such as "what are you working on?" or "is it done yet?". It answers from the app's task list.
- **Finished jobs** are mentioned at the next pause.
- **Approvals:** when work needs your permission, the agent asks one plain question, and you can answer it out loud or by typing. Only a clear yes approves and a clear no declines. An unclear spoken answer is asked once more, then declined. A typed message that is not a clear yes or no never answers: it goes to the agent as an ordinary message and the request stays open.
- **Secrets stay on screen.** Private access requests are answered on screen, and the agent tells you when that is needed.

## Calling a chat another worker runs

You can call a chat that runs on another of your workers, or pick another worker in the "Runs on" sentence of a new chat and press **Talk**. The call runs on the worker you are connected to, with that worker's voice settings and voice login. What you ask for goes to the chat on the worker that runs it, as a typed message would, and that worker records it. Replies, finished jobs and approval questions reach the call as that worker's chat updates arrive.

- The spoken back-and-forth is not saved in that chat. Only your requests and the agent's replies are.
- Spoken yes and no answers are sent to the worker that runs the chat.
- If that worker is offline, the call button says so and no call starts.

## Background noise

If breathing or room noise keeps starting a turn, raise the microphone threshold in the voice options. If a quiet microphone gets cut off, lower it.

## Dropped connections

For GPT Live and both Grok engines, if the connection drops, for example when your phone moves from Wi-Fi to mobile data, the call shows **reconnecting** and picks up where it left off. Anything said while it shows **reconnecting** is not heard. If it cannot reconnect, the call ends with a message, and you can start it again. ChatGPT voice and PersonaPlex calls end on a dropped connection; start the call again to rejoin.

## Long calls

Providers end their own voice sessions after a while (OpenAI and xAI after an hour). For OpenAI Realtime, GPT Live and both Grok engines the app opens a fresh provider session in the background before that, at a pause in the conversation, and carries the conversation over without greeting you again, so the call doesn't drop. Work updates that arrive during the swap are held and spoken afterwards. ChatGPT voice and PersonaPlex calls are not renewed and end when the provider ends its session. Even with no length limit, a call ends when your device has sent nothing for two minutes (for example, the app was closed or the phone lost its network), so a forgotten call does not keep running.

## Costs

- Call audio and voice samples are billed to your selected engine’s OpenAI or xAI API account. API keys stay on the server; the browser streams PCM audio through the app, not directly to xAI.
- Work handed to the chat is billed to that chat model's own provider.
- **Max call length** limits the length of a call, not its cost. Set it to 0 (**No limit**) to keep a call going for as long as you stay connected. Check your provider's usage page for real charges.

## Older voice chats

Voice chats created with earlier versions may still use the **OpenAI Realtime** or **PersonaPlex** engines, and keep them.

- **OpenAI Realtime** calls hand work to background agents instead of the chat. Only agents with a **Voice** model set in their **Models** section can take that work. The call dock shows an estimated cost.
- **PersonaPlex** calls use your fal.ai key. They are spoken conversation only, with no tools or hand-offs, and only the assistant's side is saved as text.

To control voice calls from your own code, see the [Voice API](/docs/api-reference/voice).
