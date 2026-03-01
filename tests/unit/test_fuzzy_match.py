"""Tests for fuzzy matching utility."""

from second_brain.utils.fuzzy_match import fuzzy_match


class TestFuzzyMatch:
    def test_exact_match(self):
        results = fuzzy_match("Reynolds Electric", ["Reynolds Electric", "Dave Reynolds"])
        assert len(results) >= 1
        assert results[0][0] == "Reynolds Electric"
        assert results[0][1] >= 0.95

    def test_case_insensitive(self):
        results = fuzzy_match("reynolds electric", ["Reynolds Electric"])
        assert len(results) == 1
        assert results[0][0] == "Reynolds Electric"

    def test_partial_match(self):
        results = fuzzy_match("Reynolds", ["Reynolds Electric", "Dave Reynolds"], threshold=0.5)
        assert len(results) >= 1

    def test_no_match_below_threshold(self):
        results = fuzzy_match("Completely Different", ["Reynolds Electric"], threshold=0.8)
        assert len(results) == 0

    def test_empty_query(self):
        results = fuzzy_match("", ["Reynolds Electric"])
        assert results == []

    def test_empty_candidates(self):
        results = fuzzy_match("Reynolds", [])
        assert results == []

    def test_results_sorted_by_score_descending(self):
        results = fuzzy_match(
            "Reynolds",
            ["Dave Reynolds", "Reynolds Electric", "John Smith"],
            threshold=0.3,
        )
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)

    def test_abbreviation_handling(self):
        results = fuzzy_match("Inc", ["Inc.", "Incorporated"], threshold=0.5)
        assert len(results) >= 1

    def test_word_reordering(self):
        results = fuzzy_match("Electric Reynolds", ["Reynolds Electric"], threshold=0.7)
        assert len(results) == 1
        assert results[0][1] >= 0.7

    def test_threshold_boundary(self):
        results_low = fuzzy_match("test", ["test string"], threshold=0.3)
        results_high = fuzzy_match("test", ["test string"], threshold=0.99)
        assert len(results_low) >= len(results_high)
