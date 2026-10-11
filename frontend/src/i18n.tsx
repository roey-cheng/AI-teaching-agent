import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

export type Language = 'zh' | 'en'

const messages = {
  zh: {
    login: '登录', register: '注册', welcomeBack: '欢迎回来', loginSubtitle: '继续上一次没有聊完的话题。',
    createSpace: '创建你的学习空间', registerSubtitle: '只需要一分钟，就可以开始对话。',
    displayName: '昵称', namePlaceholder: '希望我们怎么称呼你？', email: '邮箱', password: '密码',
    emailPlaceholder: 'you@example.com', passwordPlaceholder: '输入你的密码', newPasswordPlaceholder: '至少 8 个字符',
    pleaseWait: '请稍等…', registerAndEnter: '注册并进入', noAccount: '还没有账号？', hasAccount: '已经有账号了？',
    registerNow: '立即注册', goLogin: '去登录', authTagline: '会记住你的学习目标与讲解偏好',
    newChat: '新对话', recentChats: '最近对话', noChats: '你的对话会出现在这里。',
    profileMemory: 'Profile Memory', logout: '退出登录', closeSidebar: '关闭侧栏', rename: '重命名',
    openSidebar: '打开侧栏', newConversation: '新对话', agentLabel: 'AI 教学助手', deepAgent: 'Deep Agent',
    loadingHistory: '正在读取对话…', welcomeQuestion: '今天想学点什么？',
    welcomeHint: '问一个概念、贴一段代码，或者告诉我你的学习目标。',
    explainConcept: '解释一个概念', explainPrompt: '用一个生活中的例子解释什么是 API',
    learningPlan: '制定学习计划', planPrompt: '帮我制定一个七天 Python 入门计划',
    readCode: '一起读代码', codePrompt: '教我如何一步一步看懂一段陌生代码',
    retryAnswer: '重试回答', stillRunning: '后端仍在处理', receiving: '正在接收问题',
    modelReasoning: '模型返回的思考', sendPlaceholder: '给 Mentor 发消息', generating: '正在生成回答…',
    sendMessage: '发送消息', composerHint: 'Enter 发送 · Shift + Enter 换行　AI 可能会犯错，请核对重要信息。',
    contextReady: '上下文已准备', agentRunning: 'Agent 正在工作', thinking: '正在思考', answering: '正在组织回答',
    saving: '正在保存对话', memoryChecking: '正在检查个人记忆', memorySaving: '正在保存新记忆',
    memorySaved: '已更新个人记忆', memorySkipped: '无需更新记忆', memoryUnavailable: '记忆暂时不可用',
    memoryIntro: '这些是 Agent 从你明确说过的话中记住的信息。它们会在不同对话中帮助回答更贴合你。',
    loadingMemory: '正在读取记忆…', noMemory: '还没有保存的记忆',
    noMemoryHint: '在聊天中告诉我你的学习目标、背景或回答偏好，它们会在这里出现。',
    memoryPrivate: '记忆仅对当前账号可见', updatedOn: '更新于', closeMemory: '关闭个人记忆',
    learningPreference: '学习偏好', learningGoal: '学习目标', programmingBackground: '编程背景',
    personalBackground: '个人背景', dailyPreference: '日常偏好',
    backendUnavailable: '暂时无法连接后端', reconnect: '重新连接',
    streamEnded: '连接在完成事件到达前结束，已重新读取后端状态。', genericFailure: '回答生成失败。',
    accountActions: '账号操作', closeNotice: '关闭提示',
  },
  en: {
    login: 'Log in', register: 'Sign up', welcomeBack: 'Welcome back', loginSubtitle: 'Continue where you left off.',
    createSpace: 'Create your learning space', registerSubtitle: 'Start your first conversation in a minute.',
    displayName: 'Display name', namePlaceholder: 'What should we call you?', email: 'Email', password: 'Password',
    emailPlaceholder: 'you@example.com', passwordPlaceholder: 'Enter your password', newPasswordPlaceholder: 'At least 8 characters',
    pleaseWait: 'Please wait…', registerAndEnter: 'Sign up and continue', noAccount: 'New to Mentor?', hasAccount: 'Already have an account?',
    registerNow: 'Create an account', goLogin: 'Log in', authTagline: 'Remembers your goals and explanation preferences',
    newChat: 'New chat', recentChats: 'Recent chats', noChats: 'Your conversations will appear here.',
    profileMemory: 'Profile Memory', logout: 'Log out', closeSidebar: 'Close sidebar', rename: 'Rename',
    openSidebar: 'Open sidebar', newConversation: 'New chat', agentLabel: 'AI Teaching Assistant', deepAgent: 'Deep Agent',
    loadingHistory: 'Loading conversation…', welcomeQuestion: 'What would you like to learn?',
    welcomeHint: 'Ask about a concept, share some code, or tell me your learning goal.',
    explainConcept: 'Explain a concept', explainPrompt: 'Explain APIs with an everyday example',
    learningPlan: 'Make a study plan', planPrompt: 'Create a seven-day Python beginner plan',
    readCode: 'Read code together', codePrompt: 'Teach me how to understand unfamiliar code step by step',
    retryAnswer: 'Retry answer', stillRunning: 'The server is still working', receiving: 'Receiving your question',
    modelReasoning: 'Model-provided reasoning', sendPlaceholder: 'Message Mentor', generating: 'Generating a response…',
    sendMessage: 'Send message', composerHint: 'Enter to send · Shift + Enter for a new line　AI can make mistakes. Check important information.',
    contextReady: 'Context is ready', agentRunning: 'Agent is working', thinking: 'Thinking', answering: 'Writing the answer',
    saving: 'Saving conversation', memoryChecking: 'Checking profile memory', memorySaving: 'Saving new memory',
    memorySaved: 'Profile memory updated', memorySkipped: 'No memory update needed', memoryUnavailable: 'Memory is temporarily unavailable',
    memoryIntro: 'These are details the Agent saved from things you explicitly shared. They help tailor answers across conversations.',
    loadingMemory: 'Loading memory…', noMemory: 'No saved memories yet',
    noMemoryHint: 'Share your goals, background, or response preferences in chat and they may appear here.',
    memoryPrivate: 'Only your account can see these memories', updatedOn: 'Updated', closeMemory: 'Close profile memory',
    learningPreference: 'Learning preference', learningGoal: 'Learning goal', programmingBackground: 'Programming background',
    personalBackground: 'Personal background', dailyPreference: 'Daily preference',
    backendUnavailable: 'Unable to connect to the server', reconnect: 'Reconnect',
    streamEnded: 'The connection ended before completion. The latest server state has been reloaded.', genericFailure: 'Response generation failed.',
    accountActions: 'Account actions', closeNotice: 'Close notification',
  },
} as const

export type MessageKey = keyof typeof messages.zh

type LanguageContextValue = {
  language: Language
  setLanguage: (language: Language) => void
  t: (key: MessageKey) => string
}

const LanguageContext = createContext<LanguageContextValue | null>(null)

function initialLanguage(): Language {
  const stored = window.localStorage.getItem('mentor-language')
  if (stored === 'zh' || stored === 'en') return stored
  return navigator.language.toLowerCase().startsWith('zh') ? 'zh' : 'en'
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [language, updateLanguage] = useState<Language>(initialLanguage)

  useEffect(() => {
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en'
  }, [language])

  const value = useMemo<LanguageContextValue>(() => ({
    language,
    setLanguage(next) {
      window.localStorage.setItem('mentor-language', next)
      updateLanguage(next)
    },
    t: (key) => messages[language][key],
  }), [language])

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>
}

export function useLanguage(): LanguageContextValue {
  const context = useContext(LanguageContext)
  if (!context) throw new Error('useLanguage must be used inside LanguageProvider')
  return context
}
