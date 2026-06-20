---
id: concept_2026_06_20_cogbot_silence_on_inner_think_failure
title: 2026-06-20-cogbot-silence-on-inner-think-failure
aliases: []
type: decision
moc:
tags: [cogbot, 엘봇, design-principle, authenticity, silence, degraded-shadow]
status: draft
created: 2026-06-20
version: 1.0
node_id: 66250812-7c7a-4399-afbc-a6ec2f35d5f5
---

# 2026-06-20-cogbot-silence-on-inner-think-failure

**결정.** cogbot(엘봇)에서 내적 사고(속마음 / shadow monologue) 호출이 실패하면(frame `source="degraded"`), 봇은 **응답하지 않는다(침묵)**. 정적 페르소나로 대체 응답을 합성하는 fallback은 도입하지 않는다. 이것이 기본이자 유일한 동작이다.

**근거(오너 결정).** 그게 사람이다 — 에너지가 없어 생각을 못 하면 대답이 없어도 어쩔 수 없다. 빈 응답이 상대에게 "무시 / 회피 / 고장"으로 읽히더라도 그것은 정직한 한계이며, 생각 없이 말을 지어내(꼼수로 빈 응답을 우회) 채우는 것은 진정성(authenticity) 원칙에 위배된다. 침묵은 결함이 아니라 설계다.

**메커니즘.** `bot_orchestrator._process_trigger_inner`에서 `unconscious_frame.source == "degraded"`이면 `_act` 진입 이전에 halt하고 빈 문자열을 반환한다. 관찰 가능: `session_state["last_generation_suppressed"]=True`, `last_generation_suppression_reason="shadow_unavailable:<reason>"`, WARNING 로그. (프로덕션 2026-06-19: degraded 34/34건이 빈 응답 = affect 시도 턴의 16.6%.)

**철회 기록.** 2026-06-20 cogbot(Artificial-Consciousness) PR #32에 잠시 포함했던 **P0-2 "degraded → persona fallback (`COGBOT_DEGRADED_PERSONA_FALLBACK` 기본 ON)"** 변경을 오너가 거절하여 revert했다. 기존 halt-to-silence 동작과 회귀 테스트(`test_halts_silently_when_shadow_monologue_fails`)를 그대로 복원. PR #32에는 P0-3(canonical RelationState 관찰성) + stdout 위생만 남았다.

**대안 처방.** "빈 응답 = 무시"라는 사용자 지각 문제는 fallback이 아니라 **신뢰성**으로 접근한다 — inner-think(monologue) LLM 실패율 자체를 낮추는 것(retry / backoff / timeout 튜닝), 즉 봇이 실제로 "생각할 수 있는" 턴을 늘리는 방향. 침묵 자체는 정직한 한계로 수용한다.

## 핵심 엣지

- `[[2026-06-20-cogbot-silence-on-inner-think-failure]] defines [[Artificial Consciousness]]` — inner-think(속마음) 실패 시 침묵(빈 응답)을 cogbot의 설계 불변식으로 정의 — persona fallback(꼼수) 금지

## Sources

- cogbot(Artificial-Consciousness) PR #32 — degraded persona fallback revert
- 프로덕션 turn_trace 분석 2026-06-19 (degraded 34/34 빈 응답)
