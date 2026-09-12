import { defineStore } from "pinia"
import { invoke } from "../lib/bridge"

export const useSettingsStore = defineStore("settings", {
  state: () => ({
    settings: {} as Record<string, string>,
    credentials: { deepseek: false, dashscope: false, claude: false, gemini: false, custom: false } as Record<string, boolean>,
    loaded: false,
  }),
  actions: {
    async load() {
      const data = await invoke<{ settings: Record<string, string>; credentials: Record<string, boolean> }>("settings", "get")
      this.settings = data.settings
      this.credentials = data.credentials
      this.loaded = true
    },
    async save(settings: Record<string, string>, credentials: Record<string, string>) {
      await invoke("settings", "save", { settings, credentials })
      await this.load()
    },
    async test(provider: string, settings: Record<string, string>, api_key: string, request_id: string) {
      await invoke("settings", "test", { provider, settings, api_key, request_id })
    },
  },
})
