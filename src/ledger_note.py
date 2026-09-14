def render_note(payload: dict, *, max_chars: int = 4000) -> str:
    subject = payload.get("subject_name")
    verbatim_lines = payload.get("verbatim_lines")
    block_reason = payload.get("block_reason")

    if subject is None:
        subject_part = "NONE"
    else:
        subject_part = str(subject)

    if verbatim_lines is None or len(verbatim_lines) == 0:
        lines_part = "NONE"
    else:
        lines_part = "\n".join("  - " + str(line) for line in verbatim_lines)

    if block_reason is None:
        block_part = "NONE"
    else:
        block_part = str(block_reason)

    rendered = "SUBJECT: " + subject_part + "\nLINES: " + lines_part + "\nBLOCK: " + block_part

    if len(rendered) > max_chars:
        marker = "...[TRUNCATED]"
        cut = max_chars - len(marker)
        if cut < 0:
            cut = 0
        rendered = rendered[:cut] + marker

    return rendered
