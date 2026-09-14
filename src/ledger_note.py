def render_note(payload: dict, *, max_chars: int = 4000) -> str:
    subject = payload.get("subject_name")
    verbatim_lines = payload.get("verbatim_lines")
    block_reason = payload.get("block_reason")

    subject_text = subject if subject is not None else "NONE"

    if verbatim_lines:
        lines_text = "\n".join("  - " + str(line) for line in verbatim_lines)
    else:
        lines_text = "NONE"

    block_text = block_reason if block_reason is not None else "NONE"

    rendered = (
        "SUBJECT: " + subject_text + "\n"
        "LINES: " + lines_text + "\n"
        "BLOCK: " + block_text
    )

    if len(rendered) > max_chars:
        marker = "...[TRUNCATED]"
        cut = max_chars - len(marker)
        if cut < 0:
            cut = 0
        rendered = rendered[:cut] + marker

    return rendered
