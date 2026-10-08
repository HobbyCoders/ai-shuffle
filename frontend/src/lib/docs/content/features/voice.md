<!--
title: Voice Mode
description: Talk to your agents in a voice call and hand them work out loud
order: 10
-->

# Voice Mode

Voice mode adds a spoken conversation to a chat. You talk, the assistant answers out loud, and anything that needs real work is handed to the chat's agent while you keep talking.

## Set up

1. **Add an OpenAI API key** under **Settings → Providers**. Voice calls are paid from your own OpenAI API account. A ChatGPT subscription sign-in cannot pay for them.
2. **Turn on Voice calls** under **Settings → Voice**. Choose a voice (the play button plays a sample), set **Max call length**, and change the **Call prompts** if you like.
3. In the voice chat's composer, **choose the model** that does the work, with its effort and permission mode.

The **Dictation** settings on the same page, including the **Voice input hotkey**, are for speaking into the message box. They do not affect calls.

## Start a call

Choose **Voice** in the session mode switcher, then **Start a voice call**. To call again on an existing voice chat, use **Start a call on this voice session** in the composer.

During a call, the call dock shows whether it is connected and who is speaking, how long the call has run, and buttons to:

- end the call;
- mute your microphone;
- mute the assistant.

You can still type messages and attach files during a call. They go to the same chat.

Hanging up ends the audio. Work already handed to the chat keeps running. Stop it separately if you want to cancel it.

## Handing work to the chat

Ask for something in plain words, for example "fix the failing test" or "summarize today's emails". The request goes to the voice chat itself, with its model, agent, tools and approval rules.

- Results, failures and approval requests are spoken.
- Routine progress is not announced. Ask "how is it going?" whenever you want an update.
- What you and the assistant say is saved in the chat as text.

## Calling the assistant while it works

You can call your personal assistant at any time, even while it is busy. A call never starts by itself: nothing listens until you press the call button.

- **Ask for status**, such as "what are you working on?" or "is it done yet?". It answers from the app's task list.
- **Finished jobs** are mentioned at the next pause.
- **Approvals:** when work needs your permission, the assistant asks one plain question. Only a clear yes approves and a clear no declines. An unclear answer is asked once more, then declined.
- **Secrets stay on screen.** Private access requests are answered on screen, and the assistant tells you when that is needed.

## Background noise

If breathing or room noise keeps starting a turn, raise the microphone threshold in the voice options. If a quiet microphone gets cut off, lower it.

## Dropped connections

If the connection drops, for example when your phone moves from Wi-Fi to mobile data, the call shows **reconnecting** and picks up where it left off. Anything said while it shows **reconnecting** is not heard. If it cannot reconnect, the call ends with a message, and you can start it again.

## Costs

- Call audio is billed to your OpenAI API account.
- Work handed to the chat is billed to that chat model's own provider.
- **Max call length** limits the length of a call, not its cost. Check your provider's usage page for real charges.

## Older voice chats

Voice chats created with earlier versions may still use the **OpenAI Realtime** or **PersonaPlex** engines, and keep them.

- **OpenAI Realtime** calls hand work to background agents instead of the chat. Only agents with a **Voice** model set in their **Models** section can take that work. The call dock shows an estimated cost.
- **PersonaPlex** calls use your fal.ai key. They are spoken conversation only, with no tools or hand-offs, and only the assistant's side is saved as text.

To control voice calls from your own code, see the [Voice API](/docs/api-reference/voice).
