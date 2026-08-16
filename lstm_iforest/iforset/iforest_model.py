"""AgentTEE 的独立 Isolation Forest 模型训练模块。

只包含 Isolation Forest 参数生成逻辑。
返回的字典字段和参数内存布局与现有 APP/Enclave 接口兼容。
"""

import numpy as np


ML_FEATURE_DIM = 16


__all__ = [
    "train_iforest_model",
]


# 与 AgentTEE.py 的16维行为特征定义一致。
IFOREST_FEATURE_MEANS = np.array([
    0.50,  # 0  行为类型归一化
    0.20,  # 1  内容长度
    0.05,  # 2  危险关键词数量
    0.03,  # 3  敏感文件命中
    0.03,  # 4  网络指标命中
    0.03,  # 5  系统命令指标
    0.05,  # 6  工具风险
    0.05,  # 7  提示词风险
    0.02,  # 8  混淆编码风险
    0.10,  # 9  会话历史事件数
    0.02,  # 10 会话历史异常数
    0.10,  # 11 历史平均异常分
    0.05,  # 12 近60秒频率
    0.00,  # 13 是否存在父事件
    0.20,  # 14 文本熵
    0.05,  # 15 综合风险
], dtype=np.float32)

IFOREST_FEATURE_STDS = np.array([
    0.30, 0.20, 0.15, 0.12,
    0.12, 0.12, 0.20, 0.20,
    0.12, 0.20, 0.15, 0.20,
    0.15, 0.30, 0.20, 0.20,
], dtype=np.float32)


def train_iforest_model(normal_data=None, n_estimators=32,
                        max_samples=256, seed=42):
    """训练 Isolation Forest 并返回可直接传给 Enclave 的扁平参数。

    参数：
        normal_data: 可选的正常行为特征，形状必须是 [N, 16]。
                     未提供时使用与当前项目一致的正常行为合成基线。
        n_estimators: 树数量。
        max_samples: 每棵树的最大抽样数量。
        seed: 随机种子。

    叶节点编码：
        tree_split_features[node] = -1
        tree_split_values[node] = 叶节点样本数量
    """
    if n_estimators <= 0:
        raise ValueError("n_estimators 必须大于0")
    if max_samples < 2:
        raise ValueError("max_samples 必须至少为2")

    feature_dim = ML_FEATURE_DIM
    feature_means = IFOREST_FEATURE_MEANS.copy()
    feature_stds = IFOREST_FEATURE_STDS.copy()
    rng = np.random.default_rng(seed)

    if normal_data is None:
        normal_train_size = 1024
        normal_data = rng.normal(
            loc=feature_means,
            scale=feature_stds,
            size=(normal_train_size, feature_dim),
        ).astype(np.float32)
        normal_data = np.clip(normal_data, 0.0, 1.0)
    else:
        normal_data = np.asarray(normal_data, dtype=np.float32)
        if (normal_data.ndim != 2 or
                normal_data.shape[1] != feature_dim or
                normal_data.shape[0] < 2):
            raise ValueError("normal_data 必须满足形状 [N, 16]，且 N>=2")
        if not np.isfinite(normal_data).all():
            raise ValueError("normal_data 不能包含 NaN 或 Inf")
        normal_data = np.clip(normal_data, 0.0, 1.0)
        normal_train_size = normal_data.shape[0]

    effective_max_samples = min(max_samples, normal_train_size)
    max_depth = int(np.ceil(np.log2(effective_max_samples)))
    nodes_per_tree = (1 << (max_depth + 1)) - 1
    tree_param_len = n_estimators * nodes_per_tree

    safe_stds = np.where(feature_stds > 1e-6, feature_stds, 1.0)
    normalized_data = ((normal_data - feature_means) / safe_stds).astype(np.float32)

    tree_split_features = np.full(tree_param_len, -1, dtype=np.int32)
    tree_split_values = np.zeros(tree_param_len, dtype=np.float32)

    def make_leaf(tree_offset, node_idx, sample_count):
        absolute_idx = tree_offset + node_idx
        tree_split_features[absolute_idx] = -1
        tree_split_values[absolute_idx] = float(max(sample_count, 1))

    def build_tree(samples, tree_offset, node_idx, depth):
        if node_idx >= nodes_per_tree:
            return
        if depth >= max_depth or len(samples) <= 1:
            make_leaf(tree_offset, node_idx, len(samples))
            return

        feature_ranges = np.max(samples, axis=0) - np.min(samples, axis=0)
        valid_features = np.where(feature_ranges > 1e-6)[0]
        if len(valid_features) == 0:
            make_leaf(tree_offset, node_idx, len(samples))
            return

        split_feature = int(rng.choice(valid_features))
        values = samples[:, split_feature]
        min_value = float(np.min(values))
        max_value = float(np.max(values))
        split_value = float(rng.uniform(min_value, max_value))

        left_mask = values <= split_value
        right_mask = values > split_value
        left_samples = samples[left_mask]
        right_samples = samples[right_mask]

        if len(left_samples) == 0 or len(right_samples) == 0:
            split_value = float(np.median(values))
            left_mask = values <= split_value
            right_mask = values > split_value
            left_samples = samples[left_mask]
            right_samples = samples[right_mask]

        if len(left_samples) == 0 or len(right_samples) == 0:
            make_leaf(tree_offset, node_idx, len(samples))
            return

        absolute_idx = tree_offset + node_idx
        tree_split_features[absolute_idx] = split_feature
        tree_split_values[absolute_idx] = split_value

        build_tree(left_samples, tree_offset, node_idx * 2 + 1, depth + 1)
        build_tree(right_samples, tree_offset, node_idx * 2 + 2, depth + 1)

    for tree_idx in range(n_estimators):
        sample_indices = rng.choice(
            normal_train_size,
            size=effective_max_samples,
            replace=False,
        )
        tree_offset = tree_idx * nodes_per_tree
        build_tree(normalized_data[sample_indices], tree_offset, 0, 0)

    return {
        "n_estimators": n_estimators,
        "max_samples": effective_max_samples,
        "feature_means": feature_means,
        "feature_stds": feature_stds,
        "feature_dim": feature_dim,
        "tree_split_features": tree_split_features,
        "tree_split_values": tree_split_values,
        "tree_param_len": tree_param_len,
    }


if __name__ == "__main__":
    iforest = train_iforest_model()
    print(
        "IForest:",
        f"trees={iforest['n_estimators']}",
        f"max_samples={iforest['max_samples']}",
        f"tree_param_len={iforest['tree_param_len']}",
    )
