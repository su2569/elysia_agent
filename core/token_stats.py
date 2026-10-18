"""TokenStats - Token 消耗统计（本地持久化）

设计原则：
1. 零依赖：只写 JSONL 追加文件，不需要数据库，也不进内存常驻
2. 失败隔离：任何异常都吞掉并写日志，绝不影响 LLM 主链路
3. 可回溯：逐次记录（时间/模型/来源/会话/prompt/completion/total），
   汇总时再按维度聚合，后续想加维度不用改写入格式
4. 有界增长：超过 max_bytes 自动滚动一次（保留最近一半）

数据文件：<数据目录>/token_stats.jsonl
"""
import json
import os
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from ..utils.logger import get_logger

logger = get_logger("TokenStats")

_LOCK = threading.Lock()
_FILE_NAME = "token_stats.jsonl"
_MAX_BYTES = 8 * 1024 * 1024  # 8MB 滚动
_RECENT_KEEP = 500           # 前端展示的最近调用条数上限


_DATA_DIR: Optional[str] = None


def configure(data_dir: str) -> None:
    """由启动流程注入配置里的 data_dir（缺失时自动回退到项目 data 目录）"""
    global _DATA_DIR
    try:
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            _DATA_DIR = str(data_dir)
    except Exception as e:
        logger.warning(f"token_stats 配置数据目录失败: {e}")


def _data_dir() -> str:
    if _DATA_DIR:
        return _DATA_DIR
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    d = os.path.join(here, "data")
    os.makedirs(d, exist_ok=True)
    return d


def _file_path() -> str:
    return os.path.join(_data_dir(), _FILE_NAME)


def _roll_if_needed(path: str) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > _MAX_BYTES:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            keep = lines[len(lines) // 2:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(keep)
            logger.info(f"token_stats 已滚动，保留 {len(keep)} 条")
    except Exception as e:
        logger.warning(f"token_stats 滚动失败: {e}")


def _num(v: Any) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def record(
    model: str = "",
    usage: Optional[Dict[str, Any]] = None,
    source: str = "llm",
    session: str = "",
    provider: str = "",
) -> None:
    """记录一次 token 消耗。usage 兼容 OpenAI 格式 / 扁平字典。"""
    try:
        usage = usage or {}
        prompt = _num(usage.get("prompt_tokens") or usage.get("input_tokens"))
        completion = _num(usage.get("completion_tokens") or usage.get("output_tokens"))
        total = _num(usage.get("total_tokens")) or (prompt + completion)
        if total <= 0:
            return  # 上游没给用量就不落盘，避免污染统计
        row = {
            "ts": time.time(),
            "model": model or "unknown",
            "provider": provider or "",
            "source": source,
            "session": session or "",
            "prompt": prompt,
            "completion": completion,
            "total": total,
        }
        path = _file_path()
        with _LOCK:
            _roll_if_needed(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"token_stats 写入失败（已忽略）: {e}")


def _read_all(since: float = 0.0, limit_lines: int = 0) -> List[Dict[str, Any]]:
    path = _file_path()
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("ts", 0) >= since:
                    out.append(row)
    except Exception as e:
        logger.warning(f"token_stats 读取失败: {e}")
        return []
    if limit_lines and len(out) > limit_lines:
        out = out[-limit_lines:]
    return out


def summary(days: int = 7) -> Dict[str, Any]:
    """按模型 / 来源 / 日期聚合，返回给前端直接用的结构。"""
    # 注意：不能用 `days or 7`，那会让 days=0 意外变成 7。
    # 非整数/None 回退到默认 7 天，数值本身夹到 [1, 365]。
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = 7
    days = max(1, min(d, 365))
    since = time.time() - days * 86400
    rows = _read_all(since=since)

    by_model: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"model": "", "provider": "", "prompt": 0, "completion": 0, "total": 0, "requests": 0})
    by_day: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"date": "", "prompt": 0, "completion": 0, "total": 0, "requests": 0})
    by_source: Dict[str, int] = defaultdict(int)

    tp = tc = tt = 0
    for r in rows:
        p = _num(r.get("prompt"))
        c = _num(r.get("completion"))
        t = _num(r.get("total"))
        tp += p
        tc += c
        tt += t

        key = f"{r.get('provider') or '-'} / {r.get('model') or 'unknown'}"
        m = by_model[key]
        m["model"] = r.get("model") or "unknown"
        m["provider"] = r.get("provider") or ""
        m["prompt"] += p
        m["completion"] += c
        m["total"] += t
        m["requests"] += 1

        day = time.strftime("%Y-%m-%d", time.localtime(r.get("ts", time.time())))
        d = by_day[day]
        d["date"] = day
        d["prompt"] += p
        d["completion"] += c
        d["total"] += t
        d["requests"] += 1

        by_source[r.get("source") or "llm"] += t

    models = sorted(by_model.values(), key=lambda x: x["total"], reverse=True)
    days_list = sorted(by_day.values(), key=lambda x: x["date"])
    recent = sorted(_read_all(since=since, limit_lines=_RECENT_KEEP),
                    key=lambda x: x.get("ts", 0), reverse=True)[:50]

    return {
        "ok": True,
        "window_days": days,
        "summary": {
            "prompt": tp,
            "completion": tc,
            "total": tt,
            "requests": len(rows),
            "models": len(models),
        },
        "by_model": models,
        "by_day": days_list,
        "by_source": [{"source": k, "total": v} for k, v in
                      sorted(by_source.items(), key=lambda x: x[1], reverse=True)],
        "recent": [
            {
                "ts": r.get("ts"),
                "time": time.strftime("%m-%d %H:%M:%S", time.localtime(r.get("ts", 0))),
                "model": r.get("model"),
                "provider": r.get("provider"),
                "source": r.get("source"),
                "session": r.get("session"),
                "prompt": _num(r.get("prompt")),
                "completion": _num(r.get("completion")),
                "total": _num(r.get("total")),
            }
            for r in recent
        ],
        "file": _file_path(),
    }


def reset() -> bool:
    """清空统计文件（前端「重置统计」用）。"""
    try:
        with _LOCK:
            path = _file_path()
            if os.path.exists(path):
                os.remove(path)
        return True
    except Exception as e:
        logger.warning(f"token_stats 重置失败: {e}")
        return False
