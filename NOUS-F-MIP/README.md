# MIP (Memory Injection Proxy)

MIP is a port-based pipeline utility for attaching external memory databases to LLM interactions. It provides a persistent, lightweight retrieval system and a self-state updating mechanism to maintain conversational relevance across sessions.

---

## 💡 Philosophy: Drop-and-Go
This project is released as a reference implementation and toolkit. It is provided **as-is**.

* **No Support:** I am not maintaining this for specific use cases or integrations. If it breaks your setup, you have the source code—fix it, fork it, or leave it.
* **Self-Reliance:** If you encounter issues, utilize your local LLM to debug or adapt the code. The architecture is designed to be transparent and modifiable.
* **Open Information:** Code is information. Use it as you see fit. Attribution is appreciated but not enforced beyond the license requirements.

---

## 🏗️ Architecture
MIP is modularly split into three components to ensure maintainability and independent testing:

1. **`mip.py`**: The core proxy handler. Manages HTTP traffic, port injection, session resolution, and orchestration between the client and the LLM.
2. **`state_manager.py`**: Handles session state, TTL (Time-To-Live) decay, and per-session persistence. Each session lives in its own folder under the state directory; relevant context is retained while older data expires.
3. **`sipi_generator.py`**: Formats the SIPI (Self-Initiated Prompt Injection) block string, injecting current state and identity anchors into the prompt context.

---

## 🛠️ Installation & Setup
MIP is a local proxy. It sits between your Chat UI and your LLM backend (Ollama). To get it running, you need three things:
* **Python 3.8+** (to run MIP)
* **Ollama** (running locally with a model pulled)
* **A Chat UI** (like Open WebUI, LibreChat, or any Ollama-compatible frontend)

### Step 1: Prerequisites
Ensure you have Python 3.8+ installed. Ensure Ollama is installed and running. You should be able to chat with a model normally before installing MIP.

Pull a model if you haven't already:
```bash
ollama pull gemma2
# Or any other model you prefer
```

### Step 2: Install MIP
Clone the repository and install the single dependency (`httpx`).
```bash
git clone https://github.com/yourusername/mip-proxy.git
cd mip-proxy
pip install httpx
```

### Step 3: Configure `mip.py`
Open `mip.py` in a text editor. Look for the Configuration block near the top of the file (marked `# EDIT THESE FOR YOUR SETUP`).

You must verify these settings match your local environment:
```python
# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
# EDIT THESE FOR YOUR SETUP:
OLLAMA_HOST = "http://127.0.0.1:11434"   # Your local Ollama address
PROXY_PORT = 5555                        # The port MIP will listen on
SECRET_TOKEN = "Bearer CHANGE_ME"        # Change this to a secure key
MEMORY_DIR = Path("./memory_matrix")     # Path to your memory folder
```

* **`OLLAMA_HOST`**: Must point to where Ollama is running. Default is `127.0.0.1:11434`.
* **`PROXY_PORT`**: This is the port your Chat UI will connect to. Default is `5555`. You can change this if 5555 is taken, but remember to update your Chat UI settings accordingly.
* **`SECRET_TOKEN`**: Change this. MIP uses this to authenticate non-local requests. If you leave it as `Bearer CHANGE_ME`, MIP will print a startup warning. *(Connections from 127.0.0.1/localhost are trusted and skip the token; the token only matters for LAN access — see the security notes below.)* Set it to something random like `"Bearer MySecretKey123"`.
* **`MEMORY_DIR`**: The path to the folder where MIP stores/retrieves memories. The default `./memory_matrix` is relative to the folder you run `mip.py` from. You can also use an absolute path (e.g., `Path("D:/MyMemories")`). If the folder does not exist, memory retrieval is simply skipped — MIP will log `Directory NOT FOUND` and continue running normally.

📂 **Memory Folder Structure:** `MEMORY_DIR` should contain one subfolder per memory. Each subfolder needs a `memory.txt` (the content) and a `metadata.json` containing a `"tags"` array. A memory is injected when one of its tags appears in your message text. An example of the folder structure along with test files are included.

### Step 4: Run the Proxy
In your terminal, inside the `mip-proxy` folder, run:
```bash
python mip.py
```
You should see output confirming it is listening:
```text
[Mind Proxy] Listening on port 5555
[Mind Proxy] Forwarding to Ollama at http://127.0.0.1:11434
[Mind Proxy] State dir: <your state directory>
[Mind Proxy] Memory matrix: memory_matrix
[Mind Proxy] SIPI System Active
[Mind Proxy] Strip Output: False
```
*(If you left the token at its default, you'll also see a `[WARNING] Default secret token is in use.` line first.)*

Keep this terminal window open. MIP must be running for your chat to work.

### Step 5: Point Your Chat UI to MIP (Critical)
This is the most important step. Your Chat UI (e.g., Open WebUI) is currently pointing directly at Ollama (`11434`). You need to point it at MIP (`5555`) instead.

#### For Open WebUI Users:
1. Go to **Admin Panel** > **Settings** > **Connections**.
2. Find the Ollama connection.
3. Change the URL from `http://localhost:11434` to `http://localhost:5555`.
4. Click **Save**.

> 📌 **Note:** Because MIP trusts localhost connections, you can leave the API Key blank when MIP and your UI are on the same machine. Only set the API Key (to match your `SECRET_TOKEN`) if you connect to MIP from a different device on your network.

#### For Other UIs (LibreChat, SillyTavern, etc.):
Look for the "API Endpoint" or "Base URL" setting in your model configuration.
* **Old Value:** `http://localhost:11434`
* **New Value:** `http://localhost:5555`

#### How the Pipeline Flows:
When you send a message, it now flows dynamically through MIP:
```text
You ➔ Chat UI ➔ MIP (Port 5555) ➔ MIP injects SIPI/Memory ➔ Ollama (Port 11434) ➔ MIP processes state ➔ Chat UI ➔ You
```
By default, MIP runs in raw output mode (`STRIP_SIPI_OUTPUT = False`), so the SIPI state block stays visible in the reply and the session anchor is human-readable. If you set `STRIP_SIPI_OUTPUT = True`, MIP hides the state/anchor blocks from the visible reply (while preserving the `session:` line for continuity)—but note this forces a buffered, non-live send, so you lose token-by-token streaming along withthe ability to view internal thought processes in that mode.

### Step 6: Verify It’s Working
1. Open your Chat UI.
2. Select your model.
3. Send a test message: `"Hello, this is a test."`
4. Check your MIP terminal window. You should see logs like:
```text
[MIP] 127.0.0.1 POST /api/chat
[SESSION] No command, no anchor — passthrough mode (no SIPI).
```
Seeing the `[SESSION]` and `[MIP]` lines confirms traffic is flowing through MIP correctly. On a plain message like this, MIP is in passthrough mode—it does not inject SIPI or save a state report yet. That only happens after you run Genesis (next step). If your `MEMORY_DIR` has matching tagged memories, you'll also see `*** MATCH FOUND ***` lines.

### Step 7: Initialize SIPI (The Genesis Protocol)
By default, MIP runs in Passthrough Mode. It will inject memories if tags match, but it will not create a persistent session ID or inject the SIPI state block. This is by design: **SIPI is opt-in.**

To activate full SIPI (persistent identity, state tracking, and session isolation), you must initiate the Genesis Protocol:
1. Open your Chat UI (connected to Port 5555).
2. Start a new chat session.
3. Send only this command as your first message:
```text
---MIP GENESIS PROTOCOL---
```
4. Wait for the response.

MIP mints a provisional session ID and instructs the model to echo it back. The folder is not created yet—this is the "genesis quarantine." 

⚠️ **Crucial:** At the very bottom of the response, you should see a line like: `session: mip-xxxxxxxx`. The folder for this session is created the moment that ID is echoed back and seen by MIP. From this point on, the ID is your session anchor, stored in a folder under the state directory.

#### What happens next?
* Every subsequent message in this chat will include the SIPI state block (injected into the prompt; visible in the reply unless you enable strip mode).
* The AI maintains continuity across turns using the `session: mip-xxxxxxxx` anchor.
* If you close the chat and come back later, simply continue chatting. MIP auto-detects the ID in the recent history (it checks for the anchor line, then peeks back the last 3 turns) and re-attaches the session context dynamically.


## Troubleshooting
* **"Connection Refused" in Chat UI:** Make sure `python mip.py` is still running in your terminal. Make sure the port in your Chat UI matches `PROXY_PORT` in `mip.py`.
* **"Unauthorized" (403) Error:** This only happens for non-localhost connections. If you are running MIP and the Chat UI on the same machine, localhost is trusted automatically. If you're connecting from another device, set the same value in `SECRET_TOKEN` and your UI's API Key field.
* **"Cannot reach Ollama backend" (502):** Ollama isn't reachable at `OLLAMA_HOST`. Confirm Ollama is running and the address/port are correct.
* **No Memory Injection:** Ensure your `MEMORY_DIR` exists and contains subfolders with both `metadata.json` (with a "tags" array) and `memory.txt`. A memory only injects when one of its tags appears in your message.

## Security Notes
* **Network binding:** MIP binds to `0.0.0.0` by default, meaning it is reachable from other devices on your local network (useful for connecting from a phone or second PC). If you want MIP to be accessible only from this machine, change the bind address in the `__main__` block from `"0.0.0.0"` to `"127.0.0.1"`. If you keep `0.0.0.0`, set a real `SECRET_TOKEN` so LAN requests are authenticated.
* **Keep your data out of version control:** Add a `.gitignore` so you never accidentally commit your own session data or memories when forking or pushing. At minimum:

```gitignore
# MIP local data — never commit
_state/
memory_matrix/
*.json
```

## Appreciation
This tool is free. However, if it helps you build something successful, a small token of appreciation is welcomed to help sustain the hardware infrastructure behind this project. This is entirely optional.

* [GitHub Sponsors Link]
* BitCoin - `bc1qaw9hzjj5v77a5t7xrmyst6wu82f7rzmuxslwzx`
* Ethereum - `0xf0A85A2c62F2E7F5d64Ce190208fB0a496b8517d`

## Disclaimer
Results may vary. The SIPI mechanism is not definitive and depends on the underlying LLM's handling of injected context. Positive results have been observed in maintaining conversational continuity, but failure cases are possible depending on model architecture and prompt complexity.
