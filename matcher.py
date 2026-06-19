"""
Matching Engine — TALA v2
"""

import re

_NOISE = re.compile(
    r'INTERNAL TRANSFER|SARIE|INCOMING|OUTGOING|VIA ALINMA|VIA ALAHLI|VIA ALRIYAD|'
    r'PAYMENT DEPT|PAYMENT DEPARTMENT|VALUE DATE|REFERENCE NUMBER|UTI REF|'
    r'ONLINE TRANSFER|BANK TRANSFER|TRANSFER TO|TRANSFER FROM|'
    r'REF NO|REF #|REF:|TRF|PMT|SAR|\d{6,}',
    re.IGNORECASE
)

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

BANK_FEE_ACCOUNT = '813020 - Bank charges'
VAT_PAYABLE_ACCOUNT = '215106 - VAT Payable'

# استخراج رسم التحويل البنكي وضريبته من نص الحركة
_FEE_PATTERN = re.compile(
    r'(?:CHARGE|FEES?|FEE)\s*SAR\s*([\d.]+).{0,40}?VAT\s*SAR\s*([\d.]+)',
    re.IGNORECASE | re.DOTALL
)

_STRIP_WORDS = {
    'PAYMENT','TRANSFER','COLLECTION','SETTLEMENT','MONTHLY','BILL',
    'PMT','TRF','SAR','REF','INCOMING','OUTGOING','VIA','ONLINE',
    'INTERNAL','SARIE','DEPT','DEPARTMENT','VALUE','DATE','REFERENCE',
    'NUMBER','UTI','FROM','TO','FUEL','CHARGE','FEES',
}


def extract_bank_fee(bank_desc: str) -> tuple:
    m = _FEE_PATTERN.search(str(bank_desc))
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            return 0.0, 0.0
    return 0.0, 0.0


def clean_text(text: str) -> str:
    t = str(text).upper()
    t = _NOISE.sub(' ', t)
    return ' '.join(t.split()).strip()


def clean_for_match(text: str) -> str:
    t = clean_text(text)
    words = [w for w in t.split() if w not in _STRIP_WORDS and len(w) >= 2]
    return ' '.join(words).strip()


def match_entity(bank_desc: str, master: list) -> dict:
    clean       = clean_text(bank_desc)
    clean_match = clean_for_match(bank_desc)

    for item in master:
        key = item['bank_key']
        key_clean = clean_for_match(key)
        if key == clean or key == clean_match or key_clean == clean_match:
            return {**item, 'method': 'exact', 'confidence': 100}

    best = None
    best_score = 0
    for item in master:
        key = item['bank_key']
        key_c = clean_for_match(key)
        if len(key) >= 3 and key in clean_match:
            score = 90 + min(len(key), 10)
            if score > best_score:
                best_score = score
                best = item
        elif len(key_c) >= 3 and key_c in clean_match:
            score = 85 + min(len(key_c), 10)
            if score > best_score:
                best_score = score
                best = item
        elif len(key) >= 3 and clean_match in key:
            score = 70
            if score > best_score:
                best_score = score
                best = item
        else:
            words_key  = [w for w in key_c.split() if len(w) >= 3]
            words_desc = [w for w in clean_match.split() if len(w) >= 3]
            if words_key:
                matched = sum(1 for w in words_key if any(w in dw or dw in w for dw in words_desc))
                if matched / len(words_key) >= 0.5:
                    score = matched / len(words_key) * 80
                    if score > best_score:
                        best_score = score
                        best = item

    if best and best_score >= 45:
        return {**best, 'method': 'fuzzy', 'confidence': min(int(best_score), 99)}

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

    return {'sys_name': '', 'acc_link': '', 'entity_type': 'unknown', 'method': 'none', 'confidence': 0}


def build_memo(entity_type: str, sys_name: str, bank_desc: str) -> str:
    if entity_type == 'supplier' and sys_name:
        return f"Payment To {sys_name}"
    if entity_type == 'customer' and sys_name:
        return f"Collection From {sys_name}"
    return bank_desc


def build_journal_lines(tx: dict, bank_account: str, match: dict) -> list:
    amount = float(tx.get('amount', 0))
    date = tx.get('date', '')
    entity = match.get('sys_name', '')
    entity_type = match.get('entity_type', 'unknown')
    memo = build_memo(entity_type, entity, tx.get('bank_desc', ''))
    location = tx.get('location', '')
    acc_link = match.get('acc_link', '111700 - Bank Clearance Account')
    acc_code = acc_link.split(' - ')[0] if ' - ' in acc_link else acc_link
    acc_name = acc_link.split(' - ', 1)[1] if ' - ' in acc_link else acc_link

    bank_code = bank_account.split(' - ')[0] if ' - ' in bank_account else bank_account
    bank_name_str = bank_account.split(' - ', 1)[1] if ' - ' in bank_account else bank_account

    fee_amount, fee_vat = extract_bank_fee(tx.get('bank_desc', ''))
    fee_code = BANK_FEE_ACCOUNT.split(' - ')[0]
    fee_name = BANK_FEE_ACCOUNT.split(' - ', 1)[1]
    vat_code = VAT_PAYABLE_ACCOUNT.split(' - ')[0]
    vat_name = VAT_PAYABLE_ACCOUNT.split(' - ', 1)[1]

    lines = []
    if amount > 0:
        lines.append(_line(date, memo, entity, location, bank_code, bank_name_str, amount, 0, acc_link, 0, 0))
        lines.append(_line(date, memo, entity, location, acc_code, acc_name, 0, amount, acc_link, 0, 0))
    else:
        bank_total = abs(amount)
        vendor_net = round(bank_total - fee_amount - fee_vat, 2)
        lines.append(_line(date, memo, entity, location, acc_code, acc_name, vendor_net, 0, acc_link, 0, 0))
        if fee_amount > 0 or fee_vat > 0:
            fee_total = round(fee_amount + fee_vat, 2)
            lines.append(_line(date, f"BC- {memo}", entity, location, fee_code, fee_name, fee_total, 0, '', 0, fee_vat))
        lines.append(_line(date, memo, entity, location, bank_code, bank_name_str, 0, bank_total, bank_account, 0, 0))
    return lines


def _line(date, memo, entity, location, code, name, debit, credit, acc_link, tax_rate, tax_amount):
    return {
        'journal_date': date, 'account_code': code, 'account_name': name,
        'debit': round(debit, 2), 'credit': round(credit, 2),
        'entity_name': entity, 'memo': memo, 'location': location,
        'tax_rate': tax_rate, 'tax_amount': tax_amount,
    }
