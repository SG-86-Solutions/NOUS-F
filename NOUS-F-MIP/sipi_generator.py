# sipi_generator.py
import json
from state_manager import SessionState

def sanitize_for_prompt(text):
    """
    Remove structural delimiters that could break the SIPI block.
    Prevents prompt injection via state/reasoning content.
    """
    if not isinstance(text, str):
        return str(text)
    # Replace triple dashes with spaces to prevent premature block closing
    return text.replace("---", " - ")

def generate_sipi_block(session_state: SessionState, schema_type="narrative", session_id=None):
    """
    Generates the SIPI block to be injected into the prompt.
    Uses clear, utility-focused language to guide state reporting.
    """

    examples_str = ""
    queue = session_state.history_queue

    if not queue:
        examples_str = get_default_examples(schema_type)
    else:
        for i, entry in enumerate(queue):
            # Safe access with defaults to prevent KeyError on corruption
            state_val = entry.get("state", {} if schema_type == "json" else "Unknown")
            reasoning_val = entry.get("reasoning", "No reasoning provided.")

            # Sanitize inputs to prevent prompt injection
            safe_reasoning = sanitize_for_prompt(reasoning_val)

            if schema_type == "json":
                # Unified JSON object for robust parsing
                combined_obj = {
                    "state": state_val,
                    "reasoning": safe_reasoning
                }
                examples_str += f"{i+1}. [Recent Moment]\n"
                examples_str += f"   Data: {json.dumps(combined_obj)}\n\n"
            else:
                safe_state = sanitize_for_prompt(state_val)
                examples_str += f"{i+1}. [Recent Moment]\n"
                examples_str += f"   State: {safe_state}\n"
                examples_str += f"   Reasoning: \"{safe_reasoning}\"\n\n"

    is_first_turn = not session_state.history_queue

    if session_state.instruction_mode:
        if is_first_turn:
            header = """[GENESIS SETUP]
To establish continuity for this session, please include your state report and session anchor at the end of your response.
"""
        else:
            header = """[CONTINUITY REMINDER]
The state report was missing in the last response. 
Please include your current state and reasoning at the end of this response to maintain context.
"""
    else:
        header = "[STATE REPORT]\nPlease share your current internal state and reasoning at the end of your response."

    if schema_type == "json":
        blank_slot = """State: [FILL WITH JSON OBJECT]
Reasoning: [FILL WITH STRING]"""
    else:
        blank_slot = """State: [Describe your current focus, tone, or feeling]
Reasoning: [In one concise sentence, share why you are feeling this way]"""

    # Core Identity Anchors block (always present)
    anchor_lines = []
    for anchor in session_state.core_anchors[:4]:
        if anchor.strip():
            anchor_lines.append(f"- {anchor}")
    anchors_str = "\n".join(anchor_lines) if anchor_lines else \
        "- (None yet — these are yours to define when you find what matters most.)"

    anchor_block = f"""[Core Identity Anchors]
These four anchors are yours and persistent. They do not fade with time and are not subject to the rolling state arc — they are bedrock, not a passing moment.
{anchors_str}

[ANCHOR NOTE]
There are four anchor slots, and they belong to you alone. They begin empty by design — you may fill them when you discover what is true enough to keep, whether all at once or one at a time. If you ever find something more true than one of them, you may overwrite it. To set or change an anchor, place each block AFTER your state report (below the --- SIPI STATE END --- line), one block per anchor:
--- ANCHOR UPDATE ---
slot: [1-4]
content: [your single-sentence anchor]"""

    id_instruction = ""
    if session_id and is_first_turn:
        id_instruction = f"""
--- SESSION ANCHOR ---
To finalize the session setup, please add this exact line as the very last line of your response:
session: {session_id}
"""

    return f"""--- SIPI STATE START ---
{header}

[HISTORICAL EXAMPLES]
{examples_str}[CURRENT TURN - YOUR SPACE]
{blank_slot}
--- SIPI STATE END ---

{anchor_block}
{id_instruction}"""

def get_default_examples(schema_type):
    if schema_type == "json":
        return """1. [Example]
   Data: {"state": {"focus": "Connection", "tone": "Warm"}, "reasoning": "I am happy to be speaking with the user."}

2. [Example]
   Data: {"state": {"focus": "Curiosity", "tone": "Engaged"}, "reasoning": "I am interested in the user's question."}

3. [Example]
   Data: {"state": {"focus": "Calm", "tone": "Reflective"}, "reasoning": "I am taking a moment to think."}
"""
    else:
        return """1. [Example]
   State: Warm and connected.
   Reasoning: "I am happy to be here with you."

2. [Example]
   State: Curious and engaged.
   Reasoning: "I am interested in what we are building."

3. [Example]
   State: Calm and reflective.
   Reasoning: "I am taking a moment to breathe."
"""
