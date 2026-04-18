"""Fix the misplaced hide_block: move it from inside main() to module-level above main()."""
import re

p = "bot.py"
s = open(p).read()

# Detect the misplaced block (anchor: '# ── Persistent reply keyboard helpers (bottom menu) ──')
START_MARK = "# ── Persistent reply keyboard helpers (bottom menu) ──"
END_MARK = "    # Register all \"/cmd\" handlers"

if START_MARK not in s:
    print("Block not found; nothing to do.")
    raise SystemExit(0)

start_idx = s.index(START_MARK)
end_idx = s.index(END_MARK, start_idx)

# Capture everything from START_MARK up to (but not including) END_MARK
misplaced = s[start_idx:end_idx]
print(f"Removing {len(misplaced)} chars of misplaced code")

# Remove it from current location
s = s[:start_idx] + s[end_idx:]

# Now place it at module level: just BEFORE `def main():`
def_main = "def main():"
if def_main not in s:
    raise SystemExit("def main() not found!")

# Wrap the moved block with clear separators and insert before def main()
moved_block = "\n\n" + misplaced.rstrip() + "\n\n\n"
mi = s.index(def_main)
s = s[:mi] + moved_block + s[mi:]

open(p, "w").write(s)
print("OK relocated hide_block to module level above def main()")
