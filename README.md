# Chicha's AI Ordering — Local FastAPI POC

## Project layout
- `app/main.py`: FastAPI endpoints (menu, restaurant policy, orders, owner menu availability)
- `dashboard/index.html`: local owner menu page
- `supabase/migrations/`: migrations already applied to Chicha's Supabase project
- `supabase/seed_menu_draft.sql`: draft menu import (162 records)
- `tests/`: backend tests

## Local setup
```bash
cd ~/Projects/chichas-ai-ordering
source .venv/bin/activate
cp .env.example .env
# Edit .env on your Mac with the Supabase connection URI and a random staff key
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs` for API documentation or `http://127.0.0.1:8000/owner/menu` for the owner page.

**Safety:** The owner page is a local POC and uses a shared staff key, not production authentication. Do not expose it to the public Internet. Live ordering remains disabled until menu and tax are approved.

## Tests
```bash
pytest -q
```

## Supabase
Project ref: `lretkjtccfwtrulqohmc` (US East). Migrations and draft menu were already applied remotely. Do not rerun the seed without reviewing it.
