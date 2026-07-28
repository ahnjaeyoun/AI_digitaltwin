from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


DOCUMENT_FILENAME = (
    "06_DigitalTwin_고장유형별_논리적_물리적_판단근거_및_데이터_설명.docx"
)

FAULT_TYPES = [
    "pump_wear_efficiency",
    "suction_filter_clog",
    "pressure_line_clog",
    "cylinder_seal_leak",
    "external_leak",
    "valve_internal_leak",
    "valve_stiction",
    "relief_premature_open",
    "relief_stuck_closed",
    "oil_overheat",
]

FAULT_KOREAN = {
    "none": "정상",
    "pump_wear_efficiency": "펌프 마모·효율 저하",
    "suction_filter_clog": "흡입 필터 막힘",
    "pressure_line_clog": "압력 배관 막힘",
    "cylinder_seal_leak": "실린더 씰 내부 누설",
    "external_leak": "외부 누유",
    "valve_internal_leak": "밸브 내부 누설",
    "valve_stiction": "밸브 스틱션(고착)",
    "relief_premature_open": "릴리프 조기 개방",
    "relief_stuck_closed": "릴리프 닫힘 고착",
    "oil_overheat": "오일 과열",
}

FAULT_CONTRACTS = {
    "pump_wear_efficiency": {
        "cause": "펌프 마모 → 가능한 양정·유량 저하 → 진동·압력 리플·케이스 온도 증가",
        "legacy": "계산 유량, pump_head_m, 펌프 압력·동력",
        "new": "pump_vibration_mm_s, pump_case_temperature_c, pressure_ripple_bar",
    },
    "suction_filter_clog": {
        "cause": "흡입 필터 막힘 → 흡입 손실 증가 → 흡입압·NPSH 저하 → 캐비테이션성 진동",
        "legacy": "C_SUCTION_pressure_loss_bar, N_PUMP_IN_pressure_bar_g, NPSH_available_m",
        "new": "suction_pressure_bar_g, npsh_available_m, pump_vibration_mm_s, flow_variation_L_min",
    },
    "pressure_line_clog": {
        "cause": "압력 배관 막힘 → 차압·펌프 부하 증가 → 전달 유량·압력 저하와 발열",
        "legacy": "C_PRESSURE_pressure_loss_bar, 계산 유량, 펌프 헤드·동력",
        "new": "pressure_line_delta_bar, pump_case_temperature_c, return_oil_temperature_c",
    },
    "cylinder_seal_leak": {
        "cause": "cap→rod 내부 누설 → 유지압 저하·rod 압력 상승 → 실린더 드리프트",
        "legacy": "N_CYL_CAP_pressure_bar_g, N_CYL_ROD_pressure_bar_g, target_pressure_error_bar",
        "new": "cylinder_internal_leak_L_min, cylinder_drift_mm_s, cylinder_rod_pressure_bar_g",
    },
    "external_leak": {
        "cause": "계통 외부로 오일 유출 → 탱크 레벨·작업 압력 저하",
        "legacy": "load_pressure_bar_g, target_pressure_error_bar",
        "new": "external_leak_L_min, tank_oil_level_percent, measured_load_pressure_bar_g",
    },
    "valve_internal_leak": {
        "cause": "닫힌 유로 바이패스 → 리턴 유량 증가·가압유지 압력 저하",
        "legacy": "밸브 개도·유속·압력손실, 실린더 압력",
        "new": "valve_bypass_flow_L_min, return_flow_L_min, measured_load_pressure_bar_g",
    },
    "valve_stiction": {
        "cause": "스풀 고착 → 명령-실제 개도 차이·응답 지연·시간 변동 증가",
        "legacy": "밸브 명령 개도, 유속, 유량",
        "new": "valve_actual_opening_percent, valve_command_actual_error_percent, valve_response_delay_ms",
    },
    "relief_premature_open": {
        "cause": "설정압 이하 조기 개방 → 릴리프·리턴 유량 증가 → 최고 압력 제한·발열",
        "legacy": "relief_open, V_RELIEF_velocity_m_s, 릴리프 압력손실",
        "new": "relief_flow_L_min, return_flow_L_min, return_oil_temperature_c",
    },
    "relief_stuck_closed": {
        "cause": "릴리프 닫힘 고착 → 압력 방출 불가 → 압력 스파이크·리플·진동 증가",
        "legacy": "relief_open, 부하·펌프 출구 압력",
        "new": "relief_flow_L_min(0 유지), pressure_ripple_bar, pump_vibration_mm_s",
    },
    "oil_overheat": {
        "cause": "냉각 성능 저하 → 오일 온도 상승·점도 저하 → 누설·압력 오차 증가",
        "legacy": "온도 자체는 모델 출력에 없고, 점도 변화가 만든 유량·손실·헤드 변화만 간접 사용",
        "new": "measured_oil_temperature_c, return_oil_temperature_c, pump_case_temperature_c",
    },
}

STATUS_LABELS = ["기존 활성 데이터", "코드 구현 완료·재생성 전", "최종 재학습 후 산출"]

BLUE = "2E74B5"
DARK_BLUE = "1F4D78"
NAVY = "0B2545"
LIGHT_BLUE = "E8EEF5"
LIGHT_GRAY = "F2F4F7"
CALLOUT = "F4F6F9"
PALE_GOLD = "FFF4CC"
PALE_RED = "FCE8E6"
PALE_GREEN = "E6F4EA"
MUTED = "5F6B76"
WHITE = "FFFFFF"
BLACK = "111111"

CONTENT_WIDTH_DXA = 9360
TABLE_INDENT_DXA = 120
CELL_MARGIN_TOP_BOTTOM = 80
CELL_MARGIN_LEFT_RIGHT = 120


def _read_csv_header_and_count(path: Path) -> tuple[list[str], int]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        return header, sum(1 for _ in reader)


def _read_label_statistics(path: Path) -> dict:
    fault_counts: Counter[str] = Counter()
    stage_counts: Counter[str] = Counter()
    run_faults: dict[str, str] = {}
    rows = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for record in reader:
            rows += 1
            fault_counts[record["fault_type"]] += 1
            stage_counts[record["health_stage"]] += 1
            run_faults.setdefault(record["run_id"], record["fault_type"])
    fault_rows = rows - fault_counts.get("none", 0)
    return {
        "rows": rows,
        "fault_counts": dict(sorted(fault_counts.items())),
        "stage_counts": dict(sorted(stage_counts.items())),
        "runs": len(run_faults),
        "raw_fault_rate": fault_rows / rows if rows else math.nan,
    }


def _parse_sensor_names(source: str) -> list[str]:
    match = re.search(
        r"const\s+NOMINAL_SENSOR_VALUES\s*=\s*Object\.freeze\(\{(.*?)\}\);",
        source,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError("NOMINAL_SENSOR_VALUES block was not found")
    names = re.findall(r"^\s{4}([A-Za-z][A-Za-z0-9_]*)\s*:", match.group(1), re.MULTILINE)
    if len(names) != 21:
        raise ValueError(f"expected 21 sensor columns, found {len(names)}")
    return names


def _top_confusions(report: dict, limit: int = 8) -> list[dict]:
    confusion = report["fault_classifier"]["confusion_matrix"]
    labels = confusion["labels"]
    matrix = confusion["matrix"]
    pairs: list[dict] = []
    for row_index, actual in enumerate(labels):
        for column_index, predicted in enumerate(labels):
            if row_index == column_index:
                continue
            count = int(matrix[row_index][column_index])
            if count:
                pairs.append({"actual": actual, "predicted": predicted, "count": count})
    return sorted(pairs, key=lambda item: item["count"], reverse=True)[:limit]


def collect_document_facts(root: Path) -> dict:
    dataset = root / "dataset"
    artifacts = root / "model" / "artifacts"
    input_columns, input_rows = _read_csv_header_and_count(
        dataset / "base_input_timeseries.csv"
    )
    output_columns, output_rows = _read_csv_header_and_count(
        dataset / "base_output_timeseries.csv"
    )
    label_columns, label_rows = _read_csv_header_and_count(
        dataset / "labels_ground_truth.csv"
    )
    labels = _read_label_statistics(dataset / "labels_ground_truth.csv")
    generation_report = json.loads(
        (dataset / "data_generation_report.json").read_text(encoding="utf-8")
    )
    training_report = json.loads(
        (artifacts / "training_report.json").read_text(encoding="utf-8")
    )
    legacy_features = json.loads(
        (artifacts / "feature_columns.json").read_text(encoding="utf-8")
    )
    new_sensors = _parse_sensor_names(
        (root / "generator" / "fault_profiles.js").read_text(encoding="utf-8")
    )

    dataset_report = training_report["dataset"]
    classifier_report = training_report["fault_classifier"]
    health_report = training_report["health_regressor"]
    return {
        "root": str(root),
        "active_input_columns": input_columns,
        "active_output_columns": output_columns,
        "active_label_columns": label_columns,
        "active_input_rows": input_rows,
        "active_output_rows": output_rows,
        "active_label_rows": label_rows,
        "active_raw_fault_rate": labels["raw_fault_rate"],
        "active_fault_counts": labels["fault_counts"],
        "active_stage_counts": labels["stage_counts"],
        "active_runs_from_labels": labels["runs"],
        "legacy_model_features": legacy_features,
        "new_sensor_columns": new_sensors,
        "detailed_fault_types": list(FAULT_TYPES),
        "legacy_rows": int(dataset_report["rows"]),
        "legacy_runs": int(dataset_report["runs"]),
        "legacy_fault_rate": float(dataset_report["diagnosis_fault_rate"]),
        "legacy_diagnosis_counts": dataset_report["diagnosis_class_row_counts"],
        "legacy_raw_fault_counts": dataset_report["raw_fault_type_row_counts"],
        "legacy_accuracy": float(classifier_report["overall_accuracy"]),
        "legacy_macro_f1": float(classifier_report["macro_f1"]),
        "legacy_per_class": classifier_report["per_class"],
        "legacy_top_features": classifier_report["top_feature_importance"],
        "legacy_health_mae": float(health_report["mae"]),
        "legacy_health_r2": float(health_report["r2"]),
        "legacy_health_by_stage": health_report["by_health_stage"],
        "legacy_cross_validation": training_report["cross_validation"],
        "legacy_top_confusions": _top_confusions(training_report),
        "generation_report": generation_report,
        "status_labels": list(STATUS_LABELS),
    }


def _set_cell_margins(cell, top: int = 80, start: int = 120, bottom: int = 80, end: int = 120):
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for tag, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{tag}"))
        if node is None:
            node = OxmlElement(f"w:{tag}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def _set_cell_shading(cell, fill: str):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def _set_repeat_table_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    marker = OxmlElement("w:tblHeader")
    marker.set(qn("w:val"), "true")
    tr_pr.append(marker)


def _set_row_cant_split(row):
    tr_pr = row._tr.get_or_add_trPr()
    marker = OxmlElement("w:cantSplit")
    tr_pr.append(marker)


def _set_table_geometry(table, widths_dxa: Sequence[int], indent_dxa: int = TABLE_INDENT_DXA):
    if sum(widths_dxa) != CONTENT_WIDTH_DXA:
        raise ValueError(f"table widths must sum to {CONTENT_WIDTH_DXA}: {widths_dxa}")
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    tbl_pr = table._tbl.tblPr

    tbl_w = tbl_pr.first_child_found_in("w:tblW")
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(CONTENT_WIDTH_DXA))
    tbl_w.set(qn("w:type"), "dxa")

    tbl_ind = tbl_pr.first_child_found_in("w:tblInd")
    if tbl_ind is None:
        tbl_ind = OxmlElement("w:tblInd")
        tbl_pr.append(tbl_ind)
    tbl_ind.set(qn("w:w"), str(indent_dxa))
    tbl_ind.set(qn("w:type"), "dxa")

    layout = tbl_pr.first_child_found_in("w:tblLayout")
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")

    grid = table._tbl.tblGrid
    for child in list(grid):
        grid.remove(child)
    for width in widths_dxa:
        grid_column = OxmlElement("w:gridCol")
        grid_column.set(qn("w:w"), str(width))
        grid.append(grid_column)

    for row in table.rows:
        for index, cell in enumerate(row.cells):
            width = widths_dxa[index]
            cell.width = Inches(width / 1440)
            tc_pr = cell._tc.get_or_add_tcPr()
            tc_w = tc_pr.first_child_found_in("w:tcW")
            if tc_w is None:
                tc_w = OxmlElement("w:tcW")
                tc_pr.append(tc_w)
            tc_w.set(qn("w:w"), str(width))
            tc_w.set(qn("w:type"), "dxa")


def _set_run_font(
    run,
    *,
    size: float | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
    color: str | None = None,
    latin: str = "Calibri",
    east_asia: str = "Malgun Gothic",
):
    run.font.name = latin
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), latin)
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), latin)
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), east_asia)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if color is not None:
        run.font.color.rgb = RGBColor.from_string(color)


def _configure_styles(document: Document):
    section = document.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)
    section.different_first_page_header_footer = True

    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal._element.rPr.rFonts.set(qn("w:ascii"), "Calibri")
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), "Calibri")
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Malgun Gothic")
    normal.font.size = Pt(11)
    normal.font.color.rgb = RGBColor.from_string(BLACK)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.25

    for style_name, size, color, before, after in (
        ("Heading 1", 16, BLUE, 18, 10),
        ("Heading 2", 13, BLUE, 14, 7),
        ("Heading 3", 12, DARK_BLUE, 10, 5),
    ):
        style = document.styles[style_name]
        style.font.name = "Calibri"
        style._element.rPr.rFonts.set(qn("w:ascii"), "Calibri")
        style._element.rPr.rFonts.set(qn("w:hAnsi"), "Calibri")
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Malgun Gothic")
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True

    for style_name in ("List Bullet", "List Number"):
        style = document.styles[style_name]
        style.font.name = "Calibri"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Malgun Gothic")
        style.font.size = Pt(11)
        style.paragraph_format.left_indent = Inches(0.375)
        style.paragraph_format.first_line_indent = Inches(-0.188)
        style.paragraph_format.space_after = Pt(4)
        style.paragraph_format.line_spacing = 1.25

    if "Question" not in document.styles:
        question = document.styles.add_style("Question", WD_STYLE_TYPE.PARAGRAPH)
    else:
        question = document.styles["Question"]
    question.font.name = "Calibri"
    question._element.rPr.rFonts.set(qn("w:eastAsia"), "Malgun Gothic")
    question.font.size = Pt(11.5)
    question.font.bold = True
    question.font.color.rgb = RGBColor.from_string(DARK_BLUE)
    question.paragraph_format.space_before = Pt(8)
    question.paragraph_format.space_after = Pt(3)
    question.paragraph_format.keep_with_next = True


def _add_page_field(paragraph, instruction: str, fallback: str = "1"):
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f" {instruction} "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = fallback
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for element in (begin, instr, separate, text, end):
        run._r.append(element)
    _set_run_font(run, size=9, color=MUTED)


def _configure_header_footer(document: Document):
    section = document.sections[0]
    header = section.header
    paragraph = header.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_after = Pt(0)
    run = paragraph.add_run("DigitalTwin | 고장유형 판단근거 통합 설명서")
    _set_run_font(run, size=8.5, color=MUTED)

    footer = section.footer
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    paragraph.paragraph_format.space_before = Pt(0)
    run = paragraph.add_run("Page ")
    _set_run_font(run, size=9, color=MUTED)
    _add_page_field(paragraph, "PAGE")
    run = paragraph.add_run(" / ")
    _set_run_font(run, size=9, color=MUTED)
    _add_page_field(paragraph, "NUMPAGES")

    first_header = section.first_page_header
    first_header.paragraphs[0].text = ""
    first_footer = section.first_page_footer
    first_footer.paragraphs[0].text = ""


def _add_heading(document: Document, text: str, level: int = 1):
    return document.add_paragraph(text, style=f"Heading {level}")


def _add_body(document: Document, text: str, *, bold_lead: str | None = None):
    paragraph = document.add_paragraph()
    if bold_lead and text.startswith(bold_lead):
        lead = paragraph.add_run(bold_lead)
        _set_run_font(lead, bold=True)
        body = paragraph.add_run(text[len(bold_lead):])
        _set_run_font(body)
    else:
        run = paragraph.add_run(text)
        _set_run_font(run)
    return paragraph


def _add_bullet(document: Document, text: str):
    paragraph = document.add_paragraph(style="List Bullet")
    run = paragraph.add_run(text)
    _set_run_font(run)
    return paragraph


def _add_numbered(document: Document, number: int, text: str):
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.left_indent = Inches(0.375)
    paragraph.paragraph_format.first_line_indent = Inches(-0.188)
    paragraph.paragraph_format.space_after = Pt(4)
    paragraph.paragraph_format.line_spacing = 1.25
    run = paragraph.add_run(f"{number}. {text}")
    _set_run_font(run)
    return paragraph


def _format_table(
    table,
    widths_dxa: Sequence[int],
    *,
    header_fill: str = LIGHT_BLUE,
    body_font_size: float = 9.2,
    header_font_size: float = 9.2,
):
    table.style = "Table Grid"
    _set_table_geometry(table, widths_dxa)
    _set_repeat_table_header(table.rows[0])
    for row_index, row in enumerate(table.rows):
        _set_row_cant_split(row)
        for cell in row.cells:
            _set_cell_margins(
                cell,
                top=CELL_MARGIN_TOP_BOTTOM,
                start=CELL_MARGIN_LEFT_RIGHT,
                bottom=CELL_MARGIN_TOP_BOTTOM,
                end=CELL_MARGIN_LEFT_RIGHT,
            )
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            if row_index == 0:
                _set_cell_shading(cell, header_fill)
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.space_after = Pt(0)
                paragraph.paragraph_format.line_spacing = 1.12
                for run in paragraph.runs:
                    _set_run_font(
                        run,
                        size=header_font_size if row_index == 0 else body_font_size,
                        bold=True if row_index == 0 else None,
                        color=NAVY if row_index == 0 else BLACK,
                    )


def _add_table(
    document: Document,
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    widths_dxa: Sequence[int],
    *,
    body_font_size: float = 9.2,
    header_font_size: float = 9.2,
    header_fill: str = LIGHT_BLUE,
):
    row_values = [list(row) for row in rows]
    table = document.add_table(rows=1, cols=len(headers))
    for index, value in enumerate(headers):
        table.rows[0].cells[index].text = str(value)
    for values in row_values:
        cells = table.add_row().cells
        for index, value in enumerate(values):
            cells[index].text = str(value)
    _format_table(
        table,
        widths_dxa,
        body_font_size=body_font_size,
        header_font_size=header_font_size,
        header_fill=header_fill,
    )
    document.add_paragraph().paragraph_format.space_after = Pt(1)
    return table


def _add_callout(document: Document, title: str, text: str, *, fill: str = CALLOUT):
    table = document.add_table(rows=1, cols=1)
    cell = table.cell(0, 0)
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_after = Pt(3)
    lead = paragraph.add_run(title)
    _set_run_font(lead, size=11, bold=True, color=NAVY)
    body = cell.add_paragraph()
    body.paragraph_format.space_after = Pt(0)
    run = body.add_run(text)
    _set_run_font(run, size=10.5)
    _set_cell_shading(cell, fill)
    _set_cell_margins(cell, top=120, start=160, bottom=120, end=160)
    _set_table_geometry(table, [CONTENT_WIDTH_DXA])
    _set_row_cant_split(table.rows[0])
    document.add_paragraph().paragraph_format.space_after = Pt(1)
    return table


def _add_formula(document: Document, lines: Sequence[str]):
    table = document.add_table(rows=1, cols=1)
    cell = table.cell(0, 0)
    cell.text = ""
    for index, line in enumerate(lines):
        paragraph = cell.paragraphs[0] if index == 0 else cell.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(2 if index < len(lines) - 1 else 0)
        run = paragraph.add_run(line)
        _set_run_font(run, size=9.5, latin="Consolas", east_asia="Malgun Gothic")
    _set_cell_shading(cell, LIGHT_GRAY)
    _set_cell_margins(cell, top=120, start=160, bottom=120, end=160)
    _set_table_geometry(table, [CONTENT_WIDTH_DXA])
    _set_row_cant_split(table.rows[0])
    document.add_paragraph().paragraph_format.space_after = Pt(1)
    return table


def _add_question_answer(document: Document, question: str, answer: str):
    paragraph = document.add_paragraph(style="Question")
    run = paragraph.add_run(f"Q. {question}")
    _set_run_font(run, size=11.5, bold=True, color=DARK_BLUE)
    answer_paragraph = document.add_paragraph()
    answer_run = answer_paragraph.add_run(f"A. {answer}")
    _set_run_font(answer_run)
    return answer_paragraph


def _add_column_appendix(document: Document, title: str, columns: Sequence[str], note: str):
    _add_heading(document, title, 2)
    _add_body(document, note)
    rows = [(index, column) for index, column in enumerate(columns, start=1)]
    return _add_table(
        document,
        ["번호", "컬럼명"],
        rows,
        [700, 8660],
        body_font_size=8.5,
        header_font_size=9,
    )


def _add_cover(document: Document, facts: dict):
    spacer = document.add_paragraph()
    spacer.paragraph_format.space_after = Pt(42)

    kicker = document.add_paragraph()
    kicker.alignment = WD_ALIGN_PARAGRAPH.CENTER
    kicker.paragraph_format.space_after = Pt(12)
    run = kicker.add_run("DIGITAL TWIN TECHNICAL REFERENCE")
    _set_run_font(run, size=10, bold=True, color=BLUE)

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_after = Pt(10)
    title.paragraph_format.keep_with_next = True
    run = title.add_run("고장유형별 논리적·물리적\n판단근거 및 데이터 설명")
    _set_run_font(run, size=27, bold=True, color=NAVY)

    subtitle = document.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle.paragraph_format.space_after = Pt(28)
    run = subtitle.add_run("질의응답 통합본 · 기존 모델 감사 · 참조 데이터 컬럼 사전")
    _set_run_font(run, size=13, color=MUTED)

    metadata_rows = [
        ("문서 기준일", str(date(2026, 7, 16))),
        ("프로젝트", "DigitalTwin 유압 프레스 예지보전"),
        ("현재 활성 데이터", f"{facts['legacy_rows']:,}행 · {facts['legacy_runs']} runs"),
        ("문서 상태", "현재 상태 기준 통합 설명서(최종 재생성·재학습 전)"),
    ]
    _add_table(
        document,
        ["항목", "내용"],
        metadata_rows,
        [2200, 7160],
        body_font_size=10,
        header_font_size=10,
        header_fill=LIGHT_GRAY,
    )

    _add_callout(
        document,
        "가장 중요한 결론",
        (
            "기존 모델은 고장 유형을 스스로 발견한 것이 아니라, 생성기가 먼저 지정한 고장 라벨과 "
            "고장별 간접 솔버 패턴을 지도학습한 모델입니다. 기존 데이터에는 새 직접 센서 21개가 없었다는 "
            "사실을 전제로 읽어야 합니다. 새 센서는 코드에 구현됐지만 이 문서 작성 시점의 활성 10만 건 "
            "CSV와 모델에는 아직 반영되지 않았습니다."
        ),
        fill=PALE_GOLD,
    )

    status_rows = [
        (STATUS_LABELS[0], "103,911행 구형 CSV와 기존 XGBoost 모델", "수치 근거로 사용 가능"),
        (STATUS_LABELS[1], "21개 센서·행 라벨·무경고 fail-fast 코드", "활성 CSV 재생성 전"),
        (STATUS_LABELS[2], "새 SGKF OOF 기여도·대표 행 설명", "아직 산출되지 않음"),
    ]
    document.add_page_break()
    _add_table(
        document,
        ["구분", "대상", "현재 상태"],
        status_rows,
        [2500, 4300, 2560],
        body_font_size=9.5,
        header_font_size=9.5,
    )


def _add_scope_and_reading_guide(document: Document):
    _add_heading(document, "문서 범위와 읽는 방법", 1)
    _add_body(
        document,
        (
            "이 문서는 대화 중 나온 프로젝트 질문과 답변을 한곳에 모으고, 답변의 근거가 된 실제 파일과 "
            "컬럼명을 공개하는 기술 참고서입니다. 기존 모델의 결과와 새 설계의 목표를 섞지 않는 것이 "
            "가장 중요한 읽기 원칙입니다."
        ),
    )
    for item in [
        "‘기존’은 현재 dataset 및 model/artifacts에 저장된 103,911행 결과를 뜻합니다.",
        "‘새 센서’는 generator/fault_profiles.js에 구현됐지만 아직 활성 CSV를 재생성하지 않은 21개 열입니다.",
        "‘최종 설명’은 새 데이터로 SGKF 5겹 재학습 후 OOF 기여도를 계산해야 완성됩니다.",
        "모든 비율은 분모가 전체 행인지, 고장 행인지, run인지 확인해서 읽어야 합니다.",
        "health_index는 이름과 달리 0=정상, 1=고장에 가까운 열화도입니다.",
    ]:
        _add_bullet(document, item)

    _add_heading(document, "내용 지도", 2)
    for number, item in enumerate([
        "1~2장: 지금까지의 핵심 질의응답과 모델이 실제로 한 일",
        "3~5장: 참조 데이터, 품질 문제, 지표와 검증 방식",
        "6~7장: 고장 10종의 물리 근거와 XGBoost 논리 계산",
        "8장: 새 데이터·학습·설명 파이프라인과 남은 작업",
        "부록: 참조한 모든 CSV 및 모델 피처·새 센서 컬럼명",
    ], start=1):
        _add_numbered(document, number, item)


def _add_qa_sections(document: Document, facts: dict):
    _add_heading(document, "1. 지금까지의 핵심 질의응답", 1)

    qa_groups = [
        (
            "작업 방식과 프로젝트 방향",
            [
                (
                    "brainstorming을 매번 불러야 하나요?",
                    "아닙니다. 새 기능·동작 변경처럼 설계 판단이 필요한 창의적 작업 전에 사용하며, 단순 조회·설명·확인은 매번 호출할 필요가 없습니다.",
                ),
                (
                    "이 프로젝트가 만들려는 모델이 고장 예지보전 모델이 맞나요?",
                    "맞습니다. 현재 범위는 고장 유형 분류와 열화도(health_index) 예측입니다. 고장 발생 시점이나 남은 사이클을 뜻하는 RUL 모델은 제거했습니다.",
                ),
                (
                    "물리식 개선과 데이터 재생성은 같은 작업인가요?",
                    "같지 않습니다. 물리식 개선은 데이터를 만드는 규칙을 고치는 작업이고, 재생성은 고친 규칙을 실행해 CSV를 다시 만드는 작업입니다. 규칙을 바꾸지 않고 재생성하면 같은 문제를 반복합니다.",
                ),
                (
                    "Unity 3D 프레스는 현재 Digital Twin으로 연결됐나요?",
                    "공장·프레스·펌프·탱크·배관 모델과 프레스 애니메이션은 존재하지만, 센서 수신·AI 결과 수신·상태 매핑·경고 UI를 담당하는 실제 Digital Twin 제어 코드는 아직 없습니다.",
                ),
            ],
        ),
        (
            "데이터 생성과 실행",
            [
                (
                    "왜 우선 JavaScript로 10만 건을 실행하나요?",
                    "현재 C++ 실행기는 network.json 한 건을 처리하는 구조이고 10만 행 CSV 일괄 실행기가 아닙니다. 작업 속도 때문에 JS로 먼저 전체 데이터를 생성·검증하고, C++ 비교는 동일 입력의 표본을 추출해 수치 차이를 비교하는 방식이 현실적입니다.",
                ),
                (
                    "데이터는 어디에 생성하나요?",
                    "중간 generated 폴더가 아니라 프로젝트의 dataset 폴더에 base_input_timeseries.csv, base_output_timeseries.csv, labels_ground_truth.csv, data_generation_report.json을 직접 저장하는 구조입니다.",
                ),
                (
                    "고장률 40~50%는 어떻게 계산해야 하나요?",
                    "최종 계약은 labels_ground_truth.csv의 행 단위 fault_type이 none이 아닌 행 수를 전체 행 수로 나눈 원시 비율입니다. 기존 파일의 raw fault_type은 71.7999%였고, 학습기에서 정상 단계를 none으로 다시 해석한 진단 비율만 46.4532%였습니다.",
                ),
                (
                    "모든 고장 유형과 run을 어떻게 나누나요?",
                    "각 상세 고장은 최소 10개 독립 run을 갖게 하고, 같은 run_id가 학습·테스트에 섞이지 않도록 그룹 단위로 분리합니다. 최종 5개 fold의 학습·테스트 양쪽에 정상+고장 10종, 총 11개 클래스가 모두 있어야 합니다.",
                ),
            ],
        ),
        (
            "모델과 지표",
            [
                (
                    "train_model에는 어떤 모델이 있나요?",
                    "현재는 XGBoost 고장 분류기와 health_index 회귀기 두 개입니다. RUL 모델은 제거됐고 RUL 아티팩트도 없어야 합니다.",
                ),
                (
                    "MAE와 R²는 무엇인가요?",
                    "MAE는 예측 열화도와 실제 열화도의 절대 오차 평균으로 낮을수록 좋습니다. R²는 평균값만 예측하는 기준보다 변화를 얼마나 설명했는지 나타내며 1에 가까울수록 좋고, 음수이면 평균 기준보다 나쁩니다.",
                ),
                (
                    "기존 정확도가 낮은 이유는 무엇인가요?",
                    "직접 누설·진동·온도·밸브 응답 센서가 없고, 서로 비슷한 고장이 같은 압력·유량 간접 패턴을 공유했습니다. 일부 고장은 목표압력이나 명령 개도 변경을 정답 지름길로 사용해 일반화도 약했습니다.",
                ),
                (
                    "단일 분할보다 StratifiedGroupKFold가 왜 낫나요?",
                    "단일 분할은 한 번의 우연한 run 구성에 결과가 좌우됩니다. StratifiedGroupKFold 5겹은 run_id를 분리한 다섯 번의 OOF 평가를 합쳐 더 안정적으로 클래스별 성능을 측정합니다.",
                ),
                (
                    "전체 정확도 외에 무엇을 봐야 하나요?",
                    "macro F1, 고장별 recall·precision·F1·support, 11×11 혼동행렬, fold별 클래스 포함 여부, run 겹침 0건, health MAE·R²를 함께 봐야 합니다.",
                ),
            ],
        ),
        (
            "품질 플래그와 고장 근거",
            [
                (
                    "solver_warning·contains_inf_or_nan·exit_code를 열에서 없애면 끝인가요?",
                    "아닙니다. solver_warning 열만 제거한 것이 아니라 경고 행 자체가 남아 있었다는 점이 핵심 문제였습니다. 기존 200개 경고 행은 모델 CSV에 수치 결과로 남아 있었으므로 새 생성기는 경고 1건에도 실패해야 합니다.",
                ),
                (
                    "200개 solver warning은 어디서 나왔나요?",
                    "90개는 펌프 마모 심화 시 낮은 RPM에서 유량 근을 묶지 못한 no_residual_sign_change였고, 110개는 오일 과열 시 Reynolds 2300 경계의 불연속 마찰계수 때문에 생긴 bisection_iteration_limit였습니다.",
                ),
                (
                    "새 직접 센서가 없었는데 기존 모델은 어떻게 고장 유형을 뽑았나요?",
                    "생성기가 먼저 고장 라벨을 정하고 고장별 솔버 계수·일부 목표/명령값을 바꾼 뒤, XGBoost가 유량·헤드·압력·손실·NPSH·밸브 개도 패턴을 학습했습니다. 즉 직접 센서 진단이 아니라 간접 패턴과 일부 인위적 지름길의 지도학습이었습니다.",
                ),
                (
                    "새 센서 21개는 기존 모델이 실제 사용했던 데이터인가요?",
                    "아닙니다. 기존 데이터에는 새 직접 센서 21개가 없었다고 명확히 구분해야 합니다. 현재 생성 규칙과 출력 코드는 구현됐지만, 활성 CSV 재생성 및 모델 재학습 후에만 실제 사용 근거가 됩니다.",
                ),
                (
                    "고장 유형별 판단 근거를 확인할 수 있나요?",
                    "가능합니다. 최종 새 모델에서는 정상 대비 센서 통계와 XGBoost의 클래스별 기여도를 함께 저장합니다. OOF 테스트 행의 bias+피처 기여도 합으로 raw margin을 복원하고, 11개 margin을 softmax로 변환해 확률을 검산합니다.",
                ),
            ],
        ),
        (
            "문서와 현재 완료 상태",
            [
                (
                    "데이터는 이제 완전히 끝났나요?",
                    "이 문서 작성 시점에는 아닙니다. 고장 프로필·행 라벨·센서·fail-fast 코드까지는 수정됐지만 활성 10만 건 CSV 재생성, 전체 검증, 재학습, 새 OOF 설명 데이터 생성은 아직 남아 있습니다.",
                ),
                (
                    "문서 결과와 모델 결과는 어떻게 나누나요?",
                    "데이터 생성 규칙·재생성 결과는 별도 DOCX로, 모델 평가는 training_report.json으로, 고장 판단근거와 데이터 설명은 이 06번 DOCX 및 향후 설명 JSON/CSV로 구분합니다.",
                ),
            ],
        ),
    ]

    for group_title, questions in qa_groups:
        _add_heading(document, group_title, 2)
        for question, answer in questions:
            _add_question_answer(document, question, answer)


def _add_referenced_data(document: Document, facts: dict):
    document.add_page_break()
    _add_heading(document, "2. 참조한 데이터와 파일", 1)
    _add_body(
        document,
        (
            "아래 파일을 직접 읽어 답변과 수치를 확인했습니다. CSV는 행 데이터, JSON은 생성·학습 결과, "
            "JavaScript/Python 파일은 데이터 생성·학습 규칙의 근거입니다."
        ),
    )

    source_rows = [
        (
            "dataset/base_input_timeseries.csv",
            "JS 솔버에 입력되는 운전·경계·물리계수",
            STATUS_LABELS[0],
            f"{facts['active_input_rows']:,}행 / {len(facts['active_input_columns'])}열",
        ),
        (
            "dataset/base_output_timeseries.csv",
            "기존 모델이 읽은 솔버 계산 출력",
            STATUS_LABELS[0],
            f"{facts['active_output_rows']:,}행 / {len(facts['active_output_columns'])}열",
        ),
        (
            "dataset/labels_ground_truth.csv",
            "fault_type·health_index·health_stage 정답",
            STATUS_LABELS[0],
            f"{facts['active_label_rows']:,}행 / {len(facts['active_label_columns'])}열",
        ),
        (
            "dataset/data_generation_report.json",
            "구형 생성 품질 카운터(경고 200건 포함)",
            STATUS_LABELS[0],
            "행 단위 피처가 아닌 집계 메타데이터",
        ),
        (
            "model/artifacts/feature_columns.json",
            "기존 분류·회귀 모델이 실제 사용한 피처 순서",
            STATUS_LABELS[0],
            f"{len(facts['legacy_model_features'])}개 피처",
        ),
        (
            "model/artifacts/training_report.json",
            "SGKF·분류·health 평가 결과",
            STATUS_LABELS[0],
            "정확도·macro F1·고장별 recall·혼동행렬·MAE·R²",
        ),
        (
            "generator/fault_profiles.js",
            "새 고장 물리 프로필과 21개 측정 센서 정의",
            STATUS_LABELS[1],
            f"{len(facts['new_sensor_columns'])}개 센서",
        ),
        (
            "generator/pipeline_quality.js",
            "센서 allow-list·경계·키·해시·품질 검증",
            STATUS_LABELS[1],
            "활성 데이터 재생성 전",
        ),
        (
            "model/train_model.py",
            "XGBoost·SGKF·평가지표 계산 로직",
            "기존 활성 모델 코드 + 개선 진행 중",
            "RUL 없음",
        ),
        (
            "UnityClient/.../FactorySceneSample.unity",
            "유압 프레스·펌프·탱크·배관 3D 설비 배치",
            "시각 모델",
            "실시간 AI/센서 연동은 아직 없음",
        ),
    ]
    _add_table(
        document,
        ["파일", "역할", "상태", "규모/비고"],
        source_rows,
        [3200, 2500, 1800, 1860],
        body_font_size=8.3,
        header_font_size=8.8,
    )

    _add_heading(document, "참조 데이터의 연결 키", 2)
    _add_formula(
        document,
        [
            "model_row = base_output_timeseries.csv JOIN labels_ground_truth.csv",
            "join key  = run_id + timestamp",
            "group key = run_id  (학습/테스트 분리)",
        ],
    )
    _add_body(
        document,
        (
            "cycle_id는 같은 cycle의 여러 초 행에서 반복될 수 있으므로 단독 키가 아닙니다. run_id와 timestamp가 "
            "입력·출력·라벨에서 1:1로 일치해야 하며, 중복·누락·순서 불일치는 새 검증기에서 실패합니다."
        ),
    )

    _add_heading(document, "활성 라벨 분포", 2)
    distribution_rows = []
    for label, count in facts["active_fault_counts"].items():
        distribution_rows.append(
            (
                label,
                FAULT_KOREAN.get(label, label),
                f"{count:,}",
                f"{count / facts['active_label_rows'] * 100:.4f}%",
            )
        )
    _add_table(
        document,
        ["fault_type", "한글 설명", "행 수", "전체 비율"],
        distribution_rows,
        [2600, 3000, 1760, 2000],
        body_font_size=9,
        header_font_size=9,
    )
    _add_callout(
        document,
        "주의: 구형 raw 라벨과 진단 라벨은 다릅니다",
        (
            f"현재 labels_ground_truth.csv 원시 fault_type 고장률은 {facts['active_raw_fault_rate'] * 100:.4f}%입니다. "
            f"기존 train_model.py가 health_stage=normal을 none으로 다시 매핑한 진단 고장률은 "
            f"{facts['legacy_fault_rate'] * 100:.4f}%입니다. 새 데이터에서는 이 재해석을 없애고 row의 "
            "fault_type 자체를 40~50%로 생성해야 합니다."
        ),
        fill=PALE_GOLD,
    )


def _add_legacy_model_logic(document: Document, facts: dict):
    _add_heading(document, "3. 기존 모델은 어떻게 고장 유형을 분류했는가", 1)
    _add_callout(
        document,
        "핵심 정정",
        (
            "기존 모델은 고장 유형을 스스로 발견한 것이 아니다. 생성기가 run마다 고장 유형 정답을 먼저 "
            "지정하고, 고장별 입력 계수나 일부 목표·명령값을 바꾼 뒤, XGBoost가 솔버 결과 패턴에서 그 "
            "정답을 다시 맞히도록 학습한 지도학습 모델입니다."
        ),
        fill=PALE_RED,
    )

    flow_steps = [
        "고장 유형 정답을 run 단위로 먼저 지정",
        "고장 유형에 따라 펌프 양정·배관 손실·오일 온도 또는 일부 목표/명령값 변경",
        "JS 솔버가 압력·유량·유속·손실·NPSH·동력을 계산",
        f"계산 결과에서 {len(facts['legacy_model_features'])}개 피처를 선택",
        "XGBoost가 11개 클래스(none+고장 10종)의 패턴을 학습",
    ]
    for number, step in enumerate(flow_steps, start=1):
        _add_numbered(document, number, step)

    _add_heading(document, "고장별 기존 간접 근거와 새 직접 근거", 2)
    contract_rows = []
    for fault in FAULT_TYPES:
        contract = FAULT_CONTRACTS[fault]
        contract_rows.append(
            (
                f"{FAULT_KOREAN[fault]}\n{fault}",
                contract["cause"],
                contract["legacy"],
                contract["new"],
            )
        )
    _add_table(
        document,
        ["고장 유형", "물리적 원인 흐름", "기존 모델의 간접 근거", "새로 추가한 직접 센서"],
        contract_rows,
        [1800, 2850, 2350, 2360],
        body_font_size=7.9,
        header_font_size=8.2,
    )

    _add_heading(document, "기존 방식에서 문제가 된 정답 지름길", 2)
    for item in [
        "실린더 씰 누설·외부 누유·밸브 내부 누설에서 목표압력 또는 경계압력을 고장에 따라 낮춘 부분",
        "밸브 스틱션에서 센서의 실제 개도 대신 입력 명령 개도를 직접 줄인 부분",
        "릴리프 고장에서 설정압·목표압력을 고장 라벨에 맞춰 바꾼 부분",
        "모델 피처에 Press_target_pressure_bar_g와 밸브 개도 명령값이 포함된 부분",
    ]:
        _add_bullet(document, item)
    _add_body(
        document,
        (
            "이 값들은 실제 고장의 결과가 아니라 생성기가 라벨을 알고 미리 바꾼 값이므로, 모델이 물리적 "
            "고장 신호 대신 정답을 외울 수 있습니다. 새 프로필은 운전자 목표압력·펌프 RPM·밸브 명령·릴리프 "
            "설정값을 바꾸지 않고 실제 측정 센서와 제한된 물리계수만 바꾸도록 수정됐습니다."
        ),
    )


def _add_quality_and_split(document: Document, facts: dict):
    _add_heading(document, "4. 데이터 품질 문제와 수정 방향", 1)
    generation = facts["generation_report"]
    quality_rows = [
        ("solver 오류", generation.get("solver_error_rows", "-"), "기존 집계는 0"),
        ("NaN/무한대 행", generation.get("non_finite_rows", "-"), "기존 집계는 0"),
        ("solver warning 행", generation.get("solver_warning_rows", "-"), "90 펌프 마모 + 110 오일 과열"),
        ("금지 열", ", ".join(generation.get("excluded_from_model_data", [])), "열은 빠졌지만 경고 행 수치가 남음"),
    ]
    _add_table(
        document,
        ["검사", "기존 보고서", "해석"],
        quality_rows,
        [2300, 3000, 4060],
        body_font_size=9,
        header_font_size=9,
    )
    _add_callout(
        document,
        "왜 열 삭제만으로 충분하지 않았나",
        (
            "solver_warning 열만 제거한 것이 아니라 경고 행 자체가 남아 있었다. 모델은 경고 문자열을 피처로 "
            "읽지는 않았지만, 근을 찾지 못했거나 반복 한계에 도달한 수치 결과를 학습했습니다. 따라서 새 정책은 "
            "경고·비유한 값·예외가 1건이라도 있으면 최종 CSV와 통과 보고서를 게시하지 않는 fail-fast 방식입니다."
        ),
        fill=PALE_RED,
    )

    _add_heading(document, "Reynolds 경계의 수치식 수정", 2)
    _add_body(
        document,
        (
            "구형 JS와 C++ 솔버는 Reynolds 수 2300에서 층류식(64/Re)과 난류식(Haaland)을 즉시 전환해 "
            "마찰계수가 불연속이었습니다. JS는 2300~4000 전이구간을 선형 보간해 이분법 잔차가 연속이 되도록 "
            "수정됐습니다. C++에는 같은 수정이 아직 적용되지 않아 전이구간에서 1:1 수치 parity를 주장하면 안 됩니다."
        ),
    )

    _add_heading(document, "5. 학습·테스트 분리와 평가", 1)
    cv = facts["legacy_cross_validation"]
    cv_rows = [
        ("전략", cv["strategy"], "클래스 비율과 run 그룹을 함께 고려"),
        ("fold 수", cv["n_splits"], "5번 OOF 평가"),
        ("그룹 키", "run_id", "같은 run의 시간행이 학습·테스트에 섞이지 않음"),
        ("전 클래스 포함", cv["all_classes_present_in_train_and_test_every_fold"], "각 fold 양쪽에 11개 클래스"),
        ("그룹 중복 없음", cv["no_train_test_group_overlap_every_fold"], "모든 fold overlap=0"),
    ]
    _add_table(
        document,
        ["항목", "값", "의미"],
        cv_rows,
        [2200, 2700, 4460],
        body_font_size=9.2,
        header_font_size=9.2,
    )
    _add_body(
        document,
        (
            "StratifiedGroupKFold는 단일 분할보다 결과가 안정적이지만, 합성 규칙이 비현실적이면 5겹 검증도 "
            "현장 성능을 보장하지 못합니다. 그룹 누수 방지와 물리적으로 타당한 센서 생성이 함께 필요합니다."
        ),
    )


def _add_metrics(document: Document, facts: dict):
    _add_heading(document, "6. 기존 모델 결과와 해석", 1)
    summary_rows = [
        ("데이터 행", f"{facts['legacy_rows']:,}", "구형 활성 결과"),
        ("run 수", f"{facts['legacy_runs']}", "run_id 그룹"),
        ("피처 수", f"{len(facts['legacy_model_features'])}", "새 센서 21개 제외"),
        ("진단 고장률", f"{facts['legacy_fault_rate'] * 100:.4f}%", "학습기에서 normal stage를 none으로 매핑"),
        ("분류 정확도", f"{facts['legacy_accuracy'] * 100:.4f}%", "전체 행 기준"),
        ("Macro F1", f"{facts['legacy_macro_f1']:.6f}", "11개 클래스를 동일 가중"),
        ("Health MAE", f"{facts['legacy_health_mae']:.6f}", "낮을수록 좋음"),
        ("Health R²", f"{facts['legacy_health_r2']:.6f}", "1에 가까울수록 좋음"),
    ]
    _add_table(
        document,
        ["지표", "값", "해석"],
        summary_rows,
        [2200, 2200, 4960],
        body_font_size=9.5,
        header_font_size=9.5,
    )

    _add_heading(document, "고장별 분류 성능", 2)
    order = ["none"] + FAULT_TYPES
    performance_rows = []
    for label in order:
        metrics = facts["legacy_per_class"][label]
        performance_rows.append(
            (
                label,
                FAULT_KOREAN.get(label, label),
                f"{metrics['precision'] * 100:.2f}%",
                f"{metrics['recall'] * 100:.2f}%",
                f"{metrics['f1'] * 100:.2f}%",
                f"{int(metrics['support']):,}",
            )
        )
    _add_table(
        document,
        ["클래스", "설명", "Precision", "Recall", "F1", "Support"],
        performance_rows,
        [2500, 2380, 1120, 1120, 1040, 1200],
        body_font_size=8.2,
        header_font_size=8.2,
    )
    _add_callout(
        document,
        "낮은 recall이 보여주는 직접 센서 부족",
        (
            "릴리프 조기 개방 21.70%, 실린더 씰 누설 31.00%, 릴리프 닫힘 고착 40.19%, 외부 누유 "
            "60.57%로 낮았습니다. 압력·유량 간접 패턴만으로 유사 고장을 구분하기 어렵다는 증거입니다."
        ),
        fill=PALE_GOLD,
    )

    _add_heading(document, "주요 혼동 쌍", 2)
    confusion_rows = [
        (
            item["actual"],
            item["predicted"],
            f"{item['count']:,}",
        )
        for item in facts["legacy_top_confusions"]
    ]
    _add_table(
        document,
        ["실제 클래스", "예측 클래스", "행 수"],
        confusion_rows,
        [3600, 3600, 2160],
        body_font_size=9,
        header_font_size=9,
    )

    _add_heading(document, "Health MAE와 R² 계산", 2)
    _add_formula(
        document,
        [
            "MAE = mean(|health_true - health_pred|)",
            "R²  = 1 - sum((health_true - health_pred)²) / sum((health_true - mean(health_true))²)",
        ],
    )
    _add_body(
        document,
        (
            f"전체 MAE {facts['legacy_health_mae']:.6f}은 열화도 0~1 범위에서 평균 절대 오차가 약 "
            f"{facts['legacy_health_mae'] * 100:.2f}%p라는 뜻입니다. 전체 R²는 "
            f"{facts['legacy_health_r2']:.6f}이지만 단계별 R²가 음수인 구간이 있어 같은 단계 내부의 세밀한 "
            "열화값 예측은 불안정했습니다."
        ),
    )
    stage_rows = []
    for stage, values in facts["legacy_health_by_stage"].items():
        stage_rows.append(
            (stage, f"{int(values['rows']):,}", f"{values['mae']:.6f}", f"{values['r2']:.6f}")
        )
    _add_table(
        document,
        ["health_stage", "행 수", "MAE", "R²"],
        stage_rows,
        [3200, 1800, 2000, 2360],
        body_font_size=9,
        header_font_size=9,
    )


def _add_explainability(document: Document, facts: dict):
    _add_heading(document, "7. 새 모델의 논리적·물리적 판단근거 계산", 1)
    _add_heading(document, "물리적 근거", 2)
    _add_body(
        document,
        (
            "각 고장 행을 같은 cycle_phase의 정상 행과 먼저 비교합니다. 정상·고장의 평균, 표준편차, 중앙값, "
            "변화율과 표준화 차이를 저장해 운전 단계 차이를 고장 차이로 오해하지 않도록 합니다."
        ),
    )
    _add_formula(
        document,
        [
            "pooled_sd = sqrt((normal_sd² + fault_sd²) / 2)",
            "standardized_difference = (fault_mean - normal_mean) / pooled_sd",
            "양수 = 고장 시 증가, 음수 = 고장 시 감소",
        ],
    )

    _add_heading(document, "XGBoost의 논리적 근거", 2)
    _add_body(
        document,
        (
            "XGBoost는 여러 결정트리가 피처 임계값을 따라 분기한 결과를 클래스별 raw margin으로 합칩니다. "
            "네이티브 pred_contribs를 사용하면 각 피처가 해당 클래스 margin을 얼마나 올리거나 내렸는지 "
            "분해할 수 있습니다."
        ),
    )
    _add_formula(
        document,
        [
            "z_c = bias_c + Σ(feature_contribution_j,c)",
            "p_c = exp(z_c) / Σ exp(z_k)   (11개 클래스 softmax)",
            "predicted_class = argmax(p_c)",
        ],
    )
    _add_body(
        document,
        (
            "각 SGKF 테스트 fold의 행은 그 행을 학습하지 않은 fold 모델로 설명합니다. 기여도 합과 raw margin의 "
            "오차는 1e-4 이하, softmax 재계산 확률과 predict_proba 차이는 1e-6 이하인지 검산합니다."
        ),
    )
    _add_callout(
        document,
        "해석 한계",
        (
            "기여도는 인과관계 증명이 아니다. 이는 모델이 해당 예측에서 사용한 수학적·통계적 근거이며, "
            "실제 설비 고장의 원인을 증명하려면 현장 센서 교정·정비 이력·고장 검증이 추가로 필요합니다."
        ),
        fill=PALE_GOLD,
    )

    _add_heading(document, "향후 저장할 설명 데이터", 2)
    explanation_rows = [
        ("fault_explanation_report.json", "고장별 물리 통계·모델 기여도·검산 오차·대표/혼동 사례"),
        ("fault_type_evidence.csv", "고장×운전단계×센서 정상/고장 통계와 표준화 차이"),
        ("fault_prediction_examples.csv", "대표 행의 실제값·예측확률·대안 클래스·상위 긍정/부정 기여도"),
    ]
    _add_table(
        document,
        ["예정 파일", "내용"],
        explanation_rows,
        [3400, 5960],
        body_font_size=9.2,
        header_font_size=9.2,
    )
    _add_body(
        document,
        (
            "이 세 파일은 이 문서 작성 시점에는 아직 생성되지 않았습니다. 새 10만 건 데이터의 전체 검증과 "
            "SGKF 5겹 재학습이 끝난 뒤 실제 수치로 채워야 합니다."
        ),
    )


def _add_new_pipeline_and_status(document: Document, facts: dict):
    document.add_page_break()
    _add_heading(document, "8. 새 데이터 파이프라인과 남은 작업", 1)
    _add_heading(document, "이미 코드에 반영된 변경", 2)
    for item in [
        "고장 프로필이 목표압력·펌프 RPM·밸브 명령·릴리프 설정압을 바꾸지 않음",
        "정상 단계 행의 fault_type=none, run 전체 최종 유형은 run_fault_type 메타데이터로 분리",
        "고장 10종별 독립 run 최소 10개와 원시 행 고장률 40~50% 사전 검사",
        "21개 측정 센서와 공용 물리·센서 경계 정의",
        "Reynolds 2300~4000 마찰계수 연속 보간",
        "경고·예외·비유한 값 즉시 실패, 임시 파일 게시, 출력 SHA-256 검증",
        "입력·출력·라벨 키·행·열·물리범위의 strict verifier",
    ]:
        _add_bullet(document, item)

    _add_heading(document, "아직 실행해야 할 작업", 2)
    remaining = [
        "node generator/generate_basedata.js 100000 실행",
        "node generator/generate_solver_output.js 실행: warning/error/non-finite 0건 확인",
        "node generator/verify_basedata.js 및 전체 Node 테스트 통과",
        "과거 전용 시간 피처와 균형 가중치를 적용한 train_model.py 완성",
        "새 데이터로 StratifiedGroupKFold 5겹 재학습",
        "training_report.json 성능·모든 클래스·run overlap 0 검증",
        "OOF 기여도 JSON/CSV 생성과 이 문서의 실제 새 모델 수치 업데이트",
    ]
    for number, item in enumerate(remaining, start=1):
        _add_numbered(document, number, item)

    _add_heading(document, "JS와 C++ 비교 방법", 2)
    _add_body(
        document,
        (
            "현재 C++은 10만 행 CSV 실행기가 아니므로, JS에서 warning 0건으로 검증된 정상·초기·중증·고장 "
            "대표 입력을 고장별로 추출해 같은 network 입력으로 C++을 실행하고 핵심 결과의 절대오차·상대오차를 "
            "비교하는 방식이 적절합니다. Reynolds 전이식 차이는 먼저 동일하게 맞춰야 합니다."
        ),
    )
    _add_formula(
        document,
        [
            "absolute_error = |JS_value - C++_value|",
            "relative_error = |JS_value - C++_value| / max(|C++_value|, epsilon)",
            "비교 대상: flow, pump head, 주요 node pressure, line loss, NPSH, relief state",
        ],
    )

    _add_heading(document, "현재 상태 판정", 2)
    _add_callout(
        document,
        "데이터는 아직 ‘완전 완료’가 아닙니다",
        (
            "소스 수준 프로필·센서·품질 검증은 구현됐지만, 활성 dataset 파일은 구형입니다. 최종 완료 판정은 "
            "100,000~105,000행 재생성, 원시 고장률 40~50%, 고장별 run≥10, warning/error/non-finite 0, "
            "SGKF 모든 클래스·overlap 0, 모델 결과 JSON, 설명 데이터 생성까지 모두 확인한 뒤에만 가능합니다."
        ),
        fill=PALE_GOLD,
    )


def _add_column_appendices(document: Document, facts: dict):
    document.add_page_break()
    _add_heading(document, "부록 A. 참조 데이터 전체 컬럼명", 1)
    _add_body(
        document,
        (
            "요청하신 대로 답변에 참조한 활성 CSV 컬럼, 기존 모델 피처, 새 센서 컬럼을 모두 나열합니다. "
            "같은 이름이 여러 목록에 반복될 수 있으며, 이는 원본 CSV 열과 모델 선택 피처의 관계를 보여줍니다."
        ),
    )

    _add_column_appendix(
        document,
        f"A-1. base_input_timeseries.csv ({len(facts['active_input_columns'])}개)",
        facts["active_input_columns"],
        "현재 활성 구형 JS 솔버 입력 컬럼입니다. 새 센서 재생성 전 상태입니다.",
    )
    _add_column_appendix(
        document,
        f"A-2. base_output_timeseries.csv ({len(facts['active_output_columns'])}개)",
        facts["active_output_columns"],
        "현재 활성 구형 솔버 출력 컬럼입니다. 기존 모델의 원천 피처 후보입니다.",
    )
    _add_column_appendix(
        document,
        f"A-3. labels_ground_truth.csv ({len(facts['active_label_columns'])}개)",
        facts["active_label_columns"],
        "현재 활성 구형 라벨 컬럼입니다. 새 데이터에서는 run_fault_type이 추가되어 7개가 됩니다.",
    )
    _add_column_appendix(
        document,
        f"A-4. 기존 모델 실제 피처 ({len(facts['legacy_model_features'])}개)",
        facts["legacy_model_features"],
        "feature_columns.json에 저장된 기존 XGBoost 입력 순서입니다. 라벨과 품질 플래그는 포함되지 않습니다.",
    )
    _add_column_appendix(
        document,
        f"A-5. 새로 정의된 직접 측정 센서 ({len(facts['new_sensor_columns'])}개)",
        facts["new_sensor_columns"],
        (
            "generator/fault_profiles.js에 구현된 센서입니다. 코드 구현 완료·재생성 전 상태이며, 최종 CSV와 "
            "모델 피처에 실제 존재하는지 검증한 뒤에만 관측 근거로 사용할 수 있습니다."
        ),
    )


def _add_message_schema_comparison(document: Document):
    """message.txt와 고장 분류용 21개 센서를 빠르게 비교하는 부록을 추가한다."""
    document.add_page_break()
    _add_heading(document, "부록 C. message.txt 대비 21개 센서 비교", 1)
    _add_body(
        document,
        "의미 기준의 간단 비교입니다. gt_* 정답전용 열은 학습 입력 금지입니다.",
    )

    rows = [
        ("measured_oil_temperature_c", "temperature_c", "유사하게 있음"),
        ("measured_load_pressure_bar_g", "없음", "새로 필요"),
        ("tank_oil_level_percent", "없음", "새로 필요"),
        ("pump_vibration_mm_s", "없음", "새로 필요"),
        ("pump_case_temperature_c", "없음", "새로 필요"),
        ("pressure_ripple_bar", "압력 시계열", "계산 가능"),
        ("suction_pressure_bar_g", "pump_in_pressure_bar", "유사하게 있음"),
        ("npsh_available_m", "없음", "새로 필요"),
        ("flow_variation_L_min", "flow_rate_L_min 시계열", "계산 가능"),
        ("pressure_line_delta_bar", "pump_delta_pressure_bar", "정의 후 추가"),
        ("cylinder_rod_pressure_bar_g", "cyl_rod_pressure_bar", "있음"),
        ("cylinder_drift_mm_s", "없음", "새로 필요"),
        ("cylinder_internal_leak_L_min", "없음", "새로 필요"),
        ("external_leak_L_min", "없음", "새로 필요"),
        ("valve_actual_opening_percent", "valve_pos_pa/pb/at/bt", "있음(밸브별)"),
        ("valve_command_actual_error_percent", "valve_cmd_* - valve_pos_*", "계산 가능"),
        ("valve_response_delay_ms", "명령·실제 개도 시계열", "계산 가능"),
        ("valve_bypass_flow_L_min", "없음", "새로 필요"),
        ("return_flow_L_min", "return_flow_L_min", "있음"),
        ("relief_flow_L_min", "없음", "새로 필요"),
        ("return_oil_temperature_c", "없음", "새로 필요"),
    ]
    _add_table(
        document,
        ["신규 센서", "message.txt 대응", "판정"],
        rows,
        [3600, 3500, 2260],
        body_font_size=8.3,
        header_font_size=8.6,
    )


def _add_sources_and_limitations(document: Document, facts: dict):
    _add_heading(document, "부록 B. 근거 파일과 해석 한계", 1)
    source_rows = [
        ("기존 데이터 헤더·행 수", "dataset/*.csv", "이 문서 생성 시 직접 읽음"),
        ("기존 모델 피처", "model/artifacts/feature_columns.json", "58개 전체 나열"),
        ("기존 모델 지표", "model/artifacts/training_report.json", "SGKF OOF 결과"),
        ("기존 경고 카운터", "dataset/data_generation_report.json", "warning 200행"),
        ("새 센서 정의", "generator/fault_profiles.js", "21개; 재생성 전"),
        ("새 품질 정책", "generator/pipeline_quality.js", "경고·키·해시·경계 검증"),
        ("설명가능성 설계", "specs/2026-07-16-fault-explainability-design.md", "물리 통계+OOF 기여도"),
    ]
    _add_table(
        document,
        ["근거", "파일", "사용 방식"],
        source_rows,
        [2600, 3700, 3060],
        body_font_size=8.8,
        header_font_size=9,
    )

    _add_heading(document, "반드시 유지할 한계 설명", 2)
    for item in [
        "현재 성능은 합성 데이터 성능이며 실제 공장 설비 성능을 보장하지 않습니다.",
        "새 센서는 물리적으로 그럴듯한 합성 규칙이며 실제 센서 교정·노이즈·정비 이력이 아닙니다.",
        "XGBoost 기여도는 모델 판단의 분해이지 실제 원인의 인과 증명이 아닙니다.",
        "상관된 피처는 기여도가 여러 열에 분산되거나 한 열에 집중될 수 있습니다.",
        "기존 모델과 새 모델의 피처·라벨 의미가 달라 결과를 직접 비교할 때 같은 평가 기준을 사용해야 합니다.",
        "최종 새 모델 수치는 실제 재생성·재학습·OOF 설명 파일이 생성된 뒤 이 문서에 업데이트해야 합니다.",
    ]:
        _add_bullet(document, item)

    _add_callout(
        document,
        "문서 버전 메모",
        (
            "이 파일은 사용자가 요청한 지금까지의 질의응답과 현재 확인 가능한 데이터를 통합한 최초 배포본입니다. "
            "최종 10만 건 재생성 및 새 모델 설명이 끝나면 같은 파일명을 갱신해 실제 새 센서 통계와 OOF 기여도를 "
            "추가해야 합니다."
        ),
        fill=PALE_GREEN,
    )


def build_document(root: Path, output_path: Path) -> Path:
    facts = collect_document_facts(root)
    document = Document()
    _configure_styles(document)
    _configure_header_footer(document)
    document.core_properties.title = "DigitalTwin 고장유형별 논리적·물리적 판단근거 및 데이터 설명"
    document.core_properties.subject = "유압 프레스 예지보전 모델 질의응답·데이터·판단근거 통합본"
    document.core_properties.author = "DigitalTwin Project"
    document.core_properties.keywords = "DigitalTwin, XGBoost, 유압프레스, 고장분류, 설명가능성"
    document.core_properties.comments = "현재 상태 기준; 최종 재생성·재학습 후 갱신 필요"

    _add_cover(document, facts)
    _add_scope_and_reading_guide(document)
    _add_qa_sections(document, facts)
    _add_referenced_data(document, facts)
    _add_legacy_model_logic(document, facts)
    _add_quality_and_split(document, facts)
    _add_metrics(document, facts)
    _add_explainability(document, facts)
    _add_new_pipeline_and_status(document, facts)
    _add_column_appendices(document, facts)
    _add_sources_and_limitations(document, facts)
    _add_message_schema_comparison(document)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)
    return output_path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output_path = root / DOCUMENT_FILENAME
    facts = collect_document_facts(root)
    build_document(root, output_path)
    print(f"wrote: {output_path}")
    print(
        "facts: "
        f"rows={facts['legacy_rows']}, "
        f"active columns="
        f"{len(facts['active_input_columns'])}/"
        f"{len(facts['active_output_columns'])}/"
        f"{len(facts['active_label_columns'])}, "
        f"features={len(facts['legacy_model_features'])}, "
        f"new sensors={len(facts['new_sensor_columns'])}"
    )


if __name__ == "__main__":
    main()
