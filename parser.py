"""
Bank Statement Parser — TALA v2
يدعم:
  - بنك الإنماء  (Alinma)  — كشف Excel رسمي (header row 17, data from row 18)
  - بنك الأهلي   (AlAhli)
  - بنك الرياض   (Riyad)
  - Generic
"""

import re
import pandas as pd
from io import BytesIO

DATE_PATTERNS = [
    r'\d{4}-\d{2}-\d{2}',
    r'\d{2}/\d{2}/\d{4}',
    r'\d{2}-\d{2}-\d{4}',
    r'\d{2}\.\d{2}\.\d{4}',
]


def _normalise_date(val):
    s = str(val).strip()
    for pat in DATE_PATTERNS:
        m = re.search(pat, s)
        if m:
            raw = m.group()
            try:
                return pd.to_datetime(raw, dayfirst='/' in raw or (len(raw) > 2 and raw[2] in '-/')).strftime('%Y-%m-%d')
            except Exception:
                pass
    return s


def _to_float(val):
    if pd.isna(val) if hasattr(pd, 'isna') else val is None:
        return 0.0
    s = str(val).strip()
    if s in ('', '-', 'nan', 'None'):
        return 0.0
    cleaned = re.sub(r'[^\d.\-]', '', s)
    try:
        return float(cleaned)
    except Exception:
        return 0.0


def _detect_bank(df: pd.DataFrame) -> str:
    cols = ' '.join(df.columns.astype(str).str.upper())
    if any(k in cols for k in ['DEBIT AMOUNT', 'CREDIT AMOUNT', 'TRANSACTION DETAILS']):
        return 'AlAhli'
    if any(k in cols for k in ['WITHDRAWAL', 'DEPOSIT', 'NARRATION']):
        return 'Alinma'
    if any(k in cols for k in ['DR AMOUNT', 'CR AMOUNT', 'PARTICULARS']):
        return 'Riyad'
    return 'Generic'


# ══════════════════════════════════════════════════════════
# بنك الإنماء — كشف Excel الرسمي
# الهيكل: صفوف 1-16 = رأسية/إحصاءات، صف 17 = headers، صف 18+ = بيانات
# الأعمدة: 0=Balance, 2=Credit/Debit, 3=Description, 11=Date
# ══════════════════════════════════════════════════════════
def _parse_alinma_official(file_bytes: bytes) -> pd.DataFrame:
    """
    يقرأ كشف الإنماء الرسمي مباشرة بـ openpyxl.
    يدعم صيغتين:
      1) كشف طويل: صفوف رأسية/إحصاءات أولاً، ثم صف هيدر، ثم بيانات.
      2) كشف "Statement Report" مباشر: صف هيدر في أول صف، ثم بيانات.
    الأعمدة تُحدَّد بالاسم (Balance / Credit-Debit / Description / Date)
    بدلاً من أرقام أعمدة ثابتة، لأن ترتيب الأعمدة يختلف بين الصيغتين.
    """
    import openpyxl
    wb = openpyxl.load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))

    # ابحث عن صف الهيدر الفعلي: يحتوي على 'Credit/Debit' أو 'دائن/مدين'
    header_idx = None
    for i, row in enumerate(all_rows):
        row_str = ' '.join(str(v) for v in row if v)
        if 'Credit/Debit' in row_str or 'دائن/مدين' in row_str:
            header_idx = i
            break

    if header_idx is None:
        # fallback: جرب من صف 16 (الصيغة الطويلة القديمة)
        header_idx = 16

    header_row = all_rows[header_idx]

    # حدد فهرس كل عمود مطلوب بالاسم (case-insensitive، يدعم العربي/الإنجليزي)
    def _find_col(names):
        for j, cell in enumerate(header_row):
            if not cell:
                continue
            cell_s = str(cell)
            for n in names:
                if n in cell_s:
                    return j
        return None

    col_amount = _find_col(['Credit/Debit', 'دائن/مدين'])
    col_desc   = _find_col(['Transaction Description', 'تفاصيل العملية', 'Description', 'Narration'])
    col_date   = _find_col(['Transaction Date', 'تاريخ العملية', 'Date'])

    # fallback لو مش لقي بعض الأعمدة بالاسم: استخدم المواقع الافتراضية للصيغة الطويلة
    if col_amount is None: col_amount = 2
    if col_desc is None:   col_desc = 3
    if col_date is None:   col_date = 11

    data_rows = all_rows[header_idx + 1:]

    rows = []
    for row in data_rows:
        if not row or all(v is None for v in row):
            continue

        amount_raw = row[col_amount] if len(row) > col_amount else None
        desc_raw   = row[col_desc] if len(row) > col_desc else None
        date_raw   = row[col_date] if len(row) > col_date else None

        if amount_raw is None and desc_raw is None:
            continue

        amount = _to_float(amount_raw)
        desc   = str(desc_raw).strip() if desc_raw else ''
        date   = _normalise_date(date_raw) if date_raw else ''

        if amount == 0.0 and not desc:
            continue

        # amount سالب = سحب (debit)، موجب = إيداع (credit)
        debit  = abs(amount) if amount < 0 else 0.0
        credit = amount if amount > 0 else 0.0

        rows.append({
            'date':      date,
            'bank_desc': desc,
            'amount':    amount,
            'debit':     debit,
            'credit':    credit,
        })
    return pd.DataFrame(rows)


def _parse_alahli(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {}
    for c in df.columns:
        u = str(c).upper()
        if 'DATE' in u:                              col_map['date'] = c
        elif 'DETAIL' in u or 'DESC' in u or 'NARR' in u: col_map['desc'] = c
        elif 'DEBIT' in u:                           col_map['debit'] = c
        elif 'CREDIT' in u:                          col_map['credit'] = c
    rows = []
    for _, r in df.iterrows():
        debit  = _to_float(r.get(col_map.get('debit', ''), 0))
        credit = _to_float(r.get(col_map.get('credit', ''), 0))
        if debit == 0 and credit == 0:
            continue
        rows.append({
            'date':      _normalise_date(r.get(col_map.get('date', ''), '')),
            'bank_desc': str(r.get(col_map.get('desc', ''), '')).strip(),
            'amount':    credit - debit,
            'debit':     debit,
            'credit':    credit,
        })
    return pd.DataFrame(rows)


def _parse_alinma_csv(df: pd.DataFrame) -> pd.DataFrame:
    """fallback: كشف الإنماء بصيغة CSV عادية"""
    col_map = {}
    for c in df.columns:
        u = str(c).upper()
        if 'DATE' in u:                                       col_map['date'] = c
        elif 'NARR' in u or 'DESC' in u or 'DETAIL' in u:    col_map['desc'] = c
        elif 'WITHDRAW' in u or ('DR' in u and 'CREDIT' not in u): col_map['debit'] = c
        elif 'DEPOSIT' in u or 'CR' in u:                     col_map['credit'] = c
    rows = []
    for _, r in df.iterrows():
        debit  = _to_float(r.get(col_map.get('debit', ''), 0))
        credit = _to_float(r.get(col_map.get('credit', ''), 0))
        if debit == 0 and credit == 0:
            continue
        rows.append({
            'date':      _normalise_date(r.get(col_map.get('date', ''), '')),
            'bank_desc': str(r.get(col_map.get('desc', ''), '')).strip(),
            'amount':    credit - debit,
            'debit':     debit,
            'credit':    credit,
        })
    return pd.DataFrame(rows)


def _parse_riyad(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {}
    for c in df.columns:
        u = str(c).upper()
        if 'DATE' in u:                                  col_map['date'] = c
        elif 'PART' in u or 'DESC' in u or 'NARR' in u: col_map['desc'] = c
        elif u.startswith('DR'):                         col_map['debit'] = c
        elif u.startswith('CR'):                         col_map['credit'] = c
    rows = []
    for _, r in df.iterrows():
        debit  = _to_float(r.get(col_map.get('debit', ''), 0))
        credit = _to_float(r.get(col_map.get('credit', ''), 0))
        if debit == 0 and credit == 0:
            continue
        rows.append({
            'date':      _normalise_date(r.get(col_map.get('date', ''), '')),
            'bank_desc': str(r.get(col_map.get('desc', ''), '')).strip(),
            'amount':    credit - debit,
            'debit':     debit,
            'credit':    credit,
        })
    return pd.DataFrame(rows)


def _parse_generic(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    rows = []
    for _, r in df.iterrows():
        debit  = _to_float(r.get(mapping.get('debit', ''), 0))
        credit = _to_float(r.get(mapping.get('credit', ''), 0))
        if debit == 0 and credit == 0:
            continue
        rows.append({
            'date':      _normalise_date(r.get(mapping.get('date', ''), '')),
            'bank_desc': str(r.get(mapping.get('desc', ''), '')).strip(),
            'amount':    credit - debit,
            'debit':     debit,
            'credit':    credit,
        })
    return pd.DataFrame(rows)


def parse_statement(file_bytes: bytes, filename: str,
                    bank_hint: str = 'Auto',
                    manual_mapping: dict = None,
                    header_row: int = 0) -> tuple:
    """
    الدالة الرئيسية — تُرجع (df_clean, bank_name)
    df_clean أعمدة: date, bank_desc, amount, debit, credit
    """
    ext = filename.rsplit('.', 1)[-1].lower()

    # ── الإنماء: كشف Excel رسمي ذو هيكل خاص ──
    if bank_hint == 'Alinma' and ext in ('xlsx', 'xls'):
        try:
            df = _parse_alinma_official(file_bytes)
            if not df.empty:
                df = df[df['bank_desc'].str.strip() != ''].copy()
                df.reset_index(drop=True, inplace=True)
                return df, 'Alinma'
        except Exception as e:
            pass  # سقط على الطريقة العامة

    # ── قراءة عادية ──
    if ext in ('xlsx', 'xls'):
        raw = pd.read_excel(BytesIO(file_bytes), header=header_row, dtype=str)
    else:
        raw = pd.read_csv(BytesIO(file_bytes), header=header_row, dtype=str)

    raw.dropna(how='all', inplace=True)
    raw.columns = raw.columns.astype(str).str.strip()

    bank = bank_hint if bank_hint != 'Auto' else _detect_bank(raw)

    if bank == 'AlAhli':
        df = _parse_alahli(raw)
    elif bank == 'Alinma':
        df = _parse_alinma_csv(raw)
    elif bank == 'Riyad':
        df = _parse_riyad(raw)
    else:
        df = _parse_generic(raw, manual_mapping or {})

    df = df[df['bank_desc'].str.strip() != ''].copy()
    df.reset_index(drop=True, inplace=True)
    return df, bank
