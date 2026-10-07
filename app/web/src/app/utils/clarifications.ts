/**
 * 澄清问题的轻量纯函数：按问题文本去重，并过滤掉用户已回答过的文本。
 * 保证“自由对话后不重复提问”，同时保持原始出现顺序。
 */

/** 澄清问题所需的最小结构（与 ConversationClarification 兼容）。 */
export interface ClarificationLike {
  id: string;
  question: string;
}

/** 过滤已回答问题并按问题文本去重；保持出现顺序。 */
export function dedupeClarifications<T extends ClarificationLike>(
  items: readonly T[],
  answeredQuestionTexts: readonly string[] = []
): T[] {
  const answered = new Set(answeredQuestionTexts);
  const seen = new Set<string>();
  const result: T[] = [];
  for (const item of items) {
    if (answered.has(item.question)) continue;
    if (seen.has(item.question)) continue;
    seen.add(item.question);
    result.push(item);
  }
  return result;
}
