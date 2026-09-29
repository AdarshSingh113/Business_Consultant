from core.text import matches_alias, mention_id, parse_date, parse_rating


def test_mention_id_ignores_case_spacing_and_punctuation():
    a = mention_id("amazon", "boat", "Battery drains fast!!")
    b = mention_id("Amazon", "boat", "  battery   drains fast ")
    assert a == b


def test_mention_id_differs_by_brand_and_source():
    text = "Battery drains fast"
    assert mention_id("amazon", "boat", text) != mention_id("amazon", "noise", text)
    assert mention_id("amazon", "boat", text) != mention_id("flipkart", "boat", text)


def test_parse_rating():
    assert parse_rating("4.0 out of 5 stars") == 4.0
    assert parse_rating("3") == 3.0
    assert parse_rating("") is None
    assert parse_rating("10") is None


def test_parse_date_formats():
    assert parse_date("Reviewed in India on 21 August 2026") == "2026-08-21"
    assert parse_date("2026-08-02") == "2026-08-02"
    assert parse_date("05/08/2026") == "2026-08-05"  # Indian day/month order
    assert parse_date("2026-08-02T10:00:00Z") == "2026-08-02"
    assert parse_date("yesterday") is None


def test_matches_alias_whole_words_only():
    assert matches_alias("My boAt Airdopes died", ["boat"])
    assert not matches_alias("Went boating last weekend", ["boat"])
    assert matches_alias("the jbl tune beam is great", ["jbl tune"])
