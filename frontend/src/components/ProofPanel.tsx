/**
 * The inclusion proof, rendered as a walk a person can follow.
 *
 * The point is not that a hash matches — it is that the audience can watch a
 * leaf climb to a root that was printed before anyone scanned anything. So
 * each rung shows the sibling being folded in, which side it sat on, and the
 * running hash afterwards. The last line is the committed root, and it either
 * matches or it visibly does not.
 */

export interface WalkStep {
  hash: string
  position: 'left' | 'right'
  computed: string
}

export interface Walk {
  ok: boolean
  steps: WalkStep[]
  computed_root: string
  failure?: string
}

const FAILURE_COPY: Record<string, string> = {
  index: 'The leaf index is not a valid position in this tree.',
  tree_size: 'The leaf index falls outside the committed tree.',
  proof_length: 'The proof has the wrong number of rungs for this tree depth.',
  position: 'A sibling is on the wrong side for this leaf index.',
  malformed: 'A hash in the proof is not well formed.',
  root_mismatch: 'The walk completed but did not reach the committed root.',
}

function Hash({ value, tone = 'ink' }: { value: string; tone?: 'ink' | 'pass' | 'fail' }) {
  const cls =
    tone === 'pass' ? 'text-pass' : tone === 'fail' ? 'text-fail' : 'text-ink-soft'
  return (
    <span className={`break-all font-mono text-[11px] leading-relaxed ${cls}`}>
      {value}
    </span>
  )
}

export default function ProofPanel({
  walk,
  leafHash,
  leafIndex,
  root,
  treeSize,
  committedAt,
}: {
  walk: Walk
  leafHash: string
  leafIndex: number
  root: string
  treeSize?: number | null
  committedAt?: string | null
}) {
  return (
    <section className="rounded-2xl border border-hairline bg-white p-4">
      <header className="mb-3 flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-ink">Inclusion proof</h2>
        <span className={`text-xs font-medium ${walk.ok ? 'text-pass' : 'text-fail'}`}>
          {walk.ok ? 'reproduces the committed root' : 'does not verify'}
        </span>
      </header>

      {!walk.ok && walk.failure && (
        <p className="mb-3 rounded-lg border border-fail/30 bg-fail/5 p-2.5 text-sm text-fail">
          {FAILURE_COPY[walk.failure] ?? walk.failure}
        </p>
      )}

      <ol className="space-y-2.5">
        <li>
          <p className="text-xs text-ink-soft">
            leaf {leafIndex}
            {treeSize ? ` of ${treeSize}` : ''}
          </p>
          <Hash value={leafHash} />
        </li>
        {walk.steps.map((s, i) => (
          <li key={i} className="border-l-2 border-hairline pl-3">
            <p className="text-xs text-ink-soft">
              fold sibling on the <strong className="font-medium">{s.position}</strong>
            </p>
            <Hash value={s.hash} />
            <p className="mt-1 text-xs text-ink-soft">→ running hash</p>
            <Hash
              value={s.computed}
              tone={i === walk.steps.length - 1 ? (walk.ok ? 'pass' : 'fail') : 'ink'}
            />
          </li>
        ))}
      </ol>

      <div className="mt-4 border-t border-hairline pt-3">
        <p className="text-xs text-ink-soft">committed root</p>
        <Hash value={root} tone={walk.ok ? 'pass' : 'fail'} />
        {committedAt && (
          <p className="mt-2 text-xs text-ink-soft">
            committed {new Date(committedAt).toLocaleString()} — before this code
            was ever scanned.
          </p>
        )}
      </div>
    </section>
  )
}
