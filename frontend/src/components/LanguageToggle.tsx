import { useLanguage } from '../i18n'

export function LanguageToggle({ compact = false }: { compact?: boolean }) {
  const { language, setLanguage } = useLanguage()
  return (
    <div className={`language-toggle ${compact ? 'compact' : ''}`} aria-label="Language / 语言" role="group">
      <button aria-pressed={language === 'zh'} className={language === 'zh' ? 'active' : ''} onClick={() => setLanguage('zh')} type="button">中</button>
      <button aria-pressed={language === 'en'} className={language === 'en' ? 'active' : ''} onClick={() => setLanguage('en')} type="button">EN</button>
    </div>
  )
}
