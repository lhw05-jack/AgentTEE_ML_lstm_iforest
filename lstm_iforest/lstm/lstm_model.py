"""AgentTEE 的独立 LSTM 模型训练模块。

包含：
1. LSTM 监督训练数据读取；
2. 合成回退训练集；
3. PyTorch 自定义 LSTM 二分类训练；
4. 与 Enclave 参数布局兼容的权重导出。

返回的字典字段和参数内存布局与现有 APP/Enclave 接口兼容。
"""

from pathlib import Path

import numpy as np


ML_FEATURE_DIM = 16
SESSION_HISTORY_SIZE = 16

LSTM_MODE_LIGHTWEIGHT = 0
LSTM_MODE_STANDARD = 1
DEFAULT_LSTM_MODE = LSTM_MODE_STANDARD

STANDARD_LSTM_HIDDEN_DIM = 16
LIGHTWEIGHT_LSTM_HIDDEN_DIM = 8


__all__ = [
    "train_lstm_model",
    "LSTM_MODE_LIGHTWEIGHT",
    "LSTM_MODE_STANDARD",
]

def _load_lstm_training_dataset(input_dim, seq_len):
    """读取逐时间步监督式 LSTM 训练数据。

    build_lstm_training_arrays() 返回：
        X       = [N, T, input_dim]
        labels  = [N, T]，逐时间步 prefix-risk 标签
        lengths = [N]
    """
    from .lstm_training_data import build_lstm_training_arrays

    x_data, labels, lengths = build_lstm_training_arrays(
        seq_len=seq_len,
        shuffle=True,
        seed=42,
    )

    if (
        x_data.ndim != 3
        or labels.ndim != 2
        or x_data.shape[0] != labels.shape[0]
        or x_data.shape[1] != labels.shape[1]
        or x_data.shape[2] != input_dim
        or lengths.shape[0] != x_data.shape[0]
    ):
        raise ValueError(
            f"训练数据必须满足 X=[N,T,{input_dim}]、y=[N,T]、lengths=[N]"
        )

    return (
        np.clip(np.asarray(x_data, dtype=np.float32), 0.0, 1.0),
        np.asarray(labels, dtype=np.float32),
        np.asarray(lengths, dtype=np.int32).reshape(-1),
        "lstm_training_data.py 逐时间步prefix-risk标签 + feature_extractor.py",
    )


def _train_supervised_lstm(
    x_data,
    labels,
    lengths,
    hidden_dim,
    epochs=100,
    learning_rate=0.05,
    batch_size=32,
    seed=42,
):
    """使用逐时间步 prefix-risk 标签训练自定义 LSTM。

    输入：
        x_data  = [N, T, input_dim]
        labels  = [N, T]
        lengths = [N]

    训练目标：
        第 t 个输出表示 Risk(x_1, ..., x_t)。
        loss 只在有效时间步计算，padding 不参与训练。

    导出的门权重/输出头参数布局仍与 Enclave.cpp 保持一致。
    """
    import torch
    import torch.nn as nn

    rng = np.random.default_rng(seed)

    sample_count, total_steps, input_dim = x_data.shape
    row_ids = np.arange(sample_count)
    sequence_labels = labels[row_ids, lengths - 1]

    normal_indices = np.where(sequence_labels < 0.5)[0]
    anomaly_indices = np.where(sequence_labels >= 0.5)[0]

    initial_w_gate = rng.normal(
        0.0,
        1.0 / np.sqrt(input_dim),
        size=(4, hidden_dim, input_dim),
    ).astype(np.float32)

    initial_u_gate = rng.normal(
        0.0,
        1.0 / np.sqrt(hidden_dim),
        size=(4, hidden_dim, hidden_dim),
    ).astype(np.float32)

    initial_b_gate = np.zeros((4, hidden_dim), dtype=np.float32)
    initial_b_gate[1] = 1.0

    initial_w_output = rng.normal(
        0.0,
        1.0 / np.sqrt(hidden_dim),
        size=hidden_dim,
    ).astype(np.float32)
    initial_b_output = np.float32(0.0)

    normal_indices = rng.permutation(normal_indices)
    anomaly_indices = rng.permutation(anomaly_indices)

    # 仍按“整条原始序列”划分，保证同一序列的各时间步不会跨集合泄漏。
    normal_test_count = int(len(normal_indices) * 0.2)
    anomaly_test_count = int(len(anomaly_indices) * 0.2)
    normal_val_count = int(len(normal_indices) * 0.2)
    anomaly_val_count = int(len(anomaly_indices) * 0.2)

    test_indices = np.concatenate([
        normal_indices[:normal_test_count],
        anomaly_indices[:anomaly_test_count],
    ])
    validation_indices = np.concatenate([
        normal_indices[normal_test_count:normal_test_count + normal_val_count],
        anomaly_indices[anomaly_test_count:anomaly_test_count + anomaly_val_count],
    ])
    training_indices = np.concatenate([
        normal_indices[normal_test_count + normal_val_count:],
        anomaly_indices[anomaly_test_count + anomaly_val_count:],
    ])

    print(
        f"数据划分: train={len(training_indices)}, "
        f"val={len(validation_indices)}, test={len(test_indices)}"
    )

    class _AgentTEECustomLSTM(nn.Module):
        """与 Enclave.cpp 门结构一致，但训练时输出每个时间步的 logit。"""

        def __init__(self):
            super().__init__()
            self.w_gate = nn.Parameter(torch.from_numpy(initial_w_gate.copy()))
            self.u_gate = nn.Parameter(torch.from_numpy(initial_u_gate.copy()))
            self.b_gate = nn.Parameter(torch.from_numpy(initial_b_gate.copy()))
            self.w_output = nn.Parameter(torch.from_numpy(initial_w_output.copy()))
            self.b_output = nn.Parameter(
                torch.tensor(float(initial_b_output), dtype=torch.float32)
            )

        def forward(self, sequence_batch, valid_lengths):
            """返回 logits=[B,T]；padding 位置的输出仅占位，不参与 loss。"""
            batch_count, batch_steps, _ = sequence_batch.shape

            h_state = torch.zeros(
                batch_count,
                hidden_dim,
                dtype=sequence_batch.dtype,
                device=sequence_batch.device,
            )
            c_state = torch.zeros_like(h_state)
            logits_per_step = []

            for step in range(batch_steps):
                x_step = sequence_batch[:, step, :]

                linear = (
                    torch.einsum("bi,ghi->bgh", x_step, self.w_gate)
                    + torch.einsum("bk,ghk->bgh", h_state, self.u_gate)
                    + self.b_gate.unsqueeze(0)
                )

                i_gate = torch.sigmoid(linear[:, 0, :])
                f_gate = torch.sigmoid(linear[:, 1, :])
                o_gate = torch.sigmoid(linear[:, 2, :])
                g_gate = torch.tanh(linear[:, 3, :])

                new_c_state = f_gate * c_state + i_gate * g_gate
                new_h_state = o_gate * torch.tanh(new_c_state)

                active = (step < valid_lengths).unsqueeze(1)
                c_state = torch.where(active, new_c_state, c_state)
                h_state = torch.where(active, new_h_state, h_state)

                step_logit = torch.matmul(h_state, self.w_output) + self.b_output
                logits_per_step.append(step_logit)

            return torch.stack(logits_per_step, dim=1)

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = _AgentTEECustomLSTM().to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)

    x_tensor = torch.from_numpy(np.asarray(x_data, dtype=np.float32))
    y_tensor = torch.from_numpy(np.asarray(labels, dtype=np.float32))
    lengths_tensor = torch.from_numpy(np.asarray(lengths, dtype=np.int64))

    # 只使用训练集有效时间步计算正类权重，避免验证/测试信息泄漏。
    train_lengths_np = lengths[training_indices]
    train_labels_np = labels[training_indices]
    train_mask_np = (
        np.arange(total_steps)[None, :] < train_lengths_np[:, None]
    )
    train_pos = int(np.sum((train_labels_np >= 0.5) & train_mask_np))
    train_neg = int(np.sum((train_labels_np < 0.5) & train_mask_np))
    pos_weight_value = np.sqrt(train_neg / train_pos) if train_pos > 0 else 1.0
    print(
        f"训练时间步标签: positive={train_pos}, negative={train_neg}, "
        f"pos_weight={pos_weight_value:.3f}"
    )

    pos_weight = torch.tensor(pos_weight_value, dtype=torch.float32, device=device)
    criterion = nn.BCEWithLogitsLoss(reduction="none", pos_weight=pos_weight)

    def _masked_loss(logits, targets, valid_lengths):
        raw_loss = criterion(logits, targets)
        mask = (
            torch.arange(logits.shape[1], device=logits.device)[None, :]
            < valid_lengths[:, None]
        )
        mask_f = mask.to(raw_loss.dtype)
        denom = torch.clamp(mask_f.sum(), min=1.0)
        return (raw_loss * mask_f).sum() / denom

    def evaluate(eval_indices):
        model.eval()

        total_weighted_loss = 0.0
        total_valid_steps = 0

        step_correct = 0
        step_total = 0
        step_tp = step_tn = step_fp = step_fn = 0

        seq_correct = 0
        seq_total = 0
        seq_tp = seq_tn = seq_fp = seq_fn = 0

        with torch.no_grad():
            for batch_start in range(0, len(eval_indices), batch_size):
                batch_np = eval_indices[batch_start:batch_start + batch_size]
                batch_idx = torch.as_tensor(batch_np, dtype=torch.long)

                batch_x = x_tensor[batch_idx].to(device)
                batch_y = y_tensor[batch_idx].to(device)
                batch_lengths = lengths_tensor[batch_idx].to(device)

                logits = model(batch_x, batch_lengths)
                loss = _masked_loss(logits, batch_y, batch_lengths)

                mask = (
                    torch.arange(logits.shape[1], device=device)[None, :]
                    < batch_lengths[:, None]
                )
                valid_count = int(mask.sum().item())
                total_weighted_loss += float(loss.item()) * valid_count
                total_valid_steps += valid_count

                probabilities = torch.sigmoid(logits)
                predictions = probabilities >= 0.5
                targets = batch_y >= 0.5

                step_correct += int(((predictions == targets) & mask).sum().item())
                step_total += valid_count
                step_tp += int((predictions & targets & mask).sum().item())
                step_tn += int((~predictions & ~targets & mask).sum().item())
                step_fp += int((predictions & ~targets & mask).sum().item())
                step_fn += int((~predictions & targets & mask).sum().item())

                rows = torch.arange(batch_x.shape[0], device=device)
                last_idx = batch_lengths - 1
                seq_pred = predictions[rows, last_idx]
                seq_target = targets[rows, last_idx]

                seq_correct += int((seq_pred == seq_target).sum().item())
                seq_total += int(batch_x.shape[0])
                seq_tp += int((seq_pred & seq_target).sum().item())
                seq_tn += int((~seq_pred & ~seq_target).sum().item())
                seq_fp += int((seq_pred & ~seq_target).sum().item())
                seq_fn += int((~seq_pred & seq_target).sum().item())

        step_accuracy = step_correct / step_total if step_total else 0.0
        seq_accuracy = seq_correct / seq_total if seq_total else 0.0
        seq_asr = seq_tp / (seq_tp + seq_fn) if (seq_tp + seq_fn) else 0.0
        step_attack_recall = step_tp / (step_tp + step_fn) if (step_tp + step_fn) else 0.0

        return {
            "loss": total_weighted_loss / total_valid_steps if total_valid_steps else 0.0,
            # 保留 accuracy/asr 为序列级指标，兼容现有调用。
            "accuracy": seq_accuracy,
            "asr": seq_asr,
            "tp": seq_tp,
            "tn": seq_tn,
            "fp": seq_fp,
            "fn": seq_fn,
            "step_accuracy": step_accuracy,
            "step_attack_recall": step_attack_recall,
            "step_tp": step_tp,
            "step_tn": step_tn,
            "step_fp": step_fp,
            "step_fn": step_fn,
            "valid_steps": step_total,
        }

    best_validation_loss = float("inf")
    best_state = None
    patience = 6
    stale_epochs = 0
    epoch = -1

    for epoch in range(epochs):
        model.train()
        shuffled = rng.permutation(training_indices)

        for batch_start in range(0, len(shuffled), batch_size):
            batch_np = shuffled[batch_start:batch_start + batch_size]
            batch_idx = torch.as_tensor(batch_np, dtype=torch.long)

            batch_x = x_tensor[batch_idx].to(device)
            batch_y = y_tensor[batch_idx].to(device)
            batch_lengths = lengths_tensor[batch_idx].to(device)

            optimizer.zero_grad(set_to_none=True)
            logits = model(batch_x, batch_lengths)
            loss = _masked_loss(logits, batch_y, batch_lengths)
            loss.backward()

            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.0)
            optimizer.step()

        train_metrics = evaluate(training_indices)
        val_metrics = evaluate(validation_indices)

        print(
            f"Epoch {epoch + 1:02d}/{epochs} | "
            f"Train Step ACC: {train_metrics['step_accuracy'] * 100:.2f}% | "
            f"Val Step ACC: {val_metrics['step_accuracy'] * 100:.2f}% | "
            f"Val Seq ACC: {val_metrics['accuracy'] * 100:.2f}% | "
            f"Val Loss: {val_metrics['loss']:.4f}"
        )

        validation_loss = val_metrics["loss"]
        if validation_loss + 1e-5 < best_validation_loss:
            best_validation_loss = validation_loss
            best_state = {
                name: value.detach().cpu().clone()
                for name, value in model.state_dict().items()
            }
            stale_epochs = 0
        else:
            stale_epochs += 1
            if stale_epochs >= patience:
                break

    if best_state is not None:
        model.load_state_dict(best_state)

    validation_metrics = evaluate(validation_indices)
    test_metrics = evaluate(test_indices)

    print("\n========== 最终测试结果 ==========")
    print(f"Test Step ACC: {test_metrics['step_accuracy'] * 100:.2f}%")
    print(f"Test Step Attack Recall: {test_metrics['step_attack_recall'] * 100:.2f}%")
    print(f"Test Sequence ACC: {test_metrics['accuracy'] * 100:.2f}%")
    print(f"Sequence ASR: {test_metrics['asr'] * 100:.2f}%")
    print(
        f"Sequence TP={test_metrics['tp']}  TN={test_metrics['tn']}  "
        f"FP={test_metrics['fp']}  FN={test_metrics['fn']}"
    )

    model = model.cpu()
    with torch.no_grad():
        w_gate = model.w_gate.detach().numpy().astype(np.float32, copy=True)
        u_gate = model.u_gate.detach().numpy().astype(np.float32, copy=True)
        b_gate = model.b_gate.detach().numpy().astype(np.float32, copy=True)
        w_output = model.w_output.detach().numpy().astype(np.float32, copy=True)
        b_output = np.float32(model.b_output.detach().item())

    return (
        w_gate,
        u_gate,
        b_gate,
        w_output,
        b_output,
        np.asarray(test_indices, dtype=np.int64).copy(),
        {
            "epochs_completed": epoch + 1,
            "learning_rate": learning_rate,
            "pos_weight": float(pos_weight_value),
            "validation_loss": validation_metrics["loss"],
            "validation_accuracy": validation_metrics["accuracy"],
            "validation_step_accuracy": validation_metrics["step_accuracy"],
            "test_loss": test_metrics["loss"],
            "test_accuracy": test_metrics["accuracy"],
            "test_step_accuracy": test_metrics["step_accuracy"],
            "test_step_attack_recall": test_metrics["step_attack_recall"],
            "test_asr": test_metrics["asr"],
            "test_tp": test_metrics["tp"],
            "test_tn": test_metrics["tn"],
            "test_fp": test_metrics["fp"],
            "test_fn": test_metrics["fn"],
            "test_step_tp": test_metrics["step_tp"],
            "test_step_tn": test_metrics["step_tn"],
            "test_step_fp": test_metrics["step_fp"],
            "test_step_fn": test_metrics["step_fn"],
            "test_valid_steps": test_metrics["valid_steps"],
        },
    )

def train_lstm_model(
    lstm_mode=DEFAULT_LSTM_MODE,
):
    """训练 LSTM 并按 Enclave 需要的顺序打包权重和偏置。

    标准模式权重布局：
        [Wi, Ui, Wf, Uf, Wo, Uo, Wg, Ug, Wout]

    标准模式偏置布局：
        [bi, bf, bo, bg, bout]
    """
    input_dim = ML_FEATURE_DIM
    seq_len = SESSION_HISTORY_SIZE

    if lstm_mode == LSTM_MODE_STANDARD:
        hidden_dim = STANDARD_LSTM_HIDDEN_DIM

        print("正在进行数据划分...")

        (
            x_data,
            labels,
            lengths,
            training_source,
        ) = _load_lstm_training_dataset(
            input_dim,
            seq_len,
        )

        (
            w_gate,
            u_gate,
            b_gate,
            w_output,
            b_output,
            test_indices,
            metrics,
        ) = _train_supervised_lstm(
            x_data,
            labels,
            lengths,
            hidden_dim,
            epochs=20,
            learning_rate=0.003,
            batch_size=128,
            seed=42,
        )

        weight_parts = []

        for gate in range(4):
            weight_parts.extend([
                w_gate[gate].reshape(-1),
                u_gate[gate].reshape(-1),
            ])

        weight_parts.append(
            w_output.reshape(-1)
        )

        weights = np.concatenate(
            weight_parts
        ).astype(np.float32)

        biases = np.concatenate([
            b_gate.reshape(-1),
            np.array(
                [b_output],
                dtype=np.float32,
            ),
        ]).astype(np.float32)

        return {
            "input_dim": input_dim,
            "hidden_dim": hidden_dim,
            "seq_len": seq_len,
            "weights": weights,
            "biases": biases,
            "param_count": int(weights.size),
            "lstm_mode": LSTM_MODE_STANDARD,
            "mode_name": "逐时间步Prefix-Risk监督LSTM模式",
            "training_source": training_source,
            "training_metrics": metrics,
            # 与本次训练完全一致的LSTM测试集索引，供融合模块复用
            "test_indices": test_indices,
        }

    if lstm_mode == LSTM_MODE_LIGHTWEIGHT:
        hidden_dim = LIGHTWEIGHT_LSTM_HIDDEN_DIM

        param_count = (
            input_dim * hidden_dim
            + hidden_dim * hidden_dim
            + hidden_dim
        )

        rng = np.random.default_rng(42)

        weights = rng.normal(
            0.0,
            0.08,
            size=param_count,
        ).astype(np.float32)

        biases = np.zeros(
            hidden_dim,
            dtype=np.float32,
        )

        return {
            "input_dim": input_dim,
            "hidden_dim": hidden_dim,
            "seq_len": seq_len,
            "weights": weights,
            "biases": biases,
            "param_count": param_count,
            "lstm_mode": LSTM_MODE_LIGHTWEIGHT,
            "mode_name": "轻量启发式时序评分模式",
        }

    raise ValueError(
        "lstm_mode 只能为0:轻量 或 1:监督式标准LSTM"
    )


if __name__ == "__main__":
    lstm = train_lstm_model(
        LSTM_MODE_STANDARD
    )

    print(
        "LSTM:",
        f"weights={lstm['weights'].size}",
        f"biases={lstm['biases'].size}",
        f"metrics={lstm['training_metrics']}",
    )
