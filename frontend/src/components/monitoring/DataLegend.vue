<script setup lang="ts">
import DataBadge from './DataBadge.vue'
</script>

<template>
  <p class="flex flex-wrap items-center gap-2 text-xs text-ink-2" data-test="legend">
    <DataBadge kind="measured" /> ECU / adapter value
    <DataBadge kind="observed" /> seen in measured data
    <DataBadge kind="manual" /> marked by a person
    <DataBadge kind="inferred" /> heuristic, never confirmed
    <DataBadge kind="raw_only" /> bytes only, no value
    <DataBadge kind="not_available" /> not offered / manufacturer-only
  </p>
</template>
