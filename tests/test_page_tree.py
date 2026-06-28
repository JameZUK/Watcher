"""Tests for the typed page-tree builder (watcher/detection/page_tree.py)."""

from watcher.detection import page_tree as pt


def _blk(k, g, x, y, w=300, h=80, s="a review of some length goes here ok"):
    return {"k": k, "g": g, "x": x, "y": y, "w": w, "h": h, "s": s}


def _map(blocks, pw=1200, ph=4000):
    return {"pw": pw, "ph": ph, "dpr": 1, "blocks": blocks}


# --- guards -----------------------------------------------------------------

def test_none_and_empty_return_none():
    assert pt.build_tree(None) is None
    assert pt.build_tree({}) is None
    assert pt.build_tree({"blocks": []}) is None
    assert pt.build_tree("nonsense") is None


def test_malformed_blocks_are_skipped_not_fatal():
    tree = pt.build_tree(_map([
        {"g": "x", "s": "no key or coords"},     # dropped
        _blk("k1", "x", 0, 200),
    ]))
    assert tree is not None
    assert len(tree.regions) == 1
    assert tree.regions[0].keys == {"k1"}


# --- grouping into regions --------------------------------------------------

def test_groups_become_regions_in_reading_order():
    tree = pt.build_tree(_map([
        _blk("k1", "list>div.review", 0, 900),
        _blk("k2", "list>div.review", 0, 1000),
        _blk("n1", "nav>li.tab", 0, 10, w=80, h=30, s="Home"),
        _blk("n2", "nav>li.tab", 90, 10, w=80, h=30, s="Jobs"),
    ]))
    assert [r.rid for r in tree.regions] == ["nav>li.tab", "list>div.review"]  # by y
    assert tree.region("nav>li.tab").role == "nav"
    assert tree.region("list>div.review").role == "records"


def test_ungrouped_blocks_collapse_to_one_region():
    # Pre-upgrade snapshots have no `g` — must still yield a usable tree.
    tree = pt.build_tree(_map([
        {"k": "k1", "x": 0, "y": 100, "w": 300, "h": 80, "s": "old block one here"},
        {"k": "k2", "x": 0, "y": 200, "w": 300, "h": 80, "s": "old block two here"},
    ]))
    assert len(tree.regions) == 1
    assert tree.regions[0].rid == "_"
    assert tree.regions[0].keys == {"k1", "k2"}


# --- main_region picks the content list -------------------------------------

def test_main_region_is_text_richest_records_set():
    tree = pt.build_tree(_map([
        _blk("n1", "nav", 0, 10, w=80, h=30, s="Home"),
        _blk("n2", "nav", 90, 10, w=80, h=30, s="Jobs"),
        _blk("r1", "reviews", 0, 500, s="a long substantial review with lots of text " * 3),
        _blk("r2", "reviews", 0, 600, s="another long substantial review with text " * 3),
        _blk("f1", "footer", 0, 3000, s="Related link one"),
        _blk("f2", "footer", 0, 3050, s="Related link two"),
        _blk("f3", "footer", 0, 3100, s="Related link three"),
    ]))
    main = tree.main_region()
    assert main.rid == "reviews"               # not nav (typed out), not footer (thinner)


# --- new-item detection: the payoff -----------------------------------------

def _reviews(keys):
    return _map([_blk(k, "reviews", 0, 500 + i * 100) for i, k in enumerate(keys)])


def test_new_keys_detects_only_genuinely_new_items():
    before = pt.build_tree(_reviews(["a", "b", "c"]))
    after = pt.build_tree(_reviews(["d", "a", "b", "c"]))   # one new + reordered
    assert pt.new_keys(before, after) == {"d"}


def test_reorder_only_is_not_new():
    before = pt.build_tree(_reviews(["a", "b", "c"]))
    after = pt.build_tree(_reviews(["c", "a", "b"]))        # same items, shuffled
    assert pt.new_keys(before, after) == set()


def test_first_capture_has_no_new_items():
    after = pt.build_tree(_reviews(["a", "b", "c"]))
    assert pt.new_keys(None, after) == set()


def test_new_keys_respects_named_region():
    before = pt.build_tree(_map([
        _blk("a", "reviews", 0, 500), _blk("b", "reviews", 0, 600),
        _blk("x", "sidebar", 800, 500), _blk("y", "sidebar", 800, 600),
    ]))
    after = pt.build_tree(_map([
        _blk("a", "reviews", 0, 500), _blk("b", "reviews", 0, 600), _blk("c", "reviews", 0, 700),
        _blk("x", "sidebar", 800, 500), _blk("z", "sidebar", 800, 600),
    ]))
    assert pt.new_keys(before, after, region_id="reviews") == {"c"}
    assert pt.new_keys(before, after, region_id="sidebar") == {"z"}


# --- geometry ---------------------------------------------------------------

def test_select_region_prefers_rid_then_sample_then_main():
    tree = pt.build_tree(_map([
        _blk("a", "reviews", 0, 500, s="great place to work honestly loved it"),
        _blk("b", "reviews", 0, 600, s="another solid review of the company here"),
        _blk("x", "sidebar", 800, 500, s="related company one"),
        _blk("y", "sidebar", 800, 600, s="related company two"),
        _blk("z", "sidebar", 800, 700, s="related company three"),
    ]))
    # exact rid wins
    assert pt.select_region(tree, rid="sidebar").rid == "sidebar"
    # rid absent → fall back to sample match
    assert pt.select_region(tree, rid="gone", sample="great place to work").rid == "reviews"
    # nothing given → structural main_region (text-richest)
    assert pt.select_region(tree, rid=None, sample=None) is tree.main_region()
    # None tree
    assert pt.select_region(None, rid="x") is None


def test_union_bbox_spans_all_records():
    tree = pt.build_tree(_map([
        _blk("a", "g", 10, 100, w=200, h=50),
        _blk("b", "g", 30, 400, w=200, h=50),
    ]))
    assert tree.regions[0].box == (10, 100, 220, 350)   # x,y,w,h spanning both
