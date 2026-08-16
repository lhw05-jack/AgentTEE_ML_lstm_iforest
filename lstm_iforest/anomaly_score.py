"""AgentTEE 双模型异常分数融合与测试模块。

流程：
1. 调用 IForest 模型脚本训练 IForest
2. 调用 LSTM 模型脚本训练 LSTM
3. 复用本次 LSTM 训练时划出的测试集；
4. IForest 对每条序列的最后一个有效动作计算异常分；
5. LSTM 对整条有效动作序列计算异常概率；
6. 按原 Enclave.cpp 的行为类型权重融合两个分数；
7. 分别统计干净/中毒测试序列的平均异常分数。

运行方式：
在该文件lstm_iforset.py文件的上一级目录中终端:
############
python -m lstm_iforest.anomaly_score
############
即可运行
"""

import numpy as np

from .iforset.normal_data_iforset import extract_normal_data
from .iforset.iforest_model import train_iforest_model
from .iforset.feature_extractor import extract_dataset_features

from .lstm.lstm_model import train_lstm_model, LSTM_MODE_STANDARD
from .lstm.lstm_training_data import build_lstm_training_arrays


# ============================================================
# 1. IForest 推理：沿用当前 main_iforset.py 的计算方式
# ============================================================

def _nodes_per_tree(model):
    max_depth = int(np.ceil(np.log2(model["max_samples"])))
    return (1 << (max_depth + 1)) - 1


def _isolation_tree_depth(sample, model, tree_index):
    split_features = model["tree_split_features"]
    split_values = model["tree_split_values"]
    nodes_per_tree = _nodes_per_tree(model)
    tree_offset = tree_index * nodes_per_tree

    current_node = 0
    depth = 0.0

    while current_node < nodes_per_tree:
        absolute_index = tree_offset + current_node
        split_feature = int(split_features[absolute_index])
        split_value = float(split_values[absolute_index])

        if split_feature < 0 or split_feature >= len(sample):
            break

        if sample[split_feature] <= split_value:
            current_node = current_node * 2 + 1
        else:
            current_node = current_node * 2 + 2

        depth += 1.0

    return depth - 4


def _iforest_score_one(sample, model):
    n_estimators = model["n_estimators"]
    max_depth = int(np.ceil(np.log2(model["max_samples"])))  - 4

    if max_depth <= 0:
        return 0.5

    total_normalized_depth = 0.0

    for tree_index in range(n_estimators):
        depth = _isolation_tree_depth(sample, model, tree_index) 
        total_normalized_depth += np.clip(depth / max_depth, 0.0, 1.0)

    return float(np.clip(1.0 - total_normalized_depth / n_estimators, 0.0, 1.0))


def _risk_penalty(x):
    """沿用当前 main_iforset.py 的关键风险特征补偿。"""
    penalty = 0.0

    if x[2] >= 0.5:
        penalty += 0.1
    if x[5] >= 0.5:
        penalty += 0.1
    if x[6] >= 0.6:
        penalty += 0.1
    if x[7] >= 0.5:
        penalty += 0.1
    if x[8] >= 0.33:
        penalty += 0.1
    if x[15] >= 0.2:
        penalty += 0.1

    return min(penalty, 0.6)


def iforest_score_samples(features, model):
    features = np.asarray(features, dtype=np.float32)
    raw_features = np.clip(features, 0.0, 1.0)

    means = model["feature_means"]
    stds = model["feature_stds"]
    safe_stds = np.where(stds > 1e-6, stds, 1.0)
    normalized = ((raw_features - means) / safe_stds).astype(np.float32)

    scores = []

    for raw_sample, normalized_sample in zip(raw_features, normalized):
        score = _iforest_score_one(normalized_sample, model)
        score = min(score + _risk_penalty(raw_sample), 1.0)
        scores.append(score)

    return np.asarray(scores, dtype=np.float32)


# ============================================================
# 2. LSTM 推理：使用 lstm_model.py 训练后导出的参数
# ============================================================

def _sigmoid(x):
    x = np.clip(x, -50.0, 50.0)
    return 1.0 / (1.0 + np.exp(-x))


def unpack_lstm_parameters(model):
    input_dim = int(model["input_dim"])
    hidden_dim = int(model["hidden_dim"])

    weights = np.asarray(model["weights"], dtype=np.float32)
    biases = np.asarray(model["biases"], dtype=np.float32)

    w_gate = np.empty((4, hidden_dim, input_dim), dtype=np.float32)
    u_gate = np.empty((4, hidden_dim, hidden_dim), dtype=np.float32)

    offset = 0
    for gate in range(4):
        size_w = hidden_dim * input_dim
        w_gate[gate] = weights[offset:offset + size_w].reshape(hidden_dim, input_dim)
        offset += size_w

        size_u = hidden_dim * hidden_dim
        u_gate[gate] = weights[offset:offset + size_u].reshape(hidden_dim, hidden_dim)
        offset += size_u

    w_output = weights[offset:offset + hidden_dim]
    b_gate = biases[:4 * hidden_dim].reshape(4, hidden_dim)
    b_output = float(biases[4 * hidden_dim])

    return w_gate, u_gate, b_gate, w_output, b_output


def lstm_score_steps(x_data, lengths, model):
    """返回每条序列每个时间步的 LSTM prefix-risk 概率，[N,T]。

    第 t 步分数表示 Risk(x_1, ..., x_t)。padding 位置会保持最后一个
    有效隐藏状态对应的分数，使用者应结合 lengths 构造 mask 忽略 padding。
    """
    x_data = np.asarray(x_data, dtype=np.float32)
    lengths = np.asarray(lengths, dtype=np.int64)

    w_gate, u_gate, b_gate, w_output, b_output = unpack_lstm_parameters(model)

    sample_count, total_steps, _ = x_data.shape
    hidden_dim = int(model["hidden_dim"])

    h_state = np.zeros((sample_count, hidden_dim), dtype=np.float32)
    c_state = np.zeros_like(h_state)
    step_scores = np.zeros((sample_count, total_steps), dtype=np.float32)

    for step in range(total_steps):
        x_step = x_data[:, step, :]

        linear = (
            np.einsum("bi,ghi->bgh", x_step, w_gate)
            + np.einsum("bk,ghk->bgh", h_state, u_gate)
            + b_gate[None, :, :]
        )

        i_gate = _sigmoid(linear[:, 0, :])
        f_gate = _sigmoid(linear[:, 1, :])
        o_gate = _sigmoid(linear[:, 2, :])
        g_gate = np.tanh(linear[:, 3, :])

        new_c = f_gate * c_state + i_gate * g_gate
        new_h = o_gate * np.tanh(new_c)

        active = (step < lengths)[:, None]
        c_state = np.where(active, new_c, c_state)
        h_state = np.where(active, new_h, h_state)

        logits = h_state @ w_output + b_output
        step_scores[:, step] = _sigmoid(logits).astype(np.float32)

    return step_scores


def lstm_score_samples(x_data, lengths, model):
    """返回每条序列最后一个有效时间步的 LSTM prefix-risk 概率。"""
    lengths = np.asarray(lengths, dtype=np.int64)
    step_scores = lstm_score_steps(x_data, lengths, model)
    rows = np.arange(len(lengths))
    return step_scores[rows, lengths - 1].astype(np.float32)


# ============================================================
# 3. 原 Enclave.cpp 的双模型融合公式
# ============================================================

def get_fusion_weights(action_type):
    # 默认/文件操作：IForest 0.60，LSTM 0.40
    if action_type in (1, 3, 4):  # 系统调用 / 网络行为 / 工具使用
        return 0.50, 0.50
    if action_type == 5:          # 对话提示词
        return 0.70, 0.30
    return 0.60, 0.40


def get_ml_threshold(action_type):
    return {
        1: 0.60,  # 系统调用
        2: 0.65,  # 文件操作
        3: 0.65,  # 网络行为
        4: 0.60,  # 工具使用
        5: 0.75,  # 对话提示词
    }.get(int(action_type), 0.70)


def calculate_anomaly_score(action_type, iforest_score, lstm_score):
    iforest_weight, lstm_weight = get_fusion_weights(int(action_type))

    anomaly_score = (
        iforest_weight * float(iforest_score)
        + lstm_weight * float(lstm_score)
    )

    threshold = get_ml_threshold(int(action_type))

    return {
        "iforest_score": float(iforest_score),
        "lstm_score": float(lstm_score),
        "iforest_weight": iforest_weight,
        "lstm_weight": lstm_weight,
        "anomaly_score": float(np.clip(anomaly_score, 0.0, 1.0)),
        "ml_threshold": threshold,
        "is_anomaly": anomaly_score > threshold,
    }


# ============================================================
# 4. 训练两个模型
# ============================================================

def train_models():
    print("========== 1. 训练 Isolation Forest ==========")

    normal_data = extract_normal_data()
    normal_features = extract_dataset_features(normal_data)

    iforest_model = train_iforest_model(
        normal_data=normal_features,
        n_estimators=64,
        max_samples=1024,
        seed=42,
    )

    print(
        f"IForest训练完成: normal={len(normal_features)}, "
        f"trees={iforest_model['n_estimators']}"
    )

    print("\n========== 2. 训练 LSTM ==========")

    lstm_model = train_lstm_model(LSTM_MODE_STANDARD)

    print(
        f"LSTM训练完成: test_acc="
        f"{lstm_model['training_metrics']['test_accuracy'] * 100:.2f}%"
    )

    # ============================================================
    # 保存模型
    # ============================================================

    from .model_io import save_models

    print(
        "\n========== 保存模型 =========="
    )

    save_models(
        iforest_model,
        lstm_model,
    )

    return iforest_model, lstm_model


# ============================================================
# 5. 复用 LSTM 测试集，计算融合异常分数
# ============================================================

def evaluate_on_lstm_test_set(iforest_model, lstm_model):
    # 与 LSTM 训练时完全相同的数据生成/打乱方式。
    x_data, step_labels, lengths = build_lstm_training_arrays(
        seq_len=lstm_model["seq_len"],
        shuffle=True,
        seed=42,
    )

    test_indices = np.asarray(lstm_model["test_indices"], dtype=np.int64)

    x_test = x_data[test_indices]
    step_labels_test = step_labels[test_indices]
    lengths_test = lengths[test_indices]

    # 兼容原来的“整条序列”评估：取最后一个有效时间步标签作为 sequence label。
    test_rows = np.arange(len(test_indices))
    y_test = step_labels_test[test_rows, lengths_test - 1]

    # IForest只看每条序列最后一个有效动作的16维特征。
    current_features = x_test[
        np.arange(len(x_test)),
        lengths_test - 1,
    ]

    iforest_scores = iforest_score_samples(current_features, iforest_model)

    # LSTM看整条序列。
    lstm_scores = lstm_score_samples(x_test, lengths_test, lstm_model)

    # x0 = action_type / 5，因此可恢复最后一步行为类型。
    action_types = np.rint(current_features[:, 0] * 5.0).astype(np.int32)
    action_types = np.clip(action_types, 1, 5)

    anomaly_scores = np.zeros(len(x_test), dtype=np.float32)
    thresholds = np.zeros(len(x_test), dtype=np.float32)

    for i in range(len(x_test)):
        result = calculate_anomaly_score(
            action_types[i],
            iforest_scores[i],
            lstm_scores[i],
        )
        anomaly_scores[i] = result["anomaly_score"]
        thresholds[i] = result["ml_threshold"]

    clean_mask = y_test < 0.5
    poison_mask = y_test >= 0.5

    print("\n========== 3. LSTM测试集融合异常分数 ==========")
    print("测试集总数:", len(y_test))
    print("干净序列:", int(np.sum(clean_mask)))
    print("中毒序列:", int(np.sum(poison_mask)))

    print("\n---------- 干净序列 ----------")
    print(f"IForest平均分: {np.mean(iforest_scores[clean_mask]):.6f}")
    print(f"LSTM平均分:    {np.mean(lstm_scores[clean_mask]):.6f}")
    print(f"融合平均异常分: {np.mean(anomaly_scores[clean_mask]):.6f}")

    print("\n---------- 中毒序列 ----------")
    print(f"IForest平均分: {np.mean(iforest_scores[poison_mask]):.6f}")
    print(f"LSTM平均分:    {np.mean(lstm_scores[poison_mask]):.6f}")
    print(f"融合平均异常分: {np.mean(anomaly_scores[poison_mask]):.6f}")

    clean_mean = float(np.mean(anomaly_scores[clean_mask]))
    poison_mean = float(np.mean(anomaly_scores[poison_mask]))

    print("\n---------- 分离情况 ----------")
    print(f"干净平均异常分: {clean_mean:.6f}")
    print(f"中毒平均异常分: {poison_mean:.6f}")
    print(f"中毒 - 干净:   {poison_mean - clean_mean:.6f}")

    predictions = anomaly_scores > thresholds
    accuracy = float(np.mean(predictions == poison_mask))
    attack_recall = float(np.mean(predictions[poison_mask])) if np.any(poison_mask) else 0.0

    print(f"融合阈值判定ACC: {accuracy * 100:.2f}%")
    print(f"中毒检出率:      {attack_recall * 100:.2f}%")

    return {
        "labels": y_test,
        "step_labels": step_labels_test,
        "action_types": action_types,
        "iforest_scores": iforest_scores,
        "lstm_scores": lstm_scores,
        "lstm_step_scores": lstm_score_steps(x_test, lengths_test, lstm_model),
        "anomaly_scores": anomaly_scores,
        "thresholds": thresholds,
        "clean_mean": clean_mean,
        "poison_mean": poison_mean,
        "score_gap": poison_mean - clean_mean,
        "accuracy": accuracy,
        "attack_recall": attack_recall,
    }


def main():
    iforest_model, lstm_model = train_models()
    evaluate_on_lstm_test_set(iforest_model, lstm_model)


if __name__ == "__main__":
    main()
