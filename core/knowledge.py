"""Knowledge Base 管理模块（轻量落盘方案）

实现「配置文件 + 本地目录落盘」的知识库管理：
- 每个知识库 = data/knowledge/<kb_name>/ 目录
- 目录内：meta.json（元数据）+ files/（上传的原始文档）+ chunks.json（重建索引后的分块）
- 重建索引：对可读文本（txt/md/json/csv/py/js/ts/java 等）做纯文本抽取 + 定长分块，落盘 chunks.json

接口由 main.py 的 /api/v1/kb/* 路由调用。
安全：仅允许在 KB 根目录内操作，文件名做白名单清洗（防路径穿越）。
"""
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

# 可抽取文本的扩展名
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".json", ".csv", ".log",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp",
    ".go", ".rs", ".sh", ".bat", ".ps1", ".yaml", ".yml", ".xml", ".html",
    ".ini", ".conf", ".toml",
}
# 明确不支持（需额外解析库）的类型，前端可提示
UNSUPPORTED_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z", ".bin", ".exe", ".dll", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp3", ".mp4"}

# 文件名白名单：中英文/数字/下划线/连字符/点/空格，禁止路径分隔符
_SAFE_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff .\-]{0,120}$", re.UNICODE)

CHUNK_SIZE = 800          # 每块字符数
CHUNK_OVERLAP = 80        # 块间重叠


class KnowledgeError(Exception):
    """知识库业务异常"""


def _safe_kb_name(name: str) -> str:
    """知识库名清洗（用于目录名）"""
    name = (name or "").strip()
    if not name:
        raise KnowledgeError("知识库名称不能为空")
    if not _SAFE_NAME_RE.match(name):
        raise KnowledgeError("知识库名称仅允许中文/字母/数字/下划线/连字符/点/空格")
    if name in (".", ".."):
        raise KnowledgeError("非法知识库名称")
    return name


def _safe_filename(name: str) -> str:
    """上传文件名清洗：去掉路径成分，只保留文件名"""
    name = os.path.basename((name or "").replace("\\", "/"))
    if not name:
        raise KnowledgeError("文件名不能为空")
    if not _SAFE_NAME_RE.match(name):
        raise KnowledgeError("文件名含非法字符，已拒绝")
    return name


def _kb_root(data_dir: str) -> str:
    root = os.path.join(data_dir, "knowledge")
    os.makedirs(root, exist_ok=True)
    return root


def _kb_dir(data_dir: str, name: str) -> str:
    return os.path.join(_kb_root(data_dir), _safe_kb_name(name))


def _files_dir(data_dir: str, name: str) -> str:
    d = os.path.join(_kb_dir(data_dir, name), "files")
    os.makedirs(d, exist_ok=True)
    return d


def list_kbs(data_dir: str) -> List[Dict[str, Any]]:
    """列出全部知识库（含统计）"""
    root = _kb_root(data_dir)
    out = []
    for entry in sorted(os.listdir(root)):
        d = os.path.join(root, entry)
        if not os.path.isdir(d):
            continue
        meta_path = os.path.join(d, "meta.json")
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        files_dir = os.path.join(d, "files")
        file_count, total_size = 0, 0
        if os.path.isdir(files_dir):
            for fn in os.listdir(files_dir):
                fp = os.path.join(files_dir, fn)
                if os.path.isfile(fp):
                    file_count += 1
                    try:
                        total_size += os.path.getsize(fp)
                    except OSError:
                        pass
        chunks_path = os.path.join(d, "chunks.json")
        chunk_count = 0
        if os.path.exists(chunks_path):
            try:
                with open(chunks_path, "r", encoding="utf-8") as f:
                    chunk_count = len(json.load(f).get("chunks", []))
            except Exception:
                chunk_count = 0
        out.append({
            "name": entry,
            "description": meta.get("description", ""),
            "created_at": meta.get("created_at"),
            "file_count": file_count,
            "total_size": total_size,
            "chunk_count": chunk_count,
            "indexed": chunk_count > 0,
        })
    return out


def create_kb(data_dir: str, name: str, description: str = "") -> Dict[str, Any]:
    """新建知识库"""
    safe = _safe_kb_name(name)
    d = _kb_dir(data_dir, safe)
    if os.path.exists(d):
        raise KnowledgeError(f"知识库已存在: {safe}")
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(d, "files"), exist_ok=True)
    meta = {
        "name": safe,
        "description": (description or "").strip(),
        "created_at": time.time(),
    }
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return {
        "name": safe,
        "description": meta["description"],
        "created_at": meta["created_at"],
        "file_count": 0,
        "total_size": 0,
        "chunk_count": 0,
        "indexed": False,
    }


def delete_kb(data_dir: str, name: str) -> bool:
    """删除知识库（递归）"""
    d = _kb_dir(data_dir, name)
    if not os.path.isdir(d):
        return False
    import shutil
    shutil.rmtree(d)
    return True


def _kb_exists(data_dir: str, name: str) -> bool:
    return os.path.isdir(_kb_dir(data_dir, name))


def upload_file(data_dir: str, kb_name: str, filename: str, content: bytes) -> Dict[str, Any]:
    """上传文档到知识库（内容为字节）"""
    safe_kb = _safe_kb_name(kb_name)
    if not _kb_exists(data_dir, safe_kb):
        raise KnowledgeError(f"知识库不存在: {safe_kb}")
    safe_fn = _safe_filename(filename)
    ext = os.path.splitext(safe_fn)[1].lower()
    if ext in UNSUPPORTED_EXTS:
        raise KnowledgeError(f"暂不支持解析该类型文档: {ext}（仅支持文本类文档）")
    target = os.path.join(_files_dir(data_dir, safe_kb), safe_fn)
    if os.path.exists(target):
        raise KnowledgeError(f"文件已存在: {safe_fn}（请先删除再上传）")
    with open(target, "wb") as f:
        f.write(content)
    return {"kb": safe_kb, "filename": safe_fn, "size": len(content)}


def list_files(data_dir: str, kb_name: str) -> List[Dict[str, Any]]:
    """列出知识库已上传文件"""
    safe_kb = _safe_kb_name(kb_name)
    if not _kb_exists(data_dir, safe_kb):
        raise KnowledgeError(f"知识库不存在: {safe_kb}")
    fd = _files_dir(data_dir, safe_kb)
    out = []
    for fn in sorted(os.listdir(fd)):
        fp = os.path.join(fd, fn)
        if os.path.isfile(fp):
            out.append({
                "filename": fn,
                "size": os.path.getsize(fp),
                "modified": os.path.getmtime(fp),
            })
    return out


def delete_file(data_dir: str, kb_name: str, filename: str) -> bool:
    """删除知识库内文件"""
    safe_kb = _safe_kb_name(kb_name)
    if not _kb_exists(data_dir, safe_kb):
        raise KnowledgeError(f"知识库不存在: {safe_kb}")
    safe_fn = _safe_filename(filename)
    target = os.path.join(_files_dir(data_dir, safe_kb), safe_fn)
    if not os.path.isfile(target):
        return False
    os.remove(target)
    # 删除文件后清掉旧索引，标记为未索引
    chunks_path = os.path.join(_kb_dir(data_dir, safe_kb), "chunks.json")
    if os.path.exists(chunks_path):
        try:
            os.remove(chunks_path)
        except OSError:
            pass
    return True


def _extract_text(path: str) -> str:
    """从文本类文件抽取纯文本"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """定长分块（带重叠），保留标题信息"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    chunks: List[str] = []
    buf = ""
    for line in lines:
        if len(buf) + len(line) + 1 > size and buf:
            chunks.append(buf)
            # 重叠：保留尾部若干字符
            buf = buf[-overlap:] if overlap else ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


def rebuild_index(data_dir: str, kb_name: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """重建索引入口：抽取全部文本文件 → 分块 → 落盘 chunks.json"""
    safe_kb = _safe_kb_name(kb_name)
    if not _kb_exists(data_dir, safe_kb):
        raise KnowledgeError(f"知识库不存在: {safe_kb}")
    fd = _files_dir(data_dir, safe_kb)
    all_chunks: List[Dict[str, Any]] = []
    source_count = 0
    for fn in sorted(os.listdir(fd)):
        fp = os.path.join(fd, fn)
        if not os.path.isfile(fp):
            continue
        ext = os.path.splitext(fn)[1].lower()
        if ext not in TEXT_EXTS:
            continue
        text = _extract_text(fp)
        if not text.strip():
            continue
        source_count += 1
        for i, c in enumerate(_chunk_text(text)):
            all_chunks.append({
                "kb": safe_kb,
                "source": fn,
                "chunk_index": i,
                "text": c,
            })
    payload = {
        "kb": safe_kb,
        "built_at": time.time(),
        "chunk_count": len(all_chunks),
        "source_count": source_count,
        "chunks": all_chunks,
        "metadata": metadata or {},
    }
    with open(os.path.join(_kb_dir(data_dir, safe_kb), "chunks.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return {
        "kb": safe_kb,
        "chunk_count": len(all_chunks),
        "source_count": source_count,
        "indexed": True,
    }
