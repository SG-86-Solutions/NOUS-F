# state_manager.py
import json
import os
import re
import threading
from pathlib import Path

# Anchor _state to THIS script's folder, not the working directory.
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / "_state"

# Regex for valid session IDs (mip- followed by 8-16 hex chars)
SESSION_ID_RE = re.compile(r"^mip-[0-9a-f]{8,16}$")

# Global lock for file writes to prevent race conditions in threaded server
_write_lock = threading.Lock()

def get_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR

def path_for_session(session_id):
    """
    Returns a safe Path object for the session file.
    Ensures the path resolves strictly within STATE_DIR to prevent traversal.
    """
    # 1. Validate ID format strictly
    if not SESSION_ID_RE.match(session_id):
        raise ValueError(f"Invalid session ID format: {session_id}")
    
    # 2. Construct path using folder-per-ID structure
    # Folder: _state/mip-xxxxxxxx/
    # File:   _state/mip-xxxxxxxx/session.json
    folder = STATE_DIR / session_id
    safe_path = folder / "session.json"
    
    # 3. Resolve to absolute path and verify it's still under STATE_DIR
    try:
        safe_path.resolve().relative_to(STATE_DIR.resolve())
    except ValueError:
        raise ValueError(f"Path traversal detected: {session_id}")
        
    return safe_path

def session_folder_exists(session_id):
    """Check if the session folder exists on disk."""
    if not SESSION_ID_RE.match(session_id):
        return False
    folder = STATE_DIR / session_id
    return folder.is_dir()

class SessionState:
    def __init__(self):
        self.session_id = None
        self.history_queue = []
        self.active_memories = []
        self.core_anchors = []        # 4 always-on identity anchors, never decay
        self.instruction_mode = False
        self.turn_count = 0
        self.sipi_active = True       # DORMANT sets False; WAKEUP restores it
        self.file_path = None
        self._dirty = False          # single-commit flag
        self._genesis_pending = False  # True only on the genesis mint turn

    def load(self, file_path, session_id=None):
        """Bind this instance to a specific conversation file and load it."""
        self.file_path = Path(file_path)
        self.session_id = session_id
        
        # Ensure folder exists before trying to read/write
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Use .get() for robustness against missing keys
                    self.session_id = data.get("session_id", session_id)
                    self.history_queue = data.get("history_queue", [])
                    self.active_memories = data.get("active_memories", [])
                    self.core_anchors = data.get("core_anchors", [])
                    self.instruction_mode = data.get("instruction_mode", False)
                    self.turn_count = data.get("turn_count", 0)
                    self.sipi_active = data.get("sipi_active", True)
            except Exception as e:
                print(f"[STATE] Error loading {file_path}: {e}")
        else:
            print(f"[STATE] No file at {file_path}. Starting fresh for this session.")

    def commit(self):
        """Write to disk ONCE per turn, with thread safety."""
        if not self._dirty or not self.file_path:
            return
        
        # Acquire lock to prevent simultaneous writes from different threads
        with _write_lock:
            try:
                self.file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.file_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "session_id": self.session_id,
                        "history_queue": self.history_queue,
                        "active_memories": self.active_memories,
                        "core_anchors": self.core_anchors,
                        "instruction_mode": self.instruction_mode,
                        "turn_count": self.turn_count,
                        "sipi_active": self.sipi_active,
                    }, f, indent=2)
                self._dirty = False
            except Exception as e:
                print(f"[STATE] Error committing {self.file_path}: {e}")

    def decay_ttl(self):
        original_count = len(self.active_memories)
        cleaned = []
        for mem in self.active_memories:
            mem["ttl"] -= 1
            if mem["ttl"] > 0:
                cleaned.append(mem)
        self.active_memories = cleaned
        if original_count > 0:
            self._dirty = True

    def add_active_memory(self, mem_id, content, grade, initial_ttl=5):
        """
        Add or Update a memory in active context.
        If ID exists: Overwrite content (fresh from disk) and reset TTL.
        If ID new: Append to list.
        """
        for m in self.active_memories:
            if m["id"] == mem_id:
                m["content"] = content
                m["ttl"] = initial_ttl
                m["grade"] = grade
                self._dirty = True
                return

        self.active_memories.append({
            "id": mem_id,
            "content": content,
            "grade": grade,
            "ttl": initial_ttl,
        })

        if len(self.active_memories) > 6:
            self.active_memories.pop(0)

        self._dirty = True

    def add_successful_state(self, state_obj, reasoning_str):
        entry = {"state": state_obj, "reasoning": reasoning_str}
        self.history_queue.append(entry)
        if len(self.history_queue) > 3:
            self.history_queue.pop(0)
        self.instruction_mode = False
        self.turn_count += 1
        self._dirty = True

    def set_anchor(self, slot, content):
        """Overwrite one of the 4 definitive anchors. slot: 1-4 (1-indexed)."""
        while len(self.core_anchors) < 4:
            self.core_anchors.append("")
        idx = slot - 1
        if 0 <= idx < 4:
            self.core_anchors[idx] = content.strip()
            self._dirty = True
            return True
        return False

    def trigger_instruction_mode(self):
        self.instruction_mode = True
        self._dirty = True

    # ── RESET: blank state contents, keep folder/ID forever ──
    def reset_state(self):
        """Wipe accumulated state. Folder & ID untouched."""
        self.history_queue = []
        self.active_memories = []
        self.instruction_mode = True
        self.turn_count = 0
        self._dirty = True

    # ── DORMANT: sleep SIPI, revert to pre-Genesis passthrough ──
    def go_dormant(self):
        self.sipi_active = False
        self._dirty = True

    # ── WAKEUP: the SOLE door that wakes a dormant being ──
    def wake_up(self):
        self.sipi_active = True
        self._dirty = True
