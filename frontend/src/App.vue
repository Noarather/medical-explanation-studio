<script setup lang="ts">
import { onMounted, provide, ref } from "vue"
import AppSidebar from "./components/AppSidebar.vue"
import TopBar from "./components/TopBar.vue"
import JobsDrawer from "./components/JobsDrawer.vue"
import SettingsDialog from "./components/SettingsDialog.vue"
import UiToast from "./components/UiToast.vue"
import type { BridgeError } from "./lib/bridge"
import { useJobsStore } from "./stores/jobs"

const jobsOpen = ref(false)
const settingsOpen = ref(false)
const toastRef = ref<InstanceType<typeof UiToast> | null>(null)
const jobs = useJobsStore()

provide("openSettings", () => { settingsOpen.value = true })
provide("openJobs", () => { jobsOpen.value = true })
function toast(message: string, tone?: "default" | "success" | "destructive") {
  toastRef.value?.push(message, tone)
}
provide("toast", toast)

onMounted(() => { jobs.init().catch((error) => toast((error as BridgeError).message ?? String(error), "destructive")) })
</script>

<template>
  <div class="flex h-screen overflow-hidden">
    <AppSidebar @open-settings="settingsOpen = true" />
    <div class="flex min-w-0 flex-1 flex-col">
      <TopBar @toggle-jobs="jobsOpen = !jobsOpen" />
      <main class="min-h-0 flex-1 overflow-y-auto bg-background p-6">
        <router-view />
      </main>
    </div>
    <JobsDrawer v-model:open="jobsOpen" />
    <SettingsDialog v-model:open="settingsOpen" />
    <UiToast ref="toastRef" />
  </div>
</template>
