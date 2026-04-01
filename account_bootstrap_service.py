import asyncio
import threading
import time
from concurrent.futures import Future
from typing import Any, Dict, List, Optional

from loguru import logger

import cookie_manager
from ai_reply_engine import _extract_text_features, ai_reply_engine
from db_manager import db_manager


class AccountBootstrapService:
    """账号初始化服务：导入历史会话、提取风格样本、生成画像"""

    RUNNING_STATES = {"pending", "syncing_history", "extracting_samples", "building_profile"}

    def __init__(self):
        self._futures: Dict[str, Future] = {}
        self._lock = threading.RLock()

    def start_bootstrap(
        self,
        cookie_id: str,
        conversation_limit: int = 200,
        message_limit_per_chat: int = 100,
        force_rebuild: bool = False,
        trigger_mode: str = "manual",
    ) -> Dict[str, Any]:
        """启动账号初始化后台任务"""
        with self._lock:
            latest_job = db_manager.get_latest_account_bootstrap_job(cookie_id)
            latest_status = latest_job.get("status")
            future = self._futures.get(cookie_id)
            if future and not future.done():
                return {
                    "success": False,
                    "message": "账号初始化任务已在运行中",
                    **db_manager.get_bootstrap_summary(cookie_id),
                }
            if latest_status in self.RUNNING_STATES:
                return {
                    "success": False,
                    "message": "账号初始化任务已在运行中",
                    **db_manager.get_bootstrap_summary(cookie_id),
                }

            manager = cookie_manager.manager
            if manager is None or manager.loop is None:
                return {
                    "success": False,
                    "message": "账号任务管理器未就绪",
                    **db_manager.get_bootstrap_summary(cookie_id),
                }

            job_id = db_manager.create_account_bootstrap_job(
                cookie_id,
                trigger_mode=trigger_mode,
                conversation_limit=conversation_limit,
                message_limit_per_chat=message_limit_per_chat,
            )
            if not job_id:
                return {
                    "success": False,
                    "message": "初始化任务创建失败",
                    **db_manager.get_bootstrap_summary(cookie_id),
                }

            future = asyncio.run_coroutine_threadsafe(
                self._run_bootstrap(
                    cookie_id=cookie_id,
                    job_id=job_id,
                    conversation_limit=conversation_limit,
                    message_limit_per_chat=message_limit_per_chat,
                    force_rebuild=force_rebuild,
                ),
                manager.loop,
            )
            self._futures[cookie_id] = future
            future.add_done_callback(lambda _: self._clear_future(cookie_id))

            summary = db_manager.get_bootstrap_summary(cookie_id)
            return {
                "success": True,
                "message": "账号初始化任务已启动",
                **summary,
            }

    def _clear_future(self, cookie_id: str) -> None:
        with self._lock:
            self._futures.pop(cookie_id, None)

    def get_status(self, cookie_id: str) -> Dict[str, Any]:
        """获取账号初始化状态"""
        return db_manager.get_bootstrap_summary(cookie_id)

    async def _run_bootstrap(
        self,
        cookie_id: str,
        job_id: int,
        conversation_limit: int,
        message_limit_per_chat: int,
        force_rebuild: bool,
    ) -> None:
        from XianyuAutoAsync import XianyuLive

        try:
            live = XianyuLive.get_instance(cookie_id)
            if not live:
                raise RuntimeError("账号监听实例未启动，请稍后重试")

            db_manager.update_account_bootstrap_job(
                job_id,
                status="syncing_history",
                imported_conversations=0,
                imported_messages=0,
                extracted_samples=0,
                progress_json={
                    "status": "syncing_history",
                    "stage": "syncing_history",
                    "current_step": "拉取历史会话",
                    "conversation_limit": conversation_limit,
                    "message_limit_per_chat": message_limit_per_chat,
                    "warnings": [],
                },
            )

            imported_conversations = 0
            imported_messages = 0
            imported_chat_ids: List[str] = []
            warnings: List[str] = []

            async with live.open_im_rpc_socket() as websocket:
                conversations = await live.list_recent_conversations(
                    limit=conversation_limit,
                    websocket=websocket,
                )
                if not conversations:
                    raise RuntimeError("未拉取到任何历史会话")

                total_conversations = len(conversations)
                for index, conversation in enumerate(conversations, start=1):
                    single = (conversation or {}).get("singleChatUserConversation") or {}
                    single_chat = single.get("singleChatConversation") or {}
                    raw_chat_id = str(single_chat.get("cid") or "")
                    chat_id = raw_chat_id.split("@")[0] if "@goofish" in raw_chat_id else raw_chat_id
                    if not chat_id:
                        continue

                    item_id = live._extract_conversation_item_id(conversation)
                    history_result = await live.fetch_conversation_messages(
                        chat_id=chat_id,
                        item_id=item_id,
                        limit=message_limit_per_chat,
                        websocket=websocket,
                    )
                    messages = history_result.get("messages") or []

                    if not messages:
                        last_message = (single.get("lastMessage") or {}).get("message") or {}
                        fallback_message = live._normalize_history_message(chat_id, item_id, last_message)
                        if fallback_message:
                            messages = [fallback_message]
                            warnings.append(f"会话 {chat_id} 未拿到详细历史，已退化导入最后一条消息")

                    if not messages:
                        continue

                    inserted_count = 0
                    for message in messages:
                        result_id = db_manager.save_conversation_message(
                            cookie_id=cookie_id,
                            chat_id=message.get("chat_id") or chat_id,
                            item_id=message.get("item_id") or item_id,
                            sender_role=message.get("sender_role") or "buyer",
                            sender_id=message.get("sender_id"),
                            content=message.get("content") or "",
                            message_type=message.get("message_type") or "text",
                            reply_source=message.get("reply_source"),
                            is_manual=bool(message.get("is_manual")),
                            source_event_time=message.get("source_event_time"),
                            source_message_id=message.get("source_message_id"),
                            source_chat_id=message.get("source_chat_id") or chat_id,
                            metadata=message.get("metadata") or {},
                        )
                        if result_id:
                            inserted_count += 1

                    if inserted_count > 0:
                        imported_conversations += 1
                        imported_messages += inserted_count
                        imported_chat_ids.append(chat_id)

                    db_manager.update_account_bootstrap_job(
                        job_id,
                        status="syncing_history",
                        imported_conversations=imported_conversations,
                        imported_messages=imported_messages,
                        progress_json={
                            "status": "syncing_history",
                            "stage": "syncing_history",
                            "current_step": f"正在同步第 {index}/{total_conversations} 个会话",
                            "total_conversations": total_conversations,
                            "processed_conversations": index,
                            "warnings": warnings[-20:],
                        },
                    )

            db_manager.update_account_bootstrap_job(
                job_id,
                status="extracting_samples",
                imported_conversations=imported_conversations,
                imported_messages=imported_messages,
                progress_json={
                    "status": "extracting_samples",
                    "stage": "extracting_samples",
                    "current_step": "提取人工风格样本",
                    "warnings": warnings[-20:],
                },
            )
            extracted_samples = self._extract_style_samples(cookie_id, imported_chat_ids, force_rebuild=force_rebuild)

            db_manager.update_account_bootstrap_job(
                job_id,
                status="building_profile",
                imported_conversations=imported_conversations,
                imported_messages=imported_messages,
                extracted_samples=extracted_samples,
                progress_json={
                    "status": "building_profile",
                    "stage": "building_profile",
                    "current_step": "生成账号画像",
                    "warnings": warnings[-20:],
                },
            )
            profile_result = ai_reply_engine.rebuild_agent_profile(cookie_id)

            db_manager.update_account_bootstrap_job(
                job_id,
                status="ready",
                imported_conversations=imported_conversations,
                imported_messages=imported_messages,
                extracted_samples=extracted_samples,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                error_message=None,
                progress_json={
                    "status": "ready",
                    "stage": "ready",
                    "current_step": "初始化完成",
                    "warnings": warnings[-20:],
                    "profile_status": (profile_result.get("training_status") or {}).get("status"),
                    "profile_message": profile_result.get("message"),
                },
            )
        except Exception as error:
            logger.error(f"账号 {cookie_id} 初始化失败: {error}")
            db_manager.update_account_bootstrap_job(
                job_id,
                status="failed",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                error_message=str(error),
                progress_json={
                    "status": "failed",
                    "stage": "failed",
                    "current_step": "初始化失败",
                    "error_message": str(error),
                },
            )

    def _extract_style_samples(self, cookie_id: str, chat_ids: List[str], force_rebuild: bool = False) -> int:
        """从导入的历史消息中提取风格样本"""
        unique_chat_ids = [chat_id for chat_id in dict.fromkeys(chat_ids) if chat_id]
        total_samples = 0

        for chat_id in unique_chat_ids:
            messages = db_manager.get_conversation_messages(cookie_id, chat_id, limit=400)
            last_buyer_message: Optional[Dict[str, Any]] = None

            for message in messages:
                if message.get("sender_role") == "buyer":
                    last_buyer_message = message
                    continue

                if message.get("sender_role") != "seller" or not message.get("is_manual"):
                    continue

                if not last_buyer_message:
                    continue

                quality_score = ai_reply_engine._score_style_sample(
                    last_buyer_message.get("content", ""),
                    message.get("content", ""),
                )
                if quality_score < 0.45:
                    continue

                sample_id = db_manager.save_reply_style_sample(
                    cookie_id=cookie_id,
                    chat_id=chat_id,
                    item_id=message.get("item_id") or last_buyer_message.get("item_id"),
                    buyer_message=last_buyer_message.get("content", ""),
                    human_reply=message.get("content", ""),
                    source="history_manual",
                    quality_score=quality_score,
                    intent=ai_reply_engine.detect_intent(last_buyer_message.get("content", ""), cookie_id),
                    embedding={
                        "buyer_tokens": _extract_text_features(last_buyer_message.get("content", "")),
                        "reply_tokens": _extract_text_features(message.get("content", "")),
                    },
                    is_active=True,
                    source_buyer_message_id=last_buyer_message.get("source_message_id") or str(last_buyer_message.get("id")),
                    source_reply_message_id=message.get("source_message_id") or str(message.get("id")),
                )
                if sample_id:
                    total_samples += 1
                    last_buyer_message = None

        return total_samples


account_bootstrap_service = AccountBootstrapService()
