"""
QuickBooks Journal Entry CSV Exporter
--------------------------------------
الصيغة المطلوبة لـ QuickBooks Online - Journal Entries Import:
  JournalNo, JournalDate, AccountName, Debits, Credits,
  Description, Name, Location, TaxAmount, TaxName
"""

import pandas as pd
from io import StringIO, BytesIO


QB_COLUMNS = [
    'JournalNo',
    'JournalDate',
    'AccountName',
    'Debits',
    'Credits',
    'Description',
    'Name',
    'Location',
    'TaxAmount',
    'TaxName',
]


def journals_to_qb_csv(journals_df: pd.DataFrame) -> bytes:
    """
    يحوّل DataFrame القيود إلى bytes CSV جاهز للرفع على QuickBooks.
    journals_df أعمدة مطلوبة:
        journal_no, journal_date, account_name, debit, credit,
        memo, entity_name, location, tax_amount
    """
    out_rows = []
    for _, r in journals_df.iterrows():
        tax_name = 'VAT 15%' if float(r.get('tax_amount', 0) or 0) > 0 else ''
        out_rows.append({
            'JournalNo':   r.get('journal_no', ''),
            'JournalDate': r.get('journal_date', ''),
            'AccountName': r.get('account_name', ''),
            'Debits':      r.get('debit', 0) or '',
            'Credits':     r.get('credit', 0) or '',
            'Description': r.get('memo', ''),
            'Name':        r.get('entity_name', ''),
            'Location':    r.get('location', ''),
            'TaxAmount':   r.get('tax_amount', '') or '',
            'TaxName':     tax_name,
        })

    df_out = pd.DataFrame(out_rows, columns=QB_COLUMNS)
    buf = StringIO()
    df_out.to_csv(buf, index=False, encoding='utf-8-sig')
    return buf.getvalue().encode('utf-8-sig')


def journals_to_excel(journals_df: pd.DataFrame) -> bytes:
    """نفس البيانات لكن بـ Excel مع تنسيق للمراجعة"""
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
        journals_df.to_excel(writer, index=False, sheet_name='Journal Entries')
        wb = writer.book
        ws = writer.sheets['Journal Entries']

        hdr = wb.add_format({'bold': True, 'bg_color': '#1e3a5f',
                             'font_color': 'white', 'border': 1})
        green = wb.add_format({'bg_color': '#e8f5e9', 'border': 1})
        red   = wb.add_format({'bg_color': '#ffebee', 'border': 1})
        num   = wb.add_format({'num_format': '#,##0.00', 'border': 1})

        for col_num, col_name in enumerate(journals_df.columns):
            ws.write(0, col_num, col_name, hdr)

        for row_num, (_, row) in enumerate(journals_df.iterrows(), start=1):
            fmt = green if float(row.get('credit', 0) or 0) > 0 else red
            for col_num, val in enumerate(row):
                if col_num in (
                    list(journals_df.columns).index('debit'),
                    list(journals_df.columns).index('credit'),
                ):
                    ws.write(row_num, col_num, val or '', num)
                else:
                    ws.write(row_num, col_num, str(val) if val else '', fmt)

        ws.set_column(0, len(journals_df.columns), 20)
    return buf.getvalue()


def summary_stats(journals_df: pd.DataFrame) -> dict:
    total_debit  = journals_df['debit'].fillna(0).sum()
    total_credit = journals_df['credit'].fillna(0).sum()
    balanced     = abs(total_debit - total_credit) < 0.01
    return {
        'total_debit':  round(total_debit, 2),
        'total_credit': round(total_credit, 2),
        'balanced':     balanced,
        'diff':         round(abs(total_debit - total_credit), 2),
        'lines':        len(journals_df),
        'entries':      journals_df['journal_no'].nunique() if 'journal_no' in journals_df.columns else 0,
    }
