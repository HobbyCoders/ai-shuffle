<!--
title: Browser extension
description: Chat from a side panel in Chrome or Edge and let your agents work in that browser, one tab group per chat
order: 14
-->

# Browser extension

The AI Shuffle extension for Chrome and Microsoft Edge does two things:

- **A side panel for chatting.** Your chats open in a panel next to the page you are on. You can start new chats and keep going with old ones there, just like on your phone.
- **Your browser for your agents.** An agent can work in this browser instead of the built-in one. It sees the sites you are already signed in to, and you can watch every step. Each chat's tabs go in their own tab group, named after the chat, so the agent's tabs stay apart from yours.

It works on Windows, macOS and Linux, in Chrome 125 or later and in current Edge.

## Install and sign in

1. Install **AI Shuffle** from the Chrome Web Store or Edge Add-ons.
2. Click the AI Shuffle icon in the toolbar. The side panel opens.
3. Choose **Sign in** and sign in with your AI Shuffle account.

The panel lists the workers that are linked to your account. If it says **No workers on this account**, link a worker under **Settings → Account** in AI Shuffle, then choose **Reload workers**.

Each worker shows this browser under **Settings → Devices**, where you can remove it at any time.

## Let an agent use your browser

A chat you start from the side panel uses this browser on its own. For any other chat:

1. Open the chat's **Browser** panel.
2. In the browser menu at the top, choose your browser in place of **Built-in browser**.

From the agent's next step, it works in your browser. Its tabs open in a blue tab group with the chat's name. It only opens, reads and controls tabs in that group. Tabs you open yourself stay private, and so do your other groups.

While an agent is working, Chrome or Edge shows a bar saying that **AI Shuffle started debugging this browser**. That is how the extension controls the tab. If you choose **Cancel** on the bar, the agent loses that tab.

To switch a chat back, choose **Built-in browser** in the same menu.

## Turn it off

- **Stop agents using this browser** in the side panel stops every agent at once. The panel itself keeps working. Choose **Let agents use this browser** to turn it back on.
- Closing a tab takes it away from the agent. It can still open new tabs in its group while the chat uses your browser.
- **Sign out** disconnects the browser from all your workers.

## Things to know

- The agent works with your real sign-ins. Anything you can do on a site, the agent can do there. Watch what it does, and keep it away from accounts you would not hand to someone else.
- The browser has to stay open while its agent works. If you close it, the agent's browser steps fail until you open it again or switch the chat to the built-in browser.
- If a worker restarts, chats using your browser go back to the built-in one. Choose your browser again in the Browser panel.
