# OS Health Check

Minimal CSV lookup editor for `_data/eol_lookup.csv`.

## Stack

- `FastAPI` for a simple Python API and future workflow expansion
- `Jinja2` for serving the app shell
- Vanilla HTML, CSS, and JavaScript for a fast lightweight UI

## Run locally

```bash
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Current features

- Load the lookup CSV into a clean editable table
- Search across all columns
- Add and delete rows
- Save changes back to the CSV file

## Notes

- The app expects the CSV headers to remain:
  - `os_string`
  - `normalized_os_detailed_name`
  - `normalized_os`
  - `eol_date`
  - `eol_status`
  - `eoas_date`
  - `eoas_status`
