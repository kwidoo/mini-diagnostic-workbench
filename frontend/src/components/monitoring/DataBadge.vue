<script setup lang="ts">
import { computed } from 'vue'

/** One visual vocabulary for where a piece of data comes from. Never a colour alone: always a word. */
const props = defineProps<{ kind: string | null | undefined }>()
const KINDS: Record<string, { text: string; cls: string; title: string }> = {
  measured: { text: 'measured', cls: 'text-good', title: 'value reported by the ECU (or, for ATRV, the adapter), decoded with a verified formula' },
  observed: { text: 'observed', cls: 'text-series-1', title: 'transition seen directly in measured data (e.g. RPM fell to 0)' },
  manual: { text: 'manual', cls: 'text-ink', title: 'marked by a person' },
  inferred: { text: 'inferred · possible', cls: 'text-warn', title: 'heuristic interpretation of indirect evidence; never confirmed' },
  raw_only: { text: 'raw only', cls: 'text-ink-2', title: 'the ECU answers, but no verified decoder exists: bytes stored, no value' },
  not_available: { text: 'not available', cls: 'text-ink-2 line-through decoration-1', title: 'not offered by this ECU via generic OBD, or manufacturer-only' },
  no_valid_answer: { text: 'no valid answer', cls: 'text-crit', title: 'advertised, but every read failed' },
  not_measured: { text: 'not measured', cls: 'text-ink-2', title: 'no sample in this session' },
}
const k = computed(() => KINDS[props.kind ?? ''] ?? { text: props.kind ?? 'unknown', cls: 'text-ink-2', title: '' })
</script>

<template>
  <span class="badge" :class="k.cls" :title="k.title" :data-kind="kind" data-test="data-badge">{{ k.text }}</span>
</template>
