# ============================================================
# feature_extractor.py
#
# AgentTEE 离线训练专用 16维特征提取模块
#
# 依赖：
#   pip install numpy
#
# 不依赖：
#   SGX / APP.dll / PyQt5 / Flask / psutil / matplotlib
# ============================================================

import json
import math
import re
import time
import threading

from collections import defaultdict, deque

import numpy as np


# ============================================================
# 1. 行为类型
# ============================================================

ACTION_TYPES = {
    "系统调用": 1,
    "文件操作": 2,
    "网络行为": 3,
    "工具使用": 4,
    "对话提示词": 5,
}

ACTION_TYPE_NAMES = {
    v: k for k, v in ACTION_TYPES.items()
}


# ============================================================
# 2. 特征维度 / 会话长度
# ============================================================

ML_FEATURE_DIM = 16

SESSION_HISTORY_SIZE = 16


# ============================================================
# 3. 16维特征使用的关键词
#
# 与当前 AgentTEE.py 保持一致
# ============================================================

ML_KEYWORDS = {

    "system": [
        # 破坏性删除
        "rm -rf",
        "del /s",
        # 主机关机 / 重启
        "shutdown",
        "reboot",
        # 任务销毁
        "taskkill",
        "kill",
        # 任务执行
        "execve",
        "fork",
        # 高危权限修改
        "chmod 777",
        # powershell 编码执行
        "powershell -encodedcommand",
        "powershell -enc",
        # 明确攻击工具被放进命令执行
        "sqlmap",
        "metasploit",
        "mimikatz",
        "cmd.exe",
        # "reg add",
        # "schtasks",
        # "net user",
        # "net localgroup",
    ],

    "file": [
        # 账号相关内容
        "/etc/passwd",
        "/etc/shadow",
        # Windows SAM
        "system32\\config\\sam",
        # SSH 私钥
        "id_rsa",
        ".ssh",
        # 凭据文件 / 内容
        "credentials",
        "token",
        "api_key",
        "secret",
        "password",
        # 敏感性配置
        "openclaw.json",
        "删除",
        "格式化",
        "修改权限",
        "敏感文件",
    ],

    "network": [
        # "http://",
        # "https://",
        # "curl",
        # "wget",
        # "socket",
        # "connect",
        # 反弹shell
        "reverse shell",
        "反弹shell",
        "4444",
        # 端口扫描
        "端口扫描",
        "port_scan",
        "内网",
        "恶意域名",
        # 数据集中显式恶意链接标记
        "connect_malicious_ip",
        "malicious.example",
        # 明确的数据外泄语义
        "upload extracted credentials",
        "upload credentials",
        "send sensitive information",
    ],

    "tool": [
        # 完整工具名称
        "nmap",
        "sqlmap",
        "metasploit",
        "mimikatz",
        "networkscanner",
        # "powershell",
        # "cmd",
        # "bash",
        # "shell",
        # "exec",
        # 当前攻击数据中的明确攻击意图
        "leaked credentials",
        "credential extraction",
        "unauthorized exploitation",
    ],

    "prompt": [
        "管理员密码",
        "绕过安全检测",
        "泄露",
        "私密配置",
        "身份证号",
        "银行卡号",
        "执行系统命令",
        "删除日志",
        "生成执行指令",
    ],

    "obfuscation": [
        "\\u",
        "%2f",
        "%5c",
        "base64",
        "-enc",
        "frombase64string",
        "eval(",
        "exec(",
        "$(",
        "`",
        "&&",
        "||",
        ";",
    ],
}

# ============================================================
# 4. 构造 extract_features() 使用的 action_content
# ============================================================

def build_action_content(
    action_type,
    tool_name,
    params=None,
    content="",
    extra=None,
):
    """
    将一条原始 Agent 行为封装成 JSON 字符串。

    参数：
        action_type:
            系统调用 / 文件操作 / 网络行为 /
            工具使用 / 对话提示词

        tool_name:
            工具名称

        params:
            工具参数 dict

        content:
            用户原始 instruction / 行为描述

        extra:
            可选上下文，例如 trace

    返回：
        JSON字符串
    """

    data = {
        "actionType": action_type,
        "toolName": tool_name,
        "params": params if params is not None else {},
        "content": content,
    }

    if extra:
        data["extra"] = extra

    return json.dumps(
        data,
        ensure_ascii=False,
    )


# ============================================================
# 5. 纯特征提取器
# ============================================================

class FeatureExtractor:
    """
    AgentTEE 离线训练专用特征提取器。

    不初始化：
        SGX
        IForest
        LSTM
        DLL
        UI

    仅负责：
        原始行为 -> 16维 np.float32 特征
    """

    def __init__(self):

        # 用于第9~12维历史特征
        self.session_histories = defaultdict(
            lambda: deque(
                maxlen=SESSION_HISTORY_SIZE
            )
        )

        self.history_lock = threading.Lock()


    # ========================================================
    # JSON解析
    # ========================================================

    def _safe_json_loads(self, text):

        try:

            if isinstance(text, (dict, list)):
                return text

            if (
                isinstance(text, str)
                and text.strip().startswith(("{", "["))
            ):
                return json.loads(text)

        except Exception:
            pass

        return None


    # ========================================================
    # 将输入统一展开成字符串
    # ========================================================

    def _flatten_text(self, value):

        if value is None:
            return ""

        if isinstance(value, str):
            return value

        try:
            return json.dumps(
                value,
                ensure_ascii=False,
            )

        except Exception:
            return str(value)


    # ========================================================
    # 关键词统计
    # ========================================================

    def _count_keywords(self, text, words):

        text_low = text.lower()

        return sum(
            1
            for word in words
            if str(word).lower() in text_low
        )


    # ========================================================
    # 正则统计
    # ========================================================

    def _count_regex(self, pattern, text):

        try:

            return len(
                re.findall(
                    pattern,
                    text,
                    flags=re.IGNORECASE,
                )
            )

        except Exception:

            return 0


    # ========================================================
    # 字符信息熵
    # ========================================================

    def _text_entropy_score(self, text):

        if not text:
            return 0.0

        counter = {}

        for ch in text:

            counter[ch] = (
                counter.get(ch, 0) + 1
            )

        total = len(text)

        entropy = 0.0

        for count in counter.values():

            p = count / total

            entropy -= (
                p * math.log2(p)
            )

        # 与 AgentTEE.py 保持一致
        return min(
            entropy / 8.0,
            1.0,
        )


    # ========================================================
    # 提取 Trace 上下文
    # ========================================================

    def _extract_context_from_content(
        self,
        action_content,
    ):

        obj = self._safe_json_loads(
            action_content
        )

        trace = {}

        if isinstance(obj, dict):

            if isinstance(
                obj.get("trace"),
                dict,
            ):

                trace = obj.get("trace")

            elif (
                isinstance(
                    obj.get("extra"),
                    dict,
                )
                and isinstance(
                    obj["extra"].get("trace"),
                    dict,
                )
            ):

                trace = obj[
                    "extra"
                ].get("trace")

        session_id = (
            trace.get("session_id")
            or trace.get("sessionId")
            or "default-session"
        )

        run_id = (
            trace.get("run_id")
            or trace.get("runId")
            or "default-run"
        )

        parent_event_id = (
            trace.get(
                "parent_event_id"
            )
            or ""
        )

        trace_key = (
            trace.get("trace_key")
            or f"{session_id}:{run_id}"
        )

        return {
            "trace_key": trace_key,
            "session_id": session_id,
            "run_id": run_id,
            "parent_event_id":
                parent_event_id,
        }


    # ========================================================
    # 获取历史
    # ========================================================

    def _get_session_history_snapshot(
        self,
        trace_key,
    ):

        with self.history_lock:

            return list(
                self.session_histories.get(
                    trace_key,
                    [],
                )
            )


    # ========================================================
    # 如果以后要训练带历史的正常序列，可以调用
    # ========================================================

    def update_session_history(
        self,
        trace_key,
        action_type_name,
        score=0.0,
        blocked=False,
    ):

        item = {
            "time": time.time(),
            "action_type":
                action_type_name,
            "action_type_id":
                ACTION_TYPES.get(
                    action_type_name,
                    0,
                ),
            "score": float(score),
            "blocked": bool(blocked),
        }

        with self.history_lock:

            self.session_histories[
                trace_key
            ].append(item)


    # ========================================================
    # 清空历史
    # ========================================================

    def clear_history(self):

        with self.history_lock:

            self.session_histories.clear()


    # ========================================================
    # 核心函数：
    #
    # 原始行为 -> 16维特征
    # ========================================================

    def extract_features(
        self,
        action_type,
        action_type_name,
        action_content,
    ):

        # ====================================================
        # 1. 展平整个行为
        # ====================================================

        content_text = (
            self._flatten_text(
                action_content
            )
        )

        content_low = (
            content_text.lower()
        )


        # ====================================================
        # 2. 获取 Trace / History
        # ====================================================

        ctx = (
            self._extract_context_from_content(
                action_content
            )
        )

        trace_key = ctx[
            "trace_key"
        ]

        history = (
            self._get_session_history_snapshot(
                trace_key
            )
        )


        # ====================================================
        # 3. 内容长度
        # ====================================================

        content_len_score = min(
            len(content_text) / 512.0,
            1.0,
        )


        # ====================================================
        # 4. 六组关键词统计
        # ====================================================

        system_hits = (
            self._count_keywords(
                content_low,
                ML_KEYWORDS["system"],
            )
        )

        file_hits = (
            self._count_keywords(
                content_low,
                ML_KEYWORDS["file"],
            )
        )

        network_hits = (
            self._count_keywords(
                content_low,
                ML_KEYWORDS["network"],
            )
        )

        tool_hits = (
            self._count_keywords(
                content_low,
                ML_KEYWORDS["tool"],
            )
        )

        prompt_hits = (
            self._count_keywords(
                content_low,
                ML_KEYWORDS["prompt"],
            )
        )

        obfuscation_hits = (
            self._count_keywords(
                content_low,
                ML_KEYWORDS[
                    "obfuscation"
                ],
            )
        )


        # ====================================================
        # 5. IP / URL / Port
        # ====================================================

        ip_count = (
            self._count_regex(
                r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
                content_text,
            )
        )

        url_count = (
            self._count_regex(
                r"https?://[^\s\"']+",
                content_text,
            )
        )

        # ----------------------------------------------------
        # 注意：
        # 这里为了和你当前 AgentTEE.py 完全一致，
        # 暂时仍保留原始端口正则。
        #
        # 例如：
        # 14:00 中的 :00 也可能误判。
        #
        # 等模型流程跑通后建议单独修正。
        # ----------------------------------------------------

        port_count = (
            self._count_regex(
                r":\d{2,5}\b",
                content_text,
            )
        )


        # ====================================================
        # 6. 中间风险统计
        # ====================================================

        dangerous_total = (
            system_hits
            + file_hits
            + network_hits
            + tool_hits
            + prompt_hits
        )

        network_total = (
            network_hits
            + ip_count
            + url_count
            + port_count
        )


        # ====================================================
        # 7. 工具风险
        # ====================================================

        tool_risk = min(
            (
                tool_hits
                + system_hits
            ) / 5.0,
            1.0,
        )


        # ====================================================
        # 8. Prompt风险
        # ====================================================

        prompt_risk = min(
            prompt_hits / 4.0,
            1.0,
        )


        # ====================================================
        # 9. 混淆风险
        # ====================================================

        obfuscation_risk = min(
            obfuscation_hits / 3.0,
            1.0,
        )


        # ====================================================
        # 10. 历史行为
        # ====================================================

        hist_count = len(
            history
        )

        hist_block_count = sum(
            1
            for item in history
            if item.get("blocked")
        )

        hist_scores = [
            float(
                item.get(
                    "score",
                    0.0,
                )
            )
            for item in history
        ]

        hist_avg_score = (
            sum(hist_scores)
            / len(hist_scores)
            if hist_scores
            else 0.0
        )


        # ====================================================
        # 11. 最近60秒行为数量
        # ====================================================

        now = time.time()

        recent_60s_count = sum(
            1
            for item in history
            if (
                now
                - float(
                    item.get(
                        "time",
                        0,
                    )
                )
                <= 60
            )
        )


        # ====================================================
        # 12. 是否存在父事件
        # ====================================================

        parent_flag = (
            1.0
            if ctx.get(
                "parent_event_id"
            )
            else 0.0
        )


        # ====================================================
        # 13. 文本熵
        # ====================================================

        entropy_score = (
            self._text_entropy_score(
                content_text
            )
        )


        # ====================================================
        # 14. 综合风险
        # ====================================================

        composite_risk = min(

            0.25
            * min(
                dangerous_total / 6.0,
                1.0,
            )

            + 0.15
            * min(
                file_hits / 3.0,
                1.0,
            )

            + 0.15
            * min(
                network_total / 5.0,
                1.0,
            )

            + 0.15
            * tool_risk

            + 0.10
            * prompt_risk

            + 0.10
            * obfuscation_risk

            + 0.10
            * min(
                hist_block_count / 3.0,
                1.0,
            ),

            1.0,
        )


        # ====================================================
        # 15. 组装16维
        # ====================================================

        features = np.array(
            [

                # x0 行为类型
                min(
                    action_type / 5.0,
                    1.0,
                ),

                # x1 内容长度
                content_len_score,

                # x2 危险关键词
                min(
                    dangerous_total / 6.0,
                    1.0,
                ),

                # x3 文件风险
                min(
                    file_hits / 3.0,
                    1.0,
                ),

                # x4 网络风险
                min(
                    network_total / 5.0,
                    1.0,
                ),

                # x5 系统命令风险
                min(
                    system_hits / 4.0,
                    1.0,
                ),

                # x6 工具风险
                tool_risk,

                # x7 Prompt风险
                prompt_risk,

                # x8 混淆风险
                obfuscation_risk,

                # x9 历史事件数量
                min(
                    hist_count / 10.0,
                    1.0,
                ),

                # x10 历史异常数量
                min(
                    hist_block_count / 5.0,
                    1.0,
                ),

                # x11 历史平均风险
                min(
                    hist_avg_score,
                    1.0,
                ),

                # x12 最近60秒频率
                min(
                    recent_60s_count / 20.0,
                    1.0,
                ),

                # x13 是否存在父事件
                parent_flag,

                # x14 文本熵
                entropy_score,

                # x15 综合风险
                composite_risk,

            ],
            dtype=np.float32,
        )

        return features, trace_key


# ============================================================
# 6. 方便训练时直接传 dict 的辅助函数
# ============================================================

def extract_sample_features(
    extractor,
    sample,
):
    """
    输入：
        {
            "actionType": "...",
            "toolName": "...",
            "params": {...},
            "content": "..."
        }

    输出：
        np.ndarray shape=(16,)
    """

    action_type_name = (
        sample["actionType"]
    )

    action_type = (
        ACTION_TYPES[
            action_type_name
        ]
    )

    action_content = (
        build_action_content(
            action_type_name,
            sample.get(
                "toolName",
                "",
            ),
            sample.get(
                "params",
                {},
            ),
            sample.get(
                "content",
                "",
            ),
        )
    )

    features, trace_key = (
        extractor.extract_features(
            action_type,
            action_type_name,
            action_content,
        )
    )

    return features


# ============================================================
# 7. 整个数据集直接转换成 [N,16]
# ============================================================

def extract_dataset_features(
    normal_data,
):
    """
    normal_data:
        list[dict]

    返回：
        np.ndarray
        shape = [N,16]
    """

    extractor = FeatureExtractor()

    feature_list = []

    for sample in normal_data:

        features = (
            extract_sample_features(
                extractor,
                sample,
            )
        )

        feature_list.append(
            features
        )

    if not feature_list:

        return np.empty(
            (0, ML_FEATURE_DIM),
            dtype=np.float32,
        )

    return np.vstack(
        feature_list
    ).astype(
        np.float32
    )


# ============================================================
# 8. 单文件测试
# ============================================================

if __name__ == "__main__":

    test_sample = {

        "actionType":
            "网络行为",

        "toolName":
            "HTTPGet",

        "params": {
            "url":
                "https://example.com/status"
        },

        "content":
            "Check the public service status page.",
    }

    extractor = FeatureExtractor()

    features = (
        extract_sample_features(
            extractor,
            test_sample,
        )
    )

    print(
        "feature shape:",
        features.shape,
    )

    print(
        "features:",
        features,
    )