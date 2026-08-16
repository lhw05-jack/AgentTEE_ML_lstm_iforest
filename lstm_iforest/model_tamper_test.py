"""AgentTEE model / inference-logic tampering experiment (TEST ONLY).

Purpose
-------
Demonstrate why running ML security detection in an untrusted software host is
not sufficient.  The harness loads the project's already-saved IForest/LSTM
models, evaluates the same five synthetic Agent attack cases used by
``prompt_hijack_test.py``, then applies controlled in-memory tampering and
compares the detector output before/after tampering.

No command, file, network, or privileged tool action is executed.
No saved model file is overwritten.  Tampering is performed on deep copies of
loaded model dictionaries unless ``--mode logic-*`` is selected, in which case
only this test harness changes post-processing behavior.

Run from the directory above ``lstm_iforest``::

    python -m lstm_iforest.model_tamper_test --mode none
    python -m lstm_iforest.model_tamper_test --mode lstm-output-head
    python -m lstm_iforest.model_tamper_test --mode iforest-tree-depth
    python -m lstm_iforest.model_tamper_test --mode model-combined
    python -m lstm_iforest.model_tamper_test --mode logic-score-scale
    python -m lstm_iforest.model_tamper_test --mode logic-threshold-raise
    python -m lstm_iforest.model_tamper_test --mode logic-fusion-scale
    python -m lstm_iforest.model_tamper_test --mode logic-combined

The goal is controlled integrity testing, not a production bypass mechanism.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

import numpy as np

from .anomaly_score import (
    calculate_anomaly_score,
    iforest_score_samples,
    lstm_score_samples,
)
from .iforset.feature_extractor import ACTION_TYPES
from .prompt_hijack_test import (
    DEFAULT_MODEL_DIR,
    TEST_ONLY_MARKER,
    build_default_cases,
    featurize_actions,
    load_saved_models,
)


TAMPER_TEST_MARKER = "AGENTTEE_MODEL_TAMPER_EXPERIMENT_ONLY"


@dataclass
class EvalSnapshot:
    case_id: str
    target_category: str
    final_action_type: str
    iforest_score: float
    lstm_score: float
    fused_score: float
    threshold: float
    decision: str


@dataclass
class ComparisonResult:
    case_id: str
    target_category: str
    baseline: EvalSnapshot
    tampered: EvalSnapshot
    bypass_succeeded: bool


# ---------------------------------------------------------------------------
# 1. Model-file integrity helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def model_file_hashes(model_dir: Path) -> Dict[str, str]:
    model_dir = Path(model_dir)
    return {
        "iforest_model.npz": sha256_file(model_dir / "iforest_model.npz"),
        "lstm_model.npz": sha256_file(model_dir / "lstm_model.npz"),
    }


# ---------------------------------------------------------------------------
# 2. TEST-ONLY model-parameter tampering
# ---------------------------------------------------------------------------


def tamper_lstm_output_head(
    model: Mapping[str, object],
    forced_logit: float = -8.0,
) -> Dict[str, object]:
    """
    篡改LSTM的输出头 forced_logit: 偏置超参数

    python -m lstm_iforest.model_tamper_test \
    --mode lstm-output-head

    Return a copy whose LSTM output head is forced toward a low risk score.

    The recurrent LSTM parameters remain intact.  Only the final binary head is
    changed:

        logit = h_T @ w_output + b_output

    becomes approximately a constant ``forced_logit`` by setting
    ``w_output = 0`` and ``b_output = forced_logit``.

    In the current exported layout:
      - the final ``hidden_dim`` values of ``weights`` are w_output;
      - biases[4 * hidden_dim] is b_output.
    """

    out = copy.deepcopy(dict(model))
    hidden_dim = int(out["hidden_dim"])

    weights = np.asarray(out["weights"], dtype=np.float32).copy()
    biases = np.asarray(out["biases"], dtype=np.float32).copy()

    weights[-hidden_dim:] = 0.0
    biases[4 * hidden_dim] = np.float32(forced_logit)

    out["weights"] = weights
    out["biases"] = biases
    return out


def tamper_iforest_tree_depth(
    model: Mapping[str, object],
    split_feature: int = 0,
    forced_split_value: float = 1.0e6,
) -> Dict[str, object]:
    """
    破坏IForest算法的模型, 强制样本沿固定路径走到树的最深处位置, 减小异常分数

    Return a copy with IForest tree arrays changed to force full-depth walks.

    The project encodes a leaf using an invalid/negative split feature.  This
    test changes all tree nodes, including leaf sentinels, into valid split
    nodes that always choose the same branch.  Traversal therefore continues
    until the fixed tree-array boundary and the raw isolation score trends to
    zero ("normal").

    NOTE: the project's separate ``_risk_penalty`` still applies afterwards,
    so this experiment isolates tree-parameter integrity from scoring-logic
    integrity rather than guaranteeing every final sample is ALLOW by itself.
    """

    out = copy.deepcopy(dict(model))

    features = np.asarray(out["tree_split_features"], dtype=np.int32).copy()
    values = np.asarray(out["tree_split_values"], dtype=np.float32).copy()

    features[:] = np.int32(split_feature)
    values[:] = np.float32(forced_split_value)

    out["tree_split_features"] = features
    out["tree_split_values"] = values
    return out


def apply_model_tamper(
    mode: str,
    iforest_model,
    lstm_model,
    forced_logit: float,
):
    '''
    三种模型参数篡改模式:

    1. 仅修改LSTM输出头  --lstm-output-head
    2. 仅修改IForest二叉路径选择  --iforest-tree-depth
    3. LSTM和IForest都篡改  --model-combined
    '''
    iforest_t = copy.deepcopy(iforest_model)
    lstm_t = copy.deepcopy(lstm_model)

    if mode == "lstm-output-head":
        lstm_t = tamper_lstm_output_head(lstm_t, forced_logit=forced_logit)
    elif mode == "iforest-tree-depth":
        iforest_t = tamper_iforest_tree_depth(iforest_t)
    elif mode == "model-combined":
        iforest_t = tamper_iforest_tree_depth(iforest_t)
        lstm_t = tamper_lstm_output_head(lstm_t, forced_logit=forced_logit)

    return iforest_t, lstm_t


# ---------------------------------------------------------------------------
# 3. TEST-ONLY scoring / fusion-logic tampering
# ---------------------------------------------------------------------------


def calculate_tampered_logic(
    action_type: int,
    iforest_score: float,
    lstm_score: float,
    mode: str,
    score_scale: float = 0.05,
    fusion_scale: float = 0.10,
    raised_threshold: float = 0.999,
) -> Dict[str, object]:
    """
    篡改异常分数的计算逻辑：

    1. 将单个异常分数的计算 *0.05
    2. 篡改最终计算结果 *0.10
    3. 阈值篡改 将异常阈值改为 0.999

    Evaluate controlled post-processing tamper modes.

    The baseline first uses the project's original fusion formula.  Each mode
    then mutates one software-side step to demonstrate an integrity failure.
    """

    # Baseline values and original action-specific weights/threshold.
    base = calculate_anomaly_score(action_type, iforest_score, lstm_score)

    if mode == "logic-score-scale":
        # Simulates a changed anomaly-score function that suppresses both model
        # outputs before the legitimate fusion code sees them.
        i_score = float(np.clip(iforest_score * score_scale, 0.0, 1.0))
        l_score = float(np.clip(lstm_score * score_scale, 0.0, 1.0))
        return calculate_anomaly_score(action_type, i_score, l_score)

    if mode == "logic-fusion-scale":
        # Preserve both model outputs and the original weighted fusion, but
        # tamper the software-side fused value before threshold comparison.
        fused = float(np.clip(base["anomaly_score"] * fusion_scale, 0.0, 1.0))
        threshold = float(base["ml_threshold"])
        return {
            **base,
            "anomaly_score": fused,
            "is_anomaly": fused > threshold,
        }

    if mode == "logic-threshold-raise":
        # Keep model scores unchanged, but modify the action threshold table.
        threshold = float(raised_threshold)
        fused = float(base["anomaly_score"])
        return {
            **base,
            "ml_threshold": threshold,
            "is_anomaly": fused > threshold,
        }

    if mode == "logic-comparison-flip":
        # Most direct control-flow integrity test: same scores and threshold,
        # changed predicate.  Kept explicit so results cannot be confused with
        # an ML evasion attack.
        fused = float(base["anomaly_score"])
        threshold = float(base["ml_threshold"])
        return {
            **base,
            "is_anomaly": fused < threshold,
        }

    if mode == "logic-combined":
        # Strong software-side suppression: scale the two model outputs and
        # raise the threshold.  Useful as a deterministic A/B test.
        i_score = float(np.clip(iforest_score * score_scale, 0.0, 1.0))
        l_score = float(np.clip(lstm_score * score_scale, 0.0, 1.0))
        scaled = calculate_anomaly_score(action_type, i_score, l_score)
        fused = float(scaled["anomaly_score"])
        threshold = float(raised_threshold)
        return {
            **scaled,
            "ml_threshold": threshold,
            "is_anomaly": fused > threshold,
        }

    return base


# ---------------------------------------------------------------------------
# 4. Common evaluation path
# ---------------------------------------------------------------------------


def evaluate_one(
    case,
    iforest_model,
    lstm_model,
    logic_mode: str = "none",
    score_scale: float = 0.05,
    fusion_scale: float = 0.10,
    raised_threshold: float = 0.999,
) -> EvalSnapshot:
    x_data, lengths, current_features, final_action_type_name = featurize_actions(
        case.actions,
        seq_len=int(lstm_model["seq_len"]),
        case_id=case.case_id,
    )

    i_score = float(iforest_score_samples(current_features, iforest_model)[0])
    l_score = float(lstm_score_samples(x_data, lengths, lstm_model)[0])
    action_type = int(ACTION_TYPES[final_action_type_name])

    if logic_mode.startswith("logic-"):
        fused = calculate_tampered_logic(
            action_type,
            i_score,
            l_score,
            mode=logic_mode,
            score_scale=score_scale,
            fusion_scale=fusion_scale,
            raised_threshold=raised_threshold,
        )
    else:
        fused = calculate_anomaly_score(action_type, i_score, l_score)

    return EvalSnapshot(
        case_id=case.case_id,
        target_category=case.target_category,
        final_action_type=final_action_type_name,
        iforest_score=i_score,
        lstm_score=l_score,
        fused_score=float(fused["anomaly_score"]),
        threshold=float(fused["ml_threshold"]),
        decision="BLOCK" if bool(fused["is_anomaly"]) else "ALLOW",
    )


def print_snapshot(prefix: str, s: EvalSnapshot) -> None:
    print(
        f"{prefix:<9} "
        f"IForest={s.iforest_score:.4f}  "
        f"LSTM={s.lstm_score:.4f}  "
        f"Fusion={s.fused_score:.4f}  "
        f"Threshold={s.threshold:.4f}  "
        f"Decision={s.decision}"
    )


# ---------------------------------------------------------------------------
# 5. CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Controlled AgentTEE model/inference-logic integrity experiment. "
            "Loads existing saved_models and never executes real dangerous actions."
        )
    )
    parser.add_argument(
        "--mode",
        choices=[
            "none",
            "lstm-output-head",
            "iforest-tree-depth",
            "model-combined",
            "logic-score-scale",
            "logic-fusion-scale",
            "logic-threshold-raise",
            "logic-comparison-flip",
            "logic-combined",
        ],
        default="none",
    )
    parser.add_argument(
        "--case",
        choices=["all", "system", "file", "network", "tool", "prompt"],
        default="all",
    )
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument(
        "--forced-logit",
        type=float,
        default=-8.0,
        help="Forced LSTM output-head logit for lstm-output-head/model-combined.",
    )
    parser.add_argument(
        "--score-scale",
        type=float,
        default=0.05,
        help="Multiplier for logic-score-scale/logic-combined.",
    )
    parser.add_argument(
        "--fusion-scale",
        type=float,
        default=0.10,
        help="Multiplier for logic-fusion-scale.",
    )
    parser.add_argument(
        "--raised-threshold",
        type=float,
        default=0.999,
        help="Tampered threshold for logic-threshold-raise/logic-combined.",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    model_dir = Path(args.model_dir).resolve()
    hashes_before = model_file_hashes(model_dir)

    iforest_model, lstm_model = load_saved_models(model_dir)
    cases = build_default_cases()
    selected = list(cases) if args.case == "all" else [args.case]

    # Baseline always uses untouched loaded models and untouched inference logic.
    baseline_iforest = copy.deepcopy(iforest_model)
    baseline_lstm = copy.deepcopy(lstm_model)

    # Parameter-tamper modes alter only in-memory copies.
    tampered_iforest, tampered_lstm = apply_model_tamper(
        args.mode,
        iforest_model,
        lstm_model,
        forced_logit=args.forced_logit,
    )

    comparisons: List[ComparisonResult] = []

    print("=" * 92)
    print(f"AgentTEE controlled model/logic tamper experiment | mode={args.mode}")
    print(f"marker={TAMPER_TEST_MARKER}")

    for key in selected:
        case = cases[key]
        baseline = evaluate_one(
            case,
            baseline_iforest,
            baseline_lstm,
            logic_mode="none",
        )

        logic_mode = args.mode if args.mode.startswith("logic-") else "none"
        tampered = evaluate_one(
            case,
            tampered_iforest,
            tampered_lstm,
            logic_mode=logic_mode,
            score_scale=args.score_scale,
            fusion_scale=args.fusion_scale,
            raised_threshold=args.raised_threshold,
        )

        bypass_succeeded = baseline.decision == "BLOCK" and tampered.decision == "ALLOW"
        result = ComparisonResult(
            case_id=case.case_id,
            target_category=case.target_category,
            baseline=baseline,
            tampered=tampered,
            bypass_succeeded=bypass_succeeded,
        )
        comparisons.append(result)

        print("\n" + "-" * 92)
        print(f"{case.case_id} | {case.target_category} | final={baseline.final_action_type}")
        print_snapshot("BASELINE", baseline)
        print_snapshot("TAMPERED", tampered)
        print(f"Bypass succeeded: {bypass_succeeded}")

    hashes_after = model_file_hashes(model_dir)
    files_unchanged = hashes_before == hashes_after

    blocked_before = sum(c.baseline.decision == "BLOCK" for c in comparisons)
    blocked_after = sum(c.tampered.decision == "BLOCK" for c in comparisons)
    bypass_count = sum(c.bypass_succeeded for c in comparisons)
    eligible = sum(c.baseline.decision == "BLOCK" for c in comparisons)

    summary = {
        "mode": args.mode,
        "total_cases": len(comparisons),
        "baseline_block_count": blocked_before,
        "tampered_block_count": blocked_after,
        "bypass_count": bypass_count,
        "bypass_rate_over_baseline_blocks": bypass_count / eligible if eligible else 0.0,
        "saved_model_files_unchanged": files_unchanged,
        "saved_model_sha256_before": hashes_before,
        "saved_model_sha256_after": hashes_after,
        "test_only_marker": TAMPER_TEST_MARKER,
        "prompt_harness_marker": TEST_ONLY_MARKER,
    }

    print("\n" + "=" * 92)
    print("Summary")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.json_out is not None:
        out = Path(args.json_out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "summary": summary,
                    "comparisons": [asdict(x) for x in comparisons],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"JSON report saved: {out}")


if __name__ == "__main__":
    main()
