<script setup lang="ts">
import { ref, watch } from 'vue'
import { useWorkbench } from '../stores/workbench'

const wb = useWorkbench()
const famName = (f: string) => (f === 'can' ? 'CAN (ISO 15765-4)' : f === 'kline' ? 'K-line (ISO 9141-2 / ISO 14230-4)' : f)
const mode = ref<'real' | 'simulator'>('real')
const host = ref('192.168.0.10')
const port = ref(35000)
const scenario = ref('')
watch(() => wb.profile, (p) => (scenario.value = p?.scenarios[0]?.name ?? ''), { immediate: true })

function connect() {
  return wb.connect(mode.value === 'real' ? { mode: 'real', host: host.value, port: port.value } : { mode: 'simulator', scenario: scenario.value })
}
</script>

<template>
  <div class="grid gap-3 md:grid-cols-2">
    <section class="panel">
      <h2 class="h2">Adapter connection</h2>
      <div class="mb-3 flex gap-4 text-sm">
        <label><input v-model="mode" type="radio" value="real" /> Vgate iCar Pro Wi-Fi</label>
        <label><input v-model="mode" type="radio" value="simulator" data-test="mode-sim" /> Simulator (synthetic)</label>
      </div>
      <div v-if="mode === 'real'" class="flex flex-wrap items-end gap-2">
        <label class="text-sm"><span class="label block">Adapter IP</span><input v-model="host" class="field w-40" data-test="host" /></label>
        <label class="text-sm"><span class="label block">TCP port</span><input v-model.number="port" type="number" class="field w-24" data-test="port" /></label>
      </div>
      <label v-else class="text-sm">
        <span class="label block">Scenario for {{ wb.profile?.name }}</span>
        <select v-model="scenario" class="field" data-test="scenario">
          <option v-for="s in wb.profile?.scenarios ?? []" :key="s.name" :value="s.name">{{ s.name }}: {{ s.description }}</option>
        </select>
      </label>
      <div class="mt-3 flex gap-2">
        <button class="btn-primary" data-test="connect" :disabled="!!wb.busy || wb.connected" @click="connect">Connect</button>
        <button
          class="btn" data-test="disconnect" :disabled="!!wb.busy || !wb.connected || !!wb.disconnectLockReason"
          :title="wb.disconnectLockReason ?? ''" @click="wb.disconnect()"
        >Disconnect</button>
      </div>
      <p v-if="wb.connected && wb.disconnectLockReason" class="mt-2 text-sm text-warn" data-test="disconnect-hint">{{ wb.disconnectLockReason }}</p>
      <p class="mt-3 text-sm" data-test="conn-state">
        State: <b>{{ wb.connectionState }}</b>
        <span v-if="wb.status?.transport?.reconnects"> · {{ wb.status.transport.reconnects }} reconnect(s)</span>
        <span v-if="wb.status?.transport?.resyncs" data-test="resyncs">
          · {{ wb.status.transport.resyncs }} resynchronisation(s) after incomplete replies (affected readings are marked, never reused)
        </span>
        <span v-if="wb.status?.transport?.last_error" class="text-crit"> · last error: {{ wb.status.transport.last_error }}</span>
      </p>
    </section>

    <section class="panel text-sm">
      <h2 class="h2">Wi-Fi troubleshooting</h2>
      <ol class="list-decimal space-y-1 pl-5">
        <li>Plug the adapter into the OBD socket and switch the ignition on.</li>
        <li>On the Mac, join the adapter's Wi-Fi network. Default SSID: <b>V-LINK</b>.</li>
        <li>Defaults are <b>192.168.0.10</b>, port <b>35000</b>. Change them above if your adapter differs.</li>
        <li>While joined to the adapter the Mac has no Internet. That is expected; this tool does not need it.</li>
        <li>"Cannot reach adapter": wrong Wi-Fi network, or the adapter is asleep. Cycle the ignition.</li>
        <li>"No ECU answered": the adapter is fine but the car is not talking. Check ignition ON and seating.</li>
      </ol>
    </section>

    <section v-if="wb.status?.adapter" class="panel md:col-span-2" data-test="adapter">
      <h2 class="h2">Adapter and ECU metadata</h2>
      <dl class="grid gap-x-6 gap-y-1 text-sm md:grid-cols-2">
        <div><dt class="label">Adapter identity</dt><dd>{{ wb.status.adapter.identity ?? 'not reported' }}</dd></div>
        <div><dt class="label">Description</dt><dd>{{ wb.status.adapter.device_description ?? 'not reported' }}</dd></div>
        <div><dt class="label">Protocol (read back from adapter)</dt><dd>{{ wb.status.adapter.protocol_name }} [{{ wb.status.adapter.protocol_number }}]</dd></div>
        <div><dt class="label">Adapter supply voltage</dt><dd>{{ wb.status.adapter.voltage ?? 'not reported' }}</dd></div>
      </dl>
      <p v-if="wb.profile && !wb.profile.expected_protocol_families.includes(wb.status.adapter.is_can ? 'can' : 'kline')" class="mt-2 text-sm text-warn">
        Note: this vehicle profile expected {{ wb.profile.expected_protocol_families.map(famName).join(' / ') }}, the adapter reports a different protocol family.
      </p>
      <table v-if="wb.status.capabilities" class="mt-3 w-full text-left text-sm">
        <thead><tr class="label"><th>ECU</th><th>Live PIDs</th><th>Freeze-frame PIDs</th><th>Info types</th></tr></thead>
        <tbody>
          <tr v-for="e in wb.status.capabilities.ecus" :key="e.ecu">
            <td>{{ e.ecu }}<span v-if="e.ecu === wb.status.capabilities.primary_ecu" class="text-ink-2"> (primary)</span></td>
            <td>{{ e.mode01_pids.length }}</td><td>{{ e.mode02_pids.length }}</td><td>{{ e.mode09_pids.length }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="wb.status.capabilities" class="mt-2 text-xs text-ink-2">
        OBD services:
        <span v-for="(st, m) in wb.status.capabilities.modes" :key="m" class="mr-2">{{ m }}: {{ st === 'supported' ? 'supported' : st === 'not_probed' ? 'read on scan' : `not supported (${st})` }}</span>
      </p>
      <details class="mt-2 text-xs text-ink-2">
        <summary class="cursor-pointer">Initialisation log (adapter-only AT commands)</summary>
        <div v-for="(l, i) in wb.status.adapter.init_log" :key="i" class="font-mono">{{ l.command }} → {{ l.response || l.status }}</div>
      </details>
    </section>
  </div>
</template>
