"""Content-anchored element-map diff / classification (Option B core)."""
from watcher.detection.elements import classify_block, diff_maps, summarize_diff


def test_diff_maps_added_removed_by_key():
    before = {"blocks": [{"k": "a", "s": "x"}, {"k": "b", "s": "y"}]}
    after = {"blocks": [{"k": "b", "s": "y"}, {"k": "c", "s": "z"}]}
    d = diff_maps(before, after)
    assert [b["k"] for b in d["added"]] == ["c"]
    assert [b["k"] for b in d["removed"]] == ["a"]


def test_diff_maps_none_safe():
    assert diff_maps(None, None) == {"added": [], "removed": []}
    assert diff_maps({"blocks": [{"k": "a"}]}, None)["removed"][0]["k"] == "a"


def test_classify_block_by_shape():
    assert classify_block("Great place to work. Pros: good pay. Cons: long hours.") == "review"
    assert classify_block("Sr. Analyst, Falcon Complete (Remote, GBR) Remote View job") == "job"
    assert classify_block("Apply now for this position") == "job"
    assert classify_block("Some unrelated paragraph of text") == "item"


def test_summarize_diff_reports_counts_types_examples():
    diff = {
        "added": [{"k": "1", "s": "Sr. Analyst, Falcon Complete View job"},
                  {"k": "2", "s": "Engineer II View job"}],
        "removed": [{"k": "3", "s": "Old Role View job"}],
    }
    s = summarize_diff(diff).lower()
    assert "added 2 content block" in s
    assert "removed 1 content block" in s
    assert "job" in s
    assert "sr. analyst" in s   # example snippet included


def test_summarize_diff_empty():
    assert summarize_diff({"added": [], "removed": []}) == ""
