# mock_repo

A tiny Python web app used as a Smithic integration target.

## Stack
- Python 3.12
- A single-file FastAPI app at `app.py` for now.
- pytest for tests.

## Conventions
- One file, no premature modularization.
- Tests live next to code as `test_*.py`.

## Known gaps
- No health check endpoint yet.
