import type { ReactNode } from 'react'

/**
 * The system prompt says "never use markdown" and the model mostly obeys, but
 * gpt-oss still reaches for **bold** around a rupee figure often enough that a
 * shopper would see literal asterisks around the price — the one number on
 * screen that has to look right. Stripping is more reliable than asking again.
 *
 * Deliberately not a markdown renderer: this is a shopkeeper's chat message,
 * and the correct output is plain text. Only emphasis markers are removed.
 */
export function stripMarkdown(text: string): string {
  return text
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/(^|\s)\*(?!\s)(.+?)(?<!\s)\*/g, '$1$2')
    .replace(/(^|\s)_(?!\s)(.+?)(?<!\s)_/g, '$1$2')
    .replace(/`(.+?)`/g, '$1')
}

export default function ChatBubble({
  role,
  children,
}: {
  role: 'user' | 'assistant' | 'system'
  children: ReactNode
}) {
  // A system line is the app speaking, not the shop — a released reservation,
  // a failed card. Centred and quiet so it never reads as the shopkeeper.
  if (role === 'system') {
    return (
      <p className="mx-auto max-w-[85%] text-center text-tiny leading-relaxed text-ink-soft">
        {children}
      </p>
    )
  }

  const mine = role === 'user'
  return (
    <div className={mine ? 'flex justify-end' : 'flex justify-start'}>
      <div
        className={[
          'max-w-[85%] whitespace-pre-wrap break-words rounded-2xl px-4 py-2.5',
          'text-[15px] leading-relaxed',
          mine
            ? 'rounded-br-sm bg-ink text-white'
            : 'rounded-bl-sm border border-hairline bg-card text-ink',
        ].join(' ')}
      >
        {typeof children === 'string' && !mine ? stripMarkdown(children) : children}
      </div>
    </div>
  )
}

export function TypingBubble() {
  return (
    <div className="flex justify-start">
      <div className="rounded-2xl rounded-bl-sm border border-hairline bg-card px-4 py-3.5">
        <span className="flex gap-1" aria-label="thinking">
          {[0, 150, 300].map((delay) => (
            <span
              key={delay}
              className="h-1.5 w-1.5 animate-bounce rounded-full bg-accent-strong"
              style={{ animationDelay: `${delay}ms` }}
            />
          ))}
        </span>
      </div>
    </div>
  )
}
