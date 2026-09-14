def summarize_windows(windows: list, *, min_seconds: int = 0) -> dict:
    if min_seconds < 0:
        raise ValueError("min_seconds must be >= 0")

    # Validate and copy without mutating the input.
    pairs = []
    for pair in windows:
        start, end = pair[0], pair[1]
        if start > end:
            raise ValueError("window start must not be greater than end")
        pairs.append((start, end))

    # Sort by start, then by end.
    pairs.sort(key=lambda p: (p[0], p[1]))

    # Merge overlapping or touching windows.
    merged = []
    for start, end in pairs:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])

    # Filter AFTER merging: drop windows shorter than min_seconds.
    coverage = [w for w in merged if (w[1] - w[0]) >= min_seconds]

    if not coverage:
        return {
            "count": 0,
            "total_seconds": 0,
            "longest": None,
            "coverage": [],
        }

    total_seconds = sum(w[1] - w[0] for w in coverage)

    # Longest by duration; ties broken by earliest start.
    longest = min(coverage, key=lambda w: (-(w[1] - w[0]), w[0]))

    return {
        "count": len(coverage),
        "total_seconds": total_seconds,
        "longest": [longest[0], longest[1]],
        "coverage": [[w[0], w[1]] for w in coverage],
    }
