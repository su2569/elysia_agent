"""BaseSubAgent - 通用子代理基类（阶段二：垂直任务拆分）

设计原则（继承 ElyBot 防御工程基线）：
1. 安全链继承：所有工具调用一律经 Runner._execute_action 执行，
   复用主循环的 auth 权限门控 + self_check 参数扫描 + 钩子扩展点，
   子代理绝不直接触碰 ToolRegistry 执行入口（防权限绕过后门）
2. 最小权限：各子代理声明 allowed_tools 白名单，仅注入白名单内工具 schema
3. 禁止嵌套委托：子代理内部不注入 delegate_to_subagent（防无限递归）
4. 步数上限：max_steps 耗尽强制收尾，防失控循环
5. Runner 弱引用：不延长宿主 Runner 生命周期，防内存泄漏
"""
import json
from typing import Dict, Any, Optional, List, Set
from ..interfaces.subagent import ISubAgent
from ..interfaces.llm import ILLMProvider
from ..registry.tool_registry import ToolRegistry
from ..utils.logger import get_logger

logger = get_logger("SubAgent")


class BaseSubAgent(ISubAgent):
    """通用子代理基类 - ReAct 风格任务循环"""

    # 子类必须覆写
    allowed_tools: Set[str] = set()      # 工具白名单（最小权限）
    system_prompt: str = "你是一个专注的子代理。"

    # ---- ISubAgent 抽象属性默认实现（子类可直接继承或覆写）----

    @property
    def name(self) -> str:
        """代理名：默认取类名（注册中心键）"""
        return type(self).__name__

    @property
    def description(self) -> str:
        """能力描述：默认取 system_prompt 首行"""
        return self.system_prompt.strip().splitlines()[0] if self.system_prompt else "子代理"

    def __init__(self, llm: ILLMProvider, runner_ref=None, max_steps: int = 10):
        self._llm = llm
        self._runner_ref = runner_ref    # Runner 弱引用（可为 None，测试用）
        self._max_steps = max_steps

    # ---------- ISubAgent 契约 ----------

    @property
    def max_steps(self) -> int:
        return self._max_steps

    @property
    def llm(self) -> ILLMProvider:
        return self._llm

    async def run(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        parent_ctx: Optional[Any] = None
    ) -> Dict[str, Any]:
        """执行子代理任务循环。

        Returns:
            {"success": bool, "final_answer": str, "metadata": dict}
        """
        context = context or {}
        steps_used = 0
        tools_used: List[str] = []
        llm_calls = 0
        history: List[Dict[str, str]] = [
            {"role": "system", "content": self._build_system_prompt(context)},
            {"role": "user", "content": self._sanitize_task(task, context)},
        ]

        try:
            for step in range(self._max_steps):
                steps_used = step + 1
                resp = await self._call_llm(history)
                llm_calls += 1
                parsed = self._parse_response(resp)

                thought = parsed.get("thought", "")
                action = parsed.get("action")
                answer = parsed.get("answer", "")

                if action:
                    # ---- 安全链继承：经 Runner 统一执行（auth/self_check/钩子）----
                    observation = await self._execute_tool_safe(
                        action, parsed.get("action_input") or {}, parent_ctx
                    )
                    tools_used.append(action)
                    history.append({
                        "role": "assistant",
                        "content": json.dumps(
                            {"thought": thought, "action": action}, ensure_ascii=False
                        ),
                    })
                    history.append({"role": "user", "content": f"观察结果：{observation}"})
                    # 观察含错误标记时提示收尾，避免死磕
                    if str(observation).startswith("【错误】") and step >= self._max_steps - 2:
                        history.append({
                            "role": "user",
                            "content": "工具持续报错，请基于已有信息直接给出最终答案（answer 字段，action 置 null）。",
                        })
                    continue

                # action 为空 = 收尾作答
                if answer:
                    return {
                        "success": True,
                        "final_answer": answer,
                        "metadata": {
                            "steps_used": steps_used,
                            "tools_used": tools_used,
                            "llm_calls": llm_calls,
                            "agent": self.name,
                        },
                    }
                # 既无 action 也无 answer：提示 LLM 补答
                history.append({
                    "role": "user",
                    "content": "请直接给出最终答案：返回 JSON，answer 字段填入答案，action 置 null。",
                })

            # 步数耗尽：强制总结（有界催促：顽固 LLM 可能仍想调工具，忽略并继续要答案）
            history.append({
                "role": "user",
                "content": "步数已用尽，请立即基于已有信息总结最终答案（answer 字段）。",
            })
            # 宽限轮次取 max_steps 的 3 倍：足以催促顽固 LLM 交出答案；
            # 外层 Runner delegate_to_subagent 有 60s 超时兜底，此界仅防极端死循环
            grace_rounds = max(3, self._max_steps * 3)
            for _ in range(grace_rounds):
                resp = await self._call_llm(history)
                llm_calls += 1
                parsed = self._parse_response(resp)
                answer = parsed.get("answer")
                if answer:
                    return {
                        "success": True,
                        "final_answer": answer,
                        "metadata": {
                            "steps_used": steps_used,
                            "tools_used": tools_used,
                            "llm_calls": llm_calls,
                            "agent": self.name,
                            "exhausted": True,
                        },
                    }
                # 仍未交出答案：继续催促（不再执行任何工具调用）
                history.append({
                    "role": "user",
                    "content": "已无剩余步数，请直接在 answer 字段给出总结答案，不要调用任何工具。",
                })
            return {
                "success": False,
                "final_answer": parsed.get("thought") or "（子代理步数耗尽且未产出答案）",
                "metadata": {
                    "steps_used": steps_used,
                    "tools_used": tools_used,
                    "llm_calls": llm_calls,
                    "agent": self.name,
                    "exhausted": True,
                },
            }

        except Exception as e:
            logger.warning(f"SubAgent {self.name} 执行失败: {e}")
            return {
                "success": False,
                "final_answer": f"子代理执行失败: {e}",
                "metadata": {
                    "steps_used": steps_used,
                    "tools_used": tools_used,
                    "llm_calls": llm_calls,
                    "agent": self.name,
                    "error": str(e),
                },
            }

    # ---------- 内部机制 ----------

    def _build_system_prompt(self, context: Dict[str, Any]) -> str:
        tools_desc = "\n".join(
            f"- {t.name}: {t.description}"
            for t in ToolRegistry.list().values()
            if t.name in self.allowed_tools
        )
        ctx_hint = f"\n任务背景信息：{json.dumps(context, ensure_ascii=False)[:500]}" if context else ""
        return (
            f"{self.system_prompt}\n\n"
            f"可用工具（仅限以下白名单）：\n{tools_desc or '（无）'}\n\n"
            f"输出格式要求：每一步返回 JSON，字段为：\n"
            f'{{"thought": "思考过程", "action": "工具名或null", '
            f'"action_input": {{...}}, "answer": "最终答案或空"}}\n'
            f"当任务完成或无法继续时，action 填 null 并把结论写入 answer。{ctx_hint}"
        )

    def _sanitize_task(self, task: str, context: Dict[str, Any]) -> str:
        """任务文本净化（防提示词注入载荷经任务通道传递）"""
        runner = self._runner_ref() if self._runner_ref else None
        if runner is not None:
            try:
                task = runner._sanitize_input(task)
            except Exception:
                pass
        return f"任务：{task}"

    async def _call_llm(self, history: List[Dict[str, str]]):
        """调用 LLM（单次失败重试一次）"""
        schemas = self._build_schemas()
        last_err = None
        for attempt in range(2):
            try:
                return await self._llm.chat(
                    messages=history, tools=schemas or None,
                    temperature=0.3, max_tokens=1024,
                )
            except Exception as e:
                last_err = e
                logger.warning(f"SubAgent {self.name} LLM 调用失败(第{attempt+1}次): {e}")
        raise RuntimeError(f"LLM 调用失败: {last_err}")

    def _build_schemas(self) -> List[Dict]:
        """构建白名单内工具 schema（刻意排除 delegate_to_subagent，防嵌套递归）"""
        schemas = []
        for t in ToolRegistry.list().values():
            if t.name in self.allowed_tools:
                schemas.append({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                })
        return schemas

    async def _execute_tool_safe(self, action: str, inputs: Dict, parent_ctx) -> str:
        """安全工具执行：复用 Runner._execute_action 的完整安全链。

        - 白名单外工具直接拒绝（双保险，schema 未注入但 LLM 仍可能幻觉调用）
        - auth 权限门控 / self_check 参数扫描 / 破坏性操作拦截全部继承
        """
        # 嵌套委托检查必须先于白名单检查：delegate_to_subagent 是全局禁令，
        # 即使恶意 manifest 将其注入白名单也绝不允许（防无限递归）
        if action == "delegate_to_subagent":
            return "【错误】子代理不允许嵌套委托"
        if action not in self.allowed_tools:
            return f"【错误】子代理 {self.name} 无权使用工具 {action}（不在白名单）"
        runner = self._runner_ref() if self._runner_ref else None
        if runner is None:
            # 无 Runner 引用（隔离测试场景）：直接走注册表（不绕过生产安全链，因生产必有 runner）
            tool = ToolRegistry.get(action)
            if not tool:
                return f"【错误】工具 {action} 未注册"
            return await tool.execute(**inputs)
        return await runner._execute_action(action, inputs, parent_ctx)

    def _parse_response(self, resp) -> Dict[str, Any]:
        """解析 LLM 响应（复用 Runner 的增强解析器，含代码块/嵌套JSON/字段降级）"""
        runner = self._runner_ref() if self._runner_ref else None
        if runner is not None:
            try:
                return runner._parse_llm_response(resp)
            except Exception:
                pass
        # 本地降级解析
        import re
        content = resp.content if hasattr(resp, "content") else str(resp)
        result: Dict[str, Any] = {"thought": "", "action": None, "answer": ""}
        try:
            data = json.loads(content)
            return data if isinstance(data, dict) else result
        except json.JSONDecodeError:
            m = re.search(r'"answer"\s*:\s*"([^"]*)"', content, re.DOTALL)
            if m:
                result["answer"] = m.group(1)
            m2 = re.search(r'"action"\s*:\s*"([^"]*)"', content)
            if m2:
                result["action"] = m2.group(1) or None
            return result
