"""
Bank Statement Parser
---------------------
يدعم:
  - بنك الأهلي السعودي  (AlAhli)
  - بنك الإنماء          (Alinma)  — بصيغتين: عمودين منفصلين (Withdrawal/Deposit) أو عمود موحّد (Credit/Debit برقم موجب/سالب)
  - بنك الرياض           (Riyad)
  - Generic               (أعمدة يحددها المستخدم)

يدعم أيضاً اكتشاف صف الترويسة تلقائياً في الكشوفات التي تحتوي على معلومات
حساب (اسم العميل، الأرصدة...) في الصفوف الأولى قبل جدول العمليات الفعلي.
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

# كلمات تشير لوجود ترويسة عمود حقيقية (تُستخدم لاكتشاف صف الـ header تلقائياً)
_HEADER_HINTS = [
    'DATE', 'TARIKH', 'TRANSACTION', 'DESCRIPTION', 'DETAIL', 'NARRATION',
    'DEBIT', 'CREDIT', 'WITHDRAWAL', 'DEPOSIT', 'BALANCE', 'PARTICULARS',
    'REFERENCE', 'تاريخ', 'وصف', 'تفاصيل', 'دائن', 'مدين', 'الرصيد', 'مرجعي',
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
    """يحوّل نص لرقم، يتعامل مع فواصل الآلاف (1,234.56) والقيم الفارغة/الشرطة"""
    if pd.isna(val) or str(val).strip() in ('', '-', 'nan', 'None'):
        return 0.0
    cleaned = re.sub(r'[^\d.\-]', '', str(val).replace(',', ''))
    try:
        return float(cleaned)
    except Exception:
        return 0.0


def _find_header_row(raw: pd.DataFrame, max_scan: int = 40) -> int:
    """
    يبحث في أول max_scan صف عن الصف الذي يحتوي أكبر عدد من كلمات الترويسة
    المعروفة (DATE, DEBIT, CREDIT, البنك العربية...). يُستخدم عند رفع كشف
    خام فيه معلومات حساب فوق جدول العمليات الفعلي (مثل كشوفات Alinma الرسمية).
    """
    best_row, best_score = 0, 0
    n = min(max_scan, len(raw))
    for i in range(n):
        row_vals = [str(v).upper() for v in raw.iloc[i].values if pd.notna(v)]
        if not row_vals:
            continue
        joined = ' '.join(row_vals)
        score = sum(1 for kw in _HEADER_HINTS if kw in joined)
        # صف ترويسة حقيقي غالباً فيه عدة خلايا نصية مختلفة (مش رقم واحد فقط)
        if score > best_score and len(row_vals) >= 2:
            best_score = score
            best_row = i
    return best_row if best_score >= 2 else 0


def _detect_bank(df: pd.DataFrame) -> str:
    cols = ' '.join(df.columns.astype(str).str.upper())
    if any(k in cols for k in ['DEBIT AMOUNT', 'CREDIT AMOUNT', 'TRANSACTION DETAILS']):
        return 'AlAhli'
    if any(k in cols for k in ['WITHDRAWAL', 'DEPOSIT', 'NARRATION', 'CREDIT/DEBIT', 'CREDIT\nDEBIT']) or 'دائن' in cols or 'مدين' in cols:
        return 'Alinma'
    if any(k in cols for k in ['DR AMOUNT', 'CR AMOUNT', 'PARTICULARS']):
        return 'Riyad'
    return 'Generic'


def _col_lookup(columns, *keywords):
    """يرجع أول اسم عمود يحتوي إحدى الكلمات المفتاحية (بحث غير حساس لحالة الأحرف)"""
    for c in columns:
        u = str(c).upper().replace('\n', ' ')
        for kw in keywords:
            if kw in u:
                return c
    return None


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
        amount = credit - debit
        rows.append({
            'date':      _normalise_date(r.get(col_map.get('date', ''), '')),
            'bank_desc': str(r.get(col_map.get('desc', ''), '')).strip(),
            'amount':    amount,
            'debit':     debit,
            'credit':    credit,
        })
    return pd.DataFrame(rows)


def _parse_alinma(df: pd.DataFrame) -> pd.DataFrame:
    """
    بنك الإنماء يصدر كشوفاته بصيغتين:
    1) عمودين منفصلين Withdrawal / Deposit
    2) عمود موحّد "دائن/مدين" (Credit/Debit) برقم موجب (دائن) أو سالب (مدين)
    """
    cols = list(df.columns)

    date_col   = _col_lookup(cols, 'TRANSACTION DATE', 'تاريخ العملية', 'DATE')
    desc_col   = _col_lookup(cols, 'TRANSACTION DESCRIPTION', 'تفاصيل العملية', 'NARRATION', 'DESC', 'DETAIL')
    debit_col  = _col_lookup(cols, 'WITHDRAW', 'DR ')
    credit_col = _col_lookup(cols, 'DEPOSIT', 'CR ')
    combined_col = _col_lookup(cols, 'CREDIT/DEBIT', 'CREDIT\\DEBIT', 'دائن/مدين', 'دائن\\مدين')

    rows = []
    for _, r in df.iterrows():
        if combined_col is not None and debit_col is None and credit_col is None:
            # عمود موحّد: قيمة موجبة = دائن (إيداع)، سالبة = مدين (سحب)
            raw_val = r.get(combined_col, 0)
            amount = _to_float(raw_val)
            debit  = abs(amount) if amount < 0 else 0.0
            credit = amount if amount > 0 else 0.0
        else:
            debit  = _to_float(r.get(debit_col, 0)) if debit_col else 0.0
            credit = _to_float(r.get(credit_col, 0)) if credit_col else 0.0
            amount = credit - debit

        if debit == 0 and credit == 0:
            continue

        rows.append({
            'date':      _normalise_date(r.get(date_col, '') if date_col else ''),
            'bank_desc': str(r.get(desc_col, '') if desc_col else '').strip(),
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
                    header_row: int = None) -> tuple[pd.DataFrame, str]:
    """
    الدالة الرئيسية — تُرجع (df_clean, bank_name)
    df_clean أعمدة: date, bank_desc, amount, debit, credit

    إذا header_row=None، يحاول النظام اكتشاف صف الترويسة تلقائياً (مفيد
    للكشوفات الرسمية التي تحتوي معلومات حساب فوق جدول العمليات).
    """
    ext = filename.rsplit('.', 1)[-1].lower()

    if header_row is None:
        # قراءة استكشافية بدون ترويسة لتحديد مكان صف العناوين الحقيقي
        if ext in ('xlsx', 'xls'):
            probe = pd.read_excel(BytesIO(file_bytes), header=None, dtype=str)
        else:
            probe = pd.read_csv(BytesIO(file_bytes), header=None, dtype=str)
        header_row = _find_header_row(probe)

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
