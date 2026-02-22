"""Quick test of AI Brain NLP classification."""
import requests, json

tests = [
    # Original tests
    "how is my system doing",
    "what is eating my cpu",
    "how much battery do i have left",
    "make my screen brighter",
    "mute everything",
    "what windows are open right now",
    "google machine learning papers",
    "play next song",
    "take a pic of my screen",
    "whats in my clipboard",
    # New: Power management
    "shut down my computer",
    "restart the system",
    "put cpu to sleep",
    # New: Window multitasking
    "snap this window to the left",
    "switch to next window",
    "open task manager",
    # New: Conversational
    "hello there",
    "what time is it",
    "what is today's date",
    # New: Web shortcuts
    "search wikipedia for python",
    "open my email",
    "find me a map of tokyo",
    # New: File organization
    "organize my downloads folder",
    "clean up my desktop",
    # New: Notes and timer
    "save a note buy groceries",
    "set a timer for 5 minutes",
    # New: Networking
    "show my wifi info",
    "what is my ip address",
    # New: Keyboard shortcuts
    "undo that",
    "select everything",
    "refresh the page",
]

print("=" * 70)
print("NEXUS OS — AI Brain Test")
print("=" * 70)

for cmd in tests:
    r = requests.post("http://localhost:9000/api/command", json={"command": cmd})
    d = r.json()
    ai = d.get("ai_match", {})
    intent = ai.get("intent", "regex-match")
    conf = ai.get("confidence", 1.0)
    ok = "✓" if d["success"] else "✗"
    print(f"  {ok} [{conf:.0%}] {intent:30s} <- \"{cmd}\"")

print("=" * 70)

# Brain stats
r = requests.get("http://localhost:9000/api/brain")
d = r.json()
print(f"\nBrain active: {d['result']['brain_active']}")
print(f"Learning stats: {json.dumps(d['result']['learning_stats'], indent=2)}")
