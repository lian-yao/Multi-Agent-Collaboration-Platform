/* modelCapabilities.ts — 常见模型的上下文窗口查询表
 *
 * 用途：在「手动登记模型」与「编辑模型特化参数」里，按模型名把上下文上限自动带出来，
 * 免去每次手查厂商文档。用户手动改过之后不再覆盖（`ModelSection` 用 `contextTouched` 控制）。
 *
 * 匹配是**查表 + 归一化**，不是正则猜测：
 * 先去厂商前缀与大小写，再在「点/横线互换」「去日期后缀」「去 -latest」几个变体上精确命中，
 * 最后用最长前缀回退接住带日期/版本尾巴的名字。所以下面三个名字都会命中同一条：
 *   `openai/gpt-4o` · `gpt-4o-2024-08-06` · `GPT-4O`
 *
 * 表内数值取自各厂商公开文档的 context window；拿不准的宁可**不收**（漏掉只是少个默认值，
 * 收错会让用户以为上限比真实值大，从而攒出必然超限的请求）。
 */

/** 模型名 → 上下文窗口（token）。键一律小写、无厂商前缀。 */
export const KNOWN_CONTEXT_TOKENS: Readonly<Record<string, number>> = {
  /* OpenAI —— 新一代推理/通用模型普遍 400K，gpt-4.1 系为 1M */
  "gpt-5": 400000,
  "gpt-5-mini": 400000,
  "gpt-5-nano": 400000,
  "gpt-4.1": 1047576,
  "gpt-4.1-mini": 1047576,
  "gpt-4.1-nano": 1047576,
  "gpt-4o": 128000,
  "gpt-4o-mini": 128000,
  "gpt-4-turbo": 128000,
  "gpt-4": 8192,
  "gpt-3.5-turbo": 16385,
  o1: 200000,
  "o1-mini": 128000,
  "o1-preview": 128000,
  o3: 200000,
  "o3-mini": 200000,
  "o4-mini": 200000,

  /* Anthropic —— Claude 3 起统一 200K（Sonnet 4.5 有 1M beta，按保守的 200K 收） */
  "claude-opus-4-1": 200000,
  "claude-opus-4": 200000,
  "claude-sonnet-4-5": 200000,
  "claude-sonnet-4": 200000,
  "claude-haiku-4-5": 200000,
  "claude-3-7-sonnet": 200000,
  "claude-3-5-sonnet": 200000,
  "claude-3-5-haiku": 200000,
  "claude-3-opus": 200000,
  "claude-3-sonnet": 200000,
  "claude-3-haiku": 200000,

  /* Google Gemini —— 1M 起步，1.5 Pro 为 2M */
  "gemini-2.5-pro": 1048576,
  "gemini-2.5-flash": 1048576,
  "gemini-2.5-flash-lite": 1048576,
  "gemini-2.0-flash": 1048576,
  "gemini-2.0-flash-lite": 1048576,
  "gemini-1.5-pro": 2097152,
  "gemini-1.5-flash": 1048576,

  /* DeepSeek —— 官方 API 侧 64K */
  "deepseek-chat": 65536,
  "deepseek-reasoner": 65536,
  "deepseek-v3": 65536,
  "deepseek-v3.1": 131072,
  "deepseek-r1": 65536,

  /* 月之暗面 / 智谱 / 字节 / 阿里 / 阶跃 等国内模型 */
  "kimi-k2": 131072,
  "kimi-latest": 131072,
  "moonshot-v1-8k": 8192,
  "moonshot-v1-32k": 32768,
  "moonshot-v1-128k": 131072,
  "glm-4-plus": 128000,
  "glm-4.5": 128000,
  "glm-4-air": 128000,
  "glm-4-flash": 128000,
  "doubao-pro-32k": 32768,
  "doubao-1.5-pro-32k": 32768,
  "doubao-seed-1.6": 262144,
  "qwen-max": 32768,
  "qwen-plus": 131072,
  "qwen-turbo": 1000000,
  "qwen2.5-72b-instruct": 131072,
  "step-2-16k": 16384,
  "minimax-text-01": 1000000,
  "abab6.5s-chat": 245760,
  "hunyuan-turbos-latest": 131072,

  /* xAI / Mistral / Meta / 其它 */
  "grok-4": 256000,
  "grok-3": 131072,
  "grok-2-1212": 131072,
  "mistral-large-latest": 131072,
  "mistral-small-latest": 131072,
  "llama-3.3-70b-instruct": 131072,
  "llama-3.1-405b-instruct": 131072,
  "llama-3.1-70b-instruct": 131072,
};

/** 去掉厂商前缀（`openai/gpt-4o`）、大小写与空白，得到查表用的键。 */
export function normalizeModelKey(model: string): string {
  return model
    .trim()
    .toLowerCase()
    .replace(/^[a-z0-9._-]+\//, "")
    .replace(/\s+/g, "");
}

/**
 * 归一化键的各种写法。
 *
 * `:` 是 Ollama 的 tag 分隔符（`qwen2.5:7b`），一并折成 `-` 才能命中表里的键。
 */
export function contextLookupCandidates(key: string): string[] {
  const out = new Set<string>();
  const push = (value: string) => {
    if (value) out.add(value);
  };
  const dotted = key.replace(/:/g, "-");
  push(dotted);
  push(dotted.replace(/\./g, "-"));
  push(dotted.replace(/-/g, "."));

  // 日期 / 版本尾巴：`-2024-08-06`、`-20250806`、`-latest`、`-preview`
  const stripped = dotted
    .replace(/-\d{4}-\d{2}-\d{2}$/, "")
    .replace(/-\d{8}$/, "")
    .replace(/-(?:latest|preview)$/, "");
  push(stripped);
  push(stripped.replace(/\./g, "-"));
  push(stripped.replace(/-/g, "."));
  return [...out];
}

/**
 * 命中常见模型则返回其上下文窗口，否则 `null`。
 *
 * 三级查找：① 变体精确命中 ② 已归一化键的原样命中 ③ 最长前缀回退
 * （第 ③ 级是为了接住 `claude-sonnet-4-5-20250929` 这类带快照日期的名字）。
 */
export function resolveKnownContextTokens(model: string): number | null {
  const key = normalizeModelKey(model);
  if (!key) return null;

  for (const candidate of contextLookupCandidates(key)) {
    const hit = KNOWN_CONTEXT_TOKENS[candidate];
    if (hit !== undefined) return hit;
  }

  let bestKey = "";
  let bestValue: number | null = null;
  for (const [known, value] of Object.entries(KNOWN_CONTEXT_TOKENS)) {
    if (key === known || key.startsWith(`${known}-`)) {
      if (known.length > bestKey.length) {
        bestKey = known;
        bestValue = value;
      }
    }
  }
  return bestValue;
}

/** `128000` → `128,000`；用于表单提示，不参与计算。 */
export function formatTokenCount(value: number): string {
  return value.toLocaleString("en-US");
}
