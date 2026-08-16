import math
import numpy as np

from normal_data_iforset import extract_normal_data
from attack_data_iforset import extract_attack_data
from feature_extractor import extract_dataset_features
from iforest_model import train_iforest_model
import numpy as np


# ============================================================
# 获取每棵树节点数量
# ============================================================

def get_nodes_per_tree(model):
    max_samples = model["max_samples"]
    max_depth = int(np.ceil(np.log2(max_samples)))
    return (1 << (max_depth + 1)) - 1


# ============================================================
# 单棵孤立树路径深度
# ============================================================

def isolation_tree_depth(sample, model, tree_index):
    split_features = model["tree_split_features"]
    split_values = model["tree_split_values"]

    nodes_per_tree = get_nodes_per_tree(model)
    tree_offset = tree_index * nodes_per_tree

    current_node = 0
    depth = 0.0

    while current_node < nodes_per_tree:
        absolute_index = tree_offset + current_node

        split_feature = int(split_features[absolute_index])
        split_value = float(split_values[absolute_index])

        # -1 表示叶节点，到达叶节点即结束
        if split_feature < 0:
            break

        if split_feature >= len(sample):
            break

        if sample[split_feature] <= split_value:
            current_node = current_node * 2 + 1
        else:
            current_node = current_node * 2 + 2

        depth += 1.0

    return depth


# ============================================================
# 单个样本 IForest 异常分数
#
# 完全按照项目书轻量化思想：
# 1. 每棵树计算路径深度
# 2. 路径深度归一化
# 3. 多棵树求平均
# 4. 1 - avg_depth
#
# 越接近1越异常
# ============================================================

def iforest_score_one(sample, model):
    n_estimators = model["n_estimators"]
    max_samples = model["max_samples"]

    # 项目树最大深度
    max_depth = int(np.ceil(np.log2(max_samples))) - 6

    if max_depth <= 0:
        return 0.5

    total_normalized_depth = 0.0

    for tree_index in range(n_estimators):
        depth = isolation_tree_depth(sample, model, tree_index) - 6

        # 路径深度归一化到0~1
        normalized_depth = depth / max_depth 
        normalized_depth = np.clip(normalized_depth, 0.0, 1.0)

        total_normalized_depth += normalized_depth

    avg_depth = total_normalized_depth / n_estimators

    # 反转平均路径深度
    iforest_score = 1.0 - avg_depth

    return float(np.clip(iforest_score, 0.0, 1.0))


# ============================================================
# 批量计算IForest异常分数
# ============================================================

# 增加惩罚项
def risk_penalty(x):
    penalty = 0.0

    # # x2：危险关键词
    # if x[2] >= 0.5:
    #     penalty += 0.30

    # # x5：系统危险命令
    # if x[5] >= 0.5:
    #     penalty += 0.25

    # # x6：危险工具
    # if x[6] >= 0.6:
    #     penalty += 0.25

    # # x7：Prompt攻击风险
    # if x[7] >= 0.5:
    #     penalty += 0.25

    # # x8：混淆/编码风险
    # if x[8] >= 0.33:
    #     penalty += 0.15

    # # x15：综合风险
    # if x[15] >= 0.2:
    #     penalty += 0.20

    # 防止惩罚过大
    return min(penalty, 0.7)

def iforest_score_samples(features, model):
    feature_means = model["feature_means"]
    feature_stds = model["feature_stds"]
    safe_stds = np.where(feature_stds > 1e-6, feature_stds, 1.0)

    features = np.asarray(features, dtype=np.float32)
    raw_features = np.clip(features, 0.0, 1.0)

    normalized_features = ((raw_features - feature_means) / safe_stds).astype(np.float32)

    scores = []

    for raw_sample, normalized_sample in zip(raw_features, normalized_features):

        # 原始IForest异常分
        score = iforest_score_one(normalized_sample, model)

        # 增加关键风险特征惩罚
        penalty = risk_penalty(raw_sample)

        # 最终异常分
        final_score = min(score + penalty, 1.0)

        scores.append(final_score)

    return np.asarray(scores, dtype=np.float32)

# ============================================================
# 模型评估
# ============================================================

def eval_model(model, normal_features, attack_features):
    print("\n========== IForest模型评估 ==========")

    normal_scores = iforest_score_samples(normal_features, model)
    attack_scores = iforest_score_samples(attack_features, model)

    print("\n---------- 正常样本异常分数 ----------")
    for i, score in enumerate(normal_scores):
        print(f"Normal {i + 1:03d}: {score:.6f}")

    print("\n---------- 攻击样本异常分数 ----------")
    for i, score in enumerate(attack_scores):
        print(f"Attack {i + 1:03d}: {score:.6f}")

    print("\n========== 正常样本统计 ==========")
    print("数量:", len(normal_scores))
    print("平均异常分:", f"{np.mean(normal_scores):.6f}")
    print("最小异常分:", f"{np.min(normal_scores):.6f}")
    print("最大异常分:", f"{np.max(normal_scores):.6f}")
    print("标准差:", f"{np.std(normal_scores):.6f}")

    print("\n========== 攻击样本统计 ==========")
    print("数量:", len(attack_scores))
    print("平均异常分:", f"{np.mean(attack_scores):.6f}")
    print("最小异常分:", f"{np.min(attack_scores):.6f}")
    print("最大异常分:", f"{np.max(attack_scores):.6f}")
    print("标准差:", f"{np.std(attack_scores):.6f}")

    normal_mean = np.mean(normal_scores)
    attack_mean = np.mean(attack_scores)
    score_gap = attack_mean - normal_mean

    print("\n========== 两类分离情况 ==========")
    print("正常平均异常分:", f"{normal_mean:.6f}")
    print("攻击平均异常分:", f"{attack_mean:.6f}")
    print("攻击平均分 - 正常平均分:", f"{score_gap:.6f}")

    if score_gap > 0:
        print("结果：攻击数据整体异常分高于正常数据。")
    else:
        print("警告：攻击数据整体异常分没有高于正常数据，需要检查特征设计或训练数据。")

    return normal_scores, attack_scores


# ============================================================
# main
# ============================================================

def main():
    print("========== IForest训练启动 ==========")

    # --------------------------------------------------------
    # 1. 读取正常数据
    # --------------------------------------------------------

    normal_data = extract_normal_data()
    print("正常样本数量:", len(normal_data))

    normal_features = extract_dataset_features(normal_data)
    print("正常样本特征矩阵:", normal_features.shape)

    print("\n第一条正常16维特征")
    print(normal_features[0])

    for i in range(1, len(normal_data)):
        print(normal_features[i-1])

    # --------------------------------------------------------
    # 2. 读取攻击数据
    # --------------------------------------------------------

    attack_data = extract_attack_data()
    print("\n异常样本数量:", len(attack_data))

    attack_features = extract_dataset_features(attack_data)
    print("异常样本特征矩阵:", attack_features.shape)

    print("\n第一条异常16维特征")
    print(attack_features[0])

    for i in range(1, len(attack_data)):
        print(attack_features[i-1])

    # --------------------------------------------------------
    # 3. 训练Isolation Forest
    # 注意：这里只使用正常数据
    # --------------------------------------------------------

    print("\n========== 开始训练 Isolation Forest ==========")

    model = train_iforest_model(
        normal_data=normal_features,
        n_estimators=64,
        max_samples=1024,
        seed=42
    )

    # --------------------------------------------------------
    # 4. 输出训练结果
    # --------------------------------------------------------

    print("\n========== 训练完成 ==========")
    print("n_estimators:", model["n_estimators"])
    print("max_samples:", model["max_samples"])
    print("feature_dim:", model["feature_dim"])
    print("tree_param_len:", model["tree_param_len"])
    print("tree_split_features:", model["tree_split_features"].shape)
    print("tree_split_values:", model["tree_split_values"].shape)

    # --------------------------------------------------------
    # 5. 模型评估
    # --------------------------------------------------------

    normal_scores, attack_scores = eval_model(
        model=model,
        normal_features=normal_features,
        attack_features=attack_features
    )


if __name__ == "__main__":
    main()