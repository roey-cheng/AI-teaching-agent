import type { SVGProps } from 'react'

type IconName =
  | 'brain'
  | 'chat'
  | 'check'
  | 'chevron'
  | 'close'
  | 'edit'
  | 'logout'
  | 'memory'
  | 'menu'
  | 'plus'
  | 'retry'
  | 'send'
  | 'sparkle'
  | 'user'

const paths: Record<IconName, React.ReactNode> = {
  brain: <><path d="M9.5 4.5A3.5 3.5 0 0 0 6 8v.2A3.2 3.2 0 0 0 4 11a3 3 0 0 0 2 2.83V15a3 3 0 0 0 3.5 2.96"/><path d="M14.5 4.5A3.5 3.5 0 0 1 18 8v.2a3.2 3.2 0 0 1 2 2.8 3 3 0 0 1-2 2.83V15a3 3 0 0 1-3.5 2.96M12 4v16M8 9.5c1 0 2 .5 2 1.5M16 9.5c-1 0-2 .5-2 1.5M8.5 15.5c.8 0 1.5-.3 1.5-1M15.5 15.5c-.8 0-1.5-.3-1.5-1"/></>,
  chat: <path d="M21 12a8 8 0 0 1-8 8H6l-4 2 1.4-4.2A9 9 0 1 1 21 12Z" />,
  check: <path d="m5 12 4 4L19 6" />,
  chevron: <path d="m9 18 6-6-6-6" />,
  close: <path d="M18 6 6 18M6 6l12 12" />,
  edit: <><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z"/></>,
  logout: <><path d="M10 17l5-5-5-5M15 12H3"/><path d="M14 3h5a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-5"/></>,
  memory: <><rect x="5" y="5" width="14" height="14" rx="3"/><path d="M9 9h6v6H9zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></>,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  plus: <path d="M12 5v14M5 12h14" />,
  retry: <><path d="M20 7v5h-5"/><path d="M19 12a7 7 0 1 0-2 5"/></>,
  send: <><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></>,
  sparkle: <><path d="m12 3 1.1 3.4a5 5 0 0 0 3.2 3.2L20 11l-3.7 1.4a5 5 0 0 0-3.2 3.2L12 19l-1.1-3.4a5 5 0 0 0-3.2-3.2L4 11l3.7-1.4a5 5 0 0 0 3.2-3.2Z"/></>,
  user: <><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></>,
}

export function Icon({ name, ...props }: { name: IconName } & SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height="20"
      viewBox="0 0 24 24"
      width="20"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.8"
      {...props}
    >
      {paths[name]}
    </svg>
  )
}
