import base64
import io
import mimetypes
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, request, send_file, send_from_directory
from pillow_heif import register_heif_opener
import psycopg
from sqlalchemy import Column, Float, ForeignKey, Integer, String, create_engine, func, inspect, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.pool import NullPool
from werkzeug.utils import secure_filename

try:
    from vercel.blob import put as blob_put
except ImportError:
    blob_put = None

register_heif_opener()

BASE_DIR = os.path.dirname(__file__)
DEFAULT_DATA_DIR = "/tmp/lab-reagent-manager" if os.environ.get("VERCEL") else BASE_DIR
DATA_DIR = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
LOCAL_UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
LOCAL_DATABASE = os.path.join(DATA_DIR, "lab_reagents.db")
DEFAULT_TEAMPLUS_API_KEY = ""
DEFAULT_CATEGORY_NAMES = ["常用试剂", "危险试剂", "-20°C冰箱", "-80°C冰箱", "实验耗材", "生物样品"]
NON_STANDARD_IMAGE_EXTENSIONS = {"avif", "heic", "heif", "bmp", "tiff", "tif", "svg"}
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "avif", "gif", "bmp", "tiff", "tif", "heic", "heif", "svg"}
MAX_UPLOAD_MB = 1

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["UPLOAD_FOLDER"] = LOCAL_UPLOAD_FOLDER

Base = declarative_base()


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    icon = Column(String)
    color = Column(String)
    sort_order = Column(Integer, default=0)


class Reagent(Base):
    __tablename__ = "reagents"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    name_en = Column(String)
    cas_number = Column(String)
    catalog_number = Column(String)
    brand = Column(String)
    specification = Column(String)
    purity = Column(String)
    unit = Column(String, default="瓶")
    quantity = Column(Float, default=0)
    low_stock_threshold = Column(Float, default=1)
    category = Column(String, nullable=False, default="常用试剂")
    storage_location = Column(String)
    storage_temp = Column(String)
    hazard_level = Column(String, default="普通")
    hazard_info = Column(String)
    expiry_date = Column(String)
    supplier = Column(String)
    price = Column(Float)
    notes = Column(String)
    image_path = Column(String)
    created_at = Column(String, default=lambda: now_text())
    updated_at = Column(String, default=lambda: now_text())

    usage_records = relationship("UsageRecord", back_populates="reagent", cascade="all, delete-orphan")


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id = Column(String, primary_key=True)
    reagent_id = Column(String, ForeignKey("reagents.id"), nullable=False)
    user_name = Column(String, nullable=False)
    action = Column(String, nullable=False, default="领用")
    quantity = Column(Float, nullable=False)
    usage_unit = Column(String)
    converted_quantity = Column(Float)
    converted_unit = Column(String)
    purpose = Column(String)
    notes = Column(String)
    created_at = Column(String, default=lambda: now_text())

    reagent = relationship("Reagent", back_populates="usage_records")


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def model_to_dict(instance):
    return {column.name: getattr(instance, column.name) for column in instance.__table__.columns}


def choose_database_url():
    candidates = [
        os.environ.get("DATABASE_URL"),
        os.environ.get("POSTGRES_URL_NON_POOLING"),
        os.environ.get("POSTGRES_URL"),
    ]
    for candidate in candidates:
        if candidate:
            return candidate
    return f"sqlite:///{LOCAL_DATABASE}"


DATABASE_URL = choose_database_url()
USING_SQLITE = DATABASE_URL.startswith("sqlite")

engine_kwargs = {"future": True}
if USING_SQLITE:
    engine_kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(DATABASE_URL, **engine_kwargs)
else:
    engine_kwargs["poolclass"] = NullPool
    raw_database_url = DATABASE_URL

    def connect_postgres():
        conninfo = raw_database_url
        if "connect_timeout=" not in conninfo:
            conninfo += "&connect_timeout=10" if "?" in conninfo else "?connect_timeout=10"
        return psycopg.connect(conninfo)

    engine = create_engine("postgresql+psycopg://", creator=connect_postgres, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_ai_client_config():
    return (
        os.environ.get("BASE_URL", "https://teamplus.space/v1"),
        os.environ.get("TEAMPLUS_API_KEY", DEFAULT_TEAMPLUS_API_KEY),
        os.environ.get("MODEL", "gpt-5.4"),
    )


def parse_model_json_content(resp_data):
    import json

    result_text = resp_data["choices"][0]["message"]["content"].strip()
    if result_text.startswith("```"):
        result_text = result_text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(result_text)


UNIT_ALIASES = {
    "瓶": ("count", 1.0, "瓶"),
    "盒": ("count", 1.0, "盒"),
    "袋": ("count", 1.0, "袋"),
    "支": ("count", 1.0, "支"),
    "个": ("count", 1.0, "个"),
    "包": ("count", 1.0, "包"),
    "ml": ("volume", 1.0, "mL"),
    "mL": ("volume", 1.0, "mL"),
    "l": ("volume", 1000.0, "L"),
    "L": ("volume", 1000.0, "L"),
    "ul": ("volume", 0.001, "uL"),
    "uL": ("volume", 0.001, "uL"),
    "μl": ("volume", 0.001, "μL"),
    "μL": ("volume", 0.001, "μL"),
    "g": ("mass", 1.0, "g"),
    "kg": ("mass", 1000.0, "kg"),
    "mg": ("mass", 0.001, "mg"),
}
CONTAINER_UNITS = {"瓶", "盒", "袋", "支", "个", "包"}


def normalize_unit(unit):
    return UNIT_ALIASES.get(str(unit or "").strip())


def parse_spec_amount(specification):
    if not specification:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(mL|ml|L|l|uL|ul|μL|μl|kg|g|mg)", str(specification))
    if not match:
        return None
    value = float(match.group(1))
    normalized = normalize_unit(match.group(2))
    if not normalized:
        return None
    dimension, factor, display_unit = normalized
    return {
        "value": value,
        "unit": display_unit,
        "dimension": dimension,
        "base_amount": value * factor,
    }


def convert_usage_to_stock_units(reagent, quantity, usage_unit):
    stock_unit = reagent["unit"] if isinstance(reagent, dict) else reagent.unit
    specification = reagent["specification"] if isinstance(reagent, dict) else reagent.specification
    stock_normalized = normalize_unit(stock_unit)
    usage_normalized = normalize_unit(usage_unit)
    if not stock_normalized or not usage_normalized:
        raise ValueError("不支持的单位")

    stock_dim, stock_factor, stock_display = stock_normalized
    usage_dim, usage_factor, usage_display = usage_normalized
    qty = float(quantity)

    if stock_dim == usage_dim and stock_display not in CONTAINER_UNITS and usage_display not in CONTAINER_UNITS:
        return (qty * usage_factor) / stock_factor

    if stock_display in CONTAINER_UNITS:
        if usage_display == stock_display:
            return qty
        package_amount = parse_spec_amount(specification)
        if not package_amount:
            raise ValueError("当前试剂规格未填写可换算的容量/质量，无法按该单位领用")
        if package_amount["dimension"] != usage_dim:
            raise ValueError("领用单位与规格单位不匹配，无法换算")
        return (qty * usage_factor) / package_amount["base_amount"]

    raise ValueError("当前库存单位与领用单位无法换算")


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def use_blob_storage():
    return bool(os.environ.get("BLOB_READ_WRITE_TOKEN"))


def blob_access_mode():
    return os.environ.get("BLOB_ACCESS", "private")


def ensure_column(table_name, column_name, column_type):
    inspector = inspect(engine)
    existing = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name in existing:
        return
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    if USING_SQLITE or not use_blob_storage():
        os.makedirs(LOCAL_UPLOAD_FOLDER, exist_ok=True)

    Base.metadata.create_all(engine)

    if USING_SQLITE:
        ensure_column("reagents", "low_stock_threshold", "REAL DEFAULT 1")
        ensure_column("usage_records", "usage_unit", "TEXT")
        ensure_column("usage_records", "converted_quantity", "REAL")
        ensure_column("usage_records", "converted_unit", "TEXT")
    else:
        ensure_column("reagents", "low_stock_threshold", "DOUBLE PRECISION DEFAULT 1")
        ensure_column("usage_records", "usage_unit", "TEXT")
        ensure_column("usage_records", "converted_quantity", "DOUBLE PRECISION")
        ensure_column("usage_records", "converted_unit", "TEXT")

    session = SessionLocal()
    try:
        if session.query(Category).count() == 0:
            session.add_all(
                [
                    Category(name="常用试剂", icon="🧪", color="#3b82f6", sort_order=1),
                    Category(name="危险试剂", icon="☠️", color="#ef4444", sort_order=2),
                    Category(name="-20°C冰箱", icon="❄️", color="#06b6d4", sort_order=3),
                    Category(name="-80°C冰箱", icon="🧊", color="#8b5cf6", sort_order=4),
                    Category(name="实验耗材", icon="🔬", color="#10b981", sort_order=5),
                    Category(name="生物样品", icon="🧬", color="#f59e0b", sort_order=6),
                ]
            )
            session.commit()
    finally:
        session.close()


init_db()


def post_chat_completion(payload, timeout=120, max_retries=3):
    api_base, api_key, _ = get_ai_client_config()
    if not api_key:
        raise RuntimeError("未配置 TEAMPLUS_API_KEY")
    last_error = None
    for attempt in range(max_retries):
        try:
            response = requests.post(
                f"{api_base}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(1.2 * (attempt + 1))
            else:
                raise last_error


def upload_bytes(filename, body, content_type):
    safe_name = secure_filename(filename) or f"{uuid.uuid4().hex[:12]}.bin"
    if use_blob_storage():
        if blob_put is None:
            raise RuntimeError("未安装 vercel Blob SDK")
        pathname = f"reagents/{uuid.uuid4().hex[:12]}-{safe_name}"
        access_mode = blob_access_mode()
        blob = blob_put(
            path=pathname,
            body=body,
            access=access_mode,
            content_type=content_type,
            add_random_suffix=False,
            token=os.environ.get("BLOB_READ_WRITE_TOKEN"),
        )
        blob_url = getattr(blob, "url", None)
        blob_download_url = getattr(blob, "download_url", None)
        blob_pathname = getattr(blob, "pathname", None) or pathname
        return {
            "file_ref": blob_pathname,
            "filename": blob_pathname,
            "url": blob_download_url or blob_url,
        }

    os.makedirs(LOCAL_UPLOAD_FOLDER, exist_ok=True)
    local_name = f"{uuid.uuid4().hex[:12]}-{safe_name}"
    filepath = os.path.join(LOCAL_UPLOAD_FOLDER, local_name)
    with open(filepath, "wb") as file_obj:
        file_obj.write(body)
    return {
        "file_ref": local_name,
        "filename": local_name,
        "url": f"/uploads/{local_name}",
    }


def load_uploaded_bytes(file_ref):
    if not file_ref:
        raise FileNotFoundError("文件不存在")
    file_ref = str(file_ref)
    if file_ref.startswith("http://") or file_ref.startswith("https://"):
        response = requests.get(file_ref, timeout=30)
        response.raise_for_status()
        return response.content, response.headers.get("content-type"), file_ref

    if use_blob_storage() and file_ref.startswith("reagents/"):
        if blob_put is None:
            raise RuntimeError("未安装 vercel Blob SDK")
        from vercel.blob import get as blob_get

        blob = blob_get(
            file_ref,
            access=blob_access_mode(),
            token=os.environ.get("BLOB_READ_WRITE_TOKEN"),
            timeout=30,
            use_cache=False,
        )
        source_name = getattr(blob, "pathname", None) or file_ref
        content_type = getattr(blob, "content_type", None)
        body = getattr(blob, "content", None)
        if body is None:
            raise FileNotFoundError("文件不存在")
        return body, content_type, source_name

    filepath = os.path.join(LOCAL_UPLOAD_FOLDER, file_ref)
    if not os.path.exists(filepath):
        raise FileNotFoundError("文件不存在")
    with open(filepath, "rb") as file_obj:
        body = file_obj.read()
    content_type, _ = mimetypes.guess_type(filepath)
    return body, content_type, filepath


def media_type_from_name(name, content_type=None):
    if content_type and content_type.startswith("image/"):
        return content_type
    guessed_type, _ = mimetypes.guess_type(str(name).split("?", 1)[0])
    if guessed_type and guessed_type.startswith("image/"):
        return guessed_type
    return "image/png"


def parse_expiry_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def is_expiring_soon(value, days=30):
    expiry = parse_expiry_date(value)
    if not expiry:
        return False
    return expiry <= (datetime.now().date() + timedelta(days=days))


def compute_inventory_stats(reagents):
    stats = {
        "total": len(reagents),
        "low_stock": 0,
        "out_of_stock": 0,
        "hazardous": 0,
        "expiring_soon": 0,
        "shortcut_counts": {
            "hazardous": 0,
            "-20_storage": 0,
            "-80_storage": 0,
        },
        "category_stats": [],
    }
    category_stats = defaultdict(lambda: {"count": 0, "total_qty": 0.0})

    for reagent in reagents:
        quantity = reagent.get("quantity") or 0
        threshold = reagent.get("low_stock_threshold")
        threshold = 1 if threshold is None else threshold
        hazard_level = reagent.get("hazard_level") or "普通"
        storage_temp = reagent.get("storage_temp") or ""
        category = reagent.get("category") or "未分类"

        if quantity <= 0:
            stats["out_of_stock"] += 1
        elif threshold > 0 and quantity <= threshold:
            stats["low_stock"] += 1

        if hazard_level != "普通":
            stats["hazardous"] += 1
            stats["shortcut_counts"]["hazardous"] += 1
        if "-20" in storage_temp:
            stats["shortcut_counts"]["-20_storage"] += 1
        if "-80" in storage_temp:
            stats["shortcut_counts"]["-80_storage"] += 1
        if is_expiring_soon(reagent.get("expiry_date")):
            stats["expiring_soon"] += 1

        category_stats[category]["count"] += 1
        category_stats[category]["total_qty"] += quantity

    stats["category_stats"] = [
        {"category": category, "count": values["count"], "total_qty": values["total_qty"]}
        for category, values in sorted(category_stats.items(), key=lambda item: item[0])
    ]
    return stats


def serialize_usage_row(record, reagent_name, stock_unit):
    data = model_to_dict(record)
    data["reagent_name"] = reagent_name
    data["stock_unit"] = stock_unit
    return data


# ---------- Pages ----------


@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "templates", "index.html"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    if use_blob_storage():
        return jsonify({"error": "Vercel Blob 文件不通过本地路径访问"}), 404
    return send_from_directory(LOCAL_UPLOAD_FOLDER, filename)


# ---------- Category API ----------


@app.route("/api/categories", methods=["GET"])
def get_categories():
    session = SessionLocal()
    try:
        rows = session.query(Category).order_by(Category.sort_order.asc(), Category.id.asc()).all()
        return jsonify([model_to_dict(row) for row in rows])
    finally:
        session.close()


@app.route("/api/categories", methods=["POST"])
def add_category():
    data = request.json or {}
    session = SessionLocal()
    try:
        category = Category(
            name=data["name"],
            icon=data.get("icon", "📦"),
            color=data.get("color", "#6b7280"),
            sort_order=data.get("sort_order", 99),
        )
        session.add(category)
        session.commit()
        return jsonify({"success": True})
    except IntegrityError:
        session.rollback()
        return jsonify({"error": "分类已存在"}), 400
    finally:
        session.close()


# ---------- Reagent API ----------


@app.route("/api/reagents", methods=["GET"])
def get_reagents():
    session = SessionLocal()
    try:
        category = request.args.get("category")
        search = request.args.get("search")
        hazard = request.args.get("hazard_level")

        query = session.query(Reagent)
        if category:
            query = query.filter(Reagent.category == category)
        if search:
            term = f"%{search}%"
            query = query.filter(
                or_(
                    Reagent.name.ilike(term),
                    Reagent.name_en.ilike(term),
                    Reagent.cas_number.ilike(term),
                    Reagent.catalog_number.ilike(term),
                )
            )
        if hazard:
            query = query.filter(Reagent.hazard_level == hazard)

        rows = query.order_by(Reagent.updated_at.desc()).all()
        return jsonify([model_to_dict(row) for row in rows])
    finally:
        session.close()


@app.route("/api/storage-locations", methods=["GET"])
def get_storage_locations():
    session = SessionLocal()
    try:
        rows = (
            session.query(Reagent.storage_location)
            .filter(Reagent.storage_location.isnot(None))
            .filter(func.trim(Reagent.storage_location) != "")
            .distinct()
            .all()
        )
        locations = sorted({value for (value,) in rows if value and value.strip()}, key=lambda item: item.lower())
        return jsonify(locations)
    finally:
        session.close()


@app.route("/api/reagents", methods=["POST"])
def add_reagent():
    data = request.json or {}
    reagent_id = str(uuid.uuid4())[:8]
    session = SessionLocal()
    try:
        reagent = Reagent(
            id=reagent_id,
            name=data["name"],
            name_en=data.get("name_en"),
            cas_number=data.get("cas_number"),
            catalog_number=data.get("catalog_number"),
            brand=data.get("brand"),
            specification=data.get("specification"),
            purity=data.get("purity"),
            unit=data.get("unit", "瓶"),
            quantity=data.get("quantity", 0),
            low_stock_threshold=data.get("low_stock_threshold", 1),
            category=data.get("category", "常用试剂"),
            storage_location=data.get("storage_location"),
            storage_temp=data.get("storage_temp"),
            hazard_level=data.get("hazard_level", "普通"),
            hazard_info=data.get("hazard_info"),
            expiry_date=data.get("expiry_date"),
            supplier=data.get("supplier"),
            price=data.get("price"),
            notes=data.get("notes"),
            image_path=data.get("image_path"),
            created_at=now_text(),
            updated_at=now_text(),
        )
        session.add(reagent)
        session.commit()
        return jsonify({"success": True, "id": reagent_id})
    finally:
        session.close()


@app.route("/api/reagents/<reagent_id>", methods=["GET"])
def get_reagent(reagent_id):
    session = SessionLocal()
    try:
        row = session.get(Reagent, reagent_id)
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify(model_to_dict(row))
    finally:
        session.close()


@app.route("/api/reagents/<reagent_id>", methods=["PUT"])
def update_reagent(reagent_id):
    data = request.json or {}
    session = SessionLocal()
    try:
        reagent = session.get(Reagent, reagent_id)
        if not reagent:
            return jsonify({"error": "Not found"}), 404

        for key in [
            "name",
            "name_en",
            "cas_number",
            "catalog_number",
            "brand",
            "specification",
            "purity",
            "unit",
            "quantity",
            "low_stock_threshold",
            "category",
            "storage_location",
            "storage_temp",
            "hazard_level",
            "hazard_info",
            "expiry_date",
            "supplier",
            "price",
            "notes",
            "image_path",
        ]:
            if key in data:
                setattr(reagent, key, data[key])

        reagent.updated_at = now_text()
        session.commit()
        return jsonify({"success": True})
    finally:
        session.close()


@app.route("/api/reagents/<reagent_id>", methods=["DELETE"])
def delete_reagent(reagent_id):
    session = SessionLocal()
    try:
        reagent = session.get(Reagent, reagent_id)
        if not reagent:
            return jsonify({"error": "试剂不存在"}), 404
        session.delete(reagent)
        session.commit()
        return jsonify({"success": True})
    finally:
        session.close()


# ---------- Usage Record API ----------


@app.route("/api/usage", methods=["GET"])
def get_usage_records():
    session = SessionLocal()
    try:
        reagent_id = request.args.get("reagent_id")
        query = session.query(UsageRecord, Reagent.name, Reagent.unit).join(Reagent, UsageRecord.reagent_id == Reagent.id)
        if reagent_id:
            query = query.filter(UsageRecord.reagent_id == reagent_id)
        rows = query.order_by(UsageRecord.created_at.desc()).limit(200).all()
        return jsonify([serialize_usage_row(record, reagent_name, stock_unit) for record, reagent_name, stock_unit in rows])
    finally:
        session.close()


@app.route("/api/usage", methods=["POST"])
def add_usage_record():
    data = request.json or {}
    record_id = str(uuid.uuid4())[:8]
    session = SessionLocal()
    try:
        reagent = session.get(Reagent, data["reagent_id"])
        if not reagent:
            return jsonify({"error": "试剂不存在"}), 404

        try:
            raw_quantity = float(data.get("quantity", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "数量格式不正确"}), 400

        if raw_quantity <= 0:
            return jsonify({"error": "数量必须大于 0"}), 400

        usage_unit = data.get("usage_unit") or reagent.unit
        try:
            converted_quantity = round(convert_usage_to_stock_units(reagent, raw_quantity, usage_unit), 6)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        action = data.get("action", "领用")
        if action == "领用":
            if converted_quantity > (reagent.quantity or 0):
                return jsonify({"error": "领用数量超过当前库存"}), 400
            reagent.quantity = round((reagent.quantity or 0) - converted_quantity, 6)
        else:
            reagent.quantity = round((reagent.quantity or 0) + converted_quantity, 6)

        reagent.updated_at = now_text()

        normalized_usage_unit = normalize_unit(usage_unit)
        normalized_stock_unit = normalize_unit(reagent.unit)
        usage_unit_display = normalized_usage_unit[2] if normalized_usage_unit else usage_unit
        stock_unit_display = normalized_stock_unit[2] if normalized_stock_unit else reagent.unit

        session.add(
            UsageRecord(
                id=record_id,
                reagent_id=reagent.id,
                user_name=data["user_name"],
                action=action,
                quantity=raw_quantity,
                usage_unit=usage_unit_display,
                converted_quantity=converted_quantity,
                converted_unit=stock_unit_display,
                purpose=data.get("purpose"),
                notes=data.get("notes"),
                created_at=now_text(),
            )
        )
        session.commit()
        return jsonify(
            {
                "success": True,
                "new_quantity": reagent.quantity,
                "converted_quantity": converted_quantity,
                "converted_unit": stock_unit_display,
                "usage_unit": usage_unit_display,
            }
        )
    finally:
        session.close()


# ---------- Dashboard Stats ----------


@app.route("/api/stats", methods=["GET"])
def get_stats():
    session = SessionLocal()
    try:
        reagents = [model_to_dict(row) for row in session.query(Reagent).order_by(Reagent.updated_at.desc()).all()]
        stats = compute_inventory_stats(reagents)
        recent_usage_rows = (
            session.query(UsageRecord, Reagent.name, Reagent.unit)
            .join(Reagent, UsageRecord.reagent_id == Reagent.id)
            .order_by(UsageRecord.created_at.desc())
            .limit(10)
            .all()
        )
        stats["recent_usage"] = [
            serialize_usage_row(record, reagent_name, stock_unit) for record, reagent_name, stock_unit in recent_usage_rows
        ]
        return jsonify(stats)
    finally:
        session.close()


@app.route("/api/inspection/ai-review", methods=["POST"])
def ai_review_inventory():
    session = SessionLocal()
    try:
        reagents = [model_to_dict(row) for row in session.query(Reagent).order_by(Reagent.updated_at.desc()).all()]
        recent_usage = [
            serialize_usage_row(record, reagent_name, stock_unit)
            for record, reagent_name, stock_unit in (
                session.query(UsageRecord, Reagent.name, Reagent.unit)
                .join(Reagent, UsageRecord.reagent_id == Reagent.id)
                .order_by(UsageRecord.created_at.desc())
                .limit(20)
                .all()
            )
        ]
    finally:
        session.close()

    stats = compute_inventory_stats(reagents)

    _, _, model = get_ai_client_config()
    try:
        prompt = """你是实验室库存巡检审核助手。请根据给你的库存统计、试剂清单、最近领用记录，输出一份适合展示在首页上的巡检审核结果。

要求：
1. 必须基于库存数据给出审核，不要写泛泛而谈的介绍文案。
2. 优先关注：库存不足、已用完、危险试剂、临期试剂、最近频繁领用。
3. 如果没有明显风险，也要给出“当前稳定，但建议做什么”的审核结论。
4. 返回 JSON，不要返回其他文字。

返回格式：
{
  "headline": "一句中文结论，20-40字",
  "summary": "1-2句中文说明，点出最重要的问题和建议",
  "risk_level": "低/中/高",
  "actions": ["建议1", "建议2", "建议3"],
  "checked_at": "YYYY-MM-DD HH:MM"
}"""

        result = parse_model_json_content(
            post_chat_completion(
                {
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": f"{prompt}\n\n库存统计:\n{stats}\n\n试剂清单:\n{reagents}\n\n最近使用记录:\n{recent_usage}",
                        }
                    ],
                    "max_tokens": 1200,
                }
            )
        )
        return jsonify({"success": True, "data": result})
    except Exception as exc:
        return jsonify({"success": False, "error": f"AI审核失败: {str(exc)}"}), 500


# ---------- Image Upload & AI Recognition ----------


@app.route("/api/upload", methods=["POST"])
def upload_image():
    if "file" not in request.files:
        return jsonify({"error": "没有上传文件"}), 400

    file = request.files["file"]
    if file.filename == "" or not allowed_file(file.filename):
        return jsonify({"error": "不支持的文件格式"}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    content_type = file.mimetype or media_type_from_name(file.filename)

    if ext in NON_STANDARD_IMAGE_EXTENSIONS:
        from PIL import Image

        image = Image.open(file.stream)
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
        else:
            image = image.convert("RGB")
        output = io.BytesIO()
        image.save(output, "PNG")
        body = output.getvalue()
        upload_name = f"{uuid.uuid4().hex[:12]}.png"
        content_type = "image/png"
    else:
        body = file.read()
        upload_name = f"{uuid.uuid4().hex[:12]}.{ext}"

    try:
        stored = upload_bytes(upload_name, body, content_type)
        return jsonify({"success": True, **stored})
    except Exception as exc:
        return jsonify({"error": f"上传失败: {str(exc)}"}), 500


@app.route("/api/recognize", methods=["POST"])
def recognize_reagent():
    data = request.json or {}
    file_ref = data.get("file_ref") or data.get("url") or data.get("filename")
    if not file_ref:
        return jsonify({"error": "未提供文件引用"}), 400

    try:
        image_bytes, content_type, source_name = load_uploaded_bytes(file_ref)
    except FileNotFoundError:
        return jsonify({"error": "文件不存在"}), 404
    except Exception as exc:
        return jsonify({"error": f"读取文件失败: {str(exc)}"}), 500

    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
    media_type = media_type_from_name(source_name, content_type)

    session = SessionLocal()
    try:
        category_names = [row.name for row in session.query(Category).order_by(Category.sort_order.asc()).all()]
    finally:
        session.close()

    _, _, model = get_ai_client_config()
    try:
        prompt = """请识别这张试剂/药品图片中的信息，以JSON格式返回以下字段（如果无法识别某个字段则返回null）：
{
  "name": "中文名称",
  "name_en": "英文名称",
  "cas_number": "CAS号",
  "catalog_number": "货号/产品编号",
  "brand": "品牌/厂家",
  "specification": "规格（如500mL, 25g等）",
  "purity": "纯度（如AR, GR, ≥99%等）",
  "category": "分类（从以下分类中选一个最合适的：__CATEGORY_LIST__）",
  "hazard_level": "危险等级（普通/易燃/腐蚀/有毒/剧毒/易爆）",
  "hazard_info": "危险信息/GHS警示",
  "storage_temp": "储存温度建议",
  "expiry_date": "有效期（YYYY-MM-DD格式）",
  "supplier": "供应商"
}
只返回JSON，不要其他文字。"""
        prompt = prompt.replace("__CATEGORY_LIST__", "、".join(category_names) if category_names else "、".join(DEFAULT_CATEGORY_NAMES))

        response_data = post_chat_completion(
            {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{media_type};base64,{image_data}",
                                },
                            },
                            {
                                "type": "text",
                                "text": prompt,
                            },
                        ],
                    }
                ],
                "max_tokens": 1024,
            },
            timeout=120,
        )
        result = parse_model_json_content(response_data)
        return jsonify({"success": True, "data": result})
    except Exception as exc:
        return jsonify({"error": f"识别失败: {str(exc)}"}), 500


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    print("🧪 实验室试剂管理系统启动中...")
    print(f"📍 访问地址: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)
