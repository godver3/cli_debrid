### AI Butler Help

The AI Butler is a floating chat assistant powered by your self-hosted OpenClaw instance. It has live access to your cli_debrid data — queues, library, settings, health stats, and watch history — and can answer questions, diagnose issues, suggest fixes, and take actions on your behalf.

---

**Opening and Closing**

Click the robot icon in the bottom-right corner of any page to open or close the chat panel. The panel state (open or closed) is remembered across page loads so it stays where you left it.

*   **Status dot** — The small coloured dot in the header shows connectivity to OpenClaw. Green = connected, red = unreachable, grey = disabled.
*   **Title** — Displays the display name you configured for your AI assistant.

---

**Chat**

Type your question or request in the input box and press Enter (or Shift+Enter for a new line). The assistant responds using real-time streaming.

Examples of things you can ask:

*   *"Why is my queue stuck?"*
*   *"What's my current download count?"*
*   *"Recommend some action movies I haven't seen"*
*   *"Turn on debug logging for the scraper"*
*   *"Run the upgrade scan"*
*   *"How much memory is the app using?"*
*   *"What movies have I added recently?"*

---

**Header Buttons**

*   **+ (New Session)** — Starts a fresh session. Generates a new session ID which clears OpenClaw's server-side memory of your conversation. Your local chat history is also cleared. Use this when you want a completely clean slate.
*   **? (Help)** — Opens this help page.
*   **Cog (Settings)** — Opens the AI Assistant settings modal where you can configure OpenClaw connection details and feature toggles without leaving the page.
*   **Expand / Compress** — Toggles the chat panel between its default size and a full-screen view that fills the browser window. Useful when reading long responses or reviewing recommendations.
*   **Trash (Clear)** — Clears your local chat display and stored message history without resetting the server-side session. Tidies up the chat without losing the AI's memory context.
*   **X (Close)** — Closes the panel without clearing anything.

---

**Action Cards**

The AI Butler can produce interactive cards inline in the chat:

*   **Apply Setting card** — When the AI suggests changing a setting, it outputs an Apply button. Clicking Apply saves the setting immediately. If the change requires a program restart, a Restart button appears.
*   **Add to Library card** — When the AI recommends content, it shows a card with an Add to Library button. Clicking it adds the title to your Wanted list via Trakt lookup. A View in Library link appears for content already in your collection, linking directly to the library page. Uncollected titles link to the Discover page.

---

**What the AI Knows**

The AI receives a system prompt on each message that includes:

*   Queue state (counts, recent errors, program running status)
*   Key settings (scraper config, source config, version config summary)
*   Health metrics (memory, blacklist rate, error rate)
*   Library summary (movie/show counts, recent additions)
*   Your watch history and ratings (when Content Recommendations is enabled)
*   Collected IMDB IDs for recommendation filtering
*   Habit patterns (when Habit Learning is enabled)
*   Full config with tokens redacted (when Share Full Config is enabled)

---

**Settings Modal**

Open via the cog icon in the chat header. Changes are saved immediately.

*   **Enable AI Butler** — Show the chat widget on all pages. Requires a restart to take effect.
*   **OpenClaw URL** — HTTP URL of your OpenClaw instance (e.g. `http://192.168.1.x:18789`).
*   **Bearer Token** — Authentication token for OpenClaw. Leave blank if no auth is configured.
*   **Agent ID** — The OpenClaw agent to use (default: `main`).
*   **Display Name** — The name shown in the chat header. Set this to match your OpenClaw agent's name (configured in its `IDENTITY.md`).
*   **Settings Assistant** — Allow the AI to suggest and apply setting changes with one click.
*   **Proactive Notifications** — Background health checks run periodically and send alerts via your configured notification channels when issues are detected.
*   **Content Recommendations** — Include watch history and library in the AI's context for personalised movie/show suggestions with Add to Library buttons.
*   **Habit Learning** — Record significant actions so the AI can suggest automations.
*   **Share Full Config with AI** — Send your complete config (all tokens permanently redacted) so the AI can diagnose configuration issues.
*   **Health Notifications** — Send proactive alerts to notification channels when health issues are detected.
*   **Health Check Interval** — How often (in seconds) the background health monitor runs. Minimum 300 (5 minutes), default 900 (15 minutes).

---

**Setting Up OpenClaw**

OpenClaw is a self-hosted AI gateway. Full documentation is at [docs.openclaw.ai](https://docs.openclaw.ai).

**Requirements**

*   Docker (recommended) or Node.js 22.16+
*   Minimum 2 GB RAM
*   An API key from an AI provider (Anthropic, OpenAI, Google, or a self-hosted model via Ollama/vLLM)

**Quick Install (Linux / macOS / WSL2)**

```bash
curl -fsSL https://openclaw.ai/install.sh | bash
```

**Windows (PowerShell)**

```powershell
iwr -useb https://openclaw.ai/install.ps1 | iex
```

**Docker (recommended for home servers)**

```bash
git clone https://github.com/openclaw/openclaw.git
cd openclaw
./scripts/docker/setup.sh
```

Or using the pre-built image:

```bash
export OPENCLAW_IMAGE="ghcr.io/openclaw/openclaw:latest"
./scripts/docker/setup.sh
```

**After Installation — Onboarding**

Run the onboarding wizard to configure your model provider, gateway port, and auth:

```bash
openclaw onboard --install-daemon
# or with Docker:
docker compose run --rm openclaw-cli onboard
```

The wizard walks you through:

1.  Selecting an AI provider and entering your API key
2.  Setting the gateway port (default `18789`)
3.  Choosing an authentication mode (token recommended)
4.  Installing as a background daemon

**Access the Dashboard**

```bash
openclaw dashboard
# or with Docker:
docker compose run --rm openclaw-cli dashboard --no-open
```

Then open `http://127.0.0.1:18789/` in your browser.

**Enable Required HTTP Endpoints**

Before cli_debrid can communicate with OpenClaw, two HTTP endpoints must be enabled in the OpenClaw dashboard. Without these, the connection will fail silently.

1.  Open the OpenClaw dashboard (`http://your-openclaw-url/`)
2.  Go to **Settings → Infrastructure → Gateway HTTP API → Gateway HTTP Endpoints**
3.  Enable **Chat Completions**
4.  Enable **Responses**
5.  Save the changes

These endpoints expose the OpenAI-compatible API that cli_debrid uses to send and receive messages.

**Install the cli_debrid Skill**

The skill file tells OpenClaw how to call back into cli_debrid's tool API so it can check queue status, search your library, trigger tasks, and more.

1.  Go to **Settings → Additional Settings → AI Assistant**
2.  Click **Download OpenClaw Skill File**
3.  Drop the downloaded `cli_debrid_skill.md` into your OpenClaw workspace directory (default: `~/.openclaw/workspace/` or the `workspace/` folder in your Docker volume)

The skill file is pre-filled with your cli_debrid URL and bearer token.

**Connect cli_debrid to OpenClaw**

1.  Open the AI Butler settings cog in the chat header
2.  Set **OpenClaw URL** to the URL OpenClaw is listening on (must be reachable from inside the cli_debrid container)
3.  Set **Bearer Token** to match the token configured in OpenClaw
4.  Set **Agent ID** to `main` (or the ID of the agent you configured)
5.  Set **Display Name** to your agent's name (found in its `IDENTITY.md` file in the workspace)
6.  Click **Save** — the status dot will turn green if the connection succeeds

---

**Important Notes**

*   The OpenClaw URL must be reachable from **inside the cli_debrid Docker container**, not just from your browser. If you use a reverse proxy or Tailscale, use the internal/container-reachable address.
*   OpenClaw handles all AI provider credentials — cli_debrid never sees your API keys.
*   Session memory is scoped per session ID. Starting a new session (+) clears the AI's memory of your conversation but does not affect your library or settings.
*   The AI cannot delete media items or modify the database directly. It can trigger program actions (start/stop queue, run tasks, scan library) but these are the same actions available in the UI.

---

**Tips**

*   Use **New Session** (+) when switching topics to avoid prior context influencing responses.
*   Use **Expand** to go full-screen when reviewing long recommendation lists or detailed diagnostics.
*   For setting changes, the AI's Apply cards are safer than editing settings manually — the AI explains the reason before applying.
*   If recommendations include titles you already have, start a new session to clear accumulated context drift.
*   If the status dot is red, check that OpenClaw is running and that the URL and token in settings are correct. You can verify connectivity with `openclaw gateway status` or `curl http://your-openclaw-url/healthz`.
