import argparse
import os
import sqlite3
import sys
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DB = REPO_DIR / "lab_reagents.db"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Migrate categories, reagents, and usage records from local SQLite to Postgres."
    )
    parser.add_argument(
        "--source-db",
        default=str(DEFAULT_SOURCE_DB),
        help="Path to the local SQLite database file. Defaults to ./lab_reagents.db",
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="Target Postgres URL. If omitted, falls back to POSTGRES_URL_NON_POOLING or DATABASE_URL.",
    )
    parser.add_argument(
        "--keep-image-path",
        action="store_true",
        help="Keep image_path values from SQLite. By default image_path is cleared during migration.",
    )
    return parser.parse_args()


def get_source_connection(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def ensure_target_url(args):
    target_url = (
        args.database_url
        or os.environ.get("POSTGRES_URL_NON_POOLING")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
    )
    if not target_url:
        raise SystemExit(
            "Missing target database URL. Pass --database-url or export POSTGRES_URL_NON_POOLING / DATABASE_URL."
        )
    os.environ["DATABASE_URL"] = target_url
    return target_url


def main():
    args = parse_args()
    source_db_path = Path(args.source_db).expanduser().resolve()
    if not source_db_path.exists():
        raise SystemExit(f"SQLite database not found: {source_db_path}")

    ensure_target_url(args)

    sys.path.insert(0, str(REPO_DIR))
    import app  # noqa: WPS433

    app.init_db()

    source = get_source_connection(source_db_path)
    target = app.SessionLocal()

    migrated = {
        "categories": 0,
        "reagents": 0,
        "usage_records": 0,
    }

    try:
        category_rows = source.execute(
            "SELECT name, icon, color, sort_order FROM categories ORDER BY sort_order, id"
        ).fetchall()
        for row in category_rows:
            existing = target.query(app.Category).filter_by(name=row["name"]).one_or_none()
            if existing is None:
                existing = app.Category(name=row["name"])
                target.add(existing)
            existing.icon = row["icon"]
            existing.color = row["color"]
            existing.sort_order = row["sort_order"]
            migrated["categories"] += 1

        reagent_rows = source.execute(
            """
            SELECT id, name, name_en, cas_number, catalog_number, brand, specification, purity,
                   unit, quantity, low_stock_threshold, category, storage_location, storage_temp,
                   hazard_level, hazard_info, expiry_date, supplier, price, notes, image_path,
                   created_at, updated_at
            FROM reagents
            ORDER BY updated_at DESC
            """
        ).fetchall()
        for row in reagent_rows:
            payload = dict(row)
            if not args.keep_image_path:
                payload["image_path"] = None

            existing = target.get(app.Reagent, payload["id"])
            if existing is None:
                existing = app.Reagent(id=payload["id"])
                target.add(existing)

            for key, value in payload.items():
                setattr(existing, key, value)
            migrated["reagents"] += 1

        usage_rows = source.execute(
            """
            SELECT id, reagent_id, user_name, action, quantity, usage_unit,
                   converted_quantity, converted_unit, purpose, notes, created_at
            FROM usage_records
            ORDER BY created_at ASC
            """
        ).fetchall()
        for row in usage_rows:
            payload = dict(row)
            existing = target.get(app.UsageRecord, payload["id"])
            if existing is None:
                existing = app.UsageRecord(id=payload["id"])
                target.add(existing)

            for key, value in payload.items():
                setattr(existing, key, value)
            migrated["usage_records"] += 1

        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()
        source.close()

    print("Migration finished.")
    print(f"Source DB: {source_db_path}")
    print(f"Categories migrated: {migrated['categories']}")
    print(f"Reagents migrated: {migrated['reagents']}")
    print(f"Usage records migrated: {migrated['usage_records']}")
    if args.keep_image_path:
        print("Image paths were preserved.")
    else:
        print("Image paths were cleared. No images were migrated.")


if __name__ == "__main__":
    main()
