# ============================================================
# model_io.py
#
# AgentTEE 双模型参数保存 / 加载模块
# ============================================================

from pathlib import Path
import json

import numpy as np


DEFAULT_MODEL_DIR = Path("./lstm_iforest/saved_models")


# ============================================================
# IForest
# ============================================================

def save_iforest_model(
    model,
    path=DEFAULT_MODEL_DIR / "iforest_model.npz",
):
    """
    保存 Isolation Forest 参数。
    """

    path = Path(path)
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    np.savez_compressed(

        path,

        n_estimators=np.int32(
            model["n_estimators"]
        ),

        max_samples=np.int32(
            model["max_samples"]
        ),

        feature_dim=np.int32(
            model["feature_dim"]
        ),

        tree_param_len=np.int32(
            model["tree_param_len"]
        ),

        feature_means=np.asarray(
            model["feature_means"],
            dtype=np.float32,
        ),

        feature_stds=np.asarray(
            model["feature_stds"],
            dtype=np.float32,
        ),

        tree_split_features=np.asarray(
            model["tree_split_features"],
            dtype=np.int32,
        ),

        tree_split_values=np.asarray(
            model["tree_split_values"],
            dtype=np.float32,
        ),
    )

    print(
        f"IForest模型已保存: {path}"
    )


def load_iforest_model(
    path=DEFAULT_MODEL_DIR / "iforest_model.npz",
):
    """
    加载 Isolation Forest 参数。
    """

    path = Path(path)

    with np.load(
        path,
        allow_pickle=False,
    ) as data:

        model = {

            "n_estimators":
                int(data["n_estimators"]),

            "max_samples":
                int(data["max_samples"]),

            "feature_dim":
                int(data["feature_dim"]),

            "tree_param_len":
                int(data["tree_param_len"]),

            "feature_means":
                data["feature_means"]
                .astype(np.float32),

            "feature_stds":
                data["feature_stds"]
                .astype(np.float32),

            "tree_split_features":
                data["tree_split_features"]
                .astype(np.int32),

            "tree_split_values":
                data["tree_split_values"]
                .astype(np.float32),
        }

    print(
        f"IForest模型已加载: {path}"
    )

    return model


# ============================================================
# LSTM
# ============================================================

def save_lstm_model(
    model,
    path=DEFAULT_MODEL_DIR / "lstm_model.npz",
):
    """
    保存 LSTM 扁平权重和偏置。
    """

    path = Path(path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # 训练指标转JSON保存
    metrics = model.get(
        "training_metrics",
        {},
    )

    np.savez_compressed(

        path,

        input_dim=np.int32(
            model["input_dim"]
        ),

        hidden_dim=np.int32(
            model["hidden_dim"]
        ),

        seq_len=np.int32(
            model["seq_len"]
        ),

        param_count=np.int32(
            model["param_count"]
        ),

        lstm_mode=np.int32(
            model["lstm_mode"]
        ),

        weights=np.asarray(
            model["weights"],
            dtype=np.float32,
        ),

        biases=np.asarray(
            model["biases"],
            dtype=np.float32,
        ),

        mode_name=np.str_(
            model.get(
                "mode_name",
                "",
            )
        ),

        training_source=np.str_(
            model.get(
                "training_source",
                "",
            )
        ),

        training_metrics=np.str_(
            json.dumps(
                metrics,
                ensure_ascii=False,
            )
        ),
    )

    print(
        f"LSTM模型已保存: {path}"
    )


def load_lstm_model(
    path=DEFAULT_MODEL_DIR / "lstm_model.npz",
):
    """
    加载 LSTM 参数。
    """

    path = Path(path)

    with np.load(
        path,
        allow_pickle=False,
    ) as data:

        metrics_text = str(
            data["training_metrics"]
        )

        try:
            metrics = json.loads(
                metrics_text
            )
        except Exception:
            metrics = {}

        model = {

            "input_dim":
                int(data["input_dim"]),

            "hidden_dim":
                int(data["hidden_dim"]),

            "seq_len":
                int(data["seq_len"]),

            "param_count":
                int(data["param_count"]),

            "lstm_mode":
                int(data["lstm_mode"]),

            "weights":
                data["weights"]
                .astype(np.float32),

            "biases":
                data["biases"]
                .astype(np.float32),

            "mode_name":
                str(data["mode_name"]),

            "training_source":
                str(data["training_source"]),

            "training_metrics":
                metrics,
        }

    print(
        f"LSTM模型已加载: {path}"
    )

    return model


# ============================================================
# 同时保存两个模型
# ============================================================

def save_models(
    iforest_model,
    lstm_model,
    model_dir=DEFAULT_MODEL_DIR,
):

    model_dir = Path(
        model_dir
    )

    save_iforest_model(
        iforest_model,
        model_dir / "iforest_model.npz",
    )

    save_lstm_model(
        lstm_model,
        model_dir / "lstm_model.npz",
    )


# ============================================================
# 同时加载两个模型
# ============================================================

def load_models(
    model_dir=DEFAULT_MODEL_DIR,
):

    model_dir = Path(
        model_dir
    )

    iforest_model = (
        load_iforest_model(
            model_dir
            / "iforest_model.npz"
        )
    )

    lstm_model = (
        load_lstm_model(
            model_dir
            / "lstm_model.npz"
        )
    )

    return (
        iforest_model,
        lstm_model,
    )