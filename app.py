import os
import uuid
import base64
import time
import re
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
from pillow_heif import register_heif_opener
register_heif_opener()

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.path.join(DATA_DIR, 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max
DATABASE = os.path.join(DATA_DIR, 'lab_reagents.db')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'avif', 'gif', 'bmp', 'tiff', 'tif', 'heic', 'heif', 'svg'}
DEFAULT_TEAMPLUS_API_KEY = ""


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def get_ai_client_config():
    return (
        os.environ.get("BASE_URL", "https://teamplus.space/v1"),
        os.environ.get("TEAMPLUS_API_KEY", DEFAULT_TEAMPLUS_API_KEY),
        os.environ.get("MODEL", "gpt-5.4"),
    )


def parse_model_json_content(resp_data):
    import json as _json

    result_text = resp_data['choices'][0]['message']['content'].strip()
    if result_text.startswith('```'):
        result_text = result_text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    return _json.loads(result_text)


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
    match = re.search(r'(\d+(?:\.\d+)?)\s*(mL|ml|L|l|uL|ul|μL|μl|kg|g|mg)', str(specification))
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    normalized = normalize_unit(unit)
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
    stock_unit = reagent["unit"]
    stock_normalized = normalize_unit(stock_unit)
    usage_normalized = normalize_unit(usage_unit)
    if not stock_normalized or not usage_normalized:
        raise ValueError("不支持的单位")

    stock_dim, stock_factor, stock_display = stock_normalized
    usage_dim, usage_factor, usage_display = usage_normalized
    qty = float(quantity)

    if stock_dim == usage_dim and stock_display not in CONTAINER_UNITS and usage_display not in CONTAINER_UNITS:
        usage_base = qty * usage_factor
        return usage_base / stock_factor

    if stock_display in CONTAINER_UNITS:
        if usage_display == stock_display:
            return qty
        package_amount = parse_spec_amount(reagent["specification"])
        if not package_amount:
            raise ValueError("当前试剂规格未填写可换算的容量/质量，无法按该单位领用")
        if package_amount["dimension"] != usage_dim:
            raise ValueError("领用单位与规格单位不匹配，无法换算")
        usage_base = qty * usage_factor
        return usage_base / package_amount["base_amount"]

    raise ValueError("当前库存单位与领用单位无法换算")


def post_chat_completion(payload, timeout=120, max_retries=3):
    import requests as req_lib

    api_base, api_key, _ = get_ai_client_config()
    if not api_key:
        raise RuntimeError("未配置 TEAMPLUS_API_KEY")
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = req_lib.post(
                f"{api_base}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(1.2 * (attempt + 1))
            else:
                raise last_error


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS reagents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            name_en TEXT,
            cas_number TEXT,
            catalog_number TEXT,
            brand TEXT,
            specification TEXT,
            purity TEXT,
            unit TEXT DEFAULT '瓶',
            quantity REAL DEFAULT 0,
            low_stock_threshold REAL DEFAULT 1,
            category TEXT NOT NULL DEFAULT '常用试剂',
            storage_location TEXT,
            storage_temp TEXT,
            hazard_level TEXT DEFAULT '普通',
            hazard_info TEXT,
            expiry_date TEXT,
            supplier TEXT,
            price REAL,
            notes TEXT,
            image_path TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS usage_records (
            id TEXT PRIMARY KEY,
            reagent_id TEXT NOT NULL,
            user_name TEXT NOT NULL,
            action TEXT NOT NULL DEFAULT '领用',
            quantity REAL NOT NULL,
            usage_unit TEXT,
            converted_quantity REAL,
            converted_unit TEXT,
            purpose TEXT,
            notes TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (reagent_id) REFERENCES reagents(id)
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            icon TEXT,
            color TEXT,
            sort_order INTEGER DEFAULT 0
        );
    ''')

    reagent_columns = {row["name"] for row in db.execute("PRAGMA table_info(reagents)").fetchall()}
    if "low_stock_threshold" not in reagent_columns:
        db.execute("ALTER TABLE reagents ADD COLUMN low_stock_threshold REAL DEFAULT 1")

    usage_columns = {row["name"] for row in db.execute("PRAGMA table_info(usage_records)").fetchall()}
    if "usage_unit" not in usage_columns:
        db.execute("ALTER TABLE usage_records ADD COLUMN usage_unit TEXT")
    if "converted_quantity" not in usage_columns:
        db.execute("ALTER TABLE usage_records ADD COLUMN converted_quantity REAL")
    if "converted_unit" not in usage_columns:
        db.execute("ALTER TABLE usage_records ADD COLUMN converted_unit TEXT")

    # Insert default categories if empty
    existing = db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    if existing == 0:
        default_categories = [
            ('常用试剂', '🧪', '#3b82f6', 1),
            ('危险试剂', '☠️', '#ef4444', 2),
            ('-20°C冰箱', '❄️', '#06b6d4', 3),
            ('-80°C冰箱', '🧊', '#8b5cf6', 4),
            ('实验耗材', '🔬', '#10b981', 5),
            ('生物样品', '🧬', '#f59e0b', 6),
        ]
        db.executemany(
            "INSERT INTO categories (name, icon, color, sort_order) VALUES (?, ?, ?, ?)",
            default_categories
        )

    db.commit()
    db.close()


init_db()


# ---------- Pages ----------

@app.route('/')
def index():
    return send_file(os.path.join(os.path.dirname(__file__), 'templates', 'index.html'))


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ---------- Category API ----------

@app.route('/api/categories', methods=['GET'])
def get_categories():
    db = get_db()
    rows = db.execute("SELECT * FROM categories ORDER BY sort_order").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/categories', methods=['POST'])
def add_category():
    data = request.json
    db = get_db()
    db.execute(
        "INSERT INTO categories (name, icon, color, sort_order) VALUES (?, ?, ?, ?)",
        (data['name'], data.get('icon', '📦'), data.get('color', '#6b7280'),
         data.get('sort_order', 99))
    )
    db.commit()
    db.close()
    return jsonify({"success": True})


# ---------- Reagent API ----------

@app.route('/api/reagents', methods=['GET'])
def get_reagents():
    db = get_db()
    category = request.args.get('category')
    search = request.args.get('search')
    hazard = request.args.get('hazard_level')

    query = "SELECT * FROM reagents WHERE 1=1"
    params = []

    if category:
        query += " AND category = ?"
        params.append(category)
    if search:
        query += " AND (name LIKE ? OR name_en LIKE ? OR cas_number LIKE ? OR catalog_number LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term, term])
    if hazard:
        query += " AND hazard_level = ?"
        params.append(hazard)

    query += " ORDER BY updated_at DESC"
    rows = db.execute(query, params).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/storage-locations', methods=['GET'])
def get_storage_locations():
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT TRIM(storage_location) AS storage_location
        FROM reagents
        WHERE storage_location IS NOT NULL AND TRIM(storage_location) != ''
        ORDER BY storage_location COLLATE NOCASE
    """).fetchall()
    db.close()
    return jsonify([r["storage_location"] for r in rows])


@app.route('/api/reagents', methods=['POST'])
def add_reagent():
    data = request.json
    reagent_id = str(uuid.uuid4())[:8]
    db = get_db()
    db.execute('''
        INSERT INTO reagents (id, name, name_en, cas_number, catalog_number, brand,
            specification, purity, unit, quantity, low_stock_threshold, category, storage_location,
            storage_temp, hazard_level, hazard_info, expiry_date, supplier, price, notes, image_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        reagent_id, data['name'], data.get('name_en'), data.get('cas_number'),
        data.get('catalog_number'), data.get('brand'), data.get('specification'),
        data.get('purity'), data.get('unit', '瓶'), data.get('quantity', 0),
        data.get('low_stock_threshold', 1),
        data.get('category', '常用试剂'), data.get('storage_location'),
        data.get('storage_temp'), data.get('hazard_level', '普通'),
        data.get('hazard_info'), data.get('expiry_date'), data.get('supplier'),
        data.get('price'), data.get('notes'), data.get('image_path')
    ))
    db.commit()
    db.close()
    return jsonify({"success": True, "id": reagent_id})


@app.route('/api/reagents/<reagent_id>', methods=['GET'])
def get_reagent(reagent_id):
    db = get_db()
    row = db.execute("SELECT * FROM reagents WHERE id = ?", (reagent_id,)).fetchone()
    db.close()
    if row:
        return jsonify(dict(row))
    return jsonify({"error": "Not found"}), 404


@app.route('/api/reagents/<reagent_id>', methods=['PUT'])
def update_reagent(reagent_id):
    data = request.json
    db = get_db()
    fields = []
    values = []
    for key in ['name', 'name_en', 'cas_number', 'catalog_number', 'brand',
                'specification', 'purity', 'unit', 'quantity', 'low_stock_threshold', 'category',
                'storage_location', 'storage_temp', 'hazard_level', 'hazard_info',
                'expiry_date', 'supplier', 'price', 'notes', 'image_path']:
        if key in data:
            fields.append(f"{key} = ?")
            values.append(data[key])

    if fields:
        fields.append("updated_at = datetime('now', 'localtime')")
        values.append(reagent_id)
        db.execute(f"UPDATE reagents SET {', '.join(fields)} WHERE id = ?", values)
        db.commit()

    db.close()
    return jsonify({"success": True})


@app.route('/api/reagents/<reagent_id>', methods=['DELETE'])
def delete_reagent(reagent_id):
    db = get_db()
    db.execute("DELETE FROM usage_records WHERE reagent_id = ?", (reagent_id,))
    db.execute("DELETE FROM reagents WHERE id = ?", (reagent_id,))
    db.commit()
    db.close()
    return jsonify({"success": True})


# ---------- Usage Record API ----------

@app.route('/api/usage', methods=['GET'])
def get_usage_records():
    db = get_db()
    reagent_id = request.args.get('reagent_id')
    query = '''
        SELECT u.*, r.name as reagent_name, r.unit as stock_unit
        FROM usage_records u
        JOIN reagents r ON u.reagent_id = r.id
    '''
    params = []
    if reagent_id:
        query += " WHERE u.reagent_id = ?"
        params.append(reagent_id)
    query += " ORDER BY u.created_at DESC LIMIT 200"
    rows = db.execute(query, params).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/usage', methods=['POST'])
def add_usage_record():
    data = request.json or {}
    record_id = str(uuid.uuid4())[:8]
    db = get_db()

    reagent = db.execute("SELECT quantity, unit, specification FROM reagents WHERE id = ?",
                         (data['reagent_id'],)).fetchone()
    if not reagent:
        db.close()
        return jsonify({"error": "试剂不存在"}), 404

    try:
        raw_quantity = float(data.get('quantity', 0))
    except (TypeError, ValueError):
        db.close()
        return jsonify({"error": "数量格式不正确"}), 400

    if raw_quantity <= 0:
        db.close()
        return jsonify({"error": "数量必须大于 0"}), 400

    usage_unit = data.get('usage_unit') or reagent['unit']
    try:
        converted_quantity = round(convert_usage_to_stock_units(reagent, raw_quantity, usage_unit), 6)
    except ValueError as e:
        db.close()
        return jsonify({"error": str(e)}), 400

    action = data.get('action', '领用')
    if action == '领用':
        if converted_quantity > (reagent['quantity'] or 0):
            db.close()
            return jsonify({"error": "领用数量超过当前库存"}), 400
        new_qty = round((reagent['quantity'] or 0) - converted_quantity, 6)
    else:  # 入库
        new_qty = round((reagent['quantity'] or 0) + converted_quantity, 6)

    normalized_usage_unit = normalize_unit(usage_unit)
    normalized_stock_unit = normalize_unit(reagent['unit'])
    usage_unit_display = normalized_usage_unit[2] if normalized_usage_unit else usage_unit
    stock_unit_display = normalized_stock_unit[2] if normalized_stock_unit else reagent['unit']

    db.execute("UPDATE reagents SET quantity = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
               (new_qty, data['reagent_id']))

    db.execute('''
        INSERT INTO usage_records (
            id, reagent_id, user_name, action, quantity, usage_unit,
            converted_quantity, converted_unit, purpose, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (record_id, data['reagent_id'], data['user_name'],
          action, raw_quantity, usage_unit_display, converted_quantity,
          stock_unit_display, data.get('purpose'), data.get('notes')))

    db.commit()
    db.close()
    return jsonify({
        "success": True,
        "new_quantity": new_qty,
        "converted_quantity": converted_quantity,
        "converted_unit": stock_unit_display,
        "usage_unit": usage_unit_display,
    })


# ---------- Dashboard Stats ----------

@app.route('/api/stats', methods=['GET'])
def get_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM reagents").fetchone()[0]
    low_stock = db.execute("""
        SELECT COUNT(*)
        FROM reagents
        WHERE quantity > 0
          AND COALESCE(low_stock_threshold, 1) > 0
          AND quantity <= COALESCE(low_stock_threshold, 1)
    """).fetchone()[0]
    out_of_stock = db.execute("SELECT COUNT(*) FROM reagents WHERE quantity <= 0").fetchone()[0]
    hazardous = db.execute("SELECT COUNT(*) FROM reagents WHERE hazard_level != '普通'").fetchone()[0]
    expiring = db.execute(
        """
        SELECT COUNT(*)
        FROM reagents
        WHERE expiry_date IS NOT NULL
          AND TRIM(expiry_date) != ''
          AND expiry_date <= date('now', '+30 days', 'localtime')
        """
    ).fetchone()[0]
    frozen_20 = db.execute("SELECT COUNT(*) FROM reagents WHERE COALESCE(storage_temp, '') LIKE '%-20%'").fetchone()[0]
    frozen_80 = db.execute("SELECT COUNT(*) FROM reagents WHERE COALESCE(storage_temp, '') LIKE '%-80%'").fetchone()[0]

    category_stats = db.execute(
        "SELECT category, COUNT(*) as count, SUM(quantity) as total_qty FROM reagents GROUP BY category"
    ).fetchall()

    recent_usage = db.execute('''
        SELECT u.*, r.name as reagent_name, r.unit as stock_unit
        FROM usage_records u JOIN reagents r ON u.reagent_id = r.id
        ORDER BY u.created_at DESC LIMIT 10
    ''').fetchall()

    db.close()
    return jsonify({
        "total": total,
        "low_stock": low_stock,
        "out_of_stock": out_of_stock,
        "hazardous": hazardous,
        "expiring_soon": expiring,
        "shortcut_counts": {
            "hazardous": hazardous,
            "-20_storage": frozen_20,
            "-80_storage": frozen_80,
        },
        "category_stats": [dict(r) for r in category_stats],
        "recent_usage": [dict(r) for r in recent_usage],
    })


@app.route('/api/inspection/ai-review', methods=['POST'])
def ai_review_inventory():
    db = get_db()
    reagents = [dict(r) for r in db.execute('''
        SELECT id, name, name_en, cas_number, catalog_number, brand, specification, purity,
               unit, quantity, low_stock_threshold, category, storage_location, storage_temp, hazard_level,
               hazard_info, expiry_date, supplier, notes, updated_at
        FROM reagents
        ORDER BY updated_at DESC
    ''').fetchall()]
    recent_usage = [dict(r) for r in db.execute('''
        SELECT u.created_at, u.user_name, u.action, u.quantity, u.usage_unit, u.converted_quantity,
               u.converted_unit, r.name AS reagent_name, r.unit AS stock_unit
        FROM usage_records u
        JOIN reagents r ON u.reagent_id = r.id
        ORDER BY u.created_at DESC LIMIT 20
    ''').fetchall()]
    db.close()

    stats = {
        "total": len(reagents),
        "low_stock": sum(
            1 for r in reagents
            if (r.get('quantity') or 0) > 0
            and (r.get('low_stock_threshold') if r.get('low_stock_threshold') is not None else 1) > 0
            and (r.get('quantity') or 0) <= (r.get('low_stock_threshold') if r.get('low_stock_threshold') is not None else 1)
        ),
        "out_of_stock": sum(1 for r in reagents if (r.get('quantity') or 0) <= 0),
        "hazardous": sum(1 for r in reagents if (r.get('hazard_level') or '普通') != '普通'),
        "expiring_soon": sum(
            1 for r in reagents
            if r.get('expiry_date') and str(r['expiry_date']).strip()
            and r['expiry_date'] <= datetime.now().strftime('%Y-%m-%d')
        ),
    }

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

        result = parse_model_json_content(post_chat_completion(
            {
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": f"{prompt}\n\n库存统计:\n{stats}\n\n试剂清单:\n{reagents}\n\n最近使用记录:\n{recent_usage}"
                }],
                "max_tokens": 1200,
            }
        ))
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": f"AI审核失败: {str(e)}"}), 500


# ---------- Image Upload & AI Recognition ----------

@app.route('/api/upload', methods=['POST'])
def upload_image():
    if 'file' not in request.files:
        return jsonify({"error": "没有上传文件"}), 400

    file = request.files['file']
    if file.filename == '' or not allowed_file(file.filename):
        return jsonify({"error": "不支持的文件格式"}), 400

    ext = file.filename.rsplit('.', 1)[1].lower()

    # Convert non-standard formats to PNG for API compatibility
    if ext in ('avif', 'heic', 'heif', 'bmp', 'tiff', 'tif', 'svg'):
        from PIL import Image
        img = Image.open(file.stream)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
        else:
            img = img.convert('RGB')
        filename = f"{uuid.uuid4().hex[:12]}.png"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        img.save(filepath, 'PNG')
    else:
        filename = f"{uuid.uuid4().hex[:12]}.{ext}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

    return jsonify({"success": True, "filename": filename, "url": f"/uploads/{filename}"})


@app.route('/api/recognize', methods=['POST'])
def recognize_reagent():
    """Use GPT-5.4 via teamplus API to recognize reagent info from uploaded image."""
    data = request.json
    filename = data.get('filename')
    if not filename:
        return jsonify({"error": "未提供文件名"}), 400

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "文件不存在"}), 404

    # Read and encode image
    with open(filepath, 'rb') as f:
        image_data = base64.standard_b64encode(f.read()).decode('utf-8')

    ext = filename.rsplit('.', 1)[1].lower()
    media_type = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
    db = get_db()
    category_names = [r["name"] for r in db.execute("SELECT name FROM categories ORDER BY sort_order").fetchall()]
    db.close()

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
        prompt = prompt.replace(
            "__CATEGORY_LIST__",
            "、".join(category_names) if category_names else "常用试剂、危险试剂、-20°C冰箱、-80°C冰箱、实验耗材、生物样品"
        )

        resp_data = post_chat_completion(
            {
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{image_data}"
                            }
                        },
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                }],
                "max_tokens": 1024,
            },
            timeout=120,
        )
        result = parse_model_json_content(resp_data)
        return jsonify({"success": True, "data": result})

    except Exception as e:
        return jsonify({"error": f"识别失败: {str(e)}"}), 500


if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    init_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    print("🧪 实验室试剂管理系统启动中...")
    print(f"📍 访问地址: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)
