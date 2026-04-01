"""
AI回复引擎模块

本版本将原本写死的 AI 回复流程拆成可扩展的编排链路：
- IntentClassifier: 轻量意图识别
- PersonaResolver: 账号画像与策略解析
- StyleExampleRetriever: 从人工历史回复里检索风格样本
- PromptCompiler: 将商品、对话、画像、样本编排成统一 prompt
- ReplyGuard: 生成后做基础约束与兜底
- TraceRecorder: 记录本次命中的样本、版本、守卫结果
"""

import asyncio as _asyncio
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from loguru import logger
from openai import APIConnectionError, APITimeoutError, OpenAI

from db_manager import db_manager


def _safe_load_json(value: Any, default):
    if value in (None, ''):
        if isinstance(default, (dict, list)):
            return default.copy()
        return default

    if isinstance(value, (dict, list)):
        return value

    try:
        parsed = json.loads(value)
        return parsed if parsed is not None else default
    except Exception:
        if isinstance(default, (dict, list)):
            return default.copy()
        return default


def _safe_dump_json(value: Any) -> str:
    if value in (None, ''):
        return ''

    if isinstance(value, str):
        return value

    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return ''


def _normalize_text(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or '').strip())


def _compact_text(text: str) -> str:
    return re.sub(r'\s+', '', (text or '').strip().lower())


def _unique_preserve(items: List[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _extract_text_features(text: str) -> List[str]:
    text = _normalize_text(text)
    if not text:
        return []

    tokens: List[str] = []
    for token in re.findall(r'[\u4e00-\u9fff]{1,4}|[a-zA-Z0-9]+', text.lower()):
        tokens.append(token)

    compact = re.sub(r'\s+', '', text.lower())
    for size in (2, 3):
        for idx in range(max(0, len(compact) - size + 1)):
            gram = compact[idx: idx + size]
            if gram:
                tokens.append(gram)
    return _unique_preserve(tokens)


def _contains_any(text: str, keywords: List[str]) -> bool:
    return any(keyword and keyword in text for keyword in keywords)


def _jaccard_similarity(left: List[str], right: List[str]) -> float:
    if not left or not right:
        return 0.0

    left_set = set(left)
    right_set = set(right)
    union = left_set | right_set
    if not union:
        return 0.0
    return len(left_set & right_set) / len(union)


@dataclass
class OrchestratorResult:
    reply: Optional[str]
    intent: str
    raw_reply: Optional[str]
    compiled_prompt: str
    system_prompt: str
    user_prompt: str
    persona: Dict[str, Any]
    style_examples: List[Dict[str, Any]]
    guard_result: Dict[str, Any]
    strategy_version: str
    prompt_version: str
    model_name: str


class BaseAIProvider:
    name = "base"

    def complete(self, engine: "AIReplyEngine", settings: dict, messages: list,
                 max_tokens: int = 100, temperature: float = 0.7) -> str:
        raise NotImplementedError


class DashScopeProvider(BaseAIProvider):
    name = "dashscope"

    def complete(self, engine: "AIReplyEngine", settings: dict, messages: list,
                 max_tokens: int = 100, temperature: float = 0.7) -> str:
        return engine._call_dashscope_api(settings, messages, max_tokens=max_tokens, temperature=temperature)


class GeminiProvider(BaseAIProvider):
    name = "gemini"

    def complete(self, engine: "AIReplyEngine", settings: dict, messages: list,
                 max_tokens: int = 100, temperature: float = 0.7) -> str:
        return engine._call_gemini_api(settings, messages, max_tokens=max_tokens, temperature=temperature)


class OpenAICompatibleProvider(BaseAIProvider):
    name = "openai-compatible"

    def complete(self, engine: "AIReplyEngine", settings: dict, messages: list,
                 max_tokens: int = 100, temperature: float = 0.7) -> str:
        client = engine._create_openai_client(settings)
        if not client:
            raise ValueError("OpenAI 客户端创建失败")
        return engine._call_openai_api(client, settings, messages, max_tokens=max_tokens, temperature=temperature)


class IntentClassifier:
    GREETING_MESSAGES = {
        '在', '在?', '在？', '在吗', '在么', '在不', '哈喽', 'hello', 'hi', '你好', '您好'
    }
    AVAILABILITY_MESSAGES = {
        '还在吗', '还在么', '还在不', '还有吗', '有吗', '有货吗', '还有货吗'
    }

    def classify(self, message: str) -> str:
        msg_lower = (message or '').lower()
        msg_compact = _compact_text(message)

        if msg_compact in self.GREETING_MESSAGES:
            return 'greeting'
        if msg_compact in self.AVAILABILITY_MESSAGES:
            return 'availability'

        price_keywords = [
            '便宜', '优惠', '刀', '降价', '包邮', '价格', '多少钱', '能少', '还能', '最低',
            '底价', '实诚价', '到100', '能到', '包个邮', '给个价', '什么价', '小刀'
        ]
        if any(kw in msg_lower for kw in price_keywords):
            return 'price'

        tech_keywords = ['怎么用', '参数', '坏了', '故障', '设置', '说明书', '功能', '用法', '教程', '驱动']
        if any(kw in msg_lower for kw in tech_keywords):
            return 'tech'

        logistics_keywords = ['发货', '快递', '物流', '几天到', '包邮', '同城', '自提']
        if any(kw in msg_lower for kw in logistics_keywords):
            return 'logistics'

        aftersale_keywords = ['退', '换', '售后', '保修', '退款', '有问题', '质量']
        if any(kw in msg_lower for kw in aftersale_keywords):
            return 'aftersale'

        return 'default'


class PersonaResolver:
    def resolve(self, cookie_id: str, settings: dict, item_info: dict) -> Dict[str, Any]:
        stored_profile = db_manager.get_agent_profile(cookie_id)
        inline_profile = settings.get('agent_profile') or {}
        profile = {}
        if stored_profile:
            profile.update(stored_profile)
        if inline_profile:
            profile.update(inline_profile)

        persona_name = profile.get('persona_name') or f"{cookie_id}客服"
        tone_tags = profile.get('tone_tags') or ['友好', '简短', '成交导向']
        speaking_rules = profile.get('speaking_rules') or [
            '优先简短自然，不要像模板话术',
            '结合商品和上下文回答，不要脱离当前问题',
        ]
        forbidden_phrases = profile.get('forbidden_phrases') or []

        negotiation_policy = profile.get('negotiation_policy') or {
            'max_discount_percent': settings.get('max_discount_percent', 10),
            'max_discount_amount': settings.get('max_discount_amount', 100),
            'max_bargain_rounds': settings.get('max_bargain_rounds', 3),
            'allow_auto_bargain': settings.get('allow_auto_bargain', True),
        }
        negotiation_policy.setdefault('max_discount_percent', settings.get('max_discount_percent', 10))
        negotiation_policy.setdefault('max_discount_amount', settings.get('max_discount_amount', 100))
        negotiation_policy.setdefault('max_bargain_rounds', settings.get('max_bargain_rounds', 3))
        negotiation_policy.setdefault('allow_auto_bargain', settings.get('allow_auto_bargain', True))

        response_limits = profile.get('response_limits') or {
            'max_chars': 60,
            'prefer_short_reply': True,
        }

        return {
            **profile,
            'persona_name': persona_name,
            'tone_tags': tone_tags,
            'speaking_rules': speaking_rules,
            'forbidden_phrases': forbidden_phrases,
            'sales_style': profile.get('sales_style') or '自然成交',
            'service_style': profile.get('service_style') or '友好耐心',
            'negotiation_policy': negotiation_policy,
            'response_limits': response_limits,
            'item_title': item_info.get('title') or '未知商品',
        }


class StyleExampleRetriever:
    def retrieve(self, cookie_id: str, message: str, item_info: dict, intent: str, limit: int = 4) -> List[Dict[str, Any]]:
        samples = db_manager.get_reply_style_samples(cookie_id, limit=80, active_only=True)
        if not samples:
            return []

        query_tokens = _extract_text_features(message)
        item_id = str(item_info.get('item_id') or '')
        ranked: List[tuple] = []

        for sample in samples:
            embedding = sample.get('embedding') or {}
            if isinstance(embedding, list):
                sample_tokens = embedding
            else:
                sample_tokens = embedding.get('buyer_tokens', []) or _extract_text_features(sample.get('buyer_message', ''))

            similarity = _jaccard_similarity(query_tokens, sample_tokens)
            score = similarity * 0.65

            if intent and sample.get('intent') == intent:
                score += 0.15
            if item_id and str(sample.get('item_id') or '') == item_id:
                score += 0.1
            score += min(max(float(sample.get('quality_score') or 0), 0), 1) * 0.1

            if score > 0.08:
                sample['retrieval_score'] = round(score, 4)
                ranked.append((score, sample))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [sample for _, sample in ranked[:limit]]


class ConversationScopeGuard:
    BUSINESS_KEYWORDS = [
        '在吗', '还在', '有吗', '包邮', '顺丰', '快递', '发货', '物流', '自提', '同城',
        '价格', '多少', '便宜', '优惠', '刀', '小刀', '砍价', '最低', '包', '拍下',
        '成色', '几新', '瑕疵', '附件', '配件', '功能', '参数', '使用', '能用', '好用',
        '售后', '退款', '退货', '换货', '保修', '真假', '原装', '激活', '验机'
    ]
    OFF_TOPIC_KEYWORDS = [
        '天气', '新闻', '八卦', '热搜', '股票', '基金', '彩票', '币圈', '星座', '运势',
        '算命', '塔罗', '生肖', '笑话', '脑筋急转弯', '写代码', '编程', 'python', 'java',
        '翻译', '作文', '论文', '作业', '数学题', '英语题', '借钱', '红包', '转账',
        '几岁', '多大', '哪里人', '结婚', '对象', '身高', '体重', '吃饭', '睡了吗'
    ]
    ITEM_STOPWORDS = {
        '全新', '二手', '闲置', '转让', '出售', '商品', '宝贝', '支持', '可以', '一个',
        '这个', '那个', '正品', '原装', '官方', '版本', '型号', '详情', '默认', '链接'
    }

    def _extract_item_tokens(self, item_info: dict) -> List[str]:
        source = " ".join([
            str(item_info.get('title') or ''),
            str(item_info.get('desc') or ''),
        ])
        tokens = []
        for token in re.findall(r'[\u4e00-\u9fff]{2,6}|[a-zA-Z0-9]{2,}', source.lower()):
            cleaned = token.strip()
            if not cleaned or cleaned in self.ITEM_STOPWORDS:
                continue
            tokens.append(cleaned)
        return _unique_preserve(tokens[:80])

    def assess(self, message: str, item_info: dict, context: List[Dict[str, str]]) -> Dict[str, Any]:
        normalized = _normalize_text(message)
        compact = _compact_text(message)
        if not normalized:
            return {'is_relevant': False, 'reason': 'empty'}

        if compact in IntentClassifier.GREETING_MESSAGES or compact in IntentClassifier.AVAILABILITY_MESSAGES:
            return {'is_relevant': True, 'reason': 'greeting_or_availability'}

        if _contains_any(normalized.lower(), self.BUSINESS_KEYWORDS):
            return {'is_relevant': True, 'reason': 'business_keyword'}

        message_tokens = set(_extract_text_features(normalized))
        item_tokens = self._extract_item_tokens(item_info)
        item_overlap = [token for token in item_tokens if token in message_tokens or token in normalized.lower()]
        if item_overlap:
            return {'is_relevant': True, 'reason': 'item_overlap', 'matched_tokens': item_overlap[:5]}

        # 同一会话里的短追问，允许沿用上下文继续沟通
        if context and len(normalized) <= 8:
            follow_up_markers = ['这个', '那这个', '那款', '可以吗', '行吗', '多少', '包吗', '怎么拍', '能拍']
            if _contains_any(normalized, follow_up_markers):
                return {'is_relevant': True, 'reason': 'short_follow_up'}

        if _contains_any(normalized.lower(), self.OFF_TOPIC_KEYWORDS):
            return {'is_relevant': False, 'reason': 'off_topic_keyword'}

        unrelated_patterns = [
            r'你(是|会|能不能).*(算命|看相|占卜|塔罗)',
            r'帮我.*(写|做).*(代码|程序|作业|论文)',
            r'(今天|明天).*(天气|热搜|新闻)',
            r'你.*(几岁|多大|哪里人|结婚)',
        ]
        if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in unrelated_patterns):
            return {'is_relevant': False, 'reason': 'off_topic_pattern'}

        return {'is_relevant': True, 'reason': 'default_allow'}


class PromptCompiler:
    def __init__(self, default_prompts: Dict[str, str]):
        self.default_prompts = default_prompts

    def _load_base_prompts(self, settings: dict) -> Dict[str, str]:
        overrides_raw = settings.get('base_prompt_overrides') or settings.get('custom_prompts') or ''
        overrides = _safe_load_json(overrides_raw, {})
        prompts = dict(self.default_prompts)
        if isinstance(overrides, dict):
            for key, value in overrides.items():
                if key and value:
                    prompts[key] = value
        return prompts

    def compile(self, settings: dict, intent: str, message: str, item_info: dict, context: List[Dict[str, str]],
                bargain_count: int, persona: Dict[str, Any], style_examples: List[Dict[str, Any]]) -> Dict[str, str]:
        prompts = self._load_base_prompts(settings)
        base_system_prompt = prompts.get(intent, prompts['default'])

        tone_tags = "、".join(persona.get('tone_tags', [])) or '友好、简短'
        speaking_rules = "\n".join(f"- {rule}" for rule in persona.get('speaking_rules', []))
        forbidden_phrases = "、".join(persona.get('forbidden_phrases', [])) or '无'

        item_desc = (
            f"商品标题: {item_info.get('title', '未知')}\n"
            f"商品价格: {item_info.get('price', '未知')}元\n"
            f"商品描述: {item_info.get('desc', '无')}"
        )

        context_str = "\n".join([f"{msg['role']}: {msg['content']}" for msg in context[-10:]]) or "暂无"
        examples_str = "暂无命中样本"
        if style_examples:
            parts = []
            for index, sample in enumerate(style_examples, start=1):
                parts.append(
                    f"样本{index}（分数{sample.get('retrieval_score', 0)}）\n"
                    f"买家: {sample.get('buyer_message', '')}\n"
                    f"卖家: {sample.get('human_reply', '')}"
                )
            examples_str = "\n\n".join(parts)

        negotiation = persona.get('negotiation_policy', {})
        style_strength = settings.get('style_strength', 0.6)

        system_prompt = (
            f"{base_system_prompt}\n\n"
            f"你当前代表账号人设：{persona.get('persona_name', '卖家客服')}\n"
            f"语气标签：{tone_tags}\n"
            f"销售风格：{persona.get('sales_style', '自然成交')}\n"
            f"服务风格：{persona.get('service_style', '友好耐心')}\n"
            f"表达规则：\n{speaking_rules}\n"
            f"禁用表达：{forbidden_phrases}\n"
            f"风格强度：{style_strength}\n"
            "优先模仿该账号历史人工回复的风格，但不要逐字照抄。"
        )

        user_prompt = f"""商品信息：
{item_desc}

对话历史：
{context_str}

风格样本：
{examples_str}

议价设置：
- 当前议价次数：{bargain_count}
- 最大议价轮数：{negotiation.get('max_bargain_rounds', settings.get('max_bargain_rounds', 3))}
- 最大优惠百分比：{negotiation.get('max_discount_percent', settings.get('max_discount_percent', 10))}%
- 最大优惠金额：{negotiation.get('max_discount_amount', settings.get('max_discount_amount', 100))}元
- 是否允许自动议价：{bool(negotiation.get('allow_auto_bargain', settings.get('allow_auto_bargain', True)))}

当前用户消息：
{message}

请生成一条符合该账号人设的回复：
- 保持自然、像真人
- 只回答当前问题，不要跳到别的话题
- 只能回复与当前商品、交易流程、物流、售后、议价或当前会话衔接相关的问题
- 如果用户问题与当前商品或当前会话无关，直接输出：EMPTY_REPLY
- 如果用户只是打招呼/问在不在，只需简短确认，不要主动报价格、包邮、优惠、库存数量、留货
- 如果用户没有问价格，不要主动给价格方案或砍价方案
- 不要编造库存数量、销量、售后承诺、保留名额、帮忙留货
- 先回答当前问题，再考虑促进成交
- 不要编造承诺
- 若是议价，不能突破议价限制
"""

        return {
            'system_prompt': system_prompt,
            'user_prompt': user_prompt,
            'compiled_prompt': f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}",
        }


class ReplyGuard:
    def apply(self, reply: str, intent: str, settings: dict, persona: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        raw_reply = _normalize_text(reply)
        guard_result: Dict[str, Any] = {
            'trimmed': False,
            'removed_phrases': [],
            'fallback_used': False,
            'normalized_intent_reply': False,
        }

        if raw_reply == 'EMPTY_REPLY':
            guard_result['fallback_used'] = True
            guard_result['reason'] = 'empty_reply_sentinel'
            return 'EMPTY_REPLY', guard_result

        if not raw_reply:
            guard_result['fallback_used'] = True
            return "您好，还在的，有需要可以直接说哦。", guard_result

        if intent == 'greeting':
            guard_result['normalized_intent_reply'] = True
            return "在的，您说~", guard_result

        if intent == 'availability':
            guard_result['normalized_intent_reply'] = True
            return "有的，还在，您看中了直接说~", guard_result

        final_reply = raw_reply.replace('\n', ' ')
        response_limits = persona.get('response_limits') or {}
        max_chars = int(response_limits.get('max_chars') or 60)
        if len(final_reply) > max_chars:
            final_reply = final_reply[:max_chars].rstrip('，,。.；;!！?？')
            guard_result['trimmed'] = True

        for phrase in persona.get('forbidden_phrases', []):
            if phrase and phrase in final_reply:
                final_reply = final_reply.replace(phrase, '')
                guard_result['removed_phrases'].append(phrase)

        final_reply = _normalize_text(final_reply)
        if not final_reply:
            guard_result['fallback_used'] = True
            final_reply = "您好，还在的，有需要可以直接说哦。"

        if intent == 'price' and not settings.get('allow_auto_bargain', True):
            final_reply = "价格已经很实在了哦，喜欢可以直接拍下。"
            guard_result['fallback_used'] = True

        return final_reply, guard_result


class TraceRecorder:
    def record(self, cookie_id: str, chat_id: str, result: OrchestratorResult):
        trace = {
            'strategy_version': result.strategy_version,
            'prompt_version': result.prompt_version,
            'intent': result.intent,
            'retrieved_sample_ids': [sample['id'] for sample in result.style_examples],
            'model_name': result.model_name,
            'compiled_prompt': result.compiled_prompt,
            'raw_reply': result.raw_reply,
            'final_reply': result.reply,
            'guard_result': result.guard_result,
            'reply_source': 'ai',
            'metadata': {
                'persona_name': result.persona.get('persona_name'),
                'style_example_count': len(result.style_examples),
            },
        }
        db_manager.save_reply_generation_trace(cookie_id, chat_id, trace)


class ReplyOrchestrator:
    def __init__(self, engine: "AIReplyEngine"):
        self.engine = engine
        self.intent_classifier = IntentClassifier()
        self.persona_resolver = PersonaResolver()
        self.style_retriever = StyleExampleRetriever()
        self.scope_guard = ConversationScopeGuard()
        self.prompt_compiler = PromptCompiler(engine.default_prompts)
        self.reply_guard = ReplyGuard()
        self.trace_recorder = TraceRecorder()

    def run(self, message: str, item_info: dict, chat_id: str, cookie_id: str, user_id: str, item_id: str,
            save_history: bool = True, save_trace: bool = True) -> OrchestratorResult:
        settings = db_manager.get_ai_reply_settings(cookie_id)
        intent = self.intent_classifier.classify(message)
        logger.info(f"检测到意图: {intent} (账号: {cookie_id})")

        message_created_at = None
        if save_history:
            message_created_at = self.engine.save_conversation(
                chat_id, cookie_id, user_id, item_id, "user", message, intent
            )

        bargain_count = self.engine.get_bargain_count(chat_id, cookie_id)
        if intent == 'price' and bargain_count >= settings.get('max_bargain_rounds', 3):
            refuse_reply = "抱歉，这个价格已经是最优惠的了，不能再便宜了哦！"
            if save_history:
                self.engine.save_conversation(chat_id, cookie_id, user_id, item_id, "assistant", refuse_reply, intent)
            result = OrchestratorResult(
                reply=refuse_reply,
                intent=intent,
                raw_reply=refuse_reply,
                compiled_prompt='',
                system_prompt='',
                user_prompt='',
                persona={},
                style_examples=[],
                guard_result={'fallback_used': True, 'reason': 'bargain_limit'},
                strategy_version=settings.get('strategy_version', 'rag-v1'),
                prompt_version=settings.get('prompt_version', 'v2'),
                model_name=settings.get('model_name', ''),
            )
            if save_trace:
                self.trace_recorder.record(cookie_id, chat_id, result)
            return result

        context = self.engine.get_conversation_context(chat_id, cookie_id)
        scope_result = self.scope_guard.assess(message, item_info, context)
        if not scope_result.get('is_relevant', True):
            result = OrchestratorResult(
                reply='EMPTY_REPLY',
                intent=intent,
                raw_reply='EMPTY_REPLY',
                compiled_prompt='',
                system_prompt='',
                user_prompt='',
                persona={},
                style_examples=[],
                guard_result={'fallback_used': True, 'reason': 'off_topic_blocked', 'scope_result': scope_result},
                strategy_version=settings.get('strategy_version', 'rag-v1'),
                prompt_version=settings.get('prompt_version', 'v2'),
                model_name=settings.get('model_name', ''),
            )
            if save_trace:
                self.trace_recorder.record(cookie_id, chat_id, result)
            return result

        persona = self.persona_resolver.resolve(cookie_id, settings, {**item_info, 'item_id': item_id})

        style_examples: List[Dict[str, Any]] = []
        stats = settings.get('sample_stats') or db_manager.get_reply_style_stats(cookie_id)
        if (
            settings.get('enable_style_learning')
            and settings.get('prefer_human_style', True)
            and stats.get('active_samples', 0) >= settings.get('min_style_samples', 5)
        ):
            style_examples = self.style_retriever.retrieve(
                cookie_id=cookie_id,
                message=message,
                item_info={**item_info, 'item_id': item_id},
                intent=intent,
                limit=4,
            )

        prompt_parts = self.prompt_compiler.compile(
            settings=settings,
            intent=intent,
            message=message,
            item_info=item_info,
            context=context,
            bargain_count=bargain_count,
            persona=persona,
            style_examples=style_examples,
        )
        messages = [
            {"role": "system", "content": prompt_parts['system_prompt']},
            {"role": "user", "content": prompt_parts['user_prompt']},
        ]

        provider = self.engine._select_provider(settings)
        raw_reply = provider.complete(self.engine, settings, messages, max_tokens=96, temperature=0.3)
        final_reply, guard_result = self.reply_guard.apply(raw_reply, intent, settings, persona)

        if save_history:
            self.engine.save_conversation(chat_id, cookie_id, user_id, item_id, "assistant", final_reply, intent)

        result = OrchestratorResult(
            reply=final_reply,
            intent=intent,
            raw_reply=raw_reply,
            compiled_prompt=prompt_parts['compiled_prompt'],
            system_prompt=prompt_parts['system_prompt'],
            user_prompt=prompt_parts['user_prompt'],
            persona=persona,
            style_examples=style_examples,
            guard_result=guard_result,
            strategy_version=settings.get('strategy_version', 'rag-v1'),
            prompt_version=settings.get('prompt_version', 'v2'),
            model_name=settings.get('model_name', ''),
        )
        if save_trace:
            self.trace_recorder.record(cookie_id, chat_id, result)
        return result


class AIReplyEngine:
    """AI回复引擎"""

    def __init__(self):
        self._init_default_prompts()
        self._chat_locks = {}
        self._chat_locks_lock = threading.Lock()
        self.orchestrator = ReplyOrchestrator(self)

    def _init_default_prompts(self):
        self.default_prompts = {
            'greeting': '''你是一位二手交易卖家客服。
如果用户只是打招呼或问“在吗/在？”，只需简短确认在线即可。
不要主动报价格、优惠、包邮、留货、库存。''',
            'availability': '''你是一位二手交易卖家客服。
如果用户只是问“还在吗/有吗”，只需确认商品还在或还有，并邀请对方继续问。
不要主动报价格、优惠、包邮、留货、库存数量。''',
            'price': '''你是一位经验丰富的销售专家，擅长议价。
语言要求：简短直接、自然像真人，不要长篇大论。
议价策略：
1. 根据议价次数递减优惠力度
2. 接近最大议价轮数时坚持底线
3. 强调商品价值，不要轻易松口
4. 避免过度承诺''',
            'tech': '''你是一位懂产品的卖家客服。
语言要求：简短专业、好懂，先回答问题再补充建议。
回答重点：产品功能、使用方法、注意事项。''',
            'logistics': '''你是一位负责物流咨询的卖家客服。
语言要求：简短明确，优先回答发货、物流、同城、自提等问题。''',
            'aftersale': '''你是一位处理售后问题的卖家客服。
语言要求：态度友好、避免激化冲突，先确认问题，再给解决方向。''',
            'default': '''你是一位资深电商卖家，提供优质客服。
语言要求：简短友好、像真人聊天，不要生硬模板化。
回答重点：只回答客户当前问到的事。
如果客户没问价格，不要主动谈价格。
如果客户只是确认在线状态，只需简单回应。'''
        }

    def should_ignore_auto_reply_message(self, message: str) -> bool:
        text = _normalize_text(message)
        if not text:
            return True

        exact_ignored = {
            '[不想宝贝被砍价?设置不砍价回复  ]',
            'AI正在帮你回复消息，不错过每笔订单',
            '发来一条消息',
            '发来一条新消息',
            '[去创建合约]',
        }
        if text in exact_ignored:
            return True

        bracket_keywords = ['合约', '不砍价', '小红花', '评价', '去创建', '系统提示']
        if text.startswith('[') and text.endswith(']') and any(keyword in text for keyword in bracket_keywords):
            return True

        return False

    def _create_openai_client(self, settings: dict) -> Optional[OpenAI]:
        if not settings.get('api_key'):
            return None

        try:
            logger.info(
                f"创建新的OpenAI客户端实例: base_url={settings.get('base_url')}, "
                f"api_key_configured={bool(settings.get('api_key'))}"
            )
            client = OpenAI(
                api_key=settings['api_key'],
                base_url=settings.get('base_url'),
                timeout=45.0,
                max_retries=1,
            )
            return client
        except Exception as e:
            logger.error(f"创建OpenAI客户端失败: {e}")
            return None

    def _is_dashscope_api(self, settings: dict) -> bool:
        model_name = settings.get('model_name', '')
        base_url = settings.get('base_url', '')
        is_custom_model = model_name.lower() in ['custom', '自定义', 'dashscope', 'qwen-custom']
        is_dashscope_url = 'dashscope.aliyuncs.com' in base_url
        return is_custom_model and is_dashscope_url

    def _is_gemini_api(self, settings: dict) -> bool:
        model_name = settings.get('model_name', '').lower()
        return 'gemini' in model_name

    def _build_openai_extra_body(self, settings: dict) -> Dict[str, Any]:
        base_url = (settings.get('base_url') or '').lower()
        model_name = (settings.get('model_name') or '').lower()

        # qwen3.5-plus 在百炼侧默认开启思考模式，客服短回复显式关闭可显著降低延迟。
        if 'dashscope.aliyuncs.com' in base_url and model_name.startswith('qwen3.5'):
            return {'enable_thinking': False}
        return {}

    def _select_provider(self, settings: dict) -> BaseAIProvider:
        if self._is_dashscope_api(settings):
            return DashScopeProvider()
        if self._is_gemini_api(settings):
            return GeminiProvider()
        return OpenAICompatibleProvider()

    def _call_dashscope_api(self, settings: dict, messages: list, max_tokens: int = 100, temperature: float = 0.7) -> str:
        base_url = settings['base_url']
        if '/apps/' in base_url:
            app_id = base_url.split('/apps/')[-1].split('/')[0]
        else:
            raise ValueError("DashScope API URL中未找到app_id")

        url = f"https://dashscope.aliyuncs.com/api/v1/apps/{app_id}/completion"
        system_content = ""
        user_content = ""
        for msg in messages:
            if msg['role'] == 'system':
                system_content = msg['content']
            elif msg['role'] == 'user':
                user_content = msg['content']

        if system_content and user_content:
            prompt = f"{system_content}\n\n用户问题：{user_content}\n\n请直接回答用户的问题："
        elif user_content:
            prompt = user_content
        else:
            prompt = "\n".join([f"{msg['role']}: {msg['content']}" for msg in messages])

        data = {
            "input": {"prompt": prompt},
            "parameters": {"max_tokens": max_tokens, "temperature": temperature},
            "debug": {}
        }
        headers = {
            "Authorization": f"Bearer {settings['api_key']}",
            "Content-Type": "application/json"
        }

        response = requests.post(url, headers=headers, json=data, timeout=30)
        if response.status_code != 200:
            logger.error(f"DashScope API请求失败: {response.status_code} - {response.text}")
            raise Exception(f"DashScope API请求失败: {response.status_code} - {response.text}")

        result = response.json()
        if 'output' in result and 'text' in result['output']:
            return result['output']['text'].strip()
        raise Exception(f"DashScope API响应格式错误: {result}")

    def _call_gemini_api(self, settings: dict, messages: list, max_tokens: int = 100, temperature: float = 0.7) -> str:
        api_key = settings['api_key']
        model_name = settings['model_name']
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}

        system_instruction = ""
        user_content_parts = []
        for msg in messages:
            if msg['role'] == 'system':
                system_instruction = msg['content']
            elif msg['role'] == 'user':
                user_content_parts.append(msg['content'])

        user_content = "\n".join(user_content_parts)
        if not user_content:
            raise ValueError("未在消息中找到用户内容 (user content)")

        payload = {
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.status_code != 200:
            logger.error(f"Gemini API 请求失败: {response.status_code} - {response.text}")
            raise Exception(f"Gemini API 请求失败: {response.status_code} - {response.text}")

        result = response.json()
        try:
            return result['candidates'][0]['content']['parts'][0]['text'].strip()
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Gemini API 响应格式错误: {result} - {e}")
            raise Exception(f"Gemini API 响应格式错误: {result}")

    def _call_openai_api(self, client: OpenAI, settings: dict, messages: list, max_tokens: int = 100, temperature: float = 0.7) -> str:
        last_error: Optional[Exception] = None
        current_client = client

        for attempt in range(2):
            try:
                extra_body = self._build_openai_extra_body(settings)
                response = current_client.responses.create(
                    model=settings['model_name'],
                    input=messages,
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                    extra_body=extra_body or None,
                )
                output_text = (getattr(response, 'output_text', '') or '').strip()
                if output_text:
                    return output_text

                output = getattr(response, 'output', None) or []
                text_parts: List[str] = []
                for item in output:
                    for content in getattr(item, 'content', None) or []:
                        if getattr(content, 'type', None) == 'output_text' and getattr(content, 'text', None):
                            text_parts.append(content.text)

                merged_text = "\n".join(part.strip() for part in text_parts if part and part.strip()).strip()
                if merged_text:
                    return merged_text

                raise ValueError("Responses API 未返回可解析的文本内容")
            except (APIConnectionError, APITimeoutError) as e:
                last_error = e
                logger.warning(f"OpenAI API连接异常，第 {attempt + 1} 次尝试失败: {e}")
                if attempt == 0:
                    refreshed_client = self._create_openai_client(settings)
                    if refreshed_client:
                        current_client = refreshed_client
                    continue
                logger.error(f"OpenAI API调用失败: {e}")
                raise
            except Exception as e:
                logger.error(f"OpenAI API调用失败: {e}")
                raise

        if last_error:
            raise last_error
        raise RuntimeError("OpenAI API调用失败")

    def is_ai_enabled(self, cookie_id: str) -> bool:
        settings = db_manager.get_ai_reply_settings(cookie_id)
        return settings['ai_enabled']

    def detect_intent(self, message: str, cookie_id: str) -> str:
        try:
            settings = db_manager.get_ai_reply_settings(cookie_id)
            if not settings['ai_enabled']:
                return 'default'
            return self.orchestrator.intent_classifier.classify(message)
        except Exception as e:
            logger.error(f"本地意图检测失败 {cookie_id}: {e}")
            return 'default'

    def _get_chat_lock(self, chat_id: str) -> threading.Lock:
        with self._chat_locks_lock:
            if chat_id not in self._chat_locks:
                self._chat_locks[chat_id] = threading.Lock()
            return self._chat_locks[chat_id]

    def _should_skip_due_to_newer_message(self, chat_id: str, cookie_id: str, message_created_at: Optional[str], skip_wait: bool) -> bool:
        if not message_created_at:
            return False

        query_seconds = 6 if skip_wait else 25
        recent_messages = self._get_recent_user_messages(chat_id, cookie_id, seconds=query_seconds)
        if recent_messages:
            latest_message = recent_messages[-1]
            if message_created_at != latest_message['created_at']:
                logger.info(
                    f"【{cookie_id}】检测到有更新的消息，跳过当前消息 "
                    f"(时间:{message_created_at})，最新消息时间:{latest_message['created_at']}"
                )
                return True
        return False

    def generate_reply(self, message: str, item_info: dict, chat_id: str,
                      cookie_id: str, user_id: str, item_id: str,
                      skip_wait: bool = False) -> Optional[str]:
        if not self.is_ai_enabled(cookie_id):
            return None

        try:
            intent = self.detect_intent(message, cookie_id)
            message_created_at = self.save_conversation(chat_id, cookie_id, user_id, item_id, "user", message, intent)

            if not skip_wait:
                time.sleep(10)

            chat_lock = self._get_chat_lock(chat_id)
            with chat_lock:
                if self._should_skip_due_to_newer_message(chat_id, cookie_id, message_created_at, skip_wait):
                    return None

                result = self.orchestrator.run(
                    message=message,
                    item_info=item_info,
                    chat_id=chat_id,
                    cookie_id=cookie_id,
                    user_id=user_id,
                    item_id=item_id,
                    save_history=False,
                    save_trace=True,
                )

                # orchestrator 内部 save_history=False，因此这里统一保存 assistant，避免重复。
                if result.reply == "EMPTY_REPLY":
                    logger.info(f"AI判定当前消息与商品/会话无关，跳过回复 (账号: {cookie_id})")
                    return "EMPTY_REPLY"

                self.save_conversation(chat_id, cookie_id, user_id, item_id, "assistant", result.reply, result.intent)
                logger.info(f"AI回复生成成功 (账号: {cookie_id}): {result.reply}")
                return result.reply
        except Exception as e:
            logger.error(f"AI回复生成失败 {cookie_id}: {e}")
            return None

    async def generate_reply_async(self, message: str, item_info: dict, chat_id: str,
                                   cookie_id: str, user_id: str, item_id: str,
                                   skip_wait: bool = False) -> Optional[str]:
        try:
            return await _asyncio.to_thread(
                self.generate_reply, message, item_info, chat_id, cookie_id, user_id, item_id, skip_wait
            )
        except Exception as e:
            logger.error(f"异步生成回复失败: {e}")
            return None

    def preview_reply(self, message: str, item_info: dict, cookie_id: str, user_id: str = 'preview_user',
                      item_id: str = 'preview_item', chat_id: str = 'preview_chat') -> Dict[str, Any]:
        """预览 AI 编排结果，不写入历史对话"""
        settings = db_manager.get_ai_reply_settings(cookie_id)
        intent = self.detect_intent(message, cookie_id)
        if not settings.get('api_key'):
            context = self.get_conversation_context(chat_id, cookie_id)
            persona = self.orchestrator.persona_resolver.resolve(cookie_id, settings, {**item_info, 'item_id': item_id})
            style_examples = []
            if settings.get('enable_style_learning'):
                style_examples = self.orchestrator.style_retriever.retrieve(
                    cookie_id, message, {**item_info, 'item_id': item_id}, intent, limit=4
                )
            prompt_parts = self.orchestrator.prompt_compiler.compile(
                settings=settings,
                intent=intent,
                message=message,
                item_info=item_info,
                context=context,
                bargain_count=0,
                persona=persona,
                style_examples=style_examples,
            )
            return {
                'intent': intent,
                'reply': None,
                'warning': '当前账号未配置 API Key，仅返回编排后的 prompt 预览',
                'compiled_prompt': prompt_parts['compiled_prompt'],
                'system_prompt': prompt_parts['system_prompt'],
                'user_prompt': prompt_parts['user_prompt'],
                'style_examples': style_examples,
                'agent_profile': persona,
                'sample_stats': db_manager.get_reply_style_stats(cookie_id),
                'prompt_version': settings.get('prompt_version', 'v2'),
                'strategy_version': settings.get('strategy_version', 'rag-v1'),
            }

        result = self.orchestrator.run(
            message=message,
            item_info=item_info,
            chat_id=chat_id,
            cookie_id=cookie_id,
            user_id=user_id,
            item_id=item_id,
            save_history=False,
            save_trace=False,
        )
        return {
            'intent': result.intent,
            'reply': result.reply,
            'raw_reply': result.raw_reply,
            'compiled_prompt': result.compiled_prompt,
            'system_prompt': result.system_prompt,
            'user_prompt': result.user_prompt,
            'style_examples': result.style_examples,
            'agent_profile': result.persona,
            'guard_result': result.guard_result,
            'prompt_version': result.prompt_version,
            'strategy_version': result.strategy_version,
            'sample_stats': db_manager.get_reply_style_stats(cookie_id),
        }

    def rebuild_agent_profile(self, cookie_id: str) -> Dict[str, Any]:
        """根据人工样本重建账号画像"""
        settings = db_manager.get_ai_reply_settings(cookie_id)
        samples = db_manager.get_reply_style_samples(cookie_id, limit=40, active_only=True)
        stats = db_manager.get_reply_style_stats(cookie_id)

        if len(samples) < settings.get('min_style_samples', 5):
            profile = self._build_profile_heuristically(cookie_id, settings, samples)
            save_ok = db_manager.save_agent_profile(cookie_id, profile, status='draft')
            training_status = {
                'status': 'draft',
                'sample_count': len(samples),
                'required_samples': settings.get('min_style_samples', 5),
                'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'mode': 'heuristic',
                'bootstrap_ready': bool(save_ok),
            }
            settings['agent_profile'] = profile
            settings['training_status'] = training_status
            db_manager.save_ai_reply_settings(cookie_id, settings)
            return {
                'success': bool(save_ok),
                'message': '样本不足，已生成基础画像',
                'agent_profile': profile,
                'training_status': training_status,
                'sample_stats': stats,
            }

        profile = self._build_profile_with_fallback(cookie_id, settings, samples)
        save_ok = db_manager.save_agent_profile(cookie_id, profile, status='ready' if profile else 'draft')

        training_status = {
            'status': 'ready' if save_ok else 'failed',
            'sample_count': len(samples),
            'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'mode': profile.get('build_mode', 'heuristic') if profile else 'heuristic',
        }
        settings['agent_profile'] = profile
        settings['training_status'] = training_status
        db_manager.save_ai_reply_settings(cookie_id, settings)

        return {
            'success': bool(save_ok),
            'message': '账号画像重建完成' if save_ok else '账号画像重建失败',
            'agent_profile': profile,
            'training_status': training_status,
            'sample_stats': stats,
        }

    def get_agent_stats(self, cookie_id: str) -> Dict[str, Any]:
        settings = db_manager.get_ai_reply_settings(cookie_id)
        bootstrap_status = db_manager.get_bootstrap_summary(cookie_id)
        return {
            'cookie_id': cookie_id,
            'sample_stats': db_manager.get_reply_style_stats(cookie_id),
            'training_status': settings.get('training_status', {}),
            'bootstrap_status': bootstrap_status,
            'last_bootstrap_at': bootstrap_status.get('finished_at') or bootstrap_status.get('updated_at'),
            'imported_conversations': bootstrap_status.get('imported_conversations', 0),
            'imported_messages': bootstrap_status.get('imported_messages', 0),
            'prompt_version': settings.get('prompt_version', 'v2'),
            'strategy_version': settings.get('strategy_version', 'rag-v1'),
            'recent_traces': db_manager.get_recent_reply_generation_traces(cookie_id, limit=10),
        }

    def record_incoming_message(self, cookie_id: str, chat_id: str, user_id: str, item_id: str,
                                content: str, source_event_time: str = None):
        db_manager.save_conversation_message(
            cookie_id=cookie_id,
            chat_id=chat_id,
            item_id=item_id,
            sender_role='buyer',
            sender_id=user_id,
            content=content,
            message_type='text',
            reply_source='incoming',
            is_manual=False,
            source_event_time=source_event_time,
            source_chat_id=chat_id,
        )

    def record_manual_reply(self, cookie_id: str, chat_id: str, user_id: str, item_id: str,
                            content: str, source_event_time: str = None):
        message_id = db_manager.save_conversation_message(
            cookie_id=cookie_id,
            chat_id=chat_id,
            item_id=item_id,
            sender_role='seller',
            sender_id=user_id,
            content=content,
            message_type='text',
            reply_source='manual',
            is_manual=True,
            source_event_time=source_event_time,
            source_chat_id=chat_id,
        )

        settings = db_manager.get_ai_reply_settings(cookie_id)
        if not settings.get('capture_manual_samples', True):
            return

        buyer_message = db_manager.find_recent_buyer_message(cookie_id, chat_id, before_message_id=message_id)
        if not buyer_message:
            return

        sample_quality = self._score_style_sample(buyer_message.get('content', ''), content)
        if sample_quality < 0.45:
            return

        intent = self.detect_intent(buyer_message.get('content', ''), cookie_id)
        embedding = {
            'buyer_tokens': _extract_text_features(buyer_message.get('content', '')),
            'reply_tokens': _extract_text_features(content),
        }
        db_manager.save_reply_style_sample(
            cookie_id=cookie_id,
            chat_id=chat_id,
            item_id=item_id,
            buyer_message=buyer_message.get('content', ''),
            human_reply=content,
            source='manual',
            quality_score=sample_quality,
            intent=intent,
            embedding=embedding,
            is_active=True,
            source_buyer_message_id=buyer_message.get('source_message_id') or str(buyer_message.get('id')),
            source_reply_message_id=str(message_id) if message_id else None,
        )

    def record_auto_reply(self, cookie_id: str, chat_id: str, user_id: str, item_id: str, content: str,
                          reply_source: str = 'ai', message_type: str = 'text'):
        db_manager.save_conversation_message(
            cookie_id=cookie_id,
            chat_id=chat_id,
            item_id=item_id,
            sender_role='seller',
            sender_id=user_id,
            content=content,
            message_type=message_type,
            reply_source=reply_source,
            is_manual=False,
            source_event_time=None,
            source_chat_id=chat_id,
        )

    def _score_style_sample(self, buyer_message: str, human_reply: str) -> float:
        buyer_message = _normalize_text(buyer_message)
        human_reply = _normalize_text(human_reply)
        if not buyer_message or not human_reply:
            return 0.0
        if len(human_reply) < 2:
            return 0.0
        if re.fullmatch(r'[\W_]+', human_reply):
            return 0.0

        blocked_patterns = [
            '发货', '卡密', '复制', '系统', '自动回复', 'http://', 'https://', '__IMAGE_SEND__',
            '[卡片消息]', 'AI正在帮你回复消息'
        ]
        if any(pattern in human_reply for pattern in blocked_patterns):
            return 0.0

        score = 0.35
        if len(human_reply) <= 50:
            score += 0.15
        if re.search(r'[\u4e00-\u9fffA-Za-z0-9]', human_reply):
            score += 0.2
        if any(token in human_reply for token in ['哦', '哈', '呢', '的哈', '可以', '在的']):
            score += 0.1
        if len(set(_extract_text_features(human_reply))) >= 3:
            score += 0.1
        if len(buyer_message) >= 2:
            score += 0.1
        return round(min(score, 1.0), 4)

    def _build_profile_with_fallback(self, cookie_id: str, settings: dict, samples: List[Dict[str, Any]]) -> Dict[str, Any]:
        profile = self._build_profile_with_llm(settings, samples)
        if profile:
            return profile
        return self._build_profile_heuristically(cookie_id, settings, samples)

    def _build_profile_with_llm(self, settings: dict, samples: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not settings.get('api_key'):
            return {}

        sample_text = "\n\n".join(
            f"样本{index}\n买家: {sample['buyer_message']}\n卖家: {sample['human_reply']}"
            for index, sample in enumerate(samples[:15], start=1)
        )
        messages = [
            {
                "role": "system",
                "content": "你是客服风格分析助手。请根据样本提炼 JSON 格式账号画像，不要输出 JSON 以外的内容。"
            },
            {
                "role": "user",
                "content": f"""请输出 JSON，包含以下字段：
persona_name, tone_tags, speaking_rules, forbidden_phrases, sales_style, service_style, negotiation_policy, sample_reply

样本如下：
{sample_text}
"""
            }
        ]

        try:
            provider = self._select_provider(settings)
            raw = provider.complete(self, settings, messages, max_tokens=500, temperature=0.3)
            profile = _safe_load_json(raw, {})
            if isinstance(profile, dict) and profile:
                profile.setdefault('build_mode', 'llm')
                return profile
        except Exception as e:
            logger.warning(f"LLM 账号画像生成失败，回退启发式构建: {e}")
        return {}

    def _build_profile_heuristically(self, cookie_id: str, settings: dict, samples: List[Dict[str, Any]]) -> Dict[str, Any]:
        replies = [sample.get('human_reply', '') for sample in samples if sample.get('human_reply')]
        avg_length = sum(len(reply) for reply in replies) / len(replies) if replies else 0

        tone_tags = ['友好']
        if any('哦' in reply or '哈' in reply or '呢' in reply for reply in replies):
            tone_tags.append('口语化')
        if avg_length <= 18:
            tone_tags.append('简短')
        if any('可以' in reply or '拍' in reply or '喜欢' in reply for reply in replies):
            tone_tags.append('成交导向')

        endings = []
        for reply in replies:
            compact = _normalize_text(reply)
            endings.append(compact[-4:] if len(compact) >= 4 else compact)

        ending_freq: Dict[str, int] = {}
        for ending in endings:
            if ending:
                ending_freq[ending] = ending_freq.get(ending, 0) + 1

        common_endings = sorted(ending_freq.items(), key=lambda item: item[1], reverse=True)
        speaking_rules = [
            '优先用短句自然回复',
            '先回答客户当前问题，再补一句成交引导',
        ]
        if avg_length <= 18:
            speaking_rules.append('每次回复尽量控制在 1 到 2 句')

        forbidden_phrases = []
        if not settings.get('allow_auto_bargain', True):
            forbidden_phrases.append('再便宜点')

        sample_reply = replies[0] if replies else '您好，在的，有需要直接说哦。'
        return {
            'persona_name': f"{cookie_id}客服",
            'tone_tags': _unique_preserve(tone_tags),
            'speaking_rules': speaking_rules,
            'forbidden_phrases': forbidden_phrases,
            'sales_style': '自然成交',
            'service_style': '友好耐心',
            'negotiation_policy': {
                'max_discount_percent': settings.get('max_discount_percent', 10),
                'max_discount_amount': settings.get('max_discount_amount', 100),
                'max_bargain_rounds': settings.get('max_bargain_rounds', 3),
                'allow_auto_bargain': settings.get('allow_auto_bargain', True),
            },
            'sample_reply': sample_reply,
            'common_endings': [ending for ending, _ in common_endings[:5]],
            'build_mode': 'heuristic',
        }

    def get_conversation_context(self, chat_id: str, cookie_id: str, limit: int = 20) -> List[Dict]:
        try:
            with db_manager.lock:
                cursor = db_manager.conn.cursor()
                cursor.execute('''
                SELECT role, content FROM ai_conversations
                WHERE chat_id = ? AND cookie_id = ?
                ORDER BY created_at DESC LIMIT ?
                ''', (chat_id, cookie_id, limit))
                results = cursor.fetchall()
                return [{"role": row[0], "content": row[1]} for row in reversed(results)]
        except Exception as e:
            logger.error(f"获取对话上下文失败: {e}")
            return []

    def save_conversation(self, chat_id: str, cookie_id: str, user_id: str,
                          item_id: str, role: str, content: str, intent: str = None) -> Optional[str]:
        try:
            with db_manager.lock:
                cursor = db_manager.conn.cursor()
                cursor.execute('''
                INSERT INTO ai_conversations
                (cookie_id, chat_id, user_id, item_id, role, content, intent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (cookie_id, chat_id, user_id, item_id, role, content, intent))
                db_manager.conn.commit()
                cursor.execute('''
                SELECT created_at FROM ai_conversations
                WHERE rowid = last_insert_rowid()
                ''')
                result = cursor.fetchone()
                return result[0] if result else None
        except Exception as e:
            logger.error(f"保存对话记录失败: {e}")
            return None

    def get_bargain_count(self, chat_id: str, cookie_id: str) -> int:
        try:
            with db_manager.lock:
                cursor = db_manager.conn.cursor()
                cursor.execute('''
                SELECT COUNT(*) FROM ai_conversations
                WHERE chat_id = ? AND cookie_id = ? AND intent = 'price' AND role = 'user'
                ''', (chat_id, cookie_id))
                result = cursor.fetchone()
                return result[0] if result else 0
        except Exception as e:
            logger.error(f"获取议价次数失败: {e}")
            return 0

    def _get_recent_user_messages(self, chat_id: str, cookie_id: str, seconds: int = 2) -> List[Dict]:
        try:
            with db_manager.lock:
                cursor = db_manager.conn.cursor()
                cursor.execute('''
                SELECT content, created_at FROM ai_conversations
                WHERE chat_id = ? AND cookie_id = ? AND role = 'user'
                AND julianday('now') - julianday(created_at) < (? / 86400.0)
                ORDER BY created_at ASC
                ''', (chat_id, cookie_id, seconds))
                results = cursor.fetchall()
                return [{"content": row[0], "created_at": row[1]} for row in results]
        except Exception as e:
            logger.error(f"获取最近用户消息列表失败: {e}")
            return []


ai_reply_engine = AIReplyEngine()
