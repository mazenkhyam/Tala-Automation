"""
TALA — نظام القيود المحاسبية الذكي
Flask Backend — يخدم الواجهة (نفس تصميم tala_full_system.html)
ويوفر API لكل العمليات (CRUD + رفع الكشف + المطابقة + التصدير)
"""

import os
import re
import sqlite3
import datetime
from io import BytesIO

from flask import Flask, render_template, request, jsonify, send_file

from parser import parse_statement
from matcher import match_entity, build_journal_lines, BANK_ACCOUNTS
from exporter import journals_to_qb_csv, journals_to_excel, summary_stats
import pandas as pd

app = Flask(__name__)

DB_PATH = os.environ.get("DB_PATH", "tala_data.db")


# ══════════════════════════════════════════════════════════════
# DB helpers
# ══════════════════════════════════════════════════════════════
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_tables(conn)
    return conn


def _ensure_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            acc_code TEXT UNIQUE NOT NULL,
            acc_name TEXT NOT NULL,
            acc_type TEXT DEFAULT 'أصول'
        );
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bank_name TEXT NOT NULL,
            sys_name  TEXT NOT NULL,
            acc_link  TEXT NOT NULL,
            sup_type  TEXT
        );
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bank_name TEXT NOT NULL,
            sys_name  TEXT NOT NULL,
            acc_link  TEXT NOT NULL,
            cust_type TEXT
        );
        CREATE TABLE IF NOT EXISTS expenses_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bank_description  TEXT NOT NULL,
            expense_category  TEXT,
            acc_link          TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS locations (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            is_main   TEXT DEFAULT 'موقع رئيسي',
            parent_id INTEGER,
            city      TEXT
        );
        CREATE TABLE IF NOT EXISTS banks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            bank_key   TEXT UNIQUE NOT NULL,
            bank_label TEXT NOT NULL,
            acc_link   TEXT NOT NULL,
            parser_type TEXT DEFAULT 'Generic'
        );
        CREATE TABLE IF NOT EXISTS journals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            journal_no    TEXT NOT NULL,
            journal_date  TEXT NOT NULL,
            line_no       INTEGER DEFAULT 1,
            account_code  TEXT NOT NULL,
            account_name  TEXT NOT NULL,
            debit         REAL DEFAULT 0,
            credit        REAL DEFAULT 0,
            entity_name   TEXT,
            memo          TEXT,
            location      TEXT,
            tax_rate      REAL DEFAULT 0,
            tax_amount    REAL DEFAULT 0,
            source_bank   TEXT,
            match_method  TEXT,
            status        TEXT DEFAULT 'معتمد',
            created_at    TEXT DEFAULT (datetime('now','localtime'))
        );

        -- ALTER guard: قديماً acc_type قد لا يكون موجوداً
    """)
    # تأكد من وجود acc_type (لو الجدول قديم بدون هذا العمود)
    cols = [r['name'] for r in conn.execute("PRAGMA table_info(accounts)")]
    if 'acc_type' not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN acc_type TEXT DEFAULT 'أصول'")

    # تأكد من وجود كل أعمدة journals الحديثة (لو القاعدة قديمة)
    jcols = [r['name'] for r in conn.execute("PRAGMA table_info(journals)")]
    if 'status' not in jcols:
        conn.execute("ALTER TABLE journals ADD COLUMN status TEXT DEFAULT 'معتمد'")
    if 'line_no' not in jcols:
        conn.execute("ALTER TABLE journals ADD COLUMN line_no INTEGER DEFAULT 1")
    if 'source_bank' not in jcols:
        conn.execute("ALTER TABLE journals ADD COLUMN source_bank TEXT")
    conn.commit()

    # بذر جدول البنوك من BANK_ACCOUNTS مرة واحدة فقط (أول تشغيل بعد الترقية)
    bank_count = conn.execute("SELECT COUNT(*) c FROM banks").fetchone()['c']
    if bank_count == 0:
        seed_labels = {
            'AlAhli': 'الأهلي السعودي (AlAhli)',
            'Alinma': 'بنك الإنماء (Alinma)',
            'Riyad':  'بنك الرياض (Riyad)',
        }
        for key, acc_link in BANK_ACCOUNTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO banks (bank_key, bank_label, acc_link, parser_type) VALUES (?,?,?,?)",
                (key, seed_labels.get(key, key), acc_link, key)
            )
        conn.commit()


def rows_to_list(rows):
    return [dict(r) for r in rows]


def get_bank_accounts():
    """يرجع dict {bank_label: acc_link} من جدول banks (بدلاً من القائمة الثابتة BANK_ACCOUNTS)."""
    conn = get_conn()
    rows = conn.execute("SELECT bank_label, acc_link FROM banks").fetchall()
    conn.close()
    if not rows:
        return dict(BANK_ACCOUNTS)  # احتياطي لو الجدول فاضي لأي سبب
    return {r['bank_label']: r['acc_link'] for r in rows}


# ══════════════════════════════════════════════════════════════
# Master data للمطابقة
# ══════════════════════════════════════════════════════════════
def load_master():
    conn = get_conn()
    master = []
    for r in conn.execute("SELECT bank_name, sys_name, acc_link FROM suppliers"):
        master.append({
            'bank_key': str(r['bank_name']).upper().strip(),
            'sys_name': str(r['sys_name']) if r['sys_name'] else str(r['bank_name']),
            'acc_link': str(r['acc_link']),
            'entity_type': 'supplier',
        })
    for r in conn.execute("SELECT bank_name, sys_name, acc_link FROM customers"):
        master.append({
            'bank_key': str(r['bank_name']).upper().strip(),
            'sys_name': str(r['sys_name']) if r['sys_name'] else str(r['bank_name']),
            'acc_link': str(r['acc_link']),
            'entity_type': 'customer',
        })
    for r in conn.execute("SELECT bank_description, expense_category, acc_link FROM expenses_map"):
        sys_name = r['expense_category'] if r['expense_category'] else r['bank_description']
        master.append({
            'bank_key': str(r['bank_description']).upper().strip(),
            'sys_name': str(sys_name),
            'acc_link': str(r['acc_link']),
            'entity_type': 'expense',
        })
    conn.close()
    return master


# ══════════════════════════════════════════════════════════════
# الصفحة الرئيسية
# ══════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return render_template('index.html')


# ══════════════════════════════════════════════════════════════
# API — Dashboard
# ══════════════════════════════════════════════════════════════
@app.route('/api/dashboard')
def api_dashboard():
    conn = get_conn()
    stats = {}
    for tbl, key in [('accounts', 'accounts'), ('suppliers', 'suppliers'),
                      ('customers', 'customers'), ('expenses_map', 'expenses'),
                      ('locations', 'locations')]:
        stats[key] = conn.execute(f"SELECT COUNT(*) c FROM {tbl}").fetchone()['c']

    df = pd.read_sql_query("SELECT debit, credit, journal_date, account_name, entity_name "
                            "FROM journals ORDER BY created_at DESC LIMIT 10", conn)

    # إجمالي الإيداعات/المسحوبات الحقيقي = من سطور حسابات البنوك فقط
    # (جمع كل الجدول يعطي مدين=دائن دائماً لأن كل قيد متوازن بالتعريف، وهذا غير مفيد كمؤشر كاش فلو)
    bank_codes = [acc.split(' - ')[0] for acc in get_bank_accounts().values()]
    placeholders = ','.join('?' * len(bank_codes))
    cash_row = conn.execute(f"""
        SELECT COALESCE(SUM(CASE WHEN debit > 0 THEN debit ELSE 0 END),0) inflow,
               COALESCE(SUM(CASE WHEN credit > 0 THEN credit ELSE 0 END),0) outflow
        FROM journals WHERE account_code IN ({placeholders})
    """, bank_codes).fetchone()
    total_inflow = cash_row['inflow']
    total_outflow = cash_row['outflow']

    # أكبر 5 بنود مصروفات (حسب إجمالي المدين على حسابات غير حسابات البنوك)
    top_expenses_rows = conn.execute(f"""
        SELECT account_name, SUM(debit) total
        FROM journals
        WHERE debit > 0 AND account_code NOT IN ({placeholders})
        GROUP BY account_name ORDER BY total DESC LIMIT 5
    """, bank_codes).fetchall()
    top_expenses = [{'name': r['account_name'], 'total': float(r['total'])} for r in top_expenses_rows]

    # عدد القيود المعتمدة/المسودة
    status_rows = conn.execute(
        "SELECT status, COUNT(DISTINCT journal_no) c FROM journals GROUP BY status"
    ).fetchall()
    status_counts = {r['status']: r['c'] for r in status_rows}

    # رصيد كل حساب بنكي حالياً (تقديري: مدين - دائن منذ بداية السجل)
    bank_balances = []
    for bank_name, acc_link in get_bank_accounts().items():
        code = acc_link.split(' - ')[0]
        row = conn.execute(
            "SELECT COALESCE(SUM(debit),0) d, COALESCE(SUM(credit),0) c FROM journals WHERE account_code=?",
            (code,)
        ).fetchone()
        balance = float(row['d']) - float(row['c'])
        if row['d'] or row['c']:
            bank_balances.append({'bank': bank_name, 'account': acc_link, 'balance': balance})

    conn.close()
    return jsonify({
        'stats': {k: int(v) for k, v in stats.items()},
        'total_inflow': float(total_inflow),
        'total_outflow': float(total_outflow),
        'net_cash_flow': float(total_inflow - total_outflow),
        'top_expenses': top_expenses,
        'status_counts': status_counts,
        'bank_balances': bank_balances,
        'recent_journals': df.to_dict('records'),
    })


# ══════════════════════════════════════════════════════════════
# API — Accounts (شجرة الحسابات)
# ══════════════════════════════════════════════════════════════
@app.route('/api/accounts', methods=['GET'])
def api_accounts_list():
    conn = get_conn()
    rows = rows_to_list(conn.execute(
        "SELECT id, acc_code, acc_name, acc_type FROM accounts ORDER BY acc_code"
    ).fetchall())
    conn.close()
    return jsonify(rows)


@app.route('/api/accounts', methods=['POST'])
def api_accounts_add():
    data = request.json
    code = str(data.get('acc_code', '')).strip()
    name = str(data.get('acc_name', '')).strip()
    atype = data.get('acc_type', 'أصول')
    if not code or not name:
        return jsonify({'error': 'اكمل البيانات'}), 400
    conn = get_conn()
    try:
        conn.execute("INSERT INTO accounts (acc_code, acc_name, acc_type) VALUES (?,?,?)",
                     (code, name, atype))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'الكود مكرر'}), 400
    conn.close()
    return jsonify({'success': True})


@app.route('/api/accounts/<code>', methods=['DELETE'])
def api_accounts_delete(code):
    conn = get_conn()
    conn.execute("DELETE FROM accounts WHERE acc_code=?", (code,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ══════════════════════════════════════════════════════════════
# API — Suppliers
# ══════════════════════════════════════════════════════════════
@app.route('/api/suppliers', methods=['GET'])
def api_suppliers_list():
    conn = get_conn()
    rows = rows_to_list(conn.execute(
        "SELECT id, bank_name, sys_name, acc_link, sup_type FROM suppliers ORDER BY sys_name"
    ).fetchall())
    conn.close()
    return jsonify(rows)


@app.route('/api/suppliers', methods=['POST'])
def api_suppliers_add():
    data = request.json
    bank = str(data.get('bank_name', '')).strip()
    sys_ = str(data.get('sys_name', '')).strip()
    acc = str(data.get('acc_link', '')).strip()
    sup_type = data.get('sup_type', 'Trade')
    if not bank or not sys_:
        return jsonify({'error': 'اكمل البيانات'}), 400
    conn = get_conn()
    conn.execute("INSERT INTO suppliers (bank_name, sys_name, acc_link, sup_type) VALUES (?,?,?,?)",
                 (bank, sys_, acc, sup_type))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/suppliers/<int:item_id>', methods=['DELETE'])
def api_suppliers_delete(item_id):
    conn = get_conn()
    conn.execute("DELETE FROM suppliers WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ══════════════════════════════════════════════════════════════
# API — Customers
# ══════════════════════════════════════════════════════════════
@app.route('/api/customers', methods=['GET'])
def api_customers_list():
    conn = get_conn()
    rows = rows_to_list(conn.execute(
        "SELECT id, bank_name, sys_name, acc_link, cust_type FROM customers ORDER BY sys_name"
    ).fetchall())
    conn.close()
    return jsonify(rows)


@app.route('/api/customers', methods=['POST'])
def api_customers_add():
    data = request.json
    bank = str(data.get('bank_name', '')).strip()
    sys_ = str(data.get('sys_name', '')).strip()
    acc = str(data.get('acc_link', '')).strip()
    cust_type = data.get('cust_type', 'Trade')
    if not bank or not sys_:
        return jsonify({'error': 'اكمل البيانات'}), 400
    conn = get_conn()
    conn.execute("INSERT INTO customers (bank_name, sys_name, acc_link, cust_type) VALUES (?,?,?,?)",
                 (bank, sys_, acc, cust_type))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/customers/<int:item_id>', methods=['DELETE'])
def api_customers_delete(item_id):
    conn = get_conn()
    conn.execute("DELETE FROM customers WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ══════════════════════════════════════════════════════════════
# API — Expenses Map
# ══════════════════════════════════════════════════════════════
@app.route('/api/expenses', methods=['GET'])
def api_expenses_list():
    conn = get_conn()
    rows = rows_to_list(conn.execute(
        "SELECT id, bank_description, expense_category, acc_link FROM expenses_map"
    ).fetchall())
    conn.close()
    return jsonify(rows)


@app.route('/api/expenses', methods=['POST'])
def api_expenses_add():
    data = request.json
    bank = str(data.get('bank_description', '')).strip()
    cat = str(data.get('expense_category', '')).strip()
    acc = str(data.get('acc_link', '')).strip()
    if not bank:
        return jsonify({'error': 'اكمل البيانات'}), 400
    conn = get_conn()
    conn.execute("INSERT INTO expenses_map (bank_description, expense_category, acc_link) VALUES (?,?,?)",
                 (bank, cat or bank, acc))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/expenses/<int:item_id>', methods=['DELETE'])
def api_expenses_delete(item_id):
    conn = get_conn()
    conn.execute("DELETE FROM expenses_map WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ══════════════════════════════════════════════════════════════
# API — Locations
# ══════════════════════════════════════════════════════════════
@app.route('/api/locations', methods=['GET'])
def api_locations_list():
    conn = get_conn()
    rows = rows_to_list(conn.execute(
        "SELECT id, name, is_main, parent_id, city FROM locations ORDER BY is_main DESC, name"
    ).fetchall())
    conn.close()
    return jsonify(rows)


@app.route('/api/locations', methods=['POST'])
def api_locations_add():
    data = request.json
    name = str(data.get('name', '')).strip()
    is_main = data.get('is_main', 'موقع رئيسي')
    city = data.get('city', 'الرياض')
    if not name:
        return jsonify({'error': 'اكتب اسم الموقع'}), 400
    conn = get_conn()
    conn.execute("INSERT INTO locations (name, is_main, city) VALUES (?,?,?)",
                 (name, is_main, city))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/locations/<int:item_id>', methods=['DELETE'])
def api_locations_delete(item_id):
    conn = get_conn()
    conn.execute("DELETE FROM locations WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ══════════════════════════════════════════════════════════════
# API — Banks (إدارة البنوك: إضافة / تعديل / حذف)
# ══════════════════════════════════════════════════════════════
@app.route('/api/banks', methods=['GET'])
def api_banks_list():
    conn = get_conn()
    rows = rows_to_list(conn.execute(
        "SELECT id, bank_key, bank_label, acc_link, parser_type FROM banks ORDER BY id"
    ).fetchall())
    conn.close()
    return jsonify(rows)


@app.route('/api/banks', methods=['POST'])
def api_banks_add():
    data = request.json
    bank_key = str(data.get('bank_key', '')).strip()
    bank_label = str(data.get('bank_label', '')).strip()
    acc_link = str(data.get('acc_link', '')).strip()
    parser_type = data.get('parser_type', 'Generic')
    if not bank_key or not bank_label or not acc_link:
        return jsonify({'error': 'اكمل بيانات البنك (المفتاح، الاسم، الحساب)'}), 400
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO banks (bank_key, bank_label, acc_link, parser_type) VALUES (?,?,?,?)",
            (bank_key, bank_label, acc_link, parser_type)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'مفتاح البنك مستخدم بالفعل'}), 400
    conn.close()
    return jsonify({'success': True})


@app.route('/api/banks/<int:item_id>', methods=['PUT'])
def api_banks_update(item_id):
    data = request.json
    conn = get_conn()
    conn.execute(
        "UPDATE banks SET bank_label=?, acc_link=?, parser_type=? WHERE id=?",
        (data.get('bank_label', ''), data.get('acc_link', ''), data.get('parser_type', 'Generic'), item_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/banks/<int:item_id>', methods=['DELETE'])
def api_banks_delete(item_id):
    conn = get_conn()
    conn.execute("DELETE FROM banks WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ══════════════════════════════════════════════════════════════
# API — Master Data (موحّد)
# ══════════════════════════════════════════════════════════════
@app.route('/api/master')
def api_master():
    conn = get_conn()
    result = []
    for r in conn.execute("SELECT bank_name, sys_name, acc_link FROM suppliers"):
        result.append({'sys': r['sys_name'], 'bank': r['bank_name'],
                       'acc': r['acc_link'], 'type': 'مورد'})
    for r in conn.execute("SELECT bank_name, sys_name, acc_link FROM customers"):
        result.append({'sys': r['sys_name'], 'bank': r['bank_name'],
                       'acc': r['acc_link'], 'type': 'عميل'})
    for r in conn.execute("SELECT bank_description, expense_category, acc_link FROM expenses_map"):
        cat = r['expense_category'] or r['bank_description']
        result.append({'sys': cat, 'bank': r['bank_description'],
                       'acc': r['acc_link'], 'type': 'مصروف'})
    conn.close()
    return jsonify(result)


# ══════════════════════════════════════════════════════════════
# API — Upload & Process Bank Statement
# ══════════════════════════════════════════════════════════════
@app.route('/api/upload', methods=['POST'])
def api_upload():
    if 'file' not in request.files:
        return jsonify({'error': 'لم يتم رفع ملف'}), 400

    file = request.files['file']
    bank_hint = request.form.get('bank', 'Auto')
    bank_account = request.form.get('bank_account', '111100 - Cash & Cash Equivalents')

    file_bytes = file.read()
    try:
        df_raw, detected_bank = parse_statement(file_bytes, file.filename, bank_hint=bank_hint)
    except Exception as e:
        return jsonify({'error': f'فشل قراءة الملف: {e}'}), 400

    if df_raw.empty:
        return jsonify({'error': 'لم يُعثر على بيانات في الملف'}), 400

    # الموقع الافتراضي = "Management" دائماً (إن وُجد في جدول المواقع)، وإلا أول موقع رئيسي مسجّل
    conn = get_conn()
    default_loc_row = conn.execute(
        "SELECT name FROM locations WHERE name = 'Management' LIMIT 1"
    ).fetchone()
    if not default_loc_row:
        default_loc_row = conn.execute(
            "SELECT name FROM locations ORDER BY is_main DESC, id ASC LIMIT 1"
        ).fetchone()
    default_location = default_loc_row['name'] if default_loc_row else 'Management'
    conn.close()

    master = load_master()
    enriched = []
    for i, tx in df_raw.iterrows():
        match = match_entity(tx['bank_desc'], master)
        enriched.append({
            'idx': int(i),
            'date': tx['date'],
            'bank_desc': tx['bank_desc'],
            'amount': float(tx['amount']),
            'sys_name': match['sys_name'],
            'acc_link': match['acc_link'],
            'entity_type': match['entity_type'],
            'method': match['method'],
            'confidence': match['confidence'],
            'location': default_location,
            'memo': tx['bank_desc'],
            'approved': match['confidence'] >= 90,
            'bank_account': bank_account,
        })

    return jsonify({
        'detected_bank': detected_bank,
        'transactions': enriched,
        'count': len(enriched),
    })


# ══════════════════════════════════════════════════════════════
# API — Generate & Export Journals
# ══════════════════════════════════════════════════════════════
@app.route('/api/generate-journal', methods=['POST'])
def api_generate_journal():
    data = request.json
    txs = data.get('transactions', [])
    approved = [t for t in txs if t.get('approved')]

    if not approved:
        return jsonify({'error': 'لا توجد عمليات معتمدة'}), 400

    all_lines = []
    day_counters = {}
    for t in approved:
        tx_date = t['date']
        day_counters[tx_date] = day_counters.get(tx_date, 0) + 1
        # رقم القيد = تاريخ يومية البنك + رقم تسلسلي ضمن نفس اليوم
        # (لمنع تصادم القيود عند وجود أكثر من عملية في يوم واحد)
        jno = f"{tx_date}-{day_counters[tx_date]:02d}"
        lines = build_journal_lines(
            tx={
                'date': t['date'],
                'amount': t['amount'],
                'bank_desc': t['bank_desc'],
                'memo': t.get('memo', t['bank_desc']),
                'location': t.get('location', ''),
            },
            bank_account=t.get('bank_account', '111100 - Cash & Cash Equivalents'),
            match={
                'sys_name': t.get('sys_name', ''),
                'acc_link': t.get('acc_link', ''),
                'entity_type': t.get('entity_type', 'unknown'),
            }
        )
        for ln in lines:
            ln['journal_no'] = jno
            ln['match_method'] = t.get('method', '')
        all_lines.extend(lines)

    jdf = pd.DataFrame(all_lines)
    stats = summary_stats(jdf)
    stats = {
        'total_debit': float(stats['total_debit']),
        'total_credit': float(stats['total_credit']),
        'balanced': bool(stats['balanced']),
        'diff': float(stats['diff']),
        'lines': int(stats['lines']),
        'entries': int(stats['entries']),
    }

    return jsonify({
        'lines': jdf.to_dict('records'),
        'stats': stats,
    })


@app.route('/api/export/csv', methods=['POST'])
def api_export_csv():
    data = request.json
    jdf = pd.DataFrame(data.get('lines', []))
    csv_bytes = journals_to_qb_csv(jdf)
    return send_file(
        BytesIO(csv_bytes),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f"QB_Journal_{datetime.date.today()}.csv",
    )


@app.route('/api/export/journal/<journal_no>', methods=['GET'])
def api_export_single_journal(journal_no):
    """تصدير قيد واحد فقط (محفوظ في السجل) بصيغة QuickBooks CSV"""
    conn = get_conn()
    df = pd.read_sql_query("""
        SELECT journal_no, journal_date, account_code, debit, credit,
               memo, entity_name, location, tax_amount
        FROM journals WHERE journal_no=? ORDER BY line_no ASC
    """, conn, params=[journal_no])
    conn.close()
    if df.empty:
        return jsonify({'error': 'القيد غير موجود'}), 404
    csv_bytes = journals_to_qb_csv(df)
    return send_file(
        BytesIO(csv_bytes),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f"QB_Journal_{journal_no}.csv",
    )


@app.route('/api/export/excel', methods=['POST'])
def api_export_excel():
    data = request.json
    jdf = pd.DataFrame(data.get('lines', []))
    xl_bytes = journals_to_excel(jdf)
    return send_file(
        BytesIO(xl_bytes),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f"Journal_{datetime.date.today()}.xlsx",
    )


@app.route('/api/save-journal', methods=['POST'])
def api_save_journal():
    data = request.json
    lines = data.get('lines', [])
    status = data.get('status', 'معتمد')  # معتمد أو مسودة
    conn = get_conn()
    # حساب line_no تتابعي ضمن كل journal_no
    line_counters = {}
    for ln in lines:
        jno = ln.get('journal_no', '')
        line_counters[jno] = line_counters.get(jno, 0) + 1
        line_no = line_counters[jno]
        conn.execute("""
            INSERT INTO journals
              (journal_no,journal_date,line_no,account_code,account_name,debit,credit,
               entity_name,memo,location,tax_rate,tax_amount,match_method,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            jno, ln.get('journal_date',''), line_no,
            ln.get('account_code',''), ln.get('account_name',''),
            ln.get('debit',0) or 0, ln.get('credit',0) or 0,
            ln.get('entity_name',''), ln.get('memo',''),
            ln.get('location',''),
            ln.get('tax_rate',0) or 0, ln.get('tax_amount',0) or 0,
            ln.get('match_method',''), status,
        ))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'saved': len(lines)})


# ══════════════════════════════════════════════════════════════
# API — Journals History (سجل القيود)
# ══════════════════════════════════════════════════════════════
@app.route('/api/journals')
def api_journals():
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    query = """
        SELECT id, journal_no, journal_date, line_no, account_code, account_name,
               debit, credit, entity_name, memo, location, tax_amount,
               source_bank, status, created_at
        FROM journals
        WHERE 1=1
    """
    params = []
    if date_from:
        query += " AND journal_date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND journal_date <= ?"
        params.append(date_to)
    query += " ORDER BY journal_date DESC, journal_no DESC, line_no ASC"

    conn = get_conn()
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return jsonify(df.to_dict('records'))


@app.route('/api/journals/<journal_no>', methods=['DELETE'])
def api_journals_delete(journal_no):
    conn = get_conn()
    conn.execute("DELETE FROM journals WHERE journal_no=?", (journal_no,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/journals/bulk-delete', methods=['POST'])
def api_journals_bulk_delete():
    """حذف جماعي لعدة قيود دفعة واحدة عبر أرقام القيود (journal_no)"""
    data = request.json
    journal_nos = data.get('journal_nos', [])
    if not journal_nos:
        return jsonify({'error': 'لم يتم تحديد أي قيود'}), 400
    conn = get_conn()
    placeholders = ','.join('?' * len(journal_nos))
    conn.execute(f"DELETE FROM journals WHERE journal_no IN ({placeholders})", journal_nos)
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'deleted': len(journal_nos)})


@app.route('/api/journals/<journal_no>/status', methods=['PATCH'])
def api_journals_update_status(journal_no):
    """تحديث حالة قيد كامل (كل سطوره): معتمد / مسودة"""
    data = request.json
    status = data.get('status', 'معتمد')
    conn = get_conn()
    conn.execute("UPDATE journals SET status=? WHERE journal_no=?", (status, journal_no))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/journals/line/<int:line_id>', methods=['PATCH'])
def api_journals_update_line(line_id):
    """
    تعديل سطر قيد واحد بعد الحفظ (الحساب، المبلغ، الطرف، الوصف...).
    يُستخدم من شاشة "مراجعة القيود" عند تعديل أي قيمة في قيد محفوظ.
    """
    data = request.json
    allowed = ['journal_date', 'account_code', 'account_name', 'debit',
               'credit', 'entity_name', 'memo', 'location']
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return jsonify({'error': 'لا توجد بيانات للتحديث'}), 400

    set_clause = ', '.join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [line_id]

    conn = get_conn()
    conn.execute(f"UPDATE journals SET {set_clause} WHERE id=?", values)
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/journals/line/<int:line_id>', methods=['DELETE'])
def api_journals_delete_line(line_id):
    """حذف سطر واحد فقط من قيد (نادر الاستخدام، عادة يُحذف القيد كاملاً)"""
    conn = get_conn()
    conn.execute("DELETE FROM journals WHERE id=?", (line_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ══════════════════════════════════════════════════════════════
# API — Account options (لقوائم الـ select)
# ══════════════════════════════════════════════════════════════
@app.route('/api/account-options')
def api_account_options():
    conn = get_conn()
    df = pd.read_sql_query("SELECT acc_code, acc_name FROM accounts ORDER BY acc_code", conn)
    conn.close()
    options = (df['acc_code'] + ' - ' + df['acc_name']).tolist()
    return jsonify(options)


@app.route('/api/location-options')
def api_location_options():
    conn = get_conn()
    df = pd.read_sql_query("SELECT name FROM locations ORDER BY is_main DESC, name", conn)
    conn.close()
    return jsonify(df['name'].tolist())


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)

# ══════════════════════════════════════════════════════════════
# API — Import / Export for master data tables
# ══════════════════════════════════════════════════════════════
import csv
from io import StringIO

@app.route('/api/accounts/export')
def api_accounts_export():
    conn = get_conn()
    rows = rows_to_list(conn.execute("SELECT acc_code,acc_name,acc_type FROM accounts ORDER BY acc_code").fetchall())
    conn.close()
    buf = StringIO()
    w = csv.DictWriter(buf, fieldnames=['acc_code','acc_name','acc_type'])
    w.writeheader(); w.writerows(rows)
    return send_file(BytesIO(buf.getvalue().encode('utf-8-sig')), mimetype='text/csv',
                     as_attachment=True, download_name='accounts.csv')

@app.route('/api/accounts/import', methods=['POST'])
def api_accounts_import():
    f = request.files.get('file')
    if not f: return jsonify({'error':'لم يتم رفع ملف'}),400
    text = f.read().decode('utf-8-sig')
    reader = csv.DictReader(StringIO(text))
    conn = get_conn(); added=0; skipped=0
    for row in reader:
        code=str(row.get('acc_code','')).strip()
        name=str(row.get('acc_name','')).strip()
        atype=str(row.get('acc_type','أصول')).strip()
        if not code or not name: continue
        try:
            conn.execute("INSERT INTO accounts (acc_code,acc_name,acc_type) VALUES (?,?,?)",(code,name,atype))
            added+=1
        except: skipped+=1
    conn.commit(); conn.close()
    return jsonify({'success':True,'added':added,'skipped':skipped})

@app.route('/api/suppliers/export')
def api_suppliers_export():
    conn = get_conn()
    rows = rows_to_list(conn.execute("SELECT bank_name,sys_name,acc_link,sup_type FROM suppliers ORDER BY sys_name").fetchall())
    conn.close()
    buf = StringIO()
    w = csv.DictWriter(buf, fieldnames=['bank_name','sys_name','acc_link','sup_type'])
    w.writeheader(); w.writerows(rows)
    return send_file(BytesIO(buf.getvalue().encode('utf-8-sig')), mimetype='text/csv',
                     as_attachment=True, download_name='suppliers.csv')

@app.route('/api/suppliers/import', methods=['POST'])
def api_suppliers_import():
    f = request.files.get('file')
    if not f: return jsonify({'error':'لم يتم رفع ملف'}),400
    text = f.read().decode('utf-8-sig')
    reader = csv.DictReader(StringIO(text))
    conn = get_conn(); added=0
    for row in reader:
        bank=str(row.get('bank_name','')).strip()
        sys_=str(row.get('sys_name','')).strip()
        acc=str(row.get('acc_link','')).strip()
        sup_type=str(row.get('sup_type','Trade')).strip()
        if not bank or not sys_: continue
        conn.execute("INSERT INTO suppliers (bank_name,sys_name,acc_link,sup_type) VALUES (?,?,?,?)",(bank,sys_,acc,sup_type))
        added+=1
    conn.commit(); conn.close()
    return jsonify({'success':True,'added':added})

@app.route('/api/suppliers/<int:item_id>', methods=['PUT'])
def api_suppliers_update(item_id):
    data=request.json
    conn=get_conn()
    conn.execute("UPDATE suppliers SET bank_name=?,sys_name=?,acc_link=?,sup_type=? WHERE id=?",
                 (data.get('bank_name',''),data.get('sys_name',''),data.get('acc_link',''),data.get('sup_type','Trade'),item_id))
    conn.commit(); conn.close()
    return jsonify({'success':True})

@app.route('/api/customers/export')
def api_customers_export():
    conn = get_conn()
    rows = rows_to_list(conn.execute("SELECT bank_name,sys_name,acc_link,cust_type FROM customers ORDER BY sys_name").fetchall())
    conn.close()
    buf = StringIO()
    w = csv.DictWriter(buf, fieldnames=['bank_name','sys_name','acc_link','cust_type'])
    w.writeheader(); w.writerows(rows)
    return send_file(BytesIO(buf.getvalue().encode('utf-8-sig')), mimetype='text/csv',
                     as_attachment=True, download_name='customers.csv')

@app.route('/api/customers/import', methods=['POST'])
def api_customers_import():
    f = request.files.get('file')
    if not f: return jsonify({'error':'لم يتم رفع ملف'}),400
    text = f.read().decode('utf-8-sig')
    reader = csv.DictReader(StringIO(text))
    conn = get_conn(); added=0
    for row in reader:
        bank=str(row.get('bank_name','')).strip()
        sys_=str(row.get('sys_name','')).strip()
        acc=str(row.get('acc_link','')).strip()
        cust_type=str(row.get('cust_type','Trade')).strip()
        if not bank or not sys_: continue
        conn.execute("INSERT INTO customers (bank_name,sys_name,acc_link,cust_type) VALUES (?,?,?,?)",(bank,sys_,acc,cust_type))
        added+=1
    conn.commit(); conn.close()
    return jsonify({'success':True,'added':added})

@app.route('/api/customers/<int:item_id>', methods=['PUT'])
def api_customers_update(item_id):
    data=request.json
    conn=get_conn()
    conn.execute("UPDATE customers SET bank_name=?,sys_name=?,acc_link=?,cust_type=? WHERE id=?",
                 (data.get('bank_name',''),data.get('sys_name',''),data.get('acc_link',''),data.get('cust_type','Trade'),item_id))
    conn.commit(); conn.close()
    return jsonify({'success':True})

@app.route('/api/expenses/export')
def api_expenses_export():
    conn = get_conn()
    rows = rows_to_list(conn.execute("SELECT bank_description,expense_category,acc_link FROM expenses_map").fetchall())
    conn.close()
    buf = StringIO()
    w = csv.DictWriter(buf, fieldnames=['bank_description','expense_category','acc_link'])
    w.writeheader(); w.writerows(rows)
    return send_file(BytesIO(buf.getvalue().encode('utf-8-sig')), mimetype='text/csv',
                     as_attachment=True, download_name='expenses.csv')

@app.route('/api/expenses/import', methods=['POST'])
def api_expenses_import():
    f = request.files.get('file')
    if not f: return jsonify({'error':'لم يتم رفع ملف'}),400
    text = f.read().decode('utf-8-sig')
    reader = csv.DictReader(StringIO(text))
    conn = get_conn(); added=0
    for row in reader:
        bank=str(row.get('bank_description','')).strip()
        cat=str(row.get('expense_category','')).strip()
        acc=str(row.get('acc_link','')).strip()
        if not bank: continue
        conn.execute("INSERT INTO expenses_map (bank_description,expense_category,acc_link) VALUES (?,?,?)",(bank,cat or bank,acc))
        added+=1
    conn.commit(); conn.close()
    return jsonify({'success':True,'added':added})

@app.route('/api/expenses/<int:item_id>', methods=['PUT'])
def api_expenses_update(item_id):
    data=request.json
    conn=get_conn()
    conn.execute("UPDATE expenses_map SET bank_description=?,expense_category=?,acc_link=? WHERE id=?",
                 (data.get('bank_description',''),data.get('expense_category',''),data.get('acc_link',''),item_id))
    conn.commit(); conn.close()
    return jsonify({'success':True})

@app.route('/api/locations/export')
def api_locations_export():
    conn = get_conn()
    rows = rows_to_list(conn.execute("SELECT name,is_main,city FROM locations ORDER BY is_main DESC,name").fetchall())
    conn.close()
    buf = StringIO()
    w = csv.DictWriter(buf, fieldnames=['name','is_main','city'])
    w.writeheader(); w.writerows(rows)
    return send_file(BytesIO(buf.getvalue().encode('utf-8-sig')), mimetype='text/csv',
                     as_attachment=True, download_name='locations.csv')

@app.route('/api/locations/import', methods=['POST'])
def api_locations_import():
    f = request.files.get('file')
    if not f: return jsonify({'error':'لم يتم رفع ملف'}),400
    text = f.read().decode('utf-8-sig')
    reader = csv.DictReader(StringIO(text))
    conn = get_conn(); added=0
    for row in reader:
        name=str(row.get('name','')).strip()
        is_main=str(row.get('is_main','موقع فرعي')).strip()
        city=str(row.get('city','الرياض')).strip()
        if not name: continue
        conn.execute("INSERT INTO locations (name,is_main,city) VALUES (?,?,?)",(name,is_main,city))
        added+=1
    conn.commit(); conn.close()
    return jsonify({'success':True,'added':added})

@app.route('/api/locations/<int:item_id>', methods=['PUT'])
def api_locations_update(item_id):
    data=request.json
    conn=get_conn()
    conn.execute("UPDATE locations SET name=?,is_main=?,city=? WHERE id=?",
                 (data.get('name',''),data.get('is_main','موقع فرعي'),data.get('city','الرياض'),item_id))
    conn.commit(); conn.close()
    return jsonify({'success':True})

@app.route('/api/dashboard/cashflow')
def api_cashflow():
    """
    التدفق النقدي لشهر محدد، أو لكل البيانات لو month=all أو لو لا توجد بيانات
    في الشهر الحالي (يعرض كل البيانات تلقائياً في هذه الحالة).
    يُحسب فقط من سطور حسابات البنوك (Cash/Bank accounts)، لأن أي قيد متوازن
    بالتعريف، فحساب التدفق من إجمالي مدين/دائن الجدول كله يساوي بينهما دائماً
    وهذا غير صحيح لتحليل الكاش فلو. على حساب البنك: مدين = تدفق داخل (إيداع)،
    دائن = تدفق خارج (مسحوبات).
    """
    month = request.args.get('month', '').strip()
    show_all = (month == 'all')

    bank_codes = [acc.split(' - ')[0] for acc in get_bank_accounts().values()]
    placeholders = ','.join('?' * len(bank_codes))
    conn = get_conn()

    if not show_all and not month:
        month = datetime.date.today().strftime('%Y-%m')
        # لو الشهر الحالي مفيهوش بيانات، اعرض كل البيانات بدل شاشة فاضية
        check = conn.execute(
            "SELECT COUNT(*) c FROM journals WHERE strftime('%Y-%m', journal_date)=?", (month,)
        ).fetchone()
        if check['c'] == 0:
            show_all = True

    date_filter_sql = "" if show_all else "AND strftime('%Y-%m', journal_date) = ?"
    date_params = [] if show_all else [month]

    # تدفق يومي (أو شهري لو عرض كل البيانات)
    if show_all:
        daily = pd.read_sql_query(f"""
            SELECT journal_date as day,
                   SUM(CASE WHEN debit > 0 THEN debit ELSE 0 END) as inflow,
                   SUM(CASE WHEN credit > 0 THEN credit ELSE 0 END) as outflow
            FROM journals
            WHERE account_code IN ({placeholders})
            GROUP BY journal_date ORDER BY journal_date
        """, conn, params=bank_codes)
    else:
        daily = pd.read_sql_query(f"""
            SELECT journal_date as day,
                   SUM(CASE WHEN debit > 0 THEN debit ELSE 0 END) as inflow,
                   SUM(CASE WHEN credit > 0 THEN credit ELSE 0 END) as outflow
            FROM journals
            WHERE strftime('%Y-%m', journal_date) = ?
              AND account_code IN ({placeholders})
            GROUP BY journal_date ORDER BY journal_date
        """, conn, params=[month] + bank_codes)

    totals_row = conn.execute(f"""
        SELECT COALESCE(SUM(CASE WHEN debit>0 THEN debit ELSE 0 END),0) inflow,
               COALESCE(SUM(CASE WHEN credit>0 THEN credit ELSE 0 END),0) outflow
        FROM journals
        WHERE 1=1 {date_filter_sql}
          AND account_code IN ({placeholders})
    """, date_params + bank_codes).fetchone()

    # الموردين: السدادات (مدين على حسابات غير البنك)
    supplier_rows = conn.execute(f"""
        SELECT entity_name, SUM(debit) total, COUNT(DISTINCT journal_no) cnt,
               GROUP_CONCAT(DISTINCT memo) memos
        FROM journals
        WHERE 1=1 {date_filter_sql}
          AND debit > 0 AND account_code NOT IN ({placeholders})
          AND entity_name IS NOT NULL AND entity_name != ''
        GROUP BY entity_name ORDER BY total DESC
    """, date_params + bank_codes).fetchall()

    # العملاء: التحصيلات (دائن على حسابات غير البنك، أي ما تم تحصيله من عميل)
    customer_rows = conn.execute(f"""
        SELECT entity_name, SUM(credit) total, COUNT(DISTINCT journal_no) cnt,
               GROUP_CONCAT(DISTINCT memo) memos
        FROM journals
        WHERE 1=1 {date_filter_sql}
          AND credit > 0 AND account_code NOT IN ({placeholders})
          AND entity_name IS NOT NULL AND entity_name != ''
          AND memo LIKE 'Collection From%'
        GROUP BY entity_name ORDER BY total DESC
    """, date_params + bank_codes).fetchall()

    invoice_re = re.compile(r'(?:رقم|Bill|Invoice|فاتورة)\s*[:#]?\s*([A-Za-z0-9\-/]+)', re.IGNORECASE)

    categories = {'كهرباء': ['ELECTRIC', 'كهرباء', 'SEC'],
                  'اتصالات': ['STC', 'MOBILY', 'ZAIN', 'اتصالات'],
                  'حكومي': ['GOSI', 'GOVERNMENT', 'MOL', 'حكوم', 'DIWAN', 'IQAMA']}

    def _classify(entity_name, memos):
        text = f"{entity_name} {memos or ''}".upper()
        for cat, kws in categories.items():
            if any(kw.upper() in text for kw in kws):
                return cat
        return ''

    suppliers = []
    category_totals = {k: 0.0 for k in categories}
    for r in supplier_rows:
        memos = (r['memos'] or '')
        m = invoice_re.search(memos)
        invoice_no = m.group(1) if m else ''
        cat = _classify(r['entity_name'], memos)
        if cat:
            category_totals[cat] += float(r['total'])
        suppliers.append({
            'entity_name': r['entity_name'],
            'total': float(r['total']),
            'count': r['cnt'],
            'invoice_no': invoice_no,
            'category': cat,
        })

    customers = [{
        'entity_name': r['entity_name'],
        'total': float(r['total']),
        'count': r['cnt'],
    } for r in customer_rows]

    # كشف الأيام المكررة: نفس اليوم فيه أكثر من قيد بنفس (المبلغ + البيان) — مؤشر احتمال رفع كشف مرتين
    dup_rows = conn.execute(f"""
        SELECT journal_date, memo, debit, credit, COUNT(*) cnt,
               GROUP_CONCAT(DISTINCT journal_no) journal_nos
        FROM journals
        WHERE 1=1 {date_filter_sql}
          AND account_code IN ({placeholders})
        GROUP BY journal_date, memo, debit, credit
        HAVING COUNT(DISTINCT journal_no) > 1
        ORDER BY journal_date DESC
    """, date_params + bank_codes).fetchall()
    duplicates = [{
        'date': r['journal_date'],
        'memo': r['memo'],
        'amount': float(r['debit'] or r['credit'] or 0),
        'count': r['cnt'],
        'journal_nos': (r['journal_nos'] or '').split(','),
    } for r in dup_rows]

    conn.close()
    return jsonify({
        'month': 'all' if show_all else month,
        'is_all': show_all,
        'daily': daily.to_dict('records'),
        'total_inflow': float(totals_row['inflow']),
        'total_outflow': float(totals_row['outflow']),
        'suppliers': suppliers,
        'customers': customers,
        'category_totals': category_totals,
        'duplicates': duplicates,
    })

# ══════════════════════════════════════════════════════════════
# API — تصدير سجل القيود المحفوظة (CSV / Excel) من قاعدة البيانات
# ══════════════════════════════════════════════════════════════
@app.route('/api/journals/export/csv')
def api_journals_export_csv():
    date_from = request.args.get('date_from', '')
    date_to   = request.args.get('date_to', '')
    query = "SELECT journal_no,journal_date,account_code,debit,credit,memo,entity_name,location,tax_amount FROM journals WHERE 1=1"
    params = []
    if date_from: query += " AND journal_date >= ?"; params.append(date_from)
    if date_to:   query += " AND journal_date <= ?"; params.append(date_to)
    query += " ORDER BY journal_date ASC, journal_no ASC, line_no ASC"
    conn = get_conn()
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    if df.empty:
        return jsonify({'error': 'لا توجد قيود'}), 404
    csv_bytes = journals_to_qb_csv(df)
    return send_file(BytesIO(csv_bytes), mimetype='text/csv', as_attachment=True,
                     download_name=f"QB_Journal_{datetime.date.today()}.csv")


@app.route('/api/journals/export/excel')
def api_journals_export_excel():
    conn = get_conn()
    df = pd.read_sql_query(
        "SELECT * FROM journals ORDER BY journal_date ASC, journal_no ASC, line_no ASC", conn)
    conn.close()
    if df.empty:
        return jsonify({'error': 'لا توجد قيود'}), 404
    xl_bytes = journals_to_excel(df)
    return send_file(BytesIO(xl_bytes),
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True,
                     download_name=f"Journal_{datetime.date.today()}.xlsx")
