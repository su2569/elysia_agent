"""Elysia Agent 统一版本号

格式约定：
- 测试版：bate_x.y.z（如 bate_2.4.0）
- 正式版：x.y.z，从 1.0.0 开始（发布正式版时清零）

所有模块从此处导入版本号，避免散落不一致。
"""

__version__ = "bate_2.5.0"

# 版本元数据（供 WebUI/CLI 展示）
VERSION = __version__
RELEASE_NAME = "Elysia Agent"
AUTHOR = "su2569"