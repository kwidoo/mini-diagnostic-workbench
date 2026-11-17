<script setup lang="ts">
import { computed, ref } from 'vue'
import { useWorkbench } from '../../stores/workbench'

/** Predefined MINI events. Usable while recording, after the engine stopped and after the recording
 *  ended (time = when you press). Always stored as 'manual'. */
const props = defineProps<{ sessionId: string | null; context?: string }>()
const wb = useWorkbench()
const note = ref('')
const events = computed(() => wb.profile?.monitoring_events ?? [])
const PRIMARY = ['fan_running', 'fan_stopped', 'battery_warning', 'battery_warning_cleared', 'engine_stopped', 'ignition_off']
const primary = computed(() => events.value.filter((e) => PRIMARY.includes(e.id)))
const other = computed(() => events.value.filter((e) => !PRIMARY.includes(e.id)))
async function press(id: string) {
  if (!props.sessionId) return
  await wb.markEvent(props.sessionId, id, note.value.trim())
  note.value = ''
}
</script>

<template>
  <div v-if="events.length" data-test="event-bar">
    <p v-if="context" class="mb-2 text-xs text-ink-2" data-test="event-target">{{ context }}</p>
    <div class="flex flex-wrap gap-2">
      <button
        v-for="e in primary" :key="e.id" class="btn" :class="e.id.startsWith('fan') ? 'border-ink' : ''" :title="e.hint"
        :data-test="`event-${e.id}`" :disabled="!sessionId" @click="press(e.id)"
      >{{ e.label }}</button>
    </div>
    <div class="mt-2 flex flex-wrap gap-2">
      <button
        v-for="e in other" :key="e.id" class="btn text-xs" :title="e.hint" :data-test="`event-${e.id}`" :disabled="!sessionId"
        @click="press(e.id)"
      >{{ e.label }}</button>
      <input v-model="note" class="field w-56" placeholder="optional note for the next event" data-test="event-note" />
    </div>
  </div>
</template>
