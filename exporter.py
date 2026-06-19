"""
QuickBooks Journal Entry CSV Exporter — TALA v2
صيغة التصدير الجديدة (مطابقة لنموذج العميل):
  Journal Date, Journal no., Account (= acc_code), Debits, Credits,
  Description, Name, TAX, Location, Class
"""

import pandas as pd
from io import StringIO, BytesIO

QB_COLUMNS = ['Journal Date', 'Journal no.', 'Account', 'Debits', 'Credits',
              'Description', 'Name', 'TAX', 'Location', 'Class']


def journals_to_qb_csv(journals_df: pd.DataFrame) -> bytes:
    out_rows = []
    for _, r in journals_df.iterrows():
        tax_amt = float(r.get('tax_amount', 0) or 0)
        out_rows.append({
            'Journal Date': r.get('journal_date', ''),
            'Journal no.':  r.get('journal_no', ''),
            'Account':      r.get('account_code', ''),   # كود الحساب فقط
            'Debits':       r.get('debit', 0) or '',
            'Credits':      r.get('credit', 0) or '',
            'Description':  r.get('memo', ''),
            'Name':         r.get('entity_name', ''),
            'TAX':          tax_amt or '',
            'Location':     r.get('location', ''),
            'Class':        '',
        })
    df_out = pd.DataFrame(out_rows, columns=QB_COLUMNS)
    buf = StringIO()
    df_out.to_csv(buf, index=False, encoding='utf-8-sig')
    return buf.getvalue().encode('utf-8-sig')


def journals_to_excel(journals_df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
        journals_df.to_excel(writer, index=False, sheet_name='Journal Entries')
        wb = writer.book
        ws = writer.sheets['Journal Entries']
        hdr = wb.add_format({'bold':True,'bg_color':'#1e3a5f','font_color':'white','border':1})
        green = wb.add_format({'bg_color':'#e8f5e9','border':1})
        red   = wb.add_format({'bg_color':'#ffebee','border':1})
        num   = wb.add_format({'num_format':'#,##0.00','border':1})
        for col_num, col_name in enumerate(journals_df.columns):
            ws.write(0, col_num, col_name, hdr)
        for row_num, (_, row) in enumerate(journals_df.iterrows(), start=1):
            fmt = green if float(row.get('credit', 0) or 0) > 0 else red
            debit_col = list(journals_df.columns).index('debit') if 'debit' in journals_df.columns else -1
            credit_col = list(journals_df.columns).index('credit') if 'credit' in journals_df.columns else -1
            for col_num, val in enumerate(row):
                if col_num in (debit_col, credit_col):
                    ws.write(row_num, col_num, val or '', num)
                else:
                    ws.write(row_num, col_num, str(val) if val else '', fmt)
        ws.set_column(0, len(journals_df.columns), 22)
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
