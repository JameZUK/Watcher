"""Content-anchored element-map diff / classification (Option B core)."""
from watcher.detection.elements import (
    build_overlays,
    classify_block,
    diff_maps,
    diff_maps_matched,
    summarize_diff,
    word_diff,
)


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
    assert classify_block("Senior Engineer, Platform Team (Remote) View job") == "job"
    assert classify_block("Apply now for this position") == "job"
    assert classify_block("Some unrelated paragraph of text") == "item"


def test_summarize_diff_reports_counts_types_examples():
    diff = {
        "added": [{"k": "1", "s": "Senior Engineer, Platform Team View job"},
                  {"k": "2", "s": "Data Engineer II View job"}],
        "removed": [{"k": "3", "s": "Former Role View job"}],
    }
    s = summarize_diff(diff).lower()
    assert "added 2 content block" in s
    assert "removed 1 content block" in s
    assert "job" in s
    assert "senior engineer" in s   # example snippet included


def test_summarize_diff_empty():
    assert summarize_diff({"added": [], "removed": []}) == ""


def test_diff_maps_matched_pairs_in_place_edits():
    before = {"blocks": [{"k": "a", "s": "Review by Bob rating 4 stars great place to work"},
                         {"k": "old", "s": "Totally unrelated block that was removed"}]}
    after = {"blocks": [{"k": "a2", "s": "Review by Bob rating 5 stars great place to work"},
                        {"k": "new", "s": "A brand new and unrelated added block xyz"}]}
    m = diff_maps_matched(before, after)
    assert len(m["changed"]) == 1                       # the Bob review (4->5) is one change
    assert m["changed"][0]["before"]["k"] == "a" and m["changed"][0]["after"]["k"] == "a2"
    assert [b["k"] for b in m["added"]] == ["new"]      # genuine add
    assert [b["k"] for b in m["removed"]] == ["old"]    # genuine remove


def test_diff_maps_matched_caps_pairing_work():
    # Above max_pairs the O(R×A) in-place matching is skipped: everything reads as a
    # pure add/remove (no 'changed' pairs) so a fully-reflowed page can't stall.
    before = {"blocks": [{"k": f"b{i}", "s": f"removed block number {i} lorem ipsum dolor"} for i in range(150)]}
    after = {"blocks": [{"k": f"a{i}", "s": f"added block number {i} lorem ipsum dolor"} for i in range(150)]}
    m = diff_maps_matched(before, after, max_pairs=100)
    assert m["changed"] == []
    assert len(m["added"]) == 150 and len(m["removed"]) == 150


def test_diff_maps_matched_prefilter_keeps_true_match():
    # The length/quick-ratio prefilter must never drop a genuine in-place edit.
    before = {"blocks": [{"k": "x", "s": "Review by Bob rating 4 stars great place to work overall"}]}
    after = {"blocks": [{"k": "y", "s": "Review by Bob rating 5 stars great place to work overall"}]}
    m = diff_maps_matched(before, after)
    assert len(m["changed"]) == 1 and not m["added"] and not m["removed"]


def test_word_diff_segments():
    ops = [(s["op"], s["t"]) for s in word_diff("rating 4 stars great", "rating 5 stars great")]
    assert ("same", "rating") in ops
    assert ("del", "4") in ops and ("add", "5") in ops
    assert ("same", "stars great") in ops


def test_build_overlays_categorizes_and_carries_diff():
    before = {"pw": 1000, "ph": 2000, "blocks": [{"k": "r", "x": 1, "y": 2, "w": 3, "h": 4,
                                                   "s": "row alpha one two three"}]}
    after = {"pw": 1000, "ph": 2000, "blocks": [{"k": "r2", "x": 1, "y": 2, "w": 3, "h": 4,
                                                 "s": "row alpha one two THREE"},
                                                {"k": "n", "s": "a new added row of content here"}]}
    ov = build_overlays(before, after)
    assert ov["counts"]["added"] == 1 and ov["counts"]["changed"] == 1
    kinds = {b["kind"] for b in ov["after"]["blocks"]}
    assert kinds == {"added", "changed"}
    ch = next(b for b in ov["after"]["blocks"] if b["kind"] == "changed")
    assert any(s["op"] == "add" for s in ch["diff"])    # word diff present
    assert ov["after"]["pw"] == 1000 and ov["after"]["ph"] == 2000


def test_build_overlays_none_without_maps():
    assert build_overlays(None, None) is None
