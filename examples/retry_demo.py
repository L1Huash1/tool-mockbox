"""Exercise real Python retry code against synthetic local tool fixtures."""

from pathlib import Path

from tool_mockbox import MockToolbox, MockToolError, UnexpectedCall


box = MockToolbox.from_file(Path(__file__).with_name("flaky-search.json"))
for attempt in range(1, 4):
    try:
        value = box.call("search_notes", {"query": "release"})
    except UnexpectedCall:
        raise  # A fixture mismatch is not a temporary upstream failure.
    except MockToolError as error:
        print(f"Attempt {attempt}: scripted {error.code}")
    else:
        print(f"Attempt {attempt}: success — {value['notes'][0]}")
        break
else:
    raise RuntimeError("Retry budget exhausted")
print("This is scripted retry code, not an LLM agent or model benchmark.")
