# Your review exports go here

Every `*.csv` in this folder is imported by `python -m jobs.daily`. Re-importing is safe:
reviews already in the database are skipped.

Files here must have a `brand` column and a `source` column. For a file without them, import it
directly instead:

    python -m collectors.csv_import my_file.csv --brand boat --source amazon

CSV files in this folder are **git-ignored** on purpose: this is a public repo, and copied
marketplace reviews should not be republished there. Import them from your machine with
`DATABASE_URL` pointing at Supabase.
