# Tagger accuracy test

`labeled_reviews.csv` holds reviews with the answers a careful human would give. The eval runs the
tagger on them and compares.

    python -m evals.eval_tagger

The starter rows are the made-up sample reviews plus a few extra cases (Hinglish, off-topic posts).
**Add about 100 of your own real reviews** and label them yourself before trusting the tagger:
- `relevant`: true or false
- `overall`: positive, negative, mixed or neutral
- `aspects`: `issue:sentiment` pairs separated by `;`, using labels from `config/brands.yaml`

Labeling is where you learn what the categories really mean. When you and the model disagree,
decide whether the model is wrong or the label definition is unclear, then fix the prompt
(and bump `PROMPT_VERSION` in `brain/tagger.py`).
