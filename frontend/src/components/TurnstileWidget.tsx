import { useEffect, useRef } from 'react'

type TurnstileApi = {
  render: (container: HTMLElement, options: Record<string, unknown>) => string
  reset: (widgetId?: string) => void
  remove?: (widgetId: string) => void
}

declare global {
  interface Window {
    turnstile?: TurnstileApi
  }
}

const SCRIPT_ID = 'cloudflare-turnstile-script'
const SCRIPT_URL = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'

export function TurnstileWidget({
  language,
  resetVersion,
  onToken,
}: {
  language: 'zh' | 'en'
  resetVersion: number
  onToken: (token: string) => void
}) {
  const container = useRef<HTMLDivElement>(null)
  const widgetId = useRef<string | undefined>(undefined)
  const siteKey = import.meta.env.VITE_TURNSTILE_SITE_KEY as string | undefined

  useEffect(() => {
    if (!siteKey || !container.current) return
    let cancelled = false

    function render() {
      if (cancelled || !container.current || !window.turnstile || widgetId.current) return
      widgetId.current = window.turnstile.render(container.current, {
        sitekey: siteKey,
        action: 'register',
        language: language === 'zh' ? 'zh-CN' : 'en',
        callback: (token: string) => onToken(token),
        'expired-callback': () => onToken(''),
        'error-callback': () => onToken(''),
      })
    }

    const existing = document.getElementById(SCRIPT_ID) as HTMLScriptElement | null
    const script = existing ?? document.createElement('script')
    if (!existing) {
      script.id = SCRIPT_ID
      script.src = SCRIPT_URL
      script.async = true
      script.defer = true
      document.head.appendChild(script)
    }
    if (window.turnstile) render()
    else script.addEventListener('load', render)
    return () => {
      cancelled = true
      script.removeEventListener('load', render)
      if (widgetId.current && window.turnstile?.remove) {
        window.turnstile.remove(widgetId.current)
      }
      widgetId.current = undefined
    }
  }, [language, onToken, siteKey])

  useEffect(() => {
    if (widgetId.current) window.turnstile?.reset(widgetId.current)
  }, [resetVersion])

  if (!siteKey) return null
  return <div className="turnstile-widget" ref={container} />
}
