"""Shared ABTC official Excel (.xlsx) report builder."""
import io
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .pdf import get_signatories, printed_at_label

INK_HEX = '08291F'
SUBTITLE_HEX = '496458'
BODY_HEX = '26352F'
CREAM_HEX = 'FAF8F2'
CREAM_BORDER_HEX = 'DFCDAA'
TABLE_HEAD_HEX = 'F3F0E7'
GRID_HEX = 'C9D3CC'

_THIN_GRID = Side(style='thin', color=GRID_HEX)
_THIN_CREAM = Side(style='thin', color=CREAM_BORDER_HEX)
_SIG_LINE = Side(style='thin', color=INK_HEX)

_CELL_BORDER = Border(
    left=_THIN_GRID, right=_THIN_GRID, top=_THIN_GRID, bottom=_THIN_GRID
)
_SUMMARY_BORDER = Border(
    left=_THIN_CREAM, right=_THIN_CREAM, top=_THIN_CREAM, bottom=_THIN_CREAM
)
_TOP_SIG_BORDER = Border(top=_SIG_LINE)


def _safe_sheet_title(title):
    cleaned = re.sub(r'[\[\]\*\?/\\:]', ' ', str(title or 'Report')).strip()
    return (cleaned or 'Report')[:31]


def build_official_xlsx(
        title, meta, headers, rows, summary_lines=None, signatories=None,
        sheet_name=None):
    """Build a styled .xlsx workbook with ABTC letterhead, data table, and signatories."""
    wb = Workbook()
    ws = wb.active
    ws.title = _safe_sheet_title(sheet_name or title)
    ws.views.sheetView[0].showGridLines = True

    col_count = max(len(headers), 4)
    last_col_letter = get_column_letter(col_count)

    # Letterhead
    ws.merge_cells(f'A1:{last_col_letter}1')
    ws['A1'] = 'ANIMAL BITE TREATMENT CENTER'
    ws['A1'].font = Font(name='Calibri', size=14, bold=True, color=INK_HEX)
    ws['A1'].alignment = Alignment(horizontal='left', vertical='center')
    ws.row_dimensions[1].height = 22

    ws.merge_cells(f'A2:{last_col_letter}2')
    ws['A2'] = 'Municipal Health Office – Dumingag'
    ws['A2'].font = Font(name='Calibri', size=10, color=SUBTITLE_HEX)
    ws['A2'].alignment = Alignment(horizontal='left', vertical='center')

    ws.merge_cells(f'A3:{last_col_letter}3')
    ws['A3'] = str(title).upper()
    ws['A3'].font = Font(name='Calibri', size=12, bold=True, color=INK_HEX)
    ws['A3'].alignment = Alignment(horizontal='left', vertical='center')
    ws.row_dimensions[3].height = 20

    meta_text = f'{meta} · Date Generated: {printed_at_label()}' if meta else f'Date Generated: {printed_at_label()}'
    ws.merge_cells(f'A4:{last_col_letter}4')
    ws['A4'] = meta_text
    ws['A4'].font = Font(name='Calibri', size=9, italic=True, color=SUBTITLE_HEX)
    ws['A4'].alignment = Alignment(horizontal='left', vertical='center')

    row_idx = 6

    # Optional summary block
    if summary_lines:
        ws.merge_cells(f'A{row_idx}:{last_col_letter}{row_idx}')
        ws[f'A{row_idx}'] = 'REPORT SUMMARY'
        ws[f'A{row_idx}'].font = Font(name='Calibri', size=10, bold=True, color=INK_HEX)
        row_idx += 1

        cream_fill = PatternFill(start_color=CREAM_HEX, end_color=CREAM_HEX, fill_type='solid')
        for label, value in summary_lines:
            c_label = ws.cell(row=row_idx, column=1, value=str(label))
            c_label.font = Font(name='Calibri', size=9.5, bold=True, color=SUBTITLE_HEX)
            c_label.fill = cream_fill
            c_label.border = _SUMMARY_BORDER

            c_val = ws.cell(row=row_idx, column=2, value=str(value))
            c_val.font = Font(name='Calibri', size=9.5, bold=True, color=INK_HEX)
            c_val.fill = cream_fill
            c_val.border = _SUMMARY_BORDER
            if col_count > 2:
                ws.merge_cells(start_row=row_idx, start_column=2, end_row=row_idx, end_column=col_count)
                for col in range(3, col_count + 1):
                    cell = ws.cell(row=row_idx, column=col)
                    cell.fill = cream_fill
                    cell.border = _SUMMARY_BORDER
            row_idx += 1
        row_idx += 1

    # Table section heading
    ws.merge_cells(f'A{row_idx}:{last_col_letter}{row_idx}')
    ws[f'A{row_idx}'] = 'REPORT DETAILS'
    ws[f'A{row_idx}'].font = Font(name='Calibri', size=10, bold=True, color=INK_HEX)
    row_idx += 1

    # Table headers
    head_fill = PatternFill(start_color=TABLE_HEAD_HEX, end_color=TABLE_HEAD_HEX, fill_type='solid')
    header_row_idx = row_idx
    ws.row_dimensions[header_row_idx].height = 22
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=header_row_idx, column=col_idx, value=str(header))
        cell.font = Font(name='Calibri', size=10, bold=True, color=INK_HEX)
        cell.fill = head_fill
        cell.border = _CELL_BORDER
        cell.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)
    row_idx += 1

    # Table rows
    if rows:
        for data_row in rows:
            for col_idx, value in enumerate(data_row, start=1):
                val = '' if value is None else value
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.font = Font(name='Calibri', size=9.5, color=BODY_HEX)
                cell.border = _CELL_BORDER
                cell.alignment = Alignment(horizontal='left', vertical='top', wrap_text=True)
            row_idx += 1
    else:
        ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=len(headers))
        empty_cell = ws.cell(row=row_idx, column=1, value='No records match this report.')
        empty_cell.font = Font(name='Calibri', size=9.5, italic=True, color=SUBTITLE_HEX)
        empty_cell.alignment = Alignment(horizontal='center', vertical='center')
        for col_idx in range(1, len(headers) + 1):
            ws.cell(row=row_idx, column=col_idx).border = _CELL_BORDER
        row_idx += 1

    # Signatory block (Prepared by / Approved by)
    sig = signatories or get_signatories()
    row_idx += 2

    left_start = 1
    left_end = max(2, col_count // 2)
    right_start = left_end + 1
    right_end = max(right_start, col_count)

    def _merge_and_set(r, c1, c2, value, font, align='left', border=None):
        if c2 > c1:
            ws.merge_cells(start_row=r, start_column=c1, end_row=r, end_column=c2)
        cell = ws.cell(row=r, column=c1, value=value)
        cell.font = font
        cell.alignment = Alignment(horizontal=align, vertical='center')
        if border:
            for c in range(c1, c2 + 1):
                ws.cell(row=r, column=c).border = border

    label_font = Font(name='Calibri', size=9.5, bold=True, color=SUBTITLE_HEX)
    name_font = Font(name='Calibri', size=10.5, bold=True, color=INK_HEX)
    role_font = Font(name='Calibri', size=9, color=SUBTITLE_HEX)

    _merge_and_set(row_idx, left_start, left_end, sig['prepared_by_label'], label_font)
    _merge_and_set(row_idx, right_start, right_end, sig['approved_by_label'], label_font)

    # Space for handwritten signature
    row_idx += 2
    ws.row_dimensions[row_idx - 1].height = 22

    _merge_and_set(
        row_idx, left_start, left_end, sig['prepared_by_name'], name_font,
        border=_TOP_SIG_BORDER,
    )
    _merge_and_set(
        row_idx, right_start, right_end, sig['approved_by_name'], name_font,
        border=_TOP_SIG_BORDER,
    )
    row_idx += 1

    _merge_and_set(row_idx, left_start, left_end, sig['prepared_by_role'], role_font)
    _merge_and_set(row_idx, right_start, right_end, sig['approved_by_role'], role_font)

    # Auto-fit column widths based on table headers & rows
    for col_idx in range(1, col_count + 1):
        max_len = 12
        if col_idx <= len(headers):
            max_len = max(max_len, len(str(headers[col_idx - 1])) + 4)
        for data_row in rows[:500]:
            if col_idx <= len(data_row):
                cell_len = len(str(data_row[col_idx - 1] or ''))
                if cell_len > max_len:
                    max_len = cell_len
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(max_len + 2, 12), 42)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
