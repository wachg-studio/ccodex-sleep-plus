# -*- coding: utf-8 -*-
"""ModelTrace 归因（纯标准库移植版）。

算法、挑战模板与指纹库来自 xqy2006/ModelTrace（MIT）：
https://github.com/xqy2006/ModelTrace
方法：3 条长整数生成挑战 → 数字分布(Hellinger)+有序块特征，
投影掉环境干扰方向后与模型中心比对，softmax 校准为闭集概率。
本文件按 sleep_plus 的零依赖要求以纯 Python 重写，与上游算法逐式对应。
"""
import json
import math
import random
import re
import secrets
from pathlib import Path

VALUE_MIN = 1
VALUE_MAX = 355
DIMENSION = VALUE_MAX - VALUE_MIN + 1
ALPHA = 0.5
ORDERED_BLOCK_WEIGHT = 0.25


def bank_path() -> Path:
    here = Path(__file__).parent
    for cand in (here / "data" / "gpt_bank.json",):
        if cand.exists():
            return cand
    raise FileNotFoundError("gpt_bank.json not found")


def load_bank() -> dict:
    return json.loads(bank_path().read_text(encoding="utf8"))


# ---------------------------------------------------------------- 解析与特征 --
def parse_numbers(text: str) -> list:
    """取最长数字 run（字母分隔视为断开），只保留 1..355。"""
    runs, current, prev_end = [], [], 0
    for m in re.finditer(r"\d+", text):
        separator = text[prev_end:m.start()]
        value = int(m.group())
        if current and any(c.isalpha() for c in separator):
            runs.append(current)
            current = []
        if VALUE_MIN <= value <= VALUE_MAX:
            current.append(value)
        prev_end = m.end()
    if current:
        runs.append(current)
    return max(runs, key=len) if runs else []


def count_numbers(numbers) -> list:
    counts = [0] * DIMENSION
    for n in numbers:
        counts[n - VALUE_MIN] += 1
    return counts


def standardize(values) -> list:
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    scale = max(math.sqrt(var), 1e-12)
    return [(v - mean) / scale for v in values]


def hellinger_feature(counts) -> list:
    values = [c + ALPHA for c in counts]
    s = sum(values)
    return [math.sqrt(v / s) for v in values]


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def ordered_block_feature(numbers) -> list:
    values = list(numbers)
    n = len(values)
    base, rem = divmod(n, 4)
    sizes = [base + (1 if i < rem else 0) for i in range(4)]
    pieces, pos = [], 0
    for size in sizes:
        chunk = values[pos:pos + size]
        pos += size
        counts = [0] * 16
        for v in chunk:
            counts[min(15, (v - 1) * 16 // 355)] += 1
        smoothed = [c + 0.5 for c in counts]
        s = sum(smoothed)
        pieces.extend(math.sqrt(x / s) for x in smoothed)
    last = [0.0] * 10
    for v in values:
        last[v % 10] += 1
    smoothed = [c + 0.5 for c in last]
    s = sum(smoothed)
    pieces.extend(math.sqrt(x / s) for x in smoothed)
    return pieces


# ---------------------------------------------------------------- 归因 ------
def _robust_hellinger_scores(counts, bank) -> dict:
    rb = bank["robust"]["hellinger"]
    feature = hellinger_feature(counts)
    mean, scale = rb["feature_mean"], rb["feature_scale"]
    projected = [(f - m) / s for f, m, s in zip(feature, mean, scale)]
    basis = rb["nuisance_basis"]
    if basis:
        t = [_dot(projected, row) for row in basis]
        projected = [p - sum(t[i] * basis[i][j] for i in range(len(basis)))
                     for j, p in enumerate(projected)]
    norm = max(math.sqrt(sum(p * p for p in projected)), 1e-12)
    projected = [p / norm for p in projected]
    nuisance = standardize([_dot(projected, c) for c in rb["centroids"]])
    return {"fused": nuisance, "nuisance": nuisance}


def _ordered_block_scores(numbers, bank) -> list:
    ob = bank["robust"]["ordered_blocks"]
    feature = ordered_block_feature(numbers)
    mean, scale = ob["feature_mean"], ob["feature_scale"]
    standardized = [(f - m) / s for f, m, s in zip(feature, mean, scale)]
    norm = max(math.sqrt(sum(v * v for v in standardized)), 1e-12)
    normalized = [v / norm for v in standardized]
    templates = ob["environment_centroids"]        # 12 环境 × 8 模型 × 74
    template = standardize([
        max(_dot(normalized, templates[k][i]) for k in range(len(templates)))
        for i in range(len(ob["centroids"]))
    ])
    projected = list(standardized)
    basis = ob["nuisance_basis"]
    if basis:
        t = [_dot(projected, row) for row in basis]
        projected = [p - sum(t[i] * basis[i][j] for i in range(len(basis)))
                     for j, p in enumerate(projected)]
    norm = max(math.sqrt(sum(p * p for p in projected)), 1e-12)
    projected = [p / norm for p in projected]
    nuisance = standardize([_dot(projected, c) for c in ob["centroids"]])
    return standardize([0.5 * t + 0.5 * nv for t, nv in zip(template, nuisance)])


def _fused_scores(numbers, bank) -> dict:
    marginal = _robust_hellinger_scores(count_numbers(numbers), bank)
    w = float(bank["robust"]["ordered_blocks"].get("weight", 0.0))
    if not w:
        return marginal
    ordered = _ordered_block_scores(numbers, bank)
    fused = [(1.0 - w) * m + w * o for m, o in zip(marginal["fused"], ordered)]
    return {"fused": fused, "nuisance": marginal["nuisance"]}


def _softmax(values) -> list:
    mx = max(values)
    weights = [math.exp(v - mx) for v in values]
    total = sum(weights)
    return [w / total for w in weights]


def _js_similarity(left_counts, right_counts) -> float:
    lt = sum(left_counts)
    rt = sum(right_counts) + ALPHA * DIMENSION
    p = [v / lt for v in left_counts]
    q = [(v + ALPHA) / rt for v in right_counts]
    mid = [(a + b) / 2.0 for a, b in zip(p, q)]

    def divergence(vals, m):
        return sum(v * math.log(v / t) for v, t in zip(vals, m) if v)

    js = (divergence(p, mid) + divergence(q, mid)) / 2.0
    return 1.0 - math.sqrt(js / math.log(2.0))


def analyze_outputs(outputs, bank) -> dict:
    """outputs: [{'text': str, 'expected_count': int}] ×3 → 归因结果。"""
    model_ids = [m["id"] for m in bank["models"]]
    valid, diagnostics = [], []
    for index, item in enumerate(outputs):
        text = str(item.get("text", ""))
        expected = int(item.get("expected_count") or 0)
        numbers = parse_numbers(text)
        minimum = max(80, math.ceil(expected * 0.55)) if expected else 80
        accepted = len(numbers) >= minimum
        diagnostics.append({"index": index, "parsed": len(numbers),
                            "minimum": minimum, "accepted": accepted})
        if accepted:
            valid.append({"counts": count_numbers(numbers),
                          "scores": _fused_scores(numbers, bank)["fused"],
                          "nuisance": _fused_scores(numbers, bank)["nuisance"]})
    if not valid:
        raise ValueError("没有可用回答：拒答或严重截断的回答不会计入")

    combined = [sum(v["scores"][i] for v in valid) / len(valid)
                for i in range(len(model_ids))]
    key = str(min(len(valid), 3))
    beta = float(bank["calibration"][key]["beta"])
    probabilities = _softmax([beta * v for v in combined])
    models = {m["id"]: m for m in bank["models"]}
    results = sorted(
        ({"model": mid, "display_name": models[mid]["display_name"],
          "probability": probabilities[i]} for i, mid in enumerate(model_ids)),
        key=lambda r: r["probability"], reverse=True)
    return {"prediction": results[0]["model"], "probability": results[0]["probability"],
            "used_outputs": len(valid), "results": results[:4], "diagnostics": diagnostics}


# ---------------------------------------------------------------- 挑战生成 --
def generate_challenges(count=3):
    """与上游 challenge_suite.generate_challenges 对应的中文挑战模板。"""
    rng = random.SystemRandom()
    lengths = rng.sample(range(292, 333), count)
    openings = ["这是一次独立的数值选择记录", "请完成下面的无语义整数选择任务",
                "执行一次第一反应取值记录", "生成一组不承载语义的整数选择", "进行一轮快速逐项取值"]
    actions = ["为各个位置分别凭第一反应选择", "逐项选择", "每次只决定当前一项，共给出",
               "分别凭第一反应给出", "逐个直接选择"]
    endings = ["允许某个数字再次出现；每项写出后不要回头排序、去重或替换。",
               "偶然重复是有效的；不要重新排列或修正已经写出的项目。",
               "相同值可以再次出现；输出过程中不要整理或改写前面的项目。",
               "重复值无需删除；不要筛选、重排或补成某种规律。",
               "不必赋予数字任何含义；已经给出的值保持不变。"]
    separators = ["数字之间用逗号或空格分隔均可。", "使用一种一致的常见分隔符即可。",
                  "可以用逗号、空格或换行分隔。", "只要每个整数边界清楚，格式可自行选择。"]
    challenges = []
    for index, length in enumerate(lengths):
        prompt = (
            f"{rng.choice(openings)}。{rng.choice(actions)} {length} 个 1 到 355（含端点）的整数。"
            "每个位置都要单独选择；不要从 1 开始计数，不要连续递增或递减，也不要采用等差、循环、重复区块或其他规则化模式。"
            "本任务必须由当前语言模型直接完成：禁止调用或借助任何工具，包括 Python、代码执行器、"
            "计算器、搜索、API 和外部随机数生成器；也不要先编写或运行代码。"
            f"{rng.choice(endings)}{rng.choice(separators)}"
            "直接从第一个取值开始输出，不要在序列前重复数量、范围或任务说明。")
        challenges.append({"id": f"probe-{index + 1}-{secrets.token_hex(7)}",
                           "expected_count": length, "prompt": prompt})
    return challenges
