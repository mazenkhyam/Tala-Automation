"""
Matching Engine
---------------
ثلاث مراحل لكل عملية بنكية:
  1. Exact match  → مطابقة كاملة للنص
  2. Fuzzy match  → partial / keyword
  3. Keyword rules→ كلمات مفتاحية ثابتة
"""

import re

# ── كلمات يجب حذفها من الوصف قبل المطابقة ──
_NOISE = re.compile(
    r'INTERNAL TRANSFER|SARIE|INCOMING|OUTGOING|VIA ALINMA|VIA ALAHLI|VIA ALRIYAD|'
    r'PAYMENT DEPT|PAYMENT DEPARTMENT|VALUE DATE|REFERENCE NUMBER|UTI REF|'
    r'ONLINE TRANSFER|BANK TRANSFER|TRANSFER TO|TRANSFER FROM|'
    r'REF NO|REF #|REF:|TRF|PMT|SAR|\d{6,}',
    re.IGNORECASE
)

# ── قواعد ثابتة keyword → حساب ──
_KEYWORD_RULES = [
    (['SALARY', 'SALARIES', 'PAYROLL', 'راتب', 'رواتب'],
     '215102 - Accrued - Salaries, Wages & Overtime', 'Payroll'),
    (['GOSI', 'SOCIAL INSURANCE', 'التأمينات'],
     '215108 - GOSI Payable', 'GOSI'),
    (['IQAMA', 'RESIDENCY', 'إقامة', 'MOL', 'MINISTRY OF HUMAN'],
     '118205 - Prepaid Expenses - IQAMA', 'Gov Fees'),
    (['GOVERNMENT', 'GOV FEE', 'رسوم حكومية', 'ديوان', 'DIWAN'],
     '711211 - Government Fees', 'Gov Fees'),
    (['BANK CHARGE', 'BANK FEE', 'SERVICE CHARGE', 'رسوم بنكية', 'عمولة'],
     '813020 - Bank charges', 'Bank Charges'),
    (['CHEQUE', 'CHECK', 'شيك'],
     '111700 - Bank Clearance Account', 'Cheque'),
    (['MARKETING', 'COMPENSATION', 'تسويق'],
     '792003 - Marketing (Compensation)', 'Marketing'),
    (['GIFT', 'BONUS', 'هدية', 'مكافأة'],
     '742005 - Gifts & Bouns', 'Gifts'),
    (['ALDREES', 'الدريس'],
     '212101 - Accounts Payable Non Trade', 'Aldrees Petroleum'),
    (['ADVANCE', 'سلفة', 'عهدة', 'EMPLOYEE ADVANCE'],
     '112410 - Employee Cash Advances', 'Advance'),
    (['ELECTRICITY', 'SEC ', 'كهرباء'],
     '212100 - Accounts Payable Trade', 'Electricity'),
]

BANK_ACCOUNTS = {
    'AlAhli':  '111100 - Cash & Cash Equivalents',
    'Alinma':  '51000 - Bank Alinma',
    'Riyad':   '4149940 - Riyad Bank',
}

TAX_RATE = 0.15


# كلمات زائدة في وصف البنك تُحذف قبل المطابقة (ليست جزء من اسم الجهة)
_STRIP_WORDS = {
    'PAYMENT','TRANSFER','COLLECTION','SETTLEMENT','MONTHLY','BILL',
    'PMT','TRF','SAR','REF','INCOMING','OUTGOING','VIA','ONLINE',
    'INTERNAL','SARIE','DEPT','DEPARTMENT','VALUE','DATE','REFERENCE',
    'NUMBER','UTI','FROM','TO','FUEL','CHARGE','FEES',
}

def clean_text(text: str) -> str:
    t = str(text).upper()
    t = _NOISE.sub(' ', t)
    return ' '.join(t.split()).strip()

def clean_for_match(text: str) -> str:
    """تنظيف أعمق لأغراض المطابقة: يحذف الكلمات الزائدة"""
    t = clean_text(text)
    words = [w for w in t.split() if w not in _STRIP_WORDS and len(w) >= 2]
    return ' '.join(words).strip()


def match_entity(bank_desc: str, master: list) -> dict:
    """
    يحاول مطابقة الوصف مع الماستر.
    يُرجع dict: {sys_name, acc_link, entity_type, method, confidence}
    """
    clean       = clean_text(bank_desc)       # للـ rules
    clean_match = clean_for_match(bank_desc)   # للمطابقة (بدون كلمات زائدة)

    # --- Phase 1: Exact (على الوصف المنظّف) ---
    for item in master:
        key = item['bank_key']
        key_clean = clean_for_match(key)
        if key == clean or key == clean_match or key_clean == clean_match:
            return {**item, 'method': 'exact', 'confidence': 100}

    # --- Phase 2: Fuzzy (contains) ---
    best = None
    best_score = 0
    for item in master:
        key = item['bank_key']
        key_c = clean_for_match(key)
        # اسم البنك داخل الوصف أو الوصف داخل اسم البنك (min 3 chars)
        if len(key) >= 3 and (key in clean_match or clean_match in key or
                               key_c in clean_match or clean_match in key_c):
            score = len(key) / max(len(clean_match), 1) * 100
            if score > best_score:
                best_score = score
                best = item
        else:
            # مطابقة كلمة-بكلمة
            words_key  = [w for w in key_c.split()        if len(w) >= 3]
            words_desc = [w for w in clean_match.split()   if len(w) >= 3]
            if words_key:
                matched = sum(1 for w in words_key if any(w in dw or dw in w for dw in words_desc))
                if matched / len(words_key) >= 0.5:
                    score = matched / len(words_key) * 80
                    if score > best_score:
                        best_score = score
                        best = item

    if best and best_score >= 45:
        confidence = min(int(best_score), 95)
        return {**best, 'method': 'fuzzy', 'confidence': confidence}

    # --- Phase 3: Keyword rules ---
    for keywords, acc_link, category in _KEYWORD_RULES:
        for kw in keywords:
            if kw.upper() in clean:
                return {
                    'sys_name': category,
                    'acc_link': acc_link,
                    'entity_type': 'expense',
                    'method': 'rule',
                    'confidence': 85,
                }

    # --- No match ---
    return {
        'sys_name': '',
        'acc_link': '',
        'entity_type': 'unknown',
        'method': 'none',
        'confidence': 0,
    }


def build_journal_lines(tx: dict, bank_account: str, match: dict) -> list:
    """
    يبني سطور القيد المحاسبي لكل عملية بنكية.

    المنطق الحقيقي (مطابق لنموذج الشركة):
    - الإيداعات: مدين البنك / دائن الطرف — بدون أي تعديل.
    - المسحوبات: المبلغ المقيّد على المصروف/المورد هو المبلغ الصافي (شامل
      الضريبة إن وجدت، لكن بدون فصلها في سطر VAT منفصل).
      الفرق بين المبلغ المسحوب من البنك والمبلغ الصافي = رسوم بنكية
      (Bank Charges)، تُستخرج من نص الوصف نفسه إن وُجدت (نمط Alinma:
      "with charge SAR{x} VAT SAR{y} ... amount {z}") وتُقيّد في سطر
      منفصل بحساب 813020 مع بريفكس "BC-" في الوصف. إن لم توجد رسوم،
      لا يُضاف هذا السطر إطلاقاً.

    tx keys: date, amount (+ = إيداع, - = سحب), bank_desc, memo, location
    """
    amount = float(tx.get('amount', 0))
    date = tx.get('date', '')
    memo = tx.get('memo') or match.get('sys_name', '') or tx.get('bank_desc', '')
    bank_desc = tx.get('bank_desc', '')
    location = tx.get('location', '')
    entity = match.get('sys_name', '')
    acc_link = match.get('acc_link', '111700 - Bank Clearance Account')
    acc_code = acc_link.split(' - ')[0] if ' - ' in acc_link else acc_link
    acc_name = acc_link.split(' - ', 1)[1] if ' - ' in acc_link else acc_link

    bank_code = bank_account.split(' - ')[0] if ' - ' in bank_account else bank_account
    bank_name_str = bank_account.split(' - ', 1)[1] if ' - ' in bank_account else bank_account

    lines = []
    if amount > 0:
        # إيداع: مدين البنك / دائن الطرف — بدون رسوم أو ضريبة
        lines.append(_line(date, memo, entity, location,
                           bank_code, bank_name_str, amount, 0, acc_link, 0, 0))
        lines.append(_line(date, memo, entity, location,
                           acc_code, acc_name, 0, amount, acc_link, 0, 0))
    else:
        withdrawn = abs(amount)
        bank_charge = extract_bank_charge(bank_desc, withdrawn)
        net_amount = round(withdrawn - bank_charge, 2)

        # المصروف/المورد بكامل المبلغ الصافي (شامل الضريبة، بدون فصلها)
        lines.append(_line(date, memo, entity, location,
                           acc_code, acc_name, net_amount, 0, acc_link, 0, 0))

        if bank_charge > 0:
            lines.append(_line(date, f"BC- {memo}", entity, location,
                               '813020', 'Bank charges', bank_charge, 0, '', 0, 0))

        lines.append(_line(date, memo, entity, location,
                           bank_code, bank_name_str, 0, withdrawn, bank_account, 0, 0))
    return lines


def extract_bank_charge(bank_desc: str, withdrawn_amount: float) -> float:
    """
    يستخرج قيمة الرسوم البنكية (رسوم + VAT على الرسوم) من نص وصف العملية.
    يدعم صيغتين تستخدمهما عمليات Alinma SARIE الصادرة بالتبادل:
      "... with charge SAR{x} VAT SAR{y} ..."
      "... with Fees SAR{x} VAT SAR{y} ..."
    يرجع 0.0 إن لم توجد رسوم صريحة في الوصف (لا فرق يُخمَّن حسابياً).
    """
    m_charge = re.search(r'(?:charge|fees?)\s+SAR\s*([\d,]+\.?\d*)', bank_desc, re.IGNORECASE)
    m_vat    = re.search(r'VAT\s+SAR\s*([\d,]+\.?\d*)', bank_desc, re.IGNORECASE)
    if not m_charge:
        return 0.0
    charge = _safe_float(m_charge.group(1))
    vat = _safe_float(m_vat.group(1)) if m_vat else 0.0
    return round(charge + vat, 2)


def _safe_float(s: str) -> float:
    try:
        return float(str(s).replace(',', ''))
    except Exception:
        return 0.0


def _line(date, memo, entity, location, code, name, debit, credit,
          acc_link, tax_rate, tax_amount):
    return {
        'journal_date': date,
        'account_code': code,
        'account_name': name,
        'debit':  round(debit, 2),
        'credit': round(credit, 2),
        'entity_name': entity,
        'memo': memo,
        'location': location,
        'tax_rate': tax_rate,
        'tax_amount': tax_amount,
    }
