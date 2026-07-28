"""기존 오류 Test 시계열로 정상 오경보율과 오류 탐지율을 계산합니다."""

import numpy as np
import pandas as pd

from config import (
    FAULT_TEST_PATH,
    FEATURE_COLUMNS,
    LENGTH_BUCKETS,
    RISK_ALERT_THRESHOLD_PERCENT,
)
from preparation import convert_features


def _rates(fault: np.ndarray, alert: np.ndarray, selected: np.ndarray) -> dict:
    """선택된 행 안에서 정상 오경보율과 오류 탐지율을 안전하게 계산합니다."""
    normal_rows = selected & ~fault
    fault_rows = selected & fault
    return {
        "row_count": int(selected.sum()),
        "fault_row_count": int(fault_rows.sum()),
        "normal_false_alarm_rate": (
            float(np.mean(alert[normal_rows])) if normal_rows.any() else None
        ),
        "fault_detection_rate": (
            float(np.mean(alert[fault_rows])) if fault_rows.any() else None
        ),
    }


def summarize_metrics(
    fault_classes,
    scores,
    window_rows,
    alert_threshold_percent,
) -> dict:
    """전체 결과와 입력 길이 네 구간의 평가 지표를 함께 정리합니다."""
    fault = np.asarray(fault_classes) != "normal"
    scores = np.asarray(scores, dtype=np.float32)
    window_rows = np.asarray(window_rows, dtype=np.int32)
    alert = scores >= float(alert_threshold_percent)
    selected_all = np.ones(len(scores), dtype=bool)

    overall = _rates(fault, alert, selected_all)
    metrics = {
        "evaluation_row_count": overall["row_count"],
        "fault_row_count": overall["fault_row_count"],
        "normal_false_alarm_rate": overall["normal_false_alarm_rate"],
        "fault_detection_rate": overall["fault_detection_rate"],
        "window_length_metrics": {},
    }
    for minimum, maximum in LENGTH_BUCKETS:
        selected = (window_rows >= minimum) & (window_rows <= maximum)
        name = "150" if minimum == maximum == 150 else f"{minimum}_{maximum}"
        metrics["window_length_metrics"][name] = _rates(fault, alert, selected)
    return metrics


def evaluate(model):
    """각 열화 이력의 경계를 지키면서 Test 데이터 전체를 시계열로 평가합니다."""
    data = pd.read_csv(
        FAULT_TEST_PATH,
        usecols=["trajectory_id", "fault_class", *FEATURE_COLUMNS],
    )
    features = convert_features(data)
    scores, window_rows = model.score_stream(
        features,
        group_ids=data["trajectory_id"].to_numpy(),
    )
    return summarize_metrics(
        data["fault_class"].to_numpy(),
        scores,
        window_rows,
        alert_threshold_percent=RISK_ALERT_THRESHOLD_PERCENT,
    )
