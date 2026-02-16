<!--
title: Signal
description: Connect AI Shuffle to Signal using signal-cli for privacy-focused encrypted messaging
order: 6
-->

# Signal Connector

## Overview

The Signal connector integrates your AI Shuffle agent with Signal, the privacy-focused messaging platform known for its end-to-end encryption. Unlike most other connectors that use cloud APIs, the Signal connector operates through **signal-cli**, an open-source command-line client for Signal. This means all messages are handled locally on your server with full end-to-end encryption preserved.

Because Signal does not provide an official bot API, this connector requires a dedicated phone number registered with signal-cli. The phone number acts as the bot's identity on the Signal network. Messages are sent and received through signal-cli's daemon mode or JSON-RPC interface, giving you a fully self-hosted, privacy-preserving bot without any cloud service dependency.

## Prerequisites

- A **dedicated phone number** for the bot (can be a VoIP number that receives SMS or voice calls for verification)
- **signal-cli** installed on the server running AI Shuffle ([GitHub: AsamK/signal-cli](https://github.com/AsamK/signal-cli))
- Java Runtime Environment (JRE) 17 or newer (required by signal-cli)
- AI Shuffle backend running on the same machine as signal-cli (or with network access to the signal-cli daemon)
- Linux, macOS, or Windows (WSL recommended on Windows)

## Setup Guide

### Step 1: Install signal-cli

**Option A: Download the release archive (recommended)**

1. Go to the [signal-cli releases page](https://github.com/AsamK/signal-cli/releases) and download the latest release archive.
2. Extract it to a directory on your server:
   ```bash
   # Download the latest release (check for the actual latest version)
   wget https://github.com/AsamK/signal-cli/releases/download/v0.13.4/signal-cli-0.13.4-Linux.tar.gz

   # Extract to /opt
   sudo tar xf signal-cli-0.13.4-Linux.tar.gz -C /opt

   # Create a symlink for convenience
   sudo ln -sf /opt/signal-cli-0.13.4/bin/signal-cli /usr/local/bin/signal-cli
   ```

**Option B: Install via package manager**

On Arch Linux:
```bash
yay -S signal-cli
```

On macOS with Homebrew:
```bash
brew install signal-cli
```

**Verify the installation:**
```bash
signal-cli --version
# Output: signal-cli 0.13.4
```

### Step 2: Register a Phone Number

Register the dedicated phone number with Signal. You can use SMS or voice call verification.

**SMS verification:**
```bash
signal-cli -u +14155551234 register
```

**Voice call verification (if SMS is not available):**
```bash
signal-cli -u +14155551234 register --voice
```

Replace `+14155551234` with your actual phone number in international E.164 format.

### Step 3: Verify the Registration

After receiving the verification code (via SMS or voice call), complete the registration:

```bash
signal-cli -u +14155551234 verify 123-456
```

Replace `123-456` with the actual verification code you received.

**Set a profile name (recommended):**
```bash
signal-cli -u +14155551234 updateProfile --given-name "AI" --family-name "Shuffle"
```

### Step 4: Test signal-cli

Verify that signal-cli is working by sending a test message:

```bash
signal-cli -u +14155551234 send -m "Hello from AI Shuffle!" +15551234567
```

Replace `+15551234567` with your personal phone number. You should receive the message on your phone.

### Step 5: Start signal-cli in Daemon Mode

The connector communicates with signal-cli through its daemon mode, which runs as a background process and provides a JSON-RPC or DBus interface:

**JSON-RPC over TCP (recommended):**
```bash
signal-cli -u +14155551234 daemon --socket --tcp localhost:7583
```

**JSON-RPC over Unix socket:**
```bash
signal-cli -u +14155551234 daemon --socket /var/run/signal-cli/socket
```

**Run as a systemd service (production):**

Create `/etc/systemd/system/signal-cli.service`:
```ini
[Unit]
Description=signal-cli daemon
After=network.target

[Service]
Type=simple
User=signal-cli
ExecStart=/usr/local/bin/signal-cli -u +14155551234 daemon --socket --tcp localhost:7583
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable signal-cli
sudo systemctl start signal-cli
```

### Step 6: Configure the Connector in AI Shuffle

1. Open the AI Shuffle web UI and navigate to **Settings > Connectors**.
2. Click **Add Connector** and select **Signal**.
3. Fill in the configuration fields:
   - **Phone Number**: The registered phone number in E.164 format (e.g., `+14155551234`)
   - **signal-cli Path** (optional): Path to the signal-cli binary if not on the system PATH
4. Click **Save** and then **Enable** the connector.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `phone_number` | string | Yes | The phone number registered with signal-cli in international E.164 format (e.g., `+14155551234`). This is the identity of the bot on the Signal network. |
| `signal_cli_path` | string | No | Absolute path to the signal-cli binary. Defaults to `signal-cli` (assumes it is on the system PATH). Set this if signal-cli is installed in a non-standard location, e.g., `/opt/signal-cli-0.13.4/bin/signal-cli`. |

## Capabilities

The Signal connector supports the following capabilities:

| Capability | Supported | Notes |
|------------|-----------|-------|
| Typing indicator | Yes | Shows a typing indicator to the recipient while the agent processes a response |
| Reactions | Yes | Can react to messages with any emoji (Signal supports single-emoji reactions per message per user) |
| Rich text | No | Signal does not support text formatting in messages. All messages are sent as plain text. |
| File upload | Yes | Send images, documents, voice notes, and other files as attachments |
| Buttons | No | Signal does not support interactive buttons |
| Threads | No | Signal does not have a thread model |
| End-to-end encryption | Yes | All messages are end-to-end encrypted by default (this is inherent to Signal) |

**Message Length Limit:** 4,096 characters per message. Messages exceeding this limit are automatically split.

## Troubleshooting

### signal-cli fails to start or register

- **Java not installed or wrong version.** signal-cli requires JRE 17+. Check your Java version:
  ```bash
  java -version
  ```
  Install or update Java if needed:
  ```bash
  # Ubuntu/Debian
  sudo apt install openjdk-17-jre

  # macOS
  brew install openjdk@17
  ```

- **Phone number already registered.** If the phone number is already registered on another device, signal-cli registration will take over the number and deregister the other device. Use a dedicated phone number that is not your personal Signal account.

### Bot does not receive messages

- **signal-cli daemon not running.** Verify the daemon is running:
  ```bash
  systemctl status signal-cli
  # Or check if the process is running:
  ps aux | grep signal-cli
  ```
- **Firewall blocking the socket.** If using TCP mode, ensure the port (e.g., 7583) is accessible from the AI Shuffle backend process.
- **Trust issues.** Signal requires identity key trust. If a contact has changed their safety number, you may need to trust the new key:
  ```bash
  signal-cli -u +14155551234 trust -a +15551234567
  ```

### "Unregistered user" error when sending messages

- The recipient phone number must be registered with Signal. The bot can only communicate with active Signal users.
- Verify the phone number format uses the E.164 international format (e.g., `+14155551234`, not `4155551234`).

### Messages are delayed or slow

- signal-cli is a command-line tool and can be slower than cloud-based APIs. Each message send/receive involves cryptographic operations. For better performance:
  - Use daemon mode (Step 5) instead of invoking signal-cli per message.
  - Ensure the server has adequate CPU resources for the encryption operations.
  - Consider running signal-cli in its native image mode (GraalVM) for faster startup.

### Registration fails with captcha requirement

- Signal may require a captcha for registration from certain IPs. If you see a captcha error, you need to solve the captcha in a browser and pass the token to signal-cli:
  ```bash
  signal-cli -u +14155551234 register --captcha "signalcaptcha://signal-recaptcha-v2.example..."
  ```
  Follow the instructions in the [signal-cli documentation](https://github.com/AsamK/signal-cli/wiki/Registration-with-captcha) for solving the captcha.

### "Untrusted identity" warnings

- When a contact reinstalls Signal or changes devices, their identity key changes. By default, signal-cli may block messages to untrusted identities. To trust all identities automatically (less secure but more practical for a bot):
  ```bash
  signal-cli -u +14155551234 trust -a +15551234567
  ```
  You can also configure signal-cli to trust new identities automatically using the `--trust-new-identities always` flag in daemon mode.
