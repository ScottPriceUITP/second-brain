"""Fuzzy string matching utilities using thefuzz library."""

from thefuzz import fuzz


def fuzzy_match(
    query: str,
    candidates: list[str],
    threshold: float = 0.8,
) -> list[tuple[str, float]]:
    """Match a query string against a list of candidates using fuzzy matching.

    Uses a combination of token_sort_ratio (handles word reordering) and
    partial_ratio (handles substrings like "Bob" vs "Robert") to produce
    a combined score.

    Args:
        query: The string to match against candidates.
        candidates: List of candidate strings to compare against.
        threshold: Minimum score (0.0 to 1.0) to include in results.

    Returns:
        List of (candidate, score) tuples above the threshold, sorted by
        score descending.
    """
    if not query or not candidates:
        return []

    query_lower = query.lower().strip()
    results: list[tuple[str, float]] = []

    for candidate in candidates:
        candidate_lower = candidate.lower().strip()

        token_sort = fuzz.token_sort_ratio(query_lower, candidate_lower)
        partial = fuzz.partial_ratio(query_lower, candidate_lower)

        # Weighted average: token_sort handles reordering, partial handles substrings
        combined = (token_sort * 0.6 + partial * 0.4) / 100.0

        if combined >= threshold:
            results.append((candidate, combined))

    results.sort(key=lambda x: x[1], reverse=True)
    return results
