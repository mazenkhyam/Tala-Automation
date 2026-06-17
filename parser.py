"""
Bank Statement Parser
---------------------
يدعم:
  - بنك الأهلي السعودي  (AlAhli)
  - بنك الإنماء          (Alinma)
  - بنك الرياض           (Riyad)
  - Generic               (أعمدة يحددها المستخدم)
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
    """يوحد صيغة التاريخ إلى YYYY-MM-DD"""
    s = str(val).strip()
    for pat in DATE_PATTERNS:
        m = re.search(pat, s)
        if m:
            raw = m.group()
            try:
                return pd.to_datetime(raw, dayfirst='/' in raw or raw[2] in '-/').strftime('%Y-%m-%d')
            except Exception:
                pass
    return s


def _to_float(val):
    if pd.isna(val) or str(val).strip() in ('', '-', 'nan'):
        return 0.0
    cleaned = re.sub(r'[^\d.\-]', '', str(val))
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


def _parse_alahli(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {}
    for c in df.columns:
        u = str(c).upper()
        if 'DATE' in u:                          col_map['date'] = c
        elif 'DETAIL' in u or 'DESC' in u or 'NARR' in u: col_map['desc'] = c
        elif 'DEBIT' in u:                       col_map['debit'] = c
        elif 'CREDIT' in u:                      col_map['credit'] = c

    rows = []
    for _, r in df.iterrows():
        debit  = _to_float(r.get(col_map.get('debit', ''), 0))
        credit = _to_float(r.get(col_map.get('credit', ''), 0))
        if debit == 0 and credit == 0:
            continue
        amount = credit - debit   # موجب = دخول، سالب = خروج
        rows.append({
            'date':      _normalise_date(r.get(col_map.get('date', ''), '')),
            'bank_desc': str(r.get(col_map.get('desc', ''), '')).strip(),
            'amount':    amount,
            'debit':     debit,
            'credit':    credit,
        })
    return pd.DataFrame(rows)


def _parse_alinma(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {}
    for c in df.columns:
        u = str(c).upper()
        if 'DATE' in u:                                        col_map['date'] = c
        elif 'NARR' in u or 'DESC' in u or 'DETAIL' in u:     col_map['desc'] = c
        elif 'WITHDRAW' in u or 'DR' in u:                     col_map['debit'] = c
        elif 'DEPOSIT' in u or 'CR' in u:                      col_map['credit'] = c

    rows = []
    for _, r in df.iterrows():
        debit  = _to_float(r.get(col_map.get('debit', ''), 0))
        credit = _to_float(r.get(col_map.get('credit', ''), 0))
        if debit == 0 and credit == 0:
            continue
        amount = credit - debit
        rows.append({
            'date':      _normalise_date(r.get(col_map.get('date', ''), '')),
            'bank_desc': str(r.get(col_map.get('desc', ''), '')).strip(),
            'amount':    amount,
            'debit':     debit,
            'credit':    credit,
        })
    return pd.DataFrame(rows)


def _parse_riyad(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {}
    for c in df.columns:
        u = str(c).upper()
        if 'DATE' in u:                                         col_map['date'] = c
        elif 'PART' in u or 'DESC' in u or 'NARR' in u:        col_map['desc'] = c
        elif 'DR' in u:                                          col_map['debit'] = c
        elif 'CR' in u:                                          col_map['credit'] = c

    rows = []
    for _, r in df.iterrows():
        debit  = _to_float(r.get(col_map.get('debit', ''), 0))
        credit = _to_float(r.get(col_map.get('credit', ''), 0))
        if debit == 0 and credit == 0:
            continue
        amount = credit - debit
        rows.append({
            'date':      _normalise_date(r.get(col_map.get('date', ''), '')),
            'bank_desc': str(r.get(col_map.get('desc', ''), '')).strip(),
            'amount':    amount,
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
                    header_row: int = 0) -> tuple[pd.DataFrame, str]:
    """
    الدالة الرئيسية — تُرجع (df_clean, bank_name)
    df_clean أعمدة: date, bank_desc, amount, debit, credit
    """
    ext = filename.rsplit('.', 1)[-1].lower()
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
        df = _parse_alinma(raw)
    elif bank == 'Riyad':
        df = _parse_riyad(raw)
    else:
        df = _parse_generic(raw, manual_mapping or {})

    df = df[df['bank_desc'].str.strip() != ''].copy()
    df.reset_index(drop=True, inplace=True)
    return df, bank
