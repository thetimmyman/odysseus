def merge_intervals(intervals: list) -> list:
    if not intervals:
        return []

    for pair in intervals:
        start, end = pair[0], pair[1]
        if start > end:
            raise ValueError("interval start must not be greater than end")

    ordered = sorted((list(pair) for pair in intervals), key=lambda p: (p[0], p[1]))

    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        current = merged[-1]
        if start <= current[1]:
            if end > current[1]:
                current[1] = end
        else:
            merged.append([start, end])

    return merged
