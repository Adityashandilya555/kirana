import type { ReactNode } from 'react'

export default function ChatBubble({
  role,
  children,
}: {
  role: 'user' | 'assistant' | 'system'
  children: ReactNode
}) {
  if (role === 'system') {
    return (
      <p className="mx-auto max-w-[85%] text-center text-xs leading-relaxed text-ink-soft">
        {children}
      </p>
    )
  }
  const mine = role === 'user'
  return (
    <div className={mine ? 'flex justify-end' : 'flex justify-start'}>
      <div
        className={[
          'max-w-[85%] whitespace-pre-wrap break-words rounded-2xl px-3.5 py-2.5 text-[15px] leading-relaxed',
          mine
            ? 'rounded-br-sm bg-accent text-white'
            : 'rounded-bl-sm border border-hairline bg-white text-ink',
        ].join(' ')}
      >
        {children}
      </div>
    </div>
  )
}

export function TypingBubble() {
  return (
    <div className="flex justify-start">
      <div className="rounded-2xl rounded-bl-sm border border-hairline bg-white px-4 py-3">
        <span className="flex gap-1" aria-label="thinking">
          {[0, 150, 300].map((delay) => (
            <span
              key={delay}
              className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-soft"
              style={{ animationDelay: `${delay}ms` }}
            />
          ))}
        </span>
      </div>
    </div>
  )
}
