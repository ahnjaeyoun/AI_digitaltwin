"""
저장된 계층형 모델(models/hierarchical_lgbm.joblib)의 검증셋 성능을 시각화한 HTML 리포트 생성.
출력: dataset/model_performance_report.html (자체완결형, 별도 라이브러리 불필요)
"""
import sys
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support

sys.path.insert(0, str(Path(__file__).parent))
from build_dataset import add_engineered_features
from train_baseline import DATA_PATH, FEATURE_COLS, CATEGORICAL_FEATURES, TARGET_COL, group_stratified_split

MODEL_PATH = r"C:\pipes_press\models\hierarchical_lgbm.joblib"
OUT_PATH = r"C:\pipes_press\dataset\model_performance_report.html"
KNN_BASELINE_ACCURACY = 0.72  # 설계문서 13.4절, 사이클단위 kNN(7) 참조치

CLASS_ORDER = [
    "normal", "valve_stuck", "overheat", "relief_stuck", "suction_clog", "pump_wear",
    "line_clog", "ext_leak", "seal_leak", "valve_int_leak", "relief_early",
]

SEQ_BLUE = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
            "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]


def seq_color(t):
    """t in [0,1] -> hex from the sequential blue ramp."""
    t = max(0.0, min(1.0, t))
    idx = int(round(t * (len(SEQ_BLUE) - 1)))
    return SEQ_BLUE[idx]


def predict_cascade(bundle, df):
    feature_cols = bundle["feature_cols"]
    stage1, stage2 = bundle["stage1"], bundle["stage2"]
    coarse_label = bundle["coarse_label"]
    coarse_pred = stage1.predict(df[feature_cols]).astype(object)
    final_pred = coarse_pred.copy()
    leak_mask = coarse_pred == coarse_label
    if leak_mask.any():
        final_pred[leak_mask] = stage2.predict(df.loc[leak_mask, feature_cols])
    return final_pred


def compute_metrics(bundle, val_df):
    y_true = val_df[TARGET_COL].values
    y_pred = predict_cascade(bundle, val_df)

    cm = confusion_matrix(y_true, y_pred, labels=CLASS_ORDER)
    cm_df = pd.DataFrame(cm, index=CLASS_ORDER, columns=CLASS_ORDER)
    cm_norm = cm_df.div(cm_df.sum(axis=1), axis=0)  # 행(실제) 기준 정규화 = recall 분해

    p, r, f, support = precision_recall_fscore_support(y_true, y_pred, labels=CLASS_ORDER, zero_division=0)
    per_class = pd.DataFrame(
        {"precision": p, "recall": r, "f1": f, "support": support}, index=CLASS_ORDER
    )

    accuracy = float((y_pred == y_true).mean())
    macro_f1 = float(per_class["f1"].mean())

    val_df = val_df.copy()
    val_df["_correct"] = (y_pred == y_true).astype(int)
    by_second = val_df.groupby("cycle_second")["_correct"].mean()

    return {
        "cm": cm_df, "cm_norm": cm_norm, "per_class": per_class,
        "accuracy": accuracy, "macro_f1": macro_f1, "by_second": by_second,
        "n_rows": len(val_df), "n_cycles": val_df["cycle_id"].nunique(),
    }


# ---------- SVG 렌더링 ----------

def render_heatmap(cm_df, cm_norm):
    n = len(CLASS_ORDER)
    cell = 42
    label_w = 130
    label_h = 90
    pad = 16
    w = label_w + n * cell + pad * 2
    h = label_h + n * cell + pad * 2

    svg = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="혼동행렬 히트맵">']

    for j, pred_c in enumerate(CLASS_ORDER):
        x = label_w + j * cell + cell / 2
        svg.append(
            f'<text x="{x}" y="{label_h - 10}" transform="rotate(-40 {x} {label_h - 10})" '
            f'text-anchor="start" class="axis-label">{pred_c}</text>'
        )
    svg.append(f'<text x="{label_w + n * cell / 2}" y="16" text-anchor="middle" class="axis-title">예측(predicted)</text>')

    for i, true_c in enumerate(CLASS_ORDER):
        y = label_h + i * cell + cell / 2
        svg.append(f'<text x="{label_w - 8}" y="{y + 4}" text-anchor="end" class="axis-label">{true_c}</text>')
    svg.append(
        f'<text x="14" y="{label_h + n * cell / 2}" text-anchor="middle" class="axis-title" '
        f'transform="rotate(-90 14 {label_h + n * cell / 2})">실제(actual)</text>'
    )

    for i, true_c in enumerate(CLASS_ORDER):
        for j, pred_c in enumerate(CLASS_ORDER):
            frac = cm_norm.loc[true_c, pred_c]
            count = int(cm_df.loc[true_c, pred_c])
            color = seq_color(frac) if count > 0 else "var(--surface-1)"
            x = label_w + j * cell
            y = label_h + i * cell
            is_diag = i == j
            stroke = 'stroke="var(--text-primary)" stroke-width="1.5"' if is_diag else 'stroke="var(--surface-1)" stroke-width="2"'
            text_color = "var(--text-primary)" if frac < 0.55 else "#ffffff"
            label = f"{frac * 100:.0f}%" if count > 0 else ""
            svg.append(
                f'<rect x="{x}" y="{y}" width="{cell - 2}" height="{cell - 2}" rx="3" fill="{color}" {stroke}>'
                f'<title>실제 {true_c} → 예측 {pred_c}: {count:,}행 ({frac * 100:.1f}%)</title></rect>'
            )
            if count > 0 and cell >= 30:
                svg.append(
                    f'<text x="{x + cell / 2 - 1}" y="{y + cell / 2 + 4}" text-anchor="middle" '
                    f'font-size="10" fill="{text_color}" pointer-events="none">{label}</text>'
                )
    svg.append("</svg>")
    return "\n".join(svg)


def render_bar_chart(per_class):
    order = per_class.sort_values("recall").index.tolist()
    n = len(order)
    bar_group_w = 64
    bar_w = 14
    gap = 3
    chart_h = 220
    left_pad = 40
    top_pad = 24
    bottom_pad = 90
    w = left_pad + n * bar_group_w + 20
    h = top_pad + chart_h + bottom_pad

    metrics = [("precision", "var(--series-1)", "정밀도"), ("recall", "var(--series-2)", "재현율"), ("f1", "var(--series-3)", "F1")]

    svg = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="클래스별 precision/recall/F1">']

    for gv in [0, 0.25, 0.5, 0.75, 1.0]:
        y = top_pad + chart_h * (1 - gv)
        svg.append(f'<line x1="{left_pad}" y1="{y}" x2="{w - 10}" y2="{y}" class="gridline" />')
        svg.append(f'<text x="{left_pad - 6}" y="{y + 3}" text-anchor="end" class="axis-label">{gv:.2f}</text>')

    for i, cls in enumerate(order):
        gx = left_pad + i * bar_group_w + 6
        for k, (metric, color, kr_label) in enumerate(metrics):
            val = per_class.loc[cls, metric]
            bx = gx + k * (bar_w + gap)
            bh = chart_h * val
            by = top_pad + chart_h - bh
            svg.append(
                f'<rect x="{bx}" y="{by}" width="{bar_w}" height="{max(bh,1)}" rx="2" fill="{color}">'
                f'<title>{cls} · {kr_label}: {val:.3f}</title></rect>'
            )
            if metric == "recall":
                svg.append(
                    f'<text x="{bx + bar_w / 2}" y="{by - 4}" text-anchor="middle" font-size="9" '
                    f'fill="var(--text-secondary)">{val:.2f}</text>'
                )
        label_x = gx + (len(metrics) * (bar_w + gap)) / 2 - gap / 2
        label_y = top_pad + chart_h + 14
        svg.append(
            f'<text x="{label_x}" y="{label_y}" transform="rotate(-40 {label_x} {label_y})" '
            f'text-anchor="end" class="axis-label">{cls}</text>'
        )

    legend_x = left_pad
    legend_y = h - 14
    for k, (metric, color, kr_label) in enumerate(metrics):
        lx = legend_x + k * 90
        svg.append(f'<rect x="{lx}" y="{legend_y - 9}" width="10" height="10" rx="2" fill="{color}" />')
        svg.append(f'<text x="{lx + 14}" y="{legend_y}" class="axis-label">{kr_label}</text>')

    svg.append("</svg>")
    return "\n".join(svg)


def render_line_chart(by_second, overall_accuracy):
    seconds = by_second.index.tolist()
    values = by_second.values.tolist()
    n = len(seconds)
    w = 720
    h = 260
    left_pad = 44
    right_pad = 16
    top_pad = 16
    bottom_pad = 34
    plot_w = w - left_pad - right_pad
    plot_h = h - top_pad - bottom_pad

    y_min, y_max = 0.5, 1.0

    def xy(sec, val):
        x = left_pad + (sec / max(n - 1, 1)) * plot_w
        y = top_pad + (1 - (val - y_min) / (y_max - y_min)) * plot_h
        return x, y

    svg = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="경과 초별 정확도">']

    for gv in [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
        _, y = xy(0, gv)
        svg.append(f'<line x1="{left_pad}" y1="{y}" x2="{w - right_pad}" y2="{y}" class="gridline" />')
        svg.append(f'<text x="{left_pad - 6}" y="{y + 3}" text-anchor="end" class="axis-label">{gv:.1f}</text>')

    _, y_knn = xy(0, KNN_BASELINE_ACCURACY)
    svg.append(
        f'<line x1="{left_pad}" y1="{y_knn}" x2="{w - right_pad}" y2="{y_knn}" '
        f'stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="4 3" />'
    )
    svg.append(f'<text x="{w - right_pad}" y="{y_knn - 5}" text-anchor="end" class="axis-label">설계문서 kNN 기준 {KNN_BASELINE_ACCURACY:.2f}</text>')

    _, y_acc = xy(0, overall_accuracy)
    svg.append(
        f'<line x1="{left_pad}" y1="{y_acc}" x2="{w - right_pad}" y2="{y_acc}" '
        f'stroke="var(--text-secondary)" stroke-width="1" stroke-dasharray="2 3" />'
    )

    path_pts = [xy(s, v) for s, v in zip(seconds, values)]
    path_d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in path_pts)
    svg.append(f'<path d="{path_d}" fill="none" stroke="var(--series-1)" stroke-width="2" />')

    for (sec, val), (x, y) in zip(zip(seconds, values), path_pts):
        svg.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="1.5">'
            f'<title>{sec}초: 정확도 {val:.3f}</title></circle>'
        )

    for sec in seconds:
        x, _ = xy(sec, y_min)
        svg.append(f'<text x="{x}" y="{h - 10}" text-anchor="middle" class="axis-label">{sec}</text>')
    svg.append(f'<text x="{left_pad + plot_w / 2}" y="{h - 1}" text-anchor="middle" class="axis-title" style="display:none">cycle_second</text>')

    svg.append("</svg>")
    return "\n".join(svg)


def render_table(cm_df):
    rows = []
    header = "<tr><th>실제 \\ 예측</th>" + "".join(f"<th>{c}</th>" for c in CLASS_ORDER) + "</tr>"
    rows.append(header)
    for true_c in CLASS_ORDER:
        cells = "".join(f"<td>{int(cm_df.loc[true_c, c]):,}</td>" for c in CLASS_ORDER)
        rows.append(f"<tr><th>{true_c}</th>{cells}</tr>")
    return f'<table class="cm-table">{"".join(rows)}</table>'


def render_html(metrics, model_version_note):
    per_class_sorted = metrics["per_class"].sort_values("recall")
    weakest = per_class_sorted.index[0]

    stat_tiles = f"""
    <div class="tiles">
      <div class="tile"><div class="tile-label">정확도</div><div class="tile-value">{metrics['accuracy']*100:.1f}%</div></div>
      <div class="tile"><div class="tile-label">Macro F1</div><div class="tile-value">{metrics['macro_f1']:.3f}</div></div>
      <div class="tile"><div class="tile-label">설계문서 kNN 대비</div><div class="tile-value delta-good">+{(metrics['accuracy']-KNN_BASELINE_ACCURACY)*100:.1f}%p</div></div>
      <div class="tile"><div class="tile-label">검증 사이클 / 행</div><div class="tile-value">{metrics['n_cycles']:,} / {metrics['n_rows']:,}</div></div>
    </div>"""

    html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>원인 분류 모델 성능 리포트</title>
<style>
  .viz-root {{
    color-scheme: light;
    --surface-1: #fcfcfb; --page: #f9f9f7;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
    --gridline: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6; --series-2: #008300; --series-3: #e87ba4;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) .viz-root {{
      color-scheme: dark;
      --surface-1: #1a1a19; --page: #0d0d0d;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
      --gridline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
      --series-1: #3987e5; --series-2: #008300; --series-3: #d55181;
    }}
  }}
  :root[data-theme="dark"] .viz-root {{
    color-scheme: dark;
    --surface-1: #1a1a19; --page: #0d0d0d;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
    --gridline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #008300; --series-3: #d55181;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background: var(--page); color: var(--text-primary); }}
  .viz-root {{ max-width: 980px; margin: 0 auto; padding: 32px 20px 60px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .subtitle {{ color: var(--text-secondary); font-size: 13px; margin: 0 0 28px; }}
  .card {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 24px; }}
  .card h2 {{ font-size: 15px; margin: 0 0 4px; }}
  .card .desc {{ color: var(--text-secondary); font-size: 12.5px; margin: 0 0 16px; }}
  .tiles {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }}
  .tile {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }}
  .tile-label {{ color: var(--text-secondary); font-size: 12px; margin-bottom: 6px; }}
  .tile-value {{ font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .delta-good {{ color: var(--series-2); }}
  .axis-label {{ font-size: 10.5px; fill: var(--text-secondary); }}
  .axis-title {{ font-size: 11px; fill: var(--text-secondary); }}
  .gridline {{ stroke: var(--gridline); stroke-width: 1; }}
  table.cm-table {{ border-collapse: collapse; font-size: 11px; width: 100%; }}
  table.cm-table th, table.cm-table td {{ border: 1px solid var(--border); padding: 4px 6px; text-align: right; font-variant-numeric: tabular-nums; }}
  table.cm-table th {{ text-align: center; color: var(--text-secondary); font-weight: 500; }}
  table.cm-table th:first-child {{ text-align: left; }}
  details summary {{ cursor: pointer; color: var(--text-secondary); font-size: 12.5px; margin-top: 12px; }}
  .note {{ font-size: 12.5px; color: var(--text-secondary); line-height: 1.6; }}
  .note b {{ color: var(--text-primary); }}
  footer {{ color: var(--muted); font-size: 11px; margin-top: 32px; }}
</style>
</head>
<body>
<div class="viz-root">
  <h1>유압 프레스 고장 원인 분류 — 모델 성능 리포트</h1>
  <p class="subtitle">{model_version_note} · 검증셋(cycle_id 기준 group split, 20% held-out)</p>

  {stat_tiles}

  <div class="card">
    <h2>혼동행렬 (행=실제, 열=예측, 색=행 기준 비율)</h2>
    <p class="desc">대각선(굵은 테두리)이 정답. 셀에 마우스를 올리면 정확한 건수/비율이 나옵니다.</p>
    {render_heatmap(metrics['cm'], metrics['cm_norm'])}
    <details>
      <summary>숫자 표로 보기</summary>
      {render_table(metrics['cm'])}
    </details>
  </div>

  <div class="card">
    <h2>클래스별 정밀도 · 재현율 · F1</h2>
    <p class="desc">재현율(recall) 오름차순 정렬 — 가장 취약한 클래스가 왼쪽. 현재 최약체: <b>{weakest}</b>
      (recall {per_class_sorted.loc[weakest, 'recall']:.3f})</p>
    {render_bar_chart(metrics['per_class'])}
  </div>

  <div class="card">
    <h2>경과 시간(cycle_second)별 정확도 — 실시간 탐지 지연 곡선</h2>
    <p class="desc">사이클이 진행될수록(디지털트윈이 매 초 데이터를 더 보낼수록) 예측이 얼마나 정확해지는지.
      점선은 설계문서 kNN 사이클단위 기준({KNN_BASELINE_ACCURACY:.2f}).</p>
    {render_line_chart(metrics['by_second'], metrics['accuracy'])}
  </div>

  <div class="card">
    <h2>참고 — 알려진 한계</h2>
    <p class="note">
      <b>relief_early</b>는 목표압이 낮춰진 릴리프 설정압에 근접하는 짧은 구간(주로 downstroke 4~7초)에만
      물리적으로 신호가 존재해서, 그 외 구간에서는 정상과 실질적으로 구별이 어렵습니다(모델 용량을 늘려도
      개선 안 됨, 실험으로 확인됨 — <code>docs/원인분류_모델_설계문서.md</code> v5 참고). 누설계열
      (ext_leak/seal_leak/valve_int_leak)도 서로 물리적 신호가 겹치는 구간이 있어 완전한 분리는 어렵습니다.
    </p>
  </div>

  <footer>생성: scripts/visualize_results.py · 모델: models/hierarchical_lgbm.joblib</footer>
</div>
</body>
</html>"""
    return html


def main():
    bundle = joblib.load(MODEL_PATH)

    print("loading + engineering...")
    t0 = time.time()
    df = pd.read_csv(DATA_PATH, low_memory=False)
    df = add_engineered_features(df)
    for c in CATEGORICAL_FEATURES:
        df[c] = df[c].astype("category")
    _, val_df = group_stratified_split(df)
    print(f"  done in {time.time() - t0:.1f}s")

    metrics = compute_metrics(bundle, val_df)
    print(f"accuracy={metrics['accuracy']:.3f} macro_f1={metrics['macro_f1']:.3f}")

    html = render_html(metrics, model_version_note="v9 계층형 (relief_set_pressure_bar + 리턴라인 물리모델링 수정 반영)")
    Path(OUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(OUT_PATH).write_text(html, encoding="utf-8")
    print(f"saved -> {OUT_PATH}")


if __name__ == "__main__":
    main()
