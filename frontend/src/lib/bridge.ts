export interface BridgeError { code: string; message: string }

declare global {
  interface Window { qt?: { webChannelTransport: unknown }; QWebChannel?: new (transport: unknown, cb: (channel: any) => void) => void }
}

let channelPromise: Promise<Record<string, any>> | null = null

export function channelReady(): Promise<Record<string, any>> {
  if (!channelPromise) {
    channelPromise = new Promise((resolve, reject) => {
      if (!window.qt || !window.QWebChannel) {
        reject({ code: "no_channel", message: "Qt WebChannel 不可用（请在应用内打开页面）" } as BridgeError)
        return
      }
      new window.QWebChannel(window.qt.webChannelTransport, (channel: any) => resolve(channel.objects))
    })
  }
  return channelPromise
}

export function invoke<T = unknown>(domain: string, method: string, params: Record<string, unknown> = {}): Promise<T> {
  return channelReady().then((objects) => {
    const target = objects[domain]
    if (!target) return Promise.reject({ code: "no_domain", message: `桥接域不存在：${domain}` } as BridgeError)
    return new Promise<T>((resolve, reject) => {
      target.invoke(method, JSON.stringify(params), (raw: string) => {
        try {
          const parsed = JSON.parse(raw)
          if (parsed.ok) resolve(parsed.data as T)
          else reject(parsed.error as BridgeError)
        } catch {
          reject({ code: "bad_response", message: String(raw) } as BridgeError)
        }
      })
    })
  })
}

export function onJobsChanged(cb: (snapshot: { jobs: unknown[] }) => void): Promise<void> {
  return channelReady().then((objects) => {
    objects.jobEvents.jobs_changed.connect((json: string) => cb(JSON.parse(json)))
  })
}

export function onApiTestResult(cb: (result: { provider: string; request_id?: string; ok: boolean; message: string; elapsed_ms?: number }) => void): Promise<void> {
  return channelReady().then((objects) => {
    objects.settingsEvents.api_test_result.connect((json: string) => cb(JSON.parse(json)))
  })
}

export interface OrganizeSignal {
  token: string
  ok: boolean
  data?: { rows: Record<string, unknown>[]; method: string; warnings?: string[][]; stats?: Record<string, unknown> }
  error?: BridgeError
}

export function onOrganizeResult(cb: (payload: OrganizeSignal) => void): Promise<void> {
  return channelReady().then((objects) => {
    objects.importsEvents.organize_result.connect((json: string) => cb(JSON.parse(json)))
  })
}

export function onOrganizeProgress(cb: (payload: { token: string; current: number; total: number }) => void): Promise<void> {
  return channelReady().then((objects) => {
    objects.importsEvents.organize_progress.connect((json: string) => cb(JSON.parse(json)))
  })
}
