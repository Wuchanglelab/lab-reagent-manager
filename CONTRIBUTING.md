# Contributing

Thank you for helping improve Lab Reagent Manager. The project is being shaped from a working lab tool into a reusable open-source application, so practical improvements are very welcome.

## Good First Contributions

- Improve README, deployment notes, screenshots, or Chinese/English wording.
- Add tests for inventory, purchase approval, usage records, and reservation conflict handling.
- Split the large Flask app into smaller modules while preserving behavior.
- Improve authentication, authorization, upload safety, and deployment hardening.
- Add accessibility and mobile usability fixes to the web UI.
- Improve AI-assisted reagent recognition with structured outputs, evaluation cases, and clear failure handling.

## Development Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
set -a
source .env
set +a
python app.py
```

Use SQLite for local development unless your change specifically needs PostgreSQL.

## Pull Request Guidelines

- Keep changes focused and explain the lab workflow or bug they address.
- Do not commit `.env`, uploaded files, local databases, or secrets.
- Include manual test notes, and automated tests when changing backend behavior.
- Preserve Chinese UI text unless the change intentionally updates localization.
- For AI features, document the model/provider assumptions and expected output schema.

## Reporting Issues

When opening an issue, include:

- What workflow failed or felt confusing.
- Steps to reproduce.
- Expected vs. actual behavior.
- Deployment type: local SQLite, Render, Vercel, PostgreSQL, or other.
- Browser and Python version when relevant.
