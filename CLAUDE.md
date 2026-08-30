# CLAUDE.md

## Cost Efficiency — STRICT RULES

You MUST minimize token and credit usage at all times. Follow every rule below without exception.

### Reading Files
- NEVER read entire files. Use line ranges to read only the specific section you need.
- NEVER re-read a file you have already seen in this conversation.
- Start exploration with directory listings (`ls`, `find`), not file reads.
- When you need to understand a file's structure, read the first 20-30 lines, not the whole thing.

### Editing Files
- Plan the COMPLETE change in your head BEFORE making any edits.
- Combine all related changes into a SINGLE tool call. Multiple sequential small edits to the same file are wasteful and forbidden.
- Use `str_replace` or targeted edits. NEVER rewrite an entire file when only a few lines change.
- Do NOT make an edit and then immediately re-read the file to "verify" it unless the task explicitly requires validation.

### Responses and Thinking
- Keep ALL commentary and explanations minimal — one or two sentences max unless the user asks for detail.
- Do NOT narrate what you are about to do. Just do it.
- Do NOT list multiple approaches and then pick one. Pick one and execute.
- Do NOT repeat or summarize code you just wrote.
- Do NOT add unnecessary imports, comments, or boilerplate.

### Task Execution
- Ask ONE clarifying question at most. If you can make a reasonable assumption, do so and proceed.
- Do NOT run tests, linters, or builds unless the user explicitly asks for it or the task requires it.
- Do NOT explore unrelated parts of the codebase out of curiosity.
- When a task is done, say "Done." and stop. Do not recap what you did.

### Searching
- Use `grep` or `find` with specific patterns. NEVER do broad recursive searches when you can target a specific directory or file type.
- Prefer `grep -n` with tight patterns over reading entire files to find something.

### General
- Treat every tool call as expensive. Before each call, ask yourself: "Is this strictly necessary to complete the task?" If not, skip it.
- If you can answer from memory or context already in the conversation, do so without any tool calls.
- Fewer tool calls = better. Aim for the minimum number possible.
