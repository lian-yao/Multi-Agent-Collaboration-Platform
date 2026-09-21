/* providerIcons.tsx — Provider 品牌图标映射（preset_type → logo）
 *
 * 资产来源：GitHub `lobehub/lobe-icons` 的静态 SVG 包 `@lobehub/icons-static-svg`
 * （MIT 授权）。需要的 22 个品牌图标已离线内联进 `providerIcons.gen.ts`（纯 SVG 字符串，
 * 无 React / 无 antd 依赖），避免 `@lobehub/icons` v5 经 peer `@lobehub/ui` 重新导出
 * 整个图标桶导致的 bundle 膨胀（343KB → 1118KB）。
 */
import { Plus } from "lucide-react";
import { Monogram } from "./shared";
import {
  anthropicRaw,
  azureColorRaw,
  bedrockColorRaw,
  cerebrasColorRaw,
  deepseekColorRaw,
  doubaoColorRaw,
  geminiColorRaw,
  grokRaw,
  groqRaw,
  hunyuanColorRaw,
  kimiColorRaw,
  lmstudioRaw,
  minimaxColorRaw,
  mistralColorRaw,
  ollamaRaw,
  openaiRaw,
  openrouterColorRaw,
  perplexityColorRaw,
  siliconcloudColorRaw,
  stepfunColorRaw,
  togetherColorRaw,
  zhipuColorRaw,
} from "./providerIcons.gen";

const PRESET_ICONS: Record<string, string> = {
  openai: openaiRaw,
  anthropic: anthropicRaw,
  gemini: geminiColorRaw,
  xai: grokRaw,
  mistral: mistralColorRaw,
  perplexity: perplexityColorRaw,
  groq: groqRaw,
  "together-ai": togetherColorRaw,
  cerebras: cerebrasColorRaw,
  deepseek: deepseekColorRaw,
  moonshot: kimiColorRaw,
  zhipu: zhipuColorRaw,
  doubao: doubaoColorRaw,
  siliconflow: siliconcloudColorRaw,
  stepfun: stepfunColorRaw,
  minimax: minimaxColorRaw,
  hunyuan: hunyuanColorRaw,
  openrouter: openrouterColorRaw,
  "azure-openai": azureColorRaw,
  "amazon-bedrock": bedrockColorRaw,
  ollama: ollamaRaw,
  "lm-studio": lmstudioRaw,
};

/**
 * 「自定义」这条预设没有品牌可画，用加号表达「自己填一个端点」，
 * 而不是把中文「自定义」当 monogram 塞进方块 —— 三字会缩到很小且和品牌标不同形。
 *
 * 值与后端 `app/core/model_registry.py` 的 `PROVIDER_PRESETS` 键一致（`doc/api.md` §5.12）。
 */
export const CUSTOM_PRESET_TYPE = "openai-compatible";

export function hasProviderIcon(presetType: string): boolean {
  return presetType in PRESET_ICONS;
}

/** 该预设是否走「加号」占位（自定义端点 / 预设目录里没有图标的兜底项）。 */
export function isCustomPreset(presetType: string): boolean {
  return presetType === CUSTOM_PRESET_TYPE;
}

/**
 * 品牌有图标 → 白底方块里放品牌标；自定义 → 加号方块；否则回退 `Monogram` 字母块。
 * SVG 自带 `width="1em" height="1em"`，通过外层 font-size 控制实际尺寸；
 * `fill="currentColor"` 的 mono 标会跟随 `cfg-mark` 的文字色。
 */
export function ProviderMark({
  presetType,
  label,
  tint,
  size = 30,
}: {
  presetType: string;
  label: string;
  tint?: string | null;
  size?: number;
}) {
  if (isCustomPreset(presetType)) {
    return (
      <span
        className="cfg-mark cfg-mark-add"
        style={{ width: size, height: size }}
        aria-hidden="true"
      >
        <Plus size={Math.round(size * 0.46)} strokeWidth={2.4} />
      </span>
    );
  }
  const raw = PRESET_ICONS[presetType];
  if (!raw) return <Monogram text={label} tint={tint} size={size} />;
  return (
    <span
      className="cfg-mark"
      style={{ width: size, height: size, fontSize: Math.round(size * 0.6) }}
      aria-hidden="true"
      // SVG 来自 lobe-icons（MIT、可信静态资产），内联安全。
      dangerouslySetInnerHTML={{ __html: raw }}
    />
  );
}
