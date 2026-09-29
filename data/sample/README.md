# Sample data

These reviews are **made up** for testing the pipeline. They are not real customer reviews.

- `sample_reviews.csv`: has `brand` and `source` columns. It also contains one duplicate
  (extra space), one unknown brand (Sony) and one empty review, so you can see them skipped.
- `amazon_export_style.csv`: headers the way a copy-paste from Amazon usually looks, with no
  brand or source column. Import it with `--brand boat --source amazon`.
