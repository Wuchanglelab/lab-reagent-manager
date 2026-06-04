# Lab Reagent Manager

Lab Reagent Manager is a lightweight web application for research labs that need a practical way to manage reagents, consumables, procurement requests, expenses, and shared resource reservations without adopting a full LIMS.

The app is currently optimized for Chinese-language lab operations, with an English codebase and deployment notes so other labs can adapt it.

## Features

- Reagent and consumable inventory with categories, storage locations, owners, CAS/catalog numbers, suppliers, prices, notes, and images.
- Usage records with stock deduction, stock return, and basic unit conversion from package specifications.
- Inventory dashboards for low stock, out-of-stock items, hazardous materials, expiring items, cold-storage shortcuts, and category summaries.
- Purchase request workflow covering request submission, approval/rejection, receiving, stock-in, and procurement status summaries.
- Optional email notifications for procurement approvers and requesters through SMTP.
- Expense summaries across reagents, consumables, instruments, and proxy purchase/payment items.
- Shared resource booking for a lab server/dongle workflow, with conflict detection and admin cancellation.
- Image upload support for local storage or Vercel Blob, including HEIC/HEIF and image compression.
- Optional AI-assisted reagent recognition and inventory review through an OpenAI-compatible chat completions endpoint.
- SQLite for local/small deployments and PostgreSQL for hosted deployments.

## Tech Stack

- Python 3.12+
- Flask
- SQLAlchemy
- SQLite or PostgreSQL
- Pillow and pillow-heif for image handling
- Gunicorn for production serving
- Optional Vercel Blob for uploaded images

## Quick Start

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

Open `http://127.0.0.1:8080`.

For local development, SQLite is used by default and data is stored as `lab_reagents.db` under `DATA_DIR`. Uploaded images are stored under `DATA_DIR/uploads/`.

The app reads configuration from environment variables. The `.env.example` file is a reference for local shells and hosting platforms; source it manually as shown above or configure the same variables in your deployment provider.

## Configuration

Most configuration is optional. Copy `.env.example` and set only the values you need.

| Variable | Purpose |
| --- | --- |
| `DATA_DIR` | Directory for SQLite database and local uploads. |
| `DATABASE_URL` | PostgreSQL connection string. Falls back to SQLite when unset. |
| `POSTGRES_URL_NON_POOLING` / `POSTGRES_URL` | Alternative PostgreSQL variables used by some hosts. |
| `LAB_ADMIN_PASSWORD` | Admin password required for protected destructive actions. |
| `LAB_ADMIN_NAME` | Display name for the admin account. |
| `HOST`, `PORT` | Local bind host and port. Defaults to `127.0.0.1:8080`. |
| `OPENAI_API_KEY` | API key for AI recognition and inventory review. |
| `OPENAI_BASE_URL` | OpenAI-compatible API base URL. |
| `OPENAI_MODEL` | Model name for AI calls. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | SMTP configuration for purchase workflow emails. |
| `LAB_APPROVER_EMAILS` | Comma- or space-separated approver email list. |
| `APP_BASE_URL` | Public app URL included in notification emails. |
| `BLOB_READ_WRITE_TOKEN` | Enables Vercel Blob storage for uploads when set. |
| `BLOB_ACCESS` | `public` or `private` for Vercel Blob objects. |

## Deployment

### Render

The repository includes `render.yaml` for deploying as a Python web service:

- build command: `pip install -r requirements.txt`
- start command: `gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --timeout 120`
- persistent disk mounted at `/var/data`

For production, configure at least:

- `DATA_DIR=/var/data`
- `LAB_ADMIN_PASSWORD`
- `DATABASE_URL` or a persistent disk for SQLite
- SMTP variables if purchase approval emails are needed
- OpenAI-compatible API variables if AI features are enabled

### Vercel

The repository includes `vercel.json`. For Vercel-style deployments, use PostgreSQL for persistent data and set `BLOB_READ_WRITE_TOKEN` if users need durable uploaded image storage.

## Data Migration

To migrate a local SQLite inventory database into PostgreSQL:

```bash
python scripts/migrate_inventory_to_postgres.py \
  --source-db ./lab_reagents.db \
  --database-url "$DATABASE_URL"
```

By default, image paths are cleared because local upload files are not migrated. Use `--keep-image-path` only when you know those paths remain valid.

## Security Notes

This project is intended for trusted lab teams and small internal deployments. Before exposing it to the public internet, configure `LAB_ADMIN_PASSWORD`, restrict access at the hosting/network layer, review uploaded-file and procurement endpoints for your threat model, and avoid storing secrets or sensitive inventory data in the repository.

Please see `SECURITY.md` for responsible disclosure and deployment guidance.

## Open Source Status

This project is being opened from a real lab workflow into a reusable OSS tool. Contributions that improve documentation, tests, modularity, deployment hardening, accessibility, and AI feature evaluation are especially welcome.

Suggested GitHub topics:

```text
flask, sqlalchemy, lab-management, reagent-inventory, inventory-management, procurement, research-lab, lims, openai, python
```

## License

MIT License. See `LICENSE`.

## 中文简介

Lab Reagent Manager 是面向科研实验室的小型试剂与资源管理系统，支持试剂库存、领用记录、采购审批、入库、费用统计、邮件通知、服务器/密码狗预约、图片上传，以及可选的 AI 辅助识别和库存巡检。它适合希望用轻量开源工具替代纸质表格、共享表格或重型商业 LIMS 的小型课题组。
