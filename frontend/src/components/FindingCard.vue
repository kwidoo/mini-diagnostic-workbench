<script setup lang="ts">
import type { Finding } from '../types'
defineProps<{ finding: Finding }>()
const tone = { insufficient: 'text-ink-2', suggestive: 'text-warn', strong: 'text-crit' } as const
</script>

<template>
  <article class="panel" data-test="finding">
    <header class="mb-2 flex flex-wrap items-start justify-between gap-2">
      <h3 class="text-base font-semibold">{{ finding.title }}</h3>
      <span class="badge" :class="tone[finding.strength]" data-test="strength">Evidence: {{ finding.strength }}</span>
    </header>
    <p class="text-sm"><span class="label">Hypothesis</span><br />{{ finding.hypothesis }}</p>
    <p class="mt-2 text-sm text-ink-2">{{ finding.explanation }}</p>
    <div class="mt-3 grid gap-3 md:grid-cols-2">
      <section>
        <h4 class="label">Supporting signals</h4>
        <ul class="list-disc pl-5 text-sm"><li v-for="e in finding.supporting" :key="e.text">{{ e.text }}</li></ul>
        <p v-if="!finding.supporting.length" class="text-sm text-ink-2">None recorded.</p>
      </section>
      <section>
        <h4 class="label">Contradicting signals</h4>
        <ul class="list-disc pl-5 text-sm"><li v-for="e in finding.contradicting" :key="e.text">{{ e.text }}</li></ul>
        <p v-if="!finding.contradicting.length" class="text-sm text-ink-2">None recorded.</p>
      </section>
    </div>
    <section v-if="finding.conditions.length" class="mt-3">
      <h4 class="label">Conditions and measured context</h4>
      <ul class="list-disc pl-5 text-sm"><li v-for="c in finding.conditions" :key="c">{{ c }}</li></ul>
    </section>
    <section v-if="finding.caveats.length" class="mt-3">
      <h4 class="label">Uncertainty and measurement caveats</h4>
      <ul class="list-disc pl-5 text-sm text-ink-2"><li v-for="c in finding.caveats" :key="c">{{ c }}</li></ul>
    </section>
    <section v-if="finding.next_steps.length" class="mt-3">
      <h4 class="label">Next safe test or inspection</h4>
      <ol class="list-decimal pl-5 text-sm"><li v-for="c in finding.next_steps" :key="c">{{ c }}</li></ol>
    </section>
  </article>
</template>
