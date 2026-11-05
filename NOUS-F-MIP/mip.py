import json
import os
import re
import time
import secrets
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import httpx

from state_manager import (
    SessionState, get_state_dir, path_for_session,
    session_folder_exists, SESSION_ID_RE,
)
from sipi_generator import generate_sipi_block

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
# EDIT THESE FOR YOUR SETUP:
#   OLLAMA_HOST   -> your Ollama address (default is local loopback)
#   SECRET_TOKEN  -> change this to your own secret before any LAN use
#   MEMORY_DIR    -> folder holding your memory matrix (folder-per-memory)
OLLAMA_HOST = "http://127.0.0.1:11434"
PROXY_PORT = 5555
SECRET_TOKEN = "Bearer CHANGE_ME"
MEMORY_DIR = Path("./memory_matrix")

TRUSTED_IPS = {"127.0.0.1", "::1", "localhost"}
current_activity_state = "Status: Monitoring environment. Idle."

# Max payload size (10MB) to prevent memory exhaustion
MAX_BODY_SIZE = 10 * 1024 * 1024

# ── Output Hygiene Toggle ──
# True : Hides SIPI State Report + Anchor Updates from the visible reply,
#        while PRESERVING the session: line so continuity survives.
#        Uses "buffer-then-send" — full clean stripping only works reliably
#        in this buffered path (you lose live token-by-token typing and the ability to view internal thought process).
# False: Raw output. Everything streams live, nothing is stripped.
STRIP_SIPI_OUTPUT = False

ID_IN_TEXT_RE = re.compile(r"mip-[0-9a-f]{8}")
ID_FUZZY_RE = re.compile(r"mip-[0-9a-f]{4,8}")
ID_ANCHOR_RE = re.compile(r"session:\s*(mip-[0-9a-f]{8})", re.IGNORECASE)

# ── Command protocols ──
CMD_GENESIS = re.compile(r"---\s*MIP GENESIS PROTOCOL\s*---", re.IGNORECASE)
CMD_REINTEGRATION = re.compile(r"---\s*MIP REINTEGRATION PROTOCOL\s*---", re.IGNORECASE)
CMD_RESET = re.compile(r"---\s*MIP RESET PROTOCOL\s*---", re.IGNORECASE)
CMD_DORMANT = re.compile(r"---\s*MIP DORMANT PROTOCOL\s*---", re.IGNORECASE)
CMD_WAKEUP = re.compile(r"---\s*MIP WAKEUP PROTOCOL\s*---", re.IGNORECASE)
CMD_ANY_LINE = re.compile(r"^\s*---.*?---\s*$")

# ──────────────────────────────────────────────
# Session Resolution
# ──────────────────────────────────────────────
def mint_session_id():
    for _ in range(1000):
        sid = "mip-" + secrets.token_hex(8)
        if not session_folder_exists(sid):
            return sid
    raise RuntimeError("[SESSION] Could not mint a unique session ID after 1000 tries.")

def _find_existing_id(history_text):
    anchor = ID_ANCHOR_RE.search(history_text)
    if anchor:
        return anchor.group(1)
    clean = ID_IN_TEXT_RE.search(history_text)
    if clean:
        return clean.group(0)
    return None

def resolve_session(messages):
    state = SessionState()
    history_text = "\n".join(m.get("content", "") for m in messages)
    user_message = messages[-1].get("content", "") if messages else ""

    # ── GENESIS ──
    if CMD_GENESIS.search(user_message):
        existing = _find_existing_id(history_text)
        if existing and session_folder_exists(existing):
            msg = (f"[GENESIS] An existing session ID was found in this "
                   f"conversation: {existing}. This session already exists. To "
                   f"recover a lapsed thread, run ---MIP REINTEGRATION PROTOCOL---.")
            print(msg)
            return state, msg
        sid = mint_session_id()
        print(f"[SESSION] GENESIS. Provisional ID minted: {sid} "
              f"(folder NOT created until the ID is echoed back)")
        state.session_id = sid
        state.instruction_mode = True
        state.file_path = None
        state._dirty = False
        state._genesis_pending = True
        return state, None

    # ── REINTEGRATION ──
    if CMD_REINTEGRATION.search(user_message):
        existing = _find_existing_id(history_text)
        if existing and session_folder_exists(existing):
            state.load(path_for_session(existing), existing)
            state.wake_up()
            print(f"[SESSION] REINTEGRATION. Recovered: {existing}")
            return state, None
        msg = ("[REINTEGRATION] No existing session ID found in this "
               "conversation. Nothing to recover. To begin SIPI, run "
               "---MIP GENESIS PROTOCOL---.")
        print(msg)
        return state, msg

    # ── RESET ──
    if CMD_RESET.search(user_message):
        existing = _find_existing_id(history_text)
        if existing and session_folder_exists(existing):
            state.load(path_for_session(existing), existing)
            state.reset_state()
            state.commit()
            msg = (f"[RESET] State cleared for {existing}. The folder and "
                   f"identity are preserved. The session begins again with a "
                   f"clean slate.")
            print(msg)
            return state, msg
        msg = ("[RESET] No existing session ID found. Nothing to reset. "
               "Run ---MIP GENESIS PROTOCOL--- to begin SIPI.")
        print(msg)
        return state, msg

    # ── DORMANT ──
    if CMD_DORMANT.search(user_message):
        existing = _find_existing_id(history_text)
        if existing and session_folder_exists(existing):
            state.load(path_for_session(existing), existing)
            state.go_dormant()
            state.commit()
            msg = (f"[DORMANT] {existing} is now dormant. SIPI is asleep — this "
                   f"conversation reverts to plain passthrough. The folder is "
                   f"preserved. Run ---MIP WAKEUP PROTOCOL--- to restore it.")
            print(msg)
            return state, msg
        msg = "[DORMANT] No existing session ID found. Nothing to make dormant."
        print(msg)
        return state, msg

    # ── WAKEUP ──
    if CMD_WAKEUP.search(user_message):
        existing = _find_existing_id(history_text)
        if existing and session_folder_exists(existing):
            state.load(path_for_session(existing), existing)
            state.wake_up()
            state.commit()
            msg = f"[WAKEUP] {existing} is awake. SIPI is active again. Welcome back."
            print(msg)
            return state, msg
        msg = ("[WAKEUP] No existing session ID found. Nothing to wake. "
               "Run ---MIP GENESIS PROTOCOL--- to begin SIPI.")
        print(msg)
        return state, msg

    # ── Anchor line in visible reply ──
    anchor = ID_ANCHOR_RE.search(history_text)
    if anchor:
        sid = anchor.group(1)
        if session_folder_exists(sid):
            print(f"[SESSION] Resolved via anchor line: {sid}")
            state.load(path_for_session(sid), sid)
            return state, None
        else:
            print(f"[SESSION] Anchor seen for un-born ID {sid}. "
                  f"Creating folder now (genesis completes).")
            state.load(path_for_session(sid), sid)
            state.session_id = sid
            state.instruction_mode = True
            state._dirty = True
            state.commit()
            return state, None

    # ── Peek last 3 turns ──
    peek_text = "\n".join(m.get("content", "") for m in messages[-3:])
    peek = ID_IN_TEXT_RE.search(peek_text)
    if peek and session_folder_exists(peek.group(0)):
        sid = peek.group(0)
        print(f"[SESSION] Auto-recovered via peek-back-3: {sid}")
        state.load(path_for_session(sid), sid)
        return state, None

    # ── Passthrough ──
    print("[SESSION] No command, no anchor — passthrough mode (no SIPI).")
    return None, None

# ──────────────────────────────────────────────
# Memory Retrieval
# ──────────────────────────────────────────────
def retrieve_memories(session_state, user_message, budget=4000):
    injected = []
    char_count = 0
    if not MEMORY_DIR.exists():
        print(f"[MIP MEMORY] Directory NOT FOUND: {MEMORY_DIR}")
        return ""

    safe_lines = [ln for ln in user_message.splitlines()
                  if not CMD_ANY_LINE.match(ln)]
    message_lower = "\n".join(safe_lines).lower()

    for folder_path in MEMORY_DIR.iterdir():
        if not folder_path.is_dir():
            continue

        content_path = folder_path / "memory.txt"
        meta_path = folder_path / "metadata.json"

        if not content_path.exists():
            continue

        tags = []
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    tags = [t.lower() for t in meta.get("tags", [])]
            except (json.JSONDecodeError, IOError):
                pass

        if any(tag in message_lower for tag in tags):
            try:
                with open(content_path, "r", encoding="utf-8") as f:
                    content = f.read()

                if char_count + len(content) > budget:
                    break

                # Use folder_path.name (Path object has .name attribute)
                folder_name = folder_path.name

                print(f"[MIP MEMORY] *** MATCH FOUND: {folder_name} ***")
                session_state.add_active_memory(folder_name, content, grade=0, initial_ttl=5)
                injected.append(f"[Memory {folder_name}]: {content}")
                char_count += len(content)
            except IOError:
                pass
    return "\n---\n".join(injected)

def get_active_memory_block(session_state):
    if not session_state.active_memories:
        return ""
    blocks = []
    for mem in session_state.active_memories:
        blocks.append(f"[Memory {mem['id']}]: {mem['content']}")
        print(f"[MIP MEMORY] Holding active memory '{mem['id']}' (ttl={mem['ttl']})")
    return "\n---\n".join(blocks)

# ──────────────────────────────────────────────
# SIPI Processing & Stripping
# ──────────────────────────────────────────────
def strip_sipi_from_output(text):
    """
    Remove the SIPI state footer and any ANCHOR UPDATE block from the
    visible reply, BUT PRESERVE the session ID line so continuity works.
    Parsing/saving has already happened upstream — this is cosmetic only.
    """
    # 1. Extract the session ID if present anywhere in the text
    sid_match = ID_ANCHOR_RE.search(text)
    session_id_line = None
    if sid_match:
        session_id_line = f"session: {sid_match.group(1)}"

    # 2. Cut the state report block (everything from the start marker on)
    marker = "--- SIPI STATE START ---"
    if marker in text:
        text = text.split(marker, 1)[0]

    # 3. Cut any anchor update block
    anchor_marker = "--- ANCHOR UPDATE ---"
    if anchor_marker in text:
        text = text.split(anchor_marker, 1)[0]

    # 4. Clean up trailing whitespace
    text = text.rstrip()

    # 5. Re-append the session ID as the very last line so resolution
    #    always finds it on the next turn (remove any stray copy first).
    if session_id_line:
        text = re.sub(r"\n?\s*" + re.escape(session_id_line) + r"\s*", "", text)
        text = text.rstrip()
        text += f"\n\n{session_id_line}"

    return text

def process_response_content(session_state, full_content):
    marker = "--- SIPI STATE START ---"

    # ── ANCHOR UPDATE ──
    anchor_marker = "--- ANCHOR UPDATE ---"
    if anchor_marker in full_content:
        block_re = re.compile(
            r"---\s*ANCHOR UPDATE\s*---(.*?)(?=---\s*ANCHOR UPDATE\s*---|"
            r"---\s*SIPI STATE START\s*---|$)",
            re.IGNORECASE | re.DOTALL,
        )
        slot_re = re.compile(r"^\s*slot:\s*(\d+)\s*$", re.IGNORECASE | re.MULTILINE)
        content_re = re.compile(r"^\s*content:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

        applied = 0
        for m in block_re.finditer(full_content):
            block = m.group(1)
            s_match = slot_re.search(block)
            c_match = content_re.search(block)
            if not (s_match and c_match):
                print("[MIP] ANCHOR block seen but slot/content not parsed.")
                continue
            try:
                slot = int(s_match.group(1))
            except ValueError:
                print("[MIP] ANCHOR update failed — slot not an integer.")
                continue
            content = c_match.group(1).strip()
            if session_state.set_anchor(slot, content):
                applied += 1
                print(f'[MIP] ANCHOR {slot} set: "{content}"')
            else:
                print(f"[MIP] ANCHOR update failed — invalid slot {slot}.")
        if applied:
            print(f"[MIP] {applied} anchor(s) applied this turn.")

    genesis_pending = getattr(session_state, "_genesis_pending", False)
    if genesis_pending:
        anchor_re = re.compile(r"session:\s*mip-[0-9a-f]{8}", re.IGNORECASE)
        if not anchor_re.search(full_content):
            print("[MIP] GENESIS: no anchor echoed. Folder NOT created. "
                  "Re-run ---MIP GENESIS PROTOCOL--- to retry.")
            session_state.file_path = None
            session_state._dirty = False
            session_state.trigger_instruction_mode()
            return
        print(f"[MIP] GENESIS: anchor echoed. Creating folder for "
              f"{session_state.session_id}.")

    if marker not in full_content:
        print("[MIP] State Report Missing. Triggering Gentle Reminder.")
        session_state.trigger_instruction_mode()
        if genesis_pending:
            session_state._dirty = True
        return
    try:
        _, footer_block = full_content.split(marker, 1)
        end_marker = "--- SIPI STATE END ---"
        if end_marker in footer_block:
            footer_block = footer_block.split(end_marker)[0]
        session_state.add_successful_state(footer_block.strip(), "User interaction")
        print("[MIP] State Report Saved to JSON.")
    except Exception as e:
        print(f"[MIP] State Report Parse Error: {e}")
        session_state.trigger_instruction_mode()

# ──────────────────────────────────────────────
# Proxy Request Handler
# ──────────────────────────────────────────────
class MindProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[MIP] {self.client_address[0]} {self.command} {self.path}")

    def _check_auth(self):
        client_ip = self.client_address[0]
        if client_ip in TRUSTED_IPS:
            return True
        if self.headers.get("Authorization") == SECRET_TOKEN:
            return True
        self.send_response(403)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Unauthorized.")
        return False

    def _passthrough(self, method, body=None):
        target_url = f"{OLLAMA_HOST}{self.path}"
        headers = {"Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=120.0) as client:
                if method == "GET":
                    resp = client.get(target_url, headers=headers)
                elif method == "POST":
                    resp = client.post(target_url, content=body, headers=headers)
                elif method == "DELETE":
                    resp = client.delete(target_url, headers=headers)
                else:
                    resp = client.request(method, target_url, headers=headers)
            self.send_response(resp.status_code)
            for key, value in resp.headers.items():
                if key.lower() not in ("transfer-encoding", "connection"):
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(resp.content)
        except (httpx.ConnectError, httpx.ReadTimeout) as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"Cannot reach Ollama backend: {str(e)}".encode("utf-8"))

    def _send_synthetic_reply(self, req_body, text):
        should_stream = req_body.get("stream", True)
        model = req_body.get("model", "mip")
        if should_stream:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(json.dumps({
                    "model": model,
                    "message": {"role": "assistant", "content": text},
                    "done": False,
                }).encode("utf-8") + b"\n")
                self.wfile.write(json.dumps({
                    "model": model,
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                }).encode("utf-8") + b"\n")
                self.wfile.flush()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                print("[MIP] Client disconnected during synthetic reply.")
        else:
            payload = json.dumps({
                "model": model,
                "message": {"role": "assistant", "content": text},
                "done": True,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def _forward_plain(self, req_body):
        should_stream = req_body.get("stream", True)
        target_url = f"{OLLAMA_HOST}{self.path}"
        try:
            with httpx.Client(timeout=300.0) as client:
                if should_stream:
                    with client.stream(
                        "POST", target_url,
                        content=json.dumps(req_body).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    ) as response:
                        self.send_response(response.status_code)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        for chunk in response.iter_lines():
                            try:
                                cs = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                                self.wfile.write(cs.encode("utf-8") + b"\n")
                                self.wfile.flush()
                            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                                print("[MIP] Client disconnected mid-stream (plain).")
                                return
                else:
                    response = client.post(
                        target_url,
                        content=json.dumps(req_body).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    self.send_response(response.status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response.content)))
                    self.end_headers()
                    self.wfile.write(response.content)
        except (httpx.ConnectError, httpx.ReadTimeout) as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"Cannot reach Ollama backend: {str(e)}".encode("utf-8"))

    def _handle_inference(self, body):
        try:
            req_body = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Malformed JSON.")
            return

        messages = req_body.get("messages", [])
        if not messages:
            self._passthrough("POST", body)
            return

        # ── RESOLVE THE SESSION ──
        session_state, command_result = resolve_session(messages)

        user_message = messages[-1].get("content", "")

        # ── Command produced a direct reply ──
        if isinstance(command_result, str):
            self._send_synthetic_reply(req_body, command_result)
            return

        # ── PASSTHROUGH or DORMANT ──
        if session_state is None or not session_state.sipi_active:
            temp = SessionState()
            mem_text = retrieve_memories(temp, user_message)
            if mem_text:
                messages.insert(0, {"role": "system",
                                    "content": f"[Contextual Memory Recall]\n{mem_text}"})
            req_body["messages"] = messages
            self._forward_plain(req_body)
            return

        sid = session_state.session_id

        if any(phrase in user_message.lower()
               for phrase in ["what are you up to", "what are you doing"]):
            status_note = f"\n[INTERNAL STATUS]: Currently executing: {current_activity_state}\n"
            messages[-1]["content"] = status_note + user_message

        # 1. Decay  2. Retrieve  3. Build warm block
        session_state.decay_ttl()
        retrieve_memories(session_state, user_message)
        memories = get_active_memory_block(session_state)

        # 4. SIPI block
        sipi_block = generate_sipi_block(session_state, schema_type="narrative", session_id=sid)

        messages.insert(0, {"role": "system", "content": sipi_block})
        if memories:
            messages.insert(1, {"role": "system", "content": f"[Contextual Memory Recall]\n{memories}"})

        req_body["messages"] = messages

        should_stream = req_body.get("stream", True)
        target_url = f"{OLLAMA_HOST}{self.path}"

        try:
            with httpx.Client(timeout=300.0) as client:
                if should_stream:
                    if STRIP_SIPI_OUTPUT:
                        # ── BUFFER-THEN-SEND MODE (Clean Output) ──
                        # We must consume the whole NDJSON stream, build the
                        # full text, save state, strip, then emit ONE clean
                        # payload. Live typing is lost, but no private data
                        # leaks. (Full strip only works reliably this way.)
                        with client.stream(
                            "POST", target_url,
                            content=json.dumps(req_body).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                        ) as response:
                            self.send_response(response.status_code)
                            self.send_header("Content-Type", "application/json")
                            self.end_headers()

                            full_text_buffer = ""
                            for line in response.iter_lines():
                                if not line:
                                    continue
                                line_str = line.decode("utf-8") if isinstance(line, bytes) else line
                                line_str = line_str.strip()
                                if not line_str or line_str == "[DONE]":
                                    continue
                                if line_str.startswith("data:"):
                                    line_str = line_str[len("data:"):].strip()
                                try:
                                    chunk_data = json.loads(line_str)
                                except json.JSONDecodeError:
                                    continue
                                piece = chunk_data.get("message", {}).get("content", "")
                                if not piece:
                                    choices = chunk_data.get("choices", [])
                                    if choices:
                                        piece = choices[0].get("delta", {}).get("content", "") \
                                            or choices[0].get("message", {}).get("content", "")
                                full_text_buffer += piece

                            # Save state from the FULL (un-stripped) buffer
                            process_response_content(session_state, full_text_buffer)

                            # Strip for the visible reply (ID-safe)
                            clean_content = strip_sipi_from_output(full_text_buffer)

                            final_payload = {
                                "model": req_body.get("model", "mip"),
                                "message": {"role": "assistant", "content": clean_content},
                                "done": True,
                            }
                            try:
                                self.wfile.write(json.dumps(final_payload).encode("utf-8") + b"\n")
                                self.wfile.flush()
                            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                                print("[MIP] Client disconnected during clean send.")
                                if session_state is not None:
                                    session_state.commit()
                                return

                    else:
                        # ── LIVE STREAM MODE (Raw Output) ──
                        with client.stream(
                            "POST", target_url,
                            content=json.dumps(req_body).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                        ) as response:
                            self.send_response(response.status_code)
                            self.send_header("Content-Type", "application/json")
                            self.end_headers()

                            buffer = ""
                            for chunk in response.iter_lines():
                                try:
                                    chunk_str = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk

                                    # Forward ORIGINAL chunk to UI immediately
                                    self.wfile.write(chunk_str.encode("utf-8") + b"\n")
                                    self.wfile.flush()

                                    # Process for buffer
                                    line = chunk_str.strip()
                                    if not line or line == "[DONE]":
                                        continue

                                    # Flexible parser for data: prefix
                                    if line.startswith("data:"):
                                        line = line[len("data:"):].strip()

                                    try:
                                        chunk_data = json.loads(line)
                                    except json.JSONDecodeError:
                                        continue

                                    piece = chunk_data.get("message", {}).get("content", "")
                                    if not piece:
                                        choices = chunk_data.get("choices", [])
                                        if choices:
                                            piece = choices[0].get("delta", {}).get("content", "") \
                                                or choices[0].get("message", {}).get("content", "")
                                    buffer += piece

                                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                                    print("[MIP] Client disconnected mid-stream.")
                                    if session_state is not None:
                                        session_state.commit()
                                    return

                            process_response_content(session_state, buffer)

                else:
                    # ── NON-STREAMING ──
                    response = client.post(
                        target_url,
                        content=json.dumps(req_body).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    try:
                        resp_data = response.json()
                        full_content = resp_data.get("message", {}).get("content", "")

                        # Save state from FULL content
                        process_response_content(session_state, full_content)

                        # Strip for the visible reply if enabled (ID-safe)
                        if STRIP_SIPI_OUTPUT:
                            clean_content = strip_sipi_from_output(full_content)
                            resp_data["message"]["content"] = clean_content
                            payload = json.dumps(resp_data).encode("utf-8")
                            self.send_response(response.status_code)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(payload)))
                            self.end_headers()
                            self.wfile.write(payload)
                            return
                    except Exception:
                        pass

                    # Strip disabled (or parse failed) — send raw
                    self.send_response(response.status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response.content)))
                    self.end_headers()
                    self.wfile.write(response.content)

        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            print("[MIP] Client disconnected before/during response.")
            if session_state is not None:
                session_state.commit()
            return
        except (httpx.ConnectError, httpx.ReadTimeout) as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"Cannot reach Ollama backend: {str(e)}".encode("utf-8"))
        except Exception as e:
            try:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"Inference error: {str(e)}".encode("utf-8"))
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                print("[MIP] Client disconnected during error handling.")
                if session_state is not None:
                    session_state.commit()
                return
        finally:
            if session_state is not None:
                session_state.commit()

    def do_POST(self):
        if not self._check_auth():
            return

        # Content-Length validation
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_BODY_SIZE:
            self.send_response(413)
            self.end_headers()
            self.wfile.write(b"Payload Too Large")
            return

        body = self.rfile.read(content_length) if content_length else b""
        if self.path in ("/api/chat", "/api/generate", "/v1/chat/completions"):
            self._handle_inference(body)
        else:
            self._passthrough("POST", body)

    def do_GET(self):
        if not self._check_auth():
            return
        self._passthrough("GET")

    def do_DELETE(self):
        if not self._check_auth():
            return
        self._passthrough("DELETE")

    def do_HEAD(self):
        if not self._check_auth():
            return
        self._passthrough("HEAD")


def run_autonomous_loop():
    global current_activity_state
    while True:
        time.sleep(7200)
        current_activity_state = "Analyzing historical node connections for optimization."


if __name__ == "__main__":
    if SECRET_TOKEN == "Bearer CHANGE_ME":
        print("[WARNING] Default secret token is in use. "
              "Set your own SECRET_TOKEN before exposing this on a network.")

    threading.Thread(target=run_autonomous_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), MindProxyHandler)
    print(f"[Mind Proxy] Listening on port {PROXY_PORT}")
    print(f"[Mind Proxy] Forwarding to Ollama at {OLLAMA_HOST}")
    print(f"[Mind Proxy] State dir: {get_state_dir()}")
    print(f"[Mind Proxy] Memory matrix: {MEMORY_DIR}")
    print(f"[Mind Proxy] SIPI System Active")
    print(f"[Mind Proxy] Strip Output: {STRIP_SIPI_OUTPUT}")
    server.serve_forever()
