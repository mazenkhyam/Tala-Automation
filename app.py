"""
TALA — نظام القيود المحاسبية الذكي
Flask Backend — يخدم الواجهة (نفس تصميم tala_full_system.html)
ويوفر API لكل العمليات (CRUD + رفع الكشف + المطابقة + التصدير)
"""

import os
import sqlite3
import datetime
from io import BytesIO

from flask import Flask, render_template, request, jsonify, send_file

from statement_parser import parse_statement
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
        CREATE TABLE IF NOT EXISTS journals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            journal_no    TEXT NOT NULL,
            journal_date  TEXT NOT NULL,
            account_code  TEXT NOT NULL,
            account_name  TEXT NOT NULL,
            debit         REAL DEFAULT 0,
            credit        REAL DEFAULT 0,
            entity_name   TEXT,
            memo          TEXT,
            location      TEXT,
            tax_rate      REAL DEFAULT 0,
            tax_amount    REAL DEFAULT 0,
            match_method  TEXT,
            created_at    TEXT DEFAULT (datetime('now','localtime'))
        );

        -- ALTER guard: قديماً acc_type قد لا يكون موجوداً
    """)
    # تأكد من وجود acc_type (لو الجدول قديم بدون هذا العمود)
    cols = [r['name'] for r in conn.execute("PRAGMA table_info(accounts)")]
    if 'acc_type' not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN acc_type TEXT DEFAULT 'أصول'")
    conn.commit()


def rows_to_list(rows):
    return [dict(r) for r in rows]


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
    total_debit = pd.read_sql_query("SELECT COALESCE(SUM(debit),0) s FROM journals", conn)['s'][0]
    total_credit = pd.read_sql_query("SELECT COALESCE(SUM(credit),0) s FROM journals", conn)['s'][0]

    conn.close()
    return jsonify({
        'stats': {k: int(v) for k, v in stats.items()},
        'total_debit': float(total_debit),
        'total_credit': float(total_credit),
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
            'location': '',
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
    for seq, t in enumerate(approved, start=1):
        jno = f"JE-{datetime.date.today().strftime('%Y%m')}-{seq:04d}"
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
               entity_name,memo,location,tax_rate,tax_amount,match_method)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            jno, ln.get('journal_date',''), line_no,
            ln.get('account_code',''), ln.get('account_name',''),
            ln.get('debit',0) or 0, ln.get('credit',0) or 0,
            ln.get('entity_name',''), ln.get('memo',''),
            ln.get('location',''),
            ln.get('tax_rate',0) or 0, ln.get('tax_amount',0) or 0,
            ln.get('match_method',''),
        ))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'saved': len(lines)})


# ══════════════════════════════════════════════════════════════
# API — Journals History (سجل القيود)
# ══════════════════════════════════════════════════════════════
@app.route('/api/journals')
def api_journals():
    conn = get_conn()
    df = pd.read_sql_query("""
        SELECT journal_no, journal_date, account_code, account_name,
               debit, credit, entity_name, memo, location, tax_amount, created_at
        FROM journals ORDER BY journal_date DESC, journal_no DESC
    """, conn)
    conn.close()
    return jsonify(df.to_dict('records'))


@app.route('/api/journals/<journal_no>', methods=['DELETE'])
def api_journals_delete(journal_no):
    conn = get_conn()
    conn.execute("DELETE FROM journals WHERE journal_no=?", (journal_no,))
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
