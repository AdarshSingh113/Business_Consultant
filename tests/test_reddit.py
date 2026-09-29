from collectors.reddit import is_relevant, search_query


def test_relevance_needs_brand_and_category(config):
    boat = config.brand("boat")
    assert is_relevant("Are boAt Airdopes good earbuds under 1500?", boat, config)
    assert not is_relevant("Bought a fishing boat last week", boat, config)
    assert not is_relevant("boat earbuds", config.brand("noise"), config)


def test_search_query_quotes_every_name(config):
    assert search_query(config.brand("jbl")) == '"JBL" OR "jbl" OR "jbl wave" OR "jbl tune"'
