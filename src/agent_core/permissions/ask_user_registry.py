"""ask_user：批量选择题 + 自由填写，阻塞等待人类在前端作答。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_notify_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None


def set_ask_user_notify_hook(
    fn: Optional[Callable[[str, Dict[str, Any]], None]],
) -> None:
    """由前端/connector 注册：收到 (batch_id, payload) 时推送到人类。"""
    global _notify_hook
    _notify_hook = fn


def get_ask_user_notify_hook() -> Optional[Callable[[str, Dict[str, Any]], None]]:
    return _notify_hook


@dataclass
class AskUserAnswer:
    """单题作答：要么选中 agent 给出的某一选项，要么使用自由填写文本。"""

    question_id: str
    selected_option: Optional[str] = None
    custom_text: Optional[str] = None


@dataclass
class AskUserBatchDecision:
    """整批作答，须覆盖 batch 内全部题目。"""

    answers: List[AskUserAnswer] = field(default_factory=list)


_futures: Dict[str, asyncio.Future[AskUserBatchDecision]] = {}
_batch_option_sets: Dict[str, Dict[str, Set[str]]] = {}
_batch_question_ids: Dict[str, List[str]] = {}
# 飞书多题分卡：逐题点击合并为整批答案后再唤醒 Future
_partial_answers: Dict[str, Dict[str, AskUserAnswer]] = {}


def _normalize_questions(raw: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Set[str]], List[str]]:
    """校验并规范化 questions；返回 (用于 payload 的列表, id->options, 有序 id 列表)。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("questions 必须为非空数组")

    seen: Set[str] = set()
    payload_items: List[Dict[str, Any]] = []
    opt_map: Dict[str, Set[str]] = {}
    qids: List[str] = []

    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"questions[{i}] 必须是对象")
        qid = str(item.get("id") or "").strip()
        if not qid:
            qid = f"q{i + 1}"
        if qid in seen:
            raise ValueError(f"题目 id 重复: {qid}")
        seen.add(qid)

        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"题目 {qid} 缺少 prompt")

        opts_raw = item.get("options")
        if not isinstance(opts_raw, list) or not opts_raw:
            raise ValueError(f"题目 {qid} 的 options 必须为非空数组")
        options: List[str] = []
        opt_seen: Set[str] = set()
        for j, o in enumerate(opts_raw):
            s = str(o).strip()
            if not s:
                raise ValueError(f"题目 {qid} 的 options[{j}] 不能为空")
            if s in opt_seen:
                raise ValueError(f"题目 {qid} 存在重复选项: {s}")
            opt_seen.add(s)
            options.append(s)

        payload_items.append({"id": qid, "prompt": prompt, "options": options})
        opt_map[qid] = set(options)
        qids.append(qid)

    return payload_items, opt_map, qids


def _validate_decision(
    expected_ids: List[str],
    opt_map: Dict[str, Set[str]],
    decision: AskUserBatchDecision,
) -> Optional[str]:
    """若无效返回错误说明，否则 None。"""
    by_id: Dict[str, AskUserAnswer] = {}
    for a in decision.answers or []:
        if not isinstance(a, AskUserAnswer):
            continue
        qid = (a.question_id or "").strip()
        if qid:
            by_id[qid] = a

    for qid in expected_ids:
        ans = by_id.get(qid)
        if ans is None:
            return f"缺少题目 {qid} 的答案"
        ct = str(ans.custom_text or "").strip()
        so = str(ans.selected_option or "").strip() if ans.selected_option else ""
        if ct:
            # 自由填写优先
            continue
        if not so:
            return f"题目 {qid} 须选择一项或填写自定义说明"
        if so not in opt_map.get(qid, set()):
            return f"题目 {qid} 的选项非法: {so}"
    return None


def register_ask_user_wait(
    questions_raw: Any,
) -> Tuple[str, asyncio.Future[AskUserBatchDecision], List[Dict[str, Any]]]:
    """创建 batch_id、Future，并登记题目选项集（供 resolve 校验）。"""
    payload_items, opt_map, qids = _normalize_questions(questions_raw)
    batch_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[AskUserBatchDecision] = loop.create_future()
    _futures[batch_id] = fut
    _batch_option_sets[batch_id] = opt_map
    _batch_question_ids[batch_id] = qids
    _partial_answers[batch_id] = {}
    return batch_id, fut, payload_items


def resolve_ask_user(batch_id: str, decision: AskUserBatchDecision) -> bool:
    """由人类操作前端在用户提交后调用，唤醒挂起的 ask_user 工具。"""
    bid = (batch_id or "").strip()
    fut = _futures.get(bid)
    if fut is None:
        logger.warning("resolve_ask_user: unknown or already resolved id=%s", bid)
        return False
    if fut.done():
        return False

    expected_ids = _batch_question_ids.get(bid, [])
    opt_map = _batch_option_sets.get(bid, {})
    err = _validate_decision(expected_ids, opt_map, decision)
    if err:
        logger.warning("resolve_ask_user: invalid decision id=%s detail=%s", bid, err)
        return False

    _futures.pop(bid, None)
    _batch_question_ids.pop(bid, None)
    _batch_option_sets.pop(bid, None)
    _partial_answers.pop(bid, None)
    fut.set_result(decision)
    return True


def _validate_single_answer(
    qid: str,
    opt_map: Dict[str, Set[str]],
    answer: AskUserAnswer,
) -> Optional[str]:
    if qid not in opt_map:
        return f"未知题目: {qid}"
    ct = str(answer.custom_text or "").strip()
    so = str(answer.selected_option or "").strip() if answer.selected_option else ""
    if ct:
        return None
    if not so:
        return "须选择一项或填写自定义说明"
    if so not in opt_map[qid]:
        return f"非法选项: {so}"
    return None


def submit_ask_user_fragment(batch_id: str, answer: AskUserAnswer) -> Tuple[bool, str]:
    """飞书分题提交：合并 partial，集齐后唤醒 Future。

    返回 (accepted, detail)。accepted 表示已记录或已完成；detail 含 partial/ completed / 错误原因。
    """
    bid = (batch_id or "").strip()
    fut = _futures.get(bid)
    if fut is None:
        return False, "unknown_batch"
    if fut.done():
        return False, "already_resolved"

    expected_ids = _batch_question_ids.get(bid, [])
    opt_map = _batch_option_sets.get(bid, {})
    qid = str(answer.question_id or "").strip()
    err = _validate_single_answer(qid, opt_map, answer)
    if err:
        return False, err

    partial = _partial_answers.setdefault(bid, {})
    ct = str(answer.custom_text or "").strip()
    so = str(answer.selected_option or "").strip() if answer.selected_option else ""
    if ct:
        partial[qid] = AskUserAnswer(question_id=qid, custom_text=ct)
    else:
        partial[qid] = AskUserAnswer(question_id=qid, selected_option=so)

    missing = [x for x in expected_ids if x not in partial]
    if missing:
        return True, f"partial:{','.join(missing)}"

    decision = AskUserBatchDecision(answers=[partial[i] for i in expected_ids])
    verr = _validate_decision(expected_ids, opt_map, decision)
    if verr:
        partial.pop(qid, None)
        return False, verr

    _futures.pop(bid, None)
    _batch_question_ids.pop(bid, None)
    _batch_option_sets.pop(bid, None)
    _partial_answers.pop(bid, None)
    fut.set_result(decision)
    return True, "completed"


def cancel_ask_user_wait(batch_id: str, *, reason: str = "cancelled") -> bool:
    """取消等待（例如 Core 关闭）。"""
    fut = _futures.pop(batch_id, None)
    _batch_question_ids.pop(batch_id, None)
    _batch_option_sets.pop(batch_id, None)
    _partial_answers.pop(batch_id, None)
    if fut is None or fut.done():
        return False
    fut.set_exception(asyncio.CancelledError(reason))
    return True


def notify_ask_user_pending(batch_id: str, payload: Dict[str, Any]) -> None:
    if _notify_hook is not None:
        try:
            _notify_hook(batch_id, payload)
        except Exception as exc:
            logger.warning("ask_user notify hook failed: %s", exc)


def parse_answers_from_ipc_params(params: Dict[str, Any]) -> AskUserBatchDecision:
    """将 IPC / JSON 参数解析为 AskUserBatchDecision。"""
    raw_list = params.get("answers")
    if not isinstance(raw_list, list):
        return AskUserBatchDecision(answers=[])

    out: List[AskUserAnswer] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("question_id") or item.get("id") or "").strip()
        if not qid:
            continue
        so = item.get("selected_option")
        ct = item.get("custom_text")
        out.append(
            AskUserAnswer(
                question_id=qid,
                selected_option=str(so).strip() if so is not None and str(so).strip() else None,
                custom_text=str(ct).strip() if ct is not None and str(ct).strip() else None,
            )
        )
    return AskUserBatchDecision(answers=out)
