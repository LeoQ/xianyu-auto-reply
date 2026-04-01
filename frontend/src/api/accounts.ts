import { get, post, put, del } from '@/utils/request'
import type { Account, AccountDetail, ApiResponse } from '@/types'

// 获取账号列表（返回账号ID数组）
export const getAccounts = async (): Promise<Account[]> => {
  const ids: string[] = await get('/cookies')
  // 后端返回的是账号ID数组，转换为Account对象数组
  return ids.map(id => ({ 
    id, 
    cookie: '', 
    enabled: true,
    use_ai_reply: false,
    use_default_reply: false,
    auto_confirm: false
  }))
}

// 获取账号详情列表
export const getAccountDetails = async (): Promise<AccountDetail[]> => {
  interface BackendAccountDetail {
    id: string
    value: string
    enabled: boolean
    auto_confirm: boolean
    remark?: string
    pause_duration?: number
    username?: string
    show_browser?: boolean
  }
  const data = await get<BackendAccountDetail[]>('/cookies/details')
  // 后端返回 value 字段，前端使用 cookie 字段
  return data.map((item) => ({
    id: item.id,
    cookie: item.value,
    enabled: item.enabled,
    auto_confirm: item.auto_confirm,
    note: item.remark,
    pause_duration: item.pause_duration,
    username: item.username,
    show_browser: item.show_browser,
    use_ai_reply: false,
    use_default_reply: false,
  }))
}

// 添加账号
export const addAccount = (data: { id: string; cookie: string }): Promise<ApiResponse> => {
  // 后端需要 id 和 value 字段
  return post('/cookies', { id: data.id, value: data.cookie })
}

// 更新账号 Cookie 值
export const updateAccountCookie = (id: string, value: string): Promise<ApiResponse> => {
  return put(`/cookies/${id}`, { id, value })
}

// 更新账号启用/禁用状态
export const updateAccountStatus = (id: string, enabled: boolean): Promise<ApiResponse> => {
  return put(`/cookies/${id}/status`, { enabled })
}

// 更新账号备注
export const updateAccountRemark = (id: string, remark: string): Promise<ApiResponse> => {
  return put(`/cookies/${id}/remark`, { remark })
}

// 更新账号自动确认设置
export const updateAccountAutoConfirm = (id: string, autoConfirm: boolean): Promise<ApiResponse> => {
  return put(`/cookies/${id}/auto-confirm`, { auto_confirm: autoConfirm })
}

// 更新账号暂停时间
export const updateAccountPauseDuration = (id: string, pauseDuration: number): Promise<ApiResponse> => {
  return put(`/cookies/${id}/pause-duration`, { pause_duration: pauseDuration })
}

// 更新账号登录信息（用户名、密码、是否显示浏览器）
export const updateAccountLoginInfo = (id: string, data: {
  username?: string
  login_password?: string
  show_browser?: boolean
}): Promise<ApiResponse> => {
  return put(`/cookies/${id}/login-info`, data)
}

// 删除账号
export const deleteAccount = (id: string): Promise<ApiResponse> => {
  return del(`/cookies/${id}`)
}

// 获取账号二维码登录
export const getQRCode = (accountId: string): Promise<{ success: boolean; qrcode_url?: string; token?: string }> => {
  return post('/qrcode/generate', { account_id: accountId })
}

// 检查二维码登录状态
export const checkQRCodeStatus = (token: string): Promise<{ success: boolean; status: string; cookie?: string }> => {
  return post('/qrcode/check', { token })
}

// 账号密码登录
export const passwordLogin = (data: { account_id: string; account: string; password: string; show_browser?: boolean }): Promise<ApiResponse> => {
  return post('/password-login', data)
}

// 生成扫码登录二维码
export const generateQRLogin = (): Promise<{ success: boolean; session_id?: string; qr_code_url?: string; message?: string }> => {
  return post('/qr-login/generate')
}

// 检查扫码登录状态
// 后端直接返回 { status: ..., message?: ..., account_info?: ... }，没有 success 字段
export const checkQRLoginStatus = async (sessionId: string): Promise<{
  success: boolean
  status: 'pending' | 'scanned' | 'success' | 'expired' | 'cancelled' | 'verification_required' | 'processing' | 'already_processed' | 'error'
  message?: string
  account_info?: {
    account_id: string
    is_new_account: boolean
  }
}> => {
  const result = await get<{
    status: string
    message?: string
    account_info?: { account_id: string; is_new_account: boolean }
  }>(`/qr-login/check/${sessionId}`)
  // 后端没有返回 success 字段，根据 status 判断
  return {
    success: result.status !== 'error',
    status: result.status as 'pending' | 'scanned' | 'success' | 'expired' | 'cancelled' | 'verification_required' | 'processing' | 'already_processed' | 'error',
    message: result.message,
    account_info: result.account_info,
  }
}

// 检查密码登录状态
export const checkPasswordLoginStatus = (sessionId: string): Promise<{
  success: boolean
  status: 'pending' | 'processing' | 'success' | 'failed' | 'verification_required'
  message?: string
  account_id?: string
}> => {
  return get(`/password-login/status/${sessionId}`)
}

// AI 回复设置接口 - 与后端 AIReplySettings 模型对应
export interface AgentProfile {
  persona_name?: string
  tone_tags?: string[]
  speaking_rules?: string[]
  forbidden_phrases?: string[]
  sales_style?: string
  service_style?: string
  negotiation_policy?: Record<string, unknown>
  sample_reply?: string
  common_endings?: string[]
  build_mode?: string
  version?: number
  status?: string
  updated_at?: string
}

export interface AISampleStats {
  total_samples: number
  active_samples: number
  manual_messages: number
  incoming_messages: number
  auto_messages: number
  ai_messages: number
  human_takeover_rate: number
  latest_profile_version?: number | null
  last_profile_status?: string
  trace_count: number
  bootstrap_status?: string
  last_bootstrap_at?: string
  imported_conversations?: number
  imported_messages?: number
}

export interface AITrace {
  id: number
  created_at: string
  intent?: string
  final_reply?: string
  raw_reply?: string
  retrieved_sample_ids?: number[]
  prompt_version?: string
  strategy_version?: string
}

export interface AIReplySettings {
  ai_enabled: boolean
  model_name?: string
  api_key?: string
  base_url?: string
  max_discount_percent?: number
  max_discount_amount?: number
  max_bargain_rounds?: number
  custom_prompts?: string
  base_prompt_overrides?: string
  enable_style_learning?: boolean
  capture_manual_samples?: boolean
  min_style_samples?: number
  style_strength?: number
  allow_auto_bargain?: boolean
  prefer_human_style?: boolean
  prompt_version?: string
  strategy_version?: string
  agent_profile?: AgentProfile
  policy_flags?: Record<string, unknown>
  training_status?: Record<string, unknown>
  sample_stats?: AISampleStats
  // 兼容旧字段（前端内部使用）
  enabled?: boolean
}

export interface AIBootstrapStatus {
  job_id?: number
  cookie_id: string
  status: string
  trigger_mode?: string
  conversation_limit?: number
  message_limit_per_chat?: number
  imported_conversations?: number
  imported_messages?: number
  extracted_samples?: number
  started_at?: string
  finished_at?: string
  error_message?: string
  updated_at?: string
  progress?: Record<string, unknown>
  current_step?: string
  warnings?: string[]
}

export interface AIHistoryImportResult {
  success: boolean
  message?: string
  imported_conversations: number
  imported_messages: number
  extracted_samples: number
  agent_profile?: AgentProfile
  training_status?: Record<string, unknown>
  sample_stats?: AISampleStats
  bootstrap_status?: AIBootstrapStatus
  warnings?: string[]
}

// 获取AI回复设置
export const getAIReplySettings = (cookieId: string): Promise<AIReplySettings> => {
  return get(`/ai-reply-settings/${cookieId}`)
}

// 更新AI回复设置
export const updateAIReplySettings = (cookieId: string, settings: Partial<AIReplySettings>): Promise<ApiResponse> => {
  const payload: Record<string, unknown> = {}
  const fieldMap: Array<[keyof AIReplySettings, string]> = [
    ['ai_enabled', 'ai_enabled'],
    ['model_name', 'model_name'],
    ['api_key', 'api_key'],
    ['base_url', 'base_url'],
    ['max_discount_percent', 'max_discount_percent'],
    ['max_discount_amount', 'max_discount_amount'],
    ['max_bargain_rounds', 'max_bargain_rounds'],
    ['custom_prompts', 'custom_prompts'],
    ['base_prompt_overrides', 'base_prompt_overrides'],
    ['enable_style_learning', 'enable_style_learning'],
    ['capture_manual_samples', 'capture_manual_samples'],
    ['min_style_samples', 'min_style_samples'],
    ['style_strength', 'style_strength'],
    ['allow_auto_bargain', 'allow_auto_bargain'],
    ['prefer_human_style', 'prefer_human_style'],
    ['prompt_version', 'prompt_version'],
    ['strategy_version', 'strategy_version'],
    ['agent_profile', 'agent_profile'],
    ['policy_flags', 'policy_flags'],
    ['training_status', 'training_status'],
  ]

  fieldMap.forEach(([sourceKey, targetKey]) => {
    if (Object.prototype.hasOwnProperty.call(settings, sourceKey) && settings[sourceKey] !== undefined) {
      payload[targetKey] = settings[sourceKey]
    }
  })

  if (Object.prototype.hasOwnProperty.call(settings, 'enabled') && settings.enabled !== undefined) {
    payload.ai_enabled = settings.enabled
  }

  return put(`/ai-reply-settings/${cookieId}`, payload)
}

// 获取所有账号的AI回复设置
export const getAllAIReplySettings = (): Promise<Record<string, AIReplySettings>> => {
  return get('/ai-reply-settings')
}

export const rebuildAIReplyProfile = (cookieId: string): Promise<{
  success: boolean
  message?: string
  agent_profile?: AgentProfile
  training_status?: Record<string, unknown>
  sample_stats?: AISampleStats
}> => {
  return post(`/ai-agent/${cookieId}/rebuild-profile`)
}

export const bootstrapAIAgent = (cookieId: string, payload?: {
  conversation_limit?: number
  message_limit_per_chat?: number
  force_rebuild?: boolean
}): Promise<AIBootstrapStatus & { success: boolean; message?: string }> => {
  return post(`/ai-agent/${cookieId}/bootstrap`, {
    conversation_limit: payload?.conversation_limit ?? 200,
    message_limit_per_chat: payload?.message_limit_per_chat ?? 100,
    force_rebuild: payload?.force_rebuild ?? false,
  })
}

export const getAIBootstrapStatus = (cookieId: string): Promise<AIBootstrapStatus> => {
  return get(`/ai-agent/${cookieId}/bootstrap-status`)
}

export const importAIAgentHistory = async (
  cookieId: string,
  file: File,
  payload?: {
    force_rebuild?: boolean
  },
): Promise<AIHistoryImportResult> => {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('force_rebuild', String(payload?.force_rebuild ?? false))
  return post(`/ai-agent/${cookieId}/import-history`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}

export const getAIAgentStats = (cookieId: string): Promise<{
  cookie_id: string
  sample_stats: AISampleStats
  training_status?: Record<string, unknown>
  bootstrap_status?: AIBootstrapStatus
  last_bootstrap_at?: string
  imported_conversations?: number
  imported_messages?: number
  prompt_version?: string
  strategy_version?: string
  recent_traces?: AITrace[]
}> => {
  return get(`/ai-agent/${cookieId}/stats`)
}

export const previewAIReplyAgent = async (
  cookieId: string,
  params: {
    message: string
    item_title?: string
    item_price?: string | number
    item_desc?: string
    item_id?: string
    chat_id?: string
    user_id?: string
  },
): Promise<{
  intent: string
  reply?: string | null
  raw_reply?: string | null
  warning?: string
  compiled_prompt: string
  system_prompt: string
  user_prompt: string
  style_examples: Array<Record<string, unknown>>
  agent_profile: AgentProfile
  guard_result?: Record<string, unknown>
  sample_stats: AISampleStats
  prompt_version?: string
  strategy_version?: string
}> => {
  const query = new URLSearchParams({
    message: params.message,
    item_title: params.item_title ?? '测试商品',
    item_price: String(params.item_price ?? '100'),
    item_desc: params.item_desc ?? '这是一个测试商品',
    item_id: params.item_id ?? 'preview_item',
    chat_id: params.chat_id ?? 'preview_chat',
    user_id: params.user_id ?? 'preview_user',
  })
  return get(`/ai-agent/${cookieId}/preview?${query.toString()}`)
}
