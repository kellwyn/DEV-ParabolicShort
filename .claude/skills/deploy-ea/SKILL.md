---
name: deploy-ea
description: Copies the latest .mq5 EA file to the MetaTrader Experts folder and prompts you to compile. Use after saving changes to any .mq5 file.
---

Deploy the EA file specified in $ARGUMENTS (or detect automatically) to MetaTrader.

1. **Find the EA file** — if $ARGUMENTS is blank, find the most recently modified .mq5:
```bash
find /c/Users/kellw/OneDrive/Dev-RZY -name "*.mq5" -printf "%T@ %p\n" | sort -n | tail -3
```

2. **Find MetaTrader Experts directories**:
```bash
find "/c/Users/kellw/AppData/Roaming/MetaQuotes/Terminal" -type d -name "Experts" 2>/dev/null
```

3. If multiple terminals found, show list and wait for user to specify which one.

4. **Copy** — confirm destination before copying:
```bash
cp "source.mq5" "/path/to/Experts/"
ls -la "/path/to/Experts/" | grep ".mq5"
```

5. Print reminder:
```
Copied. Now in MetaTrader: Navigator → Expert Advisors → right-click the EA → Compile
Verify: Toolbox → Journal tab → 0 errors, 0 warnings
```
